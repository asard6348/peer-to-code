import collections
import itertools
import threading
import time

import ot
from chat_util import CHAT_MAX_LEN, NAME_MAX_LEN, sanitize_name, sanitize_text, unique_name
import share_util
from . import transport as p
from .transport import ReliableUDP

# Terminal chat limits. Rate limit is a token bucket: a client may send
# CHAT_RATE_BURST messages at once and then regains tokens at
# CHAT_RATE_BURST / CHAT_RATE_PERIOD per second (5 per 5 seconds = 1/s).
CHAT_HISTORY_MAX = 50
CHAT_RATE_BURST = 5
CHAT_RATE_PERIOD = 5.0
CHAT_MAX_RECIPIENTS = 16

# Upper bound on simultaneously shared documents, so a misbehaving peer
# can't make every other peer allocate tabs without limit.
MAX_DOCS = 32
FILENAME_MAX = 200

COLORS = ["#e06c75", "#61afef", "#98c379", "#e5c07b", "#c678dd", "#56b6c2", "#d19a66", "#be5046"]


class ClientRecord:
    def __init__(self, client_id, addr, username, color, p2p_host=None, p2p_port=None, is_sequencer=False):
        self.client_id = client_id
        self.addr = addr
        self.username = username
        self.color = color
        self.cursors = {}  # doc id -> last cursor this client reported in that document
        self.p2p_host = p2p_host
        self.p2p_port = p2p_port
        self.is_sequencer = is_sequencer
        self.chat_tokens = None  # token bucket, filled lazily (see Server._chat_allow)
        self.chat_stamp = 0.0
        self.share = None  # the runner's current/last shared run (see Server._handle_share)


class DocState:
    """One shared document: its text, its own op history and revision
    counter (ops are numbered per document, never across them) and the
    name peers show for it."""

    def __init__(self, text="", filename=None):
        self.doc = text
        self.history = []
        self.revision = 0
        self.filename = filename


class Server:
    def __init__(self, port, initial_doc="", max_clients=32, sock=None, initial_docs=None):
        """`initial_docs` ({doc id: (text, filename)}) seeds the session with
        several documents at once - how a peer promoted to sequencer after a
        failover carries every shared buffer over under the same ids, so
        everyone else's tabs still line up. Without it the session starts
        with the single, empty (or `initial_doc`) document MAIN_DOC."""
        self.port = port
        self.docs = {}
        if initial_docs:
            for doc_id, (text, filename) in initial_docs.items():
                self.docs[int(doc_id)] = DocState(text, filename)
        self.docs.setdefault(p.MAIN_DOC, DocState(initial_doc))
        self._next_doc_id = max(self.docs) + 1
        self.clients = {}
        self.clients_by_addr = {}
        self._id_gen = itertools.count(1)
        self._lock = threading.RLock()
        self.max_clients = max_clients
        self.transport = ReliableUDP(("0.0.0.0", port), sock=sock)
        self.transport.on_message = self._on_message
        self.transport.on_foreign_version = self._on_foreign_version
        self._refused_versions = {}     # addr -> when we last logged refusing it
        self.transport.on_peer_timeout = self._on_peer_timeout
        self.log = []
        self.on_log = None
        self._heartbeat_stop = threading.Event()
        self.chat_seq = 0
        self.chat_history = collections.deque(maxlen=CHAT_HISTORY_MAX)  # public messages only
        self.chat_rate_burst = CHAT_RATE_BURST
        self.chat_rate_period = CHAT_RATE_PERIOD
        self._clock = time.monotonic

    def start(self):
        self.transport.start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(2.0):
            with self._lock:
                addrs = [c.addr for c in self.clients.values()]
            for addr in addrs:
                self.transport.send(addr, p.PING, {}, reliable=True)

    def stop(self):
        self._heartbeat_stop.set()
        self._broadcast(p.HOST_SHUTDOWN, {}, reliable=False)
        time.sleep(0.05)
        self.transport.stop()

    def _on_foreign_version(self, addr, version, kind, seq, flags):
        """Someone on another protocol version is trying to join. They can't
        be served, but they can be told why: an older client shows the
        reason of a HELLO_REJECT it receives (in its own dialect) as the
        connection error, instead of waiting for a timeout."""
        if kind != p.HELLO:
            return
        if flags & p.FLAG_RELIABLE:
            self.transport.send_as_version(addr, version, p.ACK, seq)      # stop its retransmitting HELLO
        self.transport.send_as_version(addr, version, p.HELLO_REJECT, 0,
                                       {"reason": p.mismatch_message(version, host_is_theirs=False)})
        now = time.monotonic()
        if now - self._refused_versions.get(addr, -1e9) > 30:
            self._refused_versions[addr] = now
            self._emit_log(f"Refused {addr[0]}:{addr[1]}: it runs protocol version {version}, "
                           f"this host runs version {p.PROTOCOL_VERSION}")

    def _emit_log(self, msg):
        self.log.append(msg)
        if self.on_log:
            self.on_log(msg)

    def _broadcast(self, kind, payload, exclude_addr=None, reliable=True):
        with self._lock:
            targets = [c.addr for c in self.clients.values() if c.addr != exclude_addr]
        for addr in targets:
            self.transport.send(addr, kind, payload, reliable=reliable)

    def _roster_payload(self):
        with self._lock:
            return {"peers": [
                {"id": c.client_id, "name": c.username, "color": c.color, "cursors": list(c.cursors.values()),
                 "p2p_host": c.p2p_host, "p2p_port": c.p2p_port, "is_sequencer": c.is_sequencer}
                for c in self.clients.values()
            ]}

    def _on_message(self, kind, payload, addr):
        if kind == p.HELLO:
            self._handle_hello(payload, addr)
        elif kind == p.OP:
            self._handle_op(payload, addr)
        elif kind == p.CURSOR:
            self._handle_cursor(payload, addr)
        elif kind == p.BUFFER_CONTEXT:
            self._handle_buffer_context(payload, addr)
        elif kind == p.DOC_OPEN:
            self._handle_doc_open(payload, addr)
        elif kind == p.CHAT:
            self._handle_chat(payload, addr)
        elif kind in (p.RUN_START, p.RUN_OUTPUT, p.RUN_END):
            self._handle_share(kind, payload, addr)
        elif kind == p.PING:
            self.transport.send(addr, p.PONG, {}, reliable=False)
        elif kind == p.GOODBYE:
            self._handle_goodbye(addr)

    def _handle_hello(self, payload, addr):
        with self._lock:
            if len(self.clients) >= self.max_clients:
                self.transport.send(addr, p.HELLO_REJECT, {"reason": "Session is full."}, reliable=True)
                return
            # Names are what chat targets, the roster lists and cursors
            # label, so they're sanitized and made unique right here (bob,
            # bob#2, ...) and everything downstream uses this final name.
            username = unique_name(sanitize_name(payload.get("username", "anon"), NAME_MAX_LEN),
                                   [c.username for c in self.clients.values()], NAME_MAX_LEN)
            client_id = next(self._id_gen)
            color = COLORS[(client_id - 1) % len(COLORS)]
            p2p_host = payload.get("p2p_host")
            p2p_port = payload.get("p2p_port")
            is_sequencer = bool(payload.get("is_sequencer"))
            rec = ClientRecord(client_id, addr, username, color,
                                p2p_host=p2p_host, p2p_port=p2p_port, is_sequencer=is_sequencer)
            self.clients[client_id] = rec
            self.clients_by_addr[addr] = client_id
            snapshot = [(doc_id, st.doc, st.revision, st.filename)
                        for doc_id, st in sorted(self.docs.items())]

        self.transport.send(addr, p.WELCOME, {
            "client_id": client_id, "color": color, "username": username,
        }, reliable=True)
        # Every shared document, each carrying the revision it was
        # captured at (ops after that revision follow as normal OPs).
        for doc_id, text, revision, filename in snapshot:
            self._send_doc(addr, doc_id, text, revision, filename)
        with self._lock:
            history = list(self.chat_history)
        if history:
            self.transport.send(addr, p.CHAT, {"history": history}, reliable=True)
        self._send_share_catchup(addr)
        self._broadcast(p.PEERS, self._roster_payload())
        self._emit_log(f"{username} joined from {addr[0]}:{addr[1]}")

    def _doc_id_from(self, payload):
        """The id of an existing document named by `payload["doc"]` (absent
        means MAIN_DOC), else None. Caller holds the lock."""
        doc_id = payload.get("doc", p.MAIN_DOC)
        if isinstance(doc_id, int) and not isinstance(doc_id, bool) and doc_id in self.docs:
            return doc_id
        return None

    def _send_doc(self, addr, doc_id, text, revision, filename, **meta):
        """Sends one document in full: DOC_CHUNKs, each repeating the small
        header (id, revision, filename, ...) so any chunk is self-describing
        and the chunks may arrive in any order."""
        chunks = p.chunk_text(text)
        for i, chunk in enumerate(chunks):
            self.transport.send(addr, p.DOC_CHUNK, {
                "doc": doc_id, "index": i, "total": len(chunks), "text": chunk,
                "revision": revision, "filename": filename, **meta,
            }, reliable=True)

    def _handle_op(self, payload, addr):
        resync = False
        with self._lock:
            client_id = self.clients_by_addr.get(addr)
            if client_id is None:
                return
            doc_id = self._doc_id_from(payload)
            if doc_id is None:
                return
            state = self.docs[doc_id]
            try:
                base_rev = int(payload["base_rev"])
                op = ot.Op.from_json(payload["ops"])
            except (KeyError, ValueError, TypeError):
                return
            swap = payload.get("swap")
            if isinstance(swap, dict):
                filename = swap.get("filename")
                swap = {"filename": filename if isinstance(filename, str) else None}
            else:
                swap = None
            base_rev = max(0, min(base_rev, state.revision))
            try:
                for hist_op in state.history[base_rev:state.revision]:
                    op, _ = ot.transform(op, hist_op)
                state.doc = op.apply(state.doc)
            except ValueError:
                # The op doesn't fit the document it claims to be based on.
                resync = True
            else:
                state.history.append(op)
                applied_at = state.revision
                state.revision += 1
                self._shift_cursors(op, client_id, doc_id)
        if resync:
            self._resend_full_sync(addr, doc_id)
            return

        message = {"doc": doc_id, "base_rev": applied_at, "ops": op.to_json(), "from": client_id}
        if swap is not None:
            message["swap"] = swap
        self._broadcast(p.OP, message, reliable=True)

    def _shift_cursors(self, op, author_id, doc_id):
        """Moves every stored cursor in `doc_id` along with an applied op,
        so the positions handed to someone joining (the roster) still point
        at the same text instead of the offsets last reported before the
        edit."""
        for rec in self.clients.values():
            cur = rec.cursors.get(doc_id)
            if not isinstance(cur, dict):
                continue
            moved = dict(cur)
            mine = rec.client_id == author_id
            if isinstance(cur.get("index"), int):
                moved["index"] = ot.transform_position(op.ops, cur["index"], stick_right=mine)
            if cur.get("sel") and isinstance(cur.get("start"), int) and isinstance(cur.get("end"), int):
                moved["start"] = ot.transform_position(op.ops, cur["start"], stick_right=True)
                moved["end"] = ot.transform_position(op.ops, cur["end"], stick_right=False)
            rec.cursors[doc_id] = moved

    def _resend_full_sync(self, addr, doc_id):
        with self._lock:
            state = self.docs.get(doc_id)
            if state is None:
                return
            revision, text, filename = state.revision, state.doc, state.filename
        self.transport.send(addr, p.WELCOME, {"client_id": self.clients_by_addr.get(addr, 0),
                                              "doc": doc_id, "revision": revision, "resync": True}, reliable=True)
        self._send_doc(addr, doc_id, text, revision, filename)

    @staticmethod
    def _clean_filename(value):
        return sanitize_text(value, FILENAME_MAX) if isinstance(value, str) and value else None

    def _handle_buffer_context(self, payload, addr):
        with self._lock:
            client_id = self.clients_by_addr.get(addr)
            if client_id is None:
                return
            doc_id = self._doc_id_from(payload)
            if doc_id is None:
                return
            filename = self._clean_filename(payload.get("filename"))
            self.docs[doc_id].filename = filename
            ctx = {"doc": doc_id, "filename": filename, "by": client_id}
        self._broadcast(p.BUFFER_CONTEXT, ctx, reliable=True)

    def _handle_doc_open(self, payload, addr):
        """A client shares a new document: it gets the next free id, and
        every client (the sender included, which is how it learns the id)
        receives the text as a fresh document. `ref` is the sender's own
        token for the request and just rides along so it can recognise the
        reply. Whether a recipient shows it as a new tab, or attaches it to
        a tab it already has open with identical content, is up to it."""
        if not isinstance(payload, dict):
            return
        text = payload.get("text")
        ref = payload.get("ref")
        if not isinstance(ref, int) or isinstance(ref, bool):
            ref = None
        if not isinstance(text, str):
            return
        error = None
        with self._lock:
            client_id = self.clients_by_addr.get(addr)
            if client_id is None:
                return
            if len(self.docs) >= MAX_DOCS:
                error = f"Too many shared buffers in this session (limit {MAX_DOCS})."
            else:
                doc_id = self._next_doc_id
                self._next_doc_id += 1
                filename = self._clean_filename(payload.get("filename"))
                self.docs[doc_id] = DocState(text, filename)
                targets = [c.addr for c in self.clients.values()]
        if error:
            self.transport.send(addr, p.DOC_OPEN, {"ref": ref, "error": error}, reliable=True)
            return
        for target in targets:
            self._send_doc(target, doc_id, text, 0, filename, fresh=True, by=client_id, ref=ref)
        self._emit_log(f"{self._name_of(client_id)} shared {filename or 'a new buffer'}")

    def _name_of(self, client_id):
        with self._lock:
            rec = self.clients.get(client_id)
        return rec.username if rec else f"#{client_id}"

    def _chat_allow(self, rec):
        """Token bucket: True if `rec` may send a chat message now."""
        now = self._clock()
        burst = float(self.chat_rate_burst)
        if rec.chat_tokens is None:
            rec.chat_tokens, rec.chat_stamp = burst, now
        rate = burst / self.chat_rate_period
        rec.chat_tokens = min(burst, rec.chat_tokens + (now - rec.chat_stamp) * rate)
        rec.chat_stamp = now
        if rec.chat_tokens >= 1.0:
            rec.chat_tokens -= 1.0
            return True
        return False

    def _resolve_chat_targets(self, tokens, sender_id):
        """Maps recipient tokens (a user name, case-insensitive, or `#<id>`)
        to client records. Returns (records, unknown_tokens). Names are
        unique, so a name can never match two people. Caller holds the lock."""
        found, unknown = {}, []
        for tok in tokens:
            rec = None
            if tok.startswith("#") and tok[1:].isdigit():
                rec = self.clients.get(int(tok[1:]))
            else:
                low = tok.lower()
                rec = next((c for c in self.clients.values() if c.username.lower() == low), None)
            if rec is None:
                unknown.append(tok)
            elif rec.client_id != sender_id:
                found[rec.client_id] = rec
        return list(found.values()), unknown

    def _handle_chat(self, payload, addr):
        """Relays a terminal chat message. Sanitizes it (the same rules the
        clients apply again on display), rate-limits the sender, stamps it
        with a server-assigned sequence number and timestamp, and sends it
        to everyone else (public) or to the named recipients (private). Only
        public messages enter the bounded join history. The sender never
        gets its own message back (it echoes locally) but does get
        {"error": ...} if it was rate limited or nobody could receive it."""
        if not isinstance(payload, dict):
            return
        text = sanitize_text(payload.get("text", ""), CHAT_MAX_LEN)
        if not text:
            return
        raw_to = payload.get("to")
        if isinstance(raw_to, str):
            raw_to = [raw_to]
        tokens = []
        if isinstance(raw_to, list):
            for t in raw_to[:CHAT_MAX_RECIPIENTS]:
                t = sanitize_text(t, NAME_MAX_LEN + 1) if isinstance(t, str) else ""
                if t:
                    tokens.append(t)
        private = bool(tokens)
        error = None
        message, deliver = None, []
        with self._lock:
            sender_id = self.clients_by_addr.get(addr)
            sender = self.clients.get(sender_id)
            if sender is None:
                return
            if not self._chat_allow(sender):
                error = "Slow down - too many chat messages. Try again in a few seconds."
            else:
                if private:
                    targets, unknown = self._resolve_chat_targets(tokens, sender_id)
                else:
                    targets = [c for c in self.clients.values() if c.client_id != sender_id]
                    unknown = []
                if not targets:
                    if unknown:
                        error = "No such user: " + ", ".join(f"'{u}'" for u in unknown) + "."
                    elif private:
                        error = "You can't send a private message to yourself."
                    else:
                        error = "Nobody else is connected."
                else:
                    self.chat_seq += 1
                    message = {"seq": self.chat_seq, "ts": time.time(), "from": sender_id,
                               "name": sender.username, "text": text, "private": private}
                    if private:
                        message["to"] = [c.username for c in targets]
                    else:
                        self.chat_history.append(dict(message))
                    deliver = [c.addr for c in targets]
                    if unknown:
                        error = "Not delivered to: " + ", ".join(f"'{u}'" for u in unknown) + " (no such user)."
        if error:
            self.transport.send(addr, p.CHAT, {"error": error}, reliable=True)
        for target in deliver:
            self.transport.send(target, p.CHAT, message, reliable=True)

    # ---- shared run output (opt-in on the runner's side; see share_util) ----
    #
    # The server never decides to share anything: a client only sends these
    # messages after its user turned sharing on. The server relays them to
    # everyone else (read-only for them - there is no message that goes back
    # to a runner), sanitizes and caps them again, and keeps a small buffer
    # of each runner's latest run so a late joiner can catch up.

    def _share_msg(self, rec, state, **extra):
        msg = {"from": rec.client_id, "name": rec.username, "run": state["run"]}
        msg.update(extra)
        return msg

    def _relay_share(self, kind, rec, msg):
        with self._lock:
            targets = [c.addr for c in self.clients.values() if c.client_id != rec.client_id]
        for target in targets:
            self.transport.send(target, kind, msg, reliable=True)

    def _share_allow(self, state):
        now = self._clock()
        burst = float(share_util.SHARE_RATE_BURST)
        state["tokens"] = min(burst, state["tokens"] + (now - state["stamp"]) * share_util.SHARE_RATE_PER_SEC)
        state["stamp"] = now
        if state["tokens"] >= 1.0:
            state["tokens"] -= 1.0
            return True
        return False

    def _handle_share(self, kind, payload, addr):
        if not isinstance(payload, dict):
            return
        run = share_util._int(payload.get("run"), lo=1)
        if run is None:
            return
        relay = None
        with self._lock:
            rec = self.clients.get(self.clients_by_addr.get(addr))
            if rec is None:
                return
            state = rec.share
            if kind == p.RUN_START:
                state = rec.share = {
                    "run": run, "title": sanitize_text(payload.get("title", ""), share_util.SHARE_TITLE_MAX) or "command",
                    "chunks": {}, "buf_bytes": 0, "sent": 0, "truncated": False, "end": None,
                    "last_seq": 0, "from_seq": 1, "tokens": float(share_util.SHARE_RATE_BURST), "stamp": self._clock(),
                }
                relay = (p.RUN_START, self._share_msg(rec, state, title=state["title"], from_seq=1))
            elif state is None or state["run"] != run or state["end"] is not None:
                return                      # output for a run we don't know / that already ended
            elif kind == p.RUN_OUTPUT:
                seq = share_util._int(payload.get("seq"), lo=1)
                if seq is None or seq in state["chunks"] or state["truncated"]:
                    return
                state["last_seq"] = max(state["last_seq"], seq)
                segs = share_util.normalize_segs(payload.get("segs"))
                nbytes = share_util.segs_bytes(segs)
                if payload.get("truncated") or state["sent"] + nbytes > share_util.SHARE_RUN_MAX_BYTES:
                    state["truncated"] = True
                    msg = self._share_msg(rec, state, seq=seq, truncated=True)
                elif not self._share_allow(state):
                    msg = self._share_msg(rec, state, seq=seq, skipped=nbytes)   # keeps the numbering gapless
                else:
                    state["sent"] += nbytes
                    msg = self._share_msg(rec, state, seq=seq, segs=segs)
                state["chunks"][seq] = msg
                state["buf_bytes"] += nbytes
                while state["buf_bytes"] > share_util.SHARE_BUFFER_MAX_BYTES and len(state["chunks"]) > 1:
                    oldest = min(state["chunks"])
                    old = state["chunks"].pop(oldest)
                    state["buf_bytes"] -= share_util.segs_bytes(old.get("segs", []))
                    state["from_seq"] = min(state["chunks"])
                relay = (p.RUN_OUTPUT, msg)
            elif kind == p.RUN_END:
                seq = share_util._int(payload.get("seq"), lo=1)
                if seq is None:
                    return
                code = share_util._int(payload.get("code"), lo=-(2 ** 31), hi=2 ** 31)
                state["end"] = self._share_msg(
                    rec, state, seq=seq, code=code,
                    reason=sanitize_text(payload.get("reason", ""), 60))
                state["last_seq"] = max(state["last_seq"], seq)
                relay = (p.RUN_END, state["end"])
        if relay:
            self._relay_share(relay[0], rec, relay[1])

    def _send_share_catchup(self, addr):
        """Late joiner: replay every other runner's current/last run from the
        buffer (flagged `replay` so viewers don't announce it as new)."""
        with self._lock:
            me = self.clients_by_addr.get(addr)
            batches = []
            for rec in self.clients.values():
                state = rec.share
                if rec.client_id == me or not state:
                    continue
                msgs = [(p.RUN_START, self._share_msg(rec, state, title=state["title"],
                                                      from_seq=state["from_seq"], replay=True))]
                msgs += [(p.RUN_OUTPUT, dict(state["chunks"][s], replay=True)) for s in sorted(state["chunks"])]
                if state["end"] is not None:
                    msgs.append((p.RUN_END, dict(state["end"], replay=True)))
                batches.append(msgs)
        for msgs in batches:
            for kind, msg in msgs:
                self.transport.send(addr, kind, msg, reliable=True)

    def _handle_cursor(self, payload, addr):
        if not isinstance(payload, dict):
            return
        with self._lock:
            client_id = self.clients_by_addr.get(addr)
            if client_id is None:
                return
            doc_id = self._doc_id_from(payload)
            if doc_id is None:
                return
            payload = {**payload, "doc": doc_id}
            rec = self.clients.get(client_id)
            if rec:
                rec.cursors[doc_id] = payload
        self._broadcast(p.CURSOR, {**payload, "from": client_id}, exclude_addr=addr, reliable=False)

    def _handle_goodbye(self, addr):
        self._drop(addr)

    def _on_peer_timeout(self, addr):
        self._drop(addr)

    def _drop(self, addr):
        with self._lock:
            client_id = self.clients_by_addr.pop(addr, None)
            if client_id is None:
                return
            rec = self.clients.pop(client_id, None)
        if rec:
            share = rec.share
            if share and share["end"] is None:
                # A runner that vanishes mid-run: tell viewers the run is over.
                self._broadcast(p.RUN_END, {"from": rec.client_id, "name": rec.username, "run": share["run"],
                                            "seq": share["last_seq"] + 1, "code": None, "reason": "left the session"})
            self._emit_log(f"{rec.username} disconnected")
            self._broadcast(p.PEERS, self._roster_payload())

    def snapshot(self):
        with self._lock:
            main = self.docs[p.MAIN_DOC]
            return {"revision": main.revision, "peers": len(self.clients), "doc_len": len(main.doc),
                    "docs": len(self.docs)}

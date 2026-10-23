import collections
import queue
import random
import socket
import threading
import time
import traceback

import ot
from chat_util import CHAT_MAX_LEN, NAME_MAX_LEN, sanitize_name, sanitize_text
import share_util
from . import transport as p
from .transport import ReliableUDP
from .server import Server


class ConnectError(Exception):
    pass


class ConnectCancelled(ConnectError):
    """Raised when connect() is aborted mid-flight via cancel_connect()."""
    pass


class DocSync:
    """The client-side OT state machine (the classic Synchronized /
    AwaitingConfirm / AwaitingWithBuffer model) for ONE shared document.
    A session can carry several documents, each numbered by its own server
    revision counter, so each gets its own instance.

    A document is `ready` once its full text has arrived; until then the
    ops that show up are only buffered (they apply on top of that text),
    and local edits are dropped (the incoming text replaces whatever the
    editor holds for it anyway)."""

    def __init__(self):
        self.ready = False
        self.state = "synced"
        self.outstanding = None
        self.buffer = None
        # Optional "this op replaces the whole shared buffer" marker that
        # travels with the op it belongs to (see Client.local_edit's `swap`).
        # Tracked next to outstanding/buffer so it survives the same
        # queueing/composition those go through.
        self.outstanding_meta = None
        self.buffer_meta = None
        self.expected_rev = 0
        self.rev_buffer = {}
        self.chunks = {}
        self.meta = {}

    def reset_for_resync(self):
        self.ready = False
        self.state = "synced"
        self.outstanding = self.buffer = None
        self.outstanding_meta = self.buffer_meta = None
        self.chunks = {}
        self.meta = {}


class Client:
    """Wraps the transport and one DocSync per shared document."""

    def __init__(self):
        self.transport = ReliableUDP()
        self.transport.on_message = self._on_message
        self.transport.on_peer_timeout = self._on_timeout
        self.server_addr = None
        self.client_id = None
        self.color = None
        self.username = None  # the name the server finally assigned (may be "bob#2")
        self.requested_username = None
        # Terminal chat: dedupe + short reorder window (see _handle_chat).
        self.chat_reorder_window = 0.15
        self._chat_seen = set()
        self._chat_seen_order = collections.deque()
        self._chat_hold = []
        self._chat_timer = None
        self._heartbeat_stop = threading.Event()

        self.docs = {}  # doc id -> DocSync
        # Documents this peer left (see forget_doc): traffic for them is
        # ignored for the rest of the connection.
        self.forgotten = set()
        self._lock = threading.RLock()
        # Incoming document traffic (full texts and ops) is not handled on
        # the network thread but handed to `dispatcher`, which runs it on
        # the thread that also makes local edits (for the editor: the Tk
        # thread, `lambda fn: root.after(0, fn)`). That is what keeps a
        # peer's op from being transformed against one state of the
        # document and then applied to a text the user has typed into in the
        # meantime. None (the default) runs it right on the network thread.
        self.dispatcher = None
        self._inbox = collections.deque()

        self._on_remote_op = None
        self._on_full_sync = None
        self._on_peers = None
        self._on_cursor = None
        self._on_disconnected = None
        self._on_buffer_context = None
        self._on_chat = None
        self._on_share = None
        self._on_doc_rejected = None
        self._pending = {"remote_op": [], "full_sync": [], "peers": [], "cursor": [], "disconnected": [], "buffer_context": [], "chat": [], "share": [], "doc_rejected": []}
        self._connect_result = queue.Queue()
        self._cancelled = threading.Event()

    def _make_callback_prop(name):
        attr = f"_on_{name}"

        def getter(self):
            return getattr(self, attr)

        def setter(self, fn):
            with self._lock:
                setattr(self, attr, fn)
                pending = self._pending[name]
                self._pending[name] = []
            if fn:
                for args in pending:
                    fn(*args)

        return property(getter, setter)

    on_remote_op = _make_callback_prop("remote_op")
    on_full_sync = _make_callback_prop("full_sync")
    on_peers = _make_callback_prop("peers")
    on_cursor = _make_callback_prop("cursor")
    on_disconnected = _make_callback_prop("disconnected")
    on_buffer_context = _make_callback_prop("buffer_context")
    on_chat = _make_callback_prop("chat")
    on_share = _make_callback_prop("share")
    on_doc_rejected = _make_callback_prop("doc_rejected")

    def _doc(self, doc_id):
        """The DocSync for `doc_id`, created on first mention (an op for a
        document can overtake the document's own text). Caller holds the lock."""
        sync = self.docs.get(doc_id)
        if sync is None:
            sync = self.docs[doc_id] = DocSync()
        return sync

    def forget_doc(self, doc_id):
        """Stops tracking a document: drops its sync state and ignores
        everything the session still sends about it. The document itself
        carries on for the other peers."""
        with self._lock:
            self.forgotten.add(doc_id)
            self.docs.pop(doc_id, None)

    def _to_ui(self, fn, *args):
        """Queues `fn(*args)` for the dispatcher's thread. A plain FIFO that
        is drained in order, so the order messages were received in is the
        order they run in whatever the dispatcher's scheduling does. No lock
        is held here: the dispatcher may block this thread until the UI
        thread has serviced it."""
        self._inbox.append((fn, args))
        if self.dispatcher is None:
            self._run_inbox()
        else:
            self.dispatcher(self._run_inbox)

    def _run_inbox(self):
        while True:
            try:
                fn, args = self._inbox.popleft()
            except IndexError:
                return
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()

    def _fire(self, name, *args):
        """Invoke a callback if set, else buffer the event so a callback
        attached later (as happens when the editor UI is built after the
        network handshake already completed) still receives it, in order."""
        fn = getattr(self, f"_on_{name}")
        if fn:
            fn(*args)
        else:
            with self._lock:
                self._pending[name].append(args)

    def connect(self, host, port, username, timeout=6.0, extra_hello=None):
        self._cancelled.clear()
        self.username = self.requested_username = username
        self.transport.start()
        deadline = time.monotonic() + timeout

        try:
            resolved_addr = self._resolve_cancelable(host, port, deadline)
        except (ConnectCancelled, ConnectError):
            self.transport.stop()
            raise
        self.server_addr = resolved_addr

        hello = {"username": username}
        if extra_hello:
            hello.update(extra_hello)
        self.transport.send(self.server_addr, p.HELLO, hello, reliable=True)

        # A host on another protocol version can't read our HELLO and stays
        # silent - which looks exactly like a wrong address or a firewall.
        # So (1) anything that comes back from the host in another version's
        # framing is reported as the version mismatch it is, and (2) an
        # older host, which would never answer, is nudged with a PING in its
        # own dialect, which its server answers from anyone.
        server_addr = self.server_addr

        def foreign(addr, version, kind, seq, flags):
            if addr == server_addr:
                self._connect_result.put(("version", version))
        self.transport.on_foreign_version = foreign
        next_probe = 0.0

        kind = payload = None
        while True:
            if self._cancelled.is_set():
                self.transport.stop()
                raise ConnectCancelled("Connection attempt cancelled.")
            if time.monotonic() >= next_probe:
                for version in p.PROBE_VERSIONS:
                    self.transport.send_as_version(server_addr, version, p.PING)
                next_probe = time.monotonic() + 1.0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.transport.stop()
                raise ConnectError("No response from host (timed out). Check the address, port, and port forwarding or tunnel setup.")
            try:
                kind, payload = self._connect_result.get(timeout=min(0.15, remaining))
                break
            except queue.Empty:
                continue

        self.transport.on_foreign_version = None
        if kind == "version":
            self.transport.stop()
            raise ConnectError(p.mismatch_message(payload, host_is_theirs=True))
        if kind == p.HELLO_REJECT:
            self.transport.stop()
            raise ConnectError(payload.get("reason", "Connection refused by host."))
        self.client_id = payload["client_id"]
        self.color = payload["color"]
        # The server may have renamed us to keep names unique (bob -> bob#2).
        self.username = sanitize_name(payload.get("username", username), NAME_MAX_LEN)
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def _resolve_cancelable(self, host, port, deadline):
        """Resolve host/port to a numeric address without letting a slow or
        unresponsive DNS server block cancellation.

        socket.sendto() transparently calls getaddrinfo() whenever it is
        given a hostname instead of a numeric IP, and getaddrinfo() has no
        built-in timeout. If DNS is unreachable it can block the calling
        thread for a very long time, well past our connect timeout, and
        past anything cancel_connect() could interrupt, since the thread
        is stuck inside a C level OS call rather than in our poll loop.
        Every retry from ReliableUDP's retransmit loop would hit the same
        stall again, since sendto() re-resolves on every call. Resolving
        once, up front, on a background thread, and polling for either a
        result or a cancellation exactly like the handshake wait below,
        keeps Cancel responsive and gives ReliableUDP a plain numeric
        address to send to from then on.
        """
        result = queue.Queue(maxsize=1)

        def worker():
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
                result.put(("ok", infos[0][4]))
            except OSError as e:
                result.put(("error", e))

        threading.Thread(target=worker, daemon=True).start()

        while True:
            if self._cancelled.is_set():
                raise ConnectCancelled("Connection attempt cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectError(f"Could not resolve '{host}' (timed out).")
            try:
                status, value = result.get(timeout=min(0.15, remaining))
            except queue.Empty:
                continue
            if status == "error":
                raise ConnectError(f"Could not resolve '{host}': {value}")
            return value

    def cancel_connect(self):
        """Abort an in-flight connect() call from another thread."""
        self._cancelled.set()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(2.0):
            if self.server_addr:
                self.transport.send(self.server_addr, p.PING, {}, reliable=True)

    def disconnect(self):
        self._heartbeat_stop.set()
        if self.server_addr:
            for _ in range(3):
                try:
                    self.transport.send(self.server_addr, p.GOODBYE, {}, reliable=False)
                except OSError:
                    pass
            time.sleep(0.02)
        self.transport.stop()

    def send_hello_as_host(self, port, username, extra_hello=None):
        """Host connects to its own server as an ordinary client on localhost."""
        self.connect("127.0.0.1", port, username, extra_hello=extra_hello)

    def local_edit(self, op: ot.Op, doc=p.MAIN_DOC, swap=None):
        """Sends a local edit to document `doc`. `swap`, when given, is a small dict
        ({"filename": hint-or-None}) marking this op as one that replaces
        the whole shared buffer with a different document rather than
        being an ordinary edit. It rides inside the op's own message, so
        every peer sees it at exactly the right spot in the op stream -
        which is what lets them keep their old copy of the buffer as a
        local tab without racing against the (unordered) separate
        buffer-context message."""
        if op.is_noop():
            return
        with self._lock:
            sync = self._doc(doc)
            if not sync.ready:
                return      # its text hasn't arrived yet; that text replaces any edit made now
            if sync.state == "synced":
                sync.state = "awaiting"
                sync.outstanding = op
                sync.outstanding_meta = swap
                self._send_op(doc, sync.expected_rev, op, swap)
            elif sync.state == "awaiting":
                sync.state = "awaiting_buffer"
                sync.buffer = op
                sync.buffer_meta = swap
            else:
                sync.buffer = ot.compose(sync.buffer, op)
                if swap is not None:
                    sync.buffer_meta = swap

    def _send_op(self, doc, base_rev, op, swap=None):
        payload = {"doc": doc, "base_rev": base_rev, "ops": op.to_json()}
        if swap is not None:
            payload["swap"] = swap
        self.transport.send(self.server_addr, p.OP, payload, reliable=True)

    def send_cursor(self, index, has_selection, sel_start=None, sel_end=None, doc=p.MAIN_DOC):
        if not self.server_addr:
            return
        payload = {"doc": doc, "index": index, "sel": has_selection}
        if has_selection:
            payload["start"] = sel_start
            payload["end"] = sel_end
        self.transport.send(self.server_addr, p.CURSOR, payload, reliable=False)

    def send_buffer_context(self, filename, doc=p.MAIN_DOC):
        if not self.server_addr:
            return
        self.transport.send(self.server_addr, p.BUFFER_CONTEXT, {"doc": doc, "filename": filename}, reliable=True)

    def open_doc(self, filename, text, ref):
        """Shares `text` as a NEW document. The server picks its id and
        sends it to everyone, us included, as a full document carrying our
        `ref` (so the sender can tell which of its tabs it is). Returns
        False if not connected; a refusal comes back via on_doc_rejected."""
        if not self.server_addr:
            return False
        self.transport.send(self.server_addr, p.DOC_OPEN,
                            {"ref": ref, "filename": filename, "text": text}, reliable=True)
        return True

    def send_chat(self, text, to=None):
        """Sends a terminal chat message. `to` is None (everyone), a name /
        "#id" string, or a list of them. Returns False if not connected."""
        if not self.server_addr:
            return False
        payload = {"text": sanitize_text(text, CHAT_MAX_LEN)}
        if isinstance(to, str):
            to = [to]
        if to:
            payload["to"] = list(to)
        self.transport.send(self.server_addr, p.CHAT, payload, reliable=True)
        return True

    # ---- shared run output (opt-in; see share_util) ----

    def send_share_start(self, run, title):
        if not self.server_addr:
            return False
        self.transport.send(self.server_addr, p.RUN_START,
                            {"run": run, "title": sanitize_text(title, share_util.SHARE_TITLE_MAX)}, reliable=True)
        return True

    def send_share_output(self, run, seq, segs, truncated=False):
        if not self.server_addr:
            return False
        payload = {"run": run, "seq": seq}
        if truncated:
            payload["truncated"] = True
        else:
            payload["segs"] = segs
        self.transport.send(self.server_addr, p.RUN_OUTPUT, payload, reliable=True)
        return True

    def send_share_end(self, run, seq, code=None, reason=""):
        if not self.server_addr:
            return False
        self.transport.send(self.server_addr, p.RUN_END,
                            {"run": run, "seq": seq, "code": code, "reason": reason}, reliable=True)
        return True

    def _handle_share(self, kind, payload):
        """Normalizes (and so re-sanitizes) a relayed run message before the
        UI sees it: the relay isn't trusted any more than a peer is."""
        if not isinstance(payload, dict):
            return
        run = share_util._int(payload.get("run"), lo=1)
        sender = share_util._int(payload.get("from"), lo=0)
        if run is None or sender is None:
            return
        msg = {"from": sender, "run": run, "name": sanitize_name(payload.get("name"), NAME_MAX_LEN),
               "replay": bool(payload.get("replay"))}
        if kind == p.RUN_START:
            msg["title"] = sanitize_text(payload.get("title", ""), share_util.SHARE_TITLE_MAX) or "command"
            msg["from_seq"] = share_util._int(payload.get("from_seq"), default=1, lo=1)
            name = "start"
        else:
            seq = share_util._int(payload.get("seq"), lo=1)
            if seq is None:
                return
            msg["seq"] = seq
            if kind == p.RUN_OUTPUT:
                msg["segs"] = share_util.normalize_segs(payload.get("segs"))
                msg["skipped"] = share_util._int(payload.get("skipped"), default=0, lo=0)
                msg["truncated"] = bool(payload.get("truncated"))
                name = "output"
            else:
                msg["code"] = share_util._int(payload.get("code"), lo=-(2 ** 31), hi=2 ** 31)
                msg["reason"] = sanitize_text(payload.get("reason", ""), 60)
                name = "end"
        self._fire("share", name, msg)

    @staticmethod
    def _normalize_chat(msg):
        """Re-sanitizes one relayed message (never trust the relay: the
        server may be a peer running modified code). None if unusable."""
        if not isinstance(msg, dict):
            return None
        try:
            seq = int(msg["seq"])
            sender = int(msg.get("from", 0))
            ts = float(msg.get("ts") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        text = sanitize_text(msg.get("text", ""), CHAT_MAX_LEN)
        if not text:
            return None
        private = bool(msg.get("private"))
        to = msg.get("to") if isinstance(msg.get("to"), list) else []
        return {"seq": seq, "ts": ts, "from": sender, "private": private,
                "name": sanitize_name(msg.get("name"), NAME_MAX_LEN),
                "to": [sanitize_name(t, NAME_MAX_LEN) for t in to[:16] if isinstance(t, str)],
                "text": text}

    def _chat_mark_seen(self, seq):
        """True if seq is new (and remembers it), False for a duplicate."""
        if seq in self._chat_seen:
            return False
        self._chat_seen.add(seq)
        self._chat_seen_order.append(seq)
        if len(self._chat_seen_order) > 1024:
            self._chat_seen.discard(self._chat_seen_order.popleft())
        return True

    def _handle_chat(self, payload):
        """Relayed chat arrives over reliable UDP, which is not ordered across
        messages. Each message carries a server-assigned `seq`: duplicates
        are dropped, and live messages are held for chat_reorder_window
        seconds and released sorted by seq. (A message later than that is
        still delivered - late beats lost - just out of order.) Server
        errors are not sequenced and pass straight through; the join
        history arrives as one batch, sorted, as {"history": [...]}."""
        if not isinstance(payload, dict):
            return
        if "error" in payload:
            self._fire("chat", {"error": sanitize_text(payload["error"], 300)})
            return
        if isinstance(payload.get("history"), list):
            with self._lock:
                msgs = [m for m in (self._normalize_chat(x) for x in payload["history"][:200]) if m]
                msgs.sort(key=lambda m: m["seq"])
                fresh = [m for m in msgs if self._chat_mark_seen(m["seq"])]
            if fresh:
                self._fire("chat", {"history": fresh})
            return
        msg = self._normalize_chat(payload)
        if msg is None:
            return
        with self._lock:
            if not self._chat_mark_seen(msg["seq"]):
                return
            self._chat_hold.append(msg)
            if self._chat_timer is None:
                self._chat_timer = threading.Timer(self.chat_reorder_window, self._flush_chat)
                self._chat_timer.daemon = True
                self._chat_timer.start()

    def _flush_chat(self):
        with self._lock:
            held, self._chat_hold = self._chat_hold, []
            self._chat_timer = None
        for msg in sorted(held, key=lambda m: m["seq"]):
            self._fire("chat", msg)

    def _on_message(self, kind, payload, addr):
        if kind == p.WELCOME:
            if payload.get("resync"):
                # The server gave up on one of our ops: that document's text
                # is about to be resent in full, replacing our copy.
                self._to_ui(self._handle_resync, payload)
            else:
                self._connect_result.put((kind, payload))
        elif kind == p.HELLO_REJECT:
            self._connect_result.put((kind, payload))
        elif kind == p.DOC_CHUNK:
            self._to_ui(self._handle_doc_chunk, payload)
        elif kind == p.DOC_OPEN:
            if isinstance(payload, dict) and payload.get("error"):
                self._fire("doc_rejected", payload.get("ref"), sanitize_text(payload["error"], 300))
        elif kind == p.OP:
            self._to_ui(self._handle_remote_op, payload)
        elif kind == p.PEERS:
            self._fire("peers", payload.get("peers", []))
        elif kind == p.CURSOR:
            self._fire("cursor", payload)
        elif kind == p.BUFFER_CONTEXT:
            self._fire("buffer_context", payload)
        elif kind == p.CHAT:
            self._handle_chat(payload)
        elif kind in (p.RUN_START, p.RUN_OUTPUT, p.RUN_END):
            self._handle_share(kind, payload)
        elif kind == p.HOST_SHUTDOWN:
            self._fire("disconnected", "The host ended the session.")
        elif kind == p.PONG:
            pass

    def _handle_resync(self, payload):
        doc_id = payload.get("doc", p.MAIN_DOC)
        with self._lock:
            if doc_id in self.forgotten:
                return
            self._doc(doc_id).reset_for_resync()

    def _handle_doc_chunk(self, payload):
        """Collects one document's chunks (they may arrive in any order);
        once complete the document becomes ready at the revision it was
        captured at, the editor is told the full text, and any ops that
        arrived early are applied on top."""
        doc_id = payload.get("doc", p.MAIN_DOC)
        idx, total, text = payload["index"], payload["total"], payload["text"]
        events = []
        with self._lock:
            if doc_id in self.forgotten:
                return
            sync = self._doc(doc_id)
            sync.chunks[idx] = text
            if not sync.meta:
                sync.meta = {k: payload.get(k) for k in ("filename", "by", "ref")}
                sync.meta["fresh"] = bool(payload.get("fresh"))
            if len(sync.chunks) < total:
                return
            full = "".join(sync.chunks[i] for i in range(total))
            meta = sync.meta
            sync.chunks, sync.meta = {}, {}
            sync.ready = True
            sync.state = "synced"
            sync.outstanding = sync.buffer = None
            sync.outstanding_meta = sync.buffer_meta = None
            sync.expected_rev = int(payload["revision"])
            for stale in [r for r in sync.rev_buffer if r < sync.expected_rev]:
                del sync.rev_buffer[stale]      # already contained in the text
            events.append(("full_sync", (doc_id, full, meta)))
            events.extend(self._drain_rev_buffer(doc_id, sync))
        for name, args in events:
            self._fire(name, *args)

    def _handle_remote_op(self, payload):
        # Runs on the dispatcher's thread (see _to_ui). The state machine is
        # advanced under the lock, but the callbacks are only *collected*
        # there and fired after it is released: a callback can end up in
        # Tk, which makes a thread wait until the main thread services the
        # call, and waiting on the UI while holding a lock the UI needs for
        # its next keystroke (local_edit) is what used to freeze the app.
        doc_id = payload.get("doc", p.MAIN_DOC)
        with self._lock:
            if doc_id in self.forgotten:
                return
            sync = self._doc(doc_id)
            base_rev = payload["base_rev"]
            if sync.ready and base_rev < sync.expected_rev:
                return      # already folded into the text we were given
            sync.rev_buffer[base_rev] = payload
            events = self._drain_rev_buffer(doc_id, sync) if sync.ready else []
        for name, args in events:
            self._fire(name, *args)

    def _drain_rev_buffer(self, doc_id, sync):
        """Applies every buffered op of one document that is next in line
        and returns the events to fire. Caller holds the lock and must fire
        the events only after releasing it."""
        events = []
        while sync.expected_rev in sync.rev_buffer:
            payload = sync.rev_buffer.pop(sync.expected_rev)
            op = ot.Op.from_json(payload["ops"])
            is_mine = payload.get("from") == self.client_id
            swap = payload.get("swap") if isinstance(payload.get("swap"), dict) else None
            to_apply = None
            if is_mine:
                self._on_own_ack(doc_id, sync)
            else:
                to_apply = self._transform_incoming(sync, op)
            sync.expected_rev += 1
            if to_apply is not None and (swap is not None or not to_apply.is_noop()):
                events.append(("remote_op", (doc_id, to_apply, payload.get("from"), swap)))
        return events

    def _on_own_ack(self, doc_id, sync):
        if sync.state == "awaiting":
            sync.state = "synced"
            sync.outstanding = None
            sync.outstanding_meta = None
        elif sync.state == "awaiting_buffer":
            sync.state = "awaiting"
            sync.outstanding = sync.buffer
            sync.outstanding_meta = sync.buffer_meta
            sync.buffer = None
            sync.buffer_meta = None
            self._send_op(doc_id, sync.expected_rev + 1, sync.outstanding, sync.outstanding_meta)

    @staticmethod
    def _transform_incoming(sync, op):
        if sync.state == "synced":
            return op
        if sync.state == "awaiting":
            sync.outstanding, op2 = ot.transform(sync.outstanding, op)
            return op2
        sync.outstanding, op2 = ot.transform(sync.outstanding, op)
        sync.buffer, op3 = ot.transform(sync.buffer, op2)
        return op3

    def _on_timeout(self, addr):
        if addr == self.server_addr:
            self._fire("disconnected", "Connection to host timed out.")


def reachable_peers(roster):
    """Roster entries that could be dialed if elected: every peer except the
    now-vanished sequencer, which never gets re-elected."""
    return [peer for peer in roster
            if peer.get("p2p_host") and peer.get("p2p_port") and not peer.get("is_sequencer")]


def elect_sequencer(roster, my_id):
    """Pure, deterministic election: the lowest client id among the peers
    who were alive, per the last shared roster, survives, possibly
    including myself. Every peer computes this independently from the same
    last-known-good roster and reaches the same answer without asking
    anyone, which is what makes it decentralized rather than requiring a
    coordinator to hand out the new role.

    If my own last-known roster entry says I was the outgoing sequencer, I
    do not re-nominate myself, since the whole point of failover is someone
    else taking over. This matters because a sequencer's own local client is
    also a roster member and can observe its own shutdown broadcast."""
    candidates = [peer["id"] for peer in reachable_peers(roster) if peer.get("id") is not None]
    my_entry = next((peer for peer in roster if peer.get("id") == my_id), None)
    was_sequencer = bool(my_entry and my_entry.get("is_sequencer"))
    if my_id is not None and not was_sequencer:
        candidates.append(my_id)
    return min(candidates) if candidates else None


class FailoverController:
    """Owns the "who is the sequencer now" decision and the mechanics of
    reconnecting after the current one disappears. Deliberately GUI-free:
    the caller (EditorApp) supplies small callbacks for the Tk-side effects
    (getting every shared document to seed a promoted server, and
    reacting once reconnection succeeds or gives up) so this class can be
    unit-tested with plain Server/Client sockets, no Tk involved."""

    def __init__(self, username, advertise_host, advertise_port, get_docs,
                 on_reconnected, on_failed, reconnect_delay=(0.3, 0.6), advertise_socket=None):
        self.username = username
        self.advertise_host = advertise_host
        self.advertise_port = advertise_port
        self.get_docs = get_docs
        self.on_reconnected = on_reconnected
        self.on_failed = on_failed
        self._reconnect_delay = reconnect_delay
        self.advertise_socket = advertise_socket
        self._lock = threading.RLock()
        self._roster = []
        self._closed = False

    def note_roster(self, peers):
        with self._lock:
            self._roster = list(peers)
        if self.advertise_socket:
            self.advertise_socket.note_peers(
                (peer["p2p_host"], peer["p2p_port"]) for peer in peers
                if peer.get("p2p_host") and peer.get("p2p_port"))

    def hello_extra(self, is_sequencer):
        return {"p2p_host": self.advertise_host, "p2p_port": self.advertise_port,
                "is_sequencer": is_sequencer}

    def close(self):
        self._closed = True
        if self.advertise_socket:
            self.advertise_socket.close()

    def begin(self, old_client, reason):
        """Kicks off the election and reconnect attempt in the background.
        Safe to call from the Tk thread; the network work happens off it."""
        threading.Thread(target=self._run, args=(old_client, reason), daemon=True).start()

    def _run(self, old_client, reason):
        if self._closed:
            return
        try:
            old_client.transport.stop()
        except Exception:
            pass

        with self._lock:
            roster = list(self._roster)
        my_id = old_client.client_id
        winner = elect_sequencer(roster, my_id)
        if winner is None:
            self.on_failed(f"{reason} No other reachable peers to fail over to.")
            return

        try:
            if winner == my_id:
                new_client, new_server = self._promote_self()
            else:
                target = next((peer for peer in reachable_peers(roster) if peer["id"] == winner), None)
                if target is None:
                    self.on_failed(f"{reason} Couldn't determine the new sequencer's address.")
                    return
                new_client = self._reconnect_to(target["p2p_host"], target["p2p_port"])
                new_server = None
        except ConnectCancelled:
            return
        except (ConnectError, OSError) as e:
            self.on_failed(f"{reason} Automatic reconnect failed: {e}")
            return

        if self._closed:
            try:
                new_client.disconnect()
            except Exception:
                pass
            try:
                if new_server:
                    new_server.stop()
            except Exception:
                pass
            return

        self.on_reconnected(new_client, new_server, reason)

    def _promote_self(self):
        docs = self.get_docs() if self.get_docs else None
        sock = self.advertise_socket.detach() if self.advertise_socket else None
        try:
            server = Server(self.advertise_port, initial_docs=docs, sock=sock)
        except OSError:
            if self.advertise_socket:
                self.advertise_socket.close()
            raise
        try:
            server.start()
        except OSError:
            try:
                server.stop()
            except Exception:
                pass
            if self.advertise_socket:
                self.advertise_socket.close()
            raise
        client = Client()
        try:
            client.connect("127.0.0.1", self.advertise_port, self.username,
                            extra_hello=self.hello_extra(is_sequencer=True))
        except (ConnectError, OSError):
            server.stop()
            if self.advertise_socket:
                self.advertise_socket.close()
            raise
        return client, server

    def _reconnect_to(self, host, port):
        lo, hi = self._reconnect_delay
        time.sleep(lo + random.random() * (hi - lo))
        client = Client()
        client.connect(host, port, self.username,
                        extra_hello=self.hello_extra(is_sequencer=False))
        return client

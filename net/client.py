import collections
import queue
import random
import socket
import threading
import time

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


class Client:
    """Wraps the transport + client-side OT state machine described by the
    classic Synchronized / AwaitingConfirm / AwaitingWithBuffer model."""

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

        self.state = "synced"
        self.outstanding = None
        self.buffer = None
        # Optional "this op replaces the whole shared buffer" marker that
        # travels with the op it belongs to (see local_edit's `swap`).
        # Tracked next to outstanding/buffer so it survives the same
        # queueing/composition those go through.
        self.outstanding_meta = None
        self.buffer_meta = None
        self.expected_rev = 0
        self.rev_buffer = {}
        self._doc_chunks = {}
        self._doc_total = None
        self._pending_full_sync = None
        self._lock = threading.RLock()

        self._on_remote_op = None
        self._on_full_sync = None
        self._on_peers = None
        self._on_cursor = None
        self._on_disconnected = None
        self._on_buffer_context = None
        self._on_chat = None
        self._on_share = None
        self._pending = {"remote_op": [], "full_sync": [], "peers": [], "cursor": [], "disconnected": [], "buffer_context": [], "chat": [], "share": []}
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

        kind = payload = None
        while True:
            if self._cancelled.is_set():
                self.transport.stop()
                raise ConnectCancelled("Connection attempt cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.transport.stop()
                raise ConnectError("No response from host (timed out). Check the address, port, and port forwarding or tunnel setup.")
            try:
                kind, payload = self._connect_result.get(timeout=min(0.15, remaining))
                break
            except queue.Empty:
                continue

        if kind == p.HELLO_REJECT:
            self.transport.stop()
            raise ConnectError(payload.get("reason", "Connection refused by host."))
        self.client_id = payload["client_id"]
        self.color = payload["color"]
        # The server may have renamed us to keep names unique (bob -> bob#2).
        self.username = sanitize_name(payload.get("username", username), NAME_MAX_LEN)
        with self._lock:
            self.expected_rev = payload["revision"]
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

    def local_edit(self, op: ot.Op, swap=None):
        """Sends a local edit. `swap`, when given, is a small dict
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
            if self.state == "synced":
                self.state = "awaiting"
                self.outstanding = op
                self.outstanding_meta = swap
                base_rev = self.expected_rev
                self._send_op(base_rev, op, swap)
            elif self.state == "awaiting":
                self.state = "awaiting_buffer"
                self.buffer = op
                self.buffer_meta = swap
            else:
                self.buffer = ot.compose(self.buffer, op)
                if swap is not None:
                    self.buffer_meta = swap

    def _send_op(self, base_rev, op, swap=None):
        payload = {"base_rev": base_rev, "ops": op.to_json()}
        if swap is not None:
            payload["swap"] = swap
        self.transport.send(self.server_addr, p.OP, payload, reliable=True)

    def send_cursor(self, index, has_selection, sel_start=None, sel_end=None):
        if not self.server_addr:
            return
        payload = {"index": index, "sel": has_selection}
        if has_selection:
            payload["start"] = sel_start
            payload["end"] = sel_end
        self.transport.send(self.server_addr, p.CURSOR, payload, reliable=False)

    def send_buffer_context(self, filename):
        if not self.server_addr:
            return
        self.transport.send(self.server_addr, p.BUFFER_CONTEXT, {"filename": filename}, reliable=True)

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
                with self._lock:
                    self.expected_rev = payload["revision"]
                    self.state = "synced"
                    self.outstanding = None
                    self.buffer = None
                    self.outstanding_meta = None
                    self.buffer_meta = None
                    self._doc_chunks = {}
                    self._doc_total = None
            else:
                self._connect_result.put((kind, payload))
        elif kind == p.HELLO_REJECT:
            self._connect_result.put((kind, payload))
        elif kind == p.DOC_CHUNK:
            self._handle_doc_chunk(payload)
        elif kind == p.OP:
            self._handle_remote_op(payload)
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

    def _handle_doc_chunk(self, payload):
        idx, total, text = payload["index"], payload["total"], payload["text"]
        self._doc_total = total
        self._doc_chunks[idx] = text
        if len(self._doc_chunks) == total:
            full = "".join(self._doc_chunks[i] for i in range(total))
            self._doc_chunks = {}
            self._doc_total = None
            self._fire("full_sync", full)

    def _handle_remote_op(self, payload):
        with self._lock:
            base_rev = payload["base_rev"]
            self.rev_buffer[base_rev] = payload
            self._drain_rev_buffer()

    def _drain_rev_buffer(self):
        while self.expected_rev in self.rev_buffer:
            payload = self.rev_buffer.pop(self.expected_rev)
            op = ot.Op.from_json(payload["ops"])
            is_mine = payload.get("from") == self.client_id
            swap = payload.get("swap") if isinstance(payload.get("swap"), dict) else None
            to_apply = None
            if is_mine:
                self._on_own_ack()
            else:
                to_apply = self._transform_incoming(op)
            self.expected_rev += 1
            if to_apply is not None and (swap is not None or not to_apply.is_noop()):
                self._fire("remote_op", to_apply, payload.get("from"), swap)

    def _on_own_ack(self):
        if self.state == "awaiting":
            self.state = "synced"
            self.outstanding = None
            self.outstanding_meta = None
        elif self.state == "awaiting_buffer":
            self.state = "awaiting"
            self.outstanding = self.buffer
            self.outstanding_meta = self.buffer_meta
            self.buffer = None
            self.buffer_meta = None
            self._send_op(self.expected_rev + 1, self.outstanding, self.outstanding_meta)

    def _transform_incoming(self, op):
        if self.state == "synced":
            return op
        if self.state == "awaiting":
            self.outstanding, op2 = ot.transform(self.outstanding, op)
            return op2
        self.outstanding, op2 = ot.transform(self.outstanding, op)
        self.buffer, op3 = ot.transform(self.buffer, op2)
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
    (getting the current document text to seed a promoted server, and
    reacting once reconnection succeeds or gives up) so this class can be
    unit-tested with plain Server/Client sockets, no Tk involved."""

    def __init__(self, username, advertise_host, advertise_port, get_doc_text,
                 on_reconnected, on_failed, reconnect_delay=(0.3, 0.6), advertise_socket=None):
        self.username = username
        self.advertise_host = advertise_host
        self.advertise_port = advertise_port
        self.get_doc_text = get_doc_text
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
        doc = self.get_doc_text() if self.get_doc_text else ""
        sock = self.advertise_socket.detach() if self.advertise_socket else None
        try:
            server = Server(self.advertise_port, initial_doc=doc, sock=sock)
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

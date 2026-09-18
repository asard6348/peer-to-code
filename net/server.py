import itertools
import threading
import time

import ot
from . import transport as p
from .transport import ReliableUDP

COLORS = ["#e06c75", "#61afef", "#98c379", "#e5c07b", "#c678dd", "#56b6c2", "#d19a66", "#be5046"]


class ClientRecord:
    def __init__(self, client_id, addr, username, color, p2p_host=None, p2p_port=None, is_sequencer=False):
        self.client_id = client_id
        self.addr = addr
        self.username = username
        self.color = color
        self.cursor = None
        self.p2p_host = p2p_host
        self.p2p_port = p2p_port
        self.is_sequencer = is_sequencer


class Server:
    def __init__(self, port, initial_doc="", max_clients=32, sock=None):
        self.port = port
        self.doc = initial_doc
        self.history = []
        self.revision = 0
        self.clients = {}
        self.clients_by_addr = {}
        self._id_gen = itertools.count(1)
        self._lock = threading.RLock()
        self.max_clients = max_clients
        self.transport = ReliableUDP(("0.0.0.0", port), sock=sock)
        self.transport.on_message = self._on_message
        self.transport.on_peer_timeout = self._on_peer_timeout
        self.log = []
        self.on_log = None
        self._heartbeat_stop = threading.Event()
        self.buffer_context = None

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
                {"id": c.client_id, "name": c.username, "color": c.color, "cursor": c.cursor,
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
        elif kind == p.PING:
            self.transport.send(addr, p.PONG, {}, reliable=False)
        elif kind == p.GOODBYE:
            self._handle_goodbye(addr)

    def _handle_hello(self, payload, addr):
        with self._lock:
            if len(self.clients) >= self.max_clients:
                self.transport.send(addr, p.HELLO_REJECT, {"reason": "Session is full."}, reliable=True)
                return
            username = str(payload.get("username", "anon"))[:32] or "anon"
            client_id = next(self._id_gen)
            color = COLORS[(client_id - 1) % len(COLORS)]
            p2p_host = payload.get("p2p_host")
            p2p_port = payload.get("p2p_port")
            is_sequencer = bool(payload.get("is_sequencer"))
            rec = ClientRecord(client_id, addr, username, color,
                                p2p_host=p2p_host, p2p_port=p2p_port, is_sequencer=is_sequencer)
            self.clients[client_id] = rec
            self.clients_by_addr[addr] = client_id
            revision = self.revision
            doc = self.doc

        self.transport.send(addr, p.WELCOME, {
            "client_id": client_id, "color": color, "revision": revision,
        }, reliable=True)
        for i, chunk in enumerate(p.chunk_text(doc)):
            self.transport.send(addr, p.DOC_CHUNK, {
                "index": i, "total": len(p.chunk_text(doc)), "text": chunk, "revision": revision,
            }, reliable=True)
        with self._lock:
            ctx = self.buffer_context
        if ctx:
            self.transport.send(addr, p.BUFFER_CONTEXT, ctx, reliable=True)
        self._broadcast(p.PEERS, self._roster_payload())
        self._emit_log(f"{username} joined from {addr[0]}:{addr[1]}")

    def _handle_op(self, payload, addr):
        with self._lock:
            client_id = self.clients_by_addr.get(addr)
            if client_id is None:
                return
            try:
                base_rev = int(payload["base_rev"])
                op = ot.Op.from_json(payload["ops"])
            except (KeyError, ValueError, TypeError):
                return
            base_rev = max(0, min(base_rev, self.revision))
            for hist_op in self.history[base_rev:self.revision]:
                op, _ = ot.transform(op, hist_op)
            try:
                self.doc = op.apply(self.doc)
            except ValueError:
                self._resend_full_sync(addr)
                return
            self.history.append(op)
            applied_at = self.revision
            self.revision += 1

        self._broadcast(p.OP, {"base_rev": applied_at, "ops": op.to_json(), "from": client_id}, reliable=True)

    def _resend_full_sync(self, addr):
        with self._lock:
            revision, doc = self.revision, self.doc
        chunks = p.chunk_text(doc)
        self.transport.send(addr, p.WELCOME, {"client_id": self.clients_by_addr.get(addr, 0), "revision": revision, "resync": True}, reliable=True)
        for i, chunk in enumerate(chunks):
            self.transport.send(addr, p.DOC_CHUNK, {"index": i, "total": len(chunks), "text": chunk, "revision": revision}, reliable=True)

    def _handle_buffer_context(self, payload, addr):
        with self._lock:
            client_id = self.clients_by_addr.get(addr)
            if client_id is None:
                return
            ctx = {"filename": payload.get("filename"), "by": client_id}
            self.buffer_context = ctx
        self._broadcast(p.BUFFER_CONTEXT, ctx, reliable=True)

    def _handle_cursor(self, payload, addr):
        with self._lock:
            client_id = self.clients_by_addr.get(addr)
            if client_id is None:
                return
            rec = self.clients.get(client_id)
            if rec:
                rec.cursor = payload
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
            self._emit_log(f"{rec.username} disconnected")
            self._broadcast(p.PEERS, self._roster_payload())

    def snapshot(self):
        with self._lock:
            return {"revision": self.revision, "peers": len(self.clients), "doc_len": len(self.doc)}

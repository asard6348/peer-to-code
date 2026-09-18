"""The network layer this whole app talks over: how a message is packed into
bytes, and how those bytes get delivered over UDP without silently getting
lost. The two used to live in separate files, but neither means anything
without the other, so they live together here now.

Wire format: every packet starts with a fixed header (a magic value so we
never mistake noise from some other program on the same port for our own
traffic, a message kind, a sequence number, and a reliability flag), followed
by a JSON body — or, for oversized messages, by a small fragment header and
a slice of the JSON body (see "Fragmentation" below). Kept deliberately
plain text and simple to decode by hand if you ever need to read a packet
capture.

Reliability: plain UDP can drop or reorder packets, so ReliableUDP adds a
thin layer on top. Anything sent with reliable=True gets retried on a timer
until the other side acknowledges it, and duplicate deliveries are filtered
out on arrival so a retried packet never gets applied twice. Anything sent
with reliable=False (cursor position updates, pings) is fire-and-forget,
since losing one of those just means the next one corrects it.

Fragmentation: a single sendto() call fails outright (OSError: Message too
long / EMSGSIZE) once a datagram exceeds whatever ceiling the local network
stack enforces. That ceiling isn't the same everywhere — it's well below the
65507-byte theoretical UDP max on some Android/Termux loopback setups, and
real Wi-Fi/mobile links fragment or drop oversized datagrams even when the
socket itself accepts them. A single large edit (opening or pasting a big
file, replacing the whole shared buffer) can easily produce a JSON body of
several hundred KB, so send() transparently splits anything bigger than
FRAGMENT_SIZE into several small fragments, each sent and acknowledged as
its own datagram, and the receiving side reassembles them before handing a
complete message to on_message. Every caller of send() stays oblivious to
this; only ReliableUDP itself knows fragments exist.
"""

import itertools
import json
import socket
import struct
import threading
import time

MAGIC = b"CE1"

HELLO = 1
WELCOME = 2
HELLO_REJECT = 3
OP = 4
ACK = 5
DOC_CHUNK = 6
PEERS = 7
CURSOR = 8
PING = 9
PONG = 10
GOODBYE = 11
HOST_SHUTDOWN = 12
BUFFER_CONTEXT = 13

HEADER = struct.Struct("!3sBIB")
FRAG_HEADER = struct.Struct("!IHH")

FLAG_RELIABLE = 1
FLAG_FRAGMENT = 2

MAX_DATAGRAM = 60000

FRAGMENT_SIZE = 1200


def parse_header(raw: bytes):
    """Parses the fixed header (and fragment header, if present) off the
    front of a packet, returning (kind, seq, reliable, frag_info, body).
    frag_info is None for an ordinary packet, or (frag_id, frag_index,
    frag_count) for one piece of a fragmented message. body is the
    remaining raw bytes — a complete JSON document for a non-fragmented
    packet, or one slice of one for a fragment."""
    if len(raw) < HEADER.size:
        raise ValueError("packet too short")
    magic, kind, seq, flags = HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise ValueError("bad magic")
    offset = HEADER.size
    frag_info = None
    if flags & FLAG_FRAGMENT:
        if len(raw) < offset + FRAG_HEADER.size:
            raise ValueError("truncated fragment header")
        frag_info = FRAG_HEADER.unpack_from(raw, offset)
        offset += FRAG_HEADER.size
    return kind, seq, bool(flags & FLAG_RELIABLE), frag_info, raw[offset:]


def chunk_text(text: str, chunk_size: int = 8000):
    """Splits a document into pieces small enough to fit in one datagram, for
    the initial full-sync send. Always returns at least one chunk, even for
    an empty document, so the receiving side has something to assemble."""
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    return chunks or [""]


RETRANSMIT_INTERVAL = 0.25
MAX_RETRIES = 20


class Peer:
    """Per-remote-address bookkeeping: outbound reliability plus inbound
    duplicate detection, kept separately for every address we talk to."""

    def __init__(self, addr):
        self.addr = addr
        self.send_seq = itertools.count(1)
        self.pending = {}
        self.seen_seq = set()
        self.last_seen_seq_order = []
        self.alive = True
        self.last_activity = time.monotonic()
        self.frag_id_seq = itertools.count(1)
        self.frag_buffers = {}


class ReliableUDP:
    """A minimal reliable-messaging layer over a UDP socket. Everything else
    in net/ talks through this rather than touching sockets directly."""

    def __init__(self, bind_addr=("0.0.0.0", 0), sock=None):
        if sock is not None:
            self.sock = sock
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(bind_addr)
        self.sock.settimeout(0.5)
        self.local_port = self.sock.getsockname()[1]
        self.peers = {}
        self.on_message = None
        self.on_peer_timeout = None
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._retransmit_loop, daemon=True).start()

    def stop(self):
        self._running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def _get_peer(self, addr):
        with self._lock:
            peer = self.peers.get(addr)
            if peer is None:
                peer = Peer(addr)
                self.peers[addr] = peer
            return peer

    def send(self, addr, kind, payload, reliable=True):
        peer = self._get_peer(addr)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(body) <= FRAGMENT_SIZE:
            return self._send_datagram(peer, addr, kind, reliable, body)
        frag_id = next(peer.frag_id_seq) & 0xFFFFFFFF
        pieces = [body[i:i + FRAGMENT_SIZE] for i in range(0, len(body), FRAGMENT_SIZE)]
        frag_count = len(pieces)
        last_seq = None
        for idx, piece in enumerate(pieces):
            last_seq = self._send_datagram(peer, addr, kind, reliable, piece,
                                            frag=(frag_id, idx, frag_count))
        return last_seq

    def _send_datagram(self, peer, addr, kind, reliable, body, frag=None):
        seq = next(peer.send_seq) if reliable else 0
        flags = (FLAG_RELIABLE if reliable else 0) | (FLAG_FRAGMENT if frag else 0)
        raw = HEADER.pack(MAGIC, kind, seq, flags)
        if frag is not None:
            raw += FRAG_HEADER.pack(*frag)
        raw += body
        try:
            self.sock.sendto(raw, addr)
        except OSError:
            return seq
        if reliable:
            with self._lock:
                peer.pending[seq] = [raw, time.monotonic(), time.monotonic(), 0]
        return seq

    def _send_ack(self, addr, seq):
        raw = HEADER.pack(MAGIC, ACK, seq, 0)
        try:
            self.sock.sendto(raw, addr)
        except OSError:
            pass

    def _reassemble(self, peer, frag_info, chunk):
        """Buffers one fragment; returns the joined body once every fragment
        of that message has arrived, or None while still waiting."""
        frag_id, frag_index, frag_count = frag_info
        with self._lock:
            buf = peer.frag_buffers.setdefault(frag_id, {})
            buf[frag_index] = chunk
            if len(buf) < frag_count:
                return None
            pieces = [buf[i] for i in range(frag_count)]
            del peer.frag_buffers[frag_id]
        return b"".join(pieces)

    def _recv_loop(self):
        while self._running:
            try:
                raw, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                kind, seq, reliable, frag_info, body = parse_header(raw)
            except (ValueError, struct.error):
                continue
            peer = self._get_peer(addr)
            peer.last_activity = time.monotonic()
            peer.alive = True
            if kind == ACK:
                with self._lock:
                    peer.pending.pop(seq, None)
                continue
            if reliable:
                self._send_ack(addr, seq)
                with self._lock:
                    if seq in peer.seen_seq:
                        continue
                    peer.seen_seq.add(seq)
                    peer.last_seen_seq_order.append(seq)
                    if len(peer.last_seen_seq_order) > 4096:
                        old = peer.last_seen_seq_order.pop(0)
                        peer.seen_seq.discard(old)
            if frag_info is not None:
                body = self._reassemble(peer, frag_info, body)
                if body is None:
                    continue
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                continue
            if self.on_message:
                self.on_message(kind, payload, addr)

    def _retransmit_loop(self):
        while self._running:
            time.sleep(RETRANSMIT_INTERVAL)
            now = time.monotonic()
            with self._lock:
                items = list(self.peers.items())
            for addr, peer in items:
                with self._lock:
                    pending = list(peer.pending.items())
                for seq, entry in pending:
                    raw, first_ts, last_ts, retries = entry
                    if now - last_ts < RETRANSMIT_INTERVAL:
                        continue
                    if retries >= MAX_RETRIES:
                        with self._lock:
                            peer.pending.pop(seq, None)
                        if peer.alive:
                            peer.alive = False
                            if self.on_peer_timeout:
                                self.on_peer_timeout(addr)
                        continue
                    try:
                        self.sock.sendto(raw, addr)
                    except OSError:
                        pass
                    with self._lock:
                        if seq in peer.pending:
                            peer.pending[seq][2] = now
                            peer.pending[seq][3] += 1


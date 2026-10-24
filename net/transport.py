"""Wire format and reliable delivery over UDP.

Every packet is a fixed header (magic, kind, sequence number, flags) plus a
JSON body, or a fragment header plus a slice of one for oversized messages.
Reliable packets are retried until acknowledged and de-duplicated on arrival;
unreliable ones (cursors, pings) are fire-and-forget. Bodies above
FRAGMENT_SIZE are split into separately acknowledged datagrams and reassembled
before delivery, because many network stacks drop or reject large datagrams.
"""

import collections
import itertools
import json
import socket
import struct
import threading
import time
import traceback

# Bump on incompatible changes; mismatched peers get a clear message instead of a timeout.
PROTOCOL_VERSION = 2
MAGIC = b"CE%d" % PROTOCOL_VERSION

# Older versions a client probes for: their servers answer any PING with a PONG,
# which reveals that a host silent to our HELLO is merely old.
PROBE_VERSIONS = (1,)


def version_of(magic: bytes):
    """Protocol version encoded in a magic, or None if it isn't ours."""
    if len(magic) == 3 and magic[:2] == b"CE" and magic[2:].isdigit():
        return int(magic[2:])
    return None


def mismatch_message(theirs, host_is_theirs):
    """User-facing text for a protocol mismatch; host_is_theirs means we are joining their host."""
    if host_is_theirs:
        if theirs < PROTOCOL_VERSION:
            return (f"The host runs an older version of Peer to Code (protocol {theirs}) that this "
                    f"version (protocol {PROTOCOL_VERSION}) can't talk to. Ask the host to update "
                    f"to the latest version.")
        return (f"The host runs a newer version of Peer to Code (protocol {theirs}) that this "
                f"version (protocol {PROTOCOL_VERSION}) can't talk to. Please update to the latest "
                f"version.")
    if theirs < PROTOCOL_VERSION:
        return (f"This host runs a newer version of Peer to Code (protocol {PROTOCOL_VERSION}) than "
                f"yours (protocol {theirs}). Please update to the latest version and try again.")
    return (f"This host runs an older version of Peer to Code (protocol {PROTOCOL_VERSION}) than "
            f"yours (protocol {theirs}). Ask the host to update to the latest version.")

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
CHAT = 14
RUN_START = 15
RUN_OUTPUT = 16
RUN_END = 17
DOC_OPEN = 18

# Each shared document has its own id, revision and op history; id 1 always exists.
MAIN_DOC = 1

HEADER = struct.Struct("!3sBIB")
FRAG_HEADER = struct.Struct("!IHH")

FLAG_RELIABLE = 1
FLAG_FRAGMENT = 2

MAX_DATAGRAM = 60000
FRAGMENT_SIZE = 1200
MAX_FRAGMENTS = 8192
MAX_INFLIGHT_FRAGMENTED = 32
FRAGMENT_TTL = 30.0
SEEN_WINDOW = 4096


def parse_header(raw: bytes):
    """Return (kind, seq, reliable, frag_info, body); frag_info is None or (id, index, count)."""
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
        if not 0 < frag_info[2] <= MAX_FRAGMENTS or frag_info[1] >= frag_info[2]:
            raise ValueError("bad fragment header")
        offset += FRAG_HEADER.size
    return kind, seq, bool(flags & FLAG_RELIABLE), frag_info, raw[offset:]


def chunk_text(text: str, chunk_size: int = 8000):
    """Split text into chunks for the initial sync; always at least one, even when empty."""
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    return chunks or [""]


RETRANSMIT_INTERVAL = 0.25
MAX_RETRIES = 20


class Peer:
    """Per-address state: outbound retries, inbound de-duplication, fragment buffers."""

    def __init__(self, addr):
        self.addr = addr
        self.send_seq = itertools.count(1)
        self.pending = {}
        self.seen_seq = set()
        self.seen_order = collections.deque()
        self.alive = True
        self.last_activity = time.monotonic()
        self.frag_id_seq = itertools.count(1)
        self.frag_buffers = {}      # frag_id -> (first_seen, {index: bytes})

    def is_duplicate(self, seq):
        """Record seq; True if it was already delivered (sliding window)."""
        if seq in self.seen_seq:
            return True
        self.seen_seq.add(seq)
        self.seen_order.append(seq)
        if len(self.seen_order) > SEEN_WINDOW:
            self.seen_seq.discard(self.seen_order.popleft())
        return False


class ReliableUDP:
    """Reliable messaging over a UDP socket; the rest of net/ never touches sockets directly."""

    def __init__(self, bind_addr=("0.0.0.0", 0), sock=None):
        if sock is not None:
            self.sock = sock
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(bind_addr)
        self.sock.settimeout(0.5)
        self.local_port = self.sock.getsockname()[1]
        self.peers = {}
        self.on_message = None
        self.on_peer_timeout = None
        self.on_foreign_version = None      # (addr, version, kind, seq, flags)
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

    def drop_peer(self, addr):
        """Forget all state for an address (call when it leaves for good)."""
        with self._lock:
            self.peers.pop(addr, None)

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
        if frag_count > MAX_FRAGMENTS:
            raise ValueError(f"message too large ({len(body)} bytes)")
        last_seq = None
        for idx, piece in enumerate(pieces):
            last_seq = self._send_datagram(peer, addr, kind, reliable, piece,
                                            frag=(frag_id, idx, frag_count))
        return last_seq

    def _send_datagram(self, peer, addr, kind, reliable, body, frag=None):
        seq = next(peer.send_seq) & 0xFFFFFFFF if reliable else 0
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

    def send_as_version(self, addr, version, kind, seq=0, payload=None):
        """One unreliable packet with another version's magic, to answer a peer in its own dialect."""
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        try:
            self.sock.sendto(HEADER.pack(b"CE%d" % version, kind, seq, 0) + body, addr)
        except OSError:
            pass

    def _send_ack(self, addr, seq):
        raw = HEADER.pack(MAGIC, ACK, seq, 0)
        try:
            self.sock.sendto(raw, addr)
        except OSError:
            pass

    def _reassemble(self, peer, frag_info, chunk):
        """Buffer one fragment; return the joined body once complete, else None."""
        frag_id, frag_index, frag_count = frag_info
        now = time.monotonic()
        with self._lock:
            for fid in [f for f, (t, _) in peer.frag_buffers.items() if now - t > FRAGMENT_TTL]:
                del peer.frag_buffers[fid]
            if frag_id not in peer.frag_buffers and len(peer.frag_buffers) >= MAX_INFLIGHT_FRAGMENTED:
                del peer.frag_buffers[next(iter(peer.frag_buffers))]
            _, buf = peer.frag_buffers.setdefault(frag_id, (now, {}))
            buf[frag_index] = chunk
            if len(buf) < frag_count:
                return None
            del peer.frag_buffers[frag_id]
        try:
            return b"".join(buf[i] for i in range(frag_count))
        except KeyError:
            return None

    def _note_foreign_packet(self, raw, addr):
        if self.on_foreign_version is None or len(raw) < HEADER.size:
            return
        magic, kind, seq, flags = HEADER.unpack_from(raw, 0)
        version = version_of(magic)
        if version is None or version == PROTOCOL_VERSION:
            return
        try:
            self.on_foreign_version(addr, version, kind, seq, flags)
        except Exception:
            traceback.print_exc()

    def _recv_loop(self):
        while self._running:
            try:
                raw, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:    # Windows reports ICMP "port unreachable" this way
                continue
            except OSError:
                if not self._running or self.sock.fileno() == -1:
                    break
                time.sleep(0.1)
                continue
            try:
                kind, seq, reliable, frag_info, body = parse_header(raw)
            except (ValueError, struct.error):
                self._note_foreign_packet(raw, addr)
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
                    if peer.is_duplicate(seq):
                        continue
            if frag_info is not None:
                body = self._reassemble(peer, frag_info, body)
                if body is None:
                    continue
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                continue
            if self.on_message:
                try:     # a bad packet must not kill the receive thread
                    self.on_message(kind, payload, addr)
                except Exception:
                    traceback.print_exc()

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
                                try:
                                    self.on_peer_timeout(addr)
                                except Exception:
                                    traceback.print_exc()
                        continue
                    try:
                        self.sock.sendto(raw, addr)
                    except OSError:
                        pass
                    with self._lock:
                        if seq in peer.pending:
                            peer.pending[seq][2] = now
                            peer.pending[seq][3] += 1


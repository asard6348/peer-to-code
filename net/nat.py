"""Best-effort NAT traversal for the peer-to-peer mesh.

STUN (RFC 5389) tells a peer the public address its socket appears as, and UDP
hole punching opens return paths through restricted-cone NATs. Symmetric NATs
(mobile/carrier-grade, strict corporate) need a relay, which is out of scope.
"""

import os
import socket
import struct
import threading
import time

from . import transport as p

STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun2.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
    ("stun.nextcloud.com", 3478),
]

KEEPALIVE_INTERVAL = 15.0

_MAGIC_COOKIE = 0x2112A442
_BINDING_REQUEST = 0x0001
_BINDING_RESPONSE = 0x0101
_ATTR_MAPPED_ADDRESS = 0x0001
_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_ATTR_XOR_MAPPED_ADDRESS_OLD = 0x8020
_FAMILY_IPV4 = 0x01
_STUN_HEADER = struct.Struct("!HHI12s")
_PUNCH = p.HEADER.pack(p.MAGIC, p.PING, 0, 0)


def _build_binding_request(txn_id: bytes) -> bytes:
    return _STUN_HEADER.pack(_BINDING_REQUEST, 0, _MAGIC_COOKIE, txn_id)


def _parse_binding_response(data: bytes, txn_id: bytes):
    """(ip, port) from a matching STUN binding response, else None."""
    if len(data) < _STUN_HEADER.size:
        return None
    msg_type, msg_len, _cookie, resp_txn = _STUN_HEADER.unpack_from(data, 0)
    if msg_type != _BINDING_RESPONSE or resp_txn != txn_id:
        return None
    body = data[_STUN_HEADER.size:_STUN_HEADER.size + msg_len]
    plain = None
    i = 0
    while i + 4 <= len(body):
        attr_type, attr_len = struct.unpack_from("!HH", body, i)
        val = body[i + 4:i + 4 + attr_len]
        if len(val) < attr_len:
            break
        if len(val) >= 8 and val[1] == _FAMILY_IPV4:
            port = struct.unpack_from("!H", val, 2)[0]
            if attr_type in (_ATTR_XOR_MAPPED_ADDRESS, _ATTR_XOR_MAPPED_ADDRESS_OLD):
                xaddr = struct.unpack_from("!I", val, 4)[0] ^ _MAGIC_COOKIE
                return socket.inet_ntoa(struct.pack("!I", xaddr)), port ^ (_MAGIC_COOKIE >> 16)
            if attr_type == _ATTR_MAPPED_ADDRESS:
                plain = (socket.inet_ntoa(val[4:8]), port)
        i += 4 + attr_len + (-attr_len) % 4
    return plain


def discover_public_addr(sock: socket.socket, servers=None, timeout=1.0, attempts=1):
    """(public_ip, public_port) of a bound UDP socket as a STUN server sees it, or None.

    Takes over the socket's timeout and reads from it, so call it before anything else
    receives on that socket. Replies are matched by transaction id alone because
    anycast STUN clusters may answer from a different IP than the one queried."""
    prev_timeout = sock.gettimeout()
    try:
        for host, port in (servers or STUN_SERVERS):
            try:
                server_addr = (socket.gethostbyname(host), port)
            except OSError:
                continue
            for _ in range(attempts):
                txn_id = os.urandom(12)
                deadline = time.monotonic() + timeout
                try:
                    sock.sendto(_build_binding_request(txn_id), server_addr)
                    while (remaining := deadline - time.monotonic()) > 0:
                        sock.settimeout(remaining)
                        result = _parse_binding_response(sock.recvfrom(2048)[0], txn_id)
                        if result:         # stray datagrams are skipped, not treated as a miss
                            return result
                except OSError:            # timeout, unreachable, connection reset
                    continue
        return None
    finally:
        sock.settimeout(prev_timeout)


def guess_local_ip():
    """The local IP the OS would route outbound traffic through, or None when there is no network.

    Never returns loopback, so callers can tell "no network" from "found one"."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        probe.connect(("8.8.8.8", 80))      # picks an interface; sends nothing
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _local_fallback_addr(sock: socket.socket):
    """LAN address to advertise when STUN fails (loopback if there is no network at all)."""
    return guess_local_ip() or "127.0.0.1", sock.getsockname()[1]


def send_punch(sock: socket.socket, addr):
    """Send one throwaway datagram so a NAT accepts return traffic from addr; never raises."""
    try:
        sock.sendto(_PUNCH, addr)
    except OSError:
        pass


def quick_public_ip(timeout=1.0):
    """This machine's public IP via STUN on a throwaway socket, or None on any failure."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        probe.bind(("0.0.0.0", 0))
        addr = discover_public_addr(probe, timeout=timeout)
        return addr[0] if addr else None
    except OSError:
        return None
    finally:
        probe.close()


class AdvertiseSocket:
    """A peer's advertised UDP socket for the whole P2P session.

    Bound and STUN-probed up front so a later promotion to sequencer reuses an already-warm
    public address. A background thread keeps the NAT mapping alive and punches toward every
    peer from note_peers(). detach() hands the open socket to a reader (ReliableUDP); the loop
    only sends, so it never competes with that reader. close() stops the loop and closes the
    socket unless detach() passed ownership on."""

    def __init__(self, port, on_log=None):
        self._on_log = on_log or (lambda msg: None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(("0.0.0.0", port))
        except OSError:
            self.sock.close()
            raise
        self.local_port = self.sock.getsockname()[1]
        self.public_addr = None
        self._targets = set()
        self._lock = threading.Lock()
        self._handed_off = False
        self._closed = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Run the initial STUN probe (blocking: call off the UI thread) and start the keepalive loop.
        public_addr stays None if STUN finds nothing."""
        try:
            self.public_addr = discover_public_addr(self.sock)
        except OSError:
            self.public_addr = None
        if self.public_addr is None:
            self._on_log("STUN: no server reachable; falling back to local address.")
        else:
            self._on_log(f"STUN: public address is {self.public_addr[0]}:{self.public_addr[1]}")
        if not self._closed:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def fallback_addr(self):
        return _local_fallback_addr(self.sock)

    @property
    def is_closed(self):
        """True once close() has run (as opposed to a normal detach())."""
        return self._closed

    def note_peers(self, peer_addrs):
        """Add (ip, port) pairs to keep punching toward and punch new ones immediately.
        Targets accumulate so a peer dropping off the roster briefly keeps its pinhole."""
        with self._lock:
            new = {tuple(a) for a in peer_addrs if a} - self._targets
            self._targets.update(new)
        for addr in new:
            send_punch(self.sock, addr)

    def _loop(self):
        stun_target = None
        next_stun_refresh = 0.0
        while not self._stop.wait(KEEPALIVE_INTERVAL):
            now = time.monotonic()
            with self._lock:
                targets = list(self._targets)
            for addr in targets:
                send_punch(self.sock, addr)
            if now < next_stun_refresh:
                continue
            if stun_target is None:
                for host, port in STUN_SERVERS:
                    try:
                        stun_target = (socket.gethostbyname(host), port)
                        break
                    except OSError:
                        continue
            if stun_target is not None:
                try:
                    self.sock.sendto(_build_binding_request(os.urandom(12)), stun_target)
                except OSError:
                    pass
            next_stun_refresh = now + KEEPALIVE_INTERVAL * 4

    def detach(self):
        """Hand the open, bound socket to a reader (ReliableUDP). Returns None on repeat calls."""
        if self._handed_off:
            return None
        self._handed_off = True
        return self.sock

    def close(self):
        """Stop the loop and close the socket unless detach() passed it on. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if not self._handed_off:
            try:
                self.sock.close()
            except OSError:
                pass

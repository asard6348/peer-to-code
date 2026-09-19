"""Best-effort NAT traversal for the peer-to-peer mesh: STUN to find out
what address a peer actually looks like from the outside, plus UDP hole
punching to get a mapping open before it's needed.

WHAT THIS DOES AND DOESN'T SOLVE
Two peers behind home routers can't just dial each other's LAN address -
it isn't reachable from outside. STUN (RFC 5389) fixes the "what's my
public address" half of that: a peer sends a request to a public STUN
server, and the server's reply says what (ip, port) the packet actually
came from on the internet, which is the peer's NAT mapping for that
socket. Sharing *that* address (instead of a LAN address or "you'll need
to port-forward") is what makes an unmodified home connection reachable
at all for a decent chunk of real-world routers.

The part STUN doesn't solve is symmetric NAT: some routers (common on
carrier-grade/mobile NAT, and some strict corporate firewalls) hand out a
*different* external port for every destination a socket talks to, so
the mapping a STUN server observes isn't the one a third peer would see.
Getting through that reliably needs a relay the two sides both already
trust (TURN) - a piece of always-on public infrastructure this project
doesn't run and can't fake locally. So: this gets you real internet P2P
for the common case (full-cone and (address/port)-restricted-cone NATs,
which covers most consumer routers), not every network topology.

HOLE PUNCHING
Discovering a public address isn't enough by itself if the local router
only accepts inbound packets from destinations it has already seen this
socket send *to* (restricted-cone / port-restricted-cone). AdvertiseSocket
handles that by sending small unsolicited "punch" datagrams toward every
peer it learns about (see note_peers()) - once both sides have punched
toward each other, each router has seen outbound traffic to the other
side and will let its reply back in. It also keeps the STUN-observed
mapping itself alive with periodic traffic, since NAT table entries
expire after a period of silence (routers vary, but well under a minute
of total silence is not unusual).
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

_MAGIC_COOKIE = 0x2112A442
_BINDING_REQUEST = 0x0001
_BINDING_RESPONSE = 0x0101
_ATTR_MAPPED_ADDRESS = 0x0001
_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_ATTR_XOR_MAPPED_ADDRESS_OLD = 0x8020
_FAMILY_IPV4 = 0x01

_STUN_HEADER = struct.Struct("!HHI12s")


def _build_binding_request(txn_id: bytes) -> bytes:
    return _STUN_HEADER.pack(_BINDING_REQUEST, 0, _MAGIC_COOKIE, txn_id)


def _parse_binding_response(data: bytes, txn_id: bytes):
    """Returns (ip, port) parsed out of a STUN binding response, or None
    if data isn't a matching, well-formed response."""
    if len(data) < _STUN_HEADER.size:
        return None
    msg_type, msg_len, cookie, resp_txn = _STUN_HEADER.unpack_from(data, 0)
    if msg_type != _BINDING_RESPONSE or resp_txn != txn_id:
        return None
    body = data[_STUN_HEADER.size:_STUN_HEADER.size + msg_len]
    plain_result = None
    i = 0
    while i + 4 <= len(body):
        attr_type, attr_len = struct.unpack_from("!HH", body, i)
        val = body[i + 4:i + 4 + attr_len]
        if len(val) < attr_len:
            break
        if attr_type in (_ATTR_XOR_MAPPED_ADDRESS, _ATTR_XOR_MAPPED_ADDRESS_OLD) and len(val) >= 8:
            family = val[1]
            if family == _FAMILY_IPV4:
                xport = struct.unpack_from("!H", val, 2)[0] ^ (_MAGIC_COOKIE >> 16)
                xaddr = struct.unpack_from("!I", val, 4)[0] ^ _MAGIC_COOKIE
                ip = socket.inet_ntoa(struct.pack("!I", xaddr))
                return ip, xport
        elif attr_type == _ATTR_MAPPED_ADDRESS and len(val) >= 8:
            family = val[1]
            if family == _FAMILY_IPV4:
                port = struct.unpack_from("!H", val, 2)[0]
                ip = socket.inet_ntoa(val[4:8])
                plain_result = (ip, port)
        i += 4 + attr_len + ((-attr_len) % 4)
    return plain_result


def discover_public_addr(sock: socket.socket, servers=None, timeout=1.0, attempts=1):
    """Sends a STUN binding request out through an already-bound UDP
    socket and returns (public_ip, public_port) as a STUN server saw it,
    or None if nothing answered in time. Takes over the socket's timeout
    and any datagrams that arrive while it's waiting; only call this
    before the socket is handed to anything else that also reads from it
    (ReliableUDP._recv_loop), never concurrently with it."""
    prev_timeout = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        for host, port in (servers or STUN_SERVERS):
            try:
                server_addr = (socket.gethostbyname(host), port)
            except OSError:
                continue
            for _ in range(attempts):
                txn_id = os.urandom(12)
                try:
                    sock.sendto(_build_binding_request(txn_id), server_addr)
                    data, from_addr = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue
                # Deliberately not checking from_addr against server_addr
                # here: Google's STUN cluster (stun.l.google.com and
                # friends) is anycast/load-balanced, so a legitimate reply
                # can arrive from a different front-end IP than the one
                # the request was sent to. The random 12-byte transaction
                # ID matched in _parse_binding_response already proves
                # this reply answers *our* request, so an extra source-IP
                # check only rejects genuine responses - silently turning
                # a working STUN lookup into a fallback to a private LAN
                # address that no one outside the network can reach.
                result = _parse_binding_response(data, txn_id)
                if result:
                    return result
        return None
    finally:
        sock.settimeout(prev_timeout)


def guess_local_ip():
    """Best-effort non-loopback IP for this machine - what the OS would
    route a packet out through, without actually sending one (a UDP
    "connect" just picks a local interface/address; it never calls
    sendto()). Used to give the Peer to Peer tab's Address field a
    sensible starting value, and as the fallback below when STUN itself
    turns up nothing.

    Returns None when there's no route at all (no network interface up,
    cable unplugged, Wi-Fi off, ...) rather than silently returning the
    loopback address - a loopback address is meaningless for Peer to
    Peer (nothing else can be reached at it) and callers that care about
    the difference (is there a network at all?) need to be able to tell
    "no network" apart from "network found, here it is"."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except Exception:
        return None
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except Exception:
        return None
    finally:
        probe.close()


def _local_fallback_addr(sock: socket.socket):
    """Best-effort LAN-visible address for when STUN turns up nothing (no
    internet, or every server was unreachable) - same as what this app
    would have advertised before NAT traversal existed. Unlike
    guess_local_ip() itself, this always returns *something* usable,
    falling back to loopback when there's genuinely no network - callers
    here have already committed to binding a socket and need an address
    to report, not a way to detect the no-network case (see
    connect_window._require_network for that)."""
    return guess_local_ip() or "127.0.0.1", sock.getsockname()[1]


_PUNCH = p.HEADER.pack(p.MAGIC, p.PING, 0, 0)


def send_punch(sock: socket.socket, addr):
    """Fires one throwaway datagram at addr to coax a NAT/firewall into
    accepting return traffic from it. Never raises - a punch that didn't
    get out is just a punch that didn't happen, not a caller's problem."""
    try:
        sock.sendto(_PUNCH, addr)
    except OSError:
        pass


KEEPALIVE_INTERVAL = 15.0


def quick_public_ip(timeout=1.0):
    """One-off STUN lookup for just this machine's public IP, on a
    throwaway socket bound to an OS-assigned port, not any port a mesh
    might actually use. For callers that need to answer "is this my own
    public address" before they've committed to anything that binds a
    specific, meaningful port - see connect_window._do_p2p_join's same-
    address check, which has to know the answer before deciding whether
    to touch the real advertise port at all. Binding that port up front
    regardless of the answer would mean two peers being tested on the
    same machine (typically both defaulting to the same port) fighting
    over it before the person ever got a chance to say no.

    Returns just the IP as a string, or None on any failure - same
    "couldn't tell" semantics as guess_local_ip(), for the same reason:
    this is a best-effort UI convenience, never something that should
    raise into a caller that didn't ask to handle networking errors."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except Exception:
        return None
    try:
        probe.bind(("0.0.0.0", 0))
        addr = discover_public_addr(probe, timeout=timeout)
        return addr[0] if addr else None
    except Exception:
        return None
    finally:
        probe.close()


class AdvertiseSocket:
    """Owns a peer's own advertise-port UDP socket for the lifetime of a
    P2P session, from before anyone knows whether this peer will ever
    actually become sequencer.

    Binding and STUN-probing that port up front - rather than only once a
    failover promotes this peer - is what lets a later promotion reuse a
    public address that's already known and already warm, instead of
    starting a fresh NAT negotiation at the exact moment the mesh most
    needs this peer reachable. Between now and any promotion, a
    background thread keeps the mapping alive and punches toward every
    peer this session learns about via note_peers(), so that by the time
    (if ever) this peer is promoted, the other peers' routers have
    already seen it and will let its traffic through.

    detach() hands the raw, still-open socket over to whatever adopts it
    for *reading* (net.server.Server, via ReliableUDP's sock= param).
    This object's own background loop keeps running afterward, on the
    same socket - that's deliberate, not an oversight: the loop only
    ever calls sendto() (see send_punch()/_loop() below), never
    recvfrom(), so it never competes with the new owner's receive loop,
    and it's exactly what keeps this peer's external NAT mapping (and
    therefore its advertised address) alive for as long as the session
    lasts, not just until the socket happens to change hands. Without
    this, a peer with no other traffic yet flowing on the socket - most
    notably a mesh's very first member, sitting there advertised but
    unjoined - would have nothing left periodically refreshing its
    public mapping, and its router would quietly let it expire.

    close() is the actual "stop everything": it always stops the
    background loop, and additionally closes the socket itself unless
    detach() already handed that off to someone else who now owns
    closing it (Server.stop(), via ReliableUDP.stop()).
    """

    def __init__(self, port, on_log=None):
        self._on_log = on_log or (lambda msg: None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.local_port = self.sock.getsockname()[1]
        self.public_addr = None
        self._targets = set()
        self._lock = threading.Lock()
        self._handed_off = False
        self._closed = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Runs an initial STUN probe (blocking - call this off the UI
        thread) and starts the background keepalive/punch loop. Safe to
        call even when STUN turns up nothing; public_addr just stays None
        and callers should fall back to a manually-entered address."""
        try:
            self.public_addr = discover_public_addr(self.sock)
        except OSError:
            self.public_addr = None
        if self.public_addr is None:
            self._on_log("STUN: no server reachable; falling back to local address.")
        else:
            self._on_log(f"STUN: public address is {self.public_addr[0]}:{self.public_addr[1]}")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def fallback_addr(self):
        return _local_fallback_addr(self.sock)

    @property
    def is_closed(self):
        """True once close() has run - the signal callers use to tell a
        genuine close (e.g. Cancel, fired from another thread while
        start()'s STUN probe was still blocking) apart from a normal
        detach()."""
        return self._closed

    def note_peers(self, peer_addrs):
        """Registers (ip, port) pairs to keep punching toward, e.g. every
        reachable peer's advertised p2p address from the current roster.
        Safe to call repeatedly as the roster changes; targets accumulate
        (a peer that briefly drops off the roster during a reshuffle
        shouldn't lose its already-open pinhole). Punches immediately at
        anything newly added rather than waiting for the next keepalive
        tick - a peer that just joined the mesh is exactly the peer most
        likely to matter if a failover happens in the next few seconds,
        so its pinhole shouldn't have to wait behind everyone else's."""
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
                send_punch(self.sock, tuple(addr))
            if now >= next_stun_refresh:
                if stun_target is None:
                    for host, port in STUN_SERVERS:
                        try:
                            stun_target = (socket.gethostbyname(host), port)
                            break
                        except OSError:
                            continue
                if stun_target is not None:
                    send_punch(self.sock, stun_target)
                next_stun_refresh = now + KEEPALIVE_INTERVAL * 4

    def detach(self):
        """Hands the socket, still open, still bound, and still being
        kept alive by this object's own background loop, to something
        else that wants to *read* from it - namely ReliableUDP(sock=...),
        either right away (net.server.Server, when this peer starts a
        mesh) or later on promotion (FailoverController._promote_self).
        Idempotent: returns None on a second call, since the socket only
        has one legitimate new reader at a time. Does not stop the
        keepalive loop - call close() for that, once the session using
        this socket is actually done with it."""
        if self._handed_off:
            return None
        self._handed_off = True
        return self.sock

    def close(self):
        """Stops the background thread and, unless the socket was handed
        off via detach() (in which case its new owner is responsible for
        closing it - see Server.stop()/ReliableUDP.stop()), closes it.
        Safe to call more than once, and safe to call whether or not
        detach() was ever called."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if not self._handed_off:
            try:
                self.sock.close()
            except OSError:
                pass

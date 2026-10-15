"""Headless tests for the network layer (no Tk needed):
    python -m unittest discover -s tests -v
"""
import os
import json
import queue
import random
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ot  # noqa: E402
from net import server as server_mod  # noqa: E402
from net import transport as p  # noqa: E402
from net.client import Client, ConnectError  # noqa: E402
from net.server import Server  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class Model:
    """What an editor does with a Client: keeps one text per document,
    applies remote ops in order, and turns typing into local ops."""

    def __init__(self, name, port):
        self.texts, self.meta = {}, {}
        self.lock = threading.Lock()
        self.client = Client()
        self.client.on_full_sync = self._full_sync
        self.client.on_remote_op = self._remote_op
        self.client.connect("127.0.0.1", port, name)

    def _full_sync(self, doc, text, meta):
        with self.lock:
            self.texts[doc] = text
            self.meta[doc] = meta

    def _remote_op(self, doc, op, author, swap):
        with self.lock:
            self.texts[doc] = op.apply(self.texts[doc])

    def type(self, doc, pos, s):
        with self.lock:
            text = self.texts[doc]
            pos = min(pos, len(text))
            op = ot.Op()
            op.retain(pos)
            op.insert(s)
            op.retain(len(text) - pos)
            self.texts[doc] = op.apply(text)
            self.client.local_edit(op, doc)

    def stop(self):
        self.client.disconnect()


class NetTestCase(unittest.TestCase):
    def setUp(self):
        self.server = Server(0)
        self.server.start()
        self.port = self.server.transport.local_port
        self.models = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for m in self.models:
            m.stop()
        self.server.stop()

    def join(self, name):
        m = Model(name, self.port)
        self.models.append(m)
        return m


class MultiDocTests(NetTestCase):
    def test_shared_doc_reaches_everyone_with_its_name(self):
        a, b = self.join("a"), self.join("b")
        self.assertTrue(wait_for(lambda: 1 in a.texts and 1 in b.texts))
        a.client.open_doc("src/x.py", "print('x')\n", ref=7)
        self.assertTrue(wait_for(lambda: len(a.texts) == 2 and len(b.texts) == 2))
        doc = max(a.texts)
        self.assertGreater(doc, p.MAIN_DOC)
        for m in (a, b):
            self.assertEqual(m.texts[doc], "print('x')\n")
            self.assertEqual(m.meta[doc]["filename"], "src/x.py")
            self.assertTrue(m.meta[doc]["fresh"])
        # the sender can recognise its own request; the other side sees who shared it
        self.assertEqual(a.meta[doc]["ref"], 7)
        self.assertEqual(a.meta[doc]["by"], a.client.client_id)
        self.assertEqual(b.meta[doc]["by"], a.client.client_id)

    def test_late_joiner_receives_every_document(self):
        a = self.join("a")
        wait_for(lambda: 1 in a.texts)
        a.client.open_doc("one.py", "1", ref=1)
        a.client.open_doc("two.py", "22", ref=2)
        self.assertTrue(wait_for(lambda: len(a.texts) == 3))
        late = self.join("late")
        self.assertTrue(wait_for(lambda: len(late.texts) == 3))
        self.assertEqual(sorted(late.texts.values()), sorted(a.texts.values()))
        self.assertFalse(any(m["fresh"] for m in late.meta.values()))

    def test_concurrent_edits_in_several_documents_converge(self):
        a, b = self.join("a"), self.join("b")
        wait_for(lambda: 1 in a.texts and 1 in b.texts)
        a.client.open_doc("two.py", "", ref=1)
        self.assertTrue(wait_for(lambda: len(a.texts) == 2 and len(b.texts) == 2))
        docs = sorted(a.texts)

        def typist(model, seed):
            rng = random.Random(seed)
            for i in range(150):
                model.type(rng.choice(docs), rng.randint(0, 40), f"<{model.client.username}{i}>")
                time.sleep(rng.random() * 0.003)

        threads = [threading.Thread(target=typist, args=(m, i)) for i, m in enumerate((a, b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        def converged():
            return all(a.texts[d] == b.texts[d] == self.server.docs[d].doc for d in docs)
        self.assertTrue(wait_for(converged, 15), {d: (a.texts[d], b.texts[d], self.server.docs[d].doc) for d in docs})
        # every keystroke of both people made it into some document
        total = sum(len(self.server.docs[d].doc) for d in docs)
        self.assertEqual(total, sum(len(f"<{n}{i}>") for n in ("a", "b") for i in range(150)))

    def test_ops_are_numbered_per_document(self):
        a = self.join("a")
        wait_for(lambda: 1 in a.texts)
        a.client.open_doc("two.py", "", ref=1)
        wait_for(lambda: len(a.texts) == 2)
        d2 = max(a.texts)
        for n in range(1, 6):
            a.type(1, 0, "x")
            # one at a time: a client composes edits made while one is still
            # in flight, which would make the count below nondeterministic
            self.assertTrue(wait_for(lambda: self.server.docs[1].revision == n))
        a.type(d2, 0, "y")
        self.assertTrue(wait_for(lambda: self.server.docs[d2].revision == 1))
        self.assertEqual(self.server.docs[1].revision, 5)

    def test_document_limit_is_reported_to_the_sender(self):
        old = server_mod.MAX_DOCS
        server_mod.MAX_DOCS = 2
        self.addCleanup(setattr, server_mod, "MAX_DOCS", old)
        a = self.join("a")
        rejected = queue.Queue()
        a.client.on_doc_rejected = lambda ref, why: rejected.put((ref, why))
        wait_for(lambda: 1 in a.texts)
        a.client.open_doc("ok.py", "", ref=1)
        wait_for(lambda: len(a.texts) == 2)
        a.client.open_doc("too-many.py", "", ref=2)
        ref, why = rejected.get(timeout=5)
        self.assertEqual(ref, 2)
        self.assertIn("Too many", why)
        self.assertEqual(len(self.server.docs), 2)

    def test_a_bad_op_resyncs_only_that_document(self):
        a = self.join("a")
        wait_for(lambda: 1 in a.texts)
        a.client.open_doc("two.py", "keep", ref=1)
        wait_for(lambda: len(a.texts) == 2)
        d2 = max(a.texts)
        a.type(1, 0, "abc")
        wait_for(lambda: self.server.docs[1].doc == "abc")
        bad = ot.Op()
        bad.retain(999)                     # claims a document length that doesn't exist
        a.client.transport.send(a.client.server_addr, p.OP, {"doc": 1, "base_rev": 1, "ops": bad.to_json()})
        self.assertTrue(wait_for(lambda: self.server.docs[1].doc == "abc" and a.texts[1] == "abc"))
        self.assertEqual(a.texts[d2], "keep")
        a.type(1, 3, "d")                   # the connection is still healthy afterwards
        self.assertTrue(wait_for(lambda: self.server.docs[1].doc == "abcd"))

    def test_promoted_server_keeps_document_ids(self):
        s = Server(0, initial_docs={1: ("a", None), 4: ("d", "d.py")})
        try:
            self.assertEqual(sorted(s.docs), [1, 4])
            self.assertEqual(s._next_doc_id, 5)
            plain = Server(0)
            try:
                self.assertEqual(plain.docs[p.MAIN_DOC].doc, "")
            finally:
                plain.transport.stop()
        finally:
            s.transport.stop()


class VersionMismatchTests(unittest.TestCase):
    """A peer on another protocol version has to be told so, not left to
    wait for a connection timeout that looks like a network problem."""

    def test_host_answers_an_old_clients_hello_with_a_readable_refusal(self):
        server = Server(0)
        server.start()
        logs = []
        server.on_log = logs.append
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        self.addCleanup(sock.close)
        self.addCleanup(server.stop)
        # a version-1 HELLO, as the old client sends it (reliable, seq 1)
        sock.sendto(p.HEADER.pack(b"CE1", p.HELLO, 1, p.FLAG_RELIABLE) + json.dumps({"username": "old"}).encode(),
                    ("127.0.0.1", server.transport.local_port))
        got = {}
        while p.HELLO_REJECT not in got or p.ACK not in got:
            raw, _ = sock.recvfrom(65535)
            magic, kind, seq, flags = p.HEADER.unpack_from(raw, 0)
            self.assertEqual(magic, b"CE1")             # in the dialect the old client understands
            got[kind] = (seq, json.loads(raw[p.HEADER.size:] or b"{}"))
        self.assertEqual(got[p.ACK][0], 1)               # so it stops retransmitting its HELLO
        self.assertIn("newer version", got[p.HELLO_REJECT][1]["reason"])
        self.assertIn("update", got[p.HELLO_REJECT][1]["reason"].lower())
        self.assertEqual(len(server.clients), 0)
        self.assertTrue(any("protocol version 1" in m for m in logs))

    def test_new_client_tells_you_when_the_host_runs_the_old_protocol(self):
        """A stand-in for an old host: it ignores everything but a version-1
        PING, which it answers with a PONG, exactly like the old server."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.2)
        stop = threading.Event()

        def old_host():
            while not stop.is_set():
                try:
                    raw, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                magic, kind, seq, flags = p.HEADER.unpack_from(raw, 0)
                if magic == b"CE1" and kind == p.PING:
                    sock.sendto(p.HEADER.pack(b"CE1", p.PONG, 0, 0) + b"{}", addr)
        t = threading.Thread(target=old_host, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(2), sock.close()))
        started = time.monotonic()
        with self.assertRaises(ConnectError) as ctx:
            Client().connect("127.0.0.1", sock.getsockname()[1], "x", timeout=6.0)
        self.assertLess(time.monotonic() - started, 3)   # not the 6s timeout
        message = str(ctx.exception)
        self.assertIn("older version", message)
        self.assertIn("protocol 1", message)
        self.assertNotIn("timed out", message)

    def test_a_silent_host_is_still_reported_as_a_timeout(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        with self.assertRaises(ConnectError) as ctx:
            Client().connect("127.0.0.1", sock.getsockname()[1], "x", timeout=1.5)
        self.assertIn("timed out", str(ctx.exception))

    def test_junk_and_unrelated_packets_are_ignored(self):
        server = Server(0)
        server.start()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        self.addCleanup(sock.close)
        self.addCleanup(server.stop)
        for raw in (b"", b"xx", b"HTTP/1.1 200 OK\r\n\r\n",
                    p.HEADER.pack(b"CE9", p.OP, 1, 0) + b"{}",         # not a HELLO: nothing to refuse
                    p.HEADER.pack(b"CE1", p.HELLO, 1, 0) + b"not json"):
            sock.sendto(raw, ("127.0.0.1", server.transport.local_port))
        # the HELLO-shaped one is answered (it doesn't need valid JSON to be told why), the rest are not
        replies = []
        try:
            while True:
                replies.append(sock.recvfrom(65535)[0])
        except socket.timeout:
            pass
        kinds = [p.HEADER.unpack_from(r, 0)[1] for r in replies]
        self.assertEqual(kinds, [p.HELLO_REJECT])
        # and the server is still perfectly healthy
        m = Model("ok", server.transport.local_port)
        try:
            self.assertTrue(wait_for(lambda: 1 in m.texts))
        finally:
            m.stop()


class OrderingTests(unittest.TestCase):
    """Reliable UDP doesn't keep messages in order: an op can overtake the
    text it applies to."""

    def test_op_that_arrives_before_its_documents_text_is_applied_after_it(self):
        c = Client()
        c.client_id = 9
        events = []
        c.on_full_sync = lambda doc, text, meta: events.append(("text", doc, text))
        c.on_remote_op = lambda doc, op, author, swap: events.append(("op", doc, op.apply(ev_text(events, doc))))

        def ev_text(evts, doc):
            return next(t for kind, d, t in reversed(evts) if kind == "text" and d == doc)

        ins = ot.Op()
        ins.retain(2)
        ins.insert("!")
        c._on_message(p.OP, {"doc": 5, "base_rev": 3, "ops": ins.to_json(), "from": 1}, None)
        stale = ot.Op()
        stale.insert("zzz")
        c._on_message(p.OP, {"doc": 5, "base_rev": 2, "ops": stale.to_json(), "from": 1}, None)
        self.assertEqual(events, [])        # nothing is applied to a text we don't have yet
        c._on_message(p.DOC_CHUNK, {"doc": 5, "index": 0, "total": 1, "text": "hi", "revision": 3,
                                    "filename": "f.py"}, None)
        # the text first, then the buffered op on top of it; the op that was
        # already folded into the text (rev 2 < 3) is dropped, not replayed
        self.assertEqual(events, [("text", 5, "hi"), ("op", 5, "hi!")])
        c.transport.stop()

    def test_chunks_may_arrive_in_any_order(self):
        c = Client()
        got = []
        c.on_full_sync = lambda doc, text, meta: got.append((doc, text, meta["filename"]))
        for idx in (2, 0, 1):
            c._on_message(p.DOC_CHUNK, {"doc": 3, "index": idx, "total": 3, "text": "abc"[idx],
                                        "revision": 0, "filename": "n.py"}, None)
        self.assertEqual(got, [(3, "abc", "n.py")])
        c.transport.stop()


class FreezeRegressionTests(NetTestCase):
    def test_typing_while_remote_edits_arrive_neither_deadlocks_nor_drifts(self):
        """Tk makes a call from a non-main thread wait until the main thread
        services it. Model that: the client's dispatcher hands work to the
        'UI thread' (this test's thread) and blocks until it has run, while
        the UI thread keeps typing through Client.local_edit(). Holding the
        client lock across such a call hangs forever; applying a peer's op
        later than it was transformed (after more typing) makes the texts
        drift apart."""
        a, b = self.join("a"), self.join("b")
        wait_for(lambda: 1 in a.texts and 1 in b.texts)
        ui_calls = queue.Queue()

        def like_tk_after(fn):
            done = threading.Event()
            ui_calls.put((done, fn))
            done.wait()

        a.client.dispatcher = like_tk_after

        def service_ui():                       # the "mainloop"
            while True:
                try:
                    done, fn = ui_calls.get_nowait()
                except queue.Empty:
                    return
                fn()
                done.set()

        stop = threading.Event()

        def flood():
            while not stop.is_set():
                b.type(1, 0, "y")
                time.sleep(0.001)
        t = threading.Thread(target=flood, daemon=True)
        t.start()

        typed = 0
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            a.type(1, 0, "x")                   # -> Client.local_edit, which takes the client lock
            typed += 1
            service_ui()
            time.sleep(0.001)
        stop.set()
        t.join(2)
        self.assertGreater(typed, 100)

        def converged():
            service_ui()
            return a.texts[1] == b.texts[1] == self.server.docs[1].doc
        self.assertTrue(wait_for(converged, 10))
        self.assertEqual(self.server.docs[1].doc.count("x"), typed)


if __name__ == "__main__":
    unittest.main()

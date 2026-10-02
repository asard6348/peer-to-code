"""Terminal chat relay: real Server and Client objects over loopback UDP.

Run from the repo root:  python3 -m unittest tests.test_chat_server -v
"""
import os
import queue
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from net.client import Client
from net.server import Server, CHAT_MAX_LEN
from chat_util import parse_chat_command, sanitize_name, sanitize_text, unique_name


class Harness:
    def __init__(self):
        self.server = Server(0)
        self.server.start()
        self.port = self.server.transport.local_port
        self.clients = []

    def join(self, name):
        c = Client()
        c.chat_reorder_window = 0.05
        c.inbox = queue.Queue()
        c.on_chat = c.inbox.put
        c.connect("127.0.0.1", self.port, name)
        self.clients.append(c)
        return c

    def close(self):
        for c in self.clients:
            try:
                c.disconnect()
            except Exception:
                pass
        self.server.stop()


def get(c, timeout=2.0):
    return c.inbox.get(timeout=timeout)


def nothing(c, wait=0.4):
    try:
        got = c.inbox.get(timeout=wait)
    except queue.Empty:
        return True
    return False, got


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_broadcast_reaches_everyone_but_sender(self):
        a, b, c = (self.h.join(n) for n in ("alice", "bob", "carol"))
        a.send_chat("hello all")
        mb, mc = get(b), get(c)
        for m in (mb, mc):
            self.assertEqual((m["name"], m["text"], m["private"], m["from"]), ("alice", "hello all", False, a.client_id))
            self.assertIn("seq", m)
            self.assertGreater(m["ts"], 1e9)
        self.assertIs(nothing(a), True)

    def test_private_goes_only_to_target_case_insensitive(self):
        a, b, c = (self.h.join(n) for n in ("alice", "Bob", "carol"))
        a.send_chat("psst", to="bOb")
        m = get(b)
        self.assertTrue(m["private"])
        self.assertEqual(m["to"], ["Bob"])
        self.assertIs(nothing(c), True)

    def test_multiple_recipients_by_name_and_id(self):
        a, b, c, d = (self.h.join(n) for n in ("alice", "bob", "carol", "dave"))
        a.send_chat("team", to=["bob", f"#{c.client_id}"])
        self.assertEqual(get(b)["text"], "team")
        self.assertEqual(sorted(get(c)["to"]), ["bob", "carol"])
        self.assertIs(nothing(d), True)

    def test_unknown_user_errors_to_sender_only(self):
        a, b = self.h.join("alice"), self.h.join("bob")
        a.send_chat("hi", to="nobody")
        err = get(a)
        self.assertIn("No such user", err["error"])
        self.assertIs(nothing(b), True)

    def test_partial_delivery_reports_unknown(self):
        a, b = self.h.join("alice"), self.h.join("bob")
        a.send_chat("hi", to=["bob", "ghost"])
        self.assertEqual(get(b)["text"], "hi")
        self.assertIn("Not delivered to: 'ghost'", get(a)["error"])

    def test_alone_gets_error(self):
        a = self.h.join("alice")
        a.send_chat("anyone?")
        self.assertIn("Nobody else", get(a)["error"])

    def test_duplicate_names_made_unique_and_consistent(self):
        a, b, c = self.h.join("bob"), self.h.join("bob"), self.h.join("BOB")
        self.assertEqual((a.username, b.username, c.username), ("bob", "bob#2", "BOB#3"))
        self.assertEqual(b.requested_username, "bob")
        roster = {r["id"]: r["name"] for r in self.h.server._roster_payload()["peers"]}
        self.assertEqual(roster[b.client_id], "bob#2")
        # targeting bob reaches only the first bob; bob#2 only the second; #id works too
        c.send_chat("1", to="bob")
        self.assertEqual(get(a)["text"], "1")
        self.assertIs(nothing(b), True)
        c.send_chat("2", to="bob#2")
        self.assertEqual(get(b)["text"], "2")
        c.send_chat("3", to=f"#{a.client_id}")
        self.assertEqual(get(a)["text"], "3")

    def test_hostile_username_is_sanitized_at_join(self):
        a = self.h.join("\x1b[2J\r\nbob -> you [x]")
        self.assertNotIn("\x1b", a.username)
        self.assertNotIn("\n", a.username)
        self.assertNotIn("->", a.username)
        self.assertNotIn("[", a.username)

    def test_text_sanitized_and_capped_on_server(self):
        a, b = self.h.join("alice"), self.h.join("bob")
        a.send_chat("x")  # prime
        get(b)
        # Bypass Client.send_chat's own sanitizing: talk to the server raw.
        from net import transport as p
        a.transport.send(a.server_addr, p.CHAT, {"text": "hi\x1b[31m red\x07\r\nline2\u202e" + "z" * 3000}, reliable=True)
        m = get(b)
        self.assertNotIn("\x1b", m["text"])
        self.assertNotIn("\x07", m["text"])
        self.assertNotIn("\n", m["text"])
        self.assertNotIn("\u202e", m["text"])
        self.assertIn("line2", m["text"])
        self.assertLessEqual(len(m["text"]), CHAT_MAX_LEN)

    def test_client_resanitizes_what_relay_sends(self):
        a = self.h.join("alice")
        a._handle_chat({"seq": 1, "ts": 1.0, "from": 9, "name": "bob -> you\x1b[1m", "text": "\x1b]0;pwn\x07hi", "private": False})
        m = get(a)
        self.assertNotIn("\x1b", m["name"] + m["text"])
        self.assertNotIn("->", m["name"])

    def test_rate_limit(self):
        a, b = self.h.join("alice"), self.h.join("bob")
        for i in range(5):
            a.send_chat(f"m{i}")
        got = [get(b)["text"] for _ in range(5)]
        self.assertEqual(sorted(got), [f"m{i}" for i in range(5)])
        a.send_chat("m5")
        self.assertIn("Slow down", get(a)["error"])
        self.assertIs(nothing(b), True)

    def test_rate_limit_refills(self):
        now = [100.0]
        self.h.server._clock = lambda: now[0]
        a, b = self.h.join("alice"), self.h.join("bob")
        rec = self.h.server.clients[a.client_id]
        self.assertTrue(all(self.h.server._chat_allow(rec) for _ in range(5)))
        self.assertFalse(self.h.server._chat_allow(rec))
        now[0] += 1.05          # 5 per 5 s -> one token per second
        self.assertTrue(self.h.server._chat_allow(rec))
        self.assertFalse(self.h.server._chat_allow(rec))
        now[0] += 60            # never banks more than the burst
        self.assertEqual(sum(self.h.server._chat_allow(rec) for _ in range(9)), 5)

    def test_history_on_join_public_only(self):
        a, b = self.h.join("alice"), self.h.join("bob")
        a.send_chat("public one")
        a.send_chat("secret", to="bob")
        a.send_chat("public two")
        for _ in range(3):
            get(b)
        late = self.h.join("carol")
        hist = get(late)
        self.assertEqual([m["text"] for m in hist["history"]], ["public one", "public two"])
        self.assertTrue(all(not m["private"] for m in hist["history"]))
        self.assertEqual([m["seq"] for m in hist["history"]], sorted(m["seq"] for m in hist["history"]))
        self.assertIs(nothing(late), True)   # and no duplicate live delivery

    def test_history_is_bounded_to_50(self):
        self.h.server.chat_rate_burst = 1000
        a, b = self.h.join("alice"), self.h.join("bob")
        for i in range(60):
            a.send_chat(f"n{i}")
        for _ in range(60):
            get(b)
        late = self.h.join("carol")
        texts = [m["text"] for m in get(late)["history"]]
        self.assertEqual(len(texts), 50)
        self.assertEqual(texts[0], "n10")
        self.assertEqual(texts[-1], "n59")

    def test_sequence_monotonic_and_client_dedupes_and_sorts(self):
        a, b = self.h.join("alice"), self.h.join("bob")
        a.send_chat("1"); a.send_chat("2"); a.send_chat("3")
        seqs = [get(b)["seq"] for _ in range(3)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), 3)
        base = seqs[-1]
        # Out-of-order + duplicate arrivals straight into the client handler.
        def mk(n):
            return {"seq": n, "ts": 1.0, "from": 1, "name": "x", "text": f"t{n}", "private": False}
        for n in (base + 3, base + 1, base + 2, base + 1, base + 3):
            b._handle_chat(mk(n))
        got = [get(b)["seq"] for _ in range(3)]
        self.assertEqual(got, [base + 1, base + 2, base + 3])
        self.assertIs(nothing(b), True)

    def test_empty_and_malformed_payloads_ignored(self):
        from net import transport as p
        a, b = self.h.join("alice"), self.h.join("bob")
        for bad in ({"text": "   \x1b  "}, {"text": ""}, {}, "str", [1], {"text": "ok", "to": 5}):
            a.transport.send(a.server_addr, p.CHAT, bad, reliable=True)
        get(b)  # {"text": "ok", "to": 5} is treated as public
        self.assertIs(nothing(b), True)


class UtilTests(unittest.TestCase):
    def test_parse(self):
        P = parse_chat_command
        self.assertIsNone(P("ls"))
        self.assertIsNone(P("mm hi"))
        with self.assertRaises(ValueError):
            P("m")
        with self.assertRaises(ValueError):
            P("m -p bob")
        with self.assertRaises(ValueError):
            P("m -p")
        self.assertEqual(P("m -- -p").text, "-p")
        self.assertEqual(P("m -- --").text, "--")
        self.assertEqual(P('m -p "Jo Smith" hi').to, ("Jo Smith",))
        self.assertEqual(P("m -p a,#3 hi").to, ("a", "#3"))
        self.assertEqual(P("\\m foo").kind, "shell")
        self.assertEqual(P("\\m foo").text, "m foo")
        self.assertEqual(P("x hi", trigger="x").text, "hi")
        self.assertIsNone(P("m hi", trigger="x"))
        self.assertIsNone(P("m hi", running=True))
        self.assertEqual(P("/m hi", running=True).text, "hi")
        self.assertIsNone(P("/m", running=True))

    def test_sanitize(self):
        self.assertEqual(sanitize_text("a\x1b[0mb\r\nc"), "a[0mb \u23ce c")
        self.assertEqual(unique_name("bob", ["bob", "BOB#2"]), "bob#3")
        self.assertEqual(len(unique_name("x" * 32, ["x" * 32])), 32)
        self.assertEqual(sanitize_name("\x00\x1b"), "anon")
        self.assertFalse(sanitize_name("#7").startswith("#"))


if __name__ == "__main__":
    unittest.main()

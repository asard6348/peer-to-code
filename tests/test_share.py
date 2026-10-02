"""Shared run output (phase 2): helpers, server relay over loopback, and the
real EditorApp as runner and as viewer, headless.

    xvfb-run -a python3 -m unittest tests.test_share -v
"""
import os
import queue
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import share_util as su
from net.client import Client
from net.server import Server
from tests.test_chat_gui import Env, GuiBase


class Unit(unittest.TestCase):
    def test_sanitize_keeps_only_sgr(self):
        raw = "a\x1b[31mb\x1b[0m\x1b[2J\x1b[H\x1b]0;evil\x07\x1b]8;;http://x\x1b\\c\x1bP1$r\x1b\\d\x07\x08\r\ne\x9b2Jf\u202eg"
        out = su.sanitize_output(raw)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x08", out)
        self.assertNotIn("\x9b", out)
        self.assertNotIn("\u202e", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("evil", out)
        self.assertNotIn("2J\x1b", out)
        self.assertEqual(out.count("\x1b"), 2)       # only the two SGR sequences survive
        self.assertIn("\x1b[31m", out)

    def test_render_sgr_whitelist(self):
        runs, st = su.render_sgr("\x1b[38;5;196mA\x1b[1;32mB\x1b[48;2;1;2;3mC\x1b[0mD")
        self.assertEqual([(t, s) for t, s in runs],
                         [("A", (None, False)), ("B", (2, True)), ("C", (2, True)), ("D", (None, False))])

    def test_streamer_batches_orders_and_caps(self):
        st = su.ShareStreamer(1, "x", max_bytes=100)
        out = st.add([(False, "a"), (False, "b"), (True, "E"), (False, "c")])
        self.assertEqual(out, [(1, [["o", "ab"], ["e", "E"], ["o", "c"]], False)])   # one chunk, order kept
        self.assertEqual(st.add([(False, "\x1b"), (False, "[3")]), [])                 # split escape is held back
        out = st.add([(False, "1mX")])
        self.assertEqual(out[0][1], [["o", "\x1b[31mX"]])
        big = st.add([(False, "z" * 500)])
        self.assertEqual(big, [(3, None, True)])                                       # cap -> one truncation marker
        self.assertEqual(st.add([(False, "more")]), [])
        self.assertEqual(st.end_seq(), 4)

    def test_assembler_orders_dedupes_and_handles_truncation(self):
        a = su.RunAssembler()
        got = []
        for seq in (3, 1, 1, 2, 2, 3):
            got += [i["n"] for i in a.feed(seq, {"n": seq})]
        self.assertEqual(got, [1, 2, 3])
        a = su.RunAssembler()
        self.assertEqual(a.feed(2, {"truncated": True}), [])
        self.assertEqual(a.feed(3, {"n": "late data"}), [])        # still waiting for 1
        a.feed(1, {"n": 1})
        self.assertTrue(a.truncated)
        self.assertEqual(a.feed(4, {"n": "after cap"}), [])
        end = a.feed(9, {"end": True})
        self.assertEqual(end, [{"end": True}])
        a = su.RunAssembler()
        a.feed(2, {"n": 2})
        self.assertEqual(a.release_stalled(), [])
        a.first_pending_at -= 10
        self.assertEqual([i.get("gap") or i["n"] for i in a.release_stalled()], [True, 2])

    def test_commands(self):
        P = su.parse_share_command
        self.assertEqual(P("share on"), "on")
        self.assertEqual(P("share"), "status")
        self.assertEqual(P("/share off", running=True), "off")
        self.assertIsNone(P("share on", running=True))
        self.assertIsNone(P("\\share on"))
        self.assertIsNone(P("sharing"))
        with self.assertRaises(ValueError):
            P("share maybe")

    def test_title_never_contains_arguments_or_secrets(self):
        self.assertEqual(su.share_title(["/usr/bin/python3", "/p/app.py", "--token", "s3cret"], False), "python3 app.py")
        self.assertNotIn("abc", su.share_title("API_KEY=abc curl -H secret", True))
        self.assertNotIn("secret", su.share_title("curl -H secret", True))


class Relay(unittest.TestCase):
    def setUp(self):
        self.server = Server(0)
        self.server.start()
        self.port = self.server.transport.local_port
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.disconnect()
        self.server.stop()

    def join(self, name):
        c = Client()
        c.inbox = queue.Queue()
        c.on_share = lambda kind, msg: c.inbox.put((kind, msg))
        c.connect("127.0.0.1", self.port, name)
        self.clients.append(c)
        return c

    def collect(self, c, n, timeout=3):
        out = []
        end = time.time() + timeout
        while len(out) < n and time.time() < end:
            try:
                out.append(c.inbox.get(timeout=0.2))
            except queue.Empty:
                pass
        return out

    def test_relay_only_to_others_never_back_and_output_only(self):
        a, b = self.join("alice"), self.join("bob")
        a.send_share_start(1, "app.py")
        a.send_share_output(1, 1, [["o", "hi\x1b[2J there"], ["e", "oops"]])
        a.send_share_end(1, 2, code=3)
        got = self.collect(b, 3)
        kinds = [k for k, _ in got]
        self.assertEqual(sorted(kinds), ["end", "output", "start"])
        by = dict(got)
        self.assertEqual(by["start"]["name"], "alice")
        self.assertEqual(by["start"]["title"], "app.py")
        segs = by["output"]["segs"]
        self.assertEqual(segs, [["o", "hi there"], ["e", "oops"]])
        self.assertEqual(by["end"]["code"], 3)
        self.assertEqual(self.collect(a, 1, timeout=0.5), [])       # sender gets nothing back
        # A viewer cannot inject output into someone else's run: a RUN_OUTPUT from
        # bob only ever creates/extends bob's own run.
        b.send_share_output(1, 1, [["o", "forged"]])               # bob has no run -> ignored
        self.assertEqual(self.collect(a, 1, timeout=0.5), [])

    def test_unknown_or_ended_run_ignored_and_dedupe(self):
        a, b = self.join("alice"), self.join("bob")
        a.send_share_output(7, 1, [["o", "no start"]])
        a.send_share_start(1, "x")
        a.send_share_output(1, 1, [["o", "one"]])
        a.send_share_output(1, 1, [["o", "dup"]])
        a.send_share_end(1, 2)
        a.send_share_output(1, 3, [["o", "after end"]])
        got = self.collect(b, 5, timeout=1.5)
        texts = [m["segs"][0][1] for k, m in got if k == "output"]
        self.assertEqual(texts, ["one"])

    def test_cap_per_run(self):
        ticks = [0]

        def clock():                      # every call is "a second later": no rate limiting here
            ticks[0] += 1
            return float(ticks[0])
        self.server._clock = clock
        a, b = self.join("alice"), self.join("bob")
        a.send_share_start(1, "x")
        for i in range(1, 41):            # 40 x 8 KiB > the 256 KiB per-run cap
            a.send_share_output(1, i, [["o", "z" * su.SHARE_CHUNK_MAX]])
        got = self.collect(b, 42, timeout=6)
        outs = {m["seq"]: m for k, m in got if k == "output"}
        total = sum(len(t) for m in outs.values() for _s, t in m.get("segs", []))
        self.assertLessEqual(total, su.SHARE_RUN_MAX_BYTES)
        self.assertTrue(any(m.get("truncated") for m in outs.values()))
        self.assertTrue(all(not m.get('segs') for s, m in outs.items() if s > max(k for k, m in outs.items() if m.get('segs'))))

    def test_rate_limit_marks_skipped_but_keeps_numbering(self):
        now = [10.0]
        self.server._clock = lambda: now[0]
        a, b = self.join("alice"), self.join("bob")
        a.send_share_start(1, "x")
        n = su.SHARE_RATE_BURST + 10
        for i in range(1, n + 1):
            a.send_share_output(1, i, [["o", "x"]])
        got = self.collect(b, n + 1, timeout=5)
        outs = [m for k, m in got if k == "output"]
        self.assertEqual(sorted(m["seq"] for m in outs), list(range(1, n + 1)))   # gapless numbering
        self.assertTrue(any(m.get("skipped") for m in outs))
        self.assertEqual(sum(1 for m in outs if m.get("segs")), su.SHARE_RATE_BURST)

    def test_late_joiner_catches_up_with_replay_flag(self):
        a = self.join("alice")
        a.send_share_start(1, "slow.py")
        a.send_share_output(1, 1, [["o", "line1\n"]])
        a.send_share_output(1, 2, [["e", "line2\n"]])
        time.sleep(0.6)
        late = self.join("carol")
        got = self.collect(late, 3)
        self.assertEqual(sorted(k for k, _ in got), ["output", "output", "start"])
        self.assertTrue(all(m.get("replay") for _k, m in got))
        self.assertEqual(sorted(m["seq"] for k, m in got if k == "output"), [1, 2])

    def test_runner_leaving_ends_the_run_for_viewers(self):
        a, b = self.join("alice"), self.join("bob")
        a.send_share_start(1, "x")
        self.collect(b, 1)
        a.disconnect()
        self.clients.remove(a)
        got = self.collect(b, 1, timeout=3)
        self.assertEqual(got[0][0], "end")
        self.assertEqual(got[0][1]["reason"], "left the session")

    def test_client_resanitizes_and_validates(self):
        b = self.join("bob")
        b._handle_share(16, {"run": 1, "from": 5, "seq": 1, "name": "x -> you\x1b[1m", "segs": [["o", "\x1b]0;t\x07ok"], ["z", "bad"], "junk"]})
        kind, msg = b.inbox.get(timeout=1)
        self.assertEqual(msg["segs"], [["o", "ok"]])
        self.assertNotIn("->", msg["name"])
        b._handle_share(16, {"run": "x"})            # malformed: dropped, no exception
        self.assertTrue(b.inbox.empty())


class GuiShare(GuiBase):
    def setUp(self):
        super().setUp()
        e = self.env
        self.viewer_events = queue.Queue()
        self.bob.on_share = lambda kind, msg: self.viewer_events.put((kind, msg))

    def run_script(self, code):
        self.env.app._launch_process([sys.executable, "-u", "-c", code])

    def test_off_by_default_and_nothing_shared(self):
        e = self.env
        self.assertFalse(e.app._share_on)
        self.assertEqual(e.app.share_label.winfo_manager(), "")
        self.run_script("print('private')")
        self.assertTrue(e.pump(5, lambda: "[finished]" in e.text()))
        self.assertTrue(self.viewer_events.empty())
        self.assertNotIn("sharing this run", e.text())

    def test_share_on_command_warns_indicator_and_streams_output_only(self):
        e = self.env
        e.submit("share on")
        self.assertIn("secrets", e.text())
        self.assertEqual(e.app.share_label.winfo_manager(), "pack")
        self.assertTrue(e.app._share_on)
        self.run_script("import sys,time\n"
                        "print('out1', flush=True)\n"
                        "print('err1', file=sys.stderr, flush=True)\n"
                        "name = input('who? ')\n"
                        "print('hello', name, flush=True)\n")
        self.assertTrue(e.pump(5, lambda: "who? " in e.text()))
        e.submit("my-secret-answer")
        self.assertTrue(e.pump(5, lambda: "[finished]" in e.text()))
        e.pump(0.8)
        events = []
        while not self.viewer_events.empty():
            events.append(self.viewer_events.get())
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "end")
        shared_text = "".join(t for k, m in events if k == "output" for _s, t in m["segs"])
        self.assertIn("out1", shared_text)
        self.assertIn("err1", shared_text)
        self.assertIn("hello my-secret-answer", shared_text)   # the program's own output
        self.assertNotIn("who? my-secret", shared_text)        # the typed input echo is not shared
        self.assertNotIn("share on", shared_text)
        seqs = sorted(m["seq"] for k, m in events if k == "output")
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertLess(len(seqs), 12)                          # batched per poll tick, not per character
        title = events[0][1]["title"]
        self.assertEqual(title, os.path.basename(sys.executable))
        e.submit("share off")
        self.assertEqual(e.app.share_label.winfo_manager(), "")

    def test_share_off_mid_run_stops_and_ends_for_viewers(self):
        e = self.env
        e.submit("share on")
        self.run_script("import time\nprint('AAA', flush=True)\ntime.sleep(3)\nprint('BBB', flush=True)")
        self.assertTrue(e.pump(5, lambda: "AAA\n" in e.text()))
        e.submit("/share off")
        e.pump(0.6)
        events = []
        while not self.viewer_events.empty():
            events.append(self.viewer_events.get())
        self.assertEqual(events[-1][0], "end")
        self.assertEqual(events[-1][1]["reason"], "sharing turned off")
        e.pump(4, lambda: "[finished]" in e.text())
        self.assertTrue(self.viewer_events.empty())

    def test_toggle_on_mid_run_does_not_share_current_run(self):
        e = self.env
        self.run_script("import time\nprint('XXX', flush=True)\ntime.sleep(1.5)\nprint('YYY', flush=True)")
        self.assertTrue(e.pump(5, lambda: "XXX\n" in e.text()))
        e.submit("/share on")
        e.pump(3, lambda: "[finished]" in e.text())
        self.assertTrue(self.viewer_events.empty())

    def test_settings_checkbox_confirms_and_declines(self):
        import editor as editor_mod
        from settings_window import SettingsWindow
        e = self.env
        win = SettingsWindow(e.app, e.app.cfg, on_apply=e.app._on_settings_applied)
        e.root.update()
        orig = editor_mod.messagebox.askyesno
        try:
            editor_mod.messagebox.askyesno = lambda *a, **k: False
            win.share_output_var.set(True)
            win._on_share_output_toggled()
            self.assertFalse(e.app._share_on)
            self.assertFalse(win.share_output_var.get())
            editor_mod.messagebox.askyesno = lambda *a, **k: True
            win.share_output_var.set(True)
            win._on_share_output_toggled()
            self.assertTrue(e.app._share_on)
            win._save()
            import json
            with open(e.cfg_path) as f:
                self.assertNotIn("share_output", json.load(f).get("chat", {}))   # never persisted
        finally:
            editor_mod.messagebox.askyesno = orig

    def test_not_connected_cannot_share(self):
        e = self.env
        e.app.client.server_addr = None
        e.submit("share on")
        self.assertFalse(e.app._share_on)
        self.assertIn("not connected", e.text())


class GuiViewer(GuiBase):
    def feed(self, name, **msg):
        base = {"from": self.bob.client_id, "name": "bob", "run": 1, "replay": False}
        base.update(msg)
        self.env.app._show_share(name, base)

    def test_viewer_shows_in_separate_readonly_window_labelled_with_runner(self):
        e = self.env
        a = e.app
        self.feed("start", title="demo.py", from_seq=1)
        self.assertIn("bob is sharing the output of a run (demo.py)", e.text())
        self.feed("output", seq=2, segs=[["e", "\x1b[31mred\x1b[0m error\n"]], skipped=0, truncated=False)
        self.feed("output", seq=1, segs=[["o", "first \x1b[2Jline\x1b]0;x\x07\n"]], skipped=0, truncated=False)
        self.feed("end", seq=3, code=2, reason="")
        win = a._share_win
        self.assertIsNotNone(win)
        text = win.text_of(self.bob.client_id)
        self.assertEqual(text.split("\n")[:2], ["first line", "red error"])          # reordered by seq
        self.assertIn("[finished: exit code 2]", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("first line", e.text())
        tab = win._tabs[self.bob.client_id]
        self.assertEqual(str(tab["text"].cget("state")), "disabled")                  # read-only
        self.assertIn("bob", tab["header"].cget("text"))
        self.assertEqual(win.nb.tab(tab["frame"], "text"), "bob")
        tags = tab["text"].tag_names(tab["text"].search("red", "1.0"))
        self.assertTrue(any(t.startswith("sgr_1_") for t in tags))
        # typing / inserting into the viewer is impossible
        tab["text"].insert("end", "hack")
        self.assertNotIn("hack", win.text_of(self.bob.client_id))

    def test_output_before_start_is_kept_and_replay_is_quiet(self):
        e = self.env
        self.feed("output", seq=1, segs=[["o", "early\n"]], skipped=0, truncated=False)
        self.feed("start", title="t", from_seq=1, replay=True)
        self.assertNotIn("is sharing", e.text())
        self.assertIn("early", e.app._share_win.text_of(self.bob.client_id))

    def test_truncation_gap_and_own_and_muted_and_disabled(self):
        e = self.env
        a = e.app
        self.feed("start", title="t", from_seq=1)
        self.feed("output", seq=1, segs=[["o", "x\n"]], skipped=0, truncated=False)
        self.feed("output", seq=2, segs=[], skipped=0, truncated=True)
        self.feed("output", seq=3, segs=[["o", "never shown\n"]], skipped=0, truncated=False)
        self.feed("end", seq=4, code=0, reason="")
        t = a._share_win.text_of(self.bob.client_id)
        self.assertIn("output limit reached", t)
        self.assertNotIn("never shown", t)
        # own messages are ignored
        a._show_share("start", {"from": e.host.client_id, "name": "me", "run": 1, "title": "mine", "from_seq": 1, "replay": False})
        self.assertNotIn(e.host.client_id, a._share_runs)
        # view_shared off -> ignored; muted sender -> ignored
        a.cfg["chat"]["view_shared"] = False
        self.feed("start", run=2, title="hidden", from_seq=1)
        self.assertEqual(a._share_runs[self.bob.client_id]["run"], 1)
        a.cfg["chat"]["view_shared"] = True
        a.cfg["chat"]["muted"] = ["bob"]
        self.feed("start", run=3, title="muted", from_seq=1)
        self.assertEqual(a._share_runs[self.bob.client_id]["run"], 1)

    def test_end_to_end_runner_and_viewer_through_server(self):
        e = self.env
        # bob is the runner (plain Client); the EditorApp is the viewer, fed by the real server.
        self.bob.send_share_start(1, "bobscript.py")
        self.bob.send_share_output(1, 2, [["e", "second\n"]])
        self.bob.send_share_output(1, 1, [["o", "first\x1b[2J\n"]])
        self.bob.send_share_end(1, 3, code=0)
        self.assertTrue(e.pump(4, lambda: "[finished]" in (e.app._share_win.text_of(self.bob.client_id) if e.app._share_win else "")))
        text = e.app._share_win.text_of(self.bob.client_id)
        self.assertEqual(text.split("\n")[:2], ["first", "second"])
        self.assertNotIn("\x1b", text)
        self.assertNotIn("first", e.text())            # nothing leaks into the viewer's own terminal


if __name__ == "__main__":
    unittest.main()

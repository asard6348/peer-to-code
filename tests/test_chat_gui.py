"""Terminal chat in the real EditorApp, headless.

    xvfb-run -a python3 -m unittest tests.test_chat_gui -v

Client callbacks arrive on a network thread and EditorApp marshals them with
self.after(), which needs a running Tk main loop. These tests only pump
root.update(), so the callbacks are re-pointed at a queue that the pump loop
drains on the main thread, calling the same handlers EditorApp's own
wrappers would have scheduled.
"""
import json
import os
import queue
import stat
import sys
import tempfile
import time
import tkinter as tk
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from editor import EditorApp
from net.client import Client
from net.server import Server
from settings_window import SettingsWindow


class Env:
    def __init__(self, cfg_data=None, host_name="me", preexisting=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cfg_path = os.path.join(self.dir, "config.json")
        if cfg_data is not None:
            with open(self.cfg_path, "w") as f:
                json.dump(cfg_data, f)
        config.set_config_path(self.cfg_path)
        self.work = os.path.join(self.dir, "work")
        os.mkdir(self.work)
        self.server = Server(0)
        self.server.start()
        self.port = self.server.transport.local_port
        self.clients = []
        for name in preexisting:
            self.join(name)
        self.host = self._client(host_name)
        self.root = tk.Tk()
        self.root.geometry("900x600")
        self.app = EditorApp(self.root, self.host, self.server, self.work, host_name, "host",
                             cfg=config.load_config())
        self.q = queue.Queue()
        self.bells = 0
        self.app.bell = lambda *a, **k: setattr(self, "bells", self.bells + 1)
        c = self.host
        c.chat_reorder_window = 0.05
        c.on_chat = lambda p: self.q.put(("chat", p))
        c.on_peers = lambda peers: self.q.put(("peers", peers))
        c.on_share = lambda name, msg: self.q.put(("share", (name, msg)))
        for name in ("on_remote_op", "on_full_sync", "on_cursor", "on_disconnected", "on_buffer_context"):
            setattr(c, name, lambda *a: None)
        self.pump(0.5)

    def _client(self, name):
        c = Client()
        c.chat_reorder_window = 0.05
        c.inbox = queue.Queue()
        c.on_chat = c.inbox.put
        c.connect("127.0.0.1", self.port, name)
        self.clients.append(c)
        return c

    def join(self, name):
        return self._client(name)

    def drain(self):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "chat":
                self.app._show_chat(payload)
            elif kind == "share":
                self.app._show_share(*payload)
            else:
                self.app._render_peers(payload)

    def pump(self, seconds=0.3, until=None):
        end = time.time() + seconds
        while time.time() < end:
            self.drain()
            self.root.update()
            if until and until():
                return True
            time.sleep(0.01)
        return until() if until else None

    # console helpers
    def text(self):
        return self.app.console.get("1.0", "end-1c")

    def tail(self):
        return self.app.console.get("input_start", "end-1c")

    def type(self, s):
        self.app.console.insert("end-1c", s)

    def submit(self, line=None):
        if line is not None:
            self.type(line)
        self.app._submit_console_input()

    def close(self):
        try:
            self.app._cancel_pending_jobs()
        except Exception:
            pass
        for c in self.clients + [self.host]:
            try:
                c.disconnect()
            except Exception:
                pass
        try:
            self.server.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        self.tmp.cleanup()


class GuiBase(unittest.TestCase):
    cfg_data = None
    preexisting = ()

    def setUp(self):
        self.env = Env(self.cfg_data, preexisting=self.preexisting)
        self.bob = self.env.join("bob")
        self.env.pump(0.5)
        self.app = self.env.app
        self.con = self.app.console

    def tearDown(self):
        self.env.close()


class IdleAndTyping(GuiBase):
    def test_idle_prompt_message_lands_above_prompt(self):
        e = self.env
        e.pump(0.1)
        self.assertTrue(self.app._known_peer_names)
        self.bob.send_chat("hello there")
        self.assertTrue(e.pump(2, lambda: "hello there" in e.text()))
        lines = e.text().split("\n")
        self.assertEqual(lines[-1], "$ ")
        self.assertIn("[bob] hello there", lines[-2])
        self.assertEqual(e.tail(), "")

    def test_mid_typing_arrival_keeps_typed_text(self):
        e = self.env
        e.type("git sta")
        self.bob.send_chat("ping")
        self.assertTrue(e.pump(2, lambda: "[bob] ping" in e.text()))
        self.assertEqual(e.tail(), "git sta")
        self.assertEqual(e.text().split("\n")[-1], "$ git sta")
        e.type("tus")
        self.assertEqual(e.tail(), "git status")

    def test_own_message_echo_and_delivery(self):
        e = self.env
        e.submit("m hello bob")
        self.assertEqual(self.bob.inbox.get(timeout=2)["text"], "hello bob")
        self.assertIn("[you] hello bob", e.text())
        self.assertEqual(e.text().split("\n")[-1], "$ ")

    def test_private_echo_and_reply(self):
        e = self.env
        self.bob.send_chat("secret", to="me")
        self.assertTrue(e.pump(2, lambda: "[bob -> you] secret" in e.text()))
        e.submit("m -r thanks")
        self.assertEqual(self.bob.inbox.get(timeout=2)["text"], "thanks")
        self.assertIn("[you -> bob] thanks", e.text())

    def test_reply_without_private_and_usage_lines(self):
        e = self.env
        e.submit("m -r nope")
        self.assertIn("No private message to reply to", e.text())
        e.submit("m")
        self.assertIn("usage: m", e.text())
        e.submit("m -p")
        self.assertEqual(e.text().count("usage: m"), 2)
        self.assertTrue(self.bob.inbox.empty())

    def test_list_users(self):
        e = self.env
        e.submit("m -l")
        t = e.text()
        self.assertIn(f"#{self.bob.client_id} bob", t)
        self.assertIn(f"#{self.env.host.client_id} me (you)", t)

    def test_dash_dash_literal_and_multi_recipient(self):
        e = self.env
        carol = e.join("carol")
        e.pump(0.5)
        e.submit("m -- -p literal")
        self.assertEqual(self.bob.inbox.get(timeout=2)["text"], "-p literal")
        self.assertEqual(carol.inbox.get(timeout=2)["text"], "-p literal")
        e.submit(f"m -p bob,#{carol.client_id} both")
        self.assertEqual(self.bob.inbox.get(timeout=2)["text"], "both")
        self.assertEqual(carol.inbox.get(timeout=2)["text"], "both")
        self.assertIn("[you -> bob, carol] both", e.text())


class Sanitization(GuiBase):
    def test_escape_sequences_and_spoofed_names_cannot_reach_console(self):
        e = self.env
        e._show = e.app._show_chat
        # Straight into the display handler, as if a modified relay sent it.
        e.app._show_chat({"seq": 1, "ts": time.time(), "from": 99, "name": "bob -> you\x1b[2J",
                          "text": "\x1b[31mred\x1b]0;title\x07\rovertype\u202e", "private": False})
        t = e.text()
        self.assertNotIn("\x1b", t)
        self.assertNotIn("\r", t)
        self.assertNotIn("\x07", t)
        self.assertNotIn("\u202e", t)
        self.assertNotIn("[bob -> you", t)
        # and the ansi renderer's state wasn't touched
        self.assertEqual(e.app.console.tag_names("1.0") and 0, 0)

    def test_no_ansi_tag_applied_to_chat_text(self):
        e = self.env
        self.bob.send_chat("plain \x1b[31mred")
        self.assertTrue(e.pump(2, lambda: "plain" in e.text()))
        idx = e.con_index("plain") if hasattr(e, "con_index") else self.con.search("plain", "1.0")
        self.assertFalse([t for t in self.con.tag_names(idx) if t.startswith("ansi")])


class RunningProcess(GuiBase):
    CODE = ("import sys,time\n"
            "for i in range(3):\n"
            "    print('tick', i, flush=True); time.sleep(0.25)\n"
            "line = input('name> ')\n"
            "print('got:', line, flush=True)\n")

    def test_chat_during_run_does_not_corrupt_output_or_stdin(self):
        e = self.env
        e.app._launch_process([sys.executable, "-u", "-c", self.CODE])
        self.assertTrue(e.pump(5, lambda: "name> " in e.text()))
        e.type("abc")                       # half-typed reply to input()
        self.bob.send_chat("mid-run hello")
        self.assertTrue(e.pump(2, lambda: "[bob] mid-run hello" in e.text()))
        self.assertEqual(e.tail(), "abc")
        self.assertEqual(e.text().split("\n")[-1], "name> abc")
        # Plain "m ..." belongs to the process; only "/m ..." is chat.
        for _ in range(3):
            e.app.console.delete("input_start", "end-1c")
            break
        e.submit("/m from the run")
        self.assertEqual(self.bob.inbox.get(timeout=2)["text"], "from the run")
        self.assertIn("[you] from the run", e.text())
        self.assertNotIn("got:", e.text())
        e.submit("real input")
        self.assertTrue(e.pump(5, lambda: "got: real input" in e.text()))
        self.assertNotIn("got: /m", e.text())
        self.assertEqual(e.text().count("tick"), 3)
        e.pump(0.5)

    def test_bare_m_goes_to_stdin_while_running(self):
        e = self.env
        e.app._launch_process([sys.executable, "-u", "-c", "print(input('? '))"])
        self.assertTrue(e.pump(5, lambda: "? " in e.text()))
        e.submit("m hi")
        self.assertTrue(e.pump(5, lambda: "m hi\n" in e.text().split("? ", 1)[1].split("\n", 1)[1] if "? " in e.text() else False))
        self.assertTrue(self.bob.inbox.empty())

    def test_usage_while_running_mentions_slash(self):
        e = self.env
        e.app._launch_process([sys.executable, "-u", "-c", "input('? ')"])
        self.assertTrue(e.pump(5, lambda: "? " in e.text()))
        e.submit("/m -p")
        self.assertIn("usage: /m", e.text())
        e.submit("done")
        e.pump(1)


class HiddenTerminal(GuiBase):
    def test_unread_counter_and_clear(self):
        e = self.env
        if e.app._console_visible:
            e.app.toggle_console()
        self.assertFalse(e.app._console_visible)
        self.assertEqual(e.app.unread_label.winfo_manager(), "")
        self.bob.send_chat("one")
        self.bob.send_chat("two")
        self.assertTrue(e.pump(2, lambda: e.app._chat_unread == 2))
        self.assertEqual(e.app.unread_label.cget("text"), "Chat: 2 unread")
        self.assertEqual(e.app.unread_label.winfo_manager(), "pack")
        self.assertEqual(e.bells, 0)  # bell is opt-in
        e.app.toggle_console()
        self.assertEqual(e.app._chat_unread, 0)
        self.assertEqual(e.app.unread_label.winfo_manager(), "")
        self.assertIn("[bob] one", e.text())   # still printed, just unseen

    def test_bell_flash_dnd_and_history(self):
        e = self.env
        self.app.cfg["chat"]["notify_bell"] = True
        if e.app._console_visible:
            e.app.toggle_console()
        self.bob.send_chat("a")
        e.pump(1, lambda: e.app._chat_unread == 1)
        self.assertEqual(e.bells, 1)
        self.assertIsNotNone(e.app._chat_flash_job)
        self.app.cfg["chat"]["do_not_disturb"] = True
        self.bob.send_chat("b")
        e.pump(1, lambda: e.app._chat_unread == 2)
        self.assertEqual(e.bells, 1)           # DND: still shown + counted, no bell
        self.assertIn("[bob] b", e.text())
        before = e.app._chat_unread
        e.app._show_chat({"history": [{"seq": 1, "ts": 1.0, "from": 5, "name": "old", "text": "past", "private": False, "to": []}]})
        self.assertEqual(e.app._chat_unread, before)   # history never notifies
        self.assertIn("-- chat history (1) --", e.text())

    def test_history_on_join_is_marked_and_silent(self):
        e = self.env
        self.bob.send_chat("early one")
        e.pump(0.5)
        if e.app._console_visible:
            e.app.toggle_console()
        # The host app joined before this message; a *new* client sees history.
        carol = e.join("carol")
        self.assertEqual(carol.inbox.get(timeout=2)["history"][0]["text"], "early one")


class MuteAndSettings(GuiBase):
    def test_mute_unmute_persisted(self):
        e = self.env
        e.submit("m -m bob")
        self.assertIn("Muted bob", e.text())
        with open(e.cfg_path) as f:
            self.assertEqual(json.load(f)["chat"]["muted"], ["bob"])
        self.bob.send_chat("zzsecret")
        e.pump(0.8)
        self.assertNotIn("zzsecret", e.text())
        e.submit("m -u bob")
        self.bob.send_chat("visible")
        self.assertTrue(e.pump(2, lambda: "[bob] visible" in e.text()))

    def test_disabled_chat_passes_m_to_shell(self):
        e = self.env
        self.app.cfg["chat"]["enabled"] = False
        self.bob.send_chat("ignored")
        e.pump(0.8)
        self.assertNotIn("ignored", e.text())
        e.submit("m hi")        # now an ordinary shell command
        self.assertTrue(e.pump(5, lambda: "[finished]" in e.text() or "not found" in e.text()))
        self.assertTrue(self.bob.inbox.empty())

    def test_custom_trigger_and_escape(self):
        e = self.env
        self.app.cfg["chat"]["trigger"] = "say"
        e.submit("say hello")
        self.assertEqual(self.bob.inbox.get(timeout=2)["text"], "hello")
        script = os.path.join(e.work, "m")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho SHELL-M-RAN\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        self.app.cfg["chat"]["trigger"] = "m"
        os.environ["PATH"] = e.work + os.pathsep + os.environ["PATH"]
        e.submit("\\m")
        self.assertTrue(e.pump(5, lambda: "SHELL-M-RAN" in e.text()))
        self.assertTrue(self.bob.inbox.empty())

    def test_join_leave_lines_toggle(self):
        e = self.env
        self.assertIn("bob joined the session", e.text())
        self.app.cfg["chat"]["show_join_leave"] = False
        c = e.join("zed")
        e.pump(0.8)
        self.assertNotIn("zed joined", e.text())
        self.app.cfg["chat"]["show_join_leave"] = True
        e.join("yan")
        self.assertTrue(e.pump(2, lambda: "yan joined the session" in e.text()))

    def test_timestamps(self):
        e = self.env
        self.app.cfg["chat"]["show_timestamps"] = True
        self.bob.send_chat("timed")
        e.pump(1, lambda: "timed" in e.text())
        import re
        self.assertTrue(re.search(r"\d\d:\d\d \[bob\] timed", e.text()))


class SettingsPage(unittest.TestCase):
    def open_settings(self, env):
        win = SettingsWindow(env.app, env.app.cfg, on_apply=env.app._on_settings_applied)
        env.root.update()
        return win

    def run_with(self, cfg_data):
        env = Env(cfg_data)
        try:
            win = self.open_settings(env)
            self.assertIn("Chat", [win.nb.tab(i, "text") for i in range(win.nb.index("end"))])
            self.assertTrue(isinstance(env.app.cfg["chat"]["muted"], list))
            win.destroy()
            return env
        finally:
            env.close()

    def test_loads_with_old_config_without_chat_section(self):
        self.run_with({"connection": {"name": "old"}, "editor": {"word_wrap": False}})

    def test_loads_with_garbled_chat_section(self):
        self.run_with({"chat": "nope"})
        self.run_with({"chat": {"trigger": 5, "muted": "x", "enabled": "yes", "extra": 1}})

    def test_edit_live_save_and_cancel(self):
        env = Env({"chat": {"muted": ["old"]}})
        try:
            win = self.open_settings(env)
            win.chat_vars["show_timestamps"].set(True)
            win._on_chat_toggled("show_timestamps")
            self.assertTrue(env.app._chat_cfg()["show_timestamps"])      # live preview
            win.chat_trigger_var.set("say")
            win.chat_trigger_var.set("bad word")                          # invalid keeps previous
            self.assertEqual(env.app._chat_cfg()["trigger"], "say")
            win.chat_muted_var.set("old, Jo Smith")
            self.assertEqual(env.app._chat_cfg()["muted"], ["old", "Jo Smith"])
            win._cancel()
            self.assertEqual(env.app._chat_cfg()["trigger"], "m")
            self.assertFalse(env.app._chat_cfg()["show_timestamps"])
            self.assertEqual(env.app._chat_cfg()["muted"], ["old"])
            win = self.open_settings(env)
            win.chat_vars["color_names"].set(False)
            win._on_chat_toggled("color_names")
            win._save()
            with open(env.cfg_path) as f:
                saved = json.load(f)
            self.assertFalse(saved["chat"]["color_names"])
            self.assertEqual(saved["chat"]["muted"], ["old"])
        finally:
            env.close()

    def test_reset_defaults_on_chat_tab(self):
        env = Env({"chat": {"muted": ["old"], "trigger": "zz", "do_not_disturb": True}})
        try:
            win = self.open_settings(env)
            win.nb.select(win.nb.tabs()[1])
            win.update()
            win._reset_defaults()
            self.assertEqual(env.app._chat_cfg(), config.DEFAULTS["chat"])
            win.destroy()
        finally:
            env.close()


class CompletionAndColors(GuiBase):
    preexisting = ("Jo Smith", "bobby")

    def test_tab_completes_users_but_not_paths(self):
        e = self.env
        e.pump(0.3)
        e.type("m -p Jo")
        self.assertEqual(self.app._on_console_tab(None), "break")
        self.assertEqual(e.tail(), 'm -p "Jo Smith" ')
        e.app.console.delete("input_start", "end-1c")
        e.type("m -p bo")
        e.app._on_console_tab(None)
        self.assertEqual(e.tail(), "m -p bob")        # common prefix of bob / bobby
        e.app._on_console_tab(None)                   # ambiguous -> candidates listed
        self.assertIn("bob   bobby", e.text())
        self.assertEqual(e.tail(), "m -p bob")
        e.app.console.delete("input_start", "end-1c")
        e.type("m -p Jo Smith,b")                      # unquoted space ends the user list
        self.assertEqual(self.app._complete_chat_user("m -p x hello"), None)
        e.app.console.delete("input_start", "end-1c")
        e.type('m -p "Jo Smith",bobb')
        e.app._on_console_tab(None)
        self.assertEqual(e.tail(), 'm -p "Jo Smith",bobby ')
        # Ordinary path completion is untouched.
        open(os.path.join(e.work, "zzfile.txt"), "w").close()
        e.app.console.delete("input_start", "end-1c")
        e.type("cat zz")
        e.app._on_console_tab(None)
        self.assertEqual(e.tail(), "cat zzfile.txt ")

    def test_name_colors_follow_peer_and_theme(self):
        e = self.env
        self.bob.send_chat("color me")
        e.pump(1, lambda: "color me" in e.text())
        bob_id = self.bob.client_id
        tag = f"chat_name_{bob_id}"
        color = next(p["color"] for p in e.app._roster if p["id"] == bob_id)
        self.assertEqual(self.con.tag_cget(tag, "foreground"), color)
        e.app._refresh_chat_tags("#ffffff")              # light console: darkened for contrast
        dark = self.con.tag_cget(tag, "foreground")
        self.assertNotEqual(dark, color)
        r, g, b = (int(dark[i:i + 2], 16) for i in (1, 3, 5))
        r0, g0, b0 = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        self.assertTrue(r < r0 and g < g0 and b < b0 or (r, g, b) <= (r0, g0, b0))
        self.app.cfg["chat"]["color_names"] = False
        self.assertEqual(e.app._chat_name_tag(bob_id), "info")


class CodeLinks(GuiBase):
    def test_links_jump_to_line(self):
        e = self.env
        a = e.app
        a.text.insert("1.0", "\n".join(f"line {i}" for i in range(1, 11)))
        a.text.mark_set("insert", "1.0")
        self.bob.send_chat("look at :7, main.py:3 and 10:30 and http://x.com:80")
        self.assertTrue(e.pump(2, lambda: "look at" in e.text()))
        ranges = self.con.tag_ranges("chat_link")
        links = [self.con.get(str(ranges[i]), str(ranges[i + 1])) for i in range(0, len(ranges), 2)]
        self.assertEqual(links, [":7", "main.py:3"])
        # A real click on the first link.
        idx = ranges[0]
        e.root.update()
        bbox = self.con.bbox(idx)
        self.assertIsNotNone(bbox)
        self.con.event_generate("<Motion>", x=bbox[0] + 2, y=bbox[1] + 2)   # Tk needs the pointer "over" the tag
        self.con.event_generate("<Button-1>", x=bbox[0] + 2, y=bbox[1] + 2)
        e.root.update()
        self.assertEqual(a.text.index("insert").split(".")[0], "7")
        a._follow_chat_ref(None, 99)                     # clamps to the last line
        self.assertEqual(a.text.index("insert").split(".")[0], "10")
        a._follow_chat_ref("missing.py", 2)              # no such tab: shared buffer + status note
        self.assertIn("isn't open", a.status_left.cget("text"))


class DuplicateNames(unittest.TestCase):
    def test_assigned_name_is_reported_and_used(self):
        env = Env(host_name="me", preexisting=("me",))
        try:
            self.assertEqual(env.host.username, "me#2")
            self.assertIn("you appear as 'me#2'", env.text())
            names = [p["name"] for p in env.app._roster]
            self.assertEqual(sorted(names), ["me", "me#2"])
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()

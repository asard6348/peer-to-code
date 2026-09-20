import re
import signal
import tkinter as tk
from tkinter import messagebox

import config
import theme
import dnd_support
import entry_paste
from connect_window import ConnectWindow
from editor import EditorApp


class App:
    def __init__(self, config_path=None, initial_path=None):
        if config_path:
            config.set_config_path(config_path)
        self.cfg = config.load_config()

        self.root = dnd_support.make_root()
        entry_paste.install(self.root)
        self.root.title("Peer to Code")
        self._apply_saved_geometry()
        self.root.configure(bg=self.cfg["theme"]["bg"])
        self.root.minsize(340, 260)
        theme.apply_classic_widget_defaults(self.root, self.cfg["theme"])

        self.connect_frame = None
        self.editor = None
        self.client = None
        self.server = None
        self._initial_path = initial_path

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._install_sigint_handler()
        self._show_connect_screen()

    def _install_sigint_handler(self):
        """Ctrl+C in a terminal (or `kill -INT`, same thing Termux sends)
        delivers SIGINT, and Python's default disposition for that is to
        raise KeyboardInterrupt - but only the next time the interpreter
        actually checks for a pending signal. While mainloop() is sitting
        in Tk's C-level event loop waiting on X11/Tcl events, that check
        can be delayed indefinitely: with nothing else waking the loop up,
        Ctrl+C can appear to do nothing at all, and the window has to be
        force-killed - which skips _cleanup() entirely, so nothing (last
        tab, window geometry, etc.) gets saved.

        Installing an explicit handler plus a frequent no-op timer
        (_pump_signals) fixes this two ways: the timer guarantees Python
        returns to a bytecode boundary often enough to notice the signal
        promptly, and having our own handler means we trigger the same
        graceful shutdown _cleanup() path ourselves instead of hoping a
        bare KeyboardInterrupt unwinds cleanly out of the C mainloop
        call - which, on this evidence, it doesn't always do."""
        try:
            signal.signal(signal.SIGINT, self._on_sigint)
        except (ValueError, OSError):
            # Only fails when not called from the main thread, or on a
            # platform without SIGINT - the run()-level try/except around
            # mainloop() is the fallback in that case.
            return
        self._pump_signals()

    def _on_sigint(self, _signum, _frame):
        # A signal handler runs mid-bytecode in the main thread, not as a
        # normal Tk callback - don't touch widgets directly here. Hand off
        # to a real event-loop turn instead.
        self.root.after(0, self._quit_from_signal)

    def _quit_from_signal(self):
        """A real terminal's Ctrl+C is delivered to this whole process
        (e.g. Termux behind Termux:X11, an SSH session, etc.) regardless
        of which window has focus, so this can arrive while a script is
        running in the Terminal console with no chance for that console's
        own selection-aware Ctrl+C handling to weigh in first. A real
        terminal doesn't quit the shell in that situation either - it
        forwards the interrupt to the foreground job and leaves the
        shell running. So: a script running - interrupt just that,
        exactly like the console's own Ctrl+C would, and leave the app
        open. Nothing running - fall through to actually quitting, which
        is the whole reason this handler exists (kill -INT, a terminal
        closing, etc. should still shut the app down cleanly)."""
        editor = self.editor
        if editor and editor.run_proc and editor.run_proc.poll() is None:
            editor._interrupt_run_proc()
            return
        self._cleanup(ask_to_save=False)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pump_signals(self):
        self._signal_pump_job = self.root.after(200, self._pump_signals)

    def _apply_saved_geometry(self):
        """Restores the window's last size and/or position - whichever of
        the two the person has chosen to remember in Settings > Config
        File (each is independent; either can be off). Both are clamped
        so they're always fully reachable on the current screen - a saved
        position can be stale (different monitor layout, or an
        iconified-window quirk like Windows reporting geometry as
        "1x1+-32000+-32000" while minimized), so neither is trusted
        blindly. Whichever piece isn't remembered (never saved, or
        deleted because its checkbox is off) is simply left unspecified,
        so Tk/the window manager places or sizes it automatically."""
        ui = self.cfg.get("ui", {})
        size = self._sanitize_size(ui.get("window_size", "")) or "1100x700"
        position = self._sanitize_position(ui.get("window_position", ""), size) or ""
        try:
            self.root.geometry(size + position)
        except tk.TclError:
            self.root.geometry("1100x700")

    def _sanitize_size(self, value):
        """Parses a "WxH" string and clamps it to at least minsize and no
        bigger than the current screen. Returns None if unusable."""
        m = re.match(r"^(\d+)x(\d+)$", value or "")
        if not m:
            return None
        w, h = (int(v) for v in m.groups())
        if w <= 0 or h <= 0:
            return None
        min_w, min_h = 340, 260
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        if screen_w >= min_w:
            w = max(min_w, min(w, screen_w))
        if screen_h >= min_h:
            h = max(min_h, min(h, screen_h))
        return f"{w}x{h}"

    def _sanitize_position(self, value, size):
        """Parses a "+X+Y" string and clamps it so the window (at `size`)
        stays fully reachable on the current screen. Returns None if
        unusable."""
        m = re.match(r"^([+-]\d+)([+-]\d+)$", value or "")
        if not m:
            return None
        x, y = (int(v) for v in m.groups())
        w, h = (int(v) for v in size.split("x"))
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        if screen_w <= 0 or screen_h <= 0:
            return f"+{x}+{y}"
        x = max(0, min(x, max(0, screen_w - w)))
        y = max(0, min(y, max(0, screen_h - h)))
        return f"+{x}+{y}"

    def _show_connect_screen(self):
        self.client = None
        self.server = None
        if self.editor:
            self.editor._unbind_shortcuts()
            self.editor._unbind_output_shortcuts()
            self.editor._cancel_pending_jobs()
            self.editor.pack_forget()
            self.editor.destroy()
            self.editor = None
        self.root.title("Peer to Code")
        self.root.config(menu=tk.Menu(self.root))
        self.connect_frame = ConnectWindow(self.root, self.cfg, self._on_ready, initial_path=self._initial_path)
        self._initial_path = None
        self.connect_frame.pack(fill="both", expand=True)

    def _on_ready(self, mode, client, server, working_dir, username, extra=None):
        self.client = client
        self.server = server
        self.connect_frame.pack_forget()
        self.connect_frame.destroy()
        self.connect_frame = None
        extra = extra or {}
        self.editor = EditorApp(self.root, client, server, working_dir, username, mode,
                                 cfg=self.cfg, on_leave=self._show_connect_screen,
                                 p2p_advertise=extra.get("p2p_advertise"),
                                 p2p_advertise_socket=extra.get("p2p_advertise_socket"),
                                 initial_file=extra.get("open_file"))

    def _on_close(self):
        if not self._cleanup(ask_to_save=True):
            return
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _cleanup(self, ask_to_save):
        """Persists UI state and disconnects/stops networking - shared by a
        normal window-close and a Ctrl+C in the terminal (see run()), so
        both actually save things like panel visibility/sizes and
        remembered per-file-type interpreters instead of only the former.
        Returns False if the person cancelled out of an (optional) prompt
        to save unsaved changes first, meaning the caller should NOT
        proceed with closing."""
        if ask_to_save and self.editor and self.editor.dirty:
            choice = messagebox.askyesnocancel("Save before quitting?", "Save before quitting?")
            if choice is None:
                return False
            if choice:
                self.editor.action_save()
                if self.editor.dirty:
                    return False
        if self.editor:
            self.editor._persist_ui_state()
            self.editor._unbind_shortcuts()
            self.editor._unbind_output_shortcuts()
            self.editor._cancel_pending_jobs()
        try:
            normal = self.root.state() == "normal"
        except tk.TclError:
            normal = False
        if normal:
            # Only overwrite the saved geometry while the window is in its
            # regular, on-screen state - if it's minimized/withdrawn at
            # close time, .geometry() can report nonsense (e.g. Windows
            # reports iconified windows as "1x1+-32000+-32000"), and
            # saving that would leave a bad value in place for next time.
            ui = self.cfg.setdefault("ui", {})
            m = re.match(r"^(\d+x\d+)([+-]\d+[+-]\d+)$", self.root.geometry())
            size, position = (m.group(1), m.group(2)) if m else (None, None)
            # Each half is independently opt-in (Settings > Config File):
            # when its checkbox is off, the key is removed from the
            # config entirely rather than merely left unwritten, so the
            # next launch has nothing to fall back on for it and sizes
            # or places the window automatically instead.
            if ui.get("save_window_size", True) and size:
                ui["window_size"] = size
            else:
                ui.pop("window_size", None)
            if ui.get("save_window_position", True) and position:
                ui["window_position"] = position
            else:
                ui.pop("window_position", None)
        config.save_config(self.cfg)
        client = self.editor.client if self.editor else self.client
        server = self.editor.server if self.editor else self.server
        try:
            if client:
                client.on_disconnected = None
                client.disconnect()
        except Exception:
            pass
        try:
            if server:
                server.stop()
        except Exception:
            pass
        return True

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._cleanup(ask_to_save=False)
            try:
                self.root.destroy()
            except tk.TclError:
                pass


if __name__ == "__main__":
    App().run()

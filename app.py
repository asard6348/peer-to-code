import os
import re
import signal
import sys
import tkinter as tk

import config
import theme
import dnd_support
import entry_paste
import word_nav
from connect_window import ConnectWindow
from editor import EditorApp


def resource_path(name):
    """Path to a bundled data file - next to the sources when run from
    source, or inside PyInstaller's temp extraction dir (sys._MEIPASS)
    when running as a frozen executable."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class App:
    def __init__(self, config_path=None, initial_path=None):
        if config_path:
            config.set_config_path(config_path)
        self.cfg = config.load_config()
        # Resolves the "system" preset against the OS's current light/dark setting before anything.
        theme.apply_active_preset(self.cfg)
        # Remembers what the OS was reporting just now, so _poll_system_theme can tell a real change from a no-op.
        self._system_dark = theme.detect_system_dark_mode()
        self._system_theme_job = None
        self._signal_pump_job = None

        self._set_app_id()
        self.root = dnd_support.make_root()
        entry_paste.install(self.root)
        word_nav.install(self.root)
        self.root.title("Peer to Code")
        self._set_window_icon()
        self._apply_saved_geometry()
        self.root.configure(bg=self.cfg["theme"]["bg"])
        self.root.minsize(340, 260)
        theme.apply_classic_widget_defaults(self.root, self.cfg["theme"])

        self.connect_frame = None
        self.editor = None
        self.client = None
        self.server = None
        self._initial_path = initial_path
        # Height (px) the editor's native X11 menubar adds to the window on top of what Tk's geometry() reports.
        self._menubar_extra = 0

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._install_sigint_handler()
        self._show_connect_screen()
        self._start_system_theme_watch()

    @staticmethod
    def _set_app_id():
        """Windows groups taskbar buttons by AppUserModelID; without an
        explicit one, a script run through python.exe shows Python's icon
        instead of ours. Must happen before the first window exists."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "asard6348.peertocode")
        except Exception:
            pass

    def _set_window_icon(self):
        """Title-bar/taskbar icon. Windows uses icon.ico; elsewhere Tk can
        only take a PNG, so an optional icon.png is used if present. Any
        failure just leaves Tk's default icon - never fatal."""
        if sys.platform == "win32":
            try:
                self.root.iconbitmap(default=resource_path("icon.ico"))
            except tk.TclError:
                pass
            return
        png = resource_path("icon.png")
        if os.path.exists(png):
            try:
                # Keep a reference, or Tk drops the image on GC.
                self._icon_img = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon_img)
            except tk.TclError:
                pass

    def _install_sigint_handler(self):
        """Ctrl+C in a terminal delivers SIGINT, and Python's default disposition for that is to raise KeyboardInterrupt."""
        try:
            signal.signal(signal.SIGINT, self._on_sigint)
        except (ValueError, OSError):
            # Only fails when not called from the main thread, or on a platform without SIGINT.
            return
        self._pump_signals()

    def _on_sigint(self, _signum, _frame):
        # A signal handler runs mid-bytecode in the main thread, not as a normal Tk callback.
        self.root.after(0, self._quit_from_signal)

    def _quit_from_signal(self):
        """A real terminal's Ctrl+C is delivered to this whole process regardless of which window has focus."""
        editor = self.editor
        if editor and editor.run_proc and editor.run_proc.poll() is None:
            editor._interrupt_run_proc()
            return
        self._cleanup(ask_to_save=False)
        self._stop_system_theme_watch()
        self._stop_signal_pump()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pump_signals(self):
        if not self.root.winfo_exists():
            self._signal_pump_job = None
            return
        self._signal_pump_job = self.root.after(200, self._pump_signals)

    def _stop_signal_pump(self):
        """Cancels the recurring _pump_signals timer."""
        if self._signal_pump_job is not None:
            try:
                self.root.after_cancel(self._signal_pump_job)
            except tk.TclError:
                pass
            self._signal_pump_job = None

    def _start_system_theme_watch(self):
        """Polling is the only portable way to notice the OS's light/dark setting changing while the app is already running."""
        self._poll_system_theme()

    def _poll_system_theme(self):
        self._system_theme_job = self.root.after(1500, self._poll_system_theme)
        ui_cfg = self.cfg.get("ui", {})
        if ui_cfg.get("theme_preset", theme.SYSTEM_PRESET) != theme.SYSTEM_PRESET:
            # A fixed Dark/Light/custom preset deliberately doesn't follow the OS.
            return
        dark = theme.detect_system_dark_mode()
        if dark == self._system_dark:
            return
        self._system_dark = dark
        theme_cfg = self.cfg.setdefault("theme", {})
        theme_cfg.update(theme.resolve_preset_colors(theme.SYSTEM_PRESET))
        self._apply_theme_everywhere()

    def _apply_theme_everywhere(self):
        """Repaints whichever screen is currently showing with the latest
        cfg["theme"] - used both by the system-theme watch above and
        could be reused by anything else that mutates cfg["theme"]
        out from under the currently-visible screen. The editor and the
        connect screen each know how to repaint themselves live
        (apply_theme_live); which one is live right now is exactly
        whichever of self.editor/self.connect_frame is not None."""
        t = self.cfg["theme"]
        if self.editor:
            self.editor.apply_theme_live(t)
        elif self.connect_frame:
            self.connect_frame.apply_theme_live(t)

    def _apply_saved_geometry(self):
        """Restores the window's last size and/or position."""
        ui = self.cfg.get("ui", {})
        size = self._sanitize_size(ui.get("window_size", "")) or "1100x700"
        position = self._sanitize_position(ui.get("window_position", ""), size) or ""
        self._want_pos = self._request_pos = None
        if position:
            m = re.match(r"^\+(\d+)\+(\d+)$", position)
            if m:
                self._want_pos = (int(m.group(1)), int(m.group(2)))
                dx, dy = self._saved_placement_offset()
                self._request_pos = (max(0, self._want_pos[0] - dx), max(0, self._want_pos[1] - dy))
                position = f"+{self._request_pos[0]}+{self._request_pos[1]}"
        try:
            self.root.geometry(size + position)
        except tk.TclError:
            self.root.geometry("1100x700")
            self._want_pos = None
        if self._want_pos is not None:
            self.root.after(300, self._verify_placement)

    def _saved_placement_offset(self):
        m = re.match(r"^(-?\d+),(-?\d+)$", self.cfg.get("ui", {}).get("window_placement_offset", "") or "")
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    def _verify_placement(self):
        """Some window managers place a window a title bar lower than the position it was asked for."""
        if self._want_pos is None:
            return
        try:
            if self.root.state() != "normal":
                return
            self.root.update_idletasks()
            pos = self._current_position()
        except tk.TclError:
            return
        if pos is None:
            return
        want, req = self._want_pos, self._request_pos
        dx, dy = pos[0] - req[0], pos[1] - req[1]
        if not (abs(dx) <= 150 and abs(dy) <= 150):
            return          # moved by hand or tiled, not a decoration offset
        ui = self.cfg.setdefault("ui", {})
        ui["window_placement_offset"] = f"{dx},{dy}"
        if pos != want and (dx or dy):
            try:
                self.root.geometry(f"+{max(0, want[0] - dx)}+{max(0, want[1] - dy)}")
            except tk.TclError:
                pass

    def _current_position(self):
        m = re.match(r"^\d+x\d+([+-]\d+)([+-]\d+)$", self.root.geometry())
        return (int(m.group(1)), int(m.group(2))) if m else None

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

    def _is_zoomed(self):
        try:
            return bool(self.root.attributes("-zoomed"))
        except tk.TclError:
            return False

    def _geometry_size(self):
        m = re.match(r"^(\d+)x(\d+)", self.root.geometry())
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _measure_menubar_extra(self, before_rooty):
        """On X11 a native menubar is added on top of the window's client area."""
        if not sys.platform.startswith("linux"):
            return 0
        self.root.update_idletasks()
        return max(0, self.root.winfo_rooty() - before_rooty)

    @staticmethod
    def _destroy_tree(widget):
        """Destroys a widget and everything below it."""
        try:
            widget.destroy()
            return
        except tk.TclError:
            pass
        for child in list(widget.children.values()):
            App._destroy_tree(child)
        widget._tclCommands = []
        try:
            widget.destroy()
        except tk.TclError:
            try:
                widget.tk.call("destroy", widget._w)
            except tk.TclError:
                pass

    def _show_connect_screen(self):
        self.client = None
        self.server = None
        self._menubar_extra = 0
        if self.editor:
            self.editor._unbind_shortcuts()
            self.editor._unbind_output_shortcuts()
            self.editor._cancel_pending_jobs()
            editor, self.editor = self.editor, None
            try:
                editor.pack_forget()
            except tk.TclError:
                pass
            self._destroy_tree(editor)
        self.root.title("Peer to Code")
        if sys.platform == "darwin":
            self.root.config(menu=tk.Menu(self.root))
        else:
            # No menu bar at all here.
            self.root.config(menu="")
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
        before_rooty = self.root.winfo_rooty()
        self.editor = EditorApp(self.root, client, server, working_dir, username, mode,
                                 cfg=self.cfg, on_leave=self._show_connect_screen,
                                 p2p_advertise=extra.get("p2p_advertise"),
                                 p2p_advertise_socket=extra.get("p2p_advertise_socket"),
                                 initial_file=extra.get("open_file"))
        self._menubar_extra = self._measure_menubar_extra(before_rooty)

    def _stop_system_theme_watch(self):
        if self._system_theme_job is not None:
            try:
                self.root.after_cancel(self._system_theme_job)
            except tk.TclError:
                pass
            self._system_theme_job = None

    def _on_close(self):
        if not self._cleanup(ask_to_save=True):
            return
        self._stop_system_theme_watch()
        self._stop_signal_pump()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _cleanup(self, ask_to_save):
        """Persists UI state and disconnects/stops networking."""
        if ask_to_save and self.editor and not self.editor._confirm_discard_all():
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
            # Only overwrite the saved geometry while the window is in its regular, on-screen state.
            ui = self.cfg.setdefault("ui", {})
            m = re.match(r"^(\d+x\d+)([+-]\d+[+-]\d+)$", self.root.geometry())
            size, position = (m.group(1), m.group(2)) if m else (None, None)
            if size and self.editor and self._menubar_extra and self._is_zoomed():
                # A maximized window can't grow to make room for the editor's native menubar.
                w, h = (int(v) for v in size.split("x"))
                size = f"{w}x{h + self._menubar_extra}"
            # Each half is independently opt-in.
            if ui.get("save_window_size", True) and size:
                ui["window_size"] = size
            else:
                ui.pop("window_size", None)
            if ui.get("save_window_position", True) and position:
                ui["window_position"] = position
            else:
                ui.pop("window_position", None)
                ui.pop("window_placement_offset", None)
        if self.editor:
            self.editor.stop_run()
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
            self._stop_system_theme_watch()
            self._stop_signal_pump()
            try:
                self.root.destroy()
            except tk.TclError:
                pass


if __name__ == "__main__":
    App().run()

import contextlib
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import ot
import syntax
import config
import theme
import ansi
import dnd_support
import undo_history
from text_proxy import install_proxy, install_delete_guard, char_offset
from file_explorer import FileExplorer
from peer_cursors import PeerCursorLayer
from settings_window import SettingsWindow
from net.client import FailoverController

SHORTCUT_SPECS = [
    ("new_file", "New", "<Control-n>", "action_new"),
    ("open_file", "Open File", "<Control-o>", "action_open"),
    ("save_file", "Save", "<Control-s>", "action_save"),
    ("save_as", "Save As", "<Control-Shift-S>", "action_save_as"),
    ("select_all", "Select All", "<Control-a>", "action_select_all"),
    ("find", "Find", "<Control-f>", "action_find"),
    ("replace", "Replace", "<Control-h>", "action_replace"),
    ("run", "Run Script", "<F5>", "action_run"),
    ("stop", "Stop", "<Shift-F5>", "action_stop"),
    ("toggle_comment", "Toggle Comment", "<Control-slash>", "action_toggle_comment"),
    ("duplicate_line", "Duplicate Line", "<Control-d>", "action_duplicate_line"),
    ("delete_line", "Delete Line", "<Control-Shift-K>", "action_delete_line"),
    ("move_line_up", "Move Line Up", "<Alt-Up>", None),
    ("move_line_down", "Move Line Down", "<Alt-Down>", None),
    ("indent", "Indent", "<Control-bracketright>", "action_indent"),
    ("dedent", "Dedent", "<Control-bracketleft>", "action_dedent"),
    ("undo_peer", "Undo Others' Last Change", "<Alt-z>", "action_undo_peer"),
    ("toggle_explorer", "Toggle Explorer", "<Control-b>", "toggle_explorer"),
    ("toggle_output", "Toggle Terminal", "<Control-grave>", "toggle_console"),
]

# Shortcuts scoped to the Terminal console only (bound to that widget, not
# the whole window) - terminal-like conveniences that only make sense
# while it has focus. Same (action_id, label, default_accel, method_name)
# shape as SHORTCUT_SPECS so both share the Settings > Shortcuts UI and
# the same _capture_accel/accel_display machinery.
OUTPUT_SHORTCUT_SPECS = [
    ("console_interrupt", "Send Interrupt", "<Control-c>", "_console_send_interrupt"),
]

_MOD_DISPLAY = {"Control": "Ctrl", "Shift": "Shift", "Alt": "Alt", "Command": "Cmd"}
_KEY_DISPLAY = {"slash": "/", "bracketright": "]", "bracketleft": "[",
                "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right"}

EDITOR_SCOPED_ACTIONS = frozenset({
    "select_all", "toggle_comment", "duplicate_line", "delete_line",
    "move_line_up", "move_line_down", "indent", "dedent",
})
TEXT_INPUT_CLASSES = frozenset({
    "Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Text",
})

MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 40


def accel_display(accel):
    """'<Control-Shift-S>' -> 'Ctrl+Shift+S'"""
    if not accel:
        return ""
    parts = accel.strip("<>").split("-")
    *mods, key = parts
    mods = [_MOD_DISPLAY.get(m, m) for m in mods]
    key = _KEY_DISPLAY.get(key, key if len(key) > 1 else key.upper())
    return "+".join(mods + [key])


class EditorApp(ttk.Frame):
    def __init__(self, master, client, server, working_dir, username, mode, cfg=None, on_leave=None,
                 p2p_advertise=None, p2p_advertise_socket=None, initial_file=None):
        super().__init__(master)
        self.client = client
        self.server = server
        self.working_dir = working_dir
        self.username = username
        self.mode = mode
        self.on_leave = on_leave
        self.cfg = cfg if cfg is not None else config.load_config()
        self.theme = self.cfg["theme"]
        self.shortcuts = dict(self.cfg["shortcuts"])
        self.output_shortcuts = dict(self.cfg.setdefault("output_shortcuts", {}))
        self._opened_single_file = bool(initial_file)

        self.doc = ""
        self.history = undo_history.UndoHistory()
        self._saved_doc = ""
        self._author_names = {}
        self.current_file = None
        self.active_filename_hint = None
        self._loaded_mtime = None
        self.dirty = False
        self._suppress_capture = False
        self._highlight_job = None
        self.run_proc = None
        self.out_queue = queue.Queue()
        self._exit_queue = queue.Queue()
        self._proc_exit_expected = False
        self._cursor_send_job = None
        self._poll_job = None
        self._console_resize_job = None
        self._known_peer_names = {}
        self._console_history = []
        self._console_history_pos = None
        self._console_history_stash = ""
        self._bound_accels = []
        self._bound_console_accels = []

        editor_cfg = self.cfg.setdefault("editor", {})
        self._default_new_file_language = editor_cfg.get("default_new_file_language", "python")
        syntax.set_editor_background(self.theme.get("edit_bg"))
        syntax.set_custom_colors(editor_cfg.get("custom_syntax_colors", {}))
        syntax.set_color_theme(editor_cfg.get("syntax_theme", syntax.DEFAULT_COLOR_THEME))
        self._word_wrap = bool(editor_cfg.get("word_wrap", False))
        self._run_commands = editor_cfg.setdefault("run_commands", {})
        self._suppress_run_cmd_trace = False

        self._failover = None
        self.p2p_advertise = p2p_advertise if self.mode == "p2p" else None
        if self.mode == "p2p" and p2p_advertise:
            host, port = p2p_advertise
            self._failover = FailoverController(
                username=self.username, advertise_host=host, advertise_port=port,
                get_doc_text=lambda: self.text.get("1.0", "end-1c"),
                on_reconnected=self._on_failover_reconnected,
                on_failed=self._on_failover_failed,
                advertise_socket=p2p_advertise_socket,
            )

        self.pack(fill="both", expand=True)
        self._build_menu()
        self._build_layout()
        self._apply_shortcuts()
        self._apply_output_shortcuts()

        self._bind_client(self.client)

        self._pending_initial_file = initial_file

        self._poll_job = self.after(100, self._poll_output)
        if self.mode != "solo":
            self._set_status("Waiting for document sync...")

    @property
    def dirty(self):
        return self._dirty

    @dirty.setter
    def dirty(self, value):
        self._dirty = value
        self._update_title()

    def _update_title(self):
        base = "Peer to Code"
        self.winfo_toplevel().title(f"*{base}*" if self._dirty else base)

    def _refresh_dirty(self):
        dirty = self.doc != self._saved_doc
        if dirty != self._dirty:
            self.dirty = dirty

    def _bind_client(self, client):
        """Wire up all of a Client's callbacks. Used at startup, and again
        after a P2P failover promotes/reconnects to a brand-new Client."""
        client.on_full_sync = self._on_full_sync
        client.on_remote_op = self._on_remote_op
        client.on_peers = self._on_peers
        client.on_cursor = self._on_cursor_msg
        client.on_disconnected = self._on_disconnected
        client.on_buffer_context = self._on_buffer_context

    def _build_menu(self):
        root = self.winfo_toplevel()
        menubar = tk.Menu(root)
        root.config(menu=menubar)
        a = self._accel

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="New", accelerator=a("new_file"), command=self.action_new)
        m_file.add_command(label="Open", accelerator=a("open_file"), command=self.action_open)
        m_file.add_command(label="Save", accelerator=a("save_file"), command=self.action_save)
        m_file.add_command(label="Save As", accelerator=a("save_as"), command=self.action_save_as)
        m_file.add_separator()
        m_file.add_command(label="Reveal Working Directory", command=self.action_reveal)
        m_file.add_separator()
        if self.mode == "p2p" and self.p2p_advertise:
            m_file.add_command(label="Copy Invite Address", command=self.action_copy_invite_address)
            m_file.add_separator()
        m_file.add_command(label="Settings", command=self.open_settings)
        m_file.add_separator()
        m_file.add_command(label=("Close" if self.mode == "solo" else "Disconnect"), command=self.action_disconnect)
        m_file.add_command(label="Exit", command=root.destroy)
        menubar.add_cascade(label="File", menu=m_file)

        m_edit = tk.Menu(menubar, tearoff=0)
        m_edit.add_command(label="Undo", accelerator="Ctrl+Z", command=self.action_undo)
        m_edit.add_command(label="Redo", accelerator="Ctrl+Y", command=self.action_redo)
        if self.mode != "solo":
            m_edit.add_command(label="Undo Others' Last Change", accelerator=a("undo_peer"), command=self.action_undo_peer)
            self._peer_undo_menu = tk.Menu(m_edit, tearoff=0, postcommand=self._fill_peer_undo_menu)
            m_edit.add_cascade(label="Undo Change By", menu=self._peer_undo_menu)
        m_edit.add_separator()
        m_edit.add_command(label="Cut", accelerator="Ctrl+X", command=lambda: self.text.event_generate("<<Cut>>"))
        m_edit.add_command(label="Copy", accelerator="Ctrl+C", command=lambda: self.text.event_generate("<<Copy>>"))
        m_edit.add_command(label="Paste", accelerator="Ctrl+V", command=lambda: self.text.event_generate("<<Paste>>"))
        m_edit.add_command(label="Select All", accelerator=a("select_all"), command=self.action_select_all)
        m_edit.add_separator()
        m_edit.add_command(label="Find", accelerator=a("find"), command=self.action_find)
        m_edit.add_command(label="Replace", accelerator=a("replace"), command=self.action_replace)
        m_edit.add_separator()
        m_edit.add_command(label="Toggle Comment", accelerator=a("toggle_comment"), command=self.action_toggle_comment)
        m_edit.add_command(label="Duplicate Line", accelerator=a("duplicate_line"), command=self.action_duplicate_line)
        m_edit.add_command(label="Delete Line", accelerator=a("delete_line"), command=self.action_delete_line)
        m_edit.configure(postcommand=self._refresh_edit_menu)
        self._edit_menu = m_edit
        menubar.add_cascade(label="Edit", menu=m_edit)

        m_run = tk.Menu(menubar, tearoff=0)
        m_run.add_command(label="Run Script", accelerator=a("run"), command=self.action_run)
        m_run.add_command(label="Stop", accelerator=a("stop"), command=self.action_stop)
        menubar.add_cascade(label="Run", menu=m_run)

        m_view = tk.Menu(menubar, tearoff=0)
        m_view.add_command(label="Toggle Explorer", accelerator=a("toggle_explorer"), command=self.toggle_explorer)
        m_view.add_command(label="Toggle Terminal", accelerator=a("toggle_output"), command=self.toggle_console)
        menubar.add_cascade(label="View", menu=m_view)

        self.menubar = menubar

    def _author_display(self, author):
        return self._author_names.get(author) or "another user"

    def _refresh_edit_menu(self):
        menu = self._edit_menu
        menu.entryconfigure(0, state="normal" if self.history.can_undo() else "disabled")
        menu.entryconfigure(1, state="normal" if self.history.can_redo() else "disabled")
        if self.mode != "solo":
            author = self.history.latest_peer()
            if author is None:
                menu.entryconfigure(2, label="Undo Others' Last Change", state="disabled")
            else:
                menu.entryconfigure(2, label=f"Undo {self._author_display(author)}'s Last Change", state="normal")
            menu.entryconfigure(3, state="normal" if self.history.peer_authors() else "disabled")

    def _fill_peer_undo_menu(self):
        menu = self._peer_undo_menu
        menu.delete(0, "end")
        for author in self.history.peer_authors():
            menu.add_command(label=self._author_display(author), command=lambda who=author: self.action_undo_peer(who))

    def _accel(self, action_id):
        return accel_display(self.shortcuts.get(action_id, ""))

    def open_settings(self):
        SettingsWindow(self, self.cfg, on_apply=self._on_settings_applied)

    def _on_settings_applied(self, changed):
        if "shortcuts" in changed:
            self.shortcuts = dict(self.cfg["shortcuts"])
            self._apply_shortcuts()
            self._build_menu()
        if "output_shortcuts" in changed:
            self.output_shortcuts = dict(self.cfg["output_shortcuts"])
            self._apply_output_shortcuts()
        if "theme" in changed:
            try:
                self.apply_theme_live(self.cfg["theme"])
                self._set_status("Theme applied.")
            except tk.TclError:
                self._set_status("Theme saved. Restart the app to apply it.")
        if "ui" in changed:
            ui_cfg = self.cfg.get("ui", {})
            if "explorer_font_family" in ui_cfg:
                self._set_explorer_font_family(ui_cfg["explorer_font_family"])
            if "explorer_font_size" in ui_cfg:
                self._set_explorer_font_size(int(ui_cfg["explorer_font_size"]))
            if "console_font_family" in ui_cfg:
                self._set_console_font_family(ui_cfg["console_font_family"])
            if "console_font_size" in ui_cfg:
                self._set_console_font_size(int(ui_cfg["console_font_size"]))
        if "editor" in changed:
            editor_cfg = self.cfg.setdefault("editor", {})
            self._default_new_file_language = editor_cfg.get("default_new_file_language", "python")
            self._run_commands = editor_cfg.setdefault("run_commands", {})
            syntax.set_custom_colors(editor_cfg.get("custom_syntax_colors", {}))
            syntax.set_color_theme(editor_cfg.get("syntax_theme", syntax.DEFAULT_COLOR_THEME))
            syntax.configure_tags(self.text)
            self._word_wrap = bool(editor_cfg.get("word_wrap", False))
            self.text.configure(wrap=("word" if self._word_wrap else "none"))
            self._update_language_status()
            self._do_highlight()

    def _apply_ttk_style(self, t):
        style = ttk.Style(self)
        theme.apply_base_style(style, t)

    def apply_theme_live(self, new_theme):
        self.theme = t = new_theme
        font_family = t.get("font_family", "Consolas")
        self._font_size = int(t.get("font_size", 11))
        self._apply_ttk_style(t)
        theme.apply_classic_widget_defaults(self.winfo_toplevel(), t)

        for frame in (self.toolbar, self.body, self.center, self.edit_area, self.text_frame,
                      self.console_frame):
            frame.configure(bg=t["bg"])
        self.interp_label.configure(bg=t["bg"], fg=t["fg"])
        self.peers_label.configure(bg=t["bg"], fg=t["fg"])
        self.run_cmd_entry.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"])
        self.linenumbers.configure(bg=t["gutter_bg"])
        self.text.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"],
                             selectbackground=t["sel_bg"], font=(font_family, self._font_size))
        self.console_label.configure(bg=t["bg"], fg=t["muted_fg"])
        # Explorer and Terminal now have fully independent font family
        # *and* size (see _set_explorer_font_size/_family and
        # _set_console_font_size/_family) - a theme change updates
        # colors everywhere, including the console's background here,
        # but deliberately leaves both panels' own fonts alone.
        self.console.configure(bg=t["console_bg"])

        self.cursor_layer.set_theme(t["edit_bg"])
        syntax.set_editor_background(t["edit_bg"])
        syntax.configure_tags(self.text)
        self._do_highlight()
        self._redraw_linenumbers()

    def _build_layout(self):
        t = self.theme
        ui_prefs = self.cfg.get("ui", {})
        font_family = t.get("font_family", "Consolas")
        font_size = int(t.get("font_size", 11))
        self._font_size = int(ui_prefs.get("editor_font_size") or font_size)
        self._console_font_family = ui_prefs.get("console_font_family") or font_family
        self._console_font_size = int(ui_prefs.get("console_font_size") or max(font_size - 1, 8))
        self._apply_ttk_style(t)

        toolbar = tk.Frame(self, bg=t["bg"])
        toolbar.pack(fill="x")
        self.toolbar = toolbar

        self.disconnect_btn = tk.Button(toolbar, text=("Close" if self.mode == "solo" else "Disconnect"),
                  command=self.action_disconnect, bg="#5a3030", fg="white",
                  relief="flat", activebackground="#734040", padx=8)
        self.disconnect_btn.pack(side="right", padx=(0, 6), pady=4)
        self.peers_label = tk.Label(toolbar, text="", bg=t["bg"], fg=t["fg"], anchor="e")
        self.peers_label.pack(side="right", padx=(16, 6))

        self.run_btn = tk.Button(toolbar, text="Run", command=self.action_run, bg="#2f8f5b", fg="white", relief="flat",
                  activebackground="#3aa76a", padx=10)
        self.run_btn.pack(side="left", padx=6, pady=4)
        self.stop_btn = tk.Button(toolbar, text="Stop", command=self.action_stop, bg="#8f3f3f", fg="white", relief="flat",
                  activebackground="#a94e4e", padx=10)
        self.stop_btn.pack(side="left", pady=4)
        self.interp_label = tk.Label(toolbar, text="Interpreter:", bg=t["bg"], fg=t["fg"])
        self.interp_label.pack(side="left", padx=(16, 4))
        self.run_cmd = tk.StringVar(value=sys.executable)
        self.run_cmd_entry = tk.Entry(toolbar, textvariable=self.run_cmd, width=28, bg=t["edit_bg"], fg=t["fg"],
                                       insertbackground=t["fg"], relief="flat")
        self.run_cmd_entry.pack(side="left", fill="x", expand=True)
        self.run_cmd.trace_add("write", self._on_run_cmd_edited)

        self.file_path_label = tk.Label(toolbar, text="", bg=t["bg"], fg=t["muted_fg"], anchor="w")
        self.file_path_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

        status = tk.Frame(self, bg="#3a3d41", height=22)
        status.pack(fill="x", side="bottom")
        self.status_frame = status
        self.status_right = tk.Label(status, text="", bg="#3a3d41", fg="white", anchor="e")
        self.status_right.pack(side="right", padx=8)
        self.status_left = tk.Label(status, text="", bg="#3a3d41", fg="white", anchor="w")
        self.status_left.pack(side="left", padx=8)

        body = tk.PanedWindow(self, orient="horizontal", bg=t["bg"], sashwidth=4, bd=0)
        body.pack(fill="both", expand=True)
        self.body = body

        self.explorer = FileExplorer(body, self.working_dir, self._open_file_from_explorer, width=220)
        # A plain sans-serif face reads better for a file tree than
        # whatever the ttk theme's own default happens to be, and a
        # touch smaller than the editor/Terminal's own default size
        # suits a dense list of file names.
        self._explorer_font_family = ui_prefs.get("explorer_font_family") or "sans-serif"
        self._explorer_font_size = int(ui_prefs.get("explorer_font_size") or 9)
        self.explorer.set_font(self._explorer_font_family, self._explorer_font_size)
        dnd_support.register_drop(self.explorer, self._on_explorer_drop)
        dnd_support.register_drop(self.explorer.tree, self._on_explorer_drop)
        if self._opened_single_file:
            self.explorer.show_whole_computer()
        self._explorer_visible = ui_prefs.get("explorer_visible", True)
        if self._explorer_visible:
            body.add(self.explorer, minsize=60, stretch="never",
                     width=ui_prefs.get("explorer_width", 220))

        center = tk.PanedWindow(body, orient="vertical", bg=t["bg"], sashwidth=4, bd=0)
        body.add(center, minsize=260, stretch="always")
        self.center = center

        edit_area = tk.Frame(center, bg=t["bg"])
        center.add(edit_area, minsize=30, stretch="always")
        self.edit_area = edit_area

        self.linenumbers = tk.Canvas(edit_area, width=48, bg=t["gutter_bg"], highlightthickness=0)
        self.linenumbers.pack(side="left", fill="y")

        text_frame = tk.Frame(edit_area, bg=t["bg"])
        text_frame.pack(side="left", fill="both", expand=True)
        self.text_frame = text_frame

        yscroll = tk.Scrollbar(text_frame, orient="vertical")
        yscroll.pack(side="right", fill="y")
        self.yscroll = yscroll

        xscroll = tk.Scrollbar(text_frame, orient="horizontal", command=self._on_xscroll)
        xscroll.pack(side="bottom", fill="x")

        self.text = tk.Text(text_frame, wrap=("word" if self._word_wrap else "none"), undo=False,
                             bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"], selectbackground=t["sel_bg"],
                             font=(font_family, self._font_size), padx=8, pady=6, relief="flat",
                             yscrollcommand=self._on_text_yview_changed, xscrollcommand=xscroll.set, tabs=("1c",))
        self.text.pack(side="left", fill="both", expand=True)
        yscroll.config(command=self._on_yscroll)

        syntax.configure_tags(self.text)
        install_proxy(self.text, self._on_local_insert, self._on_local_delete)
        self.cursor_layer = PeerCursorLayer(self.text, t["edit_bg"], self.client.client_id)

        console_frame = tk.Frame(center, bg=t["bg"])
        self._console_visible = ui_prefs.get("console_visible", True)
        if self._console_visible:
            kwargs = {"minsize": 70, "stretch": "never"}
            if "console_height" in ui_prefs:
                kwargs["height"] = ui_prefs["console_height"]
            center.add(console_frame, **kwargs)
        self.console_frame = console_frame
        self.console_label = tk.Label(console_frame, text="TERMINAL", bg=t["bg"], fg=t["muted_fg"], font=("Segoe UI", 9, "bold"),
                  anchor="w")
        self.console_label.pack(fill="x", padx=6, pady=(4, 0))

        console_body = tk.Frame(console_frame, bg=t["bg"])
        console_body.pack(fill="both", expand=True, padx=2, pady=2)

        console_yscroll = tk.Scrollbar(console_body, orient="vertical")
        console_yscroll.pack(side="right", fill="y")

        self.console = tk.Text(console_body, height=4, bg=t["console_bg"], fg="#c9d1d9", relief="flat",
                                font=(self._console_font_family, self._console_font_size),
                                yscrollcommand=console_yscroll.set)
        self.console.pack(side="left", fill="both", expand=True)
        console_yscroll.config(command=self.console.yview)
        self.console.tag_configure("stderr", foreground="#e06c75")
        self.console.tag_configure("info", foreground="#61afef")
        self.console.tag_configure("prompt", foreground="#98c379")
        self.ansi_console = ansi.AnsiConsole(
            self.console, base_tag_colors={"stderr": "#e06c75", "info": "#61afef", "prompt": "#98c379"})

        # A run's prompt (e.g. input(">> ")) is shown right in this same
        # pane, and the person types their reply directly after it -
        # same for the $ prompt shown here whenever nothing's running,
        # where what gets typed and submitted is a command instead of a
        # reply. "input_start" is the boundary between that settled
        # output (read-only) and the live, editable tail. See
        # _on_console_key / _submit_console_input.
        self.console.mark_set("input_start", "end-1c")
        # Left gravity: text typed exactly at this mark (i.e. right after
        # a prompt, the common case) must extend past it rather than
        # push it forward - otherwise every keystroke would silently
        # widen the "already sent" region to swallow whatever was just
        # typed, and Enter would always submit an empty line.
        self.console.mark_gravity("input_start", "left")
        # Backspace/Delete are blocked from crossing input_start in
        # _on_console_key below, but Tk's Text widget has several other
        # built-in ways to delete text - Ctrl+X (<<Cut>>), Ctrl+D/Ctrl+H
        # (its emacs-style bindings), a right-click context menu - that
        # would otherwise bypass that key-level check entirely. This
        # catches all of them at once, whatever they're bound to.
        install_delete_guard(self.console, "input_start")
        self.console.bind("<Key>", self._on_console_key)
        self.console.bind("<<Paste>>", self._on_console_paste)
        self.console.bind("<Tab>", self._on_console_tab)
        self.console.bind("<Up>", self._on_console_history)
        self.console.bind("<Down>", self._on_console_history)
        self._show_prompt()

        self._console_resize_job = None
        self.console.bind("<Configure>", self._on_console_resize)
        console_frame.bind("<Configure>", self._on_console_resize)

        self._update_language_status()

        self.text.bind("<KeyRelease>", self._on_key_release)
        self.text.bind("<ButtonRelease-1>", self._update_cursor_status)
        self.text.bind("<<Undo>>", self.action_undo)
        self.text.bind("<<Redo>>", self.action_redo)
        self.text.bind("<Control-y>", self.action_redo)
        self.text.bind("<<Paste>>", self._on_text_paste)
        self._bind_zoom_gestures()

    def _on_yscroll(self, *args):
        self.text.yview(*args)
        self._redraw_linenumbers()

    def _on_text_yview_changed(self, *args):
        """The Text widget's yscrollcommand, called on every change to
        the visible range, not just drags of our own scrollbar: mouse-wheel
        (Windows/macOS <MouseWheel>), touchpad/wheel on X11 (delivered as
        <Button-4>/<Button-5>, which Tk's default Text bindings already
        handle before we ever see them), keyboard navigation, search
        jumps, etc. Hooking here instead of specific event bindings is what
        keeps the line-number gutter in sync regardless of *how* the view
        moved, rather than only for whichever input method we happened to
        bind."""
        self.yscroll.set(*args)
        self._redraw_linenumbers()

    def _on_xscroll(self, *args):
        self.text.xview(*args)
        self._redraw_linenumbers()

    def toggle_explorer(self):
        if self._explorer_visible:
            self.body.forget(self.explorer)
        else:
            width = self.cfg.get("ui", {}).get("explorer_width", 220)
            self.body.add(self.explorer, before=str(self.center), minsize=60,
                           stretch="never", width=width)
        self._explorer_visible = not self._explorer_visible

    def _switch_working_dir(self, new_dir):
        """Points the Explorer panel (and Run/Save As dialogs) at a
        different local folder - a local view change only, independent of
        the collaborative session, which keeps running unaffected."""
        if not os.path.isdir(new_dir):
            return
        self.working_dir = new_dir
        self.explorer.set_working_dir(new_dir)

    def _on_explorer_drop(self, paths):
        for path in paths:
            if os.path.isdir(path):
                self._switch_working_dir(path)
            elif os.path.isfile(path):
                self._load_file(path)

    def toggle_console(self):
        if self._console_visible:
            self.center.forget(self.console_frame)
        else:
            ui_prefs = self.cfg.get("ui", {})
            kwargs = {"minsize": 70, "stretch": "never"}
            if "console_height" in ui_prefs:
                kwargs["height"] = ui_prefs["console_height"]
            self.center.add(self.console_frame, **kwargs)
        self._console_visible = not self._console_visible

    def _redraw_linenumbers(self, *_):
        self.linenumbers.delete("all")
        i = self.text.index("@0,0")
        while True:
            dline = self.text.dlineinfo(i)
            if dline is None:
                break
            y = dline[1]
            line_no = str(i).split(".")[0]
            self.linenumbers.create_text(40, y, anchor="ne", text=line_no, fill=self.theme["gutter_fg"],
                                          font=(self.theme.get("font_family", "Consolas"), self._font_size))
            i = self.text.index(f"{i}+1line")
            if self.text.compare(i, ">=", "end"):
                dline2 = self.text.dlineinfo(i)
                if dline2 is None:
                    break
        if hasattr(self, "cursor_layer"):
            self.cursor_layer.refresh()

    def _bind_zoom_gestures(self):
        """Ctrl+scroll wheel (desktop) and pinch (touch/trackpad) each
        change the font size of whichever of the three panels they're
        used over - the code editor (together with its line-number
        gutter, so the two stay in proportion), the Explorer file tree,
        or the Terminal console - independently of one another. Bound
        directly on the widgets rather than routed through the
        remappable SHORTCUT_SPECS/shortcuts config system: these are
        gestures, not key accelerators, and "on mobile always
        pinch-to-zoom" means this shouldn't be something a shortcut
        remap could turn off.

        Caveat, to be upfront about: <<TouchpadPinch>> is only actually
        *generated* by Tk's own macOS (Aqua) build - vanilla Tk/X11 (which
        is what Termux's X11 route uses) has no standard event for a
        genuine two-finger pinch at all, so this binding is a no-op
        there. Whatever a two-finger touchscreen pinch actually turns
        into by the time it reaches Tk on that route (typically a burst
        of synthetic wheel-tick events) is handled by the coalescing in
        _bind_zoom below instead. Ctrl+scroll is the one guaranteed to
        work everywhere a wheel/scroll event reaches Tk at all.
        """
        self._bind_zoom((self.text, self.linenumbers),
                         lambda steps: self._set_font_size(self._font_size + steps))
        self._bind_zoom((self.explorer.tree,),
                         lambda steps: self._set_explorer_font_size(self._explorer_font_size + steps))
        self._bind_zoom((self.console,),
                         lambda steps: self._set_console_font_size(self._console_font_size + steps))

    def _bind_zoom(self, widgets, zoom_by):
        """A real pinch or scroll gesture - especially a touchscreen
        pinch translated into wheel ticks by whatever X server/VNC layer
        sits between the touch and Tk - can deliver a whole burst of
        these events within a handful of milliseconds, not just one or
        two. Applying every single tick immediately meant dozens of full
        font/style reflows firing back-to-back for one gesture, which is
        what showed up as the Terminal's scrollbar frantically resizing
        and the font appearing "stuck" - by the time the eye registered
        one size, several more had already been applied and half-undone
        by jitter in the other direction. Coalescing the ticks and
        applying only their net total after a brief pause fixes that
        without changing how any single Ctrl+scroll click behaves."""
        state = {"accum": 0, "job": None}
        anchor = widgets[0]

        def flush():
            state["job"] = None
            if state["accum"]:
                zoom_by(state["accum"])
                state["accum"] = 0

        def queue(steps):
            state["accum"] += steps
            if state["job"] is not None:
                anchor.after_cancel(state["job"])
            state["job"] = anchor.after(80, flush)

        def on_wheel(event):
            num = getattr(event, "num", None)
            if num == 4:
                queue(1)
            elif num == 5:
                queue(-1)
            elif getattr(event, "delta", 0) > 0:
                queue(1)
            else:
                queue(-1)
            return "break"

        def on_pinch(event):
            delta = getattr(event, "delta", 0)
            if delta > 0:
                queue(1)
            elif delta < 0:
                queue(-1)
            return "break"

        for widget in widgets:
            widget.bind("<Control-MouseWheel>", on_wheel)
            widget.bind("<Control-Button-4>", on_wheel)
            widget.bind("<Control-Button-5>", on_wheel)
            widget.bind("<<TouchpadPinch>>", on_pinch)

    def _set_font_size(self, size):
        size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))
        if size == self._font_size:
            return
        self._font_size = size
        font_family = self.theme.get("font_family", "Consolas")
        self.text.configure(font=(font_family, size))
        self._redraw_linenumbers()

    def _set_explorer_font_size(self, size):
        size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))
        if size == self._explorer_font_size:
            return
        self._explorer_font_size = size
        self.explorer.set_font(self._explorer_font_family, size)

    def _set_explorer_font_family(self, family):
        if not family or family == self._explorer_font_family:
            return
        self._explorer_font_family = family
        self.explorer.set_font(family, self._explorer_font_size)

    def _set_console_font_size(self, size):
        size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))
        if size == self._console_font_size:
            return
        self._console_font_size = size
        self.console.configure(font=(self._console_font_family, size))
        self.ansi_console.rescale_fonts(size=size)

    def _set_console_font_family(self, family):
        if not family or family == self._console_font_family:
            return
        self._console_font_family = family
        self.console.configure(font=(family, self._console_font_size))
        self.ansi_console.rescale_fonts(family=family)

    def _on_key_release(self, event):
        self._redraw_linenumbers()
        self._update_cursor_status()
        self._schedule_highlight()

    def _update_cursor_status(self, *_):
        line, col = self.text.index("insert").split(".")
        self.status_left.configure(text=f"Ln {line}, Col {int(col) + 1}   {'*' if self.dirty else ''} {self._file_label()}")
        self._schedule_cursor_send()

    def _schedule_cursor_send(self):
        if self._cursor_send_job:
            return

        def fire():
            self._cursor_send_job = None
            offset = char_offset(self.text, "insert")
            has_sel = bool(self.text.tag_ranges("sel"))
            start = end = None
            if has_sel:
                try:
                    start = char_offset(self.text, "sel.first")
                    end = char_offset(self.text, "sel.last")
                except tk.TclError:
                    has_sel = False
            self.client.send_cursor(offset, has_sel, start, end)

        self._cursor_send_job = self.after(30, fire)

    def _file_label(self):
        if self.current_file:
            return os.path.relpath(self.current_file, self.working_dir)
        return "untitled"

    def _schedule_highlight(self):
        if self._highlight_job:
            self.after_cancel(self._highlight_job)
            self._highlight_job = None
        self._do_highlight()

    def _current_language(self):
        """The active buffer's language, falling back to the user's
        configured default (Settings > General) rather than unconditionally
        assuming Python for a brand-new, not-yet-saved buffer."""
        return syntax.detect_language(self.active_filename_hint, default=self._default_new_file_language)

    def _do_highlight(self):
        self._highlight_job = None
        syntax.highlight(self.text, self._current_language())

    def _update_language_status(self):
        language = self._current_language()
        wd_name = os.path.basename(self.working_dir.rstrip(os.sep)) or self.working_dir
        self.status_right.configure(text=f"{syntax.language_label(language)}   |   {wd_name} (local)")
        self._recall_run_command(language)

    def _run_command_key(self, language):
        return language or "plaintext"

    def _on_run_cmd_edited(self, *_args):
        if self._suppress_run_cmd_trace:
            return
        key = self._run_command_key(self._current_language())
        value = self.run_cmd.get().strip()
        if value:
            self._run_commands[key] = value
        else:
            self._run_commands.pop(key, None)

    def _recall_run_command(self, language):
        """Swaps the Interpreter box to whatever command was last used for
        `language`, if anything was - leaving it untouched otherwise, so
        switching to a file type with no memorized command yet doesn't
        blank out or guess-overwrite what's already typed there."""
        remembered = self._run_commands.get(self._run_command_key(language))
        if not remembered or remembered == self.run_cmd.get():
            return
        self._suppress_run_cmd_trace = True
        try:
            self.run_cmd.set(remembered)
        finally:
            self._suppress_run_cmd_trace = False

    def _on_local_insert(self, offset, text):
        self._redraw_linenumbers()
        if self._suppress_capture:
            return
        op = ot.Op()
        op.retain(offset)
        op.insert(text)
        op.retain(len(self.doc) - offset)
        before = self.doc
        self.doc = op.apply(before)
        self.history.record(undo_history.LOCAL, op, before)
        self._refresh_dirty()
        self._update_cursor_status()
        self.client.local_edit(op)

    def _on_local_delete(self, off_start, off_end):
        self._redraw_linenumbers()
        if self._suppress_capture:
            return
        op = ot.Op()
        op.retain(off_start)
        op.delete(off_end - off_start)
        op.retain(len(self.doc) - off_end)
        before = self.doc
        self.doc = op.apply(before)
        self.history.record(undo_history.LOCAL, op, before)
        self._refresh_dirty()
        self._update_cursor_status()
        self.client.local_edit(op)

    def _on_full_sync(self, text):
        self.after(0, lambda: self._apply_full_sync(text))

    def _reset_history(self):
        self.history.reset()
        self._saved_doc = self.doc
        self._refresh_dirty()

    def _apply_full_sync(self, text):
        self._suppress_capture = True
        try:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
        finally:
            self._suppress_capture = False
        self.doc = text
        self._reset_history()
        self._redraw_linenumbers()
        self._do_highlight()
        if self.mode != "solo":
            self._set_status("Connected and synced with host")
        if self._pending_initial_file:
            path, self._pending_initial_file = self._pending_initial_file, None
            self._load_file(path)
        else:
            self._update_file_path_label()

    def _on_remote_op(self, op, author=None):
        self.after(0, lambda: self._apply_remote_op(op, author))

    def _apply_op_to_widget(self, op):
        self._suppress_capture = True
        try:
            idx = 0
            for c in op.ops:
                if isinstance(c, int) and c > 0:
                    idx += c
                elif isinstance(c, str):
                    self.text.insert(f"1.0+{idx}c", c)
                    idx += len(c)
                else:
                    n = -c
                    self.text.delete(f"1.0+{idx}c", f"1.0+{idx + n}c")
        finally:
            self._suppress_capture = False

    def _apply_remote_op(self, op, author=None):
        before = self.doc
        self._apply_op_to_widget(op)
        self.doc = op.apply(before)
        self.history.record("remote" if author is None else author, op, before)
        self._refresh_dirty()
        self._redraw_linenumbers()
        self._schedule_highlight()

    def _apply_history_op(self, op):
        before = self.doc
        self._apply_op_to_widget(op)
        self.doc = op.apply(before)
        self.client.local_edit(op)
        self._place_caret_after(op)
        self._refresh_dirty()
        self._redraw_linenumbers()
        self._schedule_highlight()
        self._update_cursor_status()

    def _place_caret_after(self, op):
        new = 0
        last = None
        for c in op.ops:
            if isinstance(c, str):
                new += len(c)
                last = new
            elif c > 0:
                new += c
            else:
                last = new
        self.text.tag_remove("sel", "1.0", "end")
        if last is not None:
            self.text.mark_set("insert", f"1.0+{last}c")
            self.text.see("insert")

    def action_undo(self, _event=None):
        op = self.history.undo(self.doc)
        if op is not None:
            self._apply_history_op(op)
        return "break"

    def action_redo(self, _event=None):
        op = self.history.redo(self.doc)
        if op is not None:
            self._apply_history_op(op)
        return "break"

    def action_undo_peer(self, author=None):
        if author is None:
            author = self.history.latest_peer()
        if author is None:
            self._set_status("No changes by other users to undo")
            return "break"
        op = self.history.undo_peer(author, self.doc)
        if op is not None:
            self._apply_history_op(op)
            self._set_status(f"Undid {self._author_display(author)}'s last change")
        return "break"

    def _on_peers(self, peers):
        self.after(0, lambda: self._render_peers(peers))

    def _render_peers(self, peers, note_failover=True):
        others = [pr for pr in peers if pr["id"] != self.client.client_id]
        current = {pr["id"]: pr["name"] for pr in others}
        for pid, name in current.items():
            if pid not in self._known_peer_names:
                self._console_write(f"{name} joined the session\n", "info")
        for pid, name in self._known_peer_names.items():
            if pid not in current:
                self._console_write(f"{name} disconnected\n", "info")
        self._known_peer_names = current
        self._author_names.update(current)
        names = ", ".join(current.values())
        self.peers_label.configure(text=f"{len(others)} connected: {names}" if others else "")
        self.cursor_layer.set_roster(peers)
        if note_failover and self._failover is not None:
            self._failover.note_roster(peers)

    def _on_cursor_msg(self, payload):
        self.after(0, lambda: self.cursor_layer.update_cursor(payload))

    def _on_disconnected(self, reason):
        self.after(0, lambda: self._connection_lost(reason))

    def _connection_lost(self, reason):
        self._render_peers([], note_failover=False)
        self._known_peer_names = {}
        if self._failover is not None:
            self.peers_label.configure(text="Reconnecting...")
            self._set_status(f"{reason} Trying to reconnect automatically...")
            self._console_write(f"{reason} Trying to reconnect automatically...\n", "info")
            self._failover.begin(self.client, reason)
            return
        self._handle_disconnected(reason)

    def _handle_disconnected(self, reason):
        self.peers_label.configure(text="Disconnected from host")
        try:
            self.client.transport.stop()
        except Exception:
            pass
        messagebox.showwarning("Disconnected", reason)

    def _on_failover_reconnected(self, new_client, new_server, reason):
        self.after(0, lambda: self._finish_failover(new_client, new_server, reason))

    def _finish_failover(self, new_client, new_server, reason):
        self.client = new_client
        self.server = new_server
        self._bind_client(new_client)
        self.cursor_layer.clear()
        self.cursor_layer.set_self_id(new_client.client_id)
        self._known_peer_names = {}
        note = "you're now the sequencer" if new_server is not None else "reconnected to the new sequencer"
        self._set_status(f"Back online: {note}")
        self._console_write(f"Reconnected ({note})\n", "info")
        self.peers_label.configure(text="")

    def _on_failover_failed(self, reason):
        self.after(0, lambda: self._handle_disconnected(reason))

    def _set_status(self, text):
        self.status_left.configure(text=text)

    def _persist_ui_state(self):
        """Remembers panel visibility, their manually-adjusted sizes,
        which Connect-screen tab this session started from, and the
        editor's zoomed-in/out font size, so all of it comes back the
        same way next time."""
        ui = self.cfg.setdefault("ui", {})
        ui["explorer_visible"] = self._explorer_visible
        ui["console_visible"] = self._console_visible
        ui["last_tab"] = {"solo": "Open", "p2p": "Peer to Peer"}.get(self.mode, "Connect")
        ui["editor_font_size"] = self._font_size
        ui["explorer_font_size"] = self._explorer_font_size
        ui["explorer_font_family"] = self._explorer_font_family
        ui["console_font_size"] = self._console_font_size
        ui["console_font_family"] = self._console_font_family
        if self._explorer_visible:
            ui["explorer_width"] = self.explorer.winfo_width()
        if self._console_visible:
            ui["console_height"] = self.console_frame.winfo_height()
        config.save_config(self.cfg)

    def action_disconnect(self):
        if not self._confirm_discard():
            return
        self._persist_ui_state()
        self._unbind_shortcuts()
        self._unbind_output_shortcuts()
        self._cancel_pending_jobs()
        if self._failover is not None:
            self._failover.close()
        try:
            self.client.on_disconnected = None
            self.client.disconnect()
        except Exception:
            pass
        try:
            if self.server:
                self.server.stop()
        except Exception:
            pass
        if self.run_proc and self.run_proc.poll() is None:
            self.action_stop()
        if self.on_leave:
            self.on_leave()

    def _confirm_discard(self):
        """Gate before anything that would throw away the current buffer
        (New, Open, Disconnect/Close). Offers Save / Don't Save / Cancel
        rather than a blunt Discard/Keep choice - "Save changes?" with
        Yes meaning save (the safe, non-destructive default) rather than
        a "Discard unsaved changes?" dialog where Yes was the destructive
        option, which is easy to click on reflex and lose work to. Also
        adds a genuine Cancel, matching how "Save before quitting?"
        already behaves.
        Returns True once it's actually safe to proceed (saved, or the
        person explicitly chose to discard); False if they cancelled, or
        chose to save but the save didn't actually go through (e.g. a
        Save As they then cancelled, or a write that failed)."""
        if not self.dirty:
            return True
        choice = messagebox.askyesnocancel(
            "Save changes?", "This buffer has unsaved changes. Save them before continuing?")
        if choice is None:
            return False
        if choice:
            self.action_save()
            return not self.dirty
        return True

    def _share_hint_for(self, path):
        """Computes what to broadcast to peers as the shared buffer's
        filename: a path relative to the working directory when the file
        is actually inside it - giving peers useful context (e.g.
        'src/main.py', the path under the top folder shown in Explorer)
        without revealing where that folder actually lives - or just the
        bare filename otherwise. Never the full local absolute path, which
        could expose personal directory structure to other peers."""
        try:
            rel = os.path.relpath(path, self.working_dir)
        except ValueError:
            rel = None
        if rel and not rel.startswith("..") and not os.path.isabs(rel):
            return rel.replace(os.sep, "/")
        return os.path.basename(path)

    def _update_file_path_label(self):
        """Shown next to the interpreter box. Only the person who actually
        opened the active file locally sees its real absolute path - for
        everyone else it shows whatever privacy-scoped hint the opener's
        client broadcast (see _share_hint_for)."""
        if self.current_file:
            text = self.current_file
        elif self.active_filename_hint:
            text = self.active_filename_hint
        elif self.text.get("1.0", "end-1c") == "":
            text = ""
        else:
            text = "(unsaved buffer)"
        self.file_path_label.configure(text=text)

    def _swap_shared_buffer(self, text, filename_hint):
        """Loads new content into the shared, collaborative buffer - this
        reaches every connected peer, not just this window. Also announces
        the new filename hint so peers' syntax highlighting follows along and
        their own (unrelated) local file association is safely cleared -
        see _on_buffer_context."""
        self._suppress_capture = True
        try:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
        finally:
            self._suppress_capture = False
        op = ot.diff_to_op(self.doc, text)
        self.doc = text
        self._reset_history()
        if not op.is_noop():
            self.client.local_edit(op)
        self.active_filename_hint = filename_hint
        self.client.send_buffer_context(filename_hint)
        self._update_language_status()
        self._update_file_path_label()
        self._redraw_linenumbers()
        self._do_highlight()

    def action_new(self):
        if not self._confirm_discard():
            return
        self.current_file = None
        self._loaded_mtime = None
        self._swap_shared_buffer("", None)
        self._update_cursor_status()

    def action_open(self):
        """Opens a file or a directory - Tkinter has no built-in picker
        that handles both, so this asks which kind first, then shows the
        matching native dialog. Opening a directory switches the working
        directory (see _switch_working_dir), the same as picking one on
        the Open tab, or dropping a folder onto Explorer."""
        choice = self._prompt_file_or_directory()
        if choice is None:
            return
        kind, path = choice
        if kind == "file":
            self._load_file(path)
        else:
            self._switch_working_dir(path)

    def _prompt_file_or_directory(self):
        t = self.theme
        dlg = tk.Toplevel(self)
        dlg.title("Open")
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        dlg.configure(bg=t["panel_bg"])
        tk.Label(dlg, text="Open a file or a directory?", bg=t["panel_bg"], fg=t["fg"],
                 padx=20, pady=16).pack()
        btn_row = tk.Frame(dlg, bg=t["panel_bg"])
        btn_row.pack(pady=(0, 16), padx=16)
        result = {}

        def pick(kind):
            result["kind"] = kind
            dlg.destroy()

        tk.Button(btn_row, text="File...", width=12, command=lambda: pick("file")).pack(side="left", padx=6)
        tk.Button(btn_row, text="Directory...", width=12, command=lambda: pick("directory")).pack(side="left", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        dlg.grab_set()
        dlg.wait_window()

        kind = result.get("kind")
        if kind is None:
            return None
        if kind == "file":
            path = filedialog.askopenfilename(initialdir=self.working_dir, title="Choose file")
        else:
            path = filedialog.askdirectory(initialdir=self.working_dir, title="Choose directory")
        if not path:
            return None
        return kind, path

    def _open_file_from_explorer(self, path):
        self._load_file(path)

    def _load_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            messagebox.showerror("Open File", str(e))
            return
        if not self._confirm_discard():
            return
        self.current_file = path
        self._loaded_mtime = self._safe_mtime(path)
        self._swap_shared_buffer(content, self._share_hint_for(path))
        self._update_cursor_status()

    def action_save(self):
        if not self.current_file:
            return self.action_save_as()
        if not self._confirm_external_change(self.current_file):
            return
        self._write_to(self.current_file)

    def action_save_as(self):
        path = filedialog.asksaveasfilename(initialdir=self.working_dir, defaultextension=".py")
        if not path:
            return
        self.current_file = path
        self._write_to(path)
        self.explorer.refresh()
        self.active_filename_hint = self._share_hint_for(path)
        self.client.send_buffer_context(self.active_filename_hint)
        self._update_language_status()
        self._update_file_path_label()
        self._do_highlight()

    def _safe_mtime(self, path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _confirm_external_change(self, path):
        """If the file on disk changed since we loaded/last saved it - e.g. a
        different local file happens to share this path, or it was edited
        outside the app - warn before silently overwriting it."""
        current = self._safe_mtime(path)
        if self._loaded_mtime is not None and current is not None and current > self._loaded_mtime + 1e-6:
            return messagebox.askyesno(
                "File changed on disk",
                f"'{os.path.basename(path)}' has changed on disk since it was loaded here "
                "(possibly by something outside this session). Overwrite it with the current buffer anyway?",
            )
        return True

    def _write_to(self, path):
        content = self.text.get("1.0", "end-1c")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            messagebox.showerror("Save", str(e))
            return
        self._saved_doc = content
        self._refresh_dirty()
        self._loaded_mtime = self._safe_mtime(path)
        self._update_cursor_status()

    def action_reveal(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(self.working_dir)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.working_dir])
            else:
                subprocess.Popen(["xdg-open", self.working_dir])
        except OSError as e:
            messagebox.showerror("Open Working Directory", str(e))

    def action_copy_invite_address(self):
        """Puts the address someone else needs to Join this mesh on the
        clipboard - advertise_host:advertise_port, the same value shown in
        the Peer to Peer tab's Address field once Start/Join found it (via
        STUN, if a public one was reachable) and is what this peer told
        the mesh to reach it at."""
        host, port = self.p2p_advertise
        addr = f"{host}:{port}"
        self.clipboard_clear()
        self.clipboard_append(addr)
        messagebox.showinfo("Invite Address", f"Copied to clipboard: {addr}")

    def _on_buffer_context(self, payload):
        self.after(0, lambda: self._handle_buffer_context(payload))

    def _handle_buffer_context(self, payload):
        filename = payload.get("filename")
        by = payload.get("by")
        self.active_filename_hint = filename
        self._update_language_status()
        self._do_highlight()
        if by == self.client.client_id:
            self._update_file_path_label()
            return
        had_local_file = self.current_file is not None
        self.current_file = None
        self._loaded_mtime = None
        self._update_file_path_label()
        who = next((n for pid, n in self._known_peer_names.items() if pid == by), "someone")
        label = filename or "a blank buffer"
        self._console_write(f"{who} loaded {label} into the shared buffer\n", "info")
        if had_local_file:
            self._console_write("  (your local file association was cleared to avoid an accidental overwrite)\n", "info")
        self._update_cursor_status()

    def _prepare_run_target(self):
        """Writes out a scratch file for an unsaved/dirty buffer if needed
        and returns (cmd, target), or None (after showing an error) if the
        scratch file couldn't be written."""
        target = self.current_file
        if target is None or self.dirty:
            target = os.path.join(self.working_dir, "._scratch_run.py")
            try:
                with open(target, "w", encoding="utf-8") as f:
                    f.write(self.text.get("1.0", "end-1c"))
            except OSError as e:
                messagebox.showerror("Run", str(e))
                return None
        cmd = self.run_cmd.get().strip() or sys.executable
        return cmd, target

    def action_run(self):
        if self.run_proc and self.run_proc.poll() is None:
            messagebox.showinfo("Run", "Something is already running in the Terminal.")
            return
        prepared = self._prepare_run_target()
        if prepared is None:
            return
        cmd, target = prepared
        self.ansi_console.state.reset()
        self._ensure_newline()
        self._console_write(f"$ {cmd} {os.path.basename(target)}\n", "info")
        self._launch_process([cmd, target])

    def _run_terminal_command(self, command):
        """Runs a line typed straight at the Terminal's own $ prompt as a
        plain system command - `ls`, `git status`, `pip install x`,
        anything, not just this project's file - the same as typing it
        into any other terminal. Goes through a shell (needed for
        pipes, globbing, and builtins like `cd`) via the exact same
        process machinery the Run button uses (_launch_process: live
        streaming, selection-aware Ctrl+C, exit reporting), rather than a
        single blocking subprocess.run() call, which would freeze this
        whole app for as long as the command runs and couldn't be
        interrupted or show output as it happens - the opposite of
        "behave like a terminal"."""
        self.ansi_console.flush()
        self.ansi_console.state.reset()
        self._launch_process(command, shell=True)

    def _launch_process(self, args, shell=False):
        """Actually starts args - either an argv list (Run button) or a
        single shell command string (shell=True, a line typed at the
        Terminal's own prompt) - as run_proc, and wires up the pump/
        watch machinery every run shares regardless of how it started."""
        # A separate process group so Ctrl+C can be sent to just the
        # child later - without this it would also land on our own
        # process. On Windows that's CREATE_NEW_PROCESS_GROUP (console
        # control events go to every process sharing the group). On
        # POSIX it's start_new_session=True (== os.setsid()): otherwise
        # the child inherits *our* foreground process group, and when
        # this app is itself running under a real terminal (a Termux
        # session behind Termux:X11, an SSH session, etc.), a Ctrl+C
        # typed into that terminal is delivered by the kernel straight
        # to every process in the group - killing the child directly,
        # no matter what has focus in our own GUI, before our own
        # selection-aware Ctrl+C handling ever gets a say.
        is_windows = sys.platform.startswith("win")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows else 0
        try:
            self.run_proc = subprocess.Popen(
                args, cwd=self.working_dir, shell=shell,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                creationflags=creationflags,
                start_new_session=(not is_windows),
            )
        except OSError as e:
            self._ensure_newline()
            self._console_write(f"Failed to launch: {e}\n", "stderr")
            self._show_prompt()
            return
        self._proc_exit_expected = False
        threading.Thread(target=self._pump_stream, args=(self.run_proc.stdout, False), daemon=True).start()
        threading.Thread(target=self._pump_stream, args=(self.run_proc.stderr, True), daemon=True).start()
        threading.Thread(target=self._watch_run_proc, args=(self.run_proc,), daemon=True).start()

    def _watch_run_proc(self, proc):
        """Waits (off the main thread - proc.wait() blocks) for this run
        to actually end, how ever it ends: normal exit, our own Stop or
        interrupt, or - the thing we actually can't see any other way -
        something outside this app killing it directly (a stray terminal
        signal delivered to the child's process/session despite the
        isolation in _launch_process, another process sending it a
        signal, etc.). Only enqueues a report for the run this thread was
        started for, so a stale watcher from a previous run can't report
        on top of a new one that's since started."""
        returncode = proc.wait()
        if proc is self.run_proc:
            self._exit_queue.put(returncode)

    def _terminal_message_enabled(self, key):
        """Settings > General > Terminal lets each of [finished], [stopped]
        and [interrupted] be turned off individually."""
        return self.cfg.get("editor", {}).get("terminal_messages", {}).get(key, True)

    def _report_proc_exit(self, returncode):
        """Describes how a run ended, in the same spot [stopped] and
        [interrupted] print. Skipped when we ourselves asked for the
        exit (action_stop/_interrupt_run_proc already said so); shown
        otherwise so an exit neither of those caused - the mysterious
        case - is at least visible instead of the console just going
        quiet with no explanation. Either way, a fresh $ prompt follows -
        every run ends back at the prompt, the same as any terminal,
        whether it was launched from the Run button or typed here."""
        if not self._proc_exit_expected:
            if returncode < 0 and not sys.platform.startswith("win"):
                # An external, unexplained kill - always reported
                # regardless of the [finished] toggle below, since it's
                # the "otherwise the console just goes quiet with no
                # explanation" case this whole method exists for.
                try:
                    sig_name = signal.Signals(-returncode).name
                except ValueError:
                    sig_name = str(-returncode)
                self._ensure_newline()
                self._console_write(f"[ended: killed by signal {-returncode} ({sig_name})]\n", "info")
            elif self._terminal_message_enabled("finished"):
                msg = "[finished]\n" if returncode == 0 else f"[finished: exit code {returncode}]\n"
                self._ensure_newline()
                self._console_write(msg, "info")
        self._show_prompt()

    def _ensure_newline(self):
        """Makes sure whatever gets written next starts on its own line -
        but without forcing a full blank line above it when the console
        is already sitting at the start of one (the common case, since
        output almost always ends with its own trailing newline already).
        Used before every one-line status message ([finished], [stopped],
        [interrupted], a $ prompt, a Run-button command echo) instead of
        each of them unconditionally prefixing "\\n", which is what
        produced a stray blank line both above *and* below [finished]:
        its own leading "\\n" made one, and the next prompt's leading
        "\\n" made a second."""
        self.ansi_console.flush()
        if self.console.index("end-1c") == "1.0":
            return
        if self.console.get("end-2c", "end-1c") != "\n":
            self._console_write("\n")

    def _show_prompt(self):
        """Shows a fresh $ prompt, ready for the next command - typing
        right after it and pressing Enter runs it, exactly like a real
        terminal sitting idle."""
        self._ensure_newline()
        self._console_write("$ ", "prompt")
        self._console_history_pos = None

    def action_stop(self):
        if self.run_proc and self.run_proc.poll() is None:
            self._proc_exit_expected = True
            self.run_proc.terminate()
            if self._terminal_message_enabled("stopped"):
                self._ensure_newline()
                self._console_write("[stopped]\n", "info")

    def _pump_stream(self, stream, is_err):
        """Reads one character at a time rather than by line: a script
        blocked on input(">> ") has already written and flushed that
        prompt, but it has no trailing newline, so readline() would sit
        waiting forever and the prompt would never reach the console.
        Reading a fixed (small) size instead returns as soon as whatever
        is currently available shows up, prompt included."""
        for ch in iter(lambda: stream.read(1), ""):
            self.out_queue.put((ch, is_err))
        stream.close()

    def _poll_output(self):
        buf = []
        cur_tag = None
        try:
            while True:
                ch, is_err = self.out_queue.get_nowait()
                tag = "stderr" if is_err else None
                if buf and tag != cur_tag:
                    self._console_write("".join(buf), cur_tag)
                    buf = []
                cur_tag = tag
                buf.append(ch)
        except queue.Empty:
            pass
        if buf:
            self._console_write("".join(buf), cur_tag)
        try:
            while True:
                returncode = self._exit_queue.get_nowait()
                self._report_proc_exit(returncode)
        except queue.Empty:
            pass
        self._poll_job = self.after(80, self._poll_output)

    def _on_console_resize(self, _event=None):
        if self._console_resize_job:
            try:
                self.after_cancel(self._console_resize_job)
            except tk.TclError:
                pass
        self._console_resize_job = self.after(120, self._settle_console_scroll)

    def _settle_console_scroll(self):
        self._console_resize_job = None
        try:
            if self.console.get("1.0", "end-1c"):
                self.console.see("end")
        except tk.TclError:
            pass

    def _console_write(self, text, tag=None):
        self.ansi_console.write(text, tag)
        self.console.see("end")
        # Everything just written becomes settled history - the live,
        # editable tail (where a typed reply, or a command at the $
        # prompt, lives) always starts fresh right after it. See
        # _on_console_key.
        #
        # "end-1c", not "end": a Tk Text widget always has an implicit
        # trailing newline past the last real character, so plain "end"
        # points one (phantom, empty) line further than the text actually
        # written - using it here would leave input_start permanently one
        # line ahead of the content, silently sending an empty line to
        # the running process's stdin no matter what was typed.
        self.console.mark_set("input_start", "end-1c")

    def _on_console_key(self, event):
        """The Terminal console is read-only history except for the tail
        after the "input_start" mark - the live, editable line, which is
        either a reply typed for a running process's stdin (input(">> "),
        etc.) or, when nothing is running, a command about to be run
        itself, right after the $ prompt - same as any real terminal.
        Either way that tail is the only editable part; everything
        before input_start stays locked, and Enter always submits it
        (_submit_console_input decides which of those two it is)."""
        if event.keysym == "Return":
            self._submit_console_input()
            return "break"
        if event.keysym == "BackSpace":
            if self.console.compare("insert", "<=", "input_start"):
                return "break"
            return None
        if event.keysym == "Delete":
            if self.console.compare("insert", "<", "input_start"):
                return "break"
            return None
        if event.char and event.char.isprintable():
            if self.console.compare("insert", "<", "input_start"):
                self.console.mark_set("insert", "end")
            return None
        return None

    def _on_console_paste(self, event):
        if self.console.compare("insert", "<", "input_start"):
            self.console.mark_set("insert", "end")
        return None

    def _on_console_tab(self, event):
        """Tab, in the Terminal console: completes the path fragment
        under the cursor against real files/directories, the same as
        any Linux shell's filename completion - one match completes it
        (with a trailing / for a directory, so completion can continue
        into it); several matches complete as far as their common
        prefix goes, or list every candidate (like a second Tab in bash)
        once there's nothing left to add automatically. Only applies
        while idle at the $ prompt - not while a script is running and
        this tail is a reply being typed for its stdin instead."""
        if self.run_proc and self.run_proc.poll() is None:
            return None
        cursor = self.console.index("insert")
        if self.console.compare(cursor, "<", "input_start"):
            return None
        line = self.console.get("input_start", cursor)
        word = re.search(r"\S*$", line).group()
        quote = ""
        if word[:1] in ("'", '"'):
            quote, word = word[0], word[1:]
        dir_part, _, partial = word.rpartition("/")
        lookup_dir = os.path.expanduser(dir_part) if dir_part else "."
        if not os.path.isabs(lookup_dir):
            lookup_dir = os.path.join(self.working_dir, lookup_dir)
        try:
            names = os.listdir(lookup_dir)
        except OSError:
            return "break"
        if partial:
            candidates = sorted(n for n in names if n.startswith(partial))
        else:
            candidates = sorted(n for n in names if not n.startswith("."))
        if not candidates:
            return "break"

        def displayed(name):
            return name + "/" if os.path.isdir(os.path.join(lookup_dir, name)) else name

        if len(candidates) == 1:
            completed = displayed(candidates[0])
            suffix = "" if completed.endswith("/") else " "
        else:
            common = os.path.commonprefix(candidates)
            if len(common) > len(partial):
                completed, suffix = common, ""
            else:
                # Nothing left to auto-complete with several candidates
                # still matching - list them, the same as a second Tab
                # press would in bash, then restore the prompt with
                # whatever was already typed so typing can continue.
                self._ensure_newline()
                self._console_write("  ".join(displayed(n) for n in candidates) + "\n", "info")
                self._show_prompt()
                self.console.insert("end", line)
                self.console.mark_set("insert", "end")
                return "break"
        replaced = quote + word[:len(word) - len(partial)] + completed
        self.console.delete(f"{cursor}-{len(word) + len(quote)}c", cursor)
        self.console.insert("insert", replaced + suffix)
        return "break"

    def _on_console_history(self, event):
        """Up/Down at the console's own $ prompt: walks backward/forward
        through commands previously run there, the same as a shell's
        readline history. Scoped the same way Tab-completion above is -
        only while idle at the $ prompt (a running process doesn't have
        a command history to walk, just whatever reply is being typed
        for its stdin) and only while the cursor is actually on that
        live tail, so arrowing up through old *output* to look at it, or
        to select/copy something, still just moves the cursor as usual
        instead of being hijacked."""
        if self.run_proc and self.run_proc.poll() is None:
            return None
        cursor = self.console.index("insert")
        if self.console.compare(cursor, "<", "input_start"):
            return None
        if not self._console_history:
            return "break"
        if event.keysym == "Up":
            if self._console_history_pos is None:
                self._console_history_stash = self.console.get("input_start", "end-1c")
                self._console_history_pos = len(self._console_history)
            if self._console_history_pos == 0:
                return "break"
            self._console_history_pos -= 1
            self._set_console_input(self._console_history[self._console_history_pos])
        else:
            if self._console_history_pos is None:
                return "break"
            self._console_history_pos += 1
            if self._console_history_pos >= len(self._console_history):
                self._console_history_pos = None
                self._set_console_input(self._console_history_stash)
            else:
                self._set_console_input(self._console_history[self._console_history_pos])
        return "break"

    def _set_console_input(self, text):
        """Replaces the console's live input tail (see _on_console_key)
        with `text`, used by history navigation to swap in a past
        command without touching the read-only output above it."""
        self.console.delete("input_start", "end-1c")
        self.console.insert("input_start", text)
        self.console.mark_set("insert", "end")
        self.console.see("end")

    def _submit_console_input(self):
        """Enter, in the Terminal console: sends the typed tail (since
        "input_start") to the running process's stdin if something's
        running - for scripts that call input(), prompt for a password,
        etc. - or, when idle, runs it as a system command instead, right
        at the console's own $ prompt, same as any real terminal."""
        running = bool(self.run_proc and self.run_proc.poll() is None)
        text = self.console.get("input_start", "end-1c")
        self.console.insert("end", "\n")
        self.console.mark_set("input_start", "end-1c")
        self.console.see("end")
        if running:
            if not self.run_proc.stdin:
                return
            try:
                self.run_proc.stdin.write(text + "\n")
                self.run_proc.stdin.flush()
            except (OSError, ValueError):
                return
            return
        command = text.strip()
        if not command:
            self._show_prompt()
            return
        self._console_history_pos = None
        if not self._console_history or self._console_history[-1] != command:
            self._console_history.append(command)
        self._run_terminal_command(command)

    @contextlib.contextmanager
    def _single_undo_step(self):
        with self.history.batch():
            yield

    def _on_text_paste(self, _event=None):
        text = self.text
        try:
            clip = text.clipboard_get()
        except tk.TclError:
            return "break"
        if not clip:
            return "break"
        with self._single_undo_step():
            if text.tag_ranges("sel"):
                start = text.index("sel.first")
                text.delete("sel.first", "sel.last")
                text.mark_set("insert", start)
            text.insert("insert", clip)
        text.see("insert")
        self._schedule_highlight()
        return "break"

    def action_select_all(self):
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def action_find(self):
        FindDialog(self, replace=False)

    def action_replace(self):
        FindDialog(self, replace=True)

    def action_toggle_comment(self):
        try:
            start = int(self.text.index("sel.first").split(".")[0])
            end = int(self.text.index("sel.last").split(".")[0])
        except tk.TclError:
            start = end = int(self.text.index("insert").split(".")[0])
        lines = [self.text.get(f"{n}.0", f"{n}.end") for n in range(start, end + 1)]
        should_comment = any(l.strip() and not l.lstrip().startswith("#") for l in lines)
        with self._single_undo_step():
            for n in range(start, end + 1):
                line = self.text.get(f"{n}.0", f"{n}.end")
                if should_comment:
                    if line.strip():
                        indent = len(line) - len(line.lstrip(" "))
                        self.text.insert(f"{n}.{indent}", "# ")
                else:
                    stripped = line.lstrip(" ")
                    if stripped.startswith("# "):
                        idx = line.index("# ")
                        self.text.delete(f"{n}.{idx}", f"{n}.{idx + 2}")
                    elif stripped.startswith("#"):
                        idx = line.index("#")
                        self.text.delete(f"{n}.{idx}", f"{n}.{idx + 1}")
        return "break"

    def action_duplicate_line(self):
        line_no = int(self.text.index("insert").split(".")[0])
        content = self.text.get(f"{line_no}.0", f"{line_no}.end")
        self.text.insert(f"{line_no}.end", "\n" + content)
        return "break"

    def action_delete_line(self):
        line_no = int(self.text.index("insert").split(".")[0])
        last = int(self.text.index("end-1c").split(".")[0])
        if line_no < last:
            self.text.delete(f"{line_no}.0", f"{line_no + 1}.0")
        else:
            self.text.delete(f"{line_no}.0", f"{line_no}.end")
        return "break"

    def action_move_line(self, direction):
        line_no = int(self.text.index("insert").split(".")[0])
        last = int(self.text.index("end-1c").split(".")[0])
        target = line_no + direction
        if target < 1 or target > last:
            return "break"
        col = int(self.text.index("insert").split(".")[1])
        a = self.text.get(f"{line_no}.0", f"{line_no}.end")
        b = self.text.get(f"{target}.0", f"{target}.end")
        lo, hi = min(line_no, target), max(line_no, target)
        new_pair = (a, b) if direction < 0 else (b, a)
        with self._single_undo_step():
            self.text.delete(f"{lo}.0", f"{hi}.end")
            self.text.insert(f"{lo}.0", new_pair[0] + "\n" + new_pair[1])
        self.text.mark_set("insert", f"{target}.{col}")
        self.text.see("insert")
        return "break"

    def _selection_line_range(self):
        """Returns (start_line, end_line) ints for the current selection,
        or None if there isn't one. Checked two ways on purpose: most Tk
        builds raise TclError from text.index("sel.first") when nothing
        is selected, but at least one build out there (seen via Termux's
        Python 3.13) just returns an empty string instead - int('') then
        blows up with an uncaught ValueError. Handle both so indent/
        dedent never crash just because nothing's selected."""
        try:
            first = self.text.index("sel.first")
            last = self.text.index("sel.last")
        except tk.TclError:
            return None
        if not first or not last:
            return None
        return int(first.split(".")[0]), int(last.split(".")[0])

    def action_indent(self, event=None):
        sel = self._selection_line_range()
        if sel is None:
            self.text.insert("insert", "    ")
            return "break"
        start, end = sel
        with self._single_undo_step():
            for n in range(start, end + 1):
                if self.text.get(f"{n}.0", f"{n}.end").strip():
                    self.text.insert(f"{n}.0", "    ")
        return "break"

    def action_dedent(self, event=None):
        sel = self._selection_line_range()
        if sel is None:
            start = end = int(self.text.index("insert").split(".")[0])
        else:
            start, end = sel
        with self._single_undo_step():
            for n in range(start, end + 1):
                line = self.text.get(f"{n}.0", f"{n}.end")
                cut = len(line) - len(line.lstrip(" "))
                cut = min(cut, 4)
                if cut:
                    self.text.delete(f"{n}.0", f"{n}.{cut}")
        return "break"

    def _cancel_pending_jobs(self):
        """Cancels every self-rescheduling after() job. Without this, a
        job like _poll_output (which reschedules itself every 80ms
        indefinitely) keeps firing after the editor is torn down - back to
        Connect, or on quit - and since the underlying widgets are gone by
        then, Tk logs an "invalid command name" error to the console for
        every tick until the process exits."""
        for attr in ("_poll_job", "_cursor_send_job", "_highlight_job", "_console_resize_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def _unbind_shortcuts(self):
        """Releases every shortcut this editor bound onto the shared root
        window (and the text widget). Without the root half of this,
        closing the editor (back to Connect, or on quit) would leave those
        root-level bindings dangling with closures over a now-destroyed
        widget - and since the root window is reused for the next screen,
        pressing e.g. Ctrl+S there would still try to invoke a method on
        the dead editor instance."""
        root = self.winfo_toplevel()
        for accel in self._bound_accels:
            try:
                root.unbind_all(accel)
            except tk.TclError:
                pass
            try:
                self.text.unbind(accel)
            except tk.TclError:
                pass
        self._bound_accels = []

    def _is_foreign_text_input(self, widget):
        if widget is self.text:
            return False
        try:
            return widget.winfo_class() in TEXT_INPUT_CLASSES
        except (AttributeError, tk.TclError):
            return False

    def _select_all_in(self, widget):
        try:
            if widget.winfo_class() == "Text":
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "end-1c")
            else:
                widget.selection_range(0, "end")
                widget.icursor("end")
        except tk.TclError:
            pass
        return "break"

    def _scoped_shortcut(self, action_id, fn):
        def handler(event):
            widget = getattr(event, "widget", None)
            if self._is_foreign_text_input(widget):
                if action_id == "select_all":
                    return self._select_all_in(widget)
                return None
            return fn(event)
        return handler

    def _apply_shortcuts(self):
        root = self.winfo_toplevel()
        self._unbind_shortcuts()

        for action_id, _label, default_accel, method_name in SHORTCUT_SPECS:
            accel = self.shortcuts.get(action_id) or default_accel
            if not accel:
                continue
            if action_id == "move_line_up":
                fn = lambda e: self.action_move_line(-1)
            elif action_id == "move_line_down":
                fn = lambda e: self.action_move_line(1)
            else:
                method = getattr(self, method_name)
                fn = (lambda e, m=method: (m(), "break")[1])
            global_fn = self._scoped_shortcut(action_id, fn) if action_id in EDITOR_SCOPED_ACTIONS else fn
            try:
                self.text.bind(accel, fn)
                root.bind_all(accel, global_fn)
                self._bound_accels.append(accel)
            except tk.TclError:
                pass

        self.text.bind("<Tab>", self.action_indent)
        self.text.bind("<Shift-Tab>", self.action_dedent)
        self.text.bind("<Configure>", self._redraw_linenumbers)

    def _unbind_output_shortcuts(self):
        """Releases shortcuts bound onto the Terminal console widget itself
        (see _apply_output_shortcuts) - mirrors _unbind_shortcuts, just
        scoped to that one widget instead of the whole window."""
        for accel in self._bound_console_accels:
            try:
                self.console.unbind(accel)
            except tk.TclError:
                pass
        self._bound_console_accels = []

    def _apply_output_shortcuts(self):
        """Binds OUTPUT_SHORTCUT_SPECS onto the Terminal console widget only
        - not root, not the editor's text widget - so these terminal-like
        conveniences (Ctrl+C to interrupt whatever's running, etc.) only
        fire while the console itself has focus, the same way a real
        terminal's shortcuts don't leak into whatever else is open."""
        self._unbind_output_shortcuts()
        for action_id, _label, default_accel, method_name in OUTPUT_SHORTCUT_SPECS:
            accel = self.output_shortcuts.get(action_id) or default_accel
            if not accel:
                continue
            method = getattr(self, method_name)
            fn = (lambda e, m=method: "break" if m() else None)
            try:
                self.console.bind(accel, fn)
                self._bound_console_accels.append(accel)
            except tk.TclError:
                pass

    def _console_send_interrupt(self):
        """Ctrl+C, by default: while a script is running, sends it the
        same interrupt a terminal's Ctrl+C would (SIGINT on POSIX,
        CTRL_C_EVENT on Windows) instead of the usual copy-to-clipboard.
        Returns True if it handled the key (caller should suppress the
        default binding), False to let Ctrl+C fall through to copying a
        selection as normal.

        This mirrors the classic PTY convention every real terminal
        emulator follows: Ctrl+C only interrupts when there's nothing
        selected. If the user has highlighted output (to copy it), Ctrl+C
        copies that selection instead - it does not also kill the running
        script. So: no selection - interrupt; a selection present - copy,
        even if something is running."""
        if self.console.tag_ranges("sel"):
            return False
        if not (self.run_proc and self.run_proc.poll() is None):
            return False
        self._interrupt_run_proc()
        return True

    def _interrupt_run_proc(self):
        """Actually delivers the interrupt to run_proc - factored out of
        _console_send_interrupt so app.py's terminal-level Ctrl+C handler
        (a real OS SIGINT, for when this whole app is itself running
        under a terminal) can reuse the exact same delivery instead of
        quitting the whole app while a script is running, same as a
        shell forwards Ctrl+C to its foreground job rather than dying
        itself. Signals the child's own process group (see
        start_new_session in _launch_process), not just the one process, so
        it reaches anything that child itself spawned too. Guarded by
        _proc_exit_expected so a second, redundant trigger for the same
        Ctrl+C - e.g. both the console's own key binding and app.py's
        terminal-level SIGINT forwarder firing for one keypress, which
        does happen in some environments - can't send the signal or
        print "[interrupted]" a second time."""
        if self._proc_exit_expected:
            return
        self._proc_exit_expected = True
        try:
            if sys.platform.startswith("win"):
                self.run_proc.send_signal(signal.CTRL_C_EVENT)
            else:
                os.killpg(os.getpgid(self.run_proc.pid), signal.SIGINT)
        except (OSError, ValueError, ProcessLookupError):
            try:
                self.run_proc.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                return
        if self._terminal_message_enabled("interrupted"):
            self._ensure_newline()
            self._console_write("[interrupted]\n", "info")


class FindDialog(tk.Toplevel):
    """Find/Replace with the basics IDLE-style editors offer: search in
    either direction, whole-word and case-sensitive matching, and optional
    wraparound, on top of the original find-next/replace/replace-all."""

    # Class-level, not instance-level, on purpose: this is what makes the
    # find/replace text survive after the dialog is closed and reopened,
    # for the lifetime of the app.
    _last_find = ""
    _last_replace = ""

    def __init__(self, app: EditorApp, replace: bool):
        super().__init__(app)
        self.app = app
        t = app.theme
        self.title("Replace" if replace else "Find")
        self.configure(bg=t["panel_bg"])
        self.resizable(False, False)
        self.transient(app.winfo_toplevel())

        tk.Label(self, text="Find:", bg=t["panel_bg"], fg=t["fg"]).grid(row=0, column=0, padx=8, pady=6, sticky="e")
        self.find_var = tk.StringVar()
        find_entry = tk.Entry(self, textvariable=self.find_var, width=32, bg=t["edit_bg"], fg=t["fg"],
                               insertbackground=t["fg"], relief="flat")
        find_entry.grid(row=0, column=1, padx=8, pady=6, columnspan=3, sticky="we")

        self.replace_var = tk.StringVar()
        if replace:
            tk.Label(self, text="Replace:", bg=t["panel_bg"], fg=t["fg"]).grid(row=1, column=0, padx=8, pady=6, sticky="e")
            tk.Entry(self, textvariable=self.replace_var, width=32, bg=t["edit_bg"], fg=t["fg"],
                     insertbackground=t["fg"], relief="flat").grid(
                row=1, column=1, padx=8, pady=6, columnspan=3, sticky="we")

        self.match_case_var = tk.BooleanVar(value=False)
        self.whole_word_var = tk.BooleanVar(value=False)
        self.wrap_var = tk.BooleanVar(value=True)
        opts = tk.Frame(self, bg=t["panel_bg"])
        opts.grid(row=2, column=0, columnspan=4, padx=8, pady=(0, 4), sticky="w")
        for label, var in (("Match case", self.match_case_var),
                            ("Whole word", self.whole_word_var),
                            ("Wrap around", self.wrap_var)):
            tk.Checkbutton(opts, text=label, variable=var, bg=t["panel_bg"], fg=t["fg"],
                           selectcolor=t["panel_bg"], activebackground=t["panel_bg"],
                           activeforeground=t["fg"], highlightthickness=0).pack(side="left", padx=(0, 10))

        btns = tk.Frame(self, bg=t["panel_bg"])
        btns.grid(row=3, column=0, columnspan=4, pady=(4, 8))
        tk.Button(btns, text="Find Previous", command=self.find_previous).pack(side="left", padx=4)
        tk.Button(btns, text="Find Next", command=self.find_next).pack(side="left", padx=4)
        if replace:
            tk.Button(btns, text="Replace", command=self.replace_one).pack(side="left", padx=4)
            tk.Button(btns, text="Replace All", command=self.replace_all).pack(side="left", padx=4)

        self.status_var = tk.StringVar()
        tk.Label(self, textvariable=self.status_var, bg=t["panel_bg"], fg=t["muted_fg"]).grid(
            row=4, column=0, columnspan=4, padx=8, pady=(0, 6), sticky="w")

        self.bind("<Return>", lambda e: self.find_next())
        self.bind("<Shift-Return>", lambda e: self.find_previous())
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Destroy>", self._on_destroy)
        self.replace_var.set(FindDialog._last_replace)
        selected = self._selected_or_empty()
        self.find_var.set(selected or FindDialog._last_find)
        find_entry.select_range(0, "end")

        def _focus():
            self.focus_force()
            find_entry.focus_set()
        self.after(50, _focus)

    def _on_destroy(self, event):
        # <Destroy> bubbles up from every child as the window closes, not
        # just this Toplevel itself - and the window manager's own close
        # button destroys us at the Tcl level directly, bypassing a
        # Python-level override of destroy() entirely, so this virtual
        # event (rather than overriding destroy()) is the one place that
        # reliably catches every way this dialog can go away.
        if event.widget is self:
            FindDialog._last_find = self.find_var.get()
            FindDialog._last_replace = self.replace_var.get()

    def _selected_or_empty(self):
        try:
            return self.app.text.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def _pattern(self, needle):
        """Returns (pattern, is_regexp). Whole-word matching is implemented
        as a regexp search with Tcl's \\y word-boundary marker around the
        needle, with the needle itself escaped first so regex-special
        characters the user typed are still matched literally."""
        if self.whole_word_var.get():
            return r"\y%s\y" % re.escape(needle), True
        return needle, False

    def _do_search(self, needle, start, stop, backwards):
        text = self.app.text
        pattern, is_regexp = self._pattern(needle)
        count_var = tk.IntVar()
        pos = text.search(pattern, start, stopindex=stop,
                           forwards=not backwards, backwards=backwards,
                           nocase=not self.match_case_var.get(),
                           regexp=is_regexp, count=count_var)
        if not pos:
            return None, None
        length = count_var.get() if is_regexp else len(needle)
        return pos, length

    def _find(self, backwards):
        text = self.app.text
        needle = self.find_var.get()
        if not needle:
            return
        anchor = "insert-1c" if backwards else "insert+1c"
        if backwards:
            start, stop = anchor, "1.0"
        else:
            start, stop = anchor, "end"
        pos, length = self._do_search(needle, start, stop, backwards)
        if pos is None and self.wrap_var.get():
            wrap_start = "end" if backwards else "1.0"
            pos, length = self._do_search(needle, wrap_start, None, backwards)
        if pos is None:
            self.status_var.set(f"'{needle}' not found.")
            return
        end = f"{pos}+{length}c"
        # A real selection, not a separate highlight tag of our own: this
        # is what makes a match behave exactly like any other selection
        # once found - it's still there after this dialog is closed, and
        # goes away the normal way (clicking elsewhere in the editor),
        # instead of a custom tag that nothing was ever clearing.
        text.tag_remove("sel", "1.0", "end")
        text.tag_add("sel", pos, end)
        text.mark_set("insert", pos if backwards else end)
        text.see(pos)
        self.status_var.set("")

    def find_next(self):
        self._find(backwards=False)

    def find_previous(self):
        self._find(backwards=True)

    def replace_one(self):
        text = self.app.text
        try:
            if text.tag_ranges("sel"):
                a, b = text.tag_ranges("sel")[:2]
                with self.app._single_undo_step():
                    text.delete(a, b)
                    text.insert(a, self.replace_var.get())
                text.mark_set("insert", a)
        finally:
            self.find_next()

    def replace_all(self):
        text = self.app.text
        needle = self.find_var.get()
        repl = self.replace_var.get()
        if not needle:
            return
        pos = "1.0"
        count = 0
        with self.app._single_undo_step():
            while True:
                pos, length = self._do_search(needle, pos, "end", backwards=False)
                if pos is None:
                    break
                end = f"{pos}+{length}c"
                text.delete(pos, end)
                text.insert(pos, repl)
                pos = f"{pos}+{len(repl)}c"
                count += 1
        self.status_var.set(f"Replaced {count} occurrence(s).")
        messagebox.showinfo("Replace All", f"Replaced {count} occurrence(s).")

import collections
import contextlib
import itertools
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
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
from tab_bar import TabBar
from settings_window import SettingsWindow
from net.client import FailoverController

# The Terminal console's stdout/stderr/prompt colors, tuned as two fixed
# pairs rather than one - the "dark" set is bright, saturated color meant
# to pop against a near-black console background, and reads as barely
# more than a smudge against a light one (and vice versa for "light"'s
# deeper, more saturated tones on a near-black background). Which pair
# applies is decided at paint time from the *current* console background's
# brightness (see _console_tag_colors below), the same test the "auto"
# syntax palette uses - so it keeps working for a user's own saved
# preset, not just the two built-in Dark/Light themes.
CONSOLE_TAG_COLORS_DARK = {"stderr": "#e06c75", "info": "#61afef", "prompt": "#98c379"}
CONSOLE_TAG_COLORS_LIGHT = {"stderr": "#cf222e", "info": "#0969da", "prompt": "#1a7f37"}

# How many spaces one indent level is - Tab, auto-indent-on-Enter, and the
# backspace-removes-a-whole-level behavior all agree on this single width
# rather than each hardcoding their own "    ".
INDENT_WIDTH = 4
# A line ending in one of these (ignoring trailing whitespace) gets one
# extra indent level on the line Enter creates - covers the common
# "opens a block" punctuation across most of this editor's supported
# languages (Python's ':', C-family/JS/etc.'s '{', and an open '(' or '['
# left dangling at line end) without needing per-language grammar.
_INDENT_AFTER_SUFFIXES = (":", "{", "(", "[")
_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSE_TO_OPEN = {v: k for k, v in _OPEN_TO_CLOSE.items()}
# Python has no closing bracket to hang a dedent off of for its
# colon-based blocks, so the electric-dedent-on-':' behavior keys off
# these instead - a bare 'else'/'elif'/'except'/'finally' clause dedents
# to line up with the statement it belongs to.
_PY_DEDENT_KEYWORDS = re.compile(r"^(else|elif|except|finally)\b")
# What a dedented else/elif/except/finally is allowed to land next to at
# the same indent level - a sibling clause of the block it's continuing,
# not just any statement that happens to be shallower.
_PY_BLOCK_OPENERS = re.compile(r"^(if|elif|else|for|while|try|except|finally|with)\b")
# A line whose statement is one of these unconditionally ends control
# flow at that point - nothing after it in the same block can still run
# - so the line Enter creates lands one level shallower instead of
# carrying the same indent forward, the same way it would if you'd
# dedented by hand to start the next statement in the enclosing block.
_PY_FLOW_DEDENT_KEYWORDS = re.compile(r"^(return|break|continue|pass|raise)\b")


def _console_tag_colors(console_bg):
    return CONSOLE_TAG_COLORS_LIGHT if syntax.brightness(console_bg) >= 0.5 else CONSOLE_TAG_COLORS_DARK


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
    ("close_tab", "Close Tab", "<Control-w>", "action_close_active_tab"),
    ("next_tab", "Next Tab", "<Control-Next>", "action_next_tab"),
    ("prev_tab", "Previous Tab", "<Control-Prior>", "action_prev_tab"),
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
_KEY_DISPLAY = {"slash": "/", "bracketright": "]", "bracketleft": "[", "Next": "PgDn", "Prior": "PgUp",
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


def _active_tab_attr(name):
    """A property that reads/writes `name` on EditorApp's active BufferTab."""
    return property(lambda self: getattr(self._active, name),
                    lambda self, value: setattr(self._active, name, value))


class BufferTab:
    """Everything that belongs to one open buffer.

    Exactly one tab at a time is the *shared* tab: it mirrors the
    collaborative document every peer edits, and only its edits are sent
    over the network. All other tabs are local - plain single-user buffers
    that peers never see. EditorApp shows one tab at a time in its single
    Text widget and keeps the others' state here, so the rest of the
    editor (undo, run, save, ...) only ever deals with "the active tab".
    """

    _ids = itertools.count(1)

    def __init__(self, shared=False, doc=""):
        self.id = next(BufferTab._ids)
        self.shared = shared
        # Only the shared tab can be hidden (closing it just stops
        # displaying it - the session's document has to keep being tracked).
        self.visible = True
        self.doc = doc
        self.saved_doc = doc
        self.history = undo_history.UndoHistory()
        self.current_file = None
        self.filename_hint = None
        self.loaded_mtime = None
        # True once *this user* has typed into the tab - as opposed to
        # content that merely arrived from a peer. Decides whether the tab
        # is worth keeping as a local tab when a peer replaces the shared
        # buffer (see EditorApp._worth_keeping).
        self.local_edited = False
        self.untitled_no = None
        # Restored when the tab is switched back to.
        self.caret = 0
        self.yview = 0.0

    @property
    def dirty(self):
        return self.doc != self.saved_doc


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

        # Open buffers. `_active` is the one shown in the Text widget; the
        # per-buffer state below (doc, history, current_file, ...) is exposed
        # as properties that read/write the active tab, so everything that
        # works on "the current buffer" keeps working unchanged.
        first_tab = BufferTab(shared=True)
        self._tabs = [first_tab]
        self._active = first_tab
        self.shared_tab = first_tab
        self._untitled_counter = itertools.count(1)
        self._author_names = {}
        self._dirty = False
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
        syntax.sync_saved_palettes(self.cfg.get("syntax_palette_presets", {}))
        syntax.set_color_theme(editor_cfg.get("syntax_theme", syntax.DEFAULT_COLOR_THEME))
        self._word_wrap = bool(editor_cfg.get("word_wrap", config.DEFAULTS["editor"]["word_wrap"]))
        self._run_commands = editor_cfg.setdefault("run_commands", {})
        self._suppress_run_cmd_trace = False

        self._failover = None
        self.p2p_advertise = p2p_advertise if self.mode == "p2p" else None
        if self.mode == "p2p" and p2p_advertise:
            host, port = p2p_advertise
            self._failover = FailoverController(
                username=self.username, advertise_host=host, advertise_port=port,
                get_doc_text=lambda: self.shared_tab.doc,
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
        self._refresh_tabs()

    doc = _active_tab_attr("doc")
    history = _active_tab_attr("history")
    _saved_doc = _active_tab_attr("saved_doc")
    current_file = _active_tab_attr("current_file")
    active_filename_hint = _active_tab_attr("filename_hint")
    _loaded_mtime = _active_tab_attr("loaded_mtime")

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
        t = self.theme
        a = self._accel

        # On Windows, a menu bar attached the normal way (root.config(menu=))
        # is drawn by the OS itself, using the system's own menu-bar theme -
        # tk.Menu's bg/fg options get mostly ignored, and what's left behind
        # is a thin light-colored border/strip along the bar that no Tk
        # option can touch, however dark the rest of the window is. Building
        # the bar ourselves instead - a plain Frame holding a Menubutton per
        # top-level entry, each posting its dropdown like any other popup
        # menu - keeps every pixel of it, border included, a normal themed
        # widget. macOS's menu bar lives outside the window entirely (so
        # this bug doesn't occur there, and replacing it would also lose
        # the native Mac menu bar for no benefit), and Linux/X11 already
        # draws tk.Menu itself and honors these colors - see _theme_menus -
        # so both keep the native menu bar as before.
        self._use_custom_menubar = sys.platform.startswith("win")

        old_bar = getattr(self, "_custom_menubar", None)
        if old_bar is not None:
            old_bar.destroy()
            self._custom_menubar = None

        self._menus = []
        self._menu_buttons = []

        if self._use_custom_menubar:
            root.config(menu="")
            bar = tk.Frame(self, bg=t["bg"], bd=0, highlightthickness=0)
            # On a rebuild (e.g. after changing shortcuts) the toolbar,
            # status bar and body are already packed. A plain pack() would
            # append the bar to the END of the pack order, after the body
            # has claimed all the space, so it would collapse to nothing.
            # Insert it ahead of whatever is already packed instead.
            existing = self.pack_slaves()
            if existing:
                bar.pack(side="top", fill="x", before=existing[0])
            else:
                bar.pack(side="top", fill="x")
            self._custom_menubar = bar

            def add_menu(label):
                mb = tk.Menubutton(bar, text=label, bg=t["bg"], fg=t["fg"],
                                    activebackground=t["sel_bg"], activeforeground=t["fg"],
                                    bd=0, highlightthickness=0, padx=10, pady=4)
                mb.pack(side="left")
                menu = tk.Menu(mb, tearoff=0)
                mb.configure(menu=menu)
                self._menu_buttons.append(mb)
                self._menus.append(menu)
                return menu
        else:
            self._custom_menubar = None
            menubar = tk.Menu(root)
            root.config(menu=menubar)
            # Kept so a later live theme switch can repaint every menu
            # without rebuilding them (see _theme_menus) - tk.Menu isn't a
            # ttk widget, so it doesn't pick up style changes automatically,
            # and unlike a combobox's popdown it also doesn't read the
            # option database fresh each time it's posted, so it has to be
            # told directly.
            self._menus.append(menubar)

            def add_menu(label):
                menu = tk.Menu(menubar, tearoff=0)
                menubar.add_cascade(label=label, menu=menu)
                self._menus.append(menu)
                return menu

        m_file = add_menu("File")
        m_file.add_command(label="New", accelerator=a("new_file"), command=self.action_new)
        m_file.add_command(label="Open", accelerator=a("open_file"), command=self.action_open)
        m_file.add_command(label="Save", accelerator=a("save_file"), command=self.action_save)
        m_file.add_command(label="Save As", accelerator=a("save_as"), command=self.action_save_as)
        m_file.add_separator()
        m_file.add_command(label="Close Tab", accelerator=a("close_tab"), command=self.action_close_active_tab)
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

        m_edit = add_menu("Edit")
        m_edit.add_command(label="Undo", accelerator="Ctrl+Z", command=self.action_undo)
        m_edit.add_command(label="Redo", accelerator="Ctrl+Y", command=self.action_redo)
        if self.mode != "solo":
            m_edit.add_command(label="Undo Others' Last Change", accelerator=a("undo_peer"), command=self.action_undo_peer)
            self._peer_undo_menu = tk.Menu(m_edit, tearoff=0, postcommand=self._fill_peer_undo_menu)
            m_edit.add_cascade(label="Undo Change By", menu=self._peer_undo_menu)
            self._menus.append(self._peer_undo_menu)
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

        m_run = add_menu("Run")
        m_run.add_command(label="Run Script", accelerator=a("run"), command=self.action_run)
        m_run.add_command(label="Stop", accelerator=a("stop"), command=self.action_stop)

        m_view = add_menu("View")
        m_view.add_command(label="Toggle Explorer", accelerator=a("toggle_explorer"), command=self.toggle_explorer)
        m_view.add_command(label="Toggle Terminal", accelerator=a("toggle_output"), command=self.toggle_console)
        m_view.add_separator()
        m_view.add_command(label="Next Tab", accelerator=a("next_tab"), command=self.action_next_tab)
        m_view.add_command(label="Previous Tab", accelerator=a("prev_tab"), command=self.action_prev_tab)
        if self.mode != "solo":
            m_view.add_command(label="Show Shared Buffer", command=self.action_show_shared)

        self.menubar = self._custom_menubar if self._use_custom_menubar else menubar
        self._theme_menus(t)

    def _theme_menus(self, t):
        """Repaints every tk.Menu this app owns (the menu bar and each of
        its dropdowns, including the dynamically-filled "Undo Change By"
        submenu) to match `t`. tk.Menu is a plain Tk widget, not a ttk
        one, so restyling "TMenubutton"/etc. never reaches it, and unlike
        the option-database-driven widgets in theme.apply_classic_widget_
        defaults, it doesn't reread anything on its own either - it has
        to be told directly, every time. See _build_menu for why Windows
        instead gets a custom widget-based bar here (self._menu_buttons)
        rather than relying on this to reach the native one: Linux/X11
        draws tk.Menu itself and honors these colors, and macOS draws its
        menu bar outside the window and mostly ignores them anyway."""
        for menu in getattr(self, "_menus", ()):
            try:
                menu.configure(bg=t["bg"], fg=t["fg"], activebackground=t["sel_bg"],
                                activeforeground=t["fg"], disabledforeground=t["muted_fg"])
            except tk.TclError:
                pass
        bar = getattr(self, "_custom_menubar", None)
        if bar is not None:
            try:
                bar.configure(bg=t["bg"])
            except tk.TclError:
                pass
            for mb in getattr(self, "_menu_buttons", ()):
                try:
                    mb.configure(bg=t["bg"], fg=t["fg"], activebackground=t["sel_bg"], activeforeground=t["fg"])
                except tk.TclError:
                    pass

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
            if "explorer_sort_key" in ui_cfg or "explorer_sort_reverse" in ui_cfg:
                self.explorer.set_sort(ui_cfg.get("explorer_sort_key", self.explorer._sort_key),
                                        ui_cfg.get("explorer_sort_reverse", self.explorer._sort_reverse))
        if "editor" in changed:
            editor_cfg = self.cfg.setdefault("editor", {})
            self._default_new_file_language = editor_cfg.get("default_new_file_language", "python")
            self._run_commands = editor_cfg.setdefault("run_commands", {})
            syntax.set_custom_colors(editor_cfg.get("custom_syntax_colors", {}))
            syntax.sync_saved_palettes(self.cfg.get("syntax_palette_presets", {}))
            syntax.set_color_theme(editor_cfg.get("syntax_theme", syntax.DEFAULT_COLOR_THEME))
            syntax.configure_tags(self.text)
            self._word_wrap = bool(editor_cfg.get("word_wrap", config.DEFAULTS["editor"]["word_wrap"]))
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
        toplevel = self.winfo_toplevel()
        toplevel.configure(bg=t["bg"])
        theme.apply_classic_widget_defaults(toplevel, t)

        for frame in (self.toolbar, self.body, self.center, self.edit_outer, self.edit_area, self.text_frame,
                      self.console_frame, self.console_body):
            frame.configure(bg=t["bg"])
        self.tabbar.set_theme(t)
        self.interp_label.configure(bg=t["bg"], fg=t["fg"])
        self.peers_label.configure(bg=t["bg"], fg=t["fg"])
        self.file_path_label.configure(bg=t["bg"], fg=t["muted_fg"])
        self.run_cmd_entry.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"])
        self.linenumbers.configure(bg=t["gutter_bg"])
        self.text.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"],
                             selectbackground=t["sel_bg"], font=(font_family, self._font_size))
        self.yscroll.configure(**theme.classic_scrollbar_options(t))
        self.xscroll.configure(**theme.classic_scrollbar_options(t))
        self.console_yscroll.configure(**theme.classic_scrollbar_options(t))
        self.console_label.configure(bg=t["bg"], fg=t["muted_fg"])
        self.status_frame.configure(bg=t["status_bg"])
        self.status_right.configure(bg=t["status_bg"], fg=t["status_fg"])
        self.status_left.configure(bg=t["status_bg"], fg=t["status_fg"])
        self._theme_menus(t)
        # Explorer and Terminal now have fully independent font family
        # *and* size (see _set_explorer_font_size/_family and
        # _set_console_font_size/_family) - a theme change updates
        # colors everywhere, including the console's background here,
        # but deliberately leaves both panels' own fonts alone.
        self.console.configure(bg=t["console_bg"], fg=t["fg"])
        console_tag_colors = _console_tag_colors(t["console_bg"])
        self.console.tag_configure("stderr", foreground=console_tag_colors["stderr"])
        self.console.tag_configure("info", foreground=console_tag_colors["info"])
        self.console.tag_configure("prompt", foreground=console_tag_colors["prompt"])
        self.ansi_console.base_tag_colors = console_tag_colors

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

        status = tk.Frame(self, bg=t["status_bg"], height=22)
        status.pack(fill="x", side="bottom")
        self.status_frame = status
        self.status_right = tk.Label(status, text="", bg=t["status_bg"], fg=t["status_fg"], anchor="e")
        self.status_right.pack(side="right", padx=8)
        self.status_left = tk.Label(status, text="", bg=t["status_bg"], fg=t["status_fg"], anchor="w")
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
        self.explorer.set_sort(ui_prefs.get("explorer_sort_key", "name"),
                                ui_prefs.get("explorer_sort_reverse", False))
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

        edit_outer = tk.Frame(center, bg=t["bg"])
        center.add(edit_outer, minsize=30, stretch="always")
        self.edit_outer = edit_outer

        self.tabbar = TabBar(edit_outer, t, on_select=self._on_tab_select,
                              on_close=self._on_tab_close_request, on_context=self._show_tab_menu)
        self.tabbar.pack(side="top", fill="x")
        dnd_support.register_drop(self.tabbar, self._on_explorer_drop)
        dnd_support.register_drop(edit_outer, self._on_explorer_drop)

        edit_area = tk.Frame(edit_outer, bg=t["bg"])
        edit_area.pack(side="top", fill="both", expand=True)
        self.edit_area = edit_area

        self.linenumbers = tk.Canvas(edit_area, width=48, bg=t["gutter_bg"], highlightthickness=0)
        self.linenumbers.pack(side="left", fill="y")

        text_frame = tk.Frame(edit_area, bg=t["bg"])
        text_frame.pack(side="left", fill="both", expand=True)
        self.text_frame = text_frame

        yscroll = tk.Scrollbar(text_frame, orient="vertical", **theme.classic_scrollbar_options(t))
        yscroll.pack(side="right", fill="y")
        self.yscroll = yscroll

        xscroll = tk.Scrollbar(text_frame, orient="horizontal", command=self._on_xscroll,
                                **theme.classic_scrollbar_options(t))
        xscroll.pack(side="bottom", fill="x")
        self.xscroll = xscroll

        self.text = tk.Text(text_frame, wrap=("word" if self._word_wrap else "none"), undo=False,
                             bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"], selectbackground=t["sel_bg"],
                             font=(font_family, self._font_size), padx=8, pady=6, relief="flat",
                             yscrollcommand=self._on_text_yview_changed, xscrollcommand=xscroll.set, tabs=("1c",))
        self.text.pack(side="left", fill="both", expand=True)
        yscroll.config(command=self._on_yscroll)

        syntax.configure_tags(self.text)
        dnd_support.register_drop(self.text, self._on_explorer_drop)
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
        self.console_body = console_body

        console_yscroll = tk.Scrollbar(console_body, orient="vertical", **theme.classic_scrollbar_options(t))
        console_yscroll.pack(side="right", fill="y")
        self.console_yscroll = console_yscroll

        self.console = tk.Text(console_body, height=4, bg=t["console_bg"], fg=t["fg"], relief="flat",
                                font=(self._console_font_family, self._console_font_size),
                                yscrollcommand=console_yscroll.set)
        self.console.pack(side="left", fill="both", expand=True)
        console_yscroll.config(command=self.console.yview)
        console_tag_colors = _console_tag_colors(t["console_bg"])
        self.console.tag_configure("stderr", foreground=console_tag_colors["stderr"])
        self.console.tag_configure("info", foreground=console_tag_colors["info"])
        self.console.tag_configure("prompt", foreground=console_tag_colors["prompt"])
        self.ansi_console = ansi.AnsiConsole(self.console, base_tag_colors=console_tag_colors)

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
        self._refresh_tabs()

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
            if not self._active.shared:
                # Cursor positions are offsets into the shared document -
                # meaningless (and confusing for peers) while a local tab
                # is showing.
                return
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
        self._highlight_job = self.after_idle(self._do_highlight)

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
        self._active.local_edited = True
        self._refresh_dirty()
        self._update_cursor_status()
        if self._active.shared:
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
        self._active.local_edited = True
        self._refresh_dirty()
        self._update_cursor_status()
        if self._active.shared:
            self.client.local_edit(op)

    def _on_full_sync(self, text):
        self.after(0, lambda: self._apply_full_sync(text))

    def _reset_history(self):
        self.history.reset()
        self._saved_doc = self.doc
        self._refresh_dirty()

    def _apply_full_sync(self, text):
        tab = self.shared_tab
        if tab is self._active:
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
        else:
            # A local tab is showing: only the shared tab's stored copy
            # needs to follow along.
            tab.doc = text
            tab.history.reset()
            tab.saved_doc = text
            self._refresh_tabs()
        tab.local_edited = False
        if self.mode != "solo":
            self._set_status("Connected and synced with host")
        if self._pending_initial_file:
            path, self._pending_initial_file = self._pending_initial_file, None
            self._load_file(path, into_shared=True)
        else:
            self._update_file_path_label()

    def _on_remote_op(self, op, author=None, swap=None):
        self.after(0, lambda: self._apply_remote_op(op, author, swap))

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

    def _apply_remote_op(self, op, author=None, swap=None):
        if swap is not None:
            self._begin_remote_swap(swap, author)
        tab = self.shared_tab
        author_key = "remote" if author is None else author
        if tab is self._active:
            before = self.doc
            self._apply_op_to_widget(op)
            self.doc = op.apply(before)
            self.history.record(author_key, op, before)
            self._refresh_dirty()
            self._redraw_linenumbers()
            self._schedule_highlight()
        else:
            # The shared buffer isn't on screen (a local tab is, or it's
            # hidden) - keep its stored copy current so it's right when it
            # is shown again.
            was_dirty = tab.dirty
            before = tab.doc
            tab.doc = op.apply(before)
            tab.history.record(author_key, op, before)
            if tab.dirty != was_dirty:
                self._refresh_tabs()
        if swap is not None:
            # The peer's buffer is a clean starting point, not "our edits".
            tab.saved_doc = tab.doc
            tab.history.reset()
            tab.local_edited = False
            if tab is self._active:
                self._refresh_dirty()
                self._update_language_status()
                self._update_file_path_label()
                self._do_highlight()
            self._refresh_tabs()

    def _apply_history_op(self, op):
        before = self.doc
        self._apply_op_to_widget(op)
        self.doc = op.apply(before)
        if self._active.shared:
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
        ui["explorer_sort_key"] = self.explorer._sort_key
        ui["explorer_sort_reverse"] = self.explorer._sort_reverse
        if self._explorer_visible:
            ui["explorer_width"] = self.explorer.winfo_width()
        if self._console_visible:
            ui["console_height"] = self.console_frame.winfo_height()
        for dialog in list(FindDialog._instances):
            dialog._store_state()
        config.save_config(self.cfg)

    def action_disconnect(self):
        if not self._confirm_discard_all():
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

    def _confirm_discard_tab(self, tab):
        """_confirm_discard for a specific tab: brings it to the front
        first, so the Save prompt (and a Save As dialog, if it comes to
        that) is clearly about the buffer being asked about."""
        if not tab.dirty:
            return True
        if tab is not self._active:
            self._switch_to(tab)
        return self._confirm_discard()

    def _confirm_discard_all(self):
        for tab in list(self.visible_tabs):
            if tab.dirty and not self._confirm_discard_tab(tab):
                return False
        return True

    def has_unsaved_tabs(self):
        return any(tab.dirty for tab in self.visible_tabs)

    def save_all_tabs(self):
        """Saves every unsaved tab (asking Save As for untitled ones).
        Returns False if any of them ended up still unsaved."""
        for tab in list(self.visible_tabs):
            if tab.dirty:
                self._switch_to(tab)
                self.action_save()
                if self.dirty:
                    return False
        return True

    def _confirm_discard(self):
        """Gate before anything that would throw away the active buffer
        (closing its tab, Disconnect/Close). Offers Save / Don't Save / Cancel
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

    def action_new(self):
        """A new, empty, local tab - like opening a file, it doesn't touch
        the shared buffer. Right-click it > Share to make it the shared one."""
        tab = BufferTab()
        self._tabs.append(tab)
        self._switch_to(tab)

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

    @staticmethod
    def _same_path(a, b):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def _find_tab_for_path(self, path):
        for tab in self.visible_tabs:
            if tab.current_file and self._same_path(tab.current_file, path):
                return tab
        return None

    def _load_file(self, path, into_shared=False):
        """Opens `path` in its own new local tab (or focuses the tab that
        already has it) - it is NOT sent to peers; right-click the tab >
        Share for that. `into_shared` is for the file a session was
        started with, which is meant to be the shared buffer from the
        start. In Solo mode there's nobody to share with, so opening a
        file over a still-untouched first tab just fills that tab
        instead of leaving an empty one behind."""
        existing = self._find_tab_for_path(path)
        if existing is not None:
            if into_shared:
                self._make_shared(existing)
            else:
                self._switch_to(existing)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            messagebox.showerror("Open File", str(e))
            return
        tab = BufferTab(doc=content)
        tab.current_file = path
        tab.loaded_mtime = self._safe_mtime(path)
        tab.filename_hint = self._share_hint_for(path)
        self._tabs.append(tab)
        old = self.shared_tab
        pristine_solo = (self.mode == "solo" and old.visible and len(self.visible_tabs) == 2
                         and old.current_file is None and not old.local_edited and old.doc == "")
        if into_shared or pristine_solo:
            self._make_shared(tab)
        else:
            self._switch_to(tab)

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
        if self._active.shared:
            self.client.send_buffer_context(self.active_filename_hint)
        self._update_language_status()
        self._update_file_path_label()
        self._do_highlight()
        self._refresh_tabs()

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
        """Tells us the *name* the shared buffer now goes by - for syntax
        highlighting and the tab title - and (for late joiners, and after
        a peer's Save As) that's all it is. Whether the shared buffer was
        actually replaced with a different document is decided by the
        "swap" marker riding on the op itself (see _begin_remote_swap),
        which - unlike this separate message - is guaranteed to arrive in
        order with the edits."""
        filename = payload.get("filename")
        by = payload.get("by")
        tab = self.shared_tab
        tab.filename_hint = filename
        if tab is self._active:
            self._update_language_status()
            self._do_highlight()
            self._update_file_path_label()
        self._refresh_tabs()
        if by == self.client.client_id:
            return
        who = next((n for pid, n in self._known_peer_names.items() if pid == by), "someone")
        label = filename or "a blank buffer"
        self._console_write(f"{who} loaded {label} into the shared buffer\n", "info")

    # ------------------------------------------------------------------
    # Buffer tabs
    #
    # Model: every open buffer is a BufferTab. Exactly one is the shared
    # tab (cloud icon) and mirrors the session's collaborative document;
    # the rest are local. One Text widget shows whichever tab is active -
    # switching stores the outgoing tab's caret/scroll and loads the
    # incoming tab's text. Remote edits always go to the shared tab (into
    # the widget if it's showing, into its stored copy if not).
    # ------------------------------------------------------------------

    @property
    def visible_tabs(self):
        return [tab for tab in self._tabs if tab.visible]

    def _tab_by_id(self, tab_id):
        return next((tab for tab in self._tabs if tab.id == tab_id), None)

    def _tab_base_title(self, tab):
        if tab.current_file:
            return os.path.basename(tab.current_file)
        if tab.shared and tab.filename_hint:
            return os.path.basename(tab.filename_hint)
        if tab.shared and self.mode != "solo":
            return "Shared"
        if tab.untitled_no is None:
            tab.untitled_no = next(self._untitled_counter)
        return f"Untitled-{tab.untitled_no}"

    def _refresh_tabs(self):
        bar = getattr(self, "tabbar", None)
        if bar is None:
            return
        tabs = self.visible_tabs
        titles = [self._tab_base_title(tab) for tab in tabs]
        counts = collections.Counter(titles)
        items = []
        for tab, title in zip(tabs, titles):
            if counts[title] > 1 and tab.current_file:
                # Two open files with the same name: say which folder each is in.
                parent = os.path.basename(os.path.dirname(tab.current_file))
                if parent:
                    title = f"{title} ({parent})"
            items.append({"id": tab.id, "title": title, "dirty": tab.dirty,
                          "shared": tab.shared and self.mode != "solo"})
        bar.render(items, self._active.id)

    def _hint_for_tab(self, tab):
        return self._share_hint_for(tab.current_file) if tab.current_file else None

    def _worth_keeping(self, tab):
        """Is there anything of *this user's* in the tab - a file they
        opened there, or text they typed - such that it shouldn't just
        vanish when the shared buffer is replaced?"""
        return tab.current_file is not None or tab.local_edited

    def _on_tab_select(self, tab_id):
        tab = self._tab_by_id(tab_id)
        if tab is not None:
            self._switch_to(tab)

    def _on_tab_close_request(self, tab_id):
        tab = self._tab_by_id(tab_id)
        if tab is not None:
            self.close_tab(tab)

    def _store_view(self, tab):
        try:
            tab.caret = char_offset(self.text, "insert")
            tab.yview = self.text.yview()[0]
        except tk.TclError:
            pass

    def _switch_to(self, tab):
        tab.visible = True
        if tab is self._active:
            self.text.focus_set()
            self._refresh_tabs()
            return
        self._store_view(self._active)
        self._active = tab
        self._display_active()

    def _display_active(self):
        """Loads the active tab's document into the Text widget and
        refreshes everything that depends on which buffer is showing."""
        tab = self._active
        self._suppress_capture = True
        try:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", tab.doc)
            self.text.tag_remove("sel", "1.0", "end")
            self.text.mark_set("insert", f"1.0+{min(tab.caret, len(tab.doc))}c")
        finally:
            self._suppress_capture = False
        self.text.update_idletasks()
        self.text.yview_moveto(tab.yview)
        self.cursor_layer.set_visible(tab.shared)
        self.dirty = tab.dirty  # also refreshes the window title and tab strip
        self._update_language_status()
        self._update_file_path_label()
        self._redraw_linenumbers()
        self._do_highlight()
        self._update_cursor_status()
        self.text.focus_set()

    def _after_role_change(self):
        """The active tab just became shared / stopped being shared."""
        self.cursor_layer.set_visible(self._active.shared)
        self._update_language_status()
        self._update_file_path_label()
        self._update_cursor_status()
        self._refresh_tabs()

    def _cycle_tab(self, step):
        tabs = self.visible_tabs
        if len(tabs) > 1:
            self._switch_to(tabs[(tabs.index(self._active) + step) % len(tabs)])

    def action_next_tab(self):
        self._cycle_tab(1)

    def action_prev_tab(self):
        self._cycle_tab(-1)

    def action_show_shared(self):
        self._switch_to(self.shared_tab)

    def action_close_active_tab(self):
        self.close_tab(self._active)

    def close_tab(self, tab):
        if not self._confirm_discard_tab(tab):
            return False
        tabs = self.visible_tabs
        index = tabs.index(tab) if tab in tabs else 0
        if tab.shared:
            # The session's document has to keep being tracked, so the
            # shared tab can't really go away - it's just hidden (bring it
            # back with View > Show Shared Buffer, or when a peer shares a
            # new buffer). Closing it also ends any file association.
            tab.visible = False
            tab.current_file = None
            tab.loaded_mtime = None
            tab.local_edited = False
            tab.saved_doc = tab.doc
        else:
            self._tabs.remove(tab)
        if tab is self._active:
            remaining = self.visible_tabs
            if not remaining:
                blank = BufferTab()
                self._tabs.append(blank)
                remaining = [blank]
            self._active = remaining[min(index, len(remaining) - 1)]
            self._display_active()
        else:
            self._refresh_tabs()
        return True

    def _show_tab_menu(self, tab_id, x_root, y_root):
        tab = self._tab_by_id(tab_id)
        if tab is None:
            return
        t = self.theme
        menu = tk.Menu(self, tearoff=0, bg=t["bg"], fg=t["fg"], activebackground=t["sel_bg"],
                        activeforeground=t["fg"], disabledforeground=t["muted_fg"])
        if self.mode != "solo":
            if tab.shared:
                menu.add_command(label="Unshare", command=lambda: self.unshare_tab(tab))
            else:
                menu.add_command(label="Share", command=lambda: self.share_tab(tab))
            menu.add_separator()
        if tab.current_file:
            menu.add_command(label="Copy Path", command=lambda: self._copy_text(tab.current_file, "path"))
        else:
            # An unsaved buffer has no path to copy - just its name.
            menu.add_command(label="Copy Name",
                             command=lambda: self._copy_text(self._tab_base_title(tab), "name"))
        menu.add_separator()
        menu.add_command(label="Close", command=lambda: self.close_tab(tab))
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _copy_text(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status(f"Copied {what}: {text}")

    def share_tab(self, tab):
        if self.mode == "solo" or tab.shared:
            return
        self._make_shared(tab)

    def _make_shared(self, tab):
        """Makes `tab` the shared tab: its content replaces the shared
        document for everyone (sent as one op flagged as a buffer swap, so
        peers keep their own file open as a local tab instead of losing
        it). The tab that was shared until now stays on as a local tab if
        there's something of ours in it, and is dropped otherwise."""
        old = self.shared_tab
        if old is tab:
            return
        base_doc = old.doc
        if old.visible and self._worth_keeping(old):
            old.shared = False
            old.filename_hint = self._hint_for_tab(old)
        elif old in self._tabs:
            self._tabs.remove(old)
        tab.shared = True
        tab.visible = True
        self.shared_tab = tab
        hint = self._hint_for_tab(tab)
        tab.filename_hint = hint
        self.client.local_edit(ot.diff_to_op(base_doc, tab.doc), {"filename": hint})
        self.client.send_buffer_context(hint)
        if tab is self._active:
            self._after_role_change()
        else:
            self._switch_to(tab)

    def unshare_tab(self, tab):
        """Stops sharing `tab`: it becomes an ordinary local tab holding
        what it holds now. The session itself carries on - the shared
        document is tracked in a fresh, hidden shared tab (View > Show
        Shared Buffer) so we keep receiving peers' changes."""
        if self.mode == "solo" or not tab.shared:
            return
        mirror = BufferTab(shared=True, doc=tab.doc)
        mirror.filename_hint = tab.filename_hint
        mirror.visible = False
        tab.shared = False
        tab.filename_hint = self._hint_for_tab(tab)
        self._tabs.insert(self._tabs.index(tab) + 1, mirror)
        self.shared_tab = mirror
        if tab is self._active:
            self._after_role_change()
        else:
            self._refresh_tabs()
        self._set_status("Unshared - this tab is now local. The shared buffer is under View > Show Shared Buffer")

    def _begin_remote_swap(self, swap, author):
        """A peer replaced the shared buffer with a different document (the
        op that's about to be applied carries `swap`). If we had a file
        open in the shared tab - or typed into it - keep that as a local
        tab exactly as it is right now, and let a new shared tab take over
        the session's document; the incoming op then turns that new tab
        into the peer's buffer. Runs *before* the op is applied, so the
        local tab really does keep our old content."""
        old = self.shared_tab
        new_hint = swap.get("filename")
        # Both of us have the same file open -> keep working on it here.
        same_file = old.current_file is not None and self._hint_for_tab(old) == new_hint
        if old.visible and self._worth_keeping(old) and not same_file:
            fresh = BufferTab(shared=True, doc=old.doc)
            fresh.filename_hint = new_hint
            old.shared = False
            old.filename_hint = self._hint_for_tab(old)
            self._tabs.insert(self._tabs.index(old), fresh)
            self.shared_tab = fresh
            if old is self._active:
                self.cursor_layer.set_visible(False)
            self._console_write(f"  ({self._tab_base_title(old)} stays open as a local tab)\n", "info")
        else:
            old.visible = True
            old.filename_hint = new_hint
            old.local_edited = old.local_edited and same_file

    def _prepare_run_target(self):
        """Returns (cmd, target, is_temp) for the buffer to run, or None
        (after showing an error, or the user declining a save prompt) if
        it couldn't be prepared. A buffer that's already saved and clean
        runs straight from its own file; a dirty one is saved back to
        that same file first (so what runs matches what's on screen,
        the same "save before run" most editors do) rather than forked
        off into a second, untracked copy sitting next to it. Only a
        buffer that was never saved anywhere - so there's no file to
        run - falls back to a temp file, and even then it's written to
        the OS temp directory rather than the project folder, and
        removed again once the run finishes (see _watch_run_proc)."""
        cmd = self.run_cmd.get().strip() or sys.executable
        if self.current_file is not None:
            if self.dirty:
                self.action_save()
                if self.dirty:
                    # action_save can no-op (declined an external-change
                    # prompt) or fail (write error, already reported by
                    # action_save itself) - either way there's nothing
                    # safe to run.
                    return None
            return cmd, self.current_file, False

        try:
            fd, target = tempfile.mkstemp(prefix="peer2code_run_", suffix=".py")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.text.get("1.0", "end-1c"))
        except OSError as e:
            messagebox.showerror("Run", str(e))
            return None
        return cmd, target, True

    def action_run(self):
        if self.run_proc and self.run_proc.poll() is None:
            messagebox.showinfo("Run", "Something is already running in the Terminal.")
            return
        prepared = self._prepare_run_target()
        if prepared is None:
            return
        cmd, target, is_temp = prepared
        self.ansi_console.state.reset()
        self._ensure_newline()
        self._console_write(f"$ {cmd} {os.path.basename(target)}\n", "info")
        self._launch_process([cmd, target], cleanup_path=(target if is_temp else None))

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

    def _launch_process(self, args, shell=False, cleanup_path=None):
        """Actually starts args - either an argv list (Run button) or a
        single shell command string (shell=True, a line typed at the
        Terminal's own prompt) - as run_proc, and wires up the pump/
        watch machinery every run shares regardless of how it started.
        cleanup_path, when given (an unsaved buffer's temp file - see
        _prepare_run_target), is removed once the process ends, or right
        away if it never managed to start."""
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
        # bufsize=1 above only affects how *we* read the pipe. It says
        # nothing about the child itself: since its stdout isn't a tty,
        # a Python child defaults to full block buffering there, so
        # prints (e.g. from a script that then sits in a tkinter
        # mainloop) don't cross the pipe until the buffer fills or the
        # process exits - looking like the terminal only "wakes up"
        # once the run finishes. PYTHONUNBUFFERED forces it unbuffered.
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.run_proc = subprocess.Popen(
                args, cwd=self.working_dir, shell=shell,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                creationflags=creationflags,
                start_new_session=(not is_windows),
                env=env,
            )
        except OSError as e:
            self._ensure_newline()
            self._console_write(f"Failed to launch: {e}\n", "stderr")
            self._show_prompt()
            if cleanup_path:
                try:
                    os.remove(cleanup_path)
                except OSError:
                    pass
            return
        self._proc_exit_expected = False
        threading.Thread(target=self._pump_stream, args=(self.run_proc.stdout, False), daemon=True).start()
        threading.Thread(target=self._pump_stream, args=(self.run_proc.stderr, True), daemon=True).start()
        threading.Thread(target=self._watch_run_proc, args=(self.run_proc, cleanup_path), daemon=True).start()

    def _watch_run_proc(self, proc, cleanup_path=None):
        """Waits (off the main thread - proc.wait() blocks) for this run
        to actually end, how ever it ends: normal exit, our own Stop or
        interrupt, or - the thing we actually can't see any other way -
        something outside this app killing it directly (a stray terminal
        signal delivered to the child's process/session despite the
        isolation in _launch_process, another process sending it a
        signal, etc.). cleanup_path is removed once it does, regardless
        of which of those it was, so an unsaved buffer's temp file (see
        _prepare_run_target) never outlives the run it was made for.
        Only enqueues a report for the run this thread was started for,
        so a stale watcher from a previous run can't report on top of a
        new one that's since started."""
        returncode = proc.wait()
        if cleanup_path:
            try:
                os.remove(cleanup_path)
            except OSError:
                pass
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
        # Normalize line endings before inserting. Clipboard content that
        # came from Windows (or was copied from a file with CRLF endings)
        # commonly uses "\r\n", and some sources use a lone "\r" - Tk's
        # Text widget treats those as literal characters rather than line
        # breaks, so without this every pasted line would carry a stray
        # control character at its end. That breaks anything downstream
        # that works line-by-line: leading-whitespace/indent math,
        # trailing-whitespace display, and the peer sync char-offset
        # accounting all assume "\n" is the only line separator.
        clip = clip.replace("\r\n", "\n").replace("\r", "\n")
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
        self._schedule_highlight()
        return "break"

    def action_duplicate_line(self):
        line_no = int(self.text.index("insert").split(".")[0])
        content = self.text.get(f"{line_no}.0", f"{line_no}.end")
        self.text.insert(f"{line_no}.end", "\n" + content)
        self._schedule_highlight()
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
        self._schedule_highlight()
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
            # No selection: pad to the next tab-stop rather than always
            # inserting a flat INDENT_WIDTH block, so Tab lands on the
            # same aligned columns (4, 8, 12, ...) regardless of where in
            # the line the cursor happens to be - matching Tab in VS
            # Code/Sublime/etc. rather than always adding exactly 4
            # characters even from an already-misaligned column.
            col = int(self.text.index("insert").split(".")[1])
            pad = INDENT_WIDTH - (col % INDENT_WIDTH)
            self.text.insert("insert", " " * pad)
            return "break"
        start, end = sel
        with self._single_undo_step():
            for n in range(start, end + 1):
                if self.text.get(f"{n}.0", f"{n}.end").strip():
                    self.text.insert(f"{n}.0", " " * INDENT_WIDTH)
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
                cut = min(cut, INDENT_WIDTH)
                if cut:
                    self.text.delete(f"{n}.0", f"{n}.{cut}")
        return "break"

    def _line_indent(self, line_text):
        """The leading run of spaces/tabs on a line of text."""
        return line_text[:len(line_text) - len(line_text.lstrip(" \t"))]

    def _matching_bracket_index(self, before_text, close_char):
        """Scans `before_text` (everything in the buffer up to some
        position) backward for the opener that `close_char` would match
        if inserted right after it, tracking nesting depth of that one
        bracket type only. Returns a character offset into `before_text`,
        or None if there's no unmatched opener - e.g. a stray ')' with
        nothing open, or genuinely balanced code with no gap to fill."""
        open_char = _CLOSE_TO_OPEN[close_char]
        depth = 0
        i = len(before_text) - 1
        while i >= 0:
            c = before_text[i]
            if c == close_char:
                depth += 1
            elif c == open_char:
                if depth == 0:
                    return i
                depth -= 1
            i -= 1
        return None

    def _on_text_return(self, event=None):
        """Auto-indent, the way every mainstream code editor does it:
        - A new line starts at the same indent level as the line it was
          split from, rather than snapping back to column 0.
        - One extra level is added when the line up to the cursor ends
          with something that conventionally opens a new block (':',
          '{', '(', '[') - covers Python's colon-based blocks and
          brace/paren-based blocks in most of this editor's other
          languages without needing per-language grammar.
        - Pressing Enter with the cursor sitting directly between a
          matching bracket pair ('{|}', '(|)', '[|]', nothing typed
          between them yet) splits it into three lines instead of one,
          with the cursor indented a level deeper and the closing
          bracket left behind at the original indent - e.g. '{}' becomes
          '{', an indented blank line with the cursor, then '}'.
        - In Python, pressing Enter at the end of a line whose statement
          is a bare 'return', 'break', 'continue', 'pass', or 'raise'
          (with or without a value/argument) dedents the new line one
          level, since nothing else in that block can execute after it -
          covers the same "no closing bracket to hang it off of" gap as
          the electric else/elif/except/finally dedent, but for the
          other direction: leaving a block instead of joining one."""
        text = self.text
        with self._single_undo_step():
            if text.tag_ranges("sel"):
                text.delete("sel.first", "sel.last")
            line_no = int(text.index("insert").split(".")[0])
            full_line = text.get(f"{line_no}.0", f"{line_no}.end")
            indent = self._line_indent(full_line)
            before_cursor = text.get(f"{line_no}.0", "insert").rstrip()
            char_before = text.get("insert-1c", "insert")
            char_after = text.get("insert", "insert+1c")
            if char_before in _OPEN_TO_CLOSE and char_after == _OPEN_TO_CLOSE.get(char_before):
                inner_indent = indent + " " * INDENT_WIDTH
                text.insert("insert", "\n" + inner_indent + "\n" + indent)
                text.mark_set("insert", f"{line_no + 1}.{len(inner_indent)}")
            else:
                if before_cursor.endswith(_INDENT_AFTER_SUFFIXES):
                    indent += " " * INDENT_WIDTH
                elif (self._current_language() == "python"
                        and len(indent) >= INDENT_WIDTH
                        and before_cursor == full_line.rstrip()
                        and _PY_FLOW_DEDENT_KEYWORDS.match(full_line.lstrip(" \t"))):
                    indent = indent[:-INDENT_WIDTH]
                text.insert("insert", "\n" + indent)
        text.see("insert")
        self._schedule_highlight()
        return "break"

    def _on_text_backspace(self, event=None):
        """Backspace removes a whole indent level (up to the previous
        multiple-of-INDENT_WIDTH column) in one press when everything
        from the start of the line to the cursor is indentation - instead
        of the default one-space-per-press, which means either repeatedly
        pressing Backspace or holding it (and overshooting into the
        previous line's text) just to back out one level. Falls through
        to the Text widget's normal Backspace anywhere else (a selection,
        mid-word, column 0, or a line that mixes indentation with tabs -
        left alone rather than guessed at)."""
        text = self.text
        if text.tag_ranges("sel"):
            return None
        line_no, col = (int(p) for p in text.index("insert").split("."))
        if col == 0:
            return None
        before = text.get(f"{line_no}.0", f"{line_no}.{col}")
        if before.strip(" ") != "":
            return None
        remainder = col % INDENT_WIDTH
        delete_count = min(remainder or INDENT_WIDTH, col)
        with self._single_undo_step():
            text.delete(f"{line_no}.{col - delete_count}", f"{line_no}.{col}")
        return "break"

    def _on_text_keypress(self, event):
        """Two more "electric" behaviors that fire on the keystroke that
        completes them, both applied before the character itself lands
        (this runs on <Key>/<KeyPress>, ahead of the widget's own default
        insertion) so the typed character still ends up in the right
        place afterward:
        - Typing a closing bracket (')', ']', '}') as the first non-
          whitespace thing on a line re-indents that line to match the
          line holding its opener first - the standard "closing bracket
          snaps to its block" behavior, instead of leaving it sitting at
          whatever indent Enter last guessed.
        - In Python, typing the ':' that completes a bare 'else',
          'elif', 'except', or 'finally' line dedents that line to line
          up with the nearest enclosing statement at a shallower indent
          (its matching 'if'/'for'/'while'/'try', or a sibling
          'elif'/'except' at the same level) - Python has no closing
          bracket to hang that dedent off of, so IDEs key it off the
          keyword instead.
        Every other key is left untouched and simply falls through
        (returns None) to normal typing."""
        ch = event.char
        text = self.text
        if ch in _CLOSE_TO_OPEN:
            if text.tag_ranges("sel"):
                return None
            line_no, col = (int(p) for p in text.index("insert").split("."))
            before = text.get(f"{line_no}.0", f"{line_no}.{col}")
            if before.strip(" \t") != "":
                return None
            buffer_before = text.get("1.0", "insert")
            match_offset = self._matching_bracket_index(buffer_before, ch)
            if match_offset is None:
                return None
            match_line = int(text.index(f"1.0+{match_offset}c").split(".")[0])
            match_line_text = text.get(f"{match_line}.0", f"{match_line}.end")
            target_indent = self._line_indent(match_line_text)
            if target_indent == before:
                return None
            with self._single_undo_step():
                text.delete(f"{line_no}.0", f"{line_no}.{col}")
                text.insert(f"{line_no}.0", target_indent)
            text.mark_set("insert", f"{line_no}.{len(target_indent)}")
            return None
        if ch == ":" and self._current_language() == "python":
            line_no = int(text.index("insert").split(".")[0])
            full_line = text.get(f"{line_no}.0", f"{line_no}.end")
            stripped = full_line.lstrip(" \t")
            if not _PY_DEDENT_KEYWORDS.match(stripped):
                return None
            current_indent = self._line_indent(full_line)
            target_indent = None
            n = line_no - 1
            while n >= 1:
                prev = text.get(f"{n}.0", f"{n}.end")
                if prev.strip():
                    prev_indent = self._line_indent(prev)
                    prev_stripped = prev.lstrip(" \t")
                    if len(prev_indent) < len(current_indent):
                        target_indent = prev_indent
                        break
                    if len(prev_indent) == len(current_indent) and _PY_BLOCK_OPENERS.match(prev_stripped):
                        target_indent = prev_indent
                        break
                    # Same or deeper indent but not a sibling clause (an
                    # ordinary statement inside the block, or a nested
                    # block's contents) - keep walking up past it rather
                    # than giving up, since skipping over regular
                    # same-level statements to find the block's own
                    # header/sibling is exactly the normal case.
                n -= 1
            if target_indent is None or target_indent == current_indent:
                return None
            with self._single_undo_step():
                text.delete(f"{line_no}.0", f"{line_no}.{len(current_indent)}")
                text.insert(f"{line_no}.0", target_indent)
            return None
        return None

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
        self.text.bind("<Return>", self._on_text_return)
        self.text.bind("<KP_Enter>", self._on_text_return)
        self.text.bind("<BackSpace>", self._on_text_backspace)
        self.text.bind("<Key>", self._on_text_keypress)
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
    # Dialogs currently open, so EditorApp._persist_ui_state can store their
    # geometry too when the whole app closes with one still showing (the
    # window's own <Destroy> then fires after the config was already saved).
    _instances = []

    _GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")

    def __init__(self, app: EditorApp, replace: bool):
        super().__init__(app)
        self.app = app
        t = app.theme
        self.title("Replace" if replace else "Find")
        self.configure(bg=t["panel_bg"])
        # Width only: the height is fixed by the layout (and differs between
        # Find and Replace), but a wider box is genuinely useful for long
        # search strings, and gives "remember size" something real to keep.
        self.resizable(True, False)
        self.transient(app.winfo_toplevel())
        for col in (1, 2, 3):
            self.columnconfigure(col, weight=1)
        self._last_geometry = None
        FindDialog._instances.append(self)

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

        ui_cfg = app.cfg.get("ui", {})
        defaults = config.DEFAULTS["ui"]
        self.match_case_var = tk.BooleanVar(
            value=bool(ui_cfg.get("find_match_case", defaults["find_match_case"])))
        self.whole_word_var = tk.BooleanVar(
            value=bool(ui_cfg.get("find_whole_word", defaults["find_whole_word"])))
        self.wrap_var = tk.BooleanVar(
            value=bool(ui_cfg.get("find_wrap", defaults["find_wrap"])))
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
        self.bind("<Configure>", self._on_configure)
        self._apply_saved_geometry()
        self.replace_var.set(FindDialog._last_replace)
        selected = self._selected_or_empty()
        self.find_var.set(selected or FindDialog._last_find)
        find_entry.select_range(0, "end")

        def _focus():
            self.focus_force()
            find_entry.focus_set()
        self.after(50, _focus)

    def _apply_saved_geometry(self):
        """Restores the dialog's last width and/or position, whichever of
        the two Settings > General > Window says to remember. Both are
        clamped to the current screen (a saved position can be stale after
        a monitor layout change), and the width never drops below what the
        buttons need. Anything not remembered is left to the window manager
        / the layout's natural size."""
        ui = self.app.cfg.get("ui", {})
        self.update_idletasks()
        req_w, req_h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.minsize(req_w, req_h)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()

        width = req_w
        m = re.match(r"^(\d+)x(\d+)$", ui.get("find_window_size", "") or "")
        if ui.get("save_find_window_size", True) and m:
            width = max(req_w, min(int(m.group(1)), screen_w))

        geometry = f"{width}x{req_h}"
        m = re.match(r"^([+-]-?\d+)([+-]-?\d+)$", ui.get("find_window_position", "") or "")
        if ui.get("save_find_window_position", True) and m:
            x = int(m.group(1).lstrip("+"))
            y = int(m.group(2).lstrip("+"))
            x = max(0, min(x, max(0, screen_w - width)))
            y = max(0, min(y, max(0, screen_h - req_h)))
            geometry += f"+{x}+{y}"
        try:
            self.geometry(geometry)
        except tk.TclError:
            pass

    def _on_configure(self, event):
        # Cache the last good geometry as it changes: by the time <Destroy>
        # fires the window may already be unqueryable, and a minimized
        # window reports nonsense (e.g. "1x1+-32000+-32000" on Windows), so
        # only a normal, on-screen state is ever remembered.
        if event.widget is not self:
            return
        try:
            if self.state() == "normal":
                self._last_geometry = self.geometry()
        except tk.TclError:
            pass

    def _store_state(self):
        """Writes this dialog's geometry and checkbox states into the app's
        in-memory config (the caller decides when to flush it to disk).
        Each half of the geometry is independently opt-in: when its
        checkbox in Settings is off, the key is removed entirely, mirroring
        how the main window's own size/position behave."""
        ui = self.app.cfg.setdefault("ui", {})
        try:
            ui["find_match_case"] = bool(self.match_case_var.get())
            ui["find_whole_word"] = bool(self.whole_word_var.get())
            ui["find_wrap"] = bool(self.wrap_var.get())
        except tk.TclError:
            pass

        # An option that's off means "forget it": drop the saved value
        # outright rather than leaving a stale one behind.
        if not ui.get("save_find_window_size", True):
            ui.pop("find_window_size", None)
        if not ui.get("save_find_window_position", True):
            ui.pop("find_window_position", None)

        # Prefer the cached last-good (normal-state) geometry; the live
        # value is only a fallback, since a minimized window reports junk.
        candidates = [self._last_geometry]
        try:
            if self.state() == "normal":
                candidates.append(self.geometry())
        except tk.TclError:
            pass
        m = next((mm for mm in (FindDialog._GEOMETRY_RE.match(c or "") for c in candidates) if mm), None)
        if not m:
            return
        w, h, x, y = m.groups()
        if ui.get("save_find_window_size", True):
            ui["find_window_size"] = f"{w}x{h}"
        if ui.get("save_find_window_position", True):
            ui["find_window_position"] = f"+{int(x.lstrip('+'))}+{int(y.lstrip('+'))}"

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
            if self in FindDialog._instances:
                FindDialog._instances.remove(self)
                self._store_state()
                config.save_config(self.app.cfg)

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

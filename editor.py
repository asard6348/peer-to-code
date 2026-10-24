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
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

import fileio
import interpreters
import ot
import procutil
import syntax
import config
import theme
import ansi
from chat_util import (parse_chat_input, chat_settings, sanitize_text, sanitize_name, find_code_refs,
                       adapt_color, completion_context, format_name_for_command, CHAT_HELP)
from share_util import (parse_share_command, share_title, ShareStreamer, RunAssembler, render_sgr,
                        sanitize_output, SHARE_WARNING, SHARE_PENDING_MAX)
from shared_output_view import SharedOutputWindow
import dnd_support
import undo_history
from text_proxy import install_proxy, install_delete_guard, char_offset
from file_explorer import FileExplorer
from dock import DockManager
from peer_cursors import PeerCursorLayer
from tab_bar import TabBar
from settings_window import SettingsWindow
from net.client import Client, ConnectCancelled, ConnectError, FailoverController
from net.transport import MAIN_DOC

# The Terminal console's stdout/stderr/prompt colors, tuned as two fixed pairs rather than one.
CONSOLE_TAG_COLORS_DARK = {"stderr": "#e06c75", "info": "#61afef", "prompt": "#98c379"}
CONSOLE_TAG_COLORS_LIGHT = {"stderr": "#cf222e", "info": "#0969da", "prompt": "#1a7f37"}

# How many spaces one indent level is.
INDENT_WIDTH = 4
# A line ending in one of these gets one extra indent level on the line Enter creates.
_INDENT_AFTER_SUFFIXES = (":", "{", "(", "[")
_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSE_TO_OPEN = {v: k for k, v in _OPEN_TO_CLOSE.items()}
# Python has no closing bracket to hang a dedent off of for its colon-based blocks.
_PY_DEDENT_KEYWORDS = re.compile(r"^(else|elif|except|finally)\b")
# What a dedented else/elif/except/finally is allowed to land next to at the same indent level.
_PY_BLOCK_OPENERS = re.compile(r"^(if|elif|else|for|while|try|except|finally|with)\b")
# A line whose statement is one of these unconditionally ends control flow at that point.
_PY_FLOW_DEDENT_KEYWORDS = re.compile(r"^(return|break|continue|pass|raise)\b")

# Default code-like font
from config import CODE_FONT


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
    ("toggle_chat", "Toggle Chat", "<Control-Shift-M>", "toggle_chat"),
    ("close_tab", "Close Tab", "<Control-w>", "action_close_active_tab"),
    ("next_tab", "Next Tab", "<Control-Next>", "action_next_tab"),
    ("prev_tab", "Previous Tab", "<Control-Prior>", "action_prev_tab"),
]

# Shortcuts scoped to the Terminal console only.
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
    """Everything that belongs to one open buffer."""

    _ids = itertools.count(1)

    def __init__(self, doc_id=None, doc=""):
        self.id = next(BufferTab._ids)
        self.doc_id = doc_id
        # Set while this tab's content has been sent to the session as a new
        # document and the server hasn't yet answered with the document's id.
        self.pending_ref = None
        # Only a shared tab can be hidden (closing it just stops displaying
        # it - the session's document has to keep being tracked).
        self.visible = True
        self.doc = doc
        self.saved_doc = doc
        self.history = undo_history.UndoHistory()
        self.current_file = None
        self.filename_hint = None
        self.loaded_mtime = None
        self.eol = "\n"      # line ending and BOM the file had on disk, restored on save
        self.bom = False
        # True once *this user* has typed into the tab - as opposed to content that merely arrived from a peer.
        self.local_edited = False
        self.untitled_no = None
        # Restored when the tab is switched back to.
        self.caret = 0
        self.yview = 0.0

    @property
    def shared(self):
        return self.doc_id is not None

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

        # Open buffers.
        first_tab = BufferTab(doc_id=MAIN_DOC)
        self._tabs = [first_tab]
        self._active = first_tab
        # Every document the session carries -> the tab tracking it (a hidden
        # one if nobody is looking at it).
        self._docs = {MAIN_DOC: first_tab}
        self._pending_shares = {}      # request ref -> the tab whose content was sent as a new document
        self._share_refs = itertools.count(1)
        self._author_names = {}
        self._dirty = False
        self.dirty = False
        self._suppress_capture = False
        self._highlight_job = None
        self.run_proc = None
        self.out_queue = queue.Queue(maxsize=256)   # bounded: a chatty child blocks instead of flooding the UI
        self._exit_queue = queue.Queue()
        self._proc_exit_expected = False
        self._interrupt_sent = False
        self._cursor_send_job = None
        self._poll_job = None
        self._console_resize_job = None
        self._known_peer_names = {}
        # Reconnect UI: the address field that appears next to the toolbar's Disconnect button once a session drops.
        self.reconnect_entry = None
        self._reconnect_token = None
        self._reconnect_client = None
        # Chat panel state (see the "Chat" section below).
        self._roster = []              # last roster from the server: [{"id", "name", "color", ...}]
        self._last_private = None      # (client id, name) of the last private sender, for `/r`
        self._chat_unread = 0
        self._chat_flash_job = None
        self._chat_name_tags = {}      # peer id -> color its name tag was last configured with
        self._chat_link_n = 0
        self._chat_input_history = []  # lines sent from the Chat input, for Up/Down recall
        self._chat_input_pos = None
        self._chat_input_stash = ""
        self._chat_was_enabled = None
        # Shared run output (see the "Shared run output" section below).
        self._share_on = False         # session-only, never saved: opt-in each launch
        self._share = None             # the run currently being shared: {"run", "streamer"}
        self._share_run_counter = 0
        self._share_runs = {}          # viewer side: sender id -> {"run", "title", "name", "asm"}
        self._share_early = {}         # viewer side: output that beat its RUN_START
        self._share_win = None
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
                get_docs=self._shared_docs_snapshot,
                on_reconnected=self._on_failover_reconnected,
                on_failed=self._on_failover_failed,
                advertise_socket=p2p_advertise_socket,
            )

        self.pack(fill="both", expand=True)
        self._build_menu()
        self._build_layout()
        self._apply_shortcuts()
        self._apply_output_shortcuts()

        # Both set before the client is bound: binding delivers whatever the
        # client already received (the documents' texts), right away.
        self._pending_initial_file = initial_file
        if self.mode != "solo":
            self._set_status("Syncing...")
        self._bind_client(self.client)
        self._note_assigned_name()

        self._poll_job = self.after(100, self._poll_output)

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
        # Reflects *any* tab having unsaved changes, not just the one currently on screen.
        base = "Peer to Code"
        self.winfo_toplevel().title(f"*{base}*" if self.has_unsaved_tabs() else base)

    def _refresh_dirty(self):
        dirty = self.doc != self._saved_doc
        if dirty != self._dirty:
            self.dirty = dirty

    def _bind_client(self, client):
        """Wire up all of a Client's callbacks. Used at startup, and again
        after a P2P failover promotes/reconnects to a brand-new Client."""
        # Documents' texts and ops are handled on the Tk thread itself (see
        # Client.dispatcher), so they can never interleave with typing.
        client.dispatcher = lambda fn: self.after(0, fn)
        client.on_full_sync = self._apply_full_sync
        client.on_remote_op = self._apply_remote_op
        client.on_doc_rejected = self._on_doc_rejected
        client.on_peers = self._on_peers
        client.on_cursor = self._on_cursor_msg
        client.on_disconnected = self._on_disconnected
        client.on_buffer_context = self._on_buffer_context
        client.on_chat = self._on_chat
        client.on_share = self._on_share

    def _build_menu(self):
        root = self.winfo_toplevel()
        t = self.theme
        a = self._accel

        # On Windows, a menu bar attached the normal way (root.config) is drawn by the OS itself.
        self._use_custom_menubar = sys.platform.startswith(("win", "linux"))

        old_bar = getattr(self, "_custom_menubar", None)
        if old_bar is not None:
            old_bar.destroy()
            self._custom_menubar = None

        self._menus = []
        self._menu_buttons = []

        if self._use_custom_menubar:
            root.config(menu="")
            bar = tk.Frame(self, bg=t["bg"], bd=0, highlightthickness=0)
            # On a rebuild (e.g. after changing shortcuts) the toolbar, status bar and body are already packed.
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
            # Kept so a later live theme switch can repaint every menu without rebuilding them.
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
        self._view_menu = m_view
        m_view.add_command(label="Toggle Explorer", accelerator=a("toggle_explorer"), command=self.toggle_explorer)
        m_view.add_command(label="Toggle Terminal", accelerator=a("toggle_output"), command=self.toggle_console)
        m_view.add_command(label="Toggle Chat", accelerator=a("toggle_chat"), command=self.toggle_chat)
        m_view.add_command(label="Reset Panel Layout", command=self.reset_panel_layout)
        m_view.add_separator()
        m_view.add_command(label="Next Tab", accelerator=a("next_tab"), command=self.action_next_tab)
        m_view.add_command(label="Previous Tab", accelerator=a("prev_tab"), command=self.action_prev_tab)
        if self.mode != "solo":
            self._shared_menu = tk.Menu(m_view, tearoff=0, postcommand=self._fill_shared_menu)
            m_view.add_cascade(label="Shared Buffers", menu=self._shared_menu)
            self._menus.append(self._shared_menu)

        self.menubar = self._custom_menubar if self._use_custom_menubar else menubar
        self._theme_menus(t)

    def _theme_menus(self, t):
        """Repaints every tk.Menu this app owns to match `t`."""
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

    def _fill_shared_menu(self):
        """Lists every document the session carries, so one that has been
        closed locally (it keeps syncing in the background) can be brought
        back, and any of them jumped to."""
        menu = self._shared_menu
        menu.delete(0, "end")
        tabs = sorted((t for t in self._docs.values()), key=lambda t: t.doc_id)
        if not tabs:
            menu.add_command(label="(none)", state="disabled")
        for tab in tabs:
            label = self._shared_menu_title(tab) + ("" if tab.visible else "  (closed)")
            menu.add_command(label=label, command=lambda t=tab: self.action_show_shared(t))

    def _shared_menu_title(self, tab):
        """Name of a document in View > Shared Buffers."""
        if tab.visible or tab.current_file or (tab.shared and tab.filename_hint):
            return self._tab_base_title(tab)
        return f"Untitled (shared buffer {tab.doc_id})"

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
            self._apply_chat_availability()
        if "output_shortcuts" in changed:
            self.output_shortcuts = dict(self.cfg["output_shortcuts"])
            self._apply_output_shortcuts()
        if "theme" in changed:
            try:
                self.apply_theme_live(self.cfg["theme"])
                self._set_status("Theme applied.")
            except tk.TclError:
                self._set_status("Theme saved. Restart to apply.")
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
        if "chat" in changed:
            self._refresh_chat_tags(self.theme["console_bg"])
            self._apply_chat_availability()
            self._update_unread_label()
            self._refresh_chat_state()
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
        font_family = theme.usable_font(t.get("font_family", CODE_FONT))
        self._font_size = int(t.get("font_size", 11))
        self._apply_ttk_style(t)
        toplevel = self.winfo_toplevel()
        toplevel.configure(bg=t["bg"])
        theme.apply_classic_widget_defaults(toplevel, t)

        for frame in (self.toolbar, self.body, self.center, self.edit_outer, self.edit_area, self.text_frame,
                      self.console_body, self.chat_body, self.chat_input_row):
            frame.configure(bg=t["bg"])
        self.dock.set_theme(t)
        self.tabbar.set_theme(t)
        self.interp_label.configure(bg=t["bg"], fg=t["fg"])
        self.peers_label.configure(bg=t["bg"], fg=t["fg"])
        if self.reconnect_entry is not None:
            self.reconnect_entry.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"])
        self.unread_label.configure(bg=t["bg"], fg=t["fg"])
        self.file_path_label.configure(bg=t["bg"], fg=t["muted_fg"])
        self.run_cmd_entry.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"])
        self.linenumbers.configure(bg=t["gutter_bg"])
        self.text.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"],
                             selectbackground=t["sel_bg"], font=(font_family, self._font_size))
        self.yscroll.configure(**theme.classic_scrollbar_options(t))
        self.xscroll.configure(**theme.classic_scrollbar_options(t))
        self.console_yscroll.configure(**theme.classic_scrollbar_options(t))
        self.chat_yscroll.configure(**theme.classic_scrollbar_options(t))
        self.chat_text.configure(bg=t["console_bg"], fg=t["fg"])
        self.chat_entry.configure(bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"],
                                  disabledbackground=t["edit_bg"], disabledforeground=t["muted_fg"])
        self.status_frame.configure(bg=t["status_bg"])
        self.status_right.configure(bg=t["status_bg"], fg=t["status_fg"])
        self.status_left.configure(bg=t["status_bg"], fg=t["status_fg"])
        self._theme_menus(t)
        # Explorer and Terminal now have fully independent font family *and* size.
        self.console.configure(bg=t["console_bg"], fg=t["fg"])
        console_tag_colors = _console_tag_colors(t["console_bg"])
        self.console.tag_configure("stderr", foreground=console_tag_colors["stderr"])
        self.console.tag_configure("info", foreground=console_tag_colors["info"])
        self.console.tag_configure("prompt", foreground=console_tag_colors["prompt"])
        self.ansi_console.base_tag_colors = console_tag_colors
        self._refresh_chat_tags(t["console_bg"])

        self.cursor_layer.set_theme(t["edit_bg"])
        syntax.set_editor_background(t["edit_bg"])
        syntax.configure_tags(self.text)
        self._do_highlight()
        self._redraw_linenumbers()

    def _build_layout(self):
        t = self.theme
        ui_prefs = self.cfg.get("ui", {})
        font_family = theme.usable_font(t.get("font_family", CODE_FONT))
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
        # Unread chat counter, shown just left of the peers label only while
        # chat arrived with the Chat panel hidden.
        self.unread_label = tk.Label(toolbar, text="", bg=t["bg"], fg=t["fg"], cursor="hand2")
        self.unread_label.bind("<Button-1>", lambda e: self._on_unread_click())
        # Shown for as long as run-output sharing is on; click to turn it off.
        self.share_label = tk.Label(toolbar, text="\u25cf Sharing run output (click to stop)", bg="#8b2e2e",
                                    fg="white", padx=6, cursor="hand2")
        self.share_label.bind("<Button-1>", lambda e: self.set_share_output(False))

        self.run_btn = tk.Button(toolbar, text="Run", command=self.action_run, bg="#2f8f5b", fg="white", relief="flat",
                  activebackground="#3aa76a", padx=10)
        self.run_btn.pack(side="left", padx=6, pady=4)
        self.stop_btn = tk.Button(toolbar, text="Stop", command=self.action_stop, bg="#8f3f3f", fg="white", relief="flat",
                  activebackground="#a94e4e", padx=10)
        self.stop_btn.pack(side="left", pady=4)
        self.interp_label = tk.Label(toolbar, text="Interpreter:", bg=t["bg"], fg=t["fg"])
        self.interp_label.pack(side="left", padx=(16, 4))
        self.run_cmd = tk.StringVar(value="")
        self.run_cmd_entry = tk.Entry(toolbar, textvariable=self.run_cmd, width=28, bg=t["edit_bg"], fg=t["fg"],
                                       insertbackground=t["fg"], relief="flat")
        self.run_cmd_entry.pack(side="left", fill="x", expand=True)
        self.run_cmd.trace_add("write", self._on_run_cmd_edited)

        # width=1 keeps the label's requested size fixed no matter how long its text is.
        self.file_path_label = tk.Label(toolbar, text="", bg=t["bg"], fg=t["muted_fg"], anchor="w", width=1)
        self.file_path_label.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self._path_full_text = ""
        self._path_offset = 0
        self._path_drag = None
        for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Button-4>", "<Button-5>",
                    "<Shift-Button-4>", "<Shift-Button-5>"):
            self.file_path_label.bind(seq, self._on_path_wheel)
        self.file_path_label.bind("<ButtonPress-1>", self._on_path_drag_start)
        self.file_path_label.bind("<B1-Motion>", self._on_path_drag)
        self.file_path_label.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_path_drag", None))
        self.file_path_label.bind("<Configure>", lambda _e: self._render_file_path())

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

        center = tk.PanedWindow(body, orient="vertical", bg=t["bg"], sashwidth=4, bd=0)
        body.add(center, minsize=260, stretch="always")
        self.center = center

        edit_outer = tk.Frame(center, bg=t["bg"])
        center.add(edit_outer, minsize=30, stretch="always")
        self.edit_outer = edit_outer

        # Explorer, Terminal and Chat are dockable panels.
        self.dock = DockManager(self, body, center, edit_outer, t, on_visibility=self._on_panel_visibility)
        self.explorer_panel = self.dock.add_panel("explorer")
        self.terminal_panel = self.dock.add_panel("terminal")
        self.chat_panel = self.dock.add_panel("chat")
        self.dock.load(self.cfg)

        self.explorer = FileExplorer(self.explorer_panel.content, self.working_dir, self._open_file_from_explorer,
                                     on_root_change=self._on_explorer_root_change)
        self.explorer.pack(fill="both", expand=True)
        # A plain sans-serif face reads better for a file tree than whatever the ttk theme's own default happens to be.
        self._explorer_font_family = ui_prefs.get("explorer_font_family") or "sans-serif"
        self._explorer_font_size = int(ui_prefs.get("explorer_font_size") or 9)
        self.explorer.set_font(self._explorer_font_family, self._explorer_font_size)
        self.explorer.set_sort(ui_prefs.get("explorer_sort_key", "name"),
                                ui_prefs.get("explorer_sort_reverse", False))
        dnd_support.register_drop(self.explorer, self._on_explorer_drop)
        dnd_support.register_drop(self.explorer.tree, self._on_explorer_drop)
        if self._opened_single_file:
            self.explorer.show_whole_computer()

        self.tabbar = TabBar(edit_outer, t, on_select=self._on_tab_select,
                              on_close=self._on_tab_close_request, on_context=self._show_tab_menu,
                              on_move=self._on_tab_move)
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

        console_body = tk.Frame(self.terminal_panel.content, bg=t["bg"])
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

        # A run's input() prompt is shown right in this pane.
        self.console.mark_set("input_start", "end-1c")
        # Left gravity: text typed exactly at this mark must extend past it rather than push it forward.
        self.console.mark_gravity("input_start", "left")
        # Backspace/Delete are blocked from crossing input_start in _on_console_key below.
        self._console_raw_cmd = install_delete_guard(self.console, "input_start")
        self.console.bind("<Key>", self._on_console_key)
        self.console.bind("<<Paste>>", self._on_console_paste)
        self.console.bind("<Tab>", self._on_console_tab)
        self.console.bind("<Up>", self._on_console_history)
        self.console.bind("<Down>", self._on_console_history)
        self._show_prompt()

        self._console_resize_job = None
        self.console.bind("<Configure>", self._on_console_resize)
        self.terminal_panel.frame.bind("<Configure>", self._on_console_resize)

        self._build_chat_panel(self.chat_panel.content)

        self._update_language_status()
        self._refresh_tabs()

        self.text.bind("<KeyRelease>", self._on_key_release)
        self.text.bind("<ButtonRelease-1>", self._update_cursor_status)
        self.text.bind("<<Undo>>", self.action_undo)
        self.text.bind("<<Redo>>", self.action_redo)
        self.text.bind("<Control-y>", self.action_redo)
        self.text.bind("<<Paste>>", self._on_text_paste)
        self._bind_zoom_gestures()
        self._apply_chat_availability()
        self.dock.present_all()

    def _on_yscroll(self, *args):
        self.text.yview(*args)
        self._redraw_linenumbers()

    def _on_text_yview_changed(self, *args):
        """The Text widget's yscrollcommand, called on every change to the visible range, not just drags of our own scrollbar."""
        self.yscroll.set(*args)
        self._redraw_linenumbers()

    def _on_xscroll(self, *args):
        self.text.xview(*args)
        self._redraw_linenumbers()

    def toggle_explorer(self):
        self.explorer_panel.toggle()

    def toggle_console(self):
        self.terminal_panel.toggle()

    def toggle_chat(self):
        if not self._chat_available():
            self._set_status("Chat is off here (Settings > Chat).")
            return
        self.chat_panel.toggle()
        if self.chat_panel.visible:
            self.chat_entry.focus_set()

    def _chat_available(self):
        """False for the Chat panel in an unshared session (opened from the
        Open tab - nobody else can join it) when Settings > Chat > "Show the
        Chat panel also in an unshared session" is off."""
        return self.mode != "solo" or self._chat_cfg()["show_in_solo"]

    def _apply_chat_availability(self):
        """Shows or hides the Chat panel to match _chat_available(), without
        touching whether the person last had it open or closed."""
        available = self._chat_available()
        self.dock.set_disabled(self.chat_panel, not available)
        try:
            self._view_menu.entryconfigure("Toggle Chat", state="normal" if available else "disabled")
        except (tk.TclError, AttributeError):
            pass
        if not available:
            self._chat_clear_unread()

    def reset_panel_layout(self):
        self.dock.reset()
        self._set_status("Panel layout reset.")

    def _on_panel_visibility(self, panel):
        """Called by the dock after a panel is shown or hidden."""
        if panel is getattr(self, "chat_panel", None) and panel.visible:
            self._chat_clear_unread()

    def _switch_working_dir(self, new_dir):
        """Points the Explorer panel (and Run/Save As dialogs) at a
        different local folder - a local view change only, independent of
        the collaborative session, which keeps running unaffected."""
        if not os.path.isdir(new_dir):
            return
        self.working_dir = new_dir
        self.explorer.set_working_dir(new_dir)

    def _on_explorer_root_change(self, new_dir):
        """The Explorer re-rooted itself (context menu "Open in Explorer"
        or the ".." button): keep the working folder in step, the same as
        _switch_working_dir does for a dropped folder."""
        self.working_dir = new_dir

    def _on_explorer_drop(self, paths):
        for path in paths:
            if os.path.isdir(path):
                self._switch_working_dir(path)
            elif os.path.isfile(path):
                self._load_file(path)

    _GUTTER_MIN_WIDTH = 48
    _GUTTER_PAD = 8

    def _fit_gutter_width(self, font):
        """Widens the line-number gutter to fit the longest line number at the current font size."""
        try:
            last_line = int(self.text.index("end-1c").split(".")[0])
            digits = max(3, len(str(last_line)))
            needed = font.measure("0" * digits) + 2 * self._GUTTER_PAD
        except (tk.TclError, ValueError):
            return
        width = max(self._GUTTER_MIN_WIDTH, needed)
        if int(self.linenumbers.cget("width")) != width:
            self.linenumbers.configure(width=width)

    def _redraw_linenumbers(self, *_):
        self.linenumbers.delete("all")
        gutter_font = (theme.usable_font(self.theme.get("font_family", CODE_FONT)), self._font_size)
        self._fit_gutter_width(tkfont.Font(font=gutter_font))
        x_right = int(self.linenumbers.cget("width")) - self._GUTTER_PAD
        i = self.text.index("@0,0")
        while True:
            dline = self.text.dlineinfo(i)
            if dline is None:
                break
            y = dline[1]
            line_no = str(i).split(".")[0]
            self.linenumbers.create_text(x_right, y, anchor="ne", text=line_no, fill=self.theme["gutter_fg"],
                                          font=gutter_font)
            i = self.text.index(f"{i}+1line")
            if self.text.compare(i, ">=", "end"):
                dline2 = self.text.dlineinfo(i)
                if dline2 is None:
                    break
        if hasattr(self, "cursor_layer"):
            self.cursor_layer.refresh()

    def _bind_zoom_gestures(self):
        """Ctrl+scroll wheel and pinch each change the font size of whichever of the three panels they're used over."""
        self._bind_zoom((self.text, self.linenumbers),
                         lambda steps: self._set_font_size(self._font_size + steps))
        self._bind_zoom((self.explorer.tree,),
                         lambda steps: self._set_explorer_font_size(self._explorer_font_size + steps))
        # Chat shares the Terminal's font, so zooming either one zooms both.
        self._bind_zoom((self.console, self.chat_text, self.chat_entry),
                         lambda steps: self._set_console_font_size(self._console_font_size + steps))

    def _bind_zoom(self, widgets, zoom_by):
        """Coalesce a burst of pinch/wheel events into a single zoom step."""
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
        font_family = theme.usable_font(self.theme.get("font_family", CODE_FONT))
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
        self._apply_chat_font()

    def _set_console_font_family(self, family):
        if not family or family == self._console_font_family:
            return
        self._console_font_family = family
        self.console.configure(font=(family, self._console_font_size))
        self.ansi_console.rescale_fonts(family=family)
        self._apply_chat_font()

    def _apply_chat_font(self):
        font = (self._console_font_family, self._console_font_size)
        self.chat_text.configure(font=font)
        self.chat_entry.configure(font=font)

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
                # Cursor positions are offsets into the shared document - meaningless while a local tab is showing.
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
            self.client.send_cursor(offset, has_sel, start, end, doc=self._active.doc_id)

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

    def _use_global_interpreter(self):
        return bool(self.cfg.get("editor", {}).get("use_global_interpreter", False))

    def _on_run_cmd_edited(self, *_args):
        if self._suppress_run_cmd_trace:
            return
        value = self.run_cmd.get().strip()
        if self._use_global_interpreter():
            self.cfg.setdefault("editor", {})["global_interpreter"] = value
            return
        key = self._run_command_key(self._current_language())
        if value:
            self._run_commands[key] = value
        else:
            self._run_commands.pop(key, None)

    def _recall_run_command(self, language):
        """Puts the interpreter for `language` into the Interpreter box."""
        if self._use_global_interpreter():
            target = self.cfg.get("editor", {}).get("global_interpreter", "")
        else:
            key = self._run_command_key(language)
            target = self._run_commands.get(key, "")
            if target and interpreters.is_self_executable(target, self.working_dir):
                self._run_commands.pop(key, None)
                target = ""
            if not target:
                target = interpreters.detect(language)
                if target:
                    self._run_commands[key] = target
        if target == self.run_cmd.get():
            return
        self._suppress_run_cmd_trace = True
        try:
            self.run_cmd.set(target)
        finally:
            self._suppress_run_cmd_trace = False

    def _ask_set_interpreter(self):
        """Small prompt shown when Run finds no usable interpreter. Returns
        True if the person wants to set it by hand."""
        t = self.theme
        top = self.winfo_toplevel()
        dlg = tk.Toplevel(top)
        dlg.title("Run")
        dlg.configure(bg=t["panel_bg"], highlightthickness=1,
                      highlightbackground=t["border"], highlightcolor=t["border"])
        dlg.transient(top)
        dlg.resizable(False, False)
        answer = {"set": False}

        def finish(value):
            answer["set"] = value
            dlg.destroy()

        tk.Label(dlg, text="Interpreter not found.\nSet it manually?", bg=t["panel_bg"], fg=t["fg"],
                 justify="left").pack(padx=24, pady=(18, 12))
        buttons = tk.Frame(dlg, bg=t["panel_bg"])
        buttons.pack(pady=(0, 14))
        set_btn = tk.Button(buttons, text="Set manually", command=lambda: finish(True), bg=t["panel_bg"],
                            fg=t["fg"], activebackground=t["sel_bg"], activeforeground=t["fg"], padx=10)
        set_btn.pack(side="left", padx=6)
        tk.Button(buttons, text="Cancel", command=lambda: finish(False), bg=t["panel_bg"], fg=t["fg"],
                  activebackground=t["sel_bg"], activeforeground=t["fg"], padx=10).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda _e: finish(True))
        dlg.bind("<Escape>", lambda _e: finish(False))
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))

        dlg.update_idletasks()
        x = top.winfo_rootx() + max(0, (top.winfo_width() - dlg.winfo_reqwidth()) // 2)
        y = top.winfo_rooty() + max(0, (top.winfo_height() - dlg.winfo_reqheight()) // 3)
        dlg.geometry(f"+{x}+{y}")
        set_btn.focus_set()
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass
        dlg.wait_window()
        return answer["set"]

    def _focus_interpreter_entry(self):
        self.run_cmd_entry.focus_set()
        self.run_cmd_entry.select_range(0, "end")
        self.run_cmd_entry.icursor("end")

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
            self._shift_peer_cursors(op, doc_id=self._active.doc_id)
            self.client.local_edit(op, self._active.doc_id)

    def _shift_peer_cursors(self, op, author=None, doc_id=None):
        """Keeps the other people's carets on the same text when a shared
        document changes under them (see PeerCursorLayer.apply_op)."""
        layer = getattr(self, "cursor_layer", None)
        if layer is not None:
            layer.apply_op(op.ops, author, doc_id)

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
            self._shift_peer_cursors(op, doc_id=self._active.doc_id)
            self.client.local_edit(op, self._active.doc_id)

    def _reset_history(self):
        self.history.reset()
        self._saved_doc = self.doc
        self._refresh_dirty()

    def _apply_full_sync(self, doc_id, text, meta=None):
        """A shared document's complete text arrived."""
        meta = meta or {}
        mine = meta.get("by") == self.client.client_id
        tab = self._docs.get(doc_id)
        if tab is None and mine and meta.get("ref") in self._pending_shares:
            tab = self._bind_pending_share(doc_id, text, meta["ref"])
        elif tab is None:
            tab = self._open_remote_document(doc_id, text, meta)
        elif tab is self._active:
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
            tab.local_edited = False
        else:
            # A different tab is showing: only this tab's stored copy needs
            # to follow along.
            tab.doc = text
            tab.history.reset()
            tab.saved_doc = text
            tab.local_edited = False
            self._refresh_tabs()
        if self.mode != "solo":
            self._set_status("Connected.")
        if self._pending_initial_file:
            path, self._pending_initial_file = self._pending_initial_file, None
            self._load_file(path, into_shared=True)
        else:
            self._update_file_path_label()

    def _bind_pending_share(self, doc_id, text, ref):
        """The server answered our share with the new document's id: the tab
        we sent becomes that document's tab. Anything typed into it since
        we sent it follows as an ordinary edit."""
        tab = self._pending_shares.pop(ref)
        tab.pending_ref = None
        tab.doc_id = doc_id
        self._docs[doc_id] = tab
        if tab not in self._tabs:
            # Closed while the request was in flight: the document exists
            # now, so it is tracked like any other, just not shown.
            self._tabs.append(tab)
            tab.visible = False
        if tab.doc != text:
            self.client.local_edit(ot.diff_to_op(text, tab.doc), doc_id)
        if tab is self._active:
            self._after_role_change()
        else:
            self._refresh_tabs()
        return tab

    def _open_remote_document(self, doc_id, text, meta):
        """A document we have no tab for yet."""
        filename = meta.get("filename")
        same = next((t for t in self._tabs
                     if not t.shared and t.pending_ref is None and t.current_file is not None
                     and filename and self._hint_for_tab(t) == filename and t.doc == text), None)
        if same is not None:
            tab = same
            tab.doc_id = doc_id
            tab.filename_hint = filename
            tab.visible = True
        else:
            tab = BufferTab(doc_id=doc_id, doc=text)
            tab.filename_hint = filename
            self._tabs.append(tab)
        self._docs[doc_id] = tab
        if tab is self._active:
            self._after_role_change()
        else:
            self._refresh_tabs()
        if meta.get("fresh") and meta.get("by") != self.client.client_id:
            who = self._known_peer_names.get(meta.get("by"), "someone")
            label = sanitize_text(filename, 200) if filename else "a new buffer"
            self._chat_append([(f"{sanitize_name(who)} shared {label}\n", "info")])
            self._set_status(f"{sanitize_name(who)} shared {label}")
        return tab

    def _on_doc_rejected(self, ref, reason):
        self.after(0, lambda: self._handle_doc_rejected(ref, reason))

    def _handle_doc_rejected(self, ref, reason):
        tab = self._pending_shares.pop(ref, None)
        if tab is not None:
            tab.pending_ref = None
        self._set_status(reason)
        messagebox.showwarning("Share", reason)

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

    def _apply_remote_op(self, doc_id, op, author=None, swap=None):
        if doc_id not in self._docs:
            return
        if swap is not None:
            self._begin_remote_swap(doc_id, swap, author)
        tab = self._docs[doc_id]
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
            # The shared buffer isn't on screen - keep its stored copy current so it's right when it is shown again.
            was_dirty = tab.dirty
            before = tab.doc
            tab.doc = op.apply(before)
            tab.history.record(author_key, op, before)
            if tab.dirty != was_dirty:
                self._refresh_tabs()
        # Their carets are offsets into the shared document, so they follow
        # this edit whether or not that document is the buffer on screen.
        self._shift_peer_cursors(op, author, doc_id)
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
            self._shift_peer_cursors(op, doc_id=self._active.doc_id)
            self.client.local_edit(op, self._active.doc_id)
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
            self._set_status("Nothing to undo.")
            return "break"
        op = self.history.undo_peer(author, self.doc)
        if op is not None:
            self._apply_history_op(op)
            self._set_status(f"Undid {self._author_display(author)}'s last change")
        return "break"

    def _on_peers(self, peers):
        self.after(0, lambda: self._render_peers(peers))

    def _render_peers(self, peers, note_failover=True):
        # Names come from other machines: sanitize before they reach the
        # chat, the peers label, cursor labels or the chat roster.
        peers = [dict(pr, name=sanitize_name(pr.get("name"))) for pr in peers]
        self._roster = peers
        others = [pr for pr in peers if pr["id"] != self.client.client_id]
        current = {pr["id"]: pr["name"] for pr in others}
        if self._chat_cfg()["show_join_leave"]:
            for pid, name in current.items():
                if pid not in self._known_peer_names:
                    self._chat_append([(f"{name} joined the session\n", "info")])
            for pid, name in self._known_peer_names.items():
                if pid not in current:
                    self._chat_append([(f"{name} disconnected\n", "info")])
        self._known_peer_names = current
        self._author_names.update(current)
        names = ", ".join(current.values())
        self.peers_label.configure(text=f"{len(others)} connected: {names}" if others else "")
        self.cursor_layer.set_roster(peers)
        if note_failover and self._failover is not None:
            self._failover.note_roster(peers)

    def _notify(self, text, error=False):
        """Tells the person something the app itself has to say. The
        Terminal only holds commands and their output, so notices go to
        Chat - or to the status bar when Chat isn't available or is off."""
        cfg = self._chat_cfg()
        if getattr(self, "chat_text", None) is not None and self._chat_available() and cfg["enabled"]:
            self._chat_append([(text + "\n", "stderr" if error else "info")], force_scroll=True)
            if error:
                self._set_status(text)
        else:
            self._set_status(text)

    # Terminal helpers

    def _console_insert_segments(self, segs):
        """Prints plain-text segments *above* the live input line."""
        c = self.console
        line_start = c.index("input_start linestart")
        at_line_start = c.compare(line_start, "==", "input_start")
        c.mark_set("chat_ins", line_start)
        c.mark_gravity("chat_ins", "right")
        for text, tag in segs:
            # Through the raw command: the delete guard would clamp an
            # insert before input_start up to input_start.
            c.tk.call(self._console_raw_cmd, "insert", "chat_ins", text, self._tags_arg(tag))
        if at_line_start:
            # Nothing before input_start on its line: it (left gravity) would
            # otherwise stay in front of what was just inserted.
            c.mark_set("input_start", "chat_ins")
        c.mark_unset("chat_ins")
        c.see("end")

    def _console_plain_write(self, segs):
        """Like _console_write but for plain segments, at the very end
        (right after the line the person just submitted)."""
        c = self.console
        for text, tag in segs:
            c.insert("end-1c", text, self._tags_arg(tag))
        c.mark_set("input_start", "end-1c")
        c.see("end")

    # Chat panel

    CHAT_MAX_LINES = 5000

    def _build_chat_panel(self, parent):
        t = self.theme
        # The input row is packed first so it keeps its room when the panel
        # gets short.
        self.chat_input_row = tk.Frame(parent, bg=t["bg"])
        self.chat_input_row.pack(side="bottom", fill="x", padx=2, pady=(0, 2))
        self.chat_body = tk.Frame(parent, bg=t["bg"])
        self.chat_body.pack(side="top", fill="both", expand=True, padx=2, pady=2)

        self.chat_yscroll = tk.Scrollbar(self.chat_body, orient="vertical", **theme.classic_scrollbar_options(t))
        self.chat_yscroll.pack(side="right", fill="y")
        font = (self._console_font_family, self._console_font_size)
        self.chat_text = tk.Text(self.chat_body, height=4, wrap="word", state="disabled", bg=t["console_bg"],
                                  fg=t["fg"], relief="flat", font=font, padx=4, pady=2,
                                  yscrollcommand=self.chat_yscroll.set)
        self.chat_text.pack(side="left", fill="both", expand=True)
        self.chat_yscroll.config(command=self.chat_text.yview)
        # A disabled Text doesn't take focus on click, which would leave
        # Ctrl+C with nothing to copy from.
        self.chat_text.bind("<Button-1>", lambda e: self.chat_text.focus_set(), add="+")

        self.chat_entry = tk.Entry(self.chat_input_row, bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"],
                                    disabledbackground=t["edit_bg"], disabledforeground=t["muted_fg"],
                                    relief="flat", font=font)
        self.chat_entry.pack(fill="x", expand=True, ipady=3)
        for seq in ("<Return>", "<KP_Enter>"):
            self.chat_entry.bind(seq, self._chat_submit)
        self.chat_entry.bind("<Tab>", self._on_chat_tab)
        self.chat_entry.bind("<FocusIn>", self._on_window_focus_in, add="+")
        self.winfo_toplevel().bind("<FocusIn>", self._on_window_focus_in, add="+")
        self.chat_entry.bind("<Up>", self._on_chat_history)
        self.chat_entry.bind("<Down>", self._on_chat_history)

        self._refresh_chat_tags(t["console_bg"])
        self._chat_append([("Type a message, Enter to send. /help lists commands.\n", "info")])
        self._refresh_chat_state()

    def _refresh_chat_state(self):
        """Input enabled/disabled to match Settings > Chat > Enable chat."""
        enabled = self._chat_cfg()["enabled"]
        self.chat_entry.configure(state="normal" if enabled else "disabled")
        if self._chat_was_enabled is not None and enabled != self._chat_was_enabled:
            self._chat_append([("Chat turned on.\n" if enabled else
                                "Chat off. Turn it on in Settings > Chat.\n", "info")], force_scroll=True)
        self._chat_was_enabled = enabled

    def _chat_cfg(self):
        return chat_settings(self.cfg)

    def _on_chat(self, payload):
        # Network thread: marshal onto Tk's, like every other callback.
        self.after(0, lambda: self._show_chat(payload))

    def _note_assigned_name(self):
        got = getattr(self.client, "username", None)
        want = getattr(self.client, "requested_username", None)
        if got and want and got != want:
            self._chat_append(
                [(f"Name '{sanitize_text(want, 40)}' is taken. You are '{got}'.\n", "info")])

    # drawing

    @staticmethod
    def _tags_arg(tag):
        if not tag:
            return ()
        return (tag,) if isinstance(tag, str) else tuple(tag)

    def _refresh_chat_tags(self, console_bg):
        """Configure chat tags for the console background."""
        light = syntax.brightness(console_bg) >= 0.5
        colors = _console_tag_colors(console_bg)
        c = self.chat_text
        c.tag_configure("info", foreground=colors["info"])
        c.tag_configure("stderr", foreground=colors["stderr"])
        c.tag_configure("chat_time", foreground=colors["info"])
        c.tag_configure("chat_link", foreground="#0b5cad" if light else "#6cb6ff", underline=True)
        c.tag_bind("chat_link", "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind("chat_link", "<Leave>", lambda e: c.configure(cursor="xterm"))
        for pid, color in self._chat_name_tags.items():
            c.tag_configure(f"chat_name_{pid}", foreground=adapt_color(color, light) or colors["info"])

    def _chat_name_tag(self, peer_id):
        """The text tag that colors a name with its peer's assigned color, or
        "info" when coloring is off or the peer is no longer in the roster."""
        if not self._chat_cfg()["color_names"]:
            return "info"
        color = next((pr.get("color") for pr in self._roster if pr.get("id") == peer_id), None)
        if adapt_color(color, False) is None:
            return "info"
        self._chat_name_tags[peer_id] = color
        light = syntax.brightness(self.theme["console_bg"]) >= 0.5
        self.chat_text.tag_configure(f"chat_name_{peer_id}", foreground=adapt_color(color, light))
        return f"chat_name_{peer_id}"

    def _chat_ref_tag(self, file, line):
        self._chat_link_n += 1
        tag = f"chat_ref_{self._chat_link_n}"
        self.chat_text.tag_bind(tag, "<Button-1>", lambda e, f=file, n=line: self._follow_chat_ref(f, n))
        return tag

    def _chat_body_segments(self, text):
        """The message text, with `:42` / `file.py:42` tokens turned into
        clickable links (see _follow_chat_ref)."""
        segs, pos = [], 0
        for start, end, file, line in find_code_refs(text):
            if start > pos:
                segs.append((text[pos:start], None))
            segs.append((text[start:end], ("chat_link", self._chat_ref_tag(file, line))))
            pos = end
        if pos < len(text):
            segs.append((text[pos:], None))
        return segs

    def _chat_segments(self, msg, force_time=False):
        """[(text, tag)] for one incoming message line: `[name] text` or
        `[name -> you] text`, optionally prefixed with HH:MM."""
        segs = []
        if (force_time or self._chat_cfg()["show_timestamps"]) and msg.get("ts"):
            segs.append((time.strftime("%H:%M ", time.localtime(msg["ts"])), "chat_time"))
        segs.append(("[", "info"))
        segs.append((sanitize_name(msg.get("name")), self._chat_name_tag(msg.get("from"))))
        if msg.get("private"):
            me = getattr(self.client, "username", None)
            to = [("you" if n == me else sanitize_name(n)) for n in msg.get("to", [])] or ["you"]
            segs.append((" -> " + ", ".join(to) + "] ", "info"))
        else:
            segs.append(("] ", "info"))
        segs += self._chat_body_segments(sanitize_text(msg.get("text", "")))
        segs.append(("\n", None))
        return segs

    def _chat_append(self, segs, force_scroll=False):
        """Adds [(text, tag)] segments at the end of the Chat panel."""
        c = self.chat_text
        at_bottom = c.yview()[1] >= 0.999
        c.configure(state="normal")
        try:
            for text, tag in segs:
                c.insert("end", text, self._tags_arg(tag))
            lines = int(c.index("end-1c").split(".")[0])
            if lines > self.CHAT_MAX_LINES:
                c.delete("1.0", f"{lines - self.CHAT_MAX_LINES}.0")
        finally:
            c.configure(state="disabled")
        if force_scroll or at_bottom:
            c.see("end")

    # receiving

    def _chat_muted(self, name, cfg=None):
        cfg = cfg or self._chat_cfg()
        low = (name or "").lower()
        return any(m.lower() == low for m in cfg["muted"])

    def _show_chat(self, payload):
        cfg = self._chat_cfg()
        if not cfg["enabled"] or not isinstance(payload, dict) or not self._chat_available():
            return
        if "error" in payload:
            text = sanitize_text(payload["error"], 300)
            self._chat_append([(text + "\n", "stderr")])
            if not self.chat_panel.visible:
                self._set_status(text)
            return
        if "history" in payload:
            msgs = [m for m in payload["history"] if not self._chat_muted(m.get("name"), cfg)]
            if msgs:
                segs = [(f"-- chat history ({len(msgs)}) --\n", "info")]
                for m in msgs:
                    segs += self._chat_segments(m, force_time=True)
                segs.append(("-- end of history --\n", "info"))
                self._chat_append(segs)   # history never notifies
            return
        if self._chat_muted(payload.get("name"), cfg):
            return
        if payload.get("private"):
            self._last_private = (payload.get("from"), sanitize_name(payload.get("name")))
        self._chat_append(self._chat_segments(payload))
        if not self._chat_is_seen():
            self._chat_note_unread(cfg)

    # unread counter

    def _chat_is_seen(self):
        """True when a message arriving right now is actually in front of the person."""
        if not self.chat_panel.visible:
            return False
        try:
            if self.winfo_toplevel().state() == "iconic":
                return False
            return self.focus_displayof() is not None
        except tk.TclError:
            return True

    def _on_window_focus_in(self, event):
        """The window (or the Chat input) got the focus back: whatever
        piled up while the panel was open but unattended is now seen."""
        try:
            if not self.winfo_exists():     # a previous session's binding on a reused window
                return
            if event.widget in (self.winfo_toplevel(), getattr(self, "chat_entry", None)) \
                    and self.chat_panel.visible:
                self._chat_clear_unread()
        except tk.TclError:
            pass

    def _chat_note_unread(self, cfg):
        self._chat_unread += 1
        self._update_unread_label()
        if cfg["notify"] and not cfg["do_not_disturb"]:
            self._flash_unread()
            if cfg["notify_bell"]:
                try:
                    self.bell()
                except tk.TclError:
                    pass

    def _update_unread_label(self):
        if self._chat_unread > 0:
            self.unread_label.configure(text=f"Chat: {self._chat_unread} unread")
            self.unread_label.pack(side="right", padx=(16, 0), after=self.peers_label)
        else:
            self.unread_label.pack_forget()

    def _flash_unread(self, steps=6):
        if self._chat_flash_job is not None:
            self.after_cancel(self._chat_flash_job)
            self._chat_flash_job = None
        t = self.theme

        def step(i):
            self._chat_flash_job = None
            if i >= steps or self._chat_unread == 0:
                self.unread_label.configure(bg=t["bg"], fg=t["fg"])
                return
            on = i % 2 == 0
            self.unread_label.configure(bg="#c9a227" if on else t["bg"], fg="black" if on else t["fg"])
            self._chat_flash_job = self.after(250, lambda: step(i + 1))
        step(0)

    def _chat_clear_unread(self):
        self._chat_unread = 0
        self._update_unread_label()

    def _on_unread_click(self):
        if self.chat_panel.visible:
            self._chat_clear_unread()
        else:
            self.toggle_chat()

    # sending / commands

    def _chat_label_for(self, token):
        if token.startswith("#") and token[1:].isdigit():
            for pr in self._roster:
                if pr.get("id") == int(token[1:]):
                    return pr["name"]
        return sanitize_text(token, 40)

    def _chat_submit(self, _event=None):
        """Enter in the Chat input: a plain line is a message to everyone,
        `/...` is a command (see chat_util.parse_chat_input)."""
        line = self.chat_entry.get()
        self.chat_entry.delete(0, "end")
        if not line.strip():
            return "break"
        hist = self._chat_input_history
        if not hist or hist[-1] != line:
            hist.append(line)
        self._chat_input_pos = None
        if not self._chat_cfg()["enabled"]:
            self._chat_error("Chat is turned off (Settings > Chat).")
            return "break"
        try:
            cmd = parse_chat_input(line)
        except ValueError as err:
            self._chat_error(str(err))
            return "break"
        if cmd is not None:
            self._handle_chat_command(cmd)
        return "break"

    def _handle_chat_command(self, cmd):
        if cmd.kind == "list":
            self._chat_list_users()
        elif cmd.kind in ("mute", "unmute"):
            self._chat_set_muted(cmd.text, cmd.kind == "mute")
        elif cmd.kind == "help":
            self._chat_append([(line + "\n", "info") for line in CHAT_HELP], force_scroll=True)
        else:
            self._chat_send(cmd)

    def _chat_error(self, text):
        self._chat_append([(text + "\n", "stderr")], force_scroll=True)

    def _chat_send(self, cmd):
        text = sanitize_text(cmd.text)
        to = list(cmd.to)
        if cmd.reply:
            if not self._last_private:
                self._chat_error("No private message to reply to yet.")
                return
            to = [f"#{self._last_private[0]}"]
        if not text:
            self._chat_error("Nothing to send. Type /help for the chat commands.")
            return
        if not self.client.send_chat(text, to or None):
            self._chat_error("Not connected to a session.")
            return
        label = "you"
        if to:
            label = "you -> " + ", ".join(self._chat_label_for(t) for t in to)
        segs = []
        if self._chat_cfg()["show_timestamps"]:
            segs.append((time.strftime("%H:%M "), "chat_time"))
        segs.append((f"[{label}] ", "info"))
        segs += self._chat_body_segments(text)
        segs.append(("\n", None))
        self._chat_append(segs, force_scroll=True)

    def _chat_list_users(self):
        peers = sorted(self._roster, key=lambda pr: pr.get("id", 0))
        if not peers:
            self._chat_append([("Nobody is connected.\n", "info")], force_scroll=True)
            return
        segs = [(f"{len(peers)} connected:\n", "info")]
        for pr in peers:
            segs.append((f"  #{pr['id']} ", "info"))
            segs.append((pr["name"], self._chat_name_tag(pr["id"])))
            if pr["id"] == self.client.client_id:
                segs.append((" (you)", "info"))
            segs.append(("\n", None))
        self._chat_append(segs, force_scroll=True)

    def _chat_set_muted(self, user, mute):
        name = self._chat_label_for(user)
        for pr in self._roster:  # canonical spelling if they're connected
            if pr["name"].lower() == name.lower():
                name = pr["name"]
        section = self.cfg.get("chat")
        if not isinstance(section, dict):
            section = self.cfg["chat"] = dict(config.DEFAULTS["chat"])
        muted = list(self._chat_cfg()["muted"])
        already = any(m.lower() == name.lower() for m in muted)
        if mute and not already:
            muted.append(name)
        elif not mute:
            muted = [m for m in muted if m.lower() != name.lower()]
        section["muted"] = muted
        try:
            config.save_config(self.cfg)
        except Exception:
            pass
        if mute:
            self._chat_append([(f"Muted {name}. /unmute {name} to undo.\n", "info")],
                              force_scroll=True)
        else:
            self._chat_append([(f"Unmuted {name}.\n", "info")], force_scroll=True)

    # links in messages

    def _follow_chat_ref(self, file, line):
        """Click on `:42` / `file.py:42`: jump to that line. Only ever moves
        the caret in a tab that's already open; nothing from the message is
        opened, executed or looked up on disk."""
        tab, note = None, ""
        if file:
            base = os.path.basename(file).lower()
            tab = next((t for t in self.visible_tabs if self._tab_base_title(t).lower() == base), None)
            if tab is None:
                note = f" ('{sanitize_text(file, 60)}' isn't open; showing the shared buffer)"
        if tab is None:
            tab = self._active if self._active.shared else next(
                (t for t in self._docs.values() if t.visible), next(iter(self._docs.values()), None))
        if tab is None:
            self._set_status("There is no shared buffer.")
            return
        if not tab.visible:
            self._set_status("The shared buffer is closed.")
            return
        if tab is not self._active:
            self._switch_to(tab)
        last = int(self.text.index("end-1c").split(".")[0])
        n = min(max(int(line), 1), last)
        self.text.mark_set("insert", f"{n}.0")
        self.text.see("insert")
        self.text.focus_set()
        self._set_status(f"Jumped to line {n}{note}")

    # Tab completion / input history

    def _on_chat_tab(self, _event=None):
        """Tab after `/p `: completes user names (or #ids) from the roster.
        Returns "break" when the line is in that context and None otherwise
        (so Tab moves focus on as usual)."""
        entry = self.chat_entry
        if not self._chat_cfg()["enabled"]:
            return None
        pos = entry.index("insert")
        ctx = completion_context(entry.get()[:pos])
        if ctx is None:
            return None
        _prefix, fragment, quote = ctx
        me = self.client.client_id
        names = [pr["name"] for pr in self._roster if pr.get("id") != me]
        if fragment.startswith("#"):
            names = [f"#{pr['id']}" for pr in self._roster if pr.get("id") != me]
        low = fragment.lower()
        cands = sorted((n for n in names if n.lower().startswith(low)), key=str.lower)
        if not cands:
            return "break"
        if len(cands) == 1:
            new = format_name_for_command(cands[0], quote, close=True) + " "
        else:
            common = os.path.commonprefix([n.lower() for n in cands])
            if len(common) > len(fragment):
                new = format_name_for_command(cands[0][:len(common)], quote, close=False)
            else:
                self._chat_append([("   ".join(cands) + "\n", "info")], force_scroll=True)
                return "break"
        start = pos - len(fragment) - len(quote)
        entry.delete(start, pos)
        entry.insert(start, new)
        return "break"

    def _on_chat_history(self, event):
        """Up/Down in the Chat input: recall what was sent before."""
        hist = self._chat_input_history
        if not hist:
            return "break"
        if self._chat_input_pos is None:
            if event.keysym != "Up":
                return "break"
            self._chat_input_stash = self.chat_entry.get()
            self._chat_input_pos = len(hist) - 1
        else:
            self._chat_input_pos += -1 if event.keysym == "Up" else 1
        self._chat_input_pos = max(self._chat_input_pos, 0)
        if self._chat_input_pos >= len(hist):
            self._chat_input_pos = None
            text = self._chat_input_stash
        else:
            text = hist[self._chat_input_pos]
        self.chat_entry.delete(0, "end")
        self.chat_entry.insert(0, text)
        self.chat_entry.icursor("end")
        return "break"

    # Shared run output (opt-in)

    def set_share_output(self, on, confirm=False):
        """Turns sharing on/off; returns the resulting state. With confirm=True
        (the Settings checkbox) turning it on asks first, showing the warning."""
        on = bool(on)
        if on == self._share_on:
            return on
        if on:
            if not self.client.server_addr:
                self._notify("Can't share: not connected.", error=True)
                return False
            if confirm and not messagebox.askyesno("Share output", SHARE_WARNING + "\n\nShare it?",
                                                   parent=self.winfo_toplevel()):
                return False
            self._share_on = True
            self.share_label.pack(side="right", padx=(16, 0), after=self.peers_label)
            self._notify("Sharing ON for new runs. Everyone sees the output, secrets included. "
                         "'share off' stops it.")
        else:
            self._share_on = False
            self.share_label.pack_forget()
            self._share_stop("sharing turned off")
            self._notify("Sharing OFF.")
        return self._share_on

    def _share_command(self, action):
        if action == "on":
            self.set_share_output(True)
        elif action == "off":
            self.set_share_output(False) if self._share_on else self._notify("Sharing is already off.")
        elif action == "view":
            self._share_window(show=True)
        else:
            cfg = self._chat_cfg()
            self._notify(f"Your output: {'shared' if self._share_on else 'not shared'}. "
                         f"Others' output: {'shown' if cfg['view_shared'] else 'hidden'}. "
                         "'share view' opens it.")

    def _share_window(self, show=False):
        win = self._share_win
        try:
            alive = win is not None and win.winfo_exists()
        except tk.TclError:
            alive = False
        if not alive:
            win = self._share_win = SharedOutputWindow(self)
            if not show:
                win.withdraw()
        if show:
            win.deiconify()
            win.lift()
        return win

    # runner side

    def _share_begin(self, args, shell):
        """Called right after a process started. Announces the run (title =
        program name only) if sharing is on."""
        self._share = None
        if not self._share_on or not self.client.server_addr:
            return
        self._share_run_counter += 1
        title = share_title(args, shell)
        self._share = {"run": self._share_run_counter, "streamer": ShareStreamer(self._share_run_counter, title)}
        self.client.send_share_start(self._share_run_counter, title)

    def _share_output(self, pairs):
        """One call per poll tick with everything read in that tick."""
        share = self._share
        if not share or not pairs:
            return
        for seq, segs, truncated in share["streamer"].add(pairs):
            self.client.send_share_output(share["run"], seq, segs, truncated=truncated)

    def _share_stop(self, reason, code=None):
        share, self._share = self._share, None
        if share:
            self.client.send_share_end(share["run"], share["streamer"].end_seq(), code=code, reason=reason)

    # viewer side

    def _on_share(self, name, msg):
        self.after(0, lambda: self._show_share(name, msg))

    def _show_share(self, name, msg):
        cfg = self._chat_cfg()
        if not cfg["view_shared"] or msg.get("from") == self.client.client_id:
            return
        if self._chat_muted(msg.get("name"), cfg):
            return
        sender = msg["from"]
        st = self._share_runs.get(sender)
        if name == "start":
            win = self._share_window()
            st = {"run": msg["run"], "title": msg["title"], "name": msg["name"], "asm": RunAssembler(msg["from_seq"])}
            self._share_runs[sender] = st
            win.start_run(sender, msg["name"], msg["title"])
            if msg["from_seq"] > 1:
                win.note(sender, msg["name"], "[earlier output of this run is no longer available]")
            if not msg["replay"]:
                self._notify(f"{msg['name']} is sharing a run ({msg['title']}). 'share view' to watch.")
            for early_name, early in self._share_early.pop((sender, msg["run"]), []):
                self._share_feed(sender, st, early_name, early)
            return
        if st is None or st["run"] != msg["run"]:
            if st is None or msg["run"] > st["run"]:    # START not here yet: keep a little
                key = (sender, msg["run"])
                if key not in self._share_early and len(self._share_early) >= 16:
                    self._share_early.pop(next(iter(self._share_early)))
                bucket = self._share_early.setdefault(key, [])
                if len(bucket) < SHARE_PENDING_MAX:
                    bucket.append((name, msg))
            return
        self._share_feed(sender, st, name, msg)

    def _share_feed(self, sender, st, name, msg):
        item = {"kind": name, "msg": msg, "end": name == "end",
                "truncated": name == "output" and bool(msg.get("truncated"))}
        self._share_apply(sender, st, st["asm"].feed(msg["seq"], item))

    def _share_apply(self, sender, st, items):
        if not items:
            return
        win = self._share_window()
        who = st["name"]
        for item in items:
            if item.get("gap"):
                win.note(sender, who, "[some output was lost in transit]")
                continue
            msg = item["msg"]
            if item["kind"] == "output":
                if item["truncated"]:
                    win.note(sender, who, "[output limit reached - the rest of this run is not shared]")
                elif msg.get("skipped"):
                    win.note(sender, who, f"[{msg['skipped']} bytes of output omitted: sender rate limit]")
                for stream, text in msg.get("segs", []):
                    # render_sgr honours only plain colour codes; this is a second sanitize pass.
                    runs, st["asm"].sgr_state = render_sgr(sanitize_output(text), st["asm"].sgr_state)
                    win.append(sender, who, runs, stream)
            elif item["kind"] == "end":
                code = msg.get("code")
                if msg.get("reason"):
                    status, line = "ended", f"[ended: {msg['reason']}]"
                elif code in (None, 0):
                    status, line = "finished", "[finished]"
                else:
                    status, line = "finished", f"[finished: exit code {code}]"
                win.set_status(sender, who, st["title"], status)
                win.note(sender, who, line)

    def _share_tick(self):
        for sender, st in list(self._share_runs.items()):
            self._share_apply(sender, st, st["asm"].release_stalled())

    def _on_cursor_msg(self, payload):
        self.after(0, lambda: self.cursor_layer.update_cursor(payload))

    def _on_disconnected(self, reason):
        self.after(0, lambda: self._connection_lost(reason))

    def _connection_lost(self, reason):
        self._render_peers([], note_failover=False)
        self._known_peer_names = {}
        if self._failover is not None:
            self.peers_label.configure(text="Reconnecting...")
            self._set_status(f"{reason} Reconnecting...")
            self._notify(f"{reason} Reconnecting...")
            self._failover.begin(self.client, reason)
            return
        self._handle_disconnected(reason)

    def _handle_disconnected(self, reason):
        self.peers_label.configure(text="Disconnected from host")
        try:
            self.client.transport.stop()
        except Exception:
            pass
        if self.mode in ("client", "p2p"):
            self._show_reconnect_ui()
        messagebox.showwarning("Disconnected", reason)

    # Reconnect

    def _reconnect_default_address(self):
        """What to prefill the address field with."""
        conn = self.cfg.get("connection", {})
        saved = conn.get("address" if self.mode == "client" else "p2p_join_address")
        if saved:
            return saved
        addr = getattr(self.client, "server_addr", None)
        if addr and self.mode == "client":
            return f"{addr[0]}:{addr[1]}"
        return ""

    def _show_reconnect_ui(self):
        """Turns the toolbar's Disconnect button into Reconnect, with an
        address field just left of it. File > Disconnect still leaves the
        session for good."""
        if self.reconnect_entry is not None:
            return
        t = self.theme
        entry = tk.Entry(self.toolbar, width=24, bg=t["edit_bg"], fg=t["fg"], insertbackground=t["fg"],
                         relief="flat", font=self.disconnect_btn.cget("font"))
        entry.insert(0, self._reconnect_default_address())
        entry.bind("<Return>", lambda e: self.action_reconnect())
        # Packed after the button (so it's one slot further left) and
        # before the peers label (so the label stays leftmost of the three).
        entry.pack(side="right", padx=(0, 6), pady=4, before=self.peers_label)
        self.reconnect_entry = entry
        self.disconnect_btn.configure(text="Reconnect", command=self.action_reconnect,
                                      bg="#2f8f5b", activebackground="#3aa76a")
        # Match the button's height exactly so the two line up: an Entry is
        # a few pixels shorter than a Button by default.
        self.toolbar.update_idletasks()
        extra = self.disconnect_btn.winfo_reqheight() - entry.winfo_reqheight()
        if extra > 0:
            entry.pack_configure(ipady=extra // 2)
        entry.focus_set()
        entry.icursor("end")

    def _hide_reconnect_ui(self):
        if self.reconnect_entry is not None:
            self.reconnect_entry.destroy()
            self.reconnect_entry = None
        self.disconnect_btn.configure(text="Disconnect", command=self.action_disconnect,
                                      bg="#5a3030", activebackground="#734040", state="normal")

    def action_reconnect(self):
        if self.reconnect_entry is None or self._reconnect_token is not None:
            return
        from connect_window import parse_address
        try:
            host, port = parse_address(self.reconnect_entry.get())
        except ValueError as e:
            messagebox.showerror("Invalid address", str(e))
            return
        if self.mode == "p2p" and self.reconnect_entry.get().strip() == "":
            messagebox.showerror("Missing address", "Enter the address of a peer still in the mesh.")
            return
        # Reconnecting makes the host re-send every shared document, which replaces what's in those tabs here.
        if not self._confirm_discard_all(shared_only=True):
            return
        extra_hello = self._failover.hello_extra(is_sequencer=False) if self._failover is not None else None
        token = self._reconnect_token = object()
        client = self._reconnect_client = Client()
        self.reconnect_entry.configure(state="disabled")
        self.disconnect_btn.configure(text="Connecting...", state="disabled")
        self._set_status(f"Reconnecting to {host}:{port}...")

        def worker():
            try:
                client.connect(host, port, self.username, extra_hello=extra_hello)
            except ConnectCancelled:
                return
            except ConnectError as e:
                outcome = (False, str(e))
            else:
                outcome = (True, None)
            try:
                self.after(0, lambda: self._reconnect_done(token, client, host, port, *outcome))
            except tk.TclError:
                pass        # the window is already gone

        threading.Thread(target=worker, daemon=True).start()

    def _reconnect_done(self, token, client, host, port, ok, error):
        if token is not self._reconnect_token:
            # Cancelled (the session was left) while connecting.
            if ok:
                client.disconnect()
            return
        self._reconnect_token = None
        self._reconnect_client = None
        if not ok:
            if self.reconnect_entry is not None:
                self.reconnect_entry.configure(state="normal")
            self.disconnect_btn.configure(text="Reconnect", state="normal")
            self._set_status(f"Reconnect failed: {error}")
            messagebox.showerror("Reconnect", error)
            return
        if self.mode == "client":
            self.cfg.setdefault("connection", {})["address"] = f"{host}:{port}"
        self._finish_failover(client, None, "")
        self._set_status(f"Reconnected to {host}:{port}")

    def _on_failover_reconnected(self, new_client, new_server, reason):
        self.after(0, lambda: self._finish_failover(new_client, new_server, reason))

    def _finish_failover(self, new_client, new_server, reason):
        self._hide_reconnect_ui()
        self.client = new_client
        self.server = new_server
        self._bind_client(new_client)
        self.cursor_layer.clear()
        self.cursor_layer.set_self_id(new_client.client_id)
        self._known_peer_names = {}
        self._roster = []
        self._last_private = None
        self._share_runs, self._share_early = {}, {}
        for tab in self._pending_shares.values():
            tab.pending_ref = None      # that request went to the old sequencer; share again if needed
        self._pending_shares = {}
        self._note_assigned_name()
        note = "you're now the sequencer" if new_server is not None else "reconnected to the new sequencer"
        self._set_status(f"Back online: {note}")
        self._notify(f"Reconnected ({note}).")
        self.peers_label.configure(text="")

    def _on_failover_failed(self, reason):
        self.after(0, lambda: self._handle_disconnected(reason))

    def _set_status(self, text):
        self.status_left.configure(text=text)

    def _persist_ui_state(self):
        """Remembers the panel layout, which Connect-screen tab this session started from."""
        ui = self.cfg.setdefault("ui", {})
        ui["last_tab"] = {"solo": "Open", "p2p": "Peer to Peer"}.get(self.mode, "Connect")
        ui["editor_font_size"] = self._font_size
        ui["explorer_font_size"] = self._explorer_font_size
        ui["explorer_font_family"] = self._explorer_font_family
        ui["console_font_size"] = self._console_font_size
        ui["console_font_family"] = self._console_font_family
        ui["explorer_sort_key"] = self.explorer._sort_key
        ui["explorer_sort_reverse"] = self.explorer._sort_reverse
        self.dock.save(self.cfg)
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
        if self._reconnect_client is not None:
            self._reconnect_token = None
            try:
                self._reconnect_client.cancel_connect()
            except Exception:
                pass
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

    def _confirm_discard_all(self, shared_only=False):
        """Gate before anything that would throw away every open buffer at once (Disconnect/Close, the window's own close button)."""
        unsaved = [tab for tab in self.visible_tabs if tab.dirty and (tab.shared or not shared_only)]
        if not unsaved:
            return True
        if len(unsaved) == 1:
            return self._confirm_discard_tab(unsaved[0])
        choice = messagebox.askyesnocancel(
            "Save changes?",
            f"{len(unsaved)} tabs have unsaved changes. Save them all before continuing?")
        if choice is None:
            return False
        if not choice:
            return True
        return self.save_all_tabs(unsaved)

    def has_unsaved_tabs(self):
        return any(tab.dirty for tab in self.visible_tabs)

    def save_all_tabs(self, tabs=None):
        """Saves every unsaved tab (or just those in `tabs`), returning False if any of them ended up still unsaved."""
        unsaved = [tab for tab in (self.visible_tabs if tabs is None else tabs) if tab.dirty]
        already_named = [tab for tab in unsaved if tab.current_file]
        untitled = [tab for tab in unsaved if not tab.current_file]
        for tab in already_named + untitled:
            self._switch_to(tab)
            self.action_save()
            if self.dirty:
                return False
        return True

    def _confirm_discard(self):
        """Gate before anything that would throw away the active buffer (closing its tab, Disconnect/Close)."""
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
        """Computes what to broadcast to peers as the shared buffer's filename."""
        try:
            rel = os.path.relpath(path, self.working_dir)
        except ValueError:
            rel = None
        if rel and not rel.startswith("..") and not os.path.isabs(rel):
            return rel.replace(os.sep, "/")
        return os.path.basename(path)

    def _update_file_path_label(self):
        """Shown next to the interpreter box."""
        if self.current_file:
            text = self.current_file
        elif self.active_filename_hint:
            text = self.active_filename_hint
        elif self.text.get("1.0", "end-1c") == "":
            text = ""
        else:
            text = "(unsaved buffer)"
        if text != self._path_full_text:
            self._path_full_text = text
            self._path_offset = 0
        self._render_file_path()

    def _path_max_offset(self):
        """How many leading characters can be scrolled off before the end
        of the path is fully visible (0 when it all fits already)."""
        label = self.file_path_label
        text = self._path_full_text
        pad = 2 * (int(label.cget("padx")) + int(label.cget("bd")) + int(label.cget("highlightthickness")))
        avail = label.winfo_width() - pad
        if avail <= 1 or not text:
            return 0
        font = tkfont.Font(font=label.cget("font"))
        if font.measure(text) <= avail:
            return 0
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if font.measure(text[mid:]) <= avail:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def _render_file_path(self):
        self._path_offset = max(0, min(self._path_offset, self._path_max_offset()))
        shown = self._path_full_text[self._path_offset:]
        if self.file_path_label.cget("text") != shown:
            self.file_path_label.configure(text=shown)

    def _on_path_wheel(self, event):
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self._path_offset += step * 3
        self._render_file_path()
        return "break"

    def _on_path_drag_start(self, event):
        self._path_drag = (event.x_root, self._path_offset)

    def _on_path_drag(self, event):
        if not self._path_drag:
            return
        start_x, start_offset = self._path_drag
        char_w = max(1, tkfont.Font(font=self.file_path_label.cget("font")).measure("0"))
        self._path_offset = start_offset + round((start_x - event.x_root) / char_w)
        self._render_file_path()

    def action_new(self):
        """A new, empty, local tab - like opening a file, it doesn't touch
        the shared buffer. Right-click it > Share to make it the shared one."""
        tab = BufferTab()
        self._tabs.append(tab)
        self._switch_to(tab)

    def action_open(self):
        """Opens a file or a directory - Tkinter has no built-in picker that handles both, so this asks which kind first."""
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

    @staticmethod
    def _is_blank_local_tab(tab):
        """A local tab with no file and nothing in it."""
        return (not tab.shared and tab.visible and tab.current_file is None
                and tab.doc == "" and tab.saved_doc == "")

    def _find_tab_for_path(self, path):
        for tab in self.visible_tabs:
            if tab.current_file and self._same_path(tab.current_file, path):
                return tab
        return None

    def _load_file(self, path, into_shared=False):
        """Opens `path` in its own new local tab - it is NOT sent to peers; right-click the tab > Share for that."""
        existing = self._find_tab_for_path(path)
        if existing is not None:
            if into_shared:
                self._make_shared(existing)
            else:
                self._switch_to(existing)
            return
        try:
            content, eol, bom = fileio.read_text(path)
        except UnicodeDecodeError:
            messagebox.showerror("Open File", f"'{os.path.basename(path)}' is not a UTF-8 text file.")
            return
        except OSError as e:
            messagebox.showerror("Open File", str(e))
            return
        tab = BufferTab(doc=content)
        tab.eol, tab.bom = eol, bom
        tab.current_file = path
        tab.loaded_mtime = self._safe_mtime(path)
        tab.filename_hint = self._share_hint_for(path)
        # An empty, never-saved local tab that's on screen is just a placeholder.
        blank = self._active if self._is_blank_local_tab(self._active) else None
        self._tabs.append(tab)
        if blank is not None:
            self._tabs.remove(tab)
            self._tabs.insert(self._tabs.index(blank), tab)
        placeholder = self._pristine_shared_tab()
        others = [t for t in self.visible_tabs if t is not tab and t is not blank]
        pristine_shared = placeholder is not None and others == [placeholder]
        if into_shared or pristine_shared:
            self._make_shared(tab)
        else:
            self._switch_to(tab)
        if blank is not None and blank in self._tabs:
            self._tabs.remove(blank)
            self._refresh_tabs()

    def action_save(self):
        if not self.current_file:
            return self.action_save_as()
        if not self._confirm_external_change(self.current_file):
            return
        self._write_to(self.current_file)

    def action_save_as(self):
        current = self.current_file
        path = filedialog.asksaveasfilename(
            initialdir=os.path.dirname(current) if current else self.working_dir,
            initialfile=os.path.basename(current) if current else "",
            defaultextension="" if current else ".py")
        if not path or not self._write_to(path):
            return
        self.current_file = path
        self.explorer.refresh()
        self.active_filename_hint = self._share_hint_for(path)
        if self._active.shared:
            self.client.send_buffer_context(self.active_filename_hint, self._active.doc_id)
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
        """Save the buffer to path; returns False (after reporting) on failure."""
        content = self.text.get("1.0", "end-1c")
        try:
            fileio.write_text(path, content, self._active.eol, self._active.bom)
        except (OSError, ValueError) as e:
            messagebox.showerror("Save", str(e))
            return False
        self._saved_doc = content
        self._refresh_dirty()
        self._loaded_mtime = self._safe_mtime(path)
        self._update_cursor_status()
        return True

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
        """Puts the address someone else needs to Join this mesh on the clipboard."""
        host, port = self.p2p_advertise
        addr = f"{host}:{port}"
        self.clipboard_clear()
        self.clipboard_append(addr)
        messagebox.showinfo("Invite Address", f"Copied to clipboard: {addr}")

    def _on_buffer_context(self, payload):
        self.after(0, lambda: self._handle_buffer_context(payload))

    def _handle_buffer_context(self, payload):
        """A peer changed the shared buffer's filename (drives highlighting and the tab title)."""
        filename = payload.get("filename")
        by = payload.get("by")
        tab = self._docs.get(payload.get("doc", MAIN_DOC))
        if tab is None:
            return
        tab.filename_hint = filename
        if tab is self._active:
            self._update_language_status()
            self._do_highlight()
            self._update_file_path_label()
        self._refresh_tabs()
        if by == self.client.client_id:
            return
        who = next((n for pid, n in self._known_peer_names.items() if pid == by), "someone")
        label = sanitize_text(filename, 200) if filename else "a blank buffer"
        self._chat_append([(f"{sanitize_name(who)} loaded {label} into a shared buffer\n", "info")])

    # Buffer tabs

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
        if tab.untitled_no is None:
            tab.untitled_no = self._next_untitled_no()
        return f"Untitled-{tab.untitled_no}"

    def _next_untitled_no(self):
        """Lowest number not currently shown as "Untitled-N" on any open
        tab - not just an ever-growing counter, so closing (or saving)
        Untitled-1 while Untitled-2 stays open frees 1 back up for the
        next new/blank tab instead of jumping straight to 3."""
        used = set()
        for tab in self._tabs:
            if tab.untitled_no is None or not tab.visible:
                continue  # hidden (closed) tabs don't hold a number
            if tab.current_file or (tab.shared and tab.filename_hint):
                continue  # no longer displayed as Untitled-N; number is free
            used.add(tab.untitled_no)
        n = 1
        while n in used:
            n += 1
        return n

    def _refresh_tabs(self):
        # Any refresh of the tab strip is a good time to also recheck the window-title asterisk.
        self._update_title()
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
        self.cursor_layer.set_doc(tab.doc_id)
        self.dirty = tab.dirty  # also refreshes the window title and tab strip
        self._update_language_status()
        self._update_file_path_label()
        self._redraw_linenumbers()
        self._do_highlight()
        self._update_cursor_status()
        self.text.focus_set()

    def _after_role_change(self):
        """The active tab just became shared / stopped being shared."""
        self.cursor_layer.set_doc(self._active.doc_id)
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

    def action_show_shared(self, tab=None):
        """Brings a shared buffer to the front (showing it again if it was
        closed): the given one, else the first one there is."""
        if tab is None:
            tab = next(iter(self._docs.values()), None)
        if tab is not None:
            self._switch_to(tab)

    def _shared_docs_snapshot(self):
        """{doc id: (text, filename)} for every document this peer tracks.
        Called from the failover thread, hence the copy."""
        return {doc_id: (tab.doc, tab.filename_hint) for doc_id, tab in list(self._docs.items())}

    def _pristine_shared_tab(self):
        """A shared tab nobody has put anything in - no file, no text, from
        us or from a peer. It is only a placeholder, so sharing something
        else just takes it over instead of leaving it sitting there empty."""
        return next((t for t in self._tabs
                     if t.shared and t.visible and t.current_file is None and not t.local_edited
                     and t.doc == "" and t.saved_doc == "" and t.filename_hint is None), None)

    def action_close_active_tab(self):
        self.close_tab(self._active)

    def close_tab(self, tab):
        if not self._confirm_discard_tab(tab):
            return False
        tabs = self.visible_tabs
        index = tabs.index(tab) if tab in tabs else 0
        if tab.shared:
            # The session's document has to keep being tracked, so a shared tab can't really go away - it's just hidden.
            tab.visible = False
            tab.current_file = None
            tab.loaded_mtime = None
            tab.local_edited = False
            tab.saved_doc = tab.doc
            # Give up its "Untitled-N" number; it gets a fresh one if the
            # tab is shown again.
            tab.untitled_no = None
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

    def _clear_shared_tab(self, tab):
        """Asks whether to really clear this shared buffer (for everyone in
        the session); if so, empties it and renames the tab to Untitled.
        Returns True if it was cleared."""
        if tab.doc == "" and tab.current_file is None and not tab.filename_hint:
            self._set_status("Clear shared buffer")
            return False
        if not messagebox.askyesno(
                "Clear Shared Buffer",
                "Clear the shared buffer? "
                "Its contents will be removed for everyone in the session and the tab "
                "will be renamed to Untitled."):
            return False
        if not self._confirm_discard_tab(tab):
            return False
        old_doc = tab.doc
        tab.current_file = None
        tab.loaded_mtime = None
        tab.filename_hint = None
        tab.untitled_no = None
        tab.local_edited = False
        tab.doc = ""
        tab.saved_doc = ""
        tab.history.reset()
        tab.caret = 0
        tab.yview = 0.0
        if tab is self._active:
            self._suppress_capture = True
            try:
                self.text.delete("1.0", "end")
            finally:
                self._suppress_capture = False
        # Sent as a buffer swap so peers with their own edits/files in the
        # shared tab keep them as local tabs, like any other replacement.
        self.client.local_edit(ot.diff_to_op(old_doc, ""), tab.doc_id, {"filename": None})
        self.client.send_buffer_context(None, tab.doc_id)
        if tab is self._active:
            self._display_active()
        else:
            self._refresh_tabs()
        return True

    def _on_tab_move(self, tab_id, index):
        """Drag-and-drop reorder: `index` is the position among the visible
        tabs other than the moved one."""
        tab = self._tab_by_id(tab_id)
        if tab is None or not tab.visible:
            return
        others = [t for t in self.visible_tabs if t is not tab]
        index = max(0, min(index, len(others)))
        self._tabs.remove(tab)
        if not others:
            self._tabs.append(tab)
        elif index >= len(others):
            self._tabs.insert(self._tabs.index(others[-1]) + 1, tab)
        else:
            self._tabs.insert(self._tabs.index(others[index]), tab)
        self._refresh_tabs()

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
        if tab.shared and self.mode != "solo":
            menu.add_command(label="Clear Shared Buffer...", command=lambda: self._clear_shared_tab(tab))
        menu.add_command(label="Close", command=lambda: self.close_tab(tab))
        # tk_popup's own local grab is enough to dismiss the menu on a click inside this window.
        menu.bind("<FocusOut>", lambda _e: menu.unpost())
        menu.bind("<Unmap>", lambda _e: self.after_idle(self._destroy_tab_menu, menu))
        try:
            menu.tk_popup(x_root + 4, y_root + 4)
        finally:
            menu.grab_release()

    def _destroy_tab_menu(self, menu):
        try:
            menu.destroy()
        except tk.TclError:
            pass

    def _copy_text(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status(f"Copied {what}: {text}")

    def share_tab(self, tab):
        if self.mode == "solo" or tab.shared or tab.pending_ref is not None:
            return
        self._make_shared(tab)

    def _make_shared(self, tab):
        """Shares `tab` with the session as a document of its own."""
        hint = self._hint_for_tab(tab)
        placeholder = self._pristine_shared_tab()
        if placeholder is not None and placeholder is not tab:
            doc_id = placeholder.doc_id
            base_doc = placeholder.doc
            if placeholder in self._tabs:
                self._tabs.remove(placeholder)
            tab.doc_id = doc_id
            tab.visible = True
            tab.filename_hint = hint
            self._docs[doc_id] = tab
            self.client.local_edit(ot.diff_to_op(base_doc, tab.doc), doc_id, {"filename": hint})
            self.client.send_buffer_context(hint, doc_id)
        else:
            ref = next(self._share_refs)
            tab.filename_hint = hint
            if not self.client.open_doc(hint, tab.doc, ref):
                self._set_status("Not connected.")
                return
            tab.pending_ref = ref
            self._pending_shares[ref] = tab
        if tab is self._active:
            self._after_role_change()
        else:
            self._switch_to(tab)

    def unshare_tab(self, tab):
        """Stops sharing `tab`: it becomes an ordinary local tab holding what it holds now."""
        if self.mode == "solo" or not tab.shared:
            return
        doc_id = tab.doc_id
        tab.doc_id = None
        tab.filename_hint = self._hint_for_tab(tab)
        self._docs.pop(doc_id, None)
        self.client.forget_doc(doc_id)
        if tab is self._active:
            self._after_role_change()
        else:
            self._refresh_tabs()
        self._set_status("Unshared. This tab is now local.")

    def _begin_remote_swap(self, doc_id, swap, author):
        """A peer replaced the content of shared document `doc_id` with a different document."""
        old = self._docs[doc_id]
        new_hint = swap.get("filename")
        # Both of us have the same file open -> keep working on it here.
        same_file = old.current_file is not None and self._hint_for_tab(old) == new_hint
        if old.visible and self._worth_keeping(old) and not same_file:
            fresh = BufferTab(doc_id=doc_id, doc=old.doc)
            fresh.filename_hint = new_hint
            old.doc_id = None
            old.filename_hint = self._hint_for_tab(old)
            self._tabs.insert(self._tabs.index(old), fresh)
            self._docs[doc_id] = fresh
            self._notify(f"{self._tab_base_title(old)} stays open as a local tab.")
        else:
            old.visible = True
            old.filename_hint = new_hint
            old.local_edited = old.local_edited and same_file
        # Peer cursors are offsets into a shared document, so whichever tab is on screen now.
        self.cursor_layer.set_doc(self._active.doc_id)

    def _prepare_run_target(self):
        """Return (command, path, is_temp) for the buffer to run, or None if it can't be prepared."""
        cmd = self.run_cmd.get().strip()
        if not interpreters.is_valid(cmd, self.working_dir):
            if self._ask_set_interpreter():
                self._focus_interpreter_entry()
            return None
        if self.current_file is not None:
            if self.dirty:
                choice = messagebox.askyesnocancel(
                    "Run",
                    f"'{os.path.basename(self.current_file)}' has unsaved changes.\n\n"
                    "Yes: save, then run.\n"
                    "No: run the last saved version (unsaved changes are not included).\n"
                    "Cancel: don't run.")
                if choice is None:
                    return None
                if not choice:
                    return cmd, self.current_file, False
                self.action_save()
                if self.dirty:
                    # action_save can no-op or fail - either way there's nothing safe to run.
                    return None
            return cmd, self.current_file, False

        try:
            fd, target = tempfile.mkstemp(prefix="peer2code_run_", suffix=self._temp_run_suffix())
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.text.get("1.0", "end-1c"))
        except OSError as e:
            messagebox.showerror("Run", str(e))
            return None
        return cmd, target, True

    def _temp_run_suffix(self):
        """File extension for an unsaved buffer's temp copy, matching its
        language so the interpreter sees the kind of file it expects."""
        language = self._current_language()
        for ext, lang in syntax.EXTENSION_LANGUAGE.items():
            if lang == language:
                return ext
        return ".txt"

    def action_run(self):
        if self.run_proc and self.run_proc.poll() is None:
            messagebox.showinfo("Run", "Something is already running in the Terminal.")
            return
        prepared = self._prepare_run_target()
        if prepared is None:
            return
        cmd, target, is_temp = prepared
        self.ansi_console.state.reset()
        self._drop_idle_prompt()
        self._ensure_newline()
        self._console_write(f"$ {cmd} {os.path.basename(target)}\n", "info")
        self._launch_process(interpreters.split_command(cmd) + [target],
                             cleanup_path=(target if is_temp else None))

    def _drop_idle_prompt(self):
        """If the console is sitting on a bare "$ " prompt nothing has been
        typed after, removes it, so the echo of a Run-button command takes
        its place ("$ interpreter script") instead of stacking under it
        ("$" on one line, "$ interpreter script" on the next). That prompt
        is only a placeholder for the next command; once something else
        takes the line it has no job left. Anything typed after the
        prompt is the user's own and is left alone."""
        self.ansi_console.flush()
        try:
            if self.console.get("input_start", "end-1c") != "":
                return
            start = self.console.index("input_start-2c")
            if self.console.get(start, "input_start") != "$ ":
                return
            if self.console.index(f"{start} linestart") != start:
                return
            # The delete guard refuses to touch anything before input_start,
            # so move the mark back over the prompt first.
            self.console.mark_set("input_start", start)
            self.console.delete(start, "end-1c")
            self.console.mark_set("input_start", "end-1c")
        except tk.TclError:
            pass

    def _run_terminal_command(self, command):
        """Runs a line typed straight at the Terminal's own $ prompt as a plain system command."""
        try:
            share = parse_share_command(command)
        except ValueError as err:
            self._notify(str(err), error=True)
            self._show_prompt()
            return
        if share is not None:
            self._share_command(share)
            self._show_prompt()
            return
        if command.startswith("\\share") and command[6:7] in ("", " "):
            command = command[1:]       # `\share ...` runs a program really named share
        self.ansi_console.flush()
        self.ansi_console.state.reset()
        self._launch_process(command, shell=True)

    def _launch_process(self, args, shell=False, cleanup_path=None):
        """Actually starts args - either an argv list or a single shell command string."""
        try:
            self.run_proc = procutil.spawn(args, self.working_dir, shell=shell)
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
        self._interrupt_sent = False
        for stream, is_err in ((self.run_proc.stdout, False), (self.run_proc.stderr, True)):
            threading.Thread(target=procutil.pump, args=(stream, is_err, self.out_queue), daemon=True).start()
        threading.Thread(target=self._watch_run_proc, args=(self.run_proc, cleanup_path), daemon=True).start()
        self._share_begin(args, shell)

    def _watch_run_proc(self, proc, cleanup_path=None):
        """Waits (off the main thread - proc.wait blocks) for this run to actually end, how ever it ends."""
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
        """Describes how a run ended, in the same spot [stopped] and [interrupted] print."""
        # An interrupt only explains the exit if the script actually died of it.
        died_of_interrupt = self._interrupt_sent and (
            returncode < 0 or returncode in (130, 3221225786))
        if not (self._proc_exit_expected or died_of_interrupt):
            if returncode < 0 and not sys.platform.startswith("win"):
                # An external, unexplained kill - always reported regardless of the [finished] toggle below.
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
        """Makes sure whatever gets written next starts on its own line."""
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
        proc = self.run_proc
        if proc and proc.poll() is None:
            self._proc_exit_expected = True
            procutil.stop(proc)
            self.after(2000, lambda: procutil.stop(proc, force=True))   # no-op if it already exited
            if self._terminal_message_enabled("stopped"):
                self._ensure_newline()
                self._console_write("[stopped]\n", "info")

    def stop_run(self):
        """Kill the running process tree now (used when the app closes)."""
        if self.run_proc and self.run_proc.poll() is None:
            self._proc_exit_expected = True
            procutil.stop(self.run_proc, force=True)

    def _poll_output(self):
        self._drain_out_queue()
        try:
            while True:
                returncode = self._exit_queue.get_nowait()
                for _ in range(50):     # the last output must precede [finished]
                    if self.out_queue.empty():
                        break
                    self._drain_out_queue()
                self._share_stop("", code=returncode)
                self._report_proc_exit(returncode)
        except queue.Empty:
            pass
        self._share_tick()
        self._poll_job = self.after(80, self._poll_output)

    def _drain_out_queue(self, limit=64):
        """Write queued output to the console; at most `limit` chunks per tick keeps the UI responsive."""
        buf, cur_tag, shared = [], None, []
        for _ in range(limit):
            try:
                chunk, is_err = self.out_queue.get_nowait()
            except queue.Empty:
                break
            shared.append((is_err, chunk))
            tag = "stderr" if is_err else None
            if buf and tag != cur_tag:
                self._console_write("".join(buf), cur_tag)
                buf = []
            cur_tag = tag
            buf.append(chunk)
        if buf:
            self._console_write("".join(buf), cur_tag)
        self._share_output(shared)      # one batch per tick, in arrival order

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
        # Everything just written becomes settled history - the live, editable tail always starts fresh right after it.
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
        """Tab, in the Terminal console: completes the path fragment under the cursor against real files/directories."""
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
                # Nothing left to auto-complete with several candidates still matching.
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
        """Up/Down at the $ prompt browse command history."""
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
        """Enter, in the Terminal console: sends the typed tail to the running process's stdin if something's running."""
        running = bool(self.run_proc and self.run_proc.poll() is None)
        text = self.console.get("input_start", "end-1c")
        self.console.insert("end", "\n")
        self.console.mark_set("input_start", "end-1c")
        self.console.see("end")
        if running:
            try:
                share = parse_share_command(text, running=True)
            except ValueError as err:
                self._notify(str(err), error=True)
                return
            if share is not None:
                self._share_command(share)
                return
            procutil.send_line(self.run_proc, text)
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
        # Normalize line endings before inserting.
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
        FindDialog.show(self, replace=False)

    def action_replace(self):
        FindDialog.show(self, replace=True)

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
        """Returns (start_line, end_line) ints for the current selection, or None if there isn't one."""
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
            # No selection: pad to the next tab-stop rather than always inserting a flat INDENT_WIDTH block.
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
        """Scans `before_text` backward for the opener that `close_char` would match if inserted right after it."""
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
        """Auto-indent on Enter."""
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
        """Backspace removes a whole indent level in one press when everything from the start of the line to the cursor is indentation."""
        text = self.text
        if text.tag_ranges("sel"):
            return None
        if event is not None and getattr(event, "state", 0) & 0x4:
            # Ctrl+Backspace is "delete the previous word" (see word_nav),
            # not "back out one indent level".
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
                    # Same or deeper indent but not a sibling clause.
                n -= 1
            if target_indent is None or target_indent == current_indent:
                return None
            with self._single_undo_step():
                text.delete(f"{line_no}.0", f"{line_no}.{len(current_indent)}")
                text.insert(f"{line_no}.0", target_indent)
            return None
        return None

    def _cancel_pending_jobs(self):
        """Cancels every self-rescheduling after() job."""
        win, self._share_win = self._share_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        for attr in ("_poll_job", "_cursor_send_job", "_highlight_job", "_console_resize_job", "_chat_flash_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def _unbind_shortcuts(self):
        """Releases every shortcut this editor bound onto the shared root window (and the text widget)."""
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
        """Binds OUTPUT_SHORTCUT_SPECS onto the Terminal console widget only - not root, not the editor's text widget."""
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
        """Ctrl+C: interrupt the running script unless text is selected (then copy). True if handled."""
        if self.console.tag_ranges("sel"):
            return False
        if not (self.run_proc and self.run_proc.poll() is None):
            # Idle, at a $ prompt: like a shell, abandon whatever is typed on the line and start a fresh prompt.
            self._console_write("^C\n", "info")
            self._show_prompt()
            return True
        self._interrupt_run_proc()
        return True

    def _interrupt_run_proc(self):
        """Actually delivers the interrupt to run_proc."""
        if not (self.run_proc and self.run_proc.poll() is None):
            return
        # Only a *near-simultaneous* duplicate is dropped.
        now = time.monotonic()
        if now - getattr(self, "_last_interrupt_at", 0.0) < 0.25:
            return
        self._last_interrupt_at = now
        self._interrupt_sent = True
        procutil.interrupt(self.run_proc)
        if self._terminal_message_enabled("interrupted"):
            self._ensure_newline()
            self._console_write("[interrupted]\n", "info")


class FindDialog(tk.Toplevel):
    """Find/Replace with the basics IDLE-style editors offer: search in
    either direction, whole-word and case-sensitive matching, and optional
    wraparound, on top of the original find-next/replace/replace-all."""

    # Class-level, not instance-level, on purpose.
    _last_find = ""
    _last_replace = ""
    # Dialogs currently open, so EditorApp._persist_ui_state can store their geometry too when the whole app closes with one still showing.
    _instances = []

    _GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")

    @classmethod
    def show(cls, app, replace):
        """Opens the dialog, or reuses the one already open so spamming Ctrl+F / Ctrl+H can't stack up copies."""
        for dlg in list(cls._instances):
            try:
                alive = dlg.winfo_exists()
            except tk.TclError:
                alive = False
            if not alive:
                cls._instances.remove(dlg)
                continue
            if dlg.replace_mode == replace and dlg.app is app:
                dlg.present()
                return dlg
            dlg.destroy()
        return cls(app, replace)

    def present(self):
        selected = self._selected_or_empty()
        if selected:
            self.find_var.set(selected)
        try:
            if self.state() == "iconic":
                self.deiconify()
            self.lift()
            self.focus_force()
            self.find_entry.focus_set()
            self.find_entry.select_range(0, "end")
        except tk.TclError:
            pass

    def __init__(self, app: EditorApp, replace: bool):
        super().__init__(app)
        self.app = app
        self.replace_mode = replace
        t = app.theme
        self.title("Replace" if replace else "Find")
        self.configure(bg=t["panel_bg"])
        # Width only: the height is fixed by the layout, but a wider box is genuinely useful for long search strings.
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
        self.find_entry = find_entry

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
        """Restores the dialog's last width and/or position, whichever of the two Settings > General > Window says to remember."""
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
        # Cache the last good geometry as it changes.
        if event.widget is not self:
            return
        try:
            if self.state() == "normal":
                self._last_geometry = self.geometry()
        except tk.TclError:
            pass

    def _store_state(self):
        """Writes this dialog's geometry and checkbox states into the app's in-memory config."""
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
        # <Destroy> bubbles up from every child as the window closes, not just this Toplevel itself.
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
        """Returns (pattern, is_regexp)."""
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
        # A real selection, not a separate highlight tag of our own.
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

import ipaddress
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import config
import fileio
import theme
import dnd_support
import scrollutil
from net.server import Server
from net.client import Client, ConnectError, ConnectCancelled
from net import nat

DEFAULT_PORT = 5123
from config import CODE_FONT


def parse_address(text, default_host="127.0.0.1", default_port=DEFAULT_PORT):
    """Accept 'host:port', ':port', 'port' or 'host' (IPv4/hostname) and fill in the rest."""
    text = (text or "").strip()
    if not text:
        return default_host, default_port
    if text.count(":") > 1 or text.startswith("["):
        raise ValueError("IPv6 addresses are not supported; use an IPv4 address or a hostname")
    if ":" in text:
        host, _, port_s = text.rpartition(":")
        host = host.strip() or default_host
    elif text.isdigit():
        host, port_s = default_host, text
    else:
        return text, default_port
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"'{port_s}' is not a valid port") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"Port {port} is out of range (1-65535)")
    return host, port


def _is_loopback_host(host):
    """True for 127.0.0.1, ::1, localhost, and the like."""
    host = (host or "").strip().lower()
    if host in ("localhost", "0.0.0.0", "::1", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


TABS = ["Open", "Connect", "Peer to Peer"]


class ConnectWindow(ttk.Frame):
    """Start screen with Open (local only), Connect (host or join) and Peer to Peer tabs."""

    def __init__(self, master, cfg, on_ready, initial_path=None):
        super().__init__(master)
        self.cfg = cfg
        self.on_ready = on_ready
        self.theme = cfg["theme"]
        self.working_dir = cfg["connection"].get("working_dir") or os.getcwd()
        self._current_client = None
        self._current_identity = None
        self._cancel_requested = threading.Event()
        self._busy = False
        self._action_buttons = []
        # Every classic widget built with an explicit theme color.
        self._theme_widgets = []
        self._build_style()
        self._build_ui()
        dnd_support.register_drop(self, self._on_window_drop, debug=False)
        dnd_support.register_drop(self.page_container, self._on_window_drop)
        if initial_path:
            self._select_tab("Open")
            self.open_dir_var.set(initial_path)
            hint = getattr(self.open_dir_var, "_path_hint", None)
            if hint is not None:
                self._flash_hint(hint, "Path filled from the command line.")
        else:
            last_tab = cfg.get("ui", {}).get("last_tab")
            self._select_tab(last_tab if last_tab in TABS else TABS[0])

    def _on_window_drop(self, paths):
        if not paths:
            return
        path = paths[0]
        var = {"Open": self.open_dir_var, "Connect": self.udp_dir_var,
               "Peer to Peer": self.p2p_dir_var}.get(self._active_tab, self.udp_dir_var)
        var.set(path)
        self.update_idletasks()
        hint = getattr(var, "_path_hint", None)
        if hint is not None:
            self._flash_hint(hint, "Path filled from drag and drop.")

    def _reg(self, widget, **color_options):
        """Registers a classic widget's theme-derived options so apply_theme_live can put current colors on it later."""
        if color_options:
            self._theme_widgets.append((widget, color_options))
        return widget

    def apply_theme_live(self, new_theme):
        """Repaints this whole screen with `new_theme` without rebuilding it."""
        self.theme = t = new_theme
        self._build_style()
        toplevel = self.winfo_toplevel()
        toplevel.configure(bg=t["bg"])
        theme.apply_classic_widget_defaults(toplevel, t)
        for widget, color_options in self._theme_widgets:
            try:
                widget.configure(**{opt: t[key] for opt, key in color_options.items()})
            except tk.TclError:
                pass
        # lbl/underline's colors depend on which tab is selected, so they aren't in the generic registry above.
        for name, (lbl, underline) in self._tab_widgets.items():
            selected = (name == self._active_tab)
            lbl.configure(bg=t["bg"], fg=t["fg"] if selected else t["muted_fg"])
            underline.configure(bg=t["accent"] if selected else t["bg"])
        self._refresh_recent_projects()

    def _build_style(self):
        t = self.theme
        style = ttk.Style(self)
        theme.apply_base_style(style, t)
        style.configure("Title.TLabel", background=t["bg"], foreground=t["fg"], font=("Segoe UI", 20, "bold"))
        style.configure("Host.TButton", font=("Segoe UI", 11, "bold"), padding=10, focuscolor=t["panel_bg"])
        style.configure("Join.TButton", font=("Segoe UI", 11, "bold"), padding=10, focuscolor=t["panel_bg"])
        style.map("Host.TButton", background=[("pressed", "#256b48"), ("active", "#3aa76a"),
                                                ("focus", "#3aa76a"), ("!disabled", "#2f8f5b")])
        join_bright = theme.lighten(t["accent"], 0.25)
        join_pressed = theme.darken(t["accent"], 0.2)
        style.map("Join.TButton", background=[("pressed", join_pressed), ("active", join_bright),
                                                ("focus", join_bright), ("!disabled", t["accent"])])
        self.configure(style="TFrame")

    def _build_ui(self):
        wrap = ttk.Frame(self, style="TFrame")
        wrap.pack(expand=True, fill="both")
        ttk.Label(wrap, text="Peer to Code", style="Title.TLabel").pack(pady=(24, 14))

        self._build_tab_bar(wrap)

        bottom_bar = ttk.Frame(wrap, style="TFrame")
        bottom_bar.pack(side="bottom", fill="x")
        self.status = ttk.Label(bottom_bar, text="", style="TLabel")
        self.status.pack(pady=(4, 2))
        self.cancel_btn = ttk.Button(bottom_bar, text="Cancel", command=self._cancel_current)

        self._build_scrollable_page_area(wrap)
        self._pages = {
            "Open": self._build_open_project_page(self.page_container),
            "Connect": self._build_udp_page(self.page_container),
            "Peer to Peer": self._build_p2p_page(self.page_container),
        }
        self._rebind_wheel()

    def _build_scrollable_page_area(self, parent):
        """The area holding each tab's content."""
        t = self.theme
        area = ttk.Frame(parent, style="TFrame")
        area.pack(fill="both", expand=True)

        column = self._reg(tk.Frame(area, bg=t["bg"]), bg="bg")
        column.place(relx=0.5, rely=0, anchor="n", relheight=1.0)
        column.pack_propagate(False)

        def on_area_resize(e):
            margin = 24
            max_reading_width = 640
            width = max(280, min(max_reading_width, e.width - margin * 2))
            column.configure(width=width)

        area.bind("<Configure>", on_area_resize)

        canvas = self._reg(tk.Canvas(column, bg=t["bg"], highlightthickness=0), bg="bg")
        vscroll = ttk.Scrollbar(column, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(fill="both", expand=True)
        self._page_canvas = canvas
        self._page_scrollbar = vscroll

        self.page_container = ttk.Frame(canvas, style="TFrame")
        self._page_window = canvas.create_window((0, 0), window=self.page_container, anchor="n")

        def on_container_resize(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            self._update_scrollbar()

        def on_canvas_resize(e):
            canvas.itemconfigure(self._page_window, width=e.width)
            self._update_scrollbar()

        self.page_container.bind("<Configure>", on_container_resize)
        canvas.bind("<Configure>", on_canvas_resize)

        scrollutil.bind_wheel(canvas)

    def _rebind_wheel(self):
        scrollutil.bind_wheel(self._page_canvas, self.page_container)

    def _update_scrollbar(self):
        canvas = self._page_canvas
        bbox = canvas.bbox("all")
        if not bbox:
            return
        content_height = bbox[3] - bbox[1]
        visible_height = canvas.winfo_height()
        needed = content_height > visible_height
        showing = self._page_scrollbar.winfo_ismapped()
        if needed and not showing:
            self._page_scrollbar.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        elif not needed and showing:
            self._page_scrollbar.place_forget()

    def _build_tab_bar(self, parent):
        t = self.theme
        bar = self._reg(tk.Frame(parent, bg=t["bg"]), bg="bg")
        bar.pack(pady=(0, 4))
        self._tab_widgets = {}
        for name in TABS:
            col = self._reg(tk.Frame(bar, bg=t["bg"], cursor="hand2"), bg="bg")
            col.pack(side="left", padx=16)
            lbl = tk.Label(col, text=name, bg=t["bg"], cursor="hand2")
            lbl.pack()
            underline = tk.Frame(col, bg=t["bg"], height=3)
            underline.pack(fill="x", pady=(5, 0))
            for widget in (col, lbl):
                widget.bind("<Button-1>", lambda _e, n=name: self._select_tab(n))
            self._tab_widgets[name] = (lbl, underline)

    def _select_tab(self, name):
        if self._busy:
            return
        t = self.theme
        self._active_tab = name
        self.cfg.setdefault("ui", {})["last_tab"] = name
        for n, (lbl, underline) in self._tab_widgets.items():
            selected = (n == name)
            lbl.configure(
                font=("Segoe UI", 15, "bold") if selected else ("Segoe UI", 11),
                fg=t["fg"] if selected else t["muted_fg"],
            )
            underline.configure(bg=t["accent"] if selected else t["bg"])
        # Everything below runs in this one callback, in an order that never shows an in-between frame.
        self._page_canvas.yview_moveto(0)
        self._pages[name].pack(fill="both", expand=True)
        for n, page in self._pages.items():
            if n != name:
                page.pack_forget()
        self.status.configure(text="")
        if name == "Open":
            self._refresh_recent_projects()
        elif name == "Peer to Peer":
            self._refresh_p2p_addr_placeholder()
        self._sync_scroll_region()

    def _sync_scroll_region(self):
        canvas = self._page_canvas
        canvas.configure(scrollregion=canvas.bbox("all"))
        self._update_scrollbar()

    def _reset_scroll(self):
        self._page_canvas.yview_moveto(0)
        self._update_scrollbar()

    def _default_name(self):
        return os.environ.get("USERNAME") or os.environ.get("USER") or "user"

    def _card(self, parent):
        card = ttk.Frame(parent, style="Panel.TFrame")
        card.pack(fill="x", padx=40, pady=(8, 0))
        inner = ttk.Frame(card, style="Panel.TFrame")
        inner.pack(fill="x", padx=24, pady=22)
        inner.columnconfigure(0, weight=1)
        return inner

    def _bind_dynamic_wrap(self, label, min_width=60):
        """Keeps `label`'s wraplength in sync with the width it's actually given, so its text reflows as the window is resized."""
        label.configure(width=1)
        def on_configure(e, l=label):
            new_width = max(min_width, e.width)
            if getattr(l, "_wrap_width", None) != new_width:
                l._wrap_width = new_width
                l.configure(wraplength=new_width)
        label.bind("<Configure>", on_configure)

    def _dir_row(self, inner, row, var):
        """Path field with a single Browse button that lets the entry hold either a folder or a single file."""
        ttk.Label(inner, text="Path", style="Panel.TLabel").grid(row=row, column=0, sticky="w")
        dir_row = ttk.Frame(inner, style="Panel.TFrame")
        dir_row.grid(row=row + 1, column=0, columnspan=2, sticky="we", pady=(2, 2))
        hint = ttk.Label(inner, text="", style="Muted.TLabel", justify="left")
        hint.grid(row=row + 2, column=0, columnspan=2, sticky="we", pady=(0, 14))
        self._bind_dynamic_wrap(hint)
        ttk.Button(dir_row, text="Browse", command=lambda: self._pick_path(var, hint)).pack(side="right")
        entry = ttk.Entry(dir_row, textvariable=var, width=28)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        var._path_hint = hint
        return entry

    def _pick_path(self, var, hint=None):
        kind = self._prompt_file_or_directory()
        if kind is None:
            return
        if kind == "file":
            f = filedialog.askopenfilename(initialdir=os.path.dirname(var.get()) or os.getcwd(),
                                            title="Choose file")
            if not f:
                return
            var.set(f)
        else:
            d = filedialog.askdirectory(initialdir=var.get() or os.getcwd(), title="Choose folder")
            if not d:
                return
            var.set(d)
        if hint is not None:
            self._flash_hint(hint, "Path filled from Browse.")

    def _prompt_file_or_directory(self):
        """Small chooser standing in for Browse."""
        t = self.theme
        dlg = tk.Toplevel(self)
        dlg.title("Browse")
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        dlg.configure(bg=t["panel_bg"])
        tk.Label(dlg, text="Open a file or a folder?", bg=t["panel_bg"], fg=t["fg"],
                 padx=20, pady=16).pack()
        btn_row = tk.Frame(dlg, bg=t["panel_bg"])
        btn_row.pack(pady=(0, 16), padx=16)
        result = {}

        def pick(kind):
            result["kind"] = kind
            dlg.destroy()

        # Plain tk.Button, so it only picks up theme colors from the Tk option database at creation time.
        tk.Button(btn_row, text="File", width=8, command=lambda: pick("file")).pack(side="left", padx=6)
        tk.Button(btn_row, text="Folder", width=8, command=lambda: pick("directory")).pack(side="left", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        dlg.grab_set()
        dlg.wait_window()
        return result.get("kind")

    def _flash_hint(self, label, text):
        """Shows `text` on `label` briefly, then clears it."""
        label.configure(text=text)
        job = getattr(label, "_clear_job", None)
        if job is not None:
            label.after_cancel(job)
        label._clear_job = label.after(1800, lambda: label.configure(text=""))

    def _add_placeholder(self, entry, text):
        """ttk.Entry has no native placeholder text, so fake one."""
        t = self.theme
        entry._placeholder_text = text
        entry._showing_placeholder = False

        def show_placeholder():
            if entry.get():
                return
            entry._showing_placeholder = True
            entry.configure(foreground=t["muted_fg"])
            entry.insert(0, text)

        def clear_placeholder(_e=None):
            if entry._showing_placeholder:
                entry._showing_placeholder = False
                entry.delete(0, "end")
                entry.configure(foreground=t["fg"])

        entry.bind("<FocusIn>", clear_placeholder)
        entry.bind("<FocusOut>", lambda _e: show_placeholder())
        show_placeholder()

    def _entry_value(self, entry, use_placeholder=False):
        """Current text of an entry set up with _add_placeholder()."""
        if getattr(entry, "_showing_placeholder", False):
            return entry._placeholder_text if use_placeholder else ""
        return entry.get().strip()

    def _build_open_project_page(self, parent):
        page = ttk.Frame(parent, style="TFrame")
        inner = self._card(page)

        self.open_dir_var = tk.StringVar(value=self.working_dir)
        self._open_entry = self._dir_row(inner, 0, self.open_dir_var)
        dnd_support.register_drop(page, self._on_window_drop)
        dnd_support.register_drop(inner, self._on_window_drop)
        dnd_support.register_drop(self._open_entry, self._on_window_drop)

        open_btn = ttk.Button(inner, text="Open", style="Join.TButton", command=self._do_open_project)
        open_btn.grid(row=3, column=0, columnspan=2, sticky="we")
        self._action_buttons.append(open_btn)

        ttk.Label(inner, text="Recent", style="Panel.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(20, 4))
        self.recent_list = ttk.Frame(inner, style="Panel.TFrame")
        self.recent_list.grid(row=5, column=0, columnspan=2, sticky="we")
        self._recent_row_widgets = []
        self._refresh_recent_projects()
        return page

    def _refresh_recent_projects(self):
        t = self.theme
        recents = self.cfg["connection"].get("recent_projects") or []
        # Nothing changed since the rows were last built (same list, same theme): keep them.
        signature = (tuple(recents), t["panel_bg"], t["fg"])
        if self._recent_row_widgets and getattr(self, "_recent_signature", None) == signature:
            return
        self._recent_signature = signature
        for w in self._recent_row_widgets:
            w.destroy()
        self._recent_row_widgets = []
        if not recents:
            lbl = ttk.Label(self.recent_list, text="No recent projects yet.", style="Muted.TLabel")
            lbl.pack(anchor="w")
            self._recent_row_widgets.append(lbl)
            self._rebind_wheel()
            return
        for path in recents:
            row = tk.Frame(self.recent_list, bg=t["panel_bg"])
            row.pack(fill="x")
            display_text = self._display_path(path)
            label = tk.Label(row, text=display_text, bg=t["panel_bg"], fg=t["fg"],
                              font=(CODE_FONT, 10), anchor="w", justify="left", cursor="hand2", padx=4, pady=3)
            label.pack(side="left", fill="both", expand=True)
            label.bind("<Configure>", lambda e, l=label: l.configure(wraplength=max(60, e.width)))

            menu = tk.Menu(row, tearoff=0)
            menu.add_command(label="Copy", command=lambda p=path, l=label, d=display_text: self._copy_recent_project(p, l, d))
            menu.add_command(label="Remove", command=lambda p=path: self._remove_recent_project(p))

            def on_enter(_e, r=row, l=label):
                r.configure(bg=t["sel_bg"]); l.configure(bg=t["sel_bg"])

            def on_leave(_e, r=row, l=label):
                r.configure(bg=t["panel_bg"]); l.configure(bg=t["panel_bg"])

            def on_right_click(e, m=menu):
                # No explicit grab_release here - tk_popup already sets its own internal grab so it can detect and dismiss on an outside click.
                m.tk_popup(e.x_root + 4, e.y_root + 4)

            for widget in (row, label):
                widget.bind("<Enter>", on_enter)
                widget.bind("<Leave>", on_leave)
                widget.bind("<Button-3>", on_right_click)
            label.bind("<Button-1>", lambda _e, p=path: self._do_open_project(p))
            self._recent_row_widgets.append(row)
        self._rebind_wheel()

    def _remove_recent_project(self, path):
        config.remove_recent_project(self.cfg, path)
        config.save_config(self.cfg)
        self._refresh_recent_projects()

    def _copy_recent_project(self, path, label=None, original_text=None):
        """Puts `path` on the system clipboard rather than filling any particular field directly."""
        self.clipboard_clear()
        self.clipboard_append(path)
        if label is not None and original_text is not None:
            job = getattr(label, "_copy_flash_job", None)
            if job is not None:
                label.after_cancel(job)
            label.configure(text="Copied path to clipboard")
            label._copy_flash_job = label.after(
                1400, lambda: (label.configure(text=original_text), setattr(label, "_copy_flash_job", None)))

    def _display_path(self, path):
        name = os.path.basename(path.rstrip("/\\")) or path
        return f"{name} ({path})"

    def _do_open_project(self, path=None):
        raw = path or self.open_dir_var.get().strip() or os.getcwd()
        raw = os.path.abspath(raw)
        single_file = None
        if os.path.isfile(raw):
            single_file = raw
            wdir = os.path.dirname(raw) or os.getcwd()
        elif os.path.isdir(raw):
            wdir = raw
        else:
            messagebox.showerror("Not found", "That file or folder does not exist.")
            return

        if single_file:
            try:
                fileio.read_text(single_file)
            except (OSError, UnicodeDecodeError) as e:
                messagebox.showerror("Open File", f"Can't open this file as UTF-8 text: {e}")
                return

        self.open_dir_var.set(raw)
        name = self.cfg["connection"].get("name") or self._default_name()
        self.cfg["connection"]["working_dir"] = wdir
        self._set_busy(True, "Opening...")

        try:
            server = Server(0)
            actual_port = server.transport.local_port
            server.start()
        except OSError as e:
            self._set_busy(False, f"Could not open: {e}")
            return

        client = Client()
        self._current_client = client

        def worker():
            try:
                client.connect("127.0.0.1", actual_port, name)
            except ConnectCancelled:
                self.after(0, lambda: self._cancelled(server))
                return
            except ConnectError as e:
                self.after(0, lambda msg=str(e): self._fail(msg, server))
                return
            extra = {"open_file": single_file} if single_file else None
            self.after(0, lambda: self._finish_open_project(raw, client, server, wdir, name, extra))

        threading.Thread(target=worker, daemon=True).start()

    def _register_recent(self, path):
        """Puts `path` (a folder, or a single file) at the top of the
        Recent list and saves it. Shared by every tab, so Open, Connect
        and Peer to Peer all feed the same list."""
        config.add_recent_project(self.cfg, path)
        config.save_config(self.cfg)
        self._refresh_recent_projects()

    def _finish_open_project(self, raw, client, server, wdir, name, extra):
        self._register_recent(raw)
        self._ready("solo", client, server, wdir, name, extra)

    def _build_udp_page(self, parent):
        page = ttk.Frame(parent, style="TFrame")
        inner = self._card(page)
        conn = self.cfg["connection"]

        ttk.Label(inner, text="Name", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.udp_name_var = tk.StringVar(value=conn.get("name") or self._default_name())
        ttk.Entry(inner, textvariable=self.udp_name_var, width=42).grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 12))

        ttk.Label(inner, text="Address", style="Panel.TLabel").grid(row=2, column=0, sticky="w")
        self.addr_var = tk.StringVar(value=conn.get("address") or f"127.0.0.1:{DEFAULT_PORT}")
        ttk.Entry(inner, textvariable=self.addr_var, width=42, font=(CODE_FONT, 11)).grid(
            row=3, column=0, columnspan=2, sticky="we", pady=(2, 16))

        self.udp_dir_var = tk.StringVar(value=self.working_dir)
        self._dir_row(inner, 5, self.udp_dir_var)
        dnd_support.register_drop(page, self._on_window_drop)
        dnd_support.register_drop(inner, self._on_window_drop)

        btn_row = ttk.Frame(inner, style="Panel.TFrame")
        btn_row.grid(row=8, column=0, columnspan=2, sticky="we")
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        self.host_btn = ttk.Button(btn_row, text="Host", style="Host.TButton", command=self._do_host)
        self.host_btn.grid(row=0, column=0, sticky="we", padx=(0, 6))
        self.join_btn = ttk.Button(btn_row, text="Connect", style="Join.TButton", command=self._do_connect)
        self.join_btn.grid(row=0, column=1, sticky="we", padx=(6, 0))
        self._action_buttons += [self.host_btn, self.join_btn]
        return page

    def _validated(self, name_var, dir_var):
        """Returns (name, wdir, single_file). single_file is set when the
        Path field holds a file rather than a folder - wdir is then that
        file's parent directory."""
        name = name_var.get().strip() or self._default_name()
        raw = os.path.abspath(dir_var.get().strip() or os.getcwd())
        if os.path.isfile(raw):
            try:
                with open(raw, "r", encoding="utf-8") as f:
                    f.read()
            except (OSError, UnicodeDecodeError) as e:
                messagebox.showerror("Open File", str(e))
                return None
            return name, os.path.dirname(raw) or os.getcwd(), raw
        if os.path.isdir(raw):
            return name, raw, None
        messagebox.showerror("Not found", "That file or folder does not exist.")
        return None

    def _persist(self, name, address, wdir):
        self.cfg["connection"]["name"] = name
        self.cfg["connection"]["address"] = address
        self.cfg["connection"]["working_dir"] = wdir
        config.save_config(self.cfg)

    def _do_host(self):
        common = self._validated(self.udp_name_var, self.udp_dir_var)
        if not common:
            return
        name, wdir, single_file = common
        try:
            host_part, port = parse_address(self.addr_var.get())
        except ValueError as e:
            messagebox.showerror("Invalid address", str(e))
            return
        self._persist(name, f"{host_part}:{port}", wdir)
        self._set_busy(True, f"Starting session on port {port}...")

        server = Server(port)
        try:
            server.start()
        except OSError as e:
            self._set_busy(False, f"Could not start: {e}")
            return

        client = Client()
        self._current_client = client

        def worker():
            try:
                client.connect("127.0.0.1", port, name)
            except ConnectCancelled:
                self.after(0, lambda: self._cancelled(server))
                return
            except ConnectError as e:
                self.after(0, lambda msg=str(e): self._fail(msg, server))
                return
            extra = {"open_file": single_file} if single_file else None
            self.after(0, lambda: self._ready("host", client, server, wdir, name, extra,
                                               recent_path=single_file or wdir))

        threading.Thread(target=worker, daemon=True).start()

    def _do_connect(self):
        common = self._validated(self.udp_name_var, self.udp_dir_var)
        if not common:
            return
        name, wdir, _single_file = common
        try:
            host, port = parse_address(self.addr_var.get())
        except ValueError as e:
            messagebox.showerror("Invalid address", str(e))
            return
        if not host:
            messagebox.showerror("Missing address", "Enter an address to connect to.")
            return
        self._persist(name, f"{host}:{port}", wdir)
        self._set_busy(True, f"Connecting to {host}:{port}...")

        client = Client()
        self._current_client = client

        def worker():
            try:
                client.connect(host, port, name)
            except ConnectCancelled:
                self.after(0, lambda: self._cancelled(None))
                return
            except ConnectError as e:
                self.after(0, lambda msg=str(e): self._fail(msg, None))
                return
            self.after(0, lambda: self._ready("client", client, None, wdir, name, recent_path=wdir))

        threading.Thread(target=worker, daemon=True).start()

    def _build_p2p_page(self, parent):
        page = ttk.Frame(parent, style="TFrame")
        inner = self._card(page)
        conn = self.cfg["connection"]

        ttk.Label(inner, text="Name", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.p2p_name_var = tk.StringVar(value=conn.get("name") or self._default_name())
        ttk.Entry(inner, textvariable=self.p2p_name_var, width=42).grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 12))

        ttk.Label(inner, text="Address", style="Panel.TLabel").grid(row=2, column=0, sticky="w")
        self.p2p_addr_entry = ttk.Entry(inner, width=42, font=(CODE_FONT, 11))
        self.p2p_addr_entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(2, 2))
        saved_addr = conn.get("p2p_address")
        if saved_addr and not _is_loopback_host(saved_addr.rsplit(":", 1)[0]):
            self.p2p_addr_entry.insert(0, saved_addr)
        self._add_placeholder(self.p2p_addr_entry, self._p2p_addr_placeholder_text())
        addr_hint = ttk.Label(inner, text="Automatically detected if left blank.", style="Muted.TLabel", justify="left")
        addr_hint.grid(row=4, column=0, columnspan=2, sticky="we", pady=(0, 12))
        self._bind_dynamic_wrap(addr_hint)
        self._p2p_addr_hint = addr_hint

        ttk.Label(inner, text="Join", style="Panel.TLabel").grid(row=5, column=0, columnspan=2, sticky="w")
        self.p2p_join_entry = ttk.Entry(inner, width=42, font=(CODE_FONT, 11))
        self.p2p_join_entry.grid(row=6, column=0, columnspan=2, sticky="we", pady=(2, 2))
        saved_join = conn.get("p2p_join_address")
        if saved_join:
            self.p2p_join_entry.insert(0, saved_join)
        join_hint = ttk.Label(
            inner,
            text="Paste the address the mesh starter got via File > Copy Invite Address and shared with you. Leave blank to start a new mesh.",
            style="Muted.TLabel",
            justify="left",
        )
        join_hint.grid(row=7, column=0, columnspan=2, sticky="we", pady=(0, 12))
        self._bind_dynamic_wrap(join_hint)

        self.p2p_dir_var = tk.StringVar(value=self.working_dir)
        self._dir_row(inner, 8, self.p2p_dir_var)

        btn_row = ttk.Frame(inner, style="Panel.TFrame")
        btn_row.grid(row=11, column=0, columnspan=2, sticky="we")
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        self.p2p_start_btn = ttk.Button(btn_row, text="Start", style="Host.TButton", command=self._do_p2p_start)
        self.p2p_start_btn.grid(row=0, column=0, sticky="we", padx=(0, 6))
        self.p2p_join_btn = ttk.Button(btn_row, text="Join", style="Join.TButton", command=self._do_p2p_join)
        self.p2p_join_btn.grid(row=0, column=1, sticky="we", padx=(6, 0))
        self._action_buttons += [self.p2p_start_btn, self.p2p_join_btn]
        return page

    def _p2p_addr_placeholder_text(self):
        try:
            local_ip = nat.guess_local_ip()
        except Exception:
            local_ip = None
        return f"{local_ip}:{DEFAULT_PORT}" if local_ip else "no network detected"

    def _refresh_p2p_addr_placeholder(self):
        """Re-detects the local address and updates the Address field's placeholder to match."""
        entry = self.p2p_addr_entry
        text = self._p2p_addr_placeholder_text()
        entry._placeholder_text = text
        if getattr(entry, "_showing_placeholder", False):
            entry.delete(0, "end")
            entry.insert(0, text)

    def _set_p2p_addr_display(self, text):
        """Reflects the address actually in use back into the Address field, replacing whatever was there."""
        entry = self.p2p_addr_entry
        entry._showing_placeholder = False
        entry.configure(foreground=self.theme["fg"])
        entry.delete(0, "end")
        entry.insert(0, text)

    def _persist_p2p(self, name, p2p_address, wdir, join_address=None):
        self.cfg["connection"]["name"] = name
        self.cfg["connection"]["p2p_address"] = p2p_address
        if join_address is not None:
            self.cfg["connection"]["p2p_join_address"] = join_address
        self.cfg["connection"]["working_dir"] = wdir
        config.save_config(self.cfg)

    def _require_network(self):
        """Bails out with a clear message instead of silently starting a Peer to Peer session that has no way to reach anyone."""
        try:
            has_network = nat.guess_local_ip() is not None
        except Exception:
            has_network = False
        if has_network:
            return True
        messagebox.showerror(
            "No Network Connection",
            "Peer to Peer needs a network connection to reach other peers, "
            "and none was found right now. Check your connection and try "
            "again.")
        return False

    def _do_p2p_start(self):
        common = self._validated(self.p2p_name_var, self.p2p_dir_var)
        if not common:
            return
        if not self._require_network():
            return
        name, wdir, _single_file = common
        addr_typed = self._entry_value(self.p2p_addr_entry)
        addr_text = self._entry_value(self.p2p_addr_entry, use_placeholder=True)
        try:
            adv_host, adv_port = parse_address(addr_text)
        except ValueError as e:
            messagebox.showerror("Invalid address", str(e))
            return
        if addr_typed and _is_loopback_host(adv_host):
            messagebox.showwarning(
                "Use the Connect Tab",
                "Peer to Peer is for connecting with peers over the internet. A "
                "loopback address like 127.0.0.1 isn't reachable by anyone else.\n\n"
                "For testing on this machine or your local network, use the "
                "Connect tab's Host/Connect instead.")
            return
        self._set_busy(True, f"Starting mesh on port {adv_port}...")
        self._cancel_requested = threading.Event()

        def on_log(msg):
            self.after(0, lambda: self._set_busy(True, msg))

        try:
            identity = nat.AdvertiseSocket(adv_port, on_log=on_log)
        except OSError as e:
            self._set_busy(False, f"Could not start: {e}")
            return
        self._current_identity = identity

        def worker():
            identity.start()
            if identity.is_closed:
                self.after(0, lambda: self._cancelled(None))
                return
            stun_failed = identity.public_addr is None
            pub_host, pub_port = identity.public_addr or (adv_host, adv_port)
            self.after(0, lambda: self._set_p2p_addr_display(f"{pub_host}:{pub_port}"))
            self._persist_p2p(name, addr_typed, wdir)
            if stun_failed:
                self.after(0, lambda: self._warn_stun_failed(pub_host, pub_port))

            server = Server(adv_port, sock=identity.detach())
            try:
                server.start()
            except OSError as e:
                try:
                    server.stop()
                except Exception:
                    pass
                identity.close()
                self.after(0, lambda msg=str(e): self._set_busy(False, f"Could not start: {msg}"))
                return

            client = Client()
            self._current_client = client
            try:
                client.connect("127.0.0.1", adv_port, name,
                                extra_hello={"p2p_host": pub_host, "p2p_port": pub_port, "is_sequencer": True})
            except ConnectCancelled:
                identity.close()
                self.after(0, lambda: self._cancelled(server))
                return
            except ConnectError as e:
                identity.close()
                self.after(0, lambda msg=str(e): self._fail(msg, server))
                return
            self.after(0, lambda: self._ready("p2p", client, server, wdir, name,
                                               {"p2p_advertise": (pub_host, pub_port),
                                                "p2p_advertise_socket": identity},
                                               recent_path=wdir))

        threading.Thread(target=worker, daemon=True).start()

    def _ask_on_main_thread(self, title, message):
        """messagebox calls must happen on the Tk thread, but this is called from a connection worker thread."""
        result = {}
        done = threading.Event()

        def show():
            result["value"] = messagebox.askyesno(title, message)
            done.set()

        self.after(0, show)
        done.wait()
        return result.get("value", False)

    def _do_p2p_join(self):
        common = self._validated(self.p2p_name_var, self.p2p_dir_var)
        if not common:
            return
        if not self._require_network():
            return
        name, wdir, _single_file = common
        addr_typed = self._entry_value(self.p2p_addr_entry)
        addr_text = self._entry_value(self.p2p_addr_entry, use_placeholder=True)
        try:
            adv_host, adv_port = parse_address(addr_text)
        except ValueError as e:
            messagebox.showerror("Invalid address", str(e))
            return
        join_addr = self._entry_value(self.p2p_join_entry)
        if not join_addr:
            messagebox.showerror("Missing address", "Enter the address of a peer already in the mesh, "
                                                      "or use Start instead.")
            return
        try:
            join_host, join_port = parse_address(join_addr)
        except ValueError as e:
            messagebox.showerror("Invalid address", str(e))
            return
        if _is_loopback_host(join_host) or (addr_typed and _is_loopback_host(adv_host)):
            messagebox.showwarning(
                "Use the Connect Tab",
                "Peer to Peer is for connecting with peers over the internet. A "
                "loopback address like 127.0.0.1 isn't reachable by anyone else.\n\n"
                "For testing on this machine or your local network, use the "
                "Connect tab's Host/Connect instead.")
            return
        self._set_busy(True, f"Connecting to {join_host}:{join_port}...")
        self._current_identity = None
        self._current_client = None
        cancel_requested = threading.Event()
        self._cancel_requested = cancel_requested

        def on_log(msg):
            self.after(0, lambda: self._set_busy(True, msg))

        def worker():
            try:
                probe_ip = nat.quick_public_ip()
            except Exception:
                probe_ip = None
            if cancel_requested.is_set():
                self.after(0, lambda: self._cancelled(None))
                return
            if probe_ip and join_host == probe_ip:
                proceed = self._ask_on_main_thread(
                    "Same Public Address",
                    f"{join_host} is this machine's own public address. If you are "
                    f"testing with another window on this network, it likely will "
                    f"not connect. Use the Connect tab for that instead.\n\n"
                    f"Continue anyway?")
                if not proceed or cancel_requested.is_set():
                    self.after(0, lambda: self._cancelled(None))
                    return

            try:
                identity = nat.AdvertiseSocket(adv_port, on_log=on_log)
            except OSError as e:
                self.after(0, lambda msg=str(e): self._set_busy(False, f"Could not start: {msg}"))
                return
            self._current_identity = identity
            if cancel_requested.is_set():
                identity.close()
                self.after(0, lambda: self._cancelled(None))
                return

            identity.start()
            if identity.is_closed:
                self.after(0, lambda: self._cancelled(None))
                return
            pub_host, pub_port = identity.public_addr or (adv_host, adv_port)
            self.after(0, lambda: self._set_p2p_addr_display(f"{pub_host}:{pub_port}"))
            self._persist_p2p(name, addr_typed, wdir, join_address=join_addr)
            self.after(0, lambda: self._set_busy(True, f"Connecting to {join_host}:{join_port}..."))

            client = Client()
            self._current_client = client
            try:
                client.connect(join_host, join_port, name,
                                extra_hello={"p2p_host": pub_host, "p2p_port": pub_port, "is_sequencer": False})
            except ConnectCancelled:
                identity.close()
                self.after(0, lambda: self._cancelled(None))
                return
            except ConnectError as e:
                identity.close()
                msg = str(e)
                if "timed out" in msg:
                    msg += ("\n\nThis usually means the host's router is not letting your "
                            "connection through - common on carrier-grade NAT (mobile, "
                            "satellite/Starlink-style connections) or a strict firewall. "
                            "Double-check the address, or have the host try port-forwarding "
                            "instead of relying on automatic NAT traversal.")
                self.after(0, lambda msg=msg: self._fail(msg, None))
                return
            self.after(0, lambda: self._ready("p2p", client, None, wdir, name,
                                               {"p2p_advertise": (pub_host, pub_port),
                                                "p2p_advertise_socket": identity},
                                               recent_path=wdir))

        threading.Thread(target=worker, daemon=True).start()

    def _warn_stun_failed(self, host, port):
        messagebox.showwarning(
            "Could Not Detect a Public Address",
            f"No STUN server answered, so the address being shared "
            f"({host}:{port}) is this machine's local network address, not "
            f"a public one. Anyone outside this network who tries to Join "
            f"at that address will get no response.\n\n"
            f"This usually means outbound UDP is blocked (a firewall, VPN, "
            f"or a network that blocks STUN) rather than anything wrong "
            f"with the mesh itself. Check your network/firewall, or share "
            f"a manually port-forwarded address instead.")

    def _set_busy(self, busy, text=""):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in self._action_buttons:
            btn.configure(state=state)
        if busy:
            self.cancel_btn.pack(pady=(0, 4))
        else:
            self.cancel_btn.pack_forget()
            self._current_client = None
            self._current_identity = None
        self.status.configure(text=text)
        self.update_idletasks()

    def _cancel_current(self):
        if self._current_client:
            self._current_client.cancel_connect()
        if self._current_identity:
            self._current_identity.close()
        self._cancel_requested.set()
        self._set_busy(True, "Cancelling...")
        self.cancel_btn.configure(state="disabled")

    def _cancelled(self, server):
        if server:
            try:
                server.stop()
            except Exception:
                pass
        self._set_busy(False, "Connection cancelled.")
        self.cancel_btn.configure(state="normal")

    def _fail(self, message, server=None):
        if server:
            try:
                server.stop()
            except Exception:
                pass
        self._set_busy(False, message)
        self.cancel_btn.configure(state="normal")

    def _ready(self, mode, client, server, wdir, name, extra=None, recent_path=None):
        self._current_client = None
        # Registered only now that the session is actually live, and before on_ready, which may tear this screen down.
        if recent_path:
            self._register_recent(recent_path)
        self.on_ready(mode, client, server, wdir, name, extra)

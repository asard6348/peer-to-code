import tkinter as tk
from tkinter import ttk, colorchooser, messagebox, simpledialog, font as tkfont

import config
import scrollutil

IGNORED_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Caps_Lock", "Num_Lock", "Super_L", "Super_R", "Meta_L", "Meta_R",
}

# The ten color settings, grouped by what they actually affect and each
# given a one-line description - rather than one flat, alphabetically-ish
# list of names like "App background" / "Editor text" that read as
# self-explanatory but aren't: "fg" colors nearly all text in the app
# (buttons, dialogs, panels), not just the editor; "sel_bg" colors
# button hover/focus and hovered list rows as much as it colors text
# selection; "bg" is specifically the toolbar/connect-screen chrome, not
# a catch-all "background of everything". Each (key, title, description)
# tuple here replaces the old flat THEME_FIELDS list.
THEME_FIELD_GROUPS = [
    ("Text & Highlights", [
        ("fg", "Primary text",
         "Nearly all text in the app: buttons, labels, dialogs, and the code editor."),
        ("muted_fg", "Secondary text",
         "De-emphasized text: hints, status messages, and unselected tab labels."),
        ("accent", "Accent",
         "Pressed buttons, the active tab's underline, and other highlighted controls."),
        ("sel_bg", "Highlight / hover",
         "Selected text, hovered list rows, and hovered or focused buttons - "
         "not just text selection, despite the name."),
    ]),
    ("Backgrounds", [
        ("bg", "Window chrome",
         "Behind the toolbar and the connect screen. Not the editor or side panels."),
        ("panel_bg", "Panels & dialogs",
         "This Settings window, other dialogs, and the tab strip."),
        ("edit_bg", "Editor & input fields",
         "The code editor itself, plus text entry fields."),
        ("console_bg", "Terminal",
         "The terminal/output panel only."),
    ]),
    ("Line Numbers", [
        ("gutter_bg", "Gutter background", "Behind the line numbers, left of the editor."),
        ("gutter_fg", "Gutter text", "The line number digits themselves."),
    ]),
]
# Flat (key, title) view of the same data, kept for code that just needs
# every key/title pair without caring about grouping.
THEME_FIELDS = [(key, title) for _group, fields in THEME_FIELD_GROUPS for key, title, _desc in fields]


def _capture_accel(event):
    if event.keysym in IGNORED_KEYSYMS:
        return None
    mods = []
    if event.state & 0x0004:
        mods.append("Control")
    if event.state & 0x0008:
        mods.append("Alt")
    if event.state & 0x0001:
        mods.append("Shift")
    return "<" + "-".join(mods + [event.keysym]) + ">"


class SettingsWindow(tk.Toplevel):
    """Shortcuts + Theme + General, all editable and written straight to the
    JSON config on save. on_apply(set_of_changed_sections) lets the editor
    rebind keys / re-theme / pick up other changes immediately without a
    restart."""

    def __init__(self, app, cfg, on_apply=None):
        super().__init__(app)
        self.app = app
        self.cfg = cfg
        self.on_apply = on_apply
        t = app.theme
        self.title("Settings")
        self.configure(bg=t["panel_bg"])
        self.geometry("560x520")
        self.minsize(420, 320)
        self.transient(app.winfo_toplevel())

        self.shortcuts_working = dict(cfg["shortcuts"])
        self.output_shortcuts_working = dict(cfg.setdefault("output_shortcuts", {}))
        self.theme_working = dict(cfg["theme"])
        self._orig_shortcuts = dict(cfg["shortcuts"])
        self._orig_output_shortcuts = dict(cfg["output_shortcuts"])
        self._orig_theme = dict(cfg["theme"])
        self._orig_default_language = cfg.setdefault("editor", {}).get("default_new_file_language", "python")
        self._orig_syntax_theme = cfg.setdefault("editor", {}).get("syntax_theme", "auto")
        self._orig_custom_colors = {k: dict(v) for k, v in cfg.setdefault("editor", {})
                                     .get("custom_syntax_colors", {}).items()}
        self._orig_terminal_messages = dict(cfg.setdefault("editor", {}).setdefault(
            "terminal_messages", dict(config.DEFAULTS["editor"]["terminal_messages"])))
        self._orig_word_wrap = cfg.setdefault("editor", {}).get(
            "word_wrap", config.DEFAULTS["editor"]["word_wrap"])
        ui_cfg_init = cfg.setdefault("ui", {})
        self._orig_explorer_font_family = ui_cfg_init.get(
            "explorer_font_family", getattr(app, "_explorer_font_family", "sans-serif"))
        self._orig_explorer_font_size = int(ui_cfg_init.get(
            "explorer_font_size", getattr(app, "_explorer_font_size", 9)))
        self._orig_console_font_family = ui_cfg_init.get(
            "console_font_family", getattr(app, "_console_font_family", "Consolas"))
        self._orig_console_font_size = int(ui_cfg_init.get(
            "console_font_size", getattr(app, "_console_font_size", 10)))
        self._orig_theme_preset = ui_cfg_init.get("theme_preset", "system")
        self._orig_theme_presets = {k: dict(v) for k, v in cfg.setdefault("theme_presets", {}).items()}
        self._orig_syntax_palette_presets = {
            k: {tag: dict(colors) for tag, colors in v.items()}
            for k, v in cfg.setdefault("syntax_palette_presets", {}).items()
        }
        self.color_vars = {}
        self._advanced_colors_visible = False
        self._listening_action = None
        self._row_widgets = {}
        self._action_scope = {}
        self._suspend_preview = False
        # Every classic (non-ttk) widget in this window that was given an
        # explicit theme color at construction time, so a live theme
        # change (_apply_live_theme, called from _preview) can put the
        # new colors on all of them - the same live update ttk widgets
        # already get for free from restyling "TFrame"/"TLabel"/etc.
        # Populated by _reg() as each widget is built; see _reg's
        # docstring for the (widget, {option: theme_key}) shape.
        self._theme_widgets = []

        footer = self._reg(tk.Frame(self, bg=t["panel_bg"]), bg="panel_bg")
        footer.pack(side="bottom", fill="x", padx=10, pady=10)
        tk.Button(footer, text="Save", command=self._save).pack(side="right", padx=4)
        tk.Button(footer, text="Cancel", command=self._cancel).pack(side="right")
        self.reset_btn = tk.Button(footer, text="Reset to Defaults", command=self._reset_defaults)
        self.reset_btn.pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.nb = nb

        general_tab = self._reg(tk.Frame(nb, bg=t["panel_bg"]), bg="panel_bg")
        shortcuts_tab = self._reg(tk.Frame(nb, bg=t["panel_bg"]), bg="panel_bg")
        theme_tab = self._reg(tk.Frame(nb, bg=t["panel_bg"]), bg="panel_bg")
        config_tab = self._reg(tk.Frame(nb, bg=t["panel_bg"]), bg="panel_bg")
        nb.add(general_tab, text="General")
        nb.add(shortcuts_tab, text="Shortcuts")
        nb.add(theme_tab, text="Theme")
        nb.add(config_tab, text="Config File")
        self._tab_ids = {
            str(general_tab): "General",
            str(shortcuts_tab): "Shortcuts",
            str(theme_tab): "Theme",
            str(config_tab): "Config File",
        }

        self._build_general_tab(general_tab)
        self._build_shortcuts_tab(shortcuts_tab)
        self._build_theme_tab(theme_tab)
        self._build_config_tab(config_tab)

        self.bind("<KeyPress>", self._on_keypress)
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._on_tab_changed()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _make_scrollable_rows(self, parent, t):
        """A vertically scrollable rows area whose content frame is kept in
        sync with the canvas's own rendered width (via the <Configure>
        binding below) - without this, `rows` just shrink-wraps to its own
        requested width and stays that size forever, so widths inside it
        can never actually track the window (see individual tabs, which
        rely on this: they configure column 0 to expand into whatever room
        this creates, and their labels rewrap to that column's new width
        rather than getting clipped or leaving dead space)."""
        canvas = self._reg(tk.Canvas(parent, bg=t["panel_bg"], highlightthickness=0), bg="panel_bg")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        rows = self._reg(tk.Frame(canvas, bg=t["panel_bg"]), bg="panel_bg")
        window_id = canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        scrollutil.bind_wheel(canvas, rows)
        return canvas, rows

    def _current_tab(self):
        return self._tab_ids.get(str(self.nb.select()), "Shortcuts")

    def _on_tab_changed(self, _event=None):
        state = "normal" if self._current_tab() != "Config File" else "disabled"
        self.reset_btn.configure(state=state)

    def _reg(self, widget, **color_options):
        """Registers a classic widget's theme-derived constructor options
        (e.g. bg="panel_bg", fg="muted_fg") so _apply_live_theme can put
        current colors on it later. `color_options` maps a tk config
        option name to the THEME_COLOR_KEYS key that should supply its
        value. Returns `widget` unchanged so calls can stay inline
        (`self._reg(tk.Label(...), bg="panel_bg").grid(...)`).

        Deliberately not used for widgets whose bg/fg IS the data being
        shown rather than decoration - the color swatches in the Theme
        tab and the Advanced Syntax Colors dialog - those already get
        refreshed with the right (possibly custom) color by
        _refresh_theme_ui / their own pick() callback."""
        if color_options:
            self._theme_widgets.append((widget, color_options))
        return widget

    def _apply_live_theme(self):
        """Pushes self.theme_working's current colors onto every classic
        widget _reg registered, plus this window's own background -
        mirrors what ttk.Style reconfiguration already does for free for
        every ttk-styled widget, so a theme preview (or a preset/system
        switch) repaints the whole Settings window immediately instead
        of just the parts ttk happens to own."""
        t = self.theme_working
        self.configure(bg=t["panel_bg"])
        for widget, color_options in self._theme_widgets:
            try:
                widget.configure(**{opt: t[key] for opt, key in color_options.items()})
            except tk.TclError:
                pass

    def _preview(self, sections):
        """Apply working changes to the live app immediately (not to disk)
        so the user can see the effect before deciding to Save or Cancel."""
        if self._suspend_preview:
            return
        if "shortcuts" in sections:
            self.cfg["shortcuts"] = dict(self.shortcuts_working)
        if "output_shortcuts" in sections:
            self.cfg["output_shortcuts"] = dict(self.output_shortcuts_working)
        if "theme" in sections:
            self.theme_working["font_family"] = self.font_family_var.get() or "Consolas"
            try:
                self.theme_working["font_size"] = int(self.font_size_var.get())
            except (tk.TclError, ValueError):
                pass
            self.cfg["theme"] = dict(self.theme_working)
            self._apply_live_theme()
            self._refresh_syntax_theme_description()
        if "ui" in sections:
            ui_cfg = self.cfg.setdefault("ui", {})
            ui_cfg["save_window_position"] = self.save_window_position_var.get()
            ui_cfg["save_window_size"] = self.save_window_size_var.get()
        if self.on_apply:
            try:
                self.on_apply(set(sections))
            except tk.TclError:
                pass

    def _row_label(self, rows, row, text, **grid_kwargs):
        lbl = self._reg(
            tk.Label(rows, text=text, bg=self.app.theme["panel_bg"], fg=self.app.theme["fg"],
                      anchor="w", justify="left"),
            bg="panel_bg", fg="fg")
        grid_kwargs.setdefault("sticky", "we")
        grid_kwargs.setdefault("padx", (4, 8))
        grid_kwargs.setdefault("pady", 4)
        lbl.grid(row=row, column=0, **grid_kwargs)
        lbl.bind("<Configure>", lambda e, l=lbl: l.configure(wraplength=max(60, e.width)))
        return lbl

    def _syntax_theme_desc_text(self, key):
        """COLOR_THEMES[key]'s description, with "auto" additionally
        noting which built-in palette it currently resolves to - against
        the Theme tab's own (possibly unsaved) editor-background color,
        so this stays accurate while previewing an unsaved Theme change,
        not just after Save."""
        import syntax
        desc = syntax.COLOR_THEMES[key]["description"]
        if key == syntax.AUTO_COLOR_THEME:
            edit_bg = self.theme_working.get("edit_bg", self._orig_theme.get("edit_bg"))
            resolved = syntax.resolve_auto_theme(edit_bg)
            desc += f" Right now: {syntax.COLOR_THEMES[resolved]['label']}."
        return desc

    def _refresh_syntax_theme_description(self):
        if not hasattr(self, "syntax_theme_desc_label"):
            return
        import syntax
        key = self.cfg.get("editor", {}).get("syntax_theme", syntax.DEFAULT_COLOR_THEME)
        self.syntax_theme_desc_label.configure(text=self._syntax_theme_desc_text(key))

    def _build_general_tab(self, parent):
        import syntax

        t = self.app.theme
        _canvas, rows = self._make_scrollable_rows(parent, t)
        rows.columnconfigure(0, weight=1)

        def section_header(row, text, pady=(16, 2)):
            self._reg(
                tk.Label(rows, text=text, bg=t["panel_bg"], fg=t["fg"], anchor="w",
                         font=("Segoe UI", 9, "bold")),
                bg="panel_bg", fg="fg",
            ).grid(row=row, column=0, columnspan=2, sticky="we", padx=4, pady=pady)

        row = 0
        section_header(row, "Editor", pady=(4, 2))
        row += 1

        self._row_label(rows, row, "Default language for new buffers")

        self._language_choices = list(syntax.LANGUAGE_LABELS.items())
        self._label_to_language = {label: code for code, label in self._language_choices}
        current_label = syntax.LANGUAGE_LABELS.get(self._orig_default_language, "Python")

        self.default_language_var = tk.StringVar(value=current_label)
        combo = ttk.Combobox(rows, textvariable=self.default_language_var, state="readonly", width=16,
                              values=[label for _code, label in self._language_choices])
        combo.grid(row=row, column=1, sticky="e", padx=(0, 4), pady=4)
        combo.bind("<<ComboboxSelected>>", self._on_default_language_selected)
        row += 1

        self.word_wrap_var = tk.BooleanVar(value=self._orig_word_wrap)
        self._reg(tk.Checkbutton(
            rows, text="Wrap long lines onto more rows instead of scrolling sideways",
            variable=self.word_wrap_var, bg=t["panel_bg"], fg=t["fg"],
            selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
            highlightthickness=0, anchor="w", command=self._on_word_wrap_toggled,
        ), bg="panel_bg", fg="fg", selectcolor="edit_bg", activebackground="panel_bg",
           activeforeground="fg").grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=(0, 4))
        row += 1

        ui_cfg = self.cfg.setdefault("ui", {})
        self._orig_save_window_position = ui_cfg.get("save_window_position", True)
        self._orig_save_window_size = ui_cfg.get("save_window_size", True)
        self.save_window_position_var = tk.BooleanVar(value=self._orig_save_window_position)
        self.save_window_size_var = tk.BooleanVar(value=self._orig_save_window_size)

        section_header(row, "Window")
        row += 1
        self._reg(tk.Checkbutton(
            rows, text="Remember window position between launches",
            variable=self.save_window_position_var, bg=t["panel_bg"], fg=t["fg"],
            selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
            highlightthickness=0, anchor="w", command=lambda: self._preview({"ui"}),
        ), bg="panel_bg", fg="fg", selectcolor="edit_bg", activebackground="panel_bg",
           activeforeground="fg").grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=2)
        row += 1
        self._reg(tk.Checkbutton(
            rows, text="Remember window size between launches",
            variable=self.save_window_size_var, bg=t["panel_bg"], fg=t["fg"],
            selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
            highlightthickness=0, anchor="w", command=lambda: self._preview({"ui"}),
        ), bg="panel_bg", fg="fg", selectcolor="edit_bg", activebackground="panel_bg",
           activeforeground="fg").grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=(2, 4))
        row += 1
        window_desc_label = self._reg(tk.Label(
            rows, text="Unchecking one deletes it from the config file on close, so next time "
                       "the window opens with that part placed/sized automatically.",
            bg=t["panel_bg"], fg=t["muted_fg"], anchor="w", justify="left"),
            bg="panel_bg", fg="muted_fg")
        window_desc_label.grid(row=row, column=0, columnspan=2, sticky="we", padx=4, pady=(0, 10))
        window_desc_label.bind(
            "<Configure>", lambda e: window_desc_label.configure(wraplength=max(60, e.width)))
        row += 1

        section_header(row, "Terminal")
        row += 1
        self.terminal_message_vars = {
            key: tk.BooleanVar(value=self._orig_terminal_messages.get(key, True))
            for key in ("finished", "stopped", "interrupted")
        }
        terminal_message_labels = {
            "finished": "Show [finished] when a run completes",
            "stopped": "Show [stopped] when Stop ends a run",
            "interrupted": "Show [interrupted] when Ctrl+C ends a run",
        }
        for key in ("finished", "stopped", "interrupted"):
            self._reg(tk.Checkbutton(
                rows, text=terminal_message_labels[key],
                variable=self.terminal_message_vars[key], bg=t["panel_bg"], fg=t["fg"],
                selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
                highlightthickness=0, anchor="w",
                command=lambda k=key: self._on_terminal_message_toggled(k),
            ), bg="panel_bg", fg="fg", selectcolor="edit_bg", activebackground="panel_bg",
               activeforeground="fg").grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=2)
            row += 1

    def _on_explorer_font_size_changed(self):
        try:
            size = int(self.explorer_font_size_var.get())
        except (tk.TclError, ValueError):
            return
        self.cfg.setdefault("ui", {})["explorer_font_size"] = size
        self._preview({"ui"})

    def _on_explorer_font_family_changed(self, _event=None):
        family = self.explorer_font_family_var.get()
        if not family:
            return
        self.cfg.setdefault("ui", {})["explorer_font_family"] = family
        self._preview({"ui"})

    def _on_console_font_size_changed(self):
        try:
            size = int(self.console_font_size_var.get())
        except (tk.TclError, ValueError):
            return
        self.cfg.setdefault("ui", {})["console_font_size"] = size
        self._preview({"ui"})

    def _on_console_font_family_changed(self, _event=None):
        family = self.console_font_family_var.get()
        if not family:
            return
        self.cfg.setdefault("ui", {})["console_font_family"] = family
        self._preview({"ui"})

    def _on_word_wrap_toggled(self):
        self.cfg.setdefault("editor", {})["word_wrap"] = self.word_wrap_var.get()
        self._preview({"editor"})

    def _on_terminal_message_toggled(self, key):
        term_cfg = self.cfg.setdefault("editor", {}).setdefault("terminal_messages", {})
        term_cfg[key] = self.terminal_message_vars[key].get()
        self._preview({"editor"})

    def _on_default_language_selected(self, _event=None):
        code = self._label_to_language.get(self.default_language_var.get(), "python")
        self.cfg.setdefault("editor", {})["default_new_file_language"] = code
        self._preview({"editor"})

    def _on_syntax_theme_selected(self, _event=None):
        import syntax
        key = self._label_to_syntax_theme.get(self.syntax_theme_var.get(), syntax.DEFAULT_COLOR_THEME)
        self.cfg.setdefault("editor", {})["syntax_theme"] = key
        self.syntax_theme_desc_label.configure(text=self._syntax_theme_desc_text(key))
        self._preview({"editor"})

    def _open_advanced_syntax_colors(self):
        """Per-token-category color picker, for anyone the two built-in
        palettes don't quite suit. Picking any color here immediately
        switches the palette dropdown above to "Custom" and previews it
        live, same as the Theme tab's own color swatches do - there's
        deliberately no separate Save/Cancel in this sub-window; the
        outer Settings window's own Save/Cancel already covers whatever
        got changed in here too (see _orig_custom_colors)."""
        import syntax

        t = self.app.theme
        dlg = tk.Toplevel(self)
        dlg.title("Advanced: Syntax Colors")
        self._reg(dlg, bg="panel_bg")
        dlg.configure(bg=t["panel_bg"])
        dlg.transient(self)
        dlg.geometry("440x380")
        dlg.minsize(360, 280)

        tk.Button(dlg, text="Close", command=dlg.destroy).pack(side="bottom", pady=10)

        _canvas, rows = self._make_scrollable_rows(dlg, t)
        rows.columnconfigure(0, weight=1)

        working = {name: dict(syntax.COLOR_THEMES["custom"]["colors"].get(name, {})) for name in syntax.TAG_NAMES}

        def pick(tag, attr, swatch):
            _rgb, hex_color = colorchooser.askcolor(color=swatch.cget("bg"), parent=dlg)
            if not hex_color:
                return
            working[tag][attr] = hex_color
            swatch.configure(bg=hex_color)
            overrides = {name: dict(v) for name, v in working.items()}
            syntax.set_custom_colors(overrides)
            self.cfg.setdefault("editor", {})["custom_syntax_colors"] = overrides
            self.cfg.setdefault("editor", {})["syntax_theme"] = "custom"
            self.syntax_theme_var.set(syntax.COLOR_THEMES["custom"]["label"])
            self.syntax_theme_desc_label.configure(text=self._syntax_theme_desc_text("custom"))
            self._update_syntax_preset_buttons_state()
            self._refresh_preset_tiles()
            self._preview({"editor"})

        for row, tag in enumerate(syntax.TAG_NAMES):
            attr = syntax.TAG_COLOR_ATTR[tag]
            label = syntax.TAG_LABELS.get(tag, tag)
            if attr == "background":
                label += "  (background color)"
            self._row_label(rows, row, label)
            fallback = t["edit_bg"] if attr == "background" else t["fg"]
            current = working[tag].get(attr) or fallback
            swatch = tk.Label(rows, text="  " * 6, bg=current, relief="flat", bd=1)
            swatch.grid(row=row, column=1, sticky="w", pady=4)
            tk.Button(rows, text="Choose", command=lambda tg=tag, at=attr, sw=swatch: pick(tg, at, sw))\
                .grid(row=row, column=2, padx=6, pady=4)

    def _build_shortcuts_tab(self, parent):
        from editor import SHORTCUT_SPECS, OUTPUT_SHORTCUT_SPECS, accel_display
        self._SHORTCUT_SPECS = SHORTCUT_SPECS
        self._OUTPUT_SHORTCUT_SPECS = OUTPUT_SHORTCUT_SPECS
        self._accel_display = accel_display
        t = self.app.theme

        _canvas, rows = self._make_scrollable_rows(parent, t)
        rows.columnconfigure(0, weight=1)

        self._reg(tk.Label(rows, text="Editor", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                            font=("Segoe UI", 9, "bold")), bg="panel_bg", fg="fg").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(4, 4))
        row = self._add_shortcut_rows(rows, 1, SHORTCUT_SPECS, self.shortcuts_working, "shortcuts")

        self._reg(tk.Label(rows, text="Terminal", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                            font=("Segoe UI", 9, "bold")), bg="panel_bg", fg="fg").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(14, 4))
        row += 1
        self._reg(tk.Label(
            rows, text="Shortcuts that only apply while the Terminal panel has focus.",
            bg=t["panel_bg"], fg=t["muted_fg"], anchor="w", justify="left",
        ), bg="panel_bg", fg="muted_fg").grid(row=row, column=0, columnspan=3, sticky="we", pady=(0, 6))
        row += 1
        self._add_shortcut_rows(rows, row, OUTPUT_SHORTCUT_SPECS, self.output_shortcuts_working, "output_shortcuts")

    def _add_shortcut_rows(self, rows, start_row, specs, working, scope):
        row = start_row
        for action_id, label, _default_accel, _method in specs:
            self._action_scope[action_id] = (working, scope)
            self._row_label(rows, row, label)
            accel_label = self._reg(
                tk.Label(rows, text=self._accel_display(working.get(action_id, "")),
                         bg=self.app.theme["edit_bg"], fg=self.app.theme["fg"], width=16,
                         anchor="w", padx=6),
                bg="edit_bg", fg="fg")
            accel_label.grid(row=row, column=1, sticky="w", pady=4)
            btn = tk.Button(rows, text="Change", command=lambda a=action_id: self._start_listen(a))
            btn.grid(row=row, column=2, padx=6, pady=4)
            self._row_widgets[action_id] = (accel_label, btn)
            row += 1
        return row

    def _start_listen(self, action_id):
        if self._listening_action == action_id:
            working, _scope = self._action_scope[action_id]
            label, btn = self._row_widgets[action_id]
            btn.configure(text="Change")
            label.configure(text=self._accel_display(working.get(action_id, "")))
            self._listening_action = None
            return
        if self._listening_action:
            prev_working, _prev_scope = self._action_scope[self._listening_action]
            label, btn = self._row_widgets[self._listening_action]
            btn.configure(text="Change")
            label.configure(text=self._accel_display(prev_working.get(self._listening_action, "")))
        self._listening_action = action_id
        label, btn = self._row_widgets[action_id]
        btn.configure(text="Press keys...")
        label.configure(text="...")
        self.focus_set()

    def _on_keypress(self, event):
        if not self._listening_action:
            return
        accel = _capture_accel(event)
        if accel is None:
            return
        action_id = self._listening_action
        working, scope = self._action_scope[action_id]
        working[action_id] = accel
        label, btn = self._row_widgets[action_id]
        label.configure(text=self._accel_display(accel))
        btn.configure(text="Change")
        self._listening_action = None
        self._preview({scope})

    def _section_header(self, rows, row, text, pady=(16, 2), columnspan=2):
        t = self.app.theme
        self._reg(
            tk.Label(rows, text=text, bg=t["panel_bg"], fg=t["fg"], anchor="w",
                     font=("Segoe UI", 9, "bold")),
            bg="panel_bg", fg="fg",
        ).grid(row=row, column=0, columnspan=columnspan, sticky="we", padx=4, pady=pady)

    def _refresh_preset_choice_maps(self):
        """Rebuilds the label<->key maps the preset combobox uses: the
        three fixed entries first (so they always sort to the top, most
        useful ones first), then the user's saved presets alphabetically.
        A custom preset's key and its displayed label are the same
        string - it's just whatever name the user gave it. Labels come
        from theme.py itself (BUILTIN_THEME_PRESETS' "label" fields and
        SYSTEM_PRESET_LABEL) rather than being duplicated here, so
        renaming a built-in preset only ever means editing theme.py."""
        import theme

        self._preset_key_to_label = {theme.SYSTEM_PRESET: theme.SYSTEM_PRESET_LABEL}
        for key, spec in theme.BUILTIN_THEME_PRESETS.items():
            self._preset_key_to_label[key] = spec["label"]
        for name in sorted(self.cfg.get("theme_presets", {}).keys(), key=str.lower):
            self._preset_key_to_label[name] = name
        self._preset_label_to_key = {label: key for key, label in self._preset_key_to_label.items()}

    def _current_preset_key(self):
        import theme
        return self._preset_label_to_key.get(self.theme_preset_var.get(), theme.SYSTEM_PRESET)

    def _update_preset_combo_values(self):
        self.preset_combo.configure(values=list(self._preset_key_to_label.values()))

    def _update_preset_buttons_state(self):
        import theme
        is_custom = self._current_preset_key() not in theme.RESERVED_PRESET_NAMES
        state = "normal" if is_custom else "disabled"
        self.rename_preset_btn.configure(state=state)
        self.remove_preset_btn.configure(state=state)

    def _update_preset_desc_label(self):
        import theme

        key = self._current_preset_key()
        if key == theme.SYSTEM_PRESET:
            mode = theme.BUILTIN_THEME_PRESETS["dark" if theme.detect_system_dark_mode() else "light"]["label"]
            text = f"Follows your system's light/dark setting. Right now: {mode}."
        elif key in theme.BUILTIN_THEME_PRESETS:
            text = theme.BUILTIN_THEME_PRESETS[key]["description"]
        else:
            text = "A saved preset of your own. Tweak any color below, then Save As to update it."
        self.preset_desc_label.configure(text=text)

    def _on_theme_preset_selected(self, _event=None):
        import theme

        key = self._current_preset_key()
        self.theme_working.update(theme.resolve_preset_colors(key, self.cfg.get("theme_presets", {})))
        self.cfg.setdefault("ui", {})["theme_preset"] = key
        self._refresh_theme_ui()
        self._update_preset_buttons_state()
        self._update_preset_desc_label()
        self._preview({"theme", "ui"})

    def _save_theme_preset(self):
        import theme

        self.theme_working["font_family"] = self.font_family_var.get() or "Consolas"
        name = simpledialog.askstring(
            "Save Color Theme Preset", "Preset name:", parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        if name.lower() in theme.RESERVED_PRESET_NAMES:
            messagebox.showerror(
                "Save Color Theme Preset",
                f'"{name}" is a reserved name. Choose a different name.', parent=self)
            return
        presets = self.cfg.setdefault("theme_presets", {})
        presets[name] = {key: self.theme_working[key] for key in theme.THEME_COLOR_KEYS}
        self.cfg.setdefault("ui", {})["theme_preset"] = name
        self._refresh_preset_choice_maps()
        self._update_preset_combo_values()
        self.theme_preset_var.set(self._preset_key_to_label.get(name, name))
        self._update_preset_buttons_state()
        self._update_preset_desc_label()
        self._refresh_preset_tiles()
        self._preview({"ui"})

    def _rename_theme_preset(self):
        import theme

        key = self._current_preset_key()
        presets = self.cfg.setdefault("theme_presets", {})
        if key not in presets:
            return
        new_name = simpledialog.askstring(
            "Rename Color Theme Preset", "New name:", initialvalue=key, parent=self)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == key:
            return
        if new_name.lower() in theme.RESERVED_PRESET_NAMES:
            messagebox.showerror(
                "Rename Color Theme Preset",
                f'"{new_name}" is a reserved name. Choose a different name.', parent=self)
            return
        if new_name in presets and not messagebox.askyesno(
                "Rename Color Theme Preset",
                f'A preset named "{new_name}" already exists. Overwrite it?', parent=self):
            return
        presets[new_name] = presets.pop(key)
        self.cfg.setdefault("ui", {})["theme_preset"] = new_name
        self._refresh_preset_choice_maps()
        self._update_preset_combo_values()
        self.theme_preset_var.set(self._preset_key_to_label.get(new_name, new_name))
        self._update_preset_buttons_state()
        self._update_preset_desc_label()
        self._refresh_preset_tiles()
        self._preview({"ui"})

    def _remove_theme_preset(self):
        import theme

        key = self._current_preset_key()
        presets = self.cfg.setdefault("theme_presets", {})
        if key not in presets:
            return
        if not messagebox.askyesno(
                "Remove Color Theme Preset",
                f'Remove the "{key}" preset? This can\'t be undone.', parent=self):
            return
        del presets[key]
        self._refresh_preset_choice_maps()
        self._update_preset_combo_values()
        self.theme_preset_var.set(theme.SYSTEM_PRESET_LABEL)
        # Route through the normal selection handler rather than just
        # setting the combobox's text: a StringVar.set() doesn't fire
        # <<ComboboxSelected>>, so the old code here never actually
        # re-resolved and applied System's colors - the picker *showed*
        # System while theme_working (and the live preview) silently kept
        # whatever the just-removed preset last looked like, until the
        # user clicked the System entry themselves. Calling the handler
        # directly is what actually loads and previews System's colors.
        self._on_theme_preset_selected()

    def _refresh_syntax_choice_maps(self):
        """Rebuilds the label<->key maps the syntax palette combobox
        uses, straight from syntax.COLOR_THEMES - so it automatically
        picks up whatever sync_saved_palettes() last put there. Mirrors
        _refresh_preset_choice_maps()."""
        import syntax
        self._syntax_theme_choices = [(key, spec["label"]) for key, spec in syntax.COLOR_THEMES.items()]
        self._label_to_syntax_theme = {label: key for key, label in self._syntax_theme_choices}

    def _current_syntax_key(self):
        import syntax
        return self._label_to_syntax_theme.get(self.syntax_theme_var.get(), syntax.DEFAULT_COLOR_THEME)

    def _update_syntax_preset_combo_values(self):
        self.syntax_combo.configure(values=[label for _key, label in self._syntax_theme_choices])

    def _update_syntax_preset_buttons_state(self):
        import syntax
        is_saved = self._current_syntax_key() not in syntax.RESERVED_PALETTE_NAMES
        state = "normal" if is_saved else "disabled"
        self.rename_syntax_preset_btn.configure(state=state)
        self.remove_syntax_preset_btn.configure(state=state)

    def _on_syntax_theme_selected(self, _event=None):
        import syntax
        key = self._current_syntax_key()
        self.cfg.setdefault("editor", {})["syntax_theme"] = key
        self.syntax_theme_desc_label.configure(text=self._syntax_theme_desc_text(key))
        self._update_syntax_preset_buttons_state()
        self._refresh_preset_tiles()
        self._preview({"editor"})

    def _save_syntax_palette_preset(self):
        import syntax

        name = simpledialog.askstring(
            "Save Syntax Palette Preset", "Preset name:", parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        if name.lower() in syntax.RESERVED_PALETTE_NAMES:
            messagebox.showerror(
                "Save Syntax Palette Preset",
                f'"{name}" is a reserved name. Choose a different name.', parent=self)
            return
        presets = self.cfg.setdefault("syntax_palette_presets", {})
        # Snapshots whatever palette is actually resolved and on screen
        # right now (works whether that's Auto, a built-in, or another
        # saved preset) rather than requiring "Custom" to be active first.
        presets[name] = syntax.active_palette_colors()
        syntax.sync_saved_palettes(presets)
        self.cfg.setdefault("editor", {})["syntax_theme"] = name
        self._refresh_syntax_choice_maps()
        self._update_syntax_preset_combo_values()
        self.syntax_theme_var.set(name)
        self.syntax_theme_desc_label.configure(text=self._syntax_theme_desc_text(name))
        self._update_syntax_preset_buttons_state()
        self._refresh_preset_tiles()
        self._preview({"editor"})

    def _rename_syntax_palette_preset(self):
        import syntax

        key = self._current_syntax_key()
        presets = self.cfg.setdefault("syntax_palette_presets", {})
        if key not in presets:
            return
        new_name = simpledialog.askstring(
            "Rename Syntax Palette Preset", "New name:", initialvalue=key, parent=self)
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name or new_name == key:
            return
        if new_name.lower() in syntax.RESERVED_PALETTE_NAMES:
            messagebox.showerror(
                "Rename Syntax Palette Preset",
                f'"{new_name}" is a reserved name. Choose a different name.', parent=self)
            return
        if new_name in presets and not messagebox.askyesno(
                "Rename Syntax Palette Preset",
                f'A preset named "{new_name}" already exists. Overwrite it?', parent=self):
            return
        presets[new_name] = presets.pop(key)
        syntax.sync_saved_palettes(presets)
        self.cfg.setdefault("editor", {})["syntax_theme"] = new_name
        self._refresh_syntax_choice_maps()
        self._update_syntax_preset_combo_values()
        self.syntax_theme_var.set(new_name)
        self.syntax_theme_desc_label.configure(text=self._syntax_theme_desc_text(new_name))
        self._update_syntax_preset_buttons_state()
        self._refresh_preset_tiles()
        self._preview({"editor"})

    def _remove_syntax_palette_preset(self):
        import syntax

        key = self._current_syntax_key()
        presets = self.cfg.setdefault("syntax_palette_presets", {})
        if key not in presets:
            return
        if not messagebox.askyesno(
                "Remove Syntax Palette Preset",
                f'Remove the "{key}" preset? This can\'t be undone.', parent=self):
            return
        del presets[key]
        syntax.sync_saved_palettes(presets)
        self._refresh_syntax_choice_maps()
        self._update_syntax_preset_combo_values()
        self.syntax_theme_var.set(syntax.COLOR_THEMES[syntax.DEFAULT_COLOR_THEME]["label"])
        # Same fix as _remove_theme_preset below: go through the real
        # selection handler so Auto's colors are actually resolved and
        # previewed, instead of just relabeling the combobox.
        self._on_syntax_theme_selected()

    def _build_presets_section(self, rows, t):
        """The combined "Presets" area at the top of the Theme tab: a
        Color Theme preset picker and a Syntax Palette preset picker,
        side by side since together they decide the app's whole look -
        each with its own Save As/Rename/Remove, plus a row of clickable
        preview tiles below both (see _refresh_preset_tiles) so picking a
        look doesn't require opening the Advanced section at all. Returns
        the next free grid row."""
        import syntax
        import theme

        self._section_header(rows, 0, "Presets", pady=(4, 4), columnspan=3)
        row = 1

        self._row_label(rows, row, "Color theme")
        self._refresh_preset_choice_maps()
        self.theme_preset_var = tk.StringVar(
            value=self._preset_key_to_label.get(self._orig_theme_preset, theme.SYSTEM_PRESET_LABEL))
        self.preset_combo = ttk.Combobox(
            rows, textvariable=self.theme_preset_var, state="readonly", width=16,
            values=list(self._preset_key_to_label.values()))
        self.preset_combo.grid(row=row, column=1, columnspan=2, sticky="e", padx=(0, 4), pady=4)
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_theme_preset_selected)
        row += 1

        preset_btns = self._reg(tk.Frame(rows, bg=t["panel_bg"]), bg="panel_bg")
        preset_btns.grid(row=row, column=0, columnspan=3, sticky="we", padx=2, pady=(0, 2))
        tk.Button(preset_btns, text="Save As...", command=self._save_theme_preset).pack(side="left")
        self.rename_preset_btn = tk.Button(preset_btns, text="Rename...", command=self._rename_theme_preset)
        self.rename_preset_btn.pack(side="left", padx=(6, 0))
        self.remove_preset_btn = tk.Button(preset_btns, text="Remove", command=self._remove_theme_preset)
        self.remove_preset_btn.pack(side="left", padx=(6, 0))
        row += 1

        self.preset_desc_label = self._reg(tk.Label(
            rows, text="", bg=t["panel_bg"], fg=t["muted_fg"], font=("Segoe UI", 9), anchor="w", justify="left"),
            bg="panel_bg", fg="muted_fg")
        self.preset_desc_label.grid(row=row, column=0, columnspan=3, sticky="we", padx=(4, 8), pady=(0, 8))
        self.preset_desc_label.bind(
            "<Configure>", lambda e: self.preset_desc_label.configure(wraplength=max(60, e.width)))
        row += 1

        self._row_label(rows, row, "Syntax palette")
        self._refresh_syntax_choice_maps()
        current_syntax_label = syntax.COLOR_THEMES.get(
            self._orig_syntax_theme, syntax.COLOR_THEMES[syntax.DEFAULT_COLOR_THEME])["label"]
        self.syntax_theme_var = tk.StringVar(value=current_syntax_label)
        self.syntax_combo = ttk.Combobox(
            rows, textvariable=self.syntax_theme_var, state="readonly", width=16,
            values=[label for _key, label in self._syntax_theme_choices])
        self.syntax_combo.grid(row=row, column=1, columnspan=2, sticky="e", padx=(0, 4), pady=4)
        self.syntax_combo.bind("<<ComboboxSelected>>", self._on_syntax_theme_selected)
        row += 1

        syntax_preset_btns = self._reg(tk.Frame(rows, bg=t["panel_bg"]), bg="panel_bg")
        syntax_preset_btns.grid(row=row, column=0, columnspan=3, sticky="we", padx=2, pady=(0, 2))
        tk.Button(syntax_preset_btns, text="Save As...", command=self._save_syntax_palette_preset).pack(side="left")
        self.rename_syntax_preset_btn = tk.Button(
            syntax_preset_btns, text="Rename...", command=self._rename_syntax_palette_preset)
        self.rename_syntax_preset_btn.pack(side="left", padx=(6, 0))
        self.remove_syntax_preset_btn = tk.Button(
            syntax_preset_btns, text="Remove", command=self._remove_syntax_palette_preset)
        self.remove_syntax_preset_btn.pack(side="left", padx=(6, 0))
        tk.Button(syntax_preset_btns, text="Advanced...", command=self._open_advanced_syntax_colors)\
            .pack(side="right")
        row += 1

        self.syntax_theme_desc_label = self._reg(tk.Label(
            rows, text=self._syntax_theme_desc_text(self._orig_syntax_theme),
            bg=t["panel_bg"], fg=t["muted_fg"], font=("Segoe UI", 9), anchor="w", justify="left"),
            bg="panel_bg", fg="muted_fg")
        self.syntax_theme_desc_label.grid(row=row, column=0, columnspan=3, sticky="we", padx=(4, 8), pady=(0, 4))
        self.syntax_theme_desc_label.bind(
            "<Configure>", lambda e: self.syntax_theme_desc_label.configure(wraplength=max(60, e.width)))
        row += 1

        self._update_preset_buttons_state()
        self._update_preset_desc_label()
        self._update_syntax_preset_buttons_state()

        self.tiles_frame = self._reg(tk.Frame(rows, bg=t["panel_bg"]), bg="panel_bg")
        self.tiles_frame.grid(row=row, column=0, columnspan=3, sticky="w", padx=2, pady=(4, 8))
        row += 1
        self._refresh_preset_tiles()

        return row

    def _refresh_preset_tiles(self):
        """Redraws the row of clickable preset preview tiles: one per
        color theme preset (System/Midnight/Daybreak/anything saved),
        each a small Windows-customization-style swatch of that preset's
        own app colors plus a peek at the *currently selected* syntax
        palette on top of it (resolving Auto against that tile's own
        editor background, same as the live app would). Clicking a tile
        picks that color theme preset, same as picking it from the combo
        above it - rebuilt from scratch on every call since it's cheap
        and only runs on an explicit preset change, never on every
        keystroke/color-pick."""
        import syntax
        import theme

        if not hasattr(self, "tiles_frame"):
            return
        for child in self.tiles_frame.winfo_children():
            child.destroy()

        t = self.app.theme
        current_key = self._current_preset_key() if hasattr(self, "theme_preset_var") else theme.SYSTEM_PRESET
        syntax_key = self._current_syntax_key() if hasattr(self, "syntax_theme_var") else syntax.DEFAULT_COLOR_THEME

        for key, label in self._preset_key_to_label.items():
            colors = theme.resolve_preset_colors(key, self.cfg.get("theme_presets", {}))
            if syntax_key == syntax.AUTO_COLOR_THEME:
                resolved_syntax_key = syntax.resolve_auto_theme(colors["edit_bg"])
            elif syntax_key in syntax.COLOR_THEMES:
                resolved_syntax_key = syntax_key
            else:
                resolved_syntax_key = "idle"
            palette = syntax.COLOR_THEMES[resolved_syntax_key]["colors"]

            selected = key == current_key
            border = t["accent"] if selected else t["panel_bg"]
            cell = tk.Frame(self.tiles_frame, bg=t["panel_bg"], highlightthickness=2,
                             highlightbackground=border, highlightcolor=border, cursor="hand2")
            cell.pack(side="left", padx=4, pady=2)

            canvas = tk.Canvas(cell, width=88, height=56, highlightthickness=0, bg=colors["bg"], cursor="hand2")
            canvas.pack(padx=3, pady=(3, 0))
            canvas.create_rectangle(6, 6, 82, 50, fill=colors["panel_bg"], outline="")
            canvas.create_rectangle(12, 14, 76, 44, fill=colors["edit_bg"], outline="")
            kw = palette.get("keyword", {}).get("foreground") or colors["fg"]
            st = palette.get("string", {}).get("foreground") or colors["fg"]
            cm = palette.get("comment", {}).get("foreground") or colors["muted_fg"]
            canvas.create_line(16, 20, 42, 20, fill=kw, width=3)
            canvas.create_line(16, 28, 60, 28, fill=st, width=3)
            canvas.create_line(16, 36, 34, 36, fill=cm, width=3)
            canvas.create_rectangle(66, 34, 72, 40, fill=colors["accent"], outline="")

            name_label = tk.Label(cell, text=label, bg=t["panel_bg"], fg=t["fg"], font=("Segoe UI", 8), cursor="hand2")
            name_label.pack(pady=(2, 3))

            for widget in (cell, canvas, name_label):
                widget.bind("<Button-1>", lambda _e, k=key: self._select_preset_tile(k))

    def _select_preset_tile(self, key):
        self.theme_preset_var.set(self._preset_key_to_label.get(key, key))
        self._on_theme_preset_selected()

    def _toggle_advanced_colors(self):
        self._advanced_colors_visible = not self._advanced_colors_visible
        if self._advanced_colors_visible:
            self._advanced_colors_frame.grid()
            self.advanced_colors_btn.configure(text="\u25be Advanced color settings")
        else:
            self._advanced_colors_frame.grid_remove()
            self.advanced_colors_btn.configure(text="\u25b8 Advanced color settings")

    def _build_advanced_colors_section(self, rows, row, t):
        """The ten individual color settings, collapsed behind a toggle
        button by default: most people just want to pick a preset above,
        and the raw list of ten colors (even grouped/labeled, see
        THEME_FIELD_GROUPS) is exactly the kind of "technical" detail
        that overwhelms someone who isn't after it. Returns the next
        free grid row."""
        self.advanced_colors_btn = self._reg(tk.Button(
            rows, text="\u25b8 Advanced color settings", command=self._toggle_advanced_colors,
            relief="flat", anchor="w", bg=t["panel_bg"], fg=t["accent"],
            activebackground=t["panel_bg"], activeforeground=t["accent"],
            bd=0, highlightthickness=0, cursor="hand2"),
            bg="panel_bg", fg="accent", activebackground="panel_bg", activeforeground="accent")
        self.advanced_colors_btn.grid(row=row, column=0, columnspan=3, sticky="w", padx=2, pady=(6, 2))
        row += 1

        self._advanced_colors_frame = self._reg(tk.Frame(rows, bg=t["panel_bg"]), bg="panel_bg")
        self._advanced_colors_frame.grid(row=row, column=0, columnspan=3, sticky="we", padx=0, pady=0)
        self._advanced_colors_frame.columnconfigure(0, weight=1)
        row += 1

        inner_row = 0
        for group_name, fields in THEME_FIELD_GROUPS:
            self._section_header(self._advanced_colors_frame, inner_row, group_name, pady=(8, 2), columnspan=3)
            inner_row += 1
            for key, title, desc in fields:
                self._build_advanced_color_row(self._advanced_colors_frame, inner_row, t, key, title, desc)
                inner_row += 1

        # Collapsed by default - grid_remove() keeps the frame (and its
        # row/column slot) around for grid() to restore later, without
        # leaving a blank gap while it's hidden.
        self._advanced_colors_frame.grid_remove()
        return row

    def _build_advanced_color_row(self, rows, row, t, key, title, desc):
        label_frame = self._reg(tk.Frame(rows, bg=t["panel_bg"]), bg="panel_bg")
        label_frame.grid(row=row, column=0, sticky="we", padx=(4, 8), pady=4)
        title_lbl = self._reg(
            tk.Label(label_frame, text=title, bg=t["panel_bg"], fg=t["fg"], anchor="w", justify="left"),
            bg="panel_bg", fg="fg")
        title_lbl.pack(anchor="w", fill="x")
        desc_lbl = self._reg(
            tk.Label(label_frame, text=desc, bg=t["panel_bg"], fg=t["muted_fg"],
                      font=("Segoe UI", 8), anchor="w", justify="left"),
            bg="panel_bg", fg="muted_fg")
        desc_lbl.pack(anchor="w", fill="x")
        desc_lbl.bind("<Configure>", lambda e: desc_lbl.configure(wraplength=max(60, e.width)))

        swatch = tk.Label(rows, text="  " * 6, bg=self.theme_working[key], relief="flat", bd=1)
        swatch.grid(row=row, column=1, sticky="w", pady=4)
        tk.Button(rows, text="Choose", command=lambda k=key, s=swatch: self._pick_color(k, s))\
            .grid(row=row, column=2, padx=6, pady=4)
        self.color_vars[key] = swatch

    def _build_theme_tab(self, parent):
        from editor import MIN_FONT_SIZE, MAX_FONT_SIZE

        t = self.app.theme

        _canvas, rows = self._make_scrollable_rows(parent, t)
        rows.columnconfigure(0, weight=1)

        row = self._build_presets_section(rows, t)
        row = self._build_advanced_colors_section(rows, row, t)

        self._section_header(rows, row, "Editor Font", columnspan=3)
        row += 1

        self._row_label(rows, row, "Font family")
        families = sorted(set(tkfont.families()))
        self.font_family_var = tk.StringVar(value=self.theme_working.get("font_family", "Consolas"))
        font_combo = ttk.Combobox(rows, textvariable=self.font_family_var, values=families, width=22, state="normal")
        font_combo.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        font_combo.bind("<<ComboboxSelected>>", lambda e: self._preview({"theme"}))
        font_combo.bind("<FocusOut>", lambda e: self._preview({"theme"}))
        font_combo.bind("<Return>", lambda e: self._preview({"theme"}))
        row += 1

        self._row_label(rows, row, "Font size")
        self.font_size_var = tk.IntVar(value=int(self.theme_working.get("font_size", 11)))
        size_spin = tk.Spinbox(rows, from_=8, to=28, textvariable=self.font_size_var, width=5,
                                command=lambda: self._preview({"theme"}))
        size_spin.grid(row=row, column=1, sticky="w", pady=4)
        size_spin.bind("<FocusOut>", lambda e: self._preview({"theme"}))
        size_spin.bind("<Return>", lambda e: self._preview({"theme"}))
        row += 1

        self._section_header(rows, row, "Explorer", columnspan=3)
        row += 1
        self._row_label(rows, row, "Font family")
        self.explorer_font_family_var = tk.StringVar(value=self._orig_explorer_font_family)
        explorer_family_combo = ttk.Combobox(
            rows, textvariable=self.explorer_font_family_var, values=families, width=22, state="normal")
        explorer_family_combo.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        explorer_family_combo.bind("<<ComboboxSelected>>", self._on_explorer_font_family_changed)
        explorer_family_combo.bind("<FocusOut>", self._on_explorer_font_family_changed)
        explorer_family_combo.bind("<Return>", self._on_explorer_font_family_changed)
        row += 1
        self._row_label(rows, row, "Font size")
        self.explorer_font_size_var = tk.IntVar(value=self._orig_explorer_font_size)
        explorer_spin = tk.Spinbox(
            rows, from_=MIN_FONT_SIZE, to=MAX_FONT_SIZE, textvariable=self.explorer_font_size_var, width=5,
            command=self._on_explorer_font_size_changed)
        explorer_spin.grid(row=row, column=1, sticky="w", pady=4)
        explorer_spin.bind("<FocusOut>", lambda e: self._on_explorer_font_size_changed())
        explorer_spin.bind("<Return>", lambda e: self._on_explorer_font_size_changed())
        row += 1

        self._section_header(rows, row, "Terminal", columnspan=3)
        row += 1
        self._row_label(rows, row, "Font family")
        self.console_font_family_var = tk.StringVar(value=self._orig_console_font_family)
        console_family_combo = ttk.Combobox(
            rows, textvariable=self.console_font_family_var, values=families, width=22, state="normal")
        console_family_combo.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        console_family_combo.bind("<<ComboboxSelected>>", self._on_console_font_family_changed)
        console_family_combo.bind("<FocusOut>", self._on_console_font_family_changed)
        console_family_combo.bind("<Return>", self._on_console_font_family_changed)
        row += 1
        self._row_label(rows, row, "Font size")
        self.console_font_size_var = tk.IntVar(value=self._orig_console_font_size)
        console_spin = tk.Spinbox(
            rows, from_=MIN_FONT_SIZE, to=MAX_FONT_SIZE, textvariable=self.console_font_size_var, width=5,
            command=self._on_console_font_size_changed)
        console_spin.grid(row=row, column=1, sticky="w", pady=4)
        console_spin.bind("<FocusOut>", lambda e: self._on_console_font_size_changed())
        console_spin.bind("<Return>", lambda e: self._on_console_font_size_changed())

    def _pick_color(self, key, swatch):
        _rgb, hex_color = colorchooser.askcolor(color=self.theme_working.get(key), parent=self)
        if hex_color:
            self.theme_working[key] = hex_color
            swatch.configure(bg=hex_color)
            self._preview({"theme"})

    def _build_config_tab(self, parent):
        import os
        import sys
        import subprocess

        t = self.app.theme



        self._reg(tk.Label(parent, text="Config file location", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                            font=("Segoe UI", 9, "bold")), bg="panel_bg", fg="fg").pack(
            fill="x", padx=8, pady=(4, 2))
        path = config.get_config_path()
        path_row = self._reg(tk.Frame(parent, bg=t["panel_bg"]), bg="panel_bg")
        path_row.pack(fill="x", padx=8)
        path_entry = self._reg(
            tk.Entry(path_row, bg=t["edit_bg"], fg=t["fg"], relief="flat"),
            bg="edit_bg", fg="fg", readonlybackground="edit_bg")
        path_entry.insert(0, str(path))
        path_entry.configure(state="readonly", readonlybackground=t["edit_bg"])
        path_entry.pack(side="left", fill="x", expand=True, ipady=3)

        is_termux = "com.termux" in os.environ.get("PREFIX", "")

        def open_folder():
            import shutil

            folder = str(path.parent)
            try:
                if is_termux and shutil.which("termux-open"):
                    subprocess.Popen(["termux-open", folder])
                elif sys.platform.startswith("win"):
                    os.startfile(folder)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                elif shutil.which("xdg-open"):
                    subprocess.Popen(["xdg-open", folder])
                elif is_termux:
                    messagebox.showerror(
                        "Open Folder",
                        "termux-open isn't installed. Run:\n\npkg install termux-api",
                        parent=self,
                    )
                else:
                    messagebox.showerror("Open Folder", "No file opener found (xdg-open is missing).",
                                          parent=self)
            except OSError as e:
                messagebox.showerror("Open Folder", str(e), parent=self)

        self._reg(tk.Label(parent, text="Actions", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                            font=("Segoe UI", 9, "bold")), bg="panel_bg", fg="fg").pack(
            fill="x", padx=8, pady=(16, 2))
        btn_row = self._reg(tk.Frame(parent, bg=t["panel_bg"]), bg="panel_bg")
        btn_row.pack(fill="x", padx=8, pady=(0, 10))
        tk.Button(btn_row, text="Open Containing Folder", command=open_folder).pack(side="left")
        tk.Button(btn_row, text="Delete Config File", command=self._delete_config_file,
                  bg="#5a3030", fg="white", activebackground="#734040").pack(side="left", padx=8)

    def _delete_config_file(self):
        import os
        path = config.get_config_path()
        if not path.exists():
            messagebox.showinfo("Delete Config File", "There's no config file on disk yet.", parent=self)
            return
        if not messagebox.askyesno(
            "Delete Config File",
            f"Delete {path}?\n\nThis clears your saved name/address, shortcuts, theme, and general "
            "preferences. Defaults apply immediately in this session too.",
            parent=self,
        ):
            return
        try:
            os.remove(path)
        except OSError as e:
            messagebox.showerror("Delete Config File", str(e), parent=self)
            return
        self.cfg["connection"] = dict(config.DEFAULTS["connection"])
        self.cfg["shortcuts"] = dict(config.DEFAULTS["shortcuts"])
        self.cfg["output_shortcuts"] = dict(config.DEFAULTS["output_shortcuts"])
        self.cfg["theme"] = dict(config.DEFAULTS["theme"])
        self.cfg["ui"] = dict(config.DEFAULTS["ui"])
        self.cfg["theme_presets"] = dict(config.DEFAULTS["theme_presets"])
        self.cfg["editor"] = {
            "default_new_file_language": config.DEFAULTS["editor"]["default_new_file_language"],
            "run_commands": {},
            "syntax_theme": config.DEFAULTS["editor"]["syntax_theme"],
            "custom_syntax_colors": dict(config.DEFAULTS["editor"]["custom_syntax_colors"]),
            "terminal_messages": dict(config.DEFAULTS["editor"]["terminal_messages"]),
            "word_wrap": config.DEFAULTS["editor"]["word_wrap"],
        }
        import theme
        theme.apply_active_preset(self.cfg)  # resolves "system" against the OS, same as a fresh launch
        self.shortcuts_working = dict(self.cfg["shortcuts"])
        self.output_shortcuts_working = dict(self.cfg["output_shortcuts"])
        self.theme_working = dict(self.cfg["theme"])
        self._orig_shortcuts = dict(self.cfg["shortcuts"])
        self._orig_output_shortcuts = dict(self.cfg["output_shortcuts"])
        self._orig_theme = dict(self.cfg["theme"])
        self._orig_theme_preset = self.cfg["ui"]["theme_preset"]
        self._orig_theme_presets = {k: dict(v) for k, v in self.cfg["theme_presets"].items()}
        self._orig_default_language = self.cfg["editor"]["default_new_file_language"]
        self._orig_syntax_theme = self.cfg["editor"]["syntax_theme"]
        self._orig_custom_colors = dict(self.cfg["editor"]["custom_syntax_colors"])
        self._orig_terminal_messages = dict(self.cfg["editor"]["terminal_messages"])
        self._orig_word_wrap = self.cfg["editor"]["word_wrap"]
        self._orig_save_window_position = self.cfg["ui"]["save_window_position"]
        self._orig_save_window_size = self.cfg["ui"]["save_window_size"]
        self.save_window_position_var.set(self._orig_save_window_position)
        self.save_window_size_var.set(self._orig_save_window_size)
        base_font_size = int(self.cfg["theme"].get("font_size", 11))
        base_font_family = self.cfg["theme"].get("font_family", "Consolas")
        self.cfg["ui"]["explorer_font_size"] = 9
        self.cfg["ui"]["explorer_font_family"] = "sans-serif"
        self.cfg["ui"]["console_font_size"] = max(base_font_size - 1, 8)
        self.cfg["ui"]["console_font_family"] = base_font_family
        self._orig_explorer_font_size = self.cfg["ui"]["explorer_font_size"]
        self._orig_explorer_font_family = self.cfg["ui"]["explorer_font_family"]
        self._orig_console_font_size = self.cfg["ui"]["console_font_size"]
        self._orig_console_font_family = self.cfg["ui"]["console_font_family"]
        for action_id in list(self._action_scope):
            if action_id in self.shortcuts_working:
                self._action_scope[action_id] = (self.shortcuts_working, "shortcuts")
            elif action_id in self.output_shortcuts_working:
                self._action_scope[action_id] = (self.output_shortcuts_working, "output_shortcuts")
        import syntax
        syntax.set_custom_colors(self.cfg["editor"]["custom_syntax_colors"])
        syntax.set_color_theme(self.cfg["editor"]["syntax_theme"])
        self._refresh_shortcuts_ui()
        self._refresh_theme_ui()
        self._refresh_general_ui()
        if self.on_apply:
            self.on_apply({"shortcuts", "output_shortcuts", "theme", "editor", "ui"})

    def _refresh_shortcuts_ui(self):
        for action_id, (label, _btn) in self._row_widgets.items():
            working, _scope = self._action_scope[action_id]
            label.configure(text=self._accel_display(working.get(action_id, "")))

    def _refresh_theme_ui(self):
        import syntax
        import theme

        for key, swatch in self.color_vars.items():
            swatch.configure(bg=self.theme_working[key])
        self._suspend_preview = True
        self.font_family_var.set(self.theme_working["font_family"])
        self.font_size_var.set(self.theme_working["font_size"])
        ui_cfg = self.cfg.get("ui", {})
        self.explorer_font_family_var.set(ui_cfg.get("explorer_font_family", self._orig_explorer_font_family))
        self.explorer_font_size_var.set(ui_cfg.get("explorer_font_size", self._orig_explorer_font_size))
        self.console_font_family_var.set(ui_cfg.get("console_font_family", self._orig_console_font_family))
        self.console_font_size_var.set(ui_cfg.get("console_font_size", self._orig_console_font_size))
        if hasattr(self, "theme_preset_var"):
            self._refresh_preset_choice_maps()
            self._update_preset_combo_values()
            self.theme_preset_var.set(self._preset_key_to_label.get(
                ui_cfg.get("theme_preset", theme.SYSTEM_PRESET), theme.SYSTEM_PRESET_LABEL))
            self._update_preset_buttons_state()
            self._update_preset_desc_label()
        if hasattr(self, "syntax_theme_var"):
            self._refresh_syntax_choice_maps()
            self._update_syntax_preset_combo_values()
            editor_cfg = self.cfg.get("editor", {})
            theme_key = editor_cfg.get("syntax_theme", syntax.DEFAULT_COLOR_THEME)
            theme_spec = syntax.COLOR_THEMES.get(theme_key, syntax.COLOR_THEMES[syntax.DEFAULT_COLOR_THEME])
            self.syntax_theme_var.set(theme_spec["label"])
            self.syntax_theme_desc_label.configure(text=self._syntax_theme_desc_text(theme_key))
            self._update_syntax_preset_buttons_state()
        self._refresh_preset_tiles()
        self._suspend_preview = False

    def _refresh_general_ui(self):
        import syntax
        code = self.cfg.get("editor", {}).get("default_new_file_language", "python")
        self.default_language_var.set(syntax.LANGUAGE_LABELS.get(code, "Python"))
        term_cfg = self.cfg.get("editor", {}).get("terminal_messages", {})
        for key, var in self.terminal_message_vars.items():
            var.set(term_cfg.get(key, True))
        self.word_wrap_var.set(self.cfg.get("editor", {}).get("word_wrap", config.DEFAULTS["editor"]["word_wrap"]))

    def _reset_defaults(self):
        import syntax

        tab = self._current_tab()
        if tab == "Shortcuts":
            self.shortcuts_working = dict(config.DEFAULTS["shortcuts"])
            self.output_shortcuts_working = dict(config.DEFAULTS["output_shortcuts"])
            for action_id in list(self._action_scope):
                if action_id in self.shortcuts_working:
                    self._action_scope[action_id] = (self.shortcuts_working, "shortcuts")
                elif action_id in self.output_shortcuts_working:
                    self._action_scope[action_id] = (self.output_shortcuts_working, "output_shortcuts")
            self._refresh_shortcuts_ui()
            self._preview({"shortcuts", "output_shortcuts"})
        elif tab == "Theme":
            import theme

            self.theme_working = dict(config.DEFAULTS["theme"])
            self.theme_working.update(theme.resolve_preset_colors(theme.SYSTEM_PRESET))
            base_font_size = int(self.theme_working.get("font_size", 11))
            base_font_family = self.theme_working.get("font_family", "Consolas")
            ui_cfg = self.cfg.setdefault("ui", {})
            ui_cfg["theme_preset"] = "system"
            ui_cfg["explorer_font_size"] = 9
            ui_cfg["explorer_font_family"] = "sans-serif"
            ui_cfg["console_font_size"] = max(base_font_size - 1, 8)
            ui_cfg["console_font_family"] = base_font_family
            # The syntax palette lives on this tab now too (see the
            # combined Presets section), so resetting Theme resets it
            # right alongside the ten app colors it's shown next to -
            # this intentionally does NOT touch either's saved presets
            # (theme_presets / syntax_palette_presets), same as picking
            # System from the dropdown never deletes your other presets.
            editor_cfg = self.cfg.setdefault("editor", {})
            editor_cfg["syntax_theme"] = config.DEFAULTS["editor"]["syntax_theme"]
            editor_cfg["custom_syntax_colors"] = dict(config.DEFAULTS["editor"]["custom_syntax_colors"])
            syntax.set_custom_colors(editor_cfg["custom_syntax_colors"])
            syntax.set_color_theme(editor_cfg["syntax_theme"])
            self._refresh_theme_ui()
            self._preview({"theme", "ui", "editor"})
        elif tab == "General":
            editor_cfg = self.cfg.setdefault("editor", {})
            editor_cfg["default_new_file_language"] = config.DEFAULTS["editor"]["default_new_file_language"]
            editor_cfg["terminal_messages"] = dict(config.DEFAULTS["editor"]["terminal_messages"])
            editor_cfg["word_wrap"] = config.DEFAULTS["editor"]["word_wrap"]
            self._refresh_general_ui()
            self._preview({"editor"})

    def _save(self):
        self.theme_working["font_family"] = self.font_family_var.get() or "Consolas"
        try:
            self.theme_working["font_size"] = int(self.font_size_var.get())
        except (tk.TclError, ValueError):
            self.theme_working["font_size"] = self._orig_theme.get("font_size", 11)

        changed = set()
        if self.shortcuts_working != self._orig_shortcuts:
            changed.add("shortcuts")
        if self.output_shortcuts_working != self._orig_output_shortcuts:
            changed.add("output_shortcuts")
        if self.theme_working != self._orig_theme:
            changed.add("theme")
        if self.cfg.get("editor", {}).get("default_new_file_language") != self._orig_default_language:
            changed.add("editor")
        if self.cfg.get("editor", {}).get("syntax_theme") != self._orig_syntax_theme:
            changed.add("editor")
        if self.cfg.get("editor", {}).get("custom_syntax_colors", {}) != self._orig_custom_colors:
            changed.add("editor")
        if self.cfg.get("editor", {}).get("terminal_messages", {}) != self._orig_terminal_messages:
            changed.add("editor")
        if self.cfg.get("editor", {}).get("word_wrap", config.DEFAULTS["editor"]["word_wrap"]) != self._orig_word_wrap:
            changed.add("editor")
        if self.save_window_position_var.get() != self._orig_save_window_position:
            changed.add("ui")
        if self.save_window_size_var.get() != self._orig_save_window_size:
            changed.add("ui")
        if self.cfg.get("ui", {}).get("explorer_font_size", self._orig_explorer_font_size) \
                != self._orig_explorer_font_size:
            changed.add("ui")
        if self.cfg.get("ui", {}).get("explorer_font_family", self._orig_explorer_font_family) \
                != self._orig_explorer_font_family:
            changed.add("ui")
        if self.cfg.get("ui", {}).get("console_font_size", self._orig_console_font_size) \
                != self._orig_console_font_size:
            changed.add("ui")
        if self.cfg.get("ui", {}).get("console_font_family", self._orig_console_font_family) \
                != self._orig_console_font_family:
            changed.add("ui")
        if self.cfg.get("ui", {}).get("theme_preset", self._orig_theme_preset) != self._orig_theme_preset:
            changed.add("ui")
        if self.cfg.get("theme_presets", {}) != self._orig_theme_presets:
            changed.add("theme")
        if self.cfg.get("syntax_palette_presets", {}) != self._orig_syntax_palette_presets:
            changed.add("editor")

        self.cfg["shortcuts"] = dict(self.shortcuts_working)
        self.cfg["output_shortcuts"] = dict(self.output_shortcuts_working)
        self.cfg["theme"] = dict(self.theme_working)
        ui_cfg = self.cfg.setdefault("ui", {})
        ui_cfg["save_window_position"] = self.save_window_position_var.get()
        ui_cfg["save_window_size"] = self.save_window_size_var.get()
        config.save_config(self.cfg)

        self.destroy()
        if changed and self.on_apply:
            self.on_apply(changed)

    def _cancel(self):
        reverted = set()
        if self.cfg.get("shortcuts") != self._orig_shortcuts:
            self.cfg["shortcuts"] = dict(self._orig_shortcuts)
            reverted.add("shortcuts")
        if self.cfg.get("output_shortcuts") != self._orig_output_shortcuts:
            self.cfg["output_shortcuts"] = dict(self._orig_output_shortcuts)
            reverted.add("output_shortcuts")
        if self.cfg.get("theme") != self._orig_theme:
            self.cfg["theme"] = dict(self._orig_theme)
            reverted.add("theme")
        if self.cfg.get("editor", {}).get("default_new_file_language") != self._orig_default_language:
            self.cfg.setdefault("editor", {})["default_new_file_language"] = self._orig_default_language
            reverted.add("editor")
        if self.cfg.get("editor", {}).get("syntax_theme") != self._orig_syntax_theme:
            self.cfg.setdefault("editor", {})["syntax_theme"] = self._orig_syntax_theme
            reverted.add("editor")
        if self.cfg.get("editor", {}).get("custom_syntax_colors", {}) != self._orig_custom_colors:
            import syntax
            restored = {k: dict(v) for k, v in self._orig_custom_colors.items()}
            self.cfg.setdefault("editor", {})["custom_syntax_colors"] = restored
            syntax.set_custom_colors(restored)
            reverted.add("editor")
        if self.cfg.get("editor", {}).get("terminal_messages", {}) != self._orig_terminal_messages:
            self.cfg.setdefault("editor", {})["terminal_messages"] = dict(self._orig_terminal_messages)
            reverted.add("editor")
        if self.cfg.get("editor", {}).get("word_wrap", config.DEFAULTS["editor"]["word_wrap"]) != self._orig_word_wrap:
            self.cfg.setdefault("editor", {})["word_wrap"] = self._orig_word_wrap
            reverted.add("editor")
        ui_cfg = self.cfg.setdefault("ui", {})
        if (ui_cfg.get("save_window_position", True) != self._orig_save_window_position
                or ui_cfg.get("save_window_size", True) != self._orig_save_window_size):
            ui_cfg["save_window_position"] = self._orig_save_window_position
            ui_cfg["save_window_size"] = self._orig_save_window_size
            reverted.add("ui")
        if ui_cfg.get("explorer_font_size", self._orig_explorer_font_size) != self._orig_explorer_font_size:
            ui_cfg["explorer_font_size"] = self._orig_explorer_font_size
            reverted.add("ui")
        if ui_cfg.get("explorer_font_family", self._orig_explorer_font_family) != self._orig_explorer_font_family:
            ui_cfg["explorer_font_family"] = self._orig_explorer_font_family
            reverted.add("ui")
        if ui_cfg.get("console_font_size", self._orig_console_font_size) != self._orig_console_font_size:
            ui_cfg["console_font_size"] = self._orig_console_font_size
            reverted.add("ui")
        if ui_cfg.get("console_font_family", self._orig_console_font_family) != self._orig_console_font_family:
            ui_cfg["console_font_family"] = self._orig_console_font_family
            reverted.add("ui")
        if self.cfg.get("theme_presets", {}) != self._orig_theme_presets:
            self.cfg["theme_presets"] = {k: dict(v) for k, v in self._orig_theme_presets.items()}
            reverted.add("theme")
        if ui_cfg.get("theme_preset", self._orig_theme_preset) != self._orig_theme_preset:
            ui_cfg["theme_preset"] = self._orig_theme_preset
            reverted.add("ui")
        if self.cfg.get("syntax_palette_presets", {}) != self._orig_syntax_palette_presets:
            import syntax
            restored_presets = {k: {tag: dict(c) for tag, c in v.items()}
                                 for k, v in self._orig_syntax_palette_presets.items()}
            self.cfg["syntax_palette_presets"] = restored_presets
            syntax.sync_saved_palettes(restored_presets)
            reverted.add("editor")
        self.destroy()
        if reverted and self.on_apply:
            try:
                self.on_apply(reverted)
            except tk.TclError:
                pass

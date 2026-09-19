import tkinter as tk
from tkinter import ttk, colorchooser, messagebox, font as tkfont

import config
import scrollutil

IGNORED_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Caps_Lock", "Num_Lock", "Super_L", "Super_R", "Meta_L", "Meta_R",
}

THEME_FIELDS = [
    ("bg", "App background"),
    ("panel_bg", "Panel background"),
    ("edit_bg", "Editor background"),
    ("fg", "Editor text"),
    ("gutter_bg", "Line number gutter"),
    ("gutter_fg", "Line number text"),
    ("sel_bg", "Selection highlight"),
    ("console_bg", "Terminal console background"),
    ("muted_fg", "Muted / secondary text"),
    ("accent", "Accent"),
]


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
        self._orig_syntax_theme = cfg.setdefault("editor", {}).get("syntax_theme", "idle")
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
        self.color_vars = {}
        self._listening_action = None
        self._row_widgets = {}
        self._action_scope = {}
        self._suspend_preview = False

        footer = tk.Frame(self, bg=t["panel_bg"])
        footer.pack(side="bottom", fill="x", padx=10, pady=10)
        tk.Button(footer, text="Save", command=self._save).pack(side="right", padx=4)
        tk.Button(footer, text="Cancel", command=self._cancel).pack(side="right")
        self.reset_btn = tk.Button(footer, text="Reset to Defaults", command=self._reset_defaults)
        self.reset_btn.pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.nb = nb

        general_tab = tk.Frame(nb, bg=t["panel_bg"])
        shortcuts_tab = tk.Frame(nb, bg=t["panel_bg"])
        theme_tab = tk.Frame(nb, bg=t["panel_bg"])
        config_tab = tk.Frame(nb, bg=t["panel_bg"])
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
        canvas = tk.Canvas(parent, bg=t["panel_bg"], highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        rows = tk.Frame(canvas, bg=t["panel_bg"])
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
        lbl = tk.Label(rows, text=text, bg=self.app.theme["panel_bg"], fg=self.app.theme["fg"],
                        anchor="w", justify="left")
        grid_kwargs.setdefault("sticky", "we")
        grid_kwargs.setdefault("padx", (4, 8))
        grid_kwargs.setdefault("pady", 4)
        lbl.grid(row=row, column=0, **grid_kwargs)
        lbl.bind("<Configure>", lambda e, l=lbl: l.configure(wraplength=max(60, e.width)))
        return lbl

    def _build_general_tab(self, parent):
        import syntax

        t = self.app.theme
        _canvas, rows = self._make_scrollable_rows(parent, t)
        rows.columnconfigure(0, weight=1)

        def section_header(row, text, pady=(16, 2)):
            tk.Label(rows, text=text, bg=t["panel_bg"], fg=t["fg"], anchor="w",
                     font=("Segoe UI", 9, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="we", padx=4, pady=pady)

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

        self._row_label(rows, row, "Syntax color palette")

        self._syntax_theme_choices = [(key, spec["label"]) for key, spec in syntax.COLOR_THEMES.items()]
        self._label_to_syntax_theme = {label: key for key, label in self._syntax_theme_choices}
        current_syntax_label = syntax.COLOR_THEMES.get(
            self._orig_syntax_theme, syntax.COLOR_THEMES[syntax.DEFAULT_COLOR_THEME])["label"]

        self.syntax_theme_var = tk.StringVar(value=current_syntax_label)
        syntax_combo = ttk.Combobox(rows, textvariable=self.syntax_theme_var, state="readonly", width=16,
                                     values=[label for _key, label in self._syntax_theme_choices])
        syntax_combo.grid(row=row, column=1, sticky="e", padx=(0, 4), pady=4)
        syntax_combo.bind("<<ComboboxSelected>>", self._on_syntax_theme_selected)
        row += 1

        self.syntax_theme_desc_label = tk.Label(
            rows, text=syntax.COLOR_THEMES[self._orig_syntax_theme]["description"],
            bg=t["panel_bg"], fg=t["muted_fg"], font=("Segoe UI", 9), anchor="w", justify="left")
        self.syntax_theme_desc_label.grid(row=row, column=0, sticky="we", padx=(4, 8), pady=(0, 4))
        self.syntax_theme_desc_label.bind(
            "<Configure>", lambda e: self.syntax_theme_desc_label.configure(wraplength=max(60, e.width)))

        tk.Button(rows, text="Advanced", command=self._open_advanced_syntax_colors).grid(
            row=row, column=1, sticky="ne", padx=(0, 4), pady=(0, 4))
        row += 1

        self.word_wrap_var = tk.BooleanVar(value=self._orig_word_wrap)
        tk.Checkbutton(
            rows, text="Wrap long lines onto more rows instead of scrolling sideways",
            variable=self.word_wrap_var, bg=t["panel_bg"], fg=t["fg"],
            selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
            highlightthickness=0, anchor="w", command=self._on_word_wrap_toggled,
        ).grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=(0, 4))
        row += 1

        ui_cfg = self.cfg.setdefault("ui", {})
        self._orig_save_window_position = ui_cfg.get("save_window_position", True)
        self._orig_save_window_size = ui_cfg.get("save_window_size", True)
        self.save_window_position_var = tk.BooleanVar(value=self._orig_save_window_position)
        self.save_window_size_var = tk.BooleanVar(value=self._orig_save_window_size)

        section_header(row, "Window")
        row += 1
        tk.Checkbutton(
            rows, text="Remember window position between launches",
            variable=self.save_window_position_var, bg=t["panel_bg"], fg=t["fg"],
            selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
            highlightthickness=0, anchor="w", command=lambda: self._preview({"ui"}),
        ).grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=2)
        row += 1
        tk.Checkbutton(
            rows, text="Remember window size between launches",
            variable=self.save_window_size_var, bg=t["panel_bg"], fg=t["fg"],
            selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
            highlightthickness=0, anchor="w", command=lambda: self._preview({"ui"}),
        ).grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=(2, 4))
        row += 1
        window_desc_label = tk.Label(
            rows, text="Unchecking one deletes it from the config file on close, so next time "
                       "the window opens with that part placed/sized automatically.",
            bg=t["panel_bg"], fg=t["muted_fg"], anchor="w", justify="left")
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
            tk.Checkbutton(
                rows, text=terminal_message_labels[key],
                variable=self.terminal_message_vars[key], bg=t["panel_bg"], fg=t["fg"],
                selectcolor=t["edit_bg"], activebackground=t["panel_bg"], activeforeground=t["fg"],
                highlightthickness=0, anchor="w",
                command=lambda k=key: self._on_terminal_message_toggled(k),
            ).grid(row=row, column=0, columnspan=2, sticky="we", padx=2, pady=2)
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
        self.syntax_theme_desc_label.configure(text=syntax.COLOR_THEMES[key]["description"])
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
            self.syntax_theme_desc_label.configure(text=syntax.COLOR_THEMES["custom"]["description"])
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

        tk.Label(rows, text="Editor", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(4, 4))
        row = self._add_shortcut_rows(rows, 1, SHORTCUT_SPECS, self.shortcuts_working, "shortcuts")

        tk.Label(rows, text="Terminal", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI", 9, "bold")).grid(row=row, column=0, columnspan=3, sticky="w", pady=(14, 4))
        row += 1
        tk.Label(
            rows, text="Shortcuts that only apply while the Terminal panel has focus.",
            bg=t["panel_bg"], fg=t["muted_fg"], anchor="w", justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="we", pady=(0, 6))
        row += 1
        self._add_shortcut_rows(rows, row, OUTPUT_SHORTCUT_SPECS, self.output_shortcuts_working, "output_shortcuts")

    def _add_shortcut_rows(self, rows, start_row, specs, working, scope):
        row = start_row
        for action_id, label, _default_accel, _method in specs:
            self._action_scope[action_id] = (working, scope)
            self._row_label(rows, row, label)
            accel_label = tk.Label(rows, text=self._accel_display(working.get(action_id, "")),
                                    bg=self.app.theme["edit_bg"], fg=self.app.theme["fg"], width=16,
                                    anchor="w", padx=6)
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
        tk.Label(rows, text=text, bg=t["panel_bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=columnspan, sticky="we", padx=4, pady=pady)

    def _build_theme_tab(self, parent):
        from editor import MIN_FONT_SIZE, MAX_FONT_SIZE

        t = self.app.theme

        _canvas, rows = self._make_scrollable_rows(parent, t)
        rows.columnconfigure(0, weight=1)

        self._section_header(rows, 0, "Colors", pady=(4, 4), columnspan=3)

        for offset, (key, label) in enumerate(THEME_FIELDS):
            row = offset + 1
            self._row_label(rows, row, label)
            swatch = tk.Label(rows, text="  " * 6, bg=self.theme_working[key], relief="flat", bd=1)
            swatch.grid(row=row, column=1, sticky="w", pady=4)
            tk.Button(rows, text="Choose", command=lambda k=key, s=swatch: self._pick_color(k, s))\
                .grid(row=row, column=2, padx=6, pady=4)
            self.color_vars[key] = swatch

        row = len(THEME_FIELDS) + 1
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



        tk.Label(parent, text="Config file location", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(fill="x", padx=8, pady=(4, 2))
        path = config.get_config_path()
        path_row = tk.Frame(parent, bg=t["panel_bg"])
        path_row.pack(fill="x", padx=8)
        path_entry = tk.Entry(path_row, bg=t["edit_bg"], fg=t["fg"], relief="flat")
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

        tk.Label(parent, text="Actions", bg=t["panel_bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(fill="x", padx=8, pady=(16, 2))
        btn_row = tk.Frame(parent, bg=t["panel_bg"])
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
        self.cfg["editor"] = {
            "default_new_file_language": config.DEFAULTS["editor"]["default_new_file_language"],
            "run_commands": {},
            "syntax_theme": config.DEFAULTS["editor"]["syntax_theme"],
            "custom_syntax_colors": dict(config.DEFAULTS["editor"]["custom_syntax_colors"]),
            "terminal_messages": dict(config.DEFAULTS["editor"]["terminal_messages"]),
            "word_wrap": config.DEFAULTS["editor"]["word_wrap"],
        }
        self.shortcuts_working = dict(self.cfg["shortcuts"])
        self.output_shortcuts_working = dict(self.cfg["output_shortcuts"])
        self.theme_working = dict(self.cfg["theme"])
        self._orig_shortcuts = dict(self.cfg["shortcuts"])
        self._orig_output_shortcuts = dict(self.cfg["output_shortcuts"])
        self._orig_theme = dict(self.cfg["theme"])
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
        self._suspend_preview = False

    def _refresh_general_ui(self):
        import syntax
        code = self.cfg.get("editor", {}).get("default_new_file_language", "python")
        self.default_language_var.set(syntax.LANGUAGE_LABELS.get(code, "Python"))
        theme_key = self.cfg.get("editor", {}).get("syntax_theme", syntax.DEFAULT_COLOR_THEME)
        theme_spec = syntax.COLOR_THEMES.get(theme_key, syntax.COLOR_THEMES[syntax.DEFAULT_COLOR_THEME])
        self.syntax_theme_var.set(theme_spec["label"])
        self.syntax_theme_desc_label.configure(text=theme_spec["description"])
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
            self.theme_working = dict(config.DEFAULTS["theme"])
            base_font_size = int(self.theme_working.get("font_size", 11))
            base_font_family = self.theme_working.get("font_family", "Consolas")
            ui_cfg = self.cfg.setdefault("ui", {})
            ui_cfg["explorer_font_size"] = 9
            ui_cfg["explorer_font_family"] = "sans-serif"
            ui_cfg["console_font_size"] = max(base_font_size - 1, 8)
            ui_cfg["console_font_family"] = base_font_family
            self._refresh_theme_ui()
            self._preview({"theme", "ui"})
        elif tab == "General":
            editor_cfg = self.cfg.setdefault("editor", {})
            editor_cfg["default_new_file_language"] = config.DEFAULTS["editor"]["default_new_file_language"]
            editor_cfg["syntax_theme"] = config.DEFAULTS["editor"]["syntax_theme"]
            editor_cfg["custom_syntax_colors"] = dict(config.DEFAULTS["editor"]["custom_syntax_colors"])
            editor_cfg["terminal_messages"] = dict(config.DEFAULTS["editor"]["terminal_messages"])
            editor_cfg["word_wrap"] = config.DEFAULTS["editor"]["word_wrap"]
            syntax.set_custom_colors(editor_cfg["custom_syntax_colors"])
            syntax.set_color_theme(editor_cfg["syntax_theme"])
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
        self.destroy()
        if reverted and self.on_apply:
            try:
                self.on_apply(reverted)
            except tk.TclError:
                pass

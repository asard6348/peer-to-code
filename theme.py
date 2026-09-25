"""One place that decides what every window in the app looks like.

The connect screen and the editor used to each build their own ttk.Style
from scratch, with two separate, slowly diverging copies of the same
button and label rules. Worse, neither one ever gave entry fields a dark
background, so any text colored to match the rest of the theme rendered
almost unreadable sitting on the ttk theme's default light field. Both
problems came from the same root cause: styling decided in more than one
place. This module is the single place now. Every window calls
apply_base_style() once, early, and layers its own screen-specific styles
on top if it needs any.
"""

import subprocess
import sys
import tkinter as tk
from tkinter import ttk

# The colors a preset (built-in or user-saved) controls. Font family/
# size live in the same "theme" config dict but are deliberately outside
# a preset's reach - switching Dark/Light/System shouldn't also reset
# whatever font the person picked.
THEME_COLOR_KEYS = (
    "bg", "panel_bg", "edit_bg", "gutter_bg", "gutter_fg",
    "fg", "muted_fg", "sel_bg", "console_bg", "accent",
    "status_bg", "status_fg", "border",
)

SYSTEM_PRESET = "system"
# Display label for the "system" preset. Kept as a constant (rather than
# hardcoded wherever the preset picker is built) so it's defined in one
# place alongside the two built-ins' own "label" fields below.
SYSTEM_PRESET_LABEL = "Match System"
# Names a saved custom preset can never use, since they're either the
# special auto-following preset or one of the two fixed built-ins.
RESERVED_PRESET_NAMES = frozenset({"system", "dark", "light"})

BUILTIN_THEME_PRESETS = {
    "dark": {
        "label": "Midnight",
        "description": "A fixed dark palette. Pairs naturally with the Midnight syntax palette.",
        # This app's original, long-standing default look.
        "colors": {
            "bg": "#1e1f22",
            "panel_bg": "#2b2d30",
            "edit_bg": "#26282b",
            "gutter_bg": "#26282b",
            "gutter_fg": "#5c6370",
            "fg": "#dcdfe4",
            "muted_fg": "#8a8f98",
            "sel_bg": "#3a4048",
            "console_bg": "#18191b",
            "accent": "#4a9eff",
            # A dedicated "floor" tone, deliberately darker than every
            # other surface (bg/panel_bg/edit_bg/console_bg) rather than
            # a value plucked from a different, unrelated gray family -
            # that's what the old hardcoded status bar (#3a3d41, a
            # medium-value blue-gray with no relation to this palette)
            # got wrong: it sat awkwardly close in value to panel_bg
            # without matching its hue, clashing rather than either
            # blending in or reading as a deliberate accent. fg is
            # softened off pure white for the same reason - full-white
            # text over near-black chrome elsewhere in this preset was
            # the highest-contrast pairing on the whole screen, standing
            # out for no reason tied to meaning.
            "status_bg": "#131416",
            "status_fg": "#c9ccd1",
            # Outlines: scrollbar thumbs, button/field/tab/panel edges,
            # the line under the tab strip, and the divider between a
            # dropdown's field and its arrow. Previously these were never
            # themed at all - "clam" (the ttk theme this app builds on)
            # falls back to its own built-in mid-gray borders whenever a
            # style leaves bordercolor unset, and plain tk widgets like
            # the Settings window's Save/Cancel buttons fall back to the
            # platform's default button face for the same reason. Both
            # read as a stray light-gray smear across a dark preset like
            # this one. One step lighter than panel_bg - visible enough
            # to read as a deliberate line, not so bright it competes
            # with real content.
            "border": "#3d4046",
        },
    },
    "light": {
        "label": "Daybreak",
        "description": "A fixed light palette. Pairs naturally with the Daybreak syntax palette.",
        "colors": {
            "bg": "#fafafa",
            "panel_bg": "#eeeeee",
            "edit_bg": "#ffffff",
            "gutter_bg": "#f5f5f5",
            "gutter_fg": "#6e7781",
            "fg": "#24292e",
            "muted_fg": "#57606a",
            "sel_bg": "#add6ff",
            "console_bg": "#f6f8fa",
            "accent": "#0969da",
            # A deliberately dark accent stripe against this light
            # palette - a common, legible pattern (VS Code's light theme
            # does the same) rather than a mismatch, so it's kept as-is.
            "status_bg": "#3a3d41",
            "status_fg": "#f5f5f5",
            # See the "dark" preset's "border" comment for what this
            # covers. A soft, slightly cool gray in the same family GitHub's
            # own light theme uses for hairlines - reads as a clean, quiet
            # division against this palette's white/off-white surfaces
            # without the harsh contrast "clam"'s own default border gray
            # has against edit_bg's near-white.
            "border": "#d0d7de",
        },
    },
}


def detect_system_dark_mode() -> bool:
    """Best-effort read of the OS's current light/dark preference, used to
    resolve the "System" theme preset. Falls back to dark - this app's
    original default look - on any platform/desktop this can't read from,
    rather than raising: a theme preset must never be allowed to crash
    startup."""
    try:
        if sys.platform.startswith("win"):
            return _detect_windows_dark_mode()
        if sys.platform == "darwin":
            return _detect_macos_dark_mode()
        if sys.platform.startswith("linux"):
            return _detect_linux_dark_mode()
    except (OSError, ImportError, subprocess.SubprocessError, ValueError):
        pass
    return True


def _detect_windows_dark_mode() -> bool:
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
    try:
        value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
    finally:
        winreg.CloseKey(key)
    return value == 0


def _detect_macos_dark_mode() -> bool:
    # `defaults read` exits non-zero (with nothing on stdout) when the key
    # is absent, which is exactly what happens in Light mode - macOS only
    # writes AppleInterfaceStyle while Dark mode is active.
    result = subprocess.run(
        ["defaults", "read", "-g", "AppleInterfaceStyle"],
        capture_output=True, text=True, timeout=2)
    return result.returncode == 0 and "dark" in result.stdout.strip().lower()


def _detect_linux_dark_mode() -> bool:
    # The freedesktop color-scheme key is the modern, desktop-agnostic
    # signal (supported by GNOME, KDE via a shim, etc.); fall back to
    # sniffing the GTK theme name for desktops that only expose that.
    result = subprocess.run(
        ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
        capture_output=True, text=True, timeout=2)
    if result.returncode == 0:
        value = result.stdout.strip().strip("'\"").lower()
        if "dark" in value:
            return True
        if "light" in value or "default" in value:
            return False
    result = subprocess.run(
        ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
        capture_output=True, text=True, timeout=2)
    if result.returncode == 0:
        return "dark" in result.stdout.strip().lower()
    return True


def resolve_preset_colors(name, custom_presets=None):
    """The THEME_COLOR_KEYS-keyed color dict for preset `name`: "system"
    resolves to "dark" or "light" depending on the OS's current setting,
    "dark"/"light" return their own fixed colors, and any other name is
    looked up in `custom_presets` (the user's saved presets) - falling
    back to "dark" if that name doesn't exist (for example it was removed
    since being selected)."""
    if name == SYSTEM_PRESET:
        name = "dark" if detect_system_dark_mode() else "light"
    if name in BUILTIN_THEME_PRESETS:
        return dict(BUILTIN_THEME_PRESETS[name]["colors"])
    if custom_presets and name in custom_presets:
        return dict(custom_presets[name])
    return dict(BUILTIN_THEME_PRESETS["dark"]["colors"])


def apply_active_preset(cfg):
    """Refreshes cfg["theme"]'s ten palette keys from whichever preset
    cfg["ui"]["theme_preset"] names - called once, early, right after the
    config loads (see app.py), before anything reads cfg["theme"].

    Only "system" (the default) actually re-resolves anything here: it
    re-reads the OS's current light/dark setting on every launch, so
    switching your system theme gets picked up the next time the app
    starts even if the saved config hasn't changed. A saved "dark"/
    "light"/custom preset name is left alone - those are one-time "load
    this preset's colors" actions (see SettingsWindow's preset picker),
    and cfg["theme"] already holds their colors from whenever they were
    selected. Font family/size and any other keys are never touched."""
    ui_cfg = cfg.setdefault("ui", {})
    if ui_cfg.get("theme_preset", SYSTEM_PRESET) != SYSTEM_PRESET:
        return
    theme_cfg = cfg.setdefault("theme", {})
    theme_cfg.update(resolve_preset_colors(SYSTEM_PRESET))


def lighten(hex_color: str, amount: float) -> str:
    """Blends hex_color toward white by `amount` (0-1). Used to derive a
    button's hover/focus shade from its base color, the same way the
    Host button's brighter green is derived from its base green, instead
    of reusing an unrelated color (like a muted selection gray) that can
    read as the button going dark or "black" instead of brightening."""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    r = round(r + (255 - r) * amount)
    g = round(g + (255 - g) * amount)
    b = round(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def darken(hex_color: str, amount: float) -> str:
    """Blends hex_color toward black by `amount` (0-1). Used for a
    button's pressed shade."""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    r = round(r * (1 - amount))
    g = round(g * (1 - amount))
    b = round(b * (1 - amount))
    return f"#{r:02x}{g:02x}{b:02x}"


def apply_base_style(style: ttk.Style, t: dict):
    """Applies the foundational look shared by the whole app: window
    chrome, buttons, entry fields, and the file tree. Safe to call more
    than once (for example once at startup and again if the theme
    changes), since ttk.Style just overwrites whatever was there before."""
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # "clam" draws every widget's outer edge - a button's border, an
    # entry's frame, the line under a tab, a scrollbar's trough - with
    # its own built-in mid-gray whenever a style leaves that color
    # unset. bordercolor/lightcolor/darkcolor below replace that with
    # t["border"] everywhere "clam" would otherwise use it, so outlines
    # come from the active preset instead of a fixed gray that clashes
    # with a dark preset and barely registers on a light one.
    border = t["border"]

    style.configure("TFrame", background=t["bg"])
    style.configure("Panel.TFrame", background=t["panel_bg"])
    style.configure("TLabel", background=t["bg"], foreground=t["fg"], font=("Segoe UI", 10))
    style.configure("Panel.TLabel", background=t["panel_bg"], foreground=t["fg"], font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=t["panel_bg"], foreground=t["muted_fg"], font=("Segoe UI", 9))

    style.configure("TButton", font=("Segoe UI", 10), padding=6,
                     background=t["panel_bg"], foreground=t["fg"], focuscolor=t["panel_bg"],
                     bordercolor=border, lightcolor=t["panel_bg"], darkcolor=t["panel_bg"])
    style.map(
        "TButton",
        background=[("pressed", t["accent"]), ("active", t["sel_bg"]), ("focus", t["sel_bg"])],
        foreground=[("disabled", t["muted_fg"])],
    )

    style.configure("Toolbar.TButton", font=("Segoe UI", 9), padding=(4, 2),
                     background=t["panel_bg"], foreground=t["fg"], focuscolor=t["panel_bg"],
                     bordercolor=border, lightcolor=t["panel_bg"], darkcolor=t["panel_bg"])
    style.map(
        "Toolbar.TButton",
        background=[("pressed", t["accent"]), ("active", t["sel_bg"]), ("focus", t["sel_bg"])],
        foreground=[("disabled", t["muted_fg"])],
    )

    style.configure("Toolbar.Thin.TButton", font=("Segoe UI", 9), padding=(1, 2),
                     background=t["panel_bg"], foreground=t["fg"], focuscolor=t["panel_bg"],
                     bordercolor=border, lightcolor=t["panel_bg"], darkcolor=t["panel_bg"])
    style.map(
        "Toolbar.Thin.TButton",
        background=[("pressed", t["accent"]), ("active", t["sel_bg"]), ("focus", t["sel_bg"])],
        foreground=[("disabled", t["muted_fg"])],
    )

    style.configure("TEntry", padding=5, fieldbackground=t["edit_bg"], foreground=t["fg"],
                     insertcolor=t["fg"], selectbackground=t["sel_bg"], selectforeground=t["fg"],
                     bordercolor=border, lightcolor=t["edit_bg"], darkcolor=t["edit_bg"])
    style.map(
        "TEntry",
        fieldbackground=[("disabled", t["panel_bg"]), ("readonly", t["panel_bg"]), ("focus", t["edit_bg"])],
        foreground=[("disabled", t["muted_fg"])],
        bordercolor=[("focus", t["accent"])],
    )

    style.configure("Treeview", background=t["edit_bg"], foreground=t["fg"],
                     fieldbackground=t["edit_bg"], borderwidth=0)
    style.map("Treeview", background=[("selected", t["sel_bg"])], foreground=[("selected", t["fg"])])
    style.configure("Treeview.Heading", background=t["panel_bg"], foreground=t["fg"],
                     bordercolor=border, lightcolor=t["panel_bg"], darkcolor=t["panel_bg"])

    # Settings' tab strip: unthemed, these otherwise keep "clam"'s fixed
    # beige regardless of which palette is active, which reads as a stray
    # mismatched patch against a light (or, for that matter, dark) panel.
    style.configure("TNotebook", background=t["panel_bg"], borderwidth=0, bordercolor=border)
    style.configure("TNotebook.Tab", background=t["bg"], foreground=t["muted_fg"], padding=(10, 4),
                     bordercolor=border, lightcolor=t["bg"], darkcolor=t["bg"])
    style.map(
        "TNotebook.Tab",
        background=[("selected", t["panel_bg"])],
        foreground=[("selected", t["fg"])],
        lightcolor=[("selected", t["panel_bg"])],
        darkcolor=[("selected", t["panel_bg"])],
    )

    # Dropdowns (syntax palette, font family, the color theme preset
    # picker): same story - "clam"'s default fieldbackground is a fixed
    # mid-gray that was never wired up to the theme. bordercolor here
    # also covers the vertical divider "clam" draws between the text
    # field and the arrow button.
    style.configure("TCombobox", fieldbackground=t["edit_bg"], background=t["panel_bg"],
                     foreground=t["fg"], arrowcolor=t["fg"], selectbackground=t["edit_bg"],
                     selectforeground=t["fg"], bordercolor=border,
                     lightcolor=t["edit_bg"], darkcolor=t["edit_bg"])
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", t["edit_bg"]), ("disabled", t["panel_bg"])],
        background=[("readonly", t["panel_bg"])],
        foreground=[("disabled", t["muted_fg"])],
        bordercolor=[("focus", t["accent"])],
    )

    # Scrollbars: "clam"'s built-in trough/thumb grays were never touched
    # by this app at all before, so every scrollbar - editor, explorer,
    # terminal, the connect screen's page, Settings' scrollable tabs -
    # showed the same fixed light-gray strip regardless of preset. The
    # thumb brightens to the accent color on hover/drag, both for
    # feedback and so it stays readable against either preset's trough.
    for orient in ("Vertical", "Horizontal"):
        style_name = f"{orient}.TScrollbar"
        style.configure(style_name, background=border, troughcolor=t["panel_bg"],
                         bordercolor=t["panel_bg"], lightcolor=t["panel_bg"], darkcolor=t["panel_bg"],
                         arrowcolor=t["muted_fg"], relief="flat", arrowsize=13)
        style.map(
            style_name,
            background=[("pressed", t["accent"]), ("active", t["accent"])],
            arrowcolor=[("pressed", t["fg"]), ("active", t["fg"])],
        )


def apply_classic_widget_defaults(root: tk.Tk, t: dict):
    """Sets sane, theme-matching defaults on the Tk option database for the
    plain (non-ttk) widgets scattered through the app: tk.Entry, tk.Text,
    tk.Label, and so on. These read the option database directly, so one
    call here covers every one of them, present and future, without having
    to remember to pass the same five colors into every constructor.

    This also closes off a specific failure mode some Tk builds have:
    highlightThickness defaults to a few pixels with highlightColor
    defaulting to black, so a plain widget can show a stray black ring the
    moment it takes keyboard focus, on themes and platforms where nothing
    else overrides it. Zeroing it out here means that ring never has a
    chance to appear in the first place, whatever the platform."""
    root.option_add("*highlightThickness", 0)
    root.option_add("*highlightBackground", t["bg"])
    root.option_add("*highlightColor", t["sel_bg"])
    root.option_add("*insertBackground", t["fg"])
    root.option_add("*selectBackground", t["sel_bg"])
    root.option_add("*selectForeground", t["fg"])
    root.option_add("*Entry.background", t["edit_bg"])
    root.option_add("*Entry.foreground", t["fg"])
    root.option_add("*Text.background", t["edit_bg"])
    root.option_add("*Text.foreground", t["fg"])
    # Plain tk.Button (Settings' Save/Cancel/Reset/Advanced/Close/Choose/
    # Change buttons, the connect screen's File/Folder picker, the
    # editor's Find/Replace dialog, and any other one-off dialog built
    # with a bare tk.Button rather than a ttk.Button) previously never
    # had a background set at all, so it fell back to the platform's
    # stock button face - light gray on Linux/X11 - no matter which
    # preset was active. Tk derives a plain button's raised-bevel border
    # from this same background color, so setting it also fixes the
    # button outline; no separate border color is needed here the way
    # the ttk styles above need bordercolor.
    root.option_add("*Button.background", t["panel_bg"])
    root.option_add("*Button.foreground", t["fg"])
    root.option_add("*Button.activeBackground", t["sel_bg"])
    root.option_add("*Button.activeForeground", t["fg"])
    root.option_add("*Button.disabledForeground", t["muted_fg"])
    root.option_add("*Button.highlightBackground", t["panel_bg"])
    # tk.Menu (the File/Edit/Run/View menu bar and its dropdowns) is a
    # plain Tk widget too, and previously had no theme-derived colors at
    # all - it just fell back to Tk's own compiled-in default, an
    # unrelated mid-gray that happened to read as reasonable next to a
    # light theme and as a jarring, disconnected patch next to a dark
    # one. This option-database entry covers any menu created fresh from
    # here on; editor.py's EditorApp additionally repaints its own
    # already-built menus directly on every live switch (see
    # EditorApp._theme_menus) since, like Entry/Text, they don't reread
    # the option database on their own once built. Windows and macOS draw
    # the actual menu bar with native OS chrome and mostly ignore this -
    # it's primarily a Linux/X11 fix, where Tk draws menus itself.
    root.option_add("*Menu.background", t["bg"])
    root.option_add("*Menu.foreground", t["fg"])
    root.option_add("*Menu.activeBackground", t["sel_bg"])
    root.option_add("*Menu.activeForeground", t["fg"])
    root.option_add("*Menu.disabledForeground", t["muted_fg"])
    # The combobox's popped-down list is a plain Tk Listbox underneath,
    # not a ttk widget, so TCombobox's style.configure() above doesn't
    # reach it - it has to go through the option database instead, or it
    # keeps showing black-on-white (or white-on-black) no matter the
    # theme.
    root.option_add("*TCombobox*Listbox.background", t["edit_bg"])
    root.option_add("*TCombobox*Listbox.foreground", t["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["sel_bg"])
    root.option_add("*TCombobox*Listbox.selectForeground", t["fg"])
    _refresh_existing_combobox_popdowns(root, t)


def _refresh_existing_combobox_popdowns(root: tk.Tk, t: dict):
    """The option_add calls above only set *defaults* - Tk applies them to
    a widget once, at that widget's creation, and never again. A
    combobox's dropdown list (a plain Tk Listbox) is built lazily the
    first time it's opened and then cached for reuse, so any combobox a
    person already opened once before a live theme switch keeps showing
    its dropdown in the old colors forever, no matter how many more times
    *TCombobox*Listbox.background gets set above - the cached Listbox
    just never rereads the option database. This walks the whole widget
    tree looking for comboboxes with an already-created popdown (the
    common case is none yet exist) and repaints those few directly."""
    def walk(widget):
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            if isinstance(child, ttk.Combobox):
                popdown_listbox = f"{child}.popdown.f.l"
                try:
                    if root.tk.call("winfo", "exists", popdown_listbox):
                        root.tk.call(
                            popdown_listbox, "configure",
                            "-background", t["edit_bg"], "-foreground", t["fg"],
                            "-selectbackground", t["sel_bg"], "-selectforeground", t["fg"])
                except tk.TclError:
                    pass
            walk(child)
    walk(root)

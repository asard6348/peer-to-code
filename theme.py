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
from tkinter import font as tkfont
from tkinter import ttk

from config import CODE_FONT

# The colors a preset (built-in or user-saved) controls.
THEME_COLOR_KEYS = (
    "bg", "panel_bg", "edit_bg", "gutter_bg", "gutter_fg",
    "fg", "muted_fg", "sel_bg", "console_bg", "accent",
    "status_bg", "status_fg", "border",
)

SYSTEM_PRESET = "system"
# Display label for the "system" preset.
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
            # A dedicated "floor" tone, deliberately darker than every other surface.
            "status_bg": "#131416",
            "status_fg": "#c9ccd1",
            # Outlines: scrollbar thumbs, button/field/tab/panel edges, the line under the tab strip.
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
            "gutter_bg": "#ffffff",
            "gutter_fg": "#6e7781",
            "fg": "#24292e",
            "muted_fg": "#57606a",
            "sel_bg": "#add6ff",
            "console_bg": "#f6f8fa",
            "accent": "#0969da",
            # A deliberately dark accent stripe against this light palette.
            "status_bg": "#3a3d41",
            "status_fg": "#f5f5f5",
            # See the "dark" preset's "border" comment for what this covers.
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
    # `defaults read` exits non-zero when the key is absent, which is exactly what happens in Light mode.
    result = subprocess.run(
        ["defaults", "read", "-g", "AppleInterfaceStyle"],
        capture_output=True, text=True, timeout=2)
    return result.returncode == 0 and "dark" in result.stdout.strip().lower()


def _detect_linux_dark_mode() -> bool:
    # The freedesktop color-scheme key is the modern, desktop-agnostic signal.
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
    """The THEME_COLOR_KEYS-keyed color dict for preset `name`."""
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
    """Blends hex_color toward white by `amount` (0-1)."""
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
    """Applies the foundational look shared by the whole app: window chrome, buttons, entry fields, and the file tree."""
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # "clam" draws every widget's outer edge.
    border = t["border"]

    style.configure("TFrame", background=t["bg"])
    # A bordered variant for panels that should read as a distinct region rather than blending into whatever's next to them.
    style.configure("Explorer.TFrame", background=t["bg"], borderwidth=1, relief="solid", bordercolor=border)
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
                     fieldbackground=t["edit_bg"], borderwidth=0,
                     # "clam" draws the Treeview's outer edge using lightcolor/darkcolor for its bevel regardless of borderwidth=0 above.
                     lightcolor=t["edit_bg"], darkcolor=t["edit_bg"], bordercolor=border)
    style.map("Treeview", background=[("selected", t["sel_bg"])], foreground=[("selected", t["fg"])])
    style.configure("Treeview.Heading", background=t["panel_bg"], foreground=t["fg"],
                     bordercolor=border, lightcolor=t["panel_bg"], darkcolor=t["panel_bg"])

    # Settings' tab strip: unthemed, these otherwise keep "clam"'s fixed beige regardless of which palette is active.
    style.configure("TNotebook", background=t["panel_bg"], borderwidth=0, bordercolor=border,
                     # Same "clam" quirk as Treeview above, on the border around the notebook's own content pane.
                     lightcolor=border, darkcolor=border)
    style.configure("TNotebook.Tab", background=t["bg"], foreground=t["muted_fg"], padding=(10, 4),
                     bordercolor=border, lightcolor=t["bg"], darkcolor=t["bg"])
    style.map(
        "TNotebook.Tab",
        background=[("selected", t["panel_bg"])],
        foreground=[("selected", t["fg"])],
        lightcolor=[("selected", t["panel_bg"])],
        darkcolor=[("selected", t["panel_bg"])],
    )

    # Dropdowns: same story - "clam"'s default fieldbackground is a fixed mid-gray that was never wired up to the theme.
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

    # Scrollbars: "clam"'s built-in trough/thumb grays were never touched by this app at all before.
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


def classic_scrollbar_options(t: dict) -> dict:
    """Color options for a plain tk.Scrollbar."""
    return dict(
        background=t["border"],
        troughcolor=t["panel_bg"],
        activebackground=t["accent"],
        highlightthickness=0,
        relief="flat",
        borderwidth=0,
        elementborderwidth=0,
    )


def apply_classic_widget_defaults(root: tk.Tk, t: dict):
    """Sets sane, theme-matching defaults on the Tk option database for the plain widgets scattered through the app."""
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
    # Plain tk.Button previously never had a background set at all.
    root.option_add("*Button.background", t["panel_bg"])
    root.option_add("*Button.foreground", t["fg"])
    root.option_add("*Button.activeBackground", t["sel_bg"])
    root.option_add("*Button.activeForeground", t["fg"])
    root.option_add("*Button.disabledForeground", t["muted_fg"])
    root.option_add("*Button.highlightBackground", t["panel_bg"])
    # tk.Menu is a plain Tk widget too, and previously had no theme-derived colors at all.
    root.option_add("*Menu.background", t["bg"])
    root.option_add("*Menu.foreground", t["fg"])
    root.option_add("*Menu.activeBackground", t["sel_bg"])
    root.option_add("*Menu.activeForeground", t["fg"])
    root.option_add("*Menu.disabledForeground", t["muted_fg"])
    # The combobox's popped-down list is a plain Tk Listbox underneath, not a ttk widget.
    root.option_add("*TCombobox*Listbox.background", t["edit_bg"])
    root.option_add("*TCombobox*Listbox.foreground", t["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["sel_bg"])
    root.option_add("*TCombobox*Listbox.selectForeground", t["fg"])
    _refresh_existing_combobox_popdowns(root, t)


def _refresh_existing_combobox_popdowns(root: tk.Tk, t: dict):
    """The option_add calls above only set *defaults*."""
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


_MONO_FALLBACKS = ("Cascadia Mono", "Menlo", "DejaVu Sans Mono", "Liberation Mono", "Consolas", "Courier New")
_installed = None


def usable_font(family):
    """`family` if installed, else the first installed monospace fallback (Tk would pick a proportional face)."""
    global _installed
    if _installed is None:
        try:
            _installed = {f.lower() for f in tkfont.families()}
        except (tk.TclError, RuntimeError):
            return family
    if not family or family.lower() in _installed or family.lower() in ("tkfixedfont", "courier", "monospace"):
        return family
    return next((f for f in (CODE_FONT,) + _MONO_FALLBACKS if f.lower() in _installed), "TkFixedFont")

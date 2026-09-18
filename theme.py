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

import tkinter as tk
from tkinter import ttk


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

    style.configure("TFrame", background=t["bg"])
    style.configure("Panel.TFrame", background=t["panel_bg"])
    style.configure("TLabel", background=t["bg"], foreground=t["fg"], font=("Segoe UI", 10))
    style.configure("Panel.TLabel", background=t["panel_bg"], foreground=t["fg"], font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=t["panel_bg"], foreground=t["muted_fg"], font=("Segoe UI", 9))

    style.configure("TButton", font=("Segoe UI", 10), padding=6,
                     background=t["panel_bg"], foreground=t["fg"], focuscolor=t["panel_bg"])
    style.map(
        "TButton",
        background=[("pressed", t["accent"]), ("active", t["sel_bg"]), ("focus", t["sel_bg"])],
        foreground=[("disabled", t["muted_fg"])],
    )

    style.configure("Toolbar.TButton", font=("Segoe UI", 9), padding=(4, 2),
                     background=t["panel_bg"], foreground=t["fg"], focuscolor=t["panel_bg"])
    style.map(
        "Toolbar.TButton",
        background=[("pressed", t["accent"]), ("active", t["sel_bg"]), ("focus", t["sel_bg"])],
        foreground=[("disabled", t["muted_fg"])],
    )

    style.configure("Toolbar.Thin.TButton", font=("Segoe UI", 9), padding=(1, 2),
                     background=t["panel_bg"], foreground=t["fg"], focuscolor=t["panel_bg"])
    style.map(
        "Toolbar.Thin.TButton",
        background=[("pressed", t["accent"]), ("active", t["sel_bg"]), ("focus", t["sel_bg"])],
        foreground=[("disabled", t["muted_fg"])],
    )

    style.configure("TEntry", padding=5, fieldbackground=t["edit_bg"], foreground=t["fg"],
                     insertcolor=t["fg"], selectbackground=t["sel_bg"], selectforeground=t["fg"])
    style.map(
        "TEntry",
        fieldbackground=[("disabled", t["panel_bg"]), ("readonly", t["panel_bg"]), ("focus", t["edit_bg"])],
        foreground=[("disabled", t["muted_fg"])],
    )

    style.configure("Treeview", background=t["edit_bg"], foreground=t["fg"],
                     fieldbackground=t["edit_bg"], borderwidth=0)
    style.map("Treeview", background=[("selected", t["sel_bg"])], foreground=[("selected", t["fg"])])
    style.configure("Treeview.Heading", background=t["panel_bg"], foreground=t["fg"])


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

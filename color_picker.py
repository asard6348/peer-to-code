"""A small modal color picker built on Hue/Saturation/Value sliders,
used in place of tkinter.colorchooser.askcolor (which only offers R/G/B).
HSV maps much more directly onto how people actually describe a color
change - "a bit darker", "more saturated", "shift it toward blue" - than
juggling three independent 0-255 channels that don't individually mean
anything on their own. Pure Tk (no Pillow dependency): each slider's
gradient strip is a tiny tk.PhotoImage built one row of pixels at a time
via .put(), then stretched to full height with .zoom() - cheap enough to
rebuild on every drag.
"""
import colorsys
import tkinter as tk
from tkinter import ttk

_BAR_WIDTH = 256
_BAR_HEIGHT = 20


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _hex_to_rgb(hex_color, default=(255, 255, 255)):
    try:
        hex_color = (hex_color or "").lstrip("#")
        if len(hex_color) == 3:
            hex_color = "".join(c * 2 for c in hex_color)
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return r, g, b
    except (ValueError, IndexError):
        return default


def _rgb_to_hex(r, g, b):
    return "#{:02x}{:02x}{:02x}".format(
        _clamp(round(r), 0, 255), _clamp(round(g), 0, 255), _clamp(round(b), 0, 255))


class HSVColorDialog(tk.Toplevel):
    """Modal HSV color picker. Build, then read .result (a "#rrggbb"
    string, or None if the person canceled/closed the window)."""

    def __init__(self, parent, initial=None, title="Choose Color", t=None):
        super().__init__(parent)
        self.result = None
        self._t = t or {}
        bg = self._t.get("panel_bg", "#2b2d30")
        fg = self._t.get("fg", "#dcdfe4")
        edit_bg = self._t.get("edit_bg", "#1e1f22")
        border = self._t.get("border", "#3d4046")
        accent = self._t.get("accent", "#4c8bf5")
        self._fg = fg
        self._border = border
        self._accent = accent

        self.title(title)
        self.configure(bg=bg, highlightthickness=1, highlightbackground=border, highlightcolor=border)
        self.resizable(False, False)
        self.transient(parent)

        r, g, b = _hex_to_rgb(initial)
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        self._h, self._s, self._v = h, s, v
        self._images = {}  # keeps PhotoImage refs alive
        self._updating_hex = False

        outer = tk.Frame(self, bg=bg, padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        top = tk.Frame(outer, bg=bg)
        top.pack(fill="x", pady=(0, 10))
        self.preview = tk.Label(top, bg=self._current_hex(), width=6, height=3,
                                 relief="flat", highlightthickness=1,
                                 highlightbackground=border, highlightcolor=border)
        self.preview.pack(side="left", padx=(0, 12))

        hex_area = tk.Frame(top, bg=bg)
        hex_area.pack(side="left", fill="x", expand=True)
        tk.Label(hex_area, text="Hex", bg=bg, fg=fg, font=("Segoe UI", 9)).pack(anchor="w")
        self.hex_var = tk.StringVar(value=self._current_hex())
        hex_entry = tk.Entry(hex_area, textvariable=self.hex_var, bg=edit_bg, fg=fg,
                              insertbackground=fg, relief="flat", highlightthickness=1,
                              highlightbackground=border, highlightcolor=accent, font=("Consolas", 11))
        hex_entry.pack(anchor="w", fill="x", pady=(2, 0))
        hex_entry.bind("<Return>", self._on_hex_entered)
        hex_entry.bind("<FocusOut>", self._on_hex_entered)

        self._sliders = {}
        self._slider_canvas = {}
        self._make_slider(outer, "hue", "Hue", self._hue_row_color)
        self._make_slider(outer, "sat", "Saturation", self._sat_row_color)
        self._make_slider(outer, "val", "Value", self._val_row_color)

        btn_row = tk.Frame(outer, bg=bg)
        btn_row.pack(fill="x", pady=(12, 0))
        tk.Button(btn_row, text="Cancel", command=self._cancel, bg=bg, fg=fg,
                  activebackground=self._t.get("sel_bg", bg), activeforeground=fg).pack(side="right")
        tk.Button(btn_row, text="OK", command=self._ok, bg=bg, fg=fg,
                  activebackground=self._t.get("sel_bg", bg), activeforeground=fg).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._ok())

        self._redraw_all()
        self.update_idletasks()
        self._center_on_parent(parent)
        self.grab_set()
        hex_entry.focus_set()
        hex_entry.selection_range(0, "end")

    # -- layout helpers ---------------------------------------------------

    def _center_on_parent(self, parent):
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        except tk.TclError:
            pass

    def _make_slider(self, parent, key, label, row_color_fn):
        bg = self._t.get("panel_bg", "#2b2d30")
        fg = self._fg
        row = tk.Frame(parent, bg=bg)
        row.pack(fill="x", pady=(0, 8))
        header = tk.Frame(row, bg=bg)
        header.pack(fill="x")
        tk.Label(header, text=label, bg=bg, fg=fg, font=("Segoe UI", 9)).pack(side="left")
        value_lbl = tk.Label(header, bg=bg, fg=self._t.get("muted_fg", fg), font=("Segoe UI", 9))
        value_lbl.pack(side="right")

        canvas = tk.Canvas(row, width=_BAR_WIDTH, height=_BAR_HEIGHT, highlightthickness=1,
                            highlightbackground=self._border, highlightcolor=self._border, bd=0)
        canvas.pack(fill="x", pady=(4, 0))
        canvas.bind("<Button-1>", lambda e, k=key: self._on_slider_drag(k, e.x))
        canvas.bind("<B1-Motion>", lambda e, k=key: self._on_slider_drag(k, e.x))

        self._sliders[key] = value_lbl
        self._slider_canvas[key] = (canvas, row_color_fn)

    # -- color math for each bar's gradient --------------------------------

    def _hue_row_color(self, frac):
        r, g, b = colorsys.hsv_to_rgb(frac, 1.0, 1.0)
        return _rgb_to_hex(r * 255, g * 255, b * 255)

    def _sat_row_color(self, frac):
        r, g, b = colorsys.hsv_to_rgb(self._h, frac, self._v)
        return _rgb_to_hex(r * 255, g * 255, b * 255)

    def _val_row_color(self, frac):
        r, g, b = colorsys.hsv_to_rgb(self._h, self._s, frac)
        return _rgb_to_hex(r * 255, g * 255, b * 255)

    def _current_hex(self):
        r, g, b = colorsys.hsv_to_rgb(self._h, self._s, self._v)
        return _rgb_to_hex(r * 255, g * 255, b * 255)

    # -- redrawing ----------------------------------------------------------

    def _build_gradient_image(self, row_color_fn):
        row = "{" + " ".join(row_color_fn(x / (_BAR_WIDTH - 1)) for x in range(_BAR_WIDTH)) + "}"
        img = tk.PhotoImage(width=_BAR_WIDTH, height=1)
        img.put(row)
        return img.zoom(1, _BAR_HEIGHT)

    def _redraw_all(self):
        values = {"hue": self._h, "sat": self._s, "val": self._v}
        labels = {"hue": f"{round(self._h * 360)}°", "sat": f"{round(self._s * 100)}%",
                  "val": f"{round(self._v * 100)}%"}
        for key, (canvas, row_color_fn) in self._slider_canvas.items():
            img = self._build_gradient_image(row_color_fn)
            self._images[key] = img  # keep alive
            canvas.delete("all")
            canvas.create_image(0, 0, image=img, anchor="nw")
            x = int(values[key] * (_BAR_WIDTH - 1))
            canvas.create_line(x, 0, x, _BAR_HEIGHT, fill="#ffffff", width=3)
            canvas.create_line(x, 0, x, _BAR_HEIGHT, fill="#000000", width=1)
            self._sliders[key].configure(text=labels[key])
        hexval = self._current_hex()
        self.preview.configure(bg=hexval)
        if not self._updating_hex:
            self.hex_var.set(hexval)

    def _on_slider_drag(self, key, x):
        frac = _clamp(x / (_BAR_WIDTH - 1), 0.0, 1.0)
        if key == "hue":
            self._h = frac
        elif key == "sat":
            self._s = frac
        else:
            self._v = frac
        self._redraw_all()

    def _on_hex_entered(self, _event=None):
        text = self.hex_var.get().strip()
        if not text:
            return
        r, g, b = _hex_to_rgb(text, default=None) if text.lstrip("#") else (None, None, None)
        if r is None:
            self._updating_hex = True
            self.hex_var.set(self._current_hex())
            self._updating_hex = False
            return
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        self._h, self._s, self._v = h, s, v
        self._updating_hex = True
        self._redraw_all()
        self._updating_hex = False

    def _ok(self):
        self.result = self._current_hex()
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


def ask_color(parent, initial=None, title="Choose Color", t=None):
    """Drop-in replacement for the hex half of colorchooser.askcolor:
    returns a "#rrggbb" string, or None if the person canceled."""
    dlg = HSVColorDialog(parent, initial=initial, title=title, t=t)
    parent.wait_window(dlg)
    return dlg.result

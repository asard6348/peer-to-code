"""A horizontally scrollable strip of buffer tabs.

Purely presentational: it knows nothing about buffers, files or the
network. EditorApp hands it a list of plain dicts through render() and gets
callbacks back (select / close / context menu), so all the state lives in
one place - the editor.
"""

import sys
import tkinter as tk

CLOUD = "\u2601"  # shown on the shared tab
_IS_MAC = sys.platform == "darwin"


class TabBar(tk.Frame):
    def __init__(self, master, theme, on_select, on_close, on_context):
        super().__init__(master, bg=theme["bg"], highlightthickness=0, bd=0)
        self.theme = theme
        self.on_select = on_select
        self.on_close = on_close
        self.on_context = on_context
        self._tabs = []
        self._active_id = None
        self._cells = {}

        self.canvas = tk.Canvas(self, height=28, bg=theme["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", expand=True)
        self.inner = tk.Frame(self.canvas, bg=theme["bg"])
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_inner_configure)
        self._bind_scroll(self.canvas)
        self._bind_scroll(self.inner)

    # ---- public API -------------------------------------------------

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme["bg"])
        self.canvas.configure(bg=theme["bg"])
        self.inner.configure(bg=theme["bg"])
        self.render(self._tabs, self._active_id)

    def render(self, tabs, active_id):
        """tabs: list of dicts {id, title, shared, dirty, tooltip}."""
        self._tabs = list(tabs)
        self._active_id = active_id
        for child in self.inner.winfo_children():
            child.destroy()
        self._cells = {}
        for tab in self._tabs:
            self._cells[tab["id"]] = self._make_cell(tab, tab["id"] == active_id)
        self.after_idle(self._on_inner_configure)
        self.after_idle(lambda: self.reveal(active_id))

    def reveal(self, tab_id):
        """Scrolls just enough that the given tab is fully visible."""
        cell = self._cells.get(tab_id)
        if cell is None:
            return
        try:
            self.update_idletasks()
            total = max(self.inner.winfo_reqwidth(), 1)
            view = self.canvas.winfo_width()
            if total <= view:
                self.canvas.xview_moveto(0)
                return
            left, right = cell.winfo_x(), cell.winfo_x() + cell.winfo_width()
            first = self.canvas.canvasx(0)
            if left < first:
                self.canvas.xview_moveto(left / total)
            elif right > first + view:
                self.canvas.xview_moveto((right - view) / total)
        except tk.TclError:
            pass

    # ---- internals --------------------------------------------------

    def _make_cell(self, tab, active):
        t = self.theme
        bg = t["edit_bg"] if active else t["bg"]
        fg = t["fg"] if active else t["muted_fg"]
        cell = tk.Frame(self.inner, bg=bg, highlightthickness=0, bd=0)
        cell.pack(side="left", fill="y", padx=(0, 1))
        # A thin accent line marks the active tab.
        tk.Frame(cell, bg=(t["accent"] if active else bg), height=2).pack(side="top", fill="x")
        row = tk.Frame(cell, bg=bg)
        row.pack(side="top", fill="both", expand=True)

        widgets = [cell, row]
        if tab["shared"]:
            icon = tk.Label(row, text=CLOUD, bg=bg, fg=t["accent"], padx=0, pady=2,
                            font=("Segoe UI Symbol", 10))
            icon.pack(side="left", padx=(8, 0))
            widgets.append(icon)
        title = tk.Label(row, text=tab["title"], bg=bg, fg=fg, padx=6, pady=3)
        title.pack(side="left")
        widgets.append(title)

        closer = tk.Label(row, text=("\u25cf" if tab["dirty"] else "\u00d7"), bg=bg, fg=fg,
                          padx=5, pady=2, cursor="arrow")
        closer.pack(side="left", padx=(0, 4))
        closer.bind("<Enter>", lambda _e, c=closer: c.configure(text="\u00d7", fg=t["fg"]))
        closer.bind("<Leave>", lambda _e, c=closer, tb=tab, f=fg:
                    c.configure(text=("\u25cf" if tb["dirty"] else "\u00d7"), fg=f))
        closer.bind("<Button-1>", lambda _e, i=tab["id"]: (self.on_close(i), "break")[1])

        for w in widgets:
            w.bind("<Button-1>", lambda _e, i=tab["id"]: self.on_select(i))
            w.bind("<Button-2>" if not _IS_MAC else "<Button-3>",
                   lambda _e, i=tab["id"]: self.on_close(i))
            self._bind_context(w, tab["id"])
            self._bind_scroll(w)
        self._bind_context(closer, tab["id"])
        self._bind_scroll(closer)
        self._bind_scroll(cell)
        return cell

    def _bind_context(self, widget, tab_id):
        def fire(event):
            self.on_context(tab_id, event.x_root, event.y_root)
            return "break"
        if _IS_MAC:
            widget.bind("<Button-2>", fire)
            widget.bind("<Control-Button-1>", fire)
        else:
            widget.bind("<Button-3>", fire)

    def _bind_scroll(self, widget):
        widget.bind("<MouseWheel>", self._on_wheel, add="+")
        widget.bind("<Button-4>", lambda e: self._scroll(-1), add="+")
        widget.bind("<Button-5>", lambda e: self._scroll(1), add="+")

    def _on_wheel(self, event):
        self._scroll(-1 if event.delta > 0 else 1)

    def _scroll(self, direction):
        if self.inner.winfo_reqwidth() > self.canvas.winfo_width():
            self.canvas.xview_scroll(direction * 3, "units")

    def _on_inner_configure(self, _event=None):
        try:
            self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(),
                                                 self.inner.winfo_reqheight()))
            self.canvas.configure(height=self.inner.winfo_reqheight())
        except tk.TclError:
            pass

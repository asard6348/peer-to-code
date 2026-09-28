"""A horizontally scrollable strip of buffer tabs.

Purely presentational: it knows nothing about buffers, files or the
network. EditorApp hands it a list of plain dicts through render() and gets
callbacks back (select / close / context menu / move), so all the state lives in
one place - the editor.
"""

import sys
import tkinter as tk

CLOUD = "\u2601"  # shown on the shared tab
_IS_MAC = sys.platform == "darwin"


class TabBar(tk.Frame):
    def __init__(self, master, theme, on_select, on_close, on_context, on_move=None):
        super().__init__(master, bg=theme["bg"], highlightthickness=0, bd=0)
        self.theme = theme
        self.on_select = on_select
        self.on_close = on_close
        self.on_context = on_context
        self.on_move = on_move
        self._drag = None
        self._indicator = None
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
        self._end_drag()
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
            # Selection happens on release (not press) so that a drag can
            # start without the tab strip being re-rendered underneath it.
            w.bind("<Button-1>", lambda e, i=tab["id"]: self._on_press(e, i))
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", lambda e, i=tab["id"]: self._on_release(e, i))
            w.bind("<Button-2>" if not _IS_MAC else "<Button-3>",
                   lambda _e, i=tab["id"]: self.on_close(i))
            self._bind_context(w, tab["id"])
            self._bind_scroll(w)
        self._bind_context(closer, tab["id"])
        self._bind_scroll(closer)
        self._bind_scroll(cell)
        return cell

    # ---- drag to reorder --------------------------------------------

    _DRAG_THRESHOLD = 5

    def _on_press(self, event, tab_id):
        self._drag = {"id": tab_id, "x0": event.x_root, "active": False, "index": None}

    def _on_drag(self, event):
        drag = self._drag
        if drag is None or self.on_move is None:
            return
        if not drag["active"]:
            if abs(event.x_root - drag["x0"]) < self._DRAG_THRESHOLD:
                return
            drag["active"] = True
            self._indicator = tk.Frame(self.inner, bg=self.theme["accent"], width=3)
        # Scroll the strip when the pointer is pushed against either edge.
        left = self.canvas.winfo_rootx()
        right = left + self.canvas.winfo_width()
        if event.x_root < left:
            self._scroll(-1)
        elif event.x_root > right:
            self._scroll(1)
        drag["index"] = self._drop_index(event.x_root, drag["id"])
        self._place_indicator(drag["index"], drag["id"])

    def _on_release(self, event, tab_id):
        drag, self._drag = self._drag, None
        active = bool(drag and drag["active"])
        index = drag["index"] if drag else None
        self._remove_indicator()
        if active and index is not None and self.on_move is not None:
            self.on_move(tab_id, index)
        self.on_select(tab_id)

    def _end_drag(self):
        self._drag = None
        self._remove_indicator()

    def _remove_indicator(self):
        if self._indicator is not None:
            try:
                self._indicator.destroy()
            except tk.TclError:
                pass
            self._indicator = None

    def _others(self, dragged_id):
        return [t["id"] for t in self._tabs if t["id"] != dragged_id and t["id"] in self._cells]

    def _drop_index(self, x_root, dragged_id):
        """Position (among the tabs other than the dragged one) it would be
        inserted at if dropped at x_root."""
        index = 0
        for tid in self._others(dragged_id):
            cell = self._cells[tid]
            if x_root > cell.winfo_rootx() + cell.winfo_width() / 2:
                index += 1
        return index

    def _place_indicator(self, index, dragged_id):
        if self._indicator is None:
            return
        others = self._others(dragged_id)
        if not others:
            self._indicator.place_forget()
            return
        if index < len(others):
            x = self._cells[others[index]].winfo_x() - 2
        else:
            last = self._cells[others[-1]]
            x = last.winfo_x() + last.winfo_width() + 1
        self._indicator.place(x=max(x, 0), y=0, width=3, height=self.inner.winfo_height())
        self._indicator.lift()

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

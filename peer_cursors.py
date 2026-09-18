import tkinter as tk


def _mix(hex_color, bg_hex, alpha):
    def to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    a, b = to_rgb(hex_color), to_rgb(bg_hex)
    mixed = tuple(int(a[i] * alpha + b[i] * (1 - alpha)) for i in range(3))
    return "#%02x%02x%02x" % mixed


class PeerCursorLayer:
    """Floating overlay widgets showing where every other collaborator's
    cursor and selection currently are, kept positioned on top of the Text
    widget via bbox() lookups (which already account for scroll/wrap)."""

    def __init__(self, text_widget, bg_color, self_id):
        self.text = text_widget
        self.bg_color = bg_color
        self.self_id = self_id
        self.pad_x = int(text_widget.cget("padx"))
        self.pad_y = int(text_widget.cget("pady"))
        self.peers = {}
        self.widgets = {}

    def set_self_id(self, self_id):
        """After a P2P failover promotion, a brand-new Server hands out
        fresh ids from 1, so "which id is me" can change mid-session."""
        self.self_id = self_id

    def set_roster(self, roster):
        live_ids = set()
        for pr in roster:
            pid = pr["id"]
            if pid == self.self_id:
                continue
            live_ids.add(pid)
            info = self.peers.setdefault(pid, {"offset": None, "sel": None})
            info["name"] = pr["name"]
            info["color"] = pr["color"]
            cursor = pr.get("cursor")
            if cursor:
                info["offset"] = cursor.get("index")
                info["sel"] = (cursor["start"], cursor["end"]) if cursor.get("sel") else None
            self._ensure_widgets(pid)
        for pid in list(self.peers):
            if pid not in live_ids:
                self._remove(pid)
        self.refresh()

    def update_cursor(self, payload):
        pid = payload.get("from")
        if pid is None or pid not in self.peers:
            return
        info = self.peers[pid]
        info["offset"] = payload.get("index")
        info["sel"] = (payload["start"], payload["end"]) if payload.get("sel") else None
        self.refresh()

    def _ensure_widgets(self, pid):
        if pid in self.widgets:
            return
        color = self.peers[pid]["color"]
        caret = tk.Frame(self.text, bg=color, width=2, height=14, bd=0)
        label = tk.Label(self.text, text=self.peers[pid]["name"], bg=color, fg="#111111",
                          font=("Segoe UI", 7, "bold"), padx=3, pady=0, bd=0)
        sel_tag = f"peer_sel_{pid}"
        self.text.tag_configure(sel_tag, background=_mix(color, self.bg_color, 0.35))
        self.text.tag_lower(sel_tag)
        self.widgets[pid] = {"caret": caret, "label": label, "sel_tag": sel_tag}

    def _remove(self, pid):
        w = self.widgets.pop(pid, None)
        if w:
            w["caret"].destroy()
            w["label"].destroy()
            self.text.tag_delete(w["sel_tag"])
        self.peers.pop(pid, None)

    def refresh(self):
        for pid, info in self.peers.items():
            w = self.widgets.get(pid)
            if not w:
                continue
            self.text.tag_remove(w["sel_tag"], "1.0", "end")
            if info["sel"]:
                a, b = info["sel"]
                if isinstance(a, int) and isinstance(b, int) and b > a:
                    self.text.tag_add(w["sel_tag"], f"1.0+{a}c", f"1.0+{b}c")
            offset = info["offset"]
            bbox = self.text.bbox(f"1.0+{offset}c") if isinstance(offset, int) else None
            if not bbox:
                w["caret"].place_forget()
                w["label"].place_forget()
                continue
            x, y, _bw, h = bbox
            x -= self.pad_x
            y -= self.pad_y
            w["caret"].place(x=x, y=y, height=h, width=2)
            label_y = y - 15 if y - 15 >= 0 else y + h
            w["label"].place(x=x, y=label_y)

    def set_theme(self, bg_color):
        self.bg_color = bg_color
        for pid, w in self.widgets.items():
            color = self.peers[pid]["color"]
            self.text.tag_configure(w["sel_tag"], background=_mix(color, bg_color, 0.35))
        self.refresh()

    def clear(self):
        for pid in list(self.widgets):
            self._remove(pid)

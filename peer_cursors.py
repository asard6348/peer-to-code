import tkinter as tk

import ot

UP_ARROW = "▲"
DOWN_ARROW = "▼"


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
    widget via bbox() lookups (which already account for scroll/wrap).

    A peer whose cursor is scrolled out of view gets a small marker pinned to
    the top or bottom edge of the Text widget instead (their name with an
    up/down arrow); clicking it scrolls to them.

    Positions are character offsets into the shared document, so they have to
    be kept in step with every edit - ours and everyone else's - or a caret
    would drift by however much text was inserted or deleted before it. See
    apply_op."""

    EDGE_MARGIN = 4
    EDGE_GAP = 4
    EDGE_FONT = ("Segoe UI", 7, "bold")
    EDGE_PAD = (4, 1)       # padx, pady of a marker

    def __init__(self, text_widget, bg_color, self_id):
        self.text = text_widget
        self.bg_color = bg_color
        self.self_id = self_id
        self.pad_x = int(text_widget.cget("padx"))
        self.pad_y = int(text_widget.cget("pady"))
        self.peers = {}
        self.widgets = {}
        self.visible = True
        # One row per edge, anchored to its right-hand corner; the markers
        # are packed into it so Tk does the sizing.
        self._edge_rows = {side: tk.Frame(text_widget, bd=0, highlightthickness=0, bg=bg_color) for side in ("up", "down")}

    def set_visible(self, visible):
        """Peer cursors are offsets into the shared document, so they're
        hidden while a local (non-shared) buffer is being shown."""
        self.visible = bool(visible)
        self.refresh()

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
            # The roster only carries the last cursor the server heard from
            # this peer, which can predate edits this side has already seen
            # (and shifted the position for). Once a peer's position is
            # known here it is kept current by update_cursor and apply_op, so
            # the roster only fills in peers not heard from yet.
            if cursor and info["offset"] is None:
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

    def apply_op(self, op_components, author=None):
        """Moves every peer's caret and selection along with an edit to the
        shared document (an ot.Op's components), so text inserted or deleted
        before a caret shifts it instead of leaving it pointing at the same
        offset - which is a different spot in the changed text. `author` is
        the peer who made the edit, or None for an edit made here: their own
        caret ends up after what they typed."""
        for pid, info in self.peers.items():
            mine = author is not None and pid == author
            offset = info["offset"]
            if isinstance(offset, int):
                info["offset"] = ot.transform_position(op_components, offset, stick_right=mine)
            sel = info["sel"]
            if sel and isinstance(sel[0], int) and isinstance(sel[1], int):
                # Text typed at either edge of a selection stays outside it.
                info["sel"] = (ot.transform_position(op_components, sel[0], stick_right=True),
                               ot.transform_position(op_components, sel[1], stick_right=False))
        self.refresh()

    def jump_to(self, pid):
        """Scrolls so that peer `pid`'s caret is on screen."""
        info = self.peers.get(pid)
        if not info or not isinstance(info["offset"], int):
            return
        try:
            self.text.see(f"1.0+{info['offset']}c")
        except tk.TclError:
            return
        self.refresh()

    def _ensure_widgets(self, pid):
        if pid in self.widgets:
            return
        color = self.peers[pid]["color"]
        name = self.peers[pid]["name"]
        caret = tk.Frame(self.text, bg=color, width=2, height=14, bd=0)
        label = tk.Label(self.text, text=name, bg=color, fg="#111111",
                          font=("Segoe UI", 7, "bold"), padx=3, pady=0, bd=0)
        edge = tk.Label(self.text, text="", bg=color, fg="#111111", font=self.EDGE_FONT,
                         padx=self.EDGE_PAD[0], pady=self.EDGE_PAD[1], bd=0, cursor="hand2")
        edge.bind("<Button-1>", lambda _e, p=pid: self.jump_to(p))
        sel_tag = f"peer_sel_{pid}"
        self.text.tag_configure(sel_tag, background=_mix(color, self.bg_color, 0.35))
        self.text.tag_lower(sel_tag)
        self.widgets[pid] = {"caret": caret, "label": label, "edge": edge, "edge_text": None,
                             "sel_tag": sel_tag}

    def _remove(self, pid):
        w = self.widgets.pop(pid, None)
        if w:
            w["caret"].destroy()
            w["label"].destroy()
            w["edge"].destroy()
            self.text.tag_delete(w["sel_tag"])
        self.peers.pop(pid, None)

    def _edge_side(self, index):
        """"up" / "down" when the line holding `index` is scrolled out of
        view above / below the Text widget, else None (including when it is
        only scrolled out sideways)."""
        t = self.text
        try:
            if t.compare(index, "<", t.index("@0,0")):
                return "up"
            if t.dlineinfo(index) is None:
                return "down"
        except tk.TclError:
            pass
        return None

    @staticmethod
    def _hide(w):
        w["caret"].place_forget()
        w["label"].place_forget()

    def refresh(self):
        edge_peers = {"up": [], "down": []}
        for pid, info in self.peers.items():
            w = self.widgets.get(pid)
            if not w:
                continue
            self.text.tag_remove(w["sel_tag"], "1.0", "end")
            if not self.visible:
                self._hide(w)
                continue
            if info["sel"]:
                a, b = info["sel"]
                if isinstance(a, int) and isinstance(b, int) and b > a:
                    self.text.tag_add(w["sel_tag"], f"1.0+{a}c", f"1.0+{b}c")
            offset = info["offset"]
            index = f"1.0+{offset}c" if isinstance(offset, int) else None
            bbox = self.text.bbox(index) if index else None
            if not bbox:
                self._hide(w)
                side = self._edge_side(index) if index else None
                if side:
                    edge_peers[side].append(pid)
                continue
            x, y, _bw, h = bbox
            x -= self.pad_x
            y -= self.pad_y
            w["caret"].place(x=x, y=y, height=h, width=2)
            label_y = y - 15 if y - 15 >= 0 else y + h
            w["label"].place(x=x, y=label_y)
        self._place_edge_markers(edge_peers)

    def _place_edge_markers(self, edge_peers):
        """Pins the markers for off-screen peers to the top/bottom edge,
        side by side from the right."""
        for side, arrow in (("up", UP_ARROW), ("down", DOWN_ARROW)):
            row = self._edge_rows[side]
            peers = edge_peers[side]
            for pid in self.widgets:
                if pid not in peers and self.widgets[pid]["edge"].winfo_manager() == "pack" \
                        and self.widgets[pid]["edge"].pack_info().get("in") is row:
                    self.widgets[pid]["edge"].pack_forget()
            for pid in peers:
                w = self.widgets[pid]
                text = f"{arrow} {self.peers[pid]['name']}"
                if w["edge_text"] != text:
                    w["edge"].configure(text=text)
                    w["edge_text"] = text
                w["edge"].pack_forget()
                w["edge"].pack(in_=row, side="right", padx=(self.EDGE_GAP, 0))
            if peers:
                if side == "up":
                    row.place(relx=1.0, x=-self.EDGE_MARGIN, y=self.EDGE_MARGIN, anchor="ne")
                else:
                    row.place(relx=1.0, x=-self.EDGE_MARGIN, rely=1.0, y=-self.EDGE_MARGIN, anchor="se")
                row.lift()
                for pid in peers:
                    self.widgets[pid]["edge"].lift(row)     # labels must sit above the row they are packed in
            else:
                row.place_forget()

    def set_theme(self, bg_color):
        self.bg_color = bg_color
        for row in self._edge_rows.values():
            row.configure(bg=bg_color)
        for pid, w in self.widgets.items():
            color = self.peers[pid]["color"]
            self.text.tag_configure(w["sel_tag"], background=_mix(color, bg_color, 0.35))
        self.refresh()

    def clear(self):
        for pid in list(self.widgets):
            self._remove(pid)

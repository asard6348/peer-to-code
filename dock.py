"""Dockable panels: Explorer, Terminal and Chat.

Every panel is one `tk.Frame` (DockPanel.frame) that is a child of the editor
frame (the "host") and is never a pane itself. That is what lets it move:

  * Docked: the frame is pack()ed `in_` a "slot" frame that lives inside a
    PanedWindow container on the left, right or bottom of the editor. Tk
    allows a widget to be packed into any descendant of its parent, so the
    panel can hop between slots without being re-created.
  * Floating: the very same frame is turned into a top-level window with
    `wm manage` (Tk 8.5+), and back again with `wm forget`. Everything inside
    it - the Text widgets, the scrollbars, the running console - stays alive.

A panel's header (title, menu, close button) is the handle: drag it to the
left/right/bottom edge of the editor to dock there, or drop it outside the
main window to tear it off; drag a floating panel's header back over an edge
to attach it again. The same actions are in the header's menu, and
double-clicking the header toggles between docked and floating.

The manager also remembers each panel's side, size and floating position in
the config (see DockManager.save / load), each part opt-in under
Settings > General > Window.
"""

import re
import tkinter as tk

SIDES = ("left", "right", "bottom")
FLOAT = "float"

# Pixels the pointer has to travel, button down, before a click on a header
# turns into a drag.
DRAG_SLOP = 6
# While dragging, the part of the editor area (from its left/right edge, or
# up from its bottom edge) that counts as "dock here".
EDGE_FRACTION = 0.22
BOTTOM_FRACTION = 0.30
# Smallest floating window, and how much of it must stay on screen.
FLOAT_MIN = (220, 120)
FLOAT_VISIBLE_MARGIN = 80

# key -> defaults. `vis_key` is the (long-standing) ui flag the panel's
# visibility is stored under; `size` is (width, height) when first docked or
# floated; `min_w`/`min_h` are the pane minimums.
PANEL_SPECS = {
    "explorer": {"title": "EXPLORER", "vis_key": "explorer_visible", "dock": "left",
                 "size": (220, 400), "min_w": 60, "min_h": 60},
    "terminal": {"title": "TERMINAL", "vis_key": "console_visible", "dock": "bottom",
                 "size": (560, 180), "min_w": 80, "min_h": 70},
    "chat": {"title": "CHAT", "vis_key": "chat_visible", "dock": "bottom",
             "size": (340, 180), "min_w": 120, "min_h": 70},
}
# Left-to-right / top-to-bottom order panels take inside a shared dock.
ORDER = ("explorer", "terminal", "chat")

_SIZE_RE = re.compile(r"^(\d+)x(\d+)$")
_POS_RE = re.compile(r"^([+-]-?\d+)([+-]-?\d+)$")
_GEOM_RE = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")


def _coord(text):
    """'+12' -> 12, '+-5' -> -5 (Tk's way of writing a negative position)."""
    return int(text[1:]) if text[0] == "+" else -int(text[1:])


def parse_size(value):
    m = _SIZE_RE.match(value or "") if isinstance(value, str) else None
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if w > 0 and h > 0 else None


def parse_pos(value):
    m = _POS_RE.match(value or "") if isinstance(value, str) else None
    return (_coord(m.group(1)), _coord(m.group(2))) if m else None


def _pos_int(value):
    """A saved dimension, or None when it is missing/garbled/absurd."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 20 <= value <= 10000 else None


class DockPanel:
    def __init__(self, mgr, key):
        spec = PANEL_SPECS[key]
        self.mgr = mgr
        self.key = key
        self.title = spec["title"]
        self.vis_key = spec["vis_key"]
        self.min_w, self.min_h = spec["min_w"], spec["min_h"]
        t = mgr.theme

        self.visible = True
        # Unavailable right now (e.g. Chat in an unshared session): kept off
        # screen whatever `visible` says, and `visible` - the person's own
        # preference, which is what gets saved - is left alone.
        self.disabled = False
        self.floating = False
        self.dock = spec["dock"]            # side used when docked (kept while floating)
        self.size = tuple(spec["size"])     # last known docked (width, height)
        self.float_size = None              # "WxH" of the floating window, once known
        self.float_pos = None               # "+X+Y" of the floating window, once known
        self._shown = None                  # None, a side of SIDES, or FLOAT
        self._drag = None

        self.frame = tk.Frame(mgr.host, bg=t["bg"])
        self.header = tk.Frame(self.frame, bg=t["bg"])
        self.header.pack(side="top", fill="x")
        self.title_label = tk.Label(self.header, text=self.title, bg=t["bg"], fg=t["muted_fg"],
                                    font=("Segoe UI", 9, "bold"), anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=(4, 0))
        self.close_btn = tk.Label(self.header, text="×", bg=t["bg"], fg=t["muted_fg"],
                                  font=("Segoe UI", 11), padx=6, cursor="hand2")
        self.close_btn.pack(side="right", pady=(2, 0))
        # A real Menubutton, not a Label that pops a menu on click: Tk's own
        # Menubutton drops the menu below the button and supports both
        # click-then-pick and press-drag-release. Popping a menu up under
        # the pointer on mouse-down made the release land on the first
        # entry and pick it straight away.
        self.menu_btn = tk.Menubutton(self.header, text="≡", bg=t["bg"], fg=t["muted_fg"],
                                      font=("Segoe UI", 11), padx=6, pady=0, bd=0, relief="flat",
                                      highlightthickness=0, indicatoron=False, cursor="hand2",
                                      activebackground=t["sel_bg"], activeforeground=t["fg"])
        self.menu_btn.pack(side="right", pady=(2, 0))
        self.content = tk.Frame(self.frame, bg=t["bg"])
        self.content.pack(side="top", fill="both", expand=True)

        self._menu = tk.Menu(self.menu_btn, tearoff=0, postcommand=self._fill_menu)
        self.menu_btn.configure(menu=self._menu)
        self._close_cb = self.frame.register(lambda: mgr.set_visible(self, False))
        self.slots = {side: tk.Frame(mgr.containers[side], bg=t["bg"]) for side in SIDES}

        for w in (self.header, self.title_label):
            w.bind("<ButtonPress-1>", lambda e: mgr.drag_start(self, e))
            w.bind("<B1-Motion>", lambda e: mgr.drag_motion(self, e))
            w.bind("<ButtonRelease-1>", lambda e: mgr.drag_end(self, e))
            w.bind("<Double-Button-1>", lambda e: mgr.toggle_float(self))
            w.bind("<Button-3>", self._popup_menu)
        self.close_btn.bind("<Button-1>", lambda e: mgr.set_visible(self, False))
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.configure(bg=self.mgr.theme["sel_bg"]))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.configure(bg=self.mgr.theme["bg"]))
        self.set_theme(t)

    # -- convenience --------------------------------------------------------

    def show(self):
        self.mgr.set_visible(self, True)

    def hide(self):
        self.mgr.set_visible(self, False)

    def toggle(self):
        if self.disabled:
            return
        self.mgr.set_visible(self, not self.visible)

    @property
    def shown_as(self):
        return self._shown

    def set_theme(self, t):
        bg = t["bg"]
        for w in (self.frame, self.header, self.content):
            w.configure(bg=bg)
        for side in SIDES:
            self.slots[side].configure(bg=bg)
        self.title_label.configure(bg=bg, fg=t["muted_fg"])
        for btn in (self.menu_btn, self.close_btn):
            btn.configure(bg=bg, fg=t["muted_fg"])
        self.menu_btn.configure(activebackground=t["sel_bg"], activeforeground=t["fg"])
        self._menu.configure(bg=t["panel_bg"], fg=t["fg"], activebackground=t["sel_bg"],
                             activeforeground=t["fg"], bd=0)

    def _fill_menu(self):
        """Rebuilds the menu for the panel's current state; runs every time
        it is about to be shown (the Menubutton's postcommand)."""
        m, mgr = self._menu, self.mgr
        m.delete(0, "end")
        if self.floating:
            m.add_command(label="Attach to main window", command=lambda: mgr.attach(self))
        else:
            m.add_command(label="Detach to window", command=lambda: mgr.float_panel(self))
        m.add_separator()
        for side in SIDES:
            mark = "✓ " if (not self.floating and self.dock == side) else "    "
            m.add_command(label=f"{mark}Dock {side}", command=lambda s=side: mgr.dock_panel(self, s))
        m.add_separator()
        m.add_command(label="Hide", command=self.hide)

    def _popup_menu(self, event):
        """Right-click on the title bar: the same menu, at the pointer."""
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()


class DockManager:
    def __init__(self, host, body, center, anchor, theme, on_visibility=None):
        """host: the editor frame every panel frame is a child of. body: the
        horizontal PanedWindow holding [left dock | center | right dock].
        center: the vertical PanedWindow holding [editor | bottom dock].
        anchor: the editor widget in `center` the bottom dock goes under.
        on_visibility(panel) is called after a panel is shown or hidden."""
        self.host = host
        self.body = body
        self.center = center
        self.anchor = anchor
        self.theme = theme
        self.on_visibility = on_visibility
        self.panels = {}
        bg = theme["bg"]
        self.containers = {
            "left": tk.PanedWindow(body, orient="vertical", bg=bg, sashwidth=4, bd=0),
            "right": tk.PanedWindow(body, orient="vertical", bg=bg, sashwidth=4, bd=0),
            "bottom": tk.PanedWindow(center, orient="horizontal", bg=bg, sashwidth=4, bd=0),
        }
        self._attached = {side: False for side in SIDES}
        # The highlight shown over the edge a dragged panel would dock to.
        self._zone = tk.Frame(host, bg=theme["accent"])
        self._zone_label = tk.Label(self._zone, bg=theme["accent"], fg="white", font=("Segoe UI", 10, "bold"))
        self._zone_label.place(relx=0.5, rely=0.5, anchor="center")
        self._zone_side = None

    # -- panels ---------------------------------------------------------------

    def add_panel(self, key):
        panel = DockPanel(self, key)
        self.panels[key] = panel
        return panel

    def ordered(self):
        return [self.panels[k] for k in ORDER if k in self.panels]

    def set_theme(self, t):
        self.theme = t
        for cont in self.containers.values():
            cont.configure(bg=t["bg"])
        for panel in self.panels.values():
            panel.set_theme(t)
        self._zone.configure(bg=t["accent"])
        self._zone_label.configure(bg=t["accent"])

    # -- state changes -----------------------------------------------------------

    def set_visible(self, panel, visible):
        visible = bool(visible)
        if panel.visible == visible:
            if visible:
                self.raise_panel(panel)
            return
        panel.visible = visible
        self.refresh(panel)
        if self.on_visibility:
            self.on_visibility(panel)

    def set_disabled(self, panel, disabled):
        """Takes `panel` out of (or back into) the layout without touching
        its `visible` preference, so it comes back exactly as it was."""
        disabled = bool(disabled)
        if panel.disabled == disabled:
            return
        panel.disabled = disabled
        self.refresh(panel)

    def raise_panel(self, panel):
        if panel._shown == FLOAT:
            try:
                panel.frame.lift()
            except tk.TclError:
                pass

    def float_panel(self, panel, x=None, y=None):
        """Detaches `panel` into its own window (and shows it). Without x/y
        it reappears where it last floated, or just offset from where it
        was docked."""
        if x is not None and y is not None:
            panel.float_pos = f"+{int(x)}+{int(y)}"
        elif not panel.floating and panel._shown in SIDES and panel.float_pos is None:
            f = panel.frame
            if f.winfo_ismapped():
                panel.float_pos = f"+{f.winfo_rootx() + 24}+{f.winfo_rooty() + 24}"
        was_visible = panel.visible
        panel.floating = True
        panel.visible = True
        self.refresh(panel)
        if not was_visible and self.on_visibility:
            self.on_visibility(panel)

    def dock_panel(self, panel, side):
        if side not in SIDES:
            return
        was_visible = panel.visible
        panel.dock = side
        panel.floating = False
        panel.visible = True
        self.refresh(panel)
        if not was_visible and self.on_visibility:
            self.on_visibility(panel)

    def attach(self, panel):
        self.dock_panel(panel, panel.dock)

    def toggle_float(self, panel):
        if panel.floating:
            self.attach(panel)
        else:
            self.float_panel(panel)

    def refresh(self, panel):
        """Makes the screen match the panel's (visible, floating, dock)."""
        want = None
        if panel.visible and not panel.disabled:
            want = FLOAT if panel.floating else panel.dock
        old = panel._shown
        if want == old:
            return
        if old is not None:
            self._capture(panel)
            panel._shown = None
            self._leave(panel, old)
        if want is not None:
            panel._shown = want
            self._enter(panel, want)

    def present_all(self):
        for panel in self.ordered():
            self.refresh(panel)

    def reset(self):
        """Back to the default layout: every panel visible, docked where it
        started, at its default size. Also the way out of a floating window
        that ended up somewhere unreachable."""
        for panel in self.ordered():
            old, panel._shown = panel._shown, None
            if old is not None:
                self._leave(panel, old)
        for panel in self.ordered():
            spec = PANEL_SPECS[panel.key]
            panel.visible = True
            panel.floating = False
            panel.dock = spec["dock"]
            panel.size = tuple(spec["size"])
            panel.float_size = panel.float_pos = None
        self.present_all()
        if self.on_visibility:
            for panel in self.ordered():
                self.on_visibility(panel)

    # -- geometry ------------------------------------------------------------------

    def _screen_bounds(self):
        h = self.host
        sw, sh = h.winfo_screenwidth(), h.winfo_screenheight()
        vx, vy = h.winfo_vrootx(), h.winfo_vrooty()
        vw, vh = h.winfo_vrootwidth() or sw, h.winfo_vrootheight() or sh
        return vx, vy, max(vw, sw), max(vh, sh)

    def _float_geometry(self, panel):
        w, h = parse_size(panel.float_size) or panel.size
        vx, vy, vw, vh = self._screen_bounds()
        w = max(FLOAT_MIN[0], min(w, vw))
        h = max(FLOAT_MIN[1], min(h, vh))
        pos = parse_pos(panel.float_pos)
        if pos is None:
            pos = (self.host.winfo_rootx() + 60, self.host.winfo_rooty() + 80)
        x = max(vx, min(pos[0], vx + vw - FLOAT_VISIBLE_MARGIN))
        y = max(vy, min(pos[1], vy + vh - FLOAT_VISIBLE_MARGIN))
        return f"{w}x{h}+{x}+{y}"

    def _capture(self, panel):
        """Remembers where the panel currently is: the slot's size when
        docked, the window's size and position when floating. Skips anything
        not on screen yet - an unmapped window reports meaningless numbers."""
        shown = panel._shown
        if shown in SIDES:
            slot = panel.slots[shown]
            if slot.winfo_ismapped():
                w, h = slot.winfo_width(), slot.winfo_height()
                if w > 1 and h > 1:
                    # The first panel in a dock stretches to fill whatever
                    # the others leave, so its extent along the dock's own
                    # axis (width in the bottom dock, height in the side
                    # docks) isn't a size anyone chose: keep the old value
                    # rather than saving "the whole window".
                    first = self._members(shown)[0] is panel
                    ow, oh = panel.size
                    if first and shown == "bottom":
                        w = ow
                    elif first:
                        h = oh
                    panel.size = (w, h)
        elif shown == FLOAT:
            f = panel.frame
            if f.winfo_viewable():
                m = _GEOM_RE.match(str(f.tk.call("wm", "geometry", f._w)))
                if m:
                    panel.float_size = f"{m.group(1)}x{m.group(2)}"
                    panel.float_pos = f"+{_coord(m.group(3))}+{_coord(m.group(4))}"

    # -- docking / floating plumbing -------------------------------------------

    def _members(self, side):
        return [p for p in self.ordered() if p._shown == side]

    def _restretch(self, side):
        """The first panel in a dock soaks up resizes; the others keep their size."""
        cont = self.containers[side]
        for i, p in enumerate(self._members(side)):
            cont.paneconfigure(p.slots[side], stretch="always" if i == 0 else "never")

    def _attach_container(self, side, panel):
        cont = self.containers[side]
        w, h = panel.size
        if side == "left":
            self.body.add(cont, before=str(self.center), minsize=60, stretch="never", width=max(60, w))
        elif side == "right":
            self.body.add(cont, after=str(self.center), minsize=60, stretch="never", width=max(60, w))
        else:
            self.center.add(cont, after=str(self.anchor), minsize=70, stretch="never", height=max(70, h))
        self._attached[side] = True

    def _enter(self, panel, where):
        f = panel.frame
        if where == FLOAT:
            root = self.host.winfo_toplevel()
            f.pack_forget()
            root.wm_manage(f)
            wm = lambda *args: f.tk.call("wm", args[0], f._w, *args[1:])
            wm("title", f"Peer to Code - {panel.title.title()}")
            wm("minsize", *FLOAT_MIN)
            wm("transient", root._w)
            wm("protocol", "WM_DELETE_WINDOW", panel._close_cb)
            wm("geometry", self._float_geometry(panel))
            return
        side = where
        if not self._attached[side]:
            self._attach_container(side, panel)
        members = self._members(side)
        slot = panel.slots[side]
        opts = {"minsize": panel.min_w if side == "bottom" else panel.min_h}
        later = [p for p in members if ORDER.index(p.key) > ORDER.index(panel.key)]
        if later:
            opts["before"] = str(later[0].slots[side])
        if members[0] is not panel:
            opts["width" if side == "bottom" else "height"] = panel.size[0 if side == "bottom" else 1]
        self.containers[side].add(slot, **opts)
        self._restretch(side)
        f.pack(in_=slot, fill="both", expand=True)
        f.lift()

    def _leave(self, panel, where):
        f = panel.frame
        if where == FLOAT:
            try:
                self.host.winfo_toplevel().wm_forget(f)
            except tk.TclError:
                pass
            return
        side = where
        f.pack_forget()
        try:
            self.containers[side].forget(panel.slots[side])
        except tk.TclError:
            pass
        if not self._members(side):
            parent = self.center if side == "bottom" else self.body
            try:
                parent.forget(self.containers[side])
            except tk.TclError:
                pass
            self._attached[side] = False
        else:
            self._restretch(side)

    # -- dragging ------------------------------------------------------------------

    def _inside(self, widget, x_root, y_root):
        x, y = widget.winfo_rootx(), widget.winfo_rooty()
        return x <= x_root < x + widget.winfo_width() and y <= y_root < y + widget.winfo_height()

    def zone_at(self, x_root, y_root):
        """Which dock edge of the editor area the pointer is over, if any."""
        body = self.body
        if not self._inside(body, x_root, y_root):
            return None
        bw, bh = max(1, body.winfo_width()), max(1, body.winfo_height())
        rx, ry = (x_root - body.winfo_rootx()) / bw, (y_root - body.winfo_rooty()) / bh
        if rx < EDGE_FRACTION:
            return "left"
        if rx > 1 - EDGE_FRACTION:
            return "right"
        if ry > 1 - BOTTOM_FRACTION:
            return "bottom"
        return None

    def _show_zone(self, side):
        if side == self._zone_side:
            return
        self._zone_side = side
        if side is None:
            self._zone.place_forget()
            return
        bw, bh = self.body.winfo_width(), self.body.winfo_height()
        if side == "bottom":
            w, h = bw, int(bh * BOTTOM_FRACTION)
            x, y = 0, bh - h
        else:
            w, h = int(bw * EDGE_FRACTION), bh
            x, y = (0 if side == "left" else bw - w), 0
        self._zone_label.configure(text=f"Dock {side}")
        self._zone.place(in_=self.body, x=x, y=y, width=w, height=h)
        self._zone.lift()

    def _hide_zone(self):
        self._show_zone(None)

    def _wm_origin(self, panel):
        m = _GEOM_RE.match(str(panel.frame.tk.call("wm", "geometry", panel.frame._w)))
        return (_coord(m.group(3)), _coord(m.group(4))) if m else (0, 0)

    def drag_start(self, panel, event):
        if panel.floating:
            ox, oy = self._wm_origin(panel)
        else:
            ox, oy = panel.frame.winfo_rootx(), panel.frame.winfo_rooty()
        panel._drag = {"x": event.x_root, "y": event.y_root, "dx": event.x_root - ox,
                       "dy": event.y_root - oy, "moved": False}

    def drag_motion(self, panel, event):
        d = panel._drag
        if d is None:
            return
        if not d["moved"]:
            if abs(event.x_root - d["x"]) + abs(event.y_root - d["y"]) < DRAG_SLOP:
                return
            d["moved"] = True
            panel.header.configure(cursor="fleur")
        if panel.floating:
            f = panel.frame
            f.tk.call("wm", "geometry", f._w, f"+{event.x_root - d['dx']}+{event.y_root - d['dy']}")
        zone = self.zone_at(event.x_root, event.y_root)
        if zone == panel.dock and not panel.floating:
            zone = None         # already there: nothing to preview
        self._show_zone(zone)

    def drag_end(self, panel, event):
        d, panel._drag = panel._drag, None
        self._hide_zone()
        try:
            panel.header.configure(cursor="")
        except tk.TclError:
            return
        if not d or not d["moved"]:
            return
        zone = self.zone_at(event.x_root, event.y_root)
        if zone:
            if panel.floating or zone != panel.dock:
                self.dock_panel(panel, zone)
            return
        if not panel.floating and not self._inside(self.host.winfo_toplevel(), event.x_root, event.y_root):
            # Dropped outside the main window: tear it off right there.
            self.float_panel(panel, x=event.x_root - min(d["dx"], 120), y=event.y_root - 30)

    # -- persistence -----------------------------------------------------------------

    def load(self, cfg):
        """Reads each panel's visibility (ui section) and layout (layout
        section) out of the config. Anything missing or garbled falls back to
        the panel's default; the pre-docking ui.explorer_width /
        ui.console_height are honoured once, as the docked size."""
        ui = cfg.get("ui") if isinstance(cfg.get("ui"), dict) else {}
        layout = cfg.get("layout") if isinstance(cfg.get("layout"), dict) else {}
        legacy = {"explorer": (ui.get("explorer_width"), None), "terminal": (None, ui.get("console_height"))}
        for panel in self.ordered():
            spec = PANEL_SPECS[panel.key]
            d = layout.get(panel.key) if isinstance(layout.get(panel.key), dict) else {}
            vis = ui.get(spec["vis_key"], True)
            panel.visible = vis if isinstance(vis, bool) else True
            panel.dock = d.get("dock") if d.get("dock") in SIDES else spec["dock"]
            panel.floating = d.get("floating") is True
            lw, lh = legacy.get(panel.key, (None, None))
            w = _pos_int(d.get("w")) or _pos_int(lw) or spec["size"][0]
            h = _pos_int(d.get("h")) or _pos_int(lh) or spec["size"][1]
            panel.size = (w, h)
            panel.float_size = d["float_size"] if parse_size(d.get("float_size")) else None
            panel.float_pos = d["float_pos"] if parse_pos(d.get("float_pos")) else None

    def save(self, cfg):
        """Writes visibility and layout into the config. Each of the three
        remember-options in Settings > General > Window (layout = which side
        / floating, size, floating position) is independent; one that is off
        has its keys deleted rather than left stale."""
        ui = cfg.setdefault("ui", {})
        layout = cfg.get("layout")
        if not isinstance(layout, dict):
            layout = cfg["layout"] = {}
        keep_layout = ui.get("save_panel_layout", True)
        keep_size = ui.get("save_panel_size", True)
        keep_pos = ui.get("save_panel_position", True)
        for panel in self.ordered():
            self._capture(panel)
            ui[panel.vis_key] = panel.visible
            entry = {}
            if keep_layout:
                entry["dock"] = panel.dock
                entry["floating"] = panel.floating
            if keep_size:
                entry["w"], entry["h"] = panel.size
                if panel.float_size:
                    entry["float_size"] = panel.float_size
            if keep_pos and panel.float_pos:
                entry["float_pos"] = panel.float_pos
            if entry:
                layout[panel.key] = entry
            else:
                layout.pop(panel.key, None)
        ui.pop("explorer_width", None)
        ui.pop("console_height", None)

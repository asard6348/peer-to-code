"""Optional OS-level drag-and-drop support.

Plain Tkinter has no built-in way to receive files dragged in from a file
manager - that needs the external tkdnd Tcl extension, which the
tkinterdnd2 package wraps. It isn't guaranteed to be installed, and even
when it is, its bundled native library might not load on every platform
(notably: Termux's Android/bionic environment is a real question mark for
a "generic linux-arm64" prebuilt binary). So every entry point here is
fully guarded: if tkinterdnd2 isn't installed, or fails to initialize for
any reason, the app just runs exactly as it did before - drag-and-drop is
a bonus when available, never a requirement.
"""

import tkinter as tk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    AVAILABLE = True
except Exception:
    DND_FILES = None
    TkinterDnD = None
    AVAILABLE = False


def make_root():
    """Returns a Tk root capable of receiving drag-and-drop, if possible,
    or a plain tk.Tk() otherwise."""
    if AVAILABLE:
        try:
            return TkinterDnD.Tk()
        except Exception:
            pass
    return tk.Tk()


def register_drop(widget, callback, debug=False):
    if not AVAILABLE:
        return

    def log(*a):
        if debug:
            import sys, time
            print(f"[dnd {time.monotonic():.3f}]", *a, file=sys.stderr, flush=True)

    def on_enter(event):
        log("enter", event.widget, event.action)
        return event.action

    def on_position(event):
        log("pos", event.x_root, event.y_root, event.action)
        return event.action

    def on_leave(event):
        log("leave")
        return event.action

    def on_drop(event):
        log("drop", event.widget, repr(event.data), event.action)
        paths = list(widget.tk.splitlist(event.data or ""))
        if paths:
            widget.after_idle(callback, paths)
        return event.action

    try:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<DropEnter>>", on_enter)
        widget.dnd_bind("<<DropPosition>>", on_position)
        widget.dnd_bind("<<Drop>>", on_drop)
        widget.dnd_bind("<<DropLeave>>", on_leave)
    except Exception:
        pass

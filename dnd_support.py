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


def register_drop(widget, callback):
    """Makes `widget` accept dropped files/folders, calling
    callback(list_of_paths) with at least one path when something is
    dropped on it. No-ops quietly if drag-and-drop isn't available here."""
    if not AVAILABLE:
        return
    try:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", lambda event: callback(widget.tk.splitlist(event.data)))
    except Exception:
        pass

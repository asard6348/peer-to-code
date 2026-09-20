import tkinter as tk


def _paste(event):
    widget = event.widget
    try:
        if str(widget.cget("state")) in ("disabled", "readonly"):
            return "break"
        clip = widget.clipboard_get()
    except tk.TclError:
        return "break"
    if not clip:
        return "break"
    try:
        if widget.selection_present():
            start = widget.index("sel.first")
            widget.delete("sel.first", "sel.last")
            widget.icursor(start)
    except tk.TclError:
        pass
    widget.insert("insert", clip)
    widget.tk.call("tk::EntrySeeInsert", widget._w)
    return "break"


def install(root):
    for widget_class in ("Entry", "Spinbox"):
        root.bind_class(widget_class, "<<Paste>>", _paste)

import tkinter as tk


def char_offset(widget, index):
    """Absolute character offset of a Tk text index from the start of the buffer."""
    result = widget.count("1.0", index, "chars")
    return result[0] if result else 0


_char_offset = char_offset


def _safe_call(widget, orig_name, cmd, args):
    """Raise a plain TclError, which Tcl-side `catch` wrappers expect."""
    try:
        return widget.tk.call((orig_name, cmd) + args)
    except tk.TclError:
        return ""


def install_proxy(widget: tk.Text, on_insert, on_delete):
    """Renames the Tcl command backing `widget` and substitutes our own, so we
    see every insert/delete the widget performs - typed, pasted, undone, or
    made programmatically - with exact character offsets."""
    orig_name = widget._w + "_orig"
    widget.tk.call("rename", widget._w, orig_name)

    def command(cmd, *args):
        if cmd in ("insert", "delete") and args:
            try:
                if cmd == "insert":
                    start_idx = widget.index(args[0])
                    offset = _char_offset(widget, start_idx)
                else:
                    start_idx = widget.index(args[0])
                    end_idx = widget.index(args[1]) if len(args) > 1 and args[1] else widget.index(f"{args[0]}+1c")
                    off_start = _char_offset(widget, start_idx)
                    off_end = _char_offset(widget, end_idx)
            except tk.TclError:
                start_idx = offset = off_start = off_end = None

        try:
            result = widget.tk.call((orig_name, cmd) + args)
        except tk.TclError:
            return ""

        if cmd == "insert" and args and offset is not None:
            text = args[1] if len(args) > 1 else ""
            if text:
                on_insert(offset, text)
        elif cmd == "delete" and args and off_start is not None:
            if off_end > off_start:
                on_delete(off_start, off_end)
        return result

    widget.tk.createcommand(widget._w, command)
    return orig_name


def install_delete_guard(widget: tk.Text, boundary_mark):
    """Renames the Tcl command backing `widget` and clamps every delete so nothing at or before `boundary_mark` can ever be removed or overwritten."""
    orig_name = widget._w + "_delguard_orig"
    widget.tk.call("rename", widget._w, orig_name)

    def command(cmd, *args):
        if cmd == "delete" and args:
            try:
                boundary = widget.index(boundary_mark)
                start = widget.index(args[0])
                end = widget.index(args[1]) if len(args) > 1 and args[1] else widget.index(f"{args[0]}+1c")
            except tk.TclError:
                return _safe_call(widget, orig_name, cmd, args)
            if widget.compare(start, "<", boundary):
                start = boundary
            if widget.compare(end, "<", boundary):
                end = boundary
            if not widget.compare(start, "<", end):
                return ""
            args = (start, end)
        elif cmd == "insert" and args:
            try:
                boundary = widget.index(boundary_mark)
                start = widget.index(args[0])
            except tk.TclError:
                return _safe_call(widget, orig_name, cmd, args)
            if widget.compare(start, "<", boundary):
                args = (boundary,) + args[1:]
        return _safe_call(widget, orig_name, cmd, args)

    widget.tk.createcommand(widget._w, command)
    return orig_name

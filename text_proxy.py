import tkinter as tk


def char_offset(widget, index):
    """Absolute character offset of a Tk text index from the start of the buffer."""
    result = widget.count("1.0", index, "chars")
    return result[0] if result else 0


_char_offset = char_offset


def _safe_call(widget, orig_name, cmd, args):
    """The Tcl side (e.g. the Text widget's own built-in Ctrl+C -> Copy
    binding, which runs `$w get sel.first sel.last`) often wraps calls
    like this in its own `catch`, expecting a plain, harmless error back
    when e.g. there's no selection to copy. But since we've replaced the
    widget's real Tcl command with this Python one, an uncaught
    tk.TclError here isn't a Tcl-level error for that surrounding catch
    to see and swallow - it's a Python exception, which propagates
    straight out of mainloop() and prints a traceback instead. Matches
    the same catch install_proxy's command() already does."""
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
    """Renames the Tcl command backing `widget` and clamps every delete
    (and insert) so nothing at or before `boundary_mark` can ever be
    removed or overwritten - however it's triggered. Backspace and
    Delete are easy enough to catch with an ordinary key binding, but
    Tk's Text widget has several other built-in ways to delete text -
    Ctrl+D and Ctrl+H (its emacs-style bindings), Ctrl+X/<<Cut>>, a
    right-click context menu, a script calling .delete() directly - and
    re-discovering and re-blocking each one individually as it turns up
    is a losing game (that's exactly how Ctrl+X and Ctrl+D slipped
    through). Catching it here, at the Tcl command level every one of
    those ultimately funnels through, covers all of them - and anything
    else - at once, the same way install_proxy above observes every
    edit instead of chasing individual key bindings."""
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

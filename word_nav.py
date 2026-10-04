"""Word-wise keyboard editing the way code editors do it.

Out of the box Tk's Text and Entry widgets only half do this: Ctrl+Left/Right
move by Tcl's idea of a word, but Ctrl+Backspace and Ctrl+Delete are treated
as plain Backspace/Delete and remove a single character. install() puts one
set of rules on every Text, Entry and ttk Entry in the app:

  Ctrl+Left / Ctrl+Right              move to the previous word start / next word end
  Ctrl+Shift+Left / Ctrl+Shift+Right  the same, extending the selection
  Ctrl+Backspace / Ctrl+Delete        delete the previous / next word

(Option instead of Ctrl on macOS.) A "word" is a run of letters, digits and
underscores; a run of other punctuation counts as a word of its own, and
whitespace is skipped on the way. Movement never skips over a line break: at
a line's edge it first steps onto the neighbouring line.
"""

import sys
import tkinter as tk

_MODS = ("Option",) if sys.platform == "darwin" else ("Control",)


def _kind(ch):
    if ch.isspace():
        return 0
    if ch.isalnum() or ch == "_":
        return 1
    return 2


def word_left(line, col):
    """Column the previous word starts at (0 when only whitespace is before
    `col`). Pure function of one line of text."""
    i = min(col, len(line))
    while i > 0 and _kind(line[i - 1]) == 0:
        i -= 1
    if i == 0:
        return 0
    kind = _kind(line[i - 1])
    while i > 0 and _kind(line[i - 1]) == kind:
        i -= 1
    return i


def word_right(line, col):
    """Column the next word ends at (the end of the line when only
    whitespace follows `col`)."""
    n = len(line)
    i = max(col, 0)
    while i < n and _kind(line[i]) == 0:
        i += 1
    if i >= n:
        return n
    kind = _kind(line[i])
    while i < n and _kind(line[i]) == kind:
        i += 1
    return i


# -- Text -------------------------------------------------------------------

def _text_target(w, forward):
    """Index to move to / delete up to from the caret, or None at the very
    start/end of the buffer."""
    line, col = (int(p) for p in w.index("insert").split("."))
    s = w.get(f"{line}.0", f"{line}.end")
    if forward:
        if col >= len(s):
            return None if w.compare("insert", ">=", "end-1c") else f"{line + 1}.0"
        return f"{line}.{word_right(s, col)}"
    if col == 0:
        return None if line == 1 else f"{line - 1}.end"
    return f"{line}.{word_left(s, col)}"


def _text_move(forward, select):
    def handler(event):
        w = event.widget
        target = _text_target(w, forward)
        if target is not None:
            w.tk.call("tk::TextKeySelect" if select else "tk::TextSetCursor", w._w, target)
            w.see("insert")
        return "break"
    return handler


def _text_delete(forward):
    def handler(event):
        w = event.widget
        if str(w.cget("state")) == "disabled":
            return "break"
        if w.tag_ranges("sel"):
            w.delete("sel.first", "sel.last")
            return "break"
        target = _text_target(w, forward)
        if target is not None:
            if forward:
                w.delete("insert", target)
            else:
                w.delete(target, "insert")
            w.see("insert")
        return "break"
    return handler


# -- Entry / ttk Entry --------------------------------------------------------

def _entry_target(w, forward):
    s = w.get()
    col = w.index("insert")
    return word_right(s, col) if forward else word_left(s, col)


def _entry_move(forward, select):
    def handler(event):
        w = event.widget
        target = _entry_target(w, forward)
        if select and w.winfo_class() == "Entry":
            w.tk.call("tk::EntryKeySelect", w._w, target)
        elif select:
            # ttk entries have no key-select helper: grow the selection from
            # whichever end of it the caret isn't at (or from the caret).
            old = w.index("insert")
            if w.selection_present():
                first, last = w.index("sel.first"), w.index("sel.last")
                anchor = last if old == first else first
            else:
                anchor = old
            w.icursor(target)
            w.selection_range(min(anchor, target), max(anchor, target))
        else:
            w.selection_clear()
            w.icursor(target)
        w.xview("insert")
        return "break"
    return handler


def _entry_delete(forward):
    def handler(event):
        w = event.widget
        if str(w.cget("state")) in ("disabled", "readonly"):
            return "break"
        if w.selection_present():
            w.delete("sel.first", "sel.last")
            return "break"
        col = w.index("insert")
        target = _entry_target(w, forward)
        if forward:
            w.delete(col, target)
        else:
            w.delete(target, col)
        return "break"
    return handler


def install(root):
    """Binds the word-wise keys on the Text, Entry and TEntry classes."""
    for mod in _MODS:
        root.bind_class("Text", f"<{mod}-BackSpace>", _text_delete(False))
        root.bind_class("Text", f"<{mod}-Delete>", _text_delete(True))
        for cls in ("Entry", "TEntry"):
            root.bind_class(cls, f"<{mod}-BackSpace>", _entry_delete(False))
            root.bind_class(cls, f"<{mod}-Delete>", _entry_delete(True))
    # Movement goes through Tk's virtual events (Ctrl+arrows on Windows/X11,
    # Option+arrows on macOS), replacing what Tk binds to them.
    for cls, moves in (("Text", _text_move), ("Entry", _entry_move), ("TEntry", _entry_move)):
        root.bind_class(cls, "<<PrevWord>>", moves(False, False))
        root.bind_class(cls, "<<NextWord>>", moves(True, False))
        root.bind_class(cls, "<<SelectPrevWord>>", moves(False, True))
        root.bind_class(cls, "<<SelectNextWord>>", moves(True, True))

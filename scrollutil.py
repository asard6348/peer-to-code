"""Mousewheel support for scrollable canvases.

The previous approach used canvas.bind_all("<MouseWheel>", ...) on <Enter>
and unbound it on <Leave>, scoping the wheel to "while hovering this
canvas". That's a common recipe, but it has a sharp edge: if the widget is
destroyed (e.g. the whole screen it's on gets torn down when the app moves
on) while the pointer happens to be over it - which is exactly what
usually just happened, since the pointer is right there after clicking a
button inside that same area - <Leave> never fires, and the bind_all
registration is left dangling on the root window, referencing a now-dead
widget. The next mousewheel event anywhere in the app then tries to invoke
that stale callback and blows up with obscure Tcl teardown errors
(deletecommand / "can't delete Tcl command").

bind_wheel() avoids the whole class of bug by never touching bind_all: it
binds directly on the canvas and, recursively, on every current descendant
widget, so Tk cleans each binding up automatically as its own widget is
destroyed - there's no global state to leak. This only needs to run once
after a scrollable area's contents are fully built (nothing here is added
dynamically after the fact).
"""


def bind_wheel(canvas, root_widget=None):
    """Makes `canvas` (and everything currently inside it) scroll on
    mousewheel/touchpad input, without registering anything globally."""
    def on_wheel(event):
        step = -1 if event.num == 4 else 1 if event.num == 5 else -event.delta // 120
        canvas.yview_scroll(step, "units")
        return "break"

    def bind_one(widget):
        widget.bind("<MouseWheel>", on_wheel)
        widget.bind("<Button-4>", on_wheel)
        widget.bind("<Button-5>", on_wheel)
        for child in widget.winfo_children():
            bind_one(child)

    bind_one(canvas)
    if root_widget is not None and root_widget is not canvas:
        bind_one(root_widget)

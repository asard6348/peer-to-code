import os
import platform
import shutil
import string
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, simpledialog, messagebox, filedialog

# Sort keys the tree can be ordered by, and their display labels for the
# "Sort" menu. Order here is also the order the menu lists them in.
SORT_OPTIONS = (
    ("name", "Name"),
    ("type", "Type"),
    ("size", "Size"),
    ("modified", "Date Modified"),
)


def _system_explorer_label():
    """Menu label for \"show this in the OS's file manager\", worded the way
    that platform's own users know it."""
    if sys.platform == "darwin":
        return "Reveal in Finder"
    if platform.system() == "Windows":
        return "Show in File Explorer"
    return "Show in File Manager"


def _spawn_detached(args):
    """Starts a file-manager process and returns without waiting for it or
    its output (file managers are long-lived, and some - Windows' explorer
    - report failure through their exit code even when they worked)."""
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, close_fds=True)


def _reveal_in_system_file_manager(path):
    """Opens `path` in the platform's file manager: a folder is opened, a
    file is shown selected inside its folder when the file manager can do
    that, and otherwise its folder is just opened."""
    path = os.path.abspath(path)
    is_dir = os.path.isdir(path)
    folder = path if is_dir else os.path.dirname(path)

    if platform.system() == "Windows":
        if is_dir:
            os.startfile(folder)
        else:
            # "/select," and the path have to be one argument, or explorer
            # ignores the selection and opens the default folder.
            _spawn_detached(["explorer", f"/select,{os.path.normpath(path)}"])
        return

    if sys.platform == "darwin":
        _spawn_detached(["open", folder] if is_dir else ["open", "-R", path])
        return

    # Linux and other Unix desktops.
    if not is_dir:
        # The freedesktop FileManager1 service is implemented by Dolphin, Nautilus, Nemo, Thunar, ...
        dbus_send = shutil.which("dbus-send")
        if dbus_send:
            try:
                result = subprocess.run(
                    [dbus_send, "--session", "--print-reply", "--type=method_call",
                     "--dest=org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                     "org.freedesktop.FileManager1.ShowItems",
                     f"array:string:file://{_quote_path(path)}", "string:"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3)
                if result.returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                pass
        for exe, args in (("dolphin", ["--select", path]), ("nautilus", ["--select", path]),
                           ("nemo", [path])):
            if shutil.which(exe):
                _spawn_detached([exe] + args)
                return
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if opener is None:
        raise OSError("no file manager found (xdg-open is not installed)")
    _spawn_detached([opener, folder] if os.path.basename(opener) != "gio" else [opener, "open", folder])


def _quote_path(path):
    """`path` as the path part of a file:// URI (percent-encoded)."""
    from urllib.parse import quote
    return quote(path)


def _filesystem_roots():
    """The top-level entries to show when no specific folder was opened -
    "the whole computer" rather than any one project. On Windows that's
    the drive letters; everywhere else it's "/", plus - specifically
    useful on Android/Termux, where the real "/" is a sandboxed app
    environment and the interesting content lives elsewhere - the shared
    device storage area, when it actually exists."""
    if platform.system() == "Windows":
        roots = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                roots.append(drive)
        return roots or ["C:\\"]
    roots = ["/"]
    is_termux = "com.termux" in os.environ.get("PREFIX", "")
    if is_termux:
        seen_real = {os.path.realpath(r) for r in roots}
        for extra in ("/storage/emulated/0", "/sdcard"):
            if not os.path.isdir(extra):
                continue
            real = os.path.realpath(extra)
            if real in seen_real:
                continue
            seen_real.add(real)
            roots.append(extra)
    return roots


class FileExplorer(ttk.Frame):
    def __init__(self, master, working_dir, on_open_file, on_root_change=None, **kw):
        # Gives the panel a visible themed edge instead of blending straight into the editor pane beside it.
        kw.setdefault("style", "Explorer.TFrame")
        super().__init__(master, **kw)
        self.working_dir = working_dir
        self.on_open_file = on_open_file
        # Called with the new root folder whenever the tree re-roots itself.
        self.on_root_change = on_root_change
        self._whole_computer = False
        # Path of the item the context menu was last opened on (None when
        # it was opened on empty space).
        self._menu_path = None
        self._item_entries_shown = False
        # The ".." row at the top of the tree.
        self._up_node = None
        self._last_up_click = 0
        # How the tree's entries are ordered.
        self._sort_key = "name"
        self._sort_reverse = False

        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Button(header, text="Refresh", width=8, style="Toolbar.TButton",
                   command=self.refresh).pack(side="right", padx=2)
        # Collapse All sits at the left edge, Refresh at the right.
        ttk.Button(header, text="Collapse All", style="Toolbar.Thin.TButton",
                   command=self.collapse_all).pack(side="left", padx=2)
        ttk.Frame(header).pack(side="left", fill="x", expand=True, pady=4)

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(tree_frame, show="tree")
        self.tree.column("#0", stretch=False)
        tree_yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_yscroll.pack(side="right", fill="y")
        tree_xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        tree_xscroll.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.configure(yscrollcommand=tree_yscroll.set, xscrollcommand=tree_xscroll.set)
        self.tree.bind("<Configure>", lambda _e: self._update_tree_width())

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<<TreeviewOpen>>", self._on_expand)

        # Drag-and-drop reordering within the tree.
        self._drag_start_node = None
        self._drag_start_pos = (0, 0)
        self._drag_active = False
        self._drag_hover_node = None
        self.tree.bind("<ButtonPress-1>", self._on_drag_start, add="+")
        self.tree.bind("<B1-Motion>", self._on_drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_drag_release, add="+")
        self.tree.tag_configure("drop_target", background=self._drop_target_color())

        self.menu = tk.Menu(self, tearoff=0)
        # "Open" / "Open in Explorer" / the system file manager entry only exist while the menu is open on an actual item.
        self.menu.add_command(label="New File", command=self._new_file)
        self.menu.add_command(label="New Folder", command=self._new_folder)
        self.menu.add_separator()
        self.menu.add_command(label="Rename", command=self._rename)
        self.menu.add_command(label="Move to...", command=self._move)
        self.menu.add_separator()
        self.menu.add_command(label="Delete", command=self._delete)
        self.menu.add_separator()
        # Sorting lives in the right-click menu, not the header.
        self._sort_key_var = tk.StringVar(value=self._sort_key)
        self._sort_reverse_var = tk.BooleanVar(value=self._sort_reverse)
        self.sort_menu = tk.Menu(self.menu, tearoff=0)
        for key, label in SORT_OPTIONS:
            self.sort_menu.add_radiobutton(label=label, value=key, variable=self._sort_key_var,
                                            command=lambda k=key: self._set_sort_key(k))
        self.sort_menu.add_separator()
        self.sort_menu.add_checkbutton(label="Descending", variable=self._sort_reverse_var,
                                        command=self._set_sort_direction)
        self.menu.add_cascade(label="Sort by", menu=self.sort_menu)

        self._node_paths = {}
        self._show_initial_view()

    def _drop_target_color(self):
        """Background used to highlight the folder a drag is currently hovering over."""
        color = ttk.Style(self.tree).lookup("Treeview", "background", ("selected",))
        return color or "#4a9eff"

    def _tree_font(self):
        name = ttk.Style(self.tree).lookup("Treeview", "font") or "TkDefaultFont"
        try:
            return tkfont.nametofont(name)
        except tk.TclError:
            return tkfont.nametofont("TkDefaultFont")

    def set_font(self, family, size):
        """Applies an Explorer-specific font size (see EditorApp's
        Ctrl+scroll/pinch zoom over the tree, and Settings > Theme >
        Explorer) via the shared ttk "Treeview" style - there's no
        per-widget font option on a Treeview the way a plain Tk widget
        has. Grows each row to match, so larger text doesn't get
        clipped against the row above/below it, and re-measures the
        tree's own column width against the new font so long names
        still aren't clipped either."""
        style = ttk.Style(self.tree)
        style.configure("Treeview", font=(family, size), rowheight=max(size + 10, 20))
        style.configure("Treeview.Heading", font=(family, size))
        self._update_tree_width()

    def current_font(self):
        """The tree's font as it stands right now."""
        font = self._tree_font()
        try:
            family = font.cget("family")
        except tk.TclError:
            family = "TkDefaultFont"
        try:
            size = abs(int(font.cget("size")))
        except (tk.TclError, ValueError):
            size = 9
        return family, size

    def _tree_indent(self):
        indent = ttk.Style(self.tree).lookup("Treeview", "indent")
        try:
            return int(indent)
        except (TypeError, ValueError):
            return 20

    def _update_tree_width(self):
        """See the comment on tree.column above."""
        font = self._tree_font()
        indent = self._tree_indent()
        allowance = indent + 12
        widest = 0

        def walk(node, depth):
            nonlocal widest
            text = self.tree.item(node, "text")
            widest = max(widest, depth * indent + allowance + font.measure(text))
            for child in self.tree.get_children(node):
                walk(child, depth + 1)

        for root in self.tree.get_children(""):
            walk(root, 0)

        visible_width = self.tree.winfo_width()
        self.tree.column("#0", width=max(widest, visible_width, 1))

    def set_working_dir(self, path):
        self.working_dir = path
        self._whole_computer = False
        self._show_initial_view()

    def show_whole_computer(self):
        """Used when no specific folder was opened: rather than rooting the tree at that file's own parent folder."""
        self._whole_computer = True
        self._rebuild(open_roots=False)
        self._update_tree_width()

    def _root_specs(self):
        """List of (path, display_label) pairs to seed the tree with."""
        if self._whole_computer:
            return [(r, r) for r in _filesystem_roots()]
        return [(self.working_dir, os.path.basename(self.working_dir) or self.working_dir)]

    def _show_initial_view(self):
        self._rebuild(open_roots=not self._whole_computer)
        self._update_tree_width()

    def refresh(self):
        expanded_paths = self._collect_expanded_paths()
        self._rebuild(open_roots=False)
        for root_node in self.tree.get_children(""):
            path = self._node_paths.get(root_node)
            if path:
                self._reexpand(root_node, path, expanded_paths)
        self._update_tree_width()

    def collapse_all(self):
        def close(node):
            self.tree.item(node, open=False)
            for child in self.tree.get_children(node):
                close(child)

        for root_node in self.tree.get_children(""):
            close(root_node)
        self.tree.selection_set(())
        self.tree.focus("")
        self.tree.yview_moveto(0)
        self.tree.xview_moveto(0)
        self._update_tree_width()

    def _rebuild(self, open_roots):
        self.tree.delete(*self.tree.get_children())
        self._node_paths = {}
        for path, label in self._root_specs():
            node = self.tree.insert("", "end", text=label, open=open_roots)
            self._node_paths[node] = path
            self._populate(node, path)
        self._refresh_up_row()

    def _parent_root(self):
        """The folder \"..\" would re-root the tree at, or None when
        there's nowhere higher to go (whole-computer view, or already at
        the top of the filesystem / a drive root)."""
        if self._whole_computer or not self.working_dir:
            return None
        current = os.path.normpath(self.working_dir)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return None
        return parent

    def _refresh_up_row(self):
        """Puts a \"..\" row first in the tree whenever there's a higher
        folder to go to (not in the whole-computer view, nor at the top
        of the filesystem / a drive root)."""
        self._up_node = None
        if self._parent_root():
            self._up_node = self.tree.insert("", 0, text="..")

    def _activate_up_row(self):
        # A double-click would otherwise register twice.
        now = time.monotonic()
        if now - self._last_up_click < 0.4:
            return
        self._last_up_click = now
        self.go_up()

    def _set_root(self, path, reveal=None):
        """Re-roots the tree at folder `path`, keeping every folder that was expanded still expanded."""
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            return
        expanded = {os.path.normpath(p) for p in self._collect_expanded_paths()}
        self.working_dir = path
        self._whole_computer = False
        self._rebuild(open_roots=True)
        for root_node in self.tree.get_children(""):
            root_path = self._node_paths.get(root_node)
            if root_path:
                self._reexpand(root_node, root_path, expanded)
        self._update_tree_width()
        self.tree.xview_moveto(0)
        self.tree.yview_moveto(0)
        if reveal:
            self._reveal(reveal)
        if self.on_root_change:
            self.on_root_change(path)

    def _reveal(self, path):
        for node, node_path in self._node_paths.items():
            if node_path == path:
                self.tree.selection_set(node)
                self.tree.focus(node)
                self.tree.see(node)
                return

    def go_up(self):
        parent = self._parent_root()
        if parent:
            self._set_root(parent, reveal=os.path.normpath(self.working_dir))

    def _collect_expanded_paths(self):
        expanded = set()

        def walk(node):
            path = self._node_paths.get(node)
            if path and self.tree.item(node, "open"):
                expanded.add(path)
            for child in self.tree.get_children(node):
                walk(child)

        for node in self.tree.get_children(""):
            walk(node)
        return expanded

    def _reexpand(self, node, path, expanded_paths):
        prefix = path.rstrip(os.sep) + os.sep
        if not any(p == path or p.startswith(prefix) for p in expanded_paths):
            return
        if path in expanded_paths:
            self.tree.item(node, open=True)
        self._load_children(node, path)
        for child in self.tree.get_children(node):
            child_path = self._node_paths.get(child)
            if child_path:
                self._reexpand(child, child_path, expanded_paths)

    def _load_children(self, node, path):
        children = self.tree.get_children(node)
        if len(children) == 1 and self.tree.item(children[0], "text") == "":
            self.tree.delete(children[0])
            self._populate(node, path)

    def _secondary_key(self, full, name):
        """The ordering value for one entry under the current sort key, before directories are pulled to the front (see _populate)."""
        key = self._sort_key
        if key == "size":
            try:
                return os.path.getsize(full)
            except OSError:
                return 0
        if key == "modified":
            try:
                return os.path.getmtime(full)
            except OSError:
                return 0
        if key == "type":
            return (os.path.splitext(name)[1].lower(), name.lower())
        return name.lower()

    def _populate(self, node, path):
        try:
            names = [n for n in os.listdir(path) if not n.startswith(".")]
        except OSError:
            return
        entries = [(n, os.path.join(path, n)) for n in names]
        # Two sorts in sequence rather than one combined key.
        entries.sort(key=lambda e: self._secondary_key(e[1], e[0]), reverse=self._sort_reverse)
        entries.sort(key=lambda e: not os.path.isdir(e[1]))
        for name, full in entries:
            child = self.tree.insert(node, "end", text=name, open=False)
            self._node_paths[child] = full
            if os.path.isdir(full):
                self.tree.insert(child, "end", text="")

    def _set_sort_key(self, key):
        self._sort_key = key
        self.refresh()

    def _set_sort_direction(self):
        self._sort_reverse = self._sort_reverse_var.get()
        self.refresh()

    def set_sort(self, key, reverse=False):
        """Programmatic equivalent of picking an option from the tree's
        own right-click "Sort by" submenu - used to apply a sort key/
        direction coming from Settings > General (or a saved config on
        startup) instead of a click, while keeping the submenu's
        radiobutton/checkbutton selection in sync either way."""
        if key not in dict(SORT_OPTIONS):
            key = "name"
        self._sort_key = key
        self._sort_reverse = bool(reverse)
        self._sort_key_var.set(self._sort_key)
        self._sort_reverse_var.set(self._sort_reverse)
        self.refresh()

    def _on_expand(self, _event):
        node = self.tree.focus()
        path = self._node_paths.get(node)
        if not path or not os.path.isdir(path):
            return
        self._load_children(node, path)
        self._update_tree_width()

    def _on_double_click(self, _event):
        node = self.tree.focus()
        path = self._node_paths.get(node)
        if path and os.path.isfile(path):
            self.on_open_file(path)

    def _on_right_click(self, event):
        node = self.tree.identify_row(event.y)
        if node:
            self.tree.selection_set(node)
            self.tree.focus(node)
        self._menu_path = self._node_paths.get(node) if node else None
        self._set_item_entries(bool(self._menu_path))
        # Offset so the pointer starts outside the menu (see dock.py).
        self.menu.tk_popup(event.x_root + 4, event.y_root + 4)

    def _set_item_entries(self, show):
        """Adds or removes the item-specific entries at the top of the menu."""
        if show == self._item_entries_shown:
            return
        if show:
            self.menu.insert_command(0, label="Open", command=self._open_item)
            self.menu.insert_command(1, label="Open in Explorer", command=self._open_in_explorer)
            self.menu.insert_command(2, label=_system_explorer_label(), command=self._open_in_system_explorer)
            self.menu.insert_separator(3)
        else:
            self.menu.delete(0, 3)
        self._item_entries_shown = show

    def _open_item(self):
        """Context-menu \"Open\": a file opens in the editor, a folder
        toggles expanded/collapsed (the same thing a double-click does
        for each in most file trees)."""
        path = self._menu_path
        if not path:
            return
        if os.path.isfile(path):
            self.on_open_file(path)
            return
        if not os.path.isdir(path):
            return
        node = next((n for n, p in self._node_paths.items() if p == path), None)
        if node is None:
            return
        if self.tree.item(node, "open"):
            self.tree.item(node, open=False)
        else:
            self._load_children(node, path)
            self.tree.item(node, open=True)
        self._update_tree_width()

    def _open_in_explorer(self):
        """Context-menu \"Open in Explorer\": makes the right-clicked
        folder the tree's top-level folder. For a file, that's the folder
        containing it, with the file selected."""
        path = self._menu_path
        if not path:
            return
        if os.path.isdir(path):
            self._set_root(path)
        elif os.path.isfile(path):
            self._set_root(os.path.dirname(path), reveal=path)

    def _open_in_system_explorer(self):
        """Context-menu entry below \"Open in Explorer\": shows the
        right-clicked item in the operating system's own file manager
        (Windows Explorer, Finder, Dolphin, Nautilus, ...). A folder is
        opened; a file is revealed (selected) inside its folder where the
        file manager supports that."""
        path = self._menu_path
        if not path or not os.path.exists(path):
            return
        try:
            _reveal_in_system_file_manager(path)
        except (OSError, subprocess.SubprocessError) as e:
            messagebox.showerror(_system_explorer_label(), f"Couldn't open the file manager:\n{e}")

    def _target_dir(self):
        node = self.tree.focus()
        path = self._node_paths.get(node, self.working_dir)
        return path if os.path.isdir(path) else os.path.dirname(path)

    def _new_file(self):
        name = simpledialog.askstring("New File", "File name:")
        if not name:
            return
        full = os.path.join(self._target_dir(), name)
        try:
            open(full, "x").close()
        except OSError as e:
            messagebox.showerror("New File", str(e))
        self.refresh()

    def _new_folder(self):
        name = simpledialog.askstring("New Folder", "Folder name:")
        if not name:
            return
        try:
            os.makedirs(os.path.join(self._target_dir(), name))
        except OSError as e:
            messagebox.showerror("New Folder", str(e))
        self.refresh()

    def _rename(self):
        node = self.tree.focus()
        path = self._node_paths.get(node)
        if not path:
            return
        new_name = simpledialog.askstring("Rename", "New name:", initialvalue=os.path.basename(path))
        if not new_name:
            return
        try:
            os.rename(path, os.path.join(os.path.dirname(path), new_name))
        except OSError as e:
            messagebox.showerror("Rename", str(e))
        self.refresh()

    def _delete(self):
        node = self.tree.focus()
        path = self._node_paths.get(node)
        if not path:
            return
        is_dir = os.path.isdir(path)
        prompt = (f"Delete '{os.path.basename(path)}' and everything inside it? This cannot be undone."
                  if is_dir else
                  f"Delete '{os.path.basename(path)}'? This cannot be undone.")
        if not messagebox.askyesno("Delete", prompt):
            return
        try:
            if is_dir:
                # os.rmdir only succeeds on an already-empty directory.
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as e:
            messagebox.showerror("Delete", str(e))
        self.refresh()

    def _move(self):
        node = self.tree.focus()
        path = self._node_paths.get(node)
        if not path:
            return
        initial = os.path.dirname(path.rstrip(os.sep)) or self.working_dir
        dest_dir = filedialog.askdirectory(title="Move to", initialdir=initial, mustexist=True)
        if not dest_dir:
            return
        self._perform_move(path, dest_dir)

    def _perform_move(self, path, dest_dir):
        """Moves `path` into folder `dest_dir`, used by both the "Move
        to..." dialog and drag-and-drop within the tree. Guards against
        the couple of ways this can go wrong that os.rename/shutil.move
        wouldn't otherwise explain clearly: dropping something onto the
        folder it's already in, dragging a folder into its own
        descendant, and a same-named entry already sitting at the
        destination."""
        if not dest_dir or not os.path.isdir(dest_dir):
            return
        src_dir = os.path.dirname(path.rstrip(os.sep)) or self.working_dir
        if os.path.realpath(dest_dir) == os.path.realpath(src_dir):
            return
        real_path = os.path.realpath(path)
        real_dest_dir = os.path.realpath(dest_dir)
        if real_dest_dir == real_path or real_dest_dir.startswith(real_path + os.sep):
            messagebox.showerror("Move", "Can't move a folder into itself.")
            return
        dest = os.path.join(dest_dir, os.path.basename(path))
        if os.path.exists(dest):
            messagebox.showerror("Move", f"'{os.path.basename(path)}' already exists in that location.")
            return
        try:
            shutil.move(path, dest)
        except (OSError, shutil.Error) as e:
            messagebox.showerror("Move", str(e))
        self.refresh()

    def _on_drag_start(self, event):
        self._drag_start_node = self.tree.identify_row(event.y)
        self._drag_start_pos = (event.x, event.y)
        self._drag_active = False

    def _on_drag_motion(self, event):
        if not self._drag_start_node:
            return
        if not self._drag_active:
            sx, sy = self._drag_start_pos
            if abs(event.x - sx) < 4 and abs(event.y - sy) < 4:
                return
            self._drag_active = True
            self.tree.configure(cursor="hand2")

        target = self.tree.identify_row(event.y)
        if target == self._drag_hover_node:
            return
        if self._drag_hover_node:
            self.tree.item(self._drag_hover_node, tags=())
            self._drag_hover_node = None
        target_path = self._node_paths.get(target, "")
        if target and target != self._drag_start_node and os.path.isdir(target_path):
            self.tree.item(target, tags=("drop_target",))
            self._drag_hover_node = target

    def _on_drag_release(self, event):
        start = self._drag_start_node
        hover = self._drag_hover_node
        active = self._drag_active
        if hover:
            self.tree.item(hover, tags=())
        self.tree.configure(cursor="")
        self._drag_start_node = None
        self._drag_hover_node = None
        self._drag_active = False
        if (not active and start and start == self._up_node
                and self.tree.identify_row(event.y) == start):
            self._activate_up_row()
            return
        if active and start and hover and hover != start:
            path = self._node_paths.get(start)
            dest_dir = self._node_paths.get(hover)
            if path and dest_dir:
                self._perform_move(path, dest_dir)

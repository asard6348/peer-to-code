import os
import platform
import string
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, simpledialog, messagebox


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
    def __init__(self, master, working_dir, on_open_file, **kw):
        super().__init__(master, **kw)
        self.working_dir = working_dir
        self.on_open_file = on_open_file
        self._whole_computer = False

        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Button(header, text="Refresh", width=8, style="Toolbar.TButton",
                   command=self.refresh).pack(side="right", padx=2)
        ttk.Button(header, text="Collapse All", style="Toolbar.Thin.TButton",
                   command=self.collapse_all).pack(side="right", padx=2)
        ttk.Label(header, text="EXPLORER", font=("Segoe UI", 9, "bold")).pack(
            side="left", fill="x", expand=True, padx=6, pady=4)

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

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="New File", command=self._new_file)
        self.menu.add_command(label="New Folder", command=self._new_folder)
        self.menu.add_command(label="Rename", command=self._rename)
        self.menu.add_command(label="Delete", command=self._delete)

        self._node_paths = {}
        self._show_initial_view()

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
        """The tree's font as it stands right now - whatever the active
        ttk theme's own "Treeview" style default is, if set_font() above
        has never been called to override it. Used as the starting point
        for zoom/Settings so a person who's never touched Explorer's font
        controls keeps seeing exactly the size and family their ttk theme
        already gave them, rather than this app silently substituting
        some hardcoded size the first time it starts up."""
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
        """See the comment on tree.column("#0", ...) above - sizes the
        tree column to whichever currently-realized row is widest (its
        text plus its indent level), floored at the panel's own visible
        width so short content still fills the panel instead of leaving
        a sliver of unused space on the right."""
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
        """Used when no specific folder was opened (a single-file Open):
        rather than rooting the tree at that file's own parent folder -
        which isn't really "the project", just wherever the file happened
        to be - shows the whole local filesystem instead, the same way
        Explorer starting hidden in that case avoids implying a folder was
        opened when it wasn't. Starts collapsed: there's no single natural
        "project root" here the way there is for one opened folder, and
        several roots (each of which can be enormous, like "/") all
        auto-expanded at once isn't useful."""
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

    def _populate(self, node, path):
        try:
            entries = sorted(os.listdir(path), key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower()))
        except OSError:
            return
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            child = self.tree.insert(node, "end", text=name, open=False)
            self._node_paths[child] = full
            if os.path.isdir(full):
                self.tree.insert(child, "end", text="")

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
        self.menu.tk_popup(event.x_root, event.y_root)

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
        if not messagebox.askyesno("Delete", f"Delete '{os.path.basename(path)}'? This cannot be undone."):
            return
        try:
            if os.path.isdir(path):
                os.rmdir(path)
            else:
                os.remove(path)
        except OSError as e:
            messagebox.showerror("Delete", str(e))
        self.refresh()

"""Read-only window showing the run output other people chose to share.

One tab per runner, labelled with the runner's name, kept apart from the
viewer's own Terminal on purpose: it is never mixed with (or typed into)
their own console. The Text widgets are disabled (no editing, no stdin of
any kind), and text only ever gets in through Text.insert with whitelisted
colour tags - there is no terminal emulation (see share_util.render_sgr).
"""

import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

from chat_util import adapt_color
import syntax

MAX_LINES = 5000

# Standard 16 ANSI colours, tuned for a dark background; adapt_color darkens
# them when the console background is light.
ANSI_COLORS = ["#6e7681", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#abb2bf",
               "#8b949e", "#ff7b86", "#b5e890", "#ffd98a", "#82c4ff", "#e3a0f7", "#7fdbe6", "#ffffff"]

NOTICE = ("Output shared by people in this session. Read-only: nothing you type here goes anywhere, "
          "and it is never mixed into your own Terminal. Paths and files it mentions belong to their "
          "machine and may not exist on yours.")


class SharedOutputWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.winfo_toplevel())
        self.app = app
        t = app.theme
        self.title("Shared output (read-only)")
        self.geometry("720x420")
        self.configure(bg=t["panel_bg"])
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self._light = syntax.brightness(t["console_bg"]) >= 0.5
        note = tk.Label(self, text=NOTICE, bg=t["panel_bg"], fg=t["muted_fg"], justify="left", anchor="w")
        note.pack(fill="x", padx=8, pady=(6, 2))
        note.bind("<Configure>", lambda e: note.configure(wraplength=max(100, e.width - 4)))
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)
        self._tabs = {}          # sender id -> {"frame", "header", "text", "name"}
        self._tag_cache = set()

    # -- tabs ---------------------------------------------------------------

    def _tab(self, sender, name):
        tab = self._tabs.get(sender)
        t = self.app.theme
        if tab is not None:
            if tab["name"] != name:
                tab["name"] = name
                self.nb.tab(tab["frame"], text=name)
            return tab
        frame = tk.Frame(self.nb, bg=t["panel_bg"])
        header = tk.Label(frame, text="", bg=t["panel_bg"], fg=t["fg"], anchor="w")
        header.pack(fill="x", padx=4, pady=(4, 2))
        body = tk.Frame(frame, bg=t["console_bg"])
        body.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical")
        scroll.pack(side="right", fill="y")
        text = tk.Text(body, bg=t["console_bg"], fg=t["fg"], relief="flat", wrap="word", state="disabled",
                       font=(self.app._console_font_family, self.app._console_font_size),
                       yscrollcommand=scroll.set, cursor="arrow")
        text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=text.yview)
        from editor import _console_tag_colors
        colors = _console_tag_colors(t["console_bg"])
        text.tag_configure("stderr", foreground=colors["stderr"])
        text.tag_configure("note", foreground=colors["info"])
        self.nb.add(frame, text=name)
        tab = {"frame": frame, "header": header, "text": text, "name": name, "base": tkfont.Font(font=text.cget("font"))}
        self._tabs[sender] = tab
        return tab

    def _write(self, tab, pieces):
        text = tab["text"]
        at_bottom = text.yview()[1] >= 0.999
        text.configure(state="normal")
        for chunk, tags in pieces:
            text.insert("end", chunk, tags)
        lines = int(text.index("end-1c").split(".")[0])
        if lines > MAX_LINES:
            text.delete("1.0", f"{lines - MAX_LINES}.0")
        text.configure(state="disabled")
        if at_bottom:
            text.see("end")

    def _style_tag(self, tab, fg, bold):
        name = f"sgr_{fg}_{int(bold)}"
        if name not in self._tag_cache or name not in tab["text"].tag_names():
            self._tag_cache.add(name)
        if name not in tab["text"].tag_names():
            font = tkfont.Font(font=tab["base"])
            if bold:
                font.configure(weight="bold")
            tab.setdefault("fonts", []).append(font)   # keep a reference alive
            opts = {"font": font}
            if fg is not None:
                opts["foreground"] = adapt_color(ANSI_COLORS[fg], self._light) or ANSI_COLORS[fg]
            tab["text"].tag_configure(name, **opts)
        return name

    # -- API used by EditorApp ---------------------------------------------

    def start_run(self, sender, name, title, status="running"):
        tab = self._tab(sender, name)
        tab["header"].configure(text=f"{name}  -  {title}  [{status}]")
        if tab["text"].get("1.0", "end-1c"):
            self._write(tab, [(f"\n---- new run: {title} ----\n", ("note",))])
        return tab

    def append(self, sender, name, runs, stream):
        tab = self._tab(sender, name)
        pieces = []
        for chunk, (fg, bold) in runs:
            tags = []
            if fg is not None or bold:
                tags.append(self._style_tag(tab, fg, bold))
            elif stream == "e":
                tags.append("stderr")
            pieces.append((chunk, tuple(tags)))
        self._write(tab, pieces)

    def note(self, sender, name, message):
        tab = self._tab(sender, name)
        text = tab["text"]
        prefix = "" if text.get("end-2c", "end-1c") in ("\n", "") else "\n"
        self._write(tab, [(f"{prefix}{message}\n", ("note",))])

    def set_status(self, sender, name, title, status):
        tab = self._tab(sender, name)
        tab["header"].configure(text=f"{name}  -  {title}  [{status}]")

    def text_of(self, sender):
        tab = self._tabs.get(sender)
        return tab["text"].get("1.0", "end-1c") if tab else ""

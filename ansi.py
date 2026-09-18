"""Minimal ANSI escape-code rendering for the Terminal console.

This isn't a full terminal emulator (no cursor positioning, no alternate
screen, no line-overwrite for progress bars) — it exists so that a
subprocess printing colored output (pytest, ruff, black, rich/click apps,
etc.) shows up as actual colors and styles in the Terminal pane instead of
raw "\\x1b[32m...\\x1b[0m" escape-sequence noise. It supports:

  - SGR color/style codes: bold, dim, italic, underline, strikethrough,
    reverse-video, the 16 standard/bright colors, the xterm 256-color
    palette (38/48;5;N), and 24-bit truecolor (38/48;2;R;G;B)
  - Every other escape sequence (cursor movement, screen/line clearing,
    OSC window-title-setting, etc.) is recognized and dropped rather than
    printed as literal junk characters

AnsiConsole.write() is safe to call repeatedly with successive chunks of
subprocess output; SGR state persists across calls the same way it would
in a real terminal (a "bold" turned on in one line stays on until turned
off), and a chunk that ends mid-escape-sequence is held back and completed
by the next write() rather than being split and rendered incorrectly.
"""

import re
import tkinter.font as tkfont

_SEQ_RE = re.compile(
    r"\x1b\[([0-9;]*)([A-Za-z])"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)

_BASE16 = [
    "#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
    "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#f5f5f5",
]


def _xterm256(n):
    if n < 0 or n > 255:
        return None
    if n < 16:
        return _BASE16[n]
    if n < 232:
        n -= 16
        levels = (0, 95, 135, 175, 215, 255)
        r, g, b = levels[n // 36], levels[(n // 6) % 6], levels[n % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    gray = 8 + (n - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


class _AnsiState:
    """Current SGR state — persists across write() calls, like a real
    terminal's graphics state does."""

    __slots__ = ("fg", "bg", "bold", "dim", "italic", "underline", "strike", "reverse")

    def __init__(self):
        self.reset()

    def reset(self):
        self.fg = None
        self.bg = None
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False
        self.strike = False
        self.reverse = False

    def key(self):
        return (self.fg, self.bg, self.bold, self.dim, self.italic,
                self.underline, self.strike, self.reverse)

    def apply_sgr(self, params):
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self.reset()
            elif p == 1:
                self.bold = True
            elif p == 2:
                self.dim = True
            elif p == 3:
                self.italic = True
            elif p == 4:
                self.underline = True
            elif p == 7:
                self.reverse = True
            elif p == 9:
                self.strike = True
            elif p == 21 or p == 22:
                self.bold = self.dim = False
            elif p == 23:
                self.italic = False
            elif p == 24:
                self.underline = False
            elif p == 27:
                self.reverse = False
            elif p == 29:
                self.strike = False
            elif 30 <= p <= 37:
                self.fg = _BASE16[p - 30]
            elif p == 38:
                if i + 2 < len(params) and params[i + 1] == 5:
                    self.fg = _xterm256(params[i + 2]) or self.fg
                    i += 2
                elif i + 4 < len(params) and params[i + 1] == 2:
                    r, g, b = params[i + 2], params[i + 3], params[i + 4]
                    self.fg = f"#{r & 0xff:02x}{g & 0xff:02x}{b & 0xff:02x}"
                    i += 4
            elif p == 39:
                self.fg = None
            elif 40 <= p <= 47:
                self.bg = _BASE16[p - 40]
            elif p == 48:
                if i + 2 < len(params) and params[i + 1] == 5:
                    self.bg = _xterm256(params[i + 2]) or self.bg
                    i += 2
                elif i + 4 < len(params) and params[i + 1] == 2:
                    r, g, b = params[i + 2], params[i + 3], params[i + 4]
                    self.bg = f"#{r & 0xff:02x}{g & 0xff:02x}{b & 0xff:02x}"
                    i += 4
            elif p == 49:
                self.bg = None
            elif 90 <= p <= 97:
                self.fg = _BASE16[8 + (p - 90)]
            elif 100 <= p <= 107:
                self.bg = _BASE16[8 + (p - 100)]
            i += 1


class AnsiConsole:
    """Feeds ANSI-colored subprocess output into a Tk Text widget, creating
    and caching one tag per distinct style combination it encounters.

    base_tag colors (e.g. "stderr" -> red) are only used for stretches of
    text with no explicit ANSI color of their own, so plain stderr output
    still looks the way it always has, while colored tool output (pytest,
    ruff, rich/click apps, ...) shows its own real colors.
    """

    def __init__(self, text_widget, base_tag_colors=None):
        self.text = text_widget
        self.base_tag_colors = base_tag_colors or {}
        self.state = _AnsiState()
        self._tag_cache = {}
        self._font_cache = {}
        self._pending = ""
        self._tag_counter = 0

    def rescale_fonts(self, family=None, size=None):
        """Called whenever the console's own base font changes (zoom, or
        Settings > Theme > Terminal) - see _tag_for below: essentially
        every span of text ever written here, not just bold/italic/
        colored ones, is tagged with its own explicit Font object frozen
        at whatever size and family were active the moment that text was
        first written. Just reconfiguring the Text widget's base font
        left all of that old output on screen at its old size, while
        the *line geometry* (built from the widget's base font) resized
        around it - which is what showed up as the cursor and scrollbar
        changing size while the actual text only seemed to shift. Each
        cached Font object is mutated in place here instead of replaced,
        so every tag referencing it - past output included - re-renders
        at the new size immediately, since a tag's font is a live
        reference to the Font object, not a snapshot of it."""
        if not self._font_cache:
            return
        rescaled = {}
        for (old_family, old_size, weight, slant), face in self._font_cache.items():
            new_family = family if family is not None else old_family
            new_size = size if size is not None else old_size
            face.configure(family=new_family, size=new_size)
            rescaled[(new_family, new_size, weight, slant)] = face
        self._font_cache = rescaled

    def _tag_for(self, state, base_tag):
        key = (state.key(), base_tag)
        tag = self._tag_cache.get(key)
        if tag:
            return tag
        self._tag_counter += 1
        tag = f"ansi_{self._tag_counter}"

        fg, bg = state.fg, state.bg
        if state.reverse:
            fg, bg = bg, fg
        if fg is None:
            fg = self.base_tag_colors.get(base_tag)
        if fg is None and state.dim:
            fg = "#8b949e"

        try:
            base = tkfont.Font(font=self.text.cget("font"))
            weight = "bold" if state.bold else "normal"
            slant = "italic" if state.italic else "roman"
            font_key = (base.actual("family"), base.actual("size"), weight, slant)
            face = self._font_cache.get(font_key)
            if face is None:
                face = tkfont.Font(family=font_key[0], size=font_key[1], weight=weight, slant=slant)
                self._font_cache[font_key] = face
        except Exception:
            face = None

        kwargs = {"underline": state.underline, "overstrike": state.strike}
        if face is not None:
            kwargs["font"] = face
        if fg:
            kwargs["foreground"] = fg
        if bg:
            kwargs["background"] = bg
        self.text.tag_configure(tag, **kwargs)
        self._tag_cache[key] = tag
        return tag

    def write(self, raw_text, base_tag=None):
        """Parses and inserts one chunk of subprocess output, carrying SGR
        state and any not-yet-complete escape sequence over to the next
        call. Insertion happens at the Text widget's current "end"."""
        text = self._pending + raw_text
        self._pending = ""
        pos = 0
        for m in _SEQ_RE.finditer(text):
            if m.start() > pos:
                self._insert(text[pos:m.start()], base_tag)
            params_str, final = m.group(1), m.group(2)
            if final == "m":
                params = [int(x) for x in params_str.split(";") if x] if params_str else [0]
                self.state.apply_sgr(params)
            pos = m.end()
        tail = text[pos:]
        esc_idx = tail.find("\x1b")
        if esc_idx == -1:
            self._insert(tail, base_tag)
        else:
            self._insert(tail[:esc_idx], base_tag)
            self._pending = tail[esc_idx:]

    def flush(self, base_tag=None):
        """Dumps any leftover partial escape sequence as literal text.
        Call this once a subprocess's stream has closed, so a truncated
        escape sequence at end-of-output doesn't just vanish."""
        if self._pending:
            self._insert(self._pending, base_tag)
            self._pending = ""

    def _insert(self, segment, base_tag):
        if not segment:
            return
        tag = self._tag_for(self.state, base_tag)
        self.text.insert("end", segment, tag)

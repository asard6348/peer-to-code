"""Pure helpers for the Terminal chat feature (no Tk, no sockets), shared by
net/server.py, net/client.py and editor.py so that every layer applies the
*same* rules and so they can be unit-tested on their own.

  - sanitize_text / sanitize_name: what a peer is allowed to put in front of
    another user's console. Anything that could be interpreted by a terminal
    emulator (ESC and the rest of the C0/C1 control range) or could fake the
    layout of a chat line (newlines, bidi overrides, "->", brackets) is
    removed or flattened.
  - parse_chat_command: the `m ...` / `/m ...` grammar.
  - chat_settings: coerces whatever is in the config's "chat" section into
    sane types, so a hand-edited or old config can never break the UI.
  - completion_context / find_code_refs: Tab-completion and :LINE links.
"""

import re
import unicodedata
from collections import namedtuple

CHAT_MAX_LEN = 2000
NAME_MAX_LEN = 32
DEFAULT_TRIGGER = "m"

# Shown in place of a line break when a message is flattened to one line.
NEWLINE_MARK = " \u23ce "

# Characters that, inside a *name*, would let someone fake the shape of a
# chat label ("[bob -> you]"), break `-p a,b` parsing, or break quoting.
_NAME_FORBIDDEN = str.maketrans({c: "_" for c in '[]<>,"'})

_LINE_BREAKS = {"\n", "\r", "\u2028", "\u2029", "\x0b", "\x0c", "\x85"}


def _strip_controls(text):
    out = []
    for ch in text:
        if ch in _LINE_BREAKS:
            out.append("\n")
        elif ch == "\t":
            out.append(" ")
        else:
            cat = unicodedata.category(ch)
            # Cc: C0/C1 controls (ESC, BEL, CR, DEL, 0x9b CSI, ...).
            # Cf: invisible format characters - bidi overrides can reorder
            #     a label on screen, zero-width characters can hide text.
            # Cs/Co/Cn: surrogates, private use, unassigned.
            if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
                continue
            out.append(ch)
    return "".join(out)


def sanitize_text(text, max_len=CHAT_MAX_LEN):
    """One safe line of text: control characters removed, line breaks
    flattened to a visible marker, surrounding whitespace trimmed, length
    capped. Never raises; non-strings are str()'d."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = _strip_controls(text)
    text = re.sub(r"[ ]*\n[\n ]*", NEWLINE_MARK, text.strip("\n"))
    return text.strip()[:max_len]


def sanitize_name(name, max_len=NAME_MAX_LEN, default="anon"):
    """A safe display name: no controls, no newlines, no label-spoofing
    characters, single spaces, no leading '#' (reserved for `#<id>`
    targeting), at most max_len characters. Falls back to `default`."""
    if not isinstance(name, str):
        name = "" if name is None else str(name)
    name = _strip_controls(name).replace("\n", " ").translate(_NAME_FORBIDDEN)
    name = re.sub(r"\s+", " ", name).strip().lstrip("#").strip()
    return name[:max_len].rstrip() or default


def unique_name(name, taken, max_len=NAME_MAX_LEN):
    """`name`, or `name#2`, `name#3`, ... - the first not in `taken`
    (compared case-insensitively). The base is trimmed so the result still
    fits in max_len."""
    taken = {t.lower() for t in taken}
    if name.lower() not in taken:
        return name
    n = 2
    while True:
        suffix = f"#{n}"
        candidate = name[:max_len - len(suffix)].rstrip() + suffix
        if candidate.lower() not in taken:
            return candidate
        n += 1


# ---------------------------------------------------------------- settings

CHAT_DEFAULTS = {
    "enabled": True,
    "trigger": DEFAULT_TRIGGER,
    "show_timestamps": False,
    "notify": True,
    "notify_bell": False,
    "do_not_disturb": False,
    "muted": [],
    "show_join_leave": True,
    "color_names": True,
    "view_shared": True,
}

_TRIGGER_RE = re.compile(r"^[A-Za-z0-9_!?.:;@%^&*+=~]{1,16}$")


def normalize_trigger(value):
    """The trigger word if it's usable, else None. It must be a single
    shell-ish word that can't be mistaken for an option (`-`), the
    running-process prefix (`/`) or the shell escape (`\\`)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _TRIGGER_RE.match(value) else None


def chat_settings(cfg):
    """The "chat" section of `cfg` coerced to the types the UI relies on
    (always a complete dict; unknown/garbled values fall back to
    defaults). Pure read - never mutates cfg."""
    raw = cfg.get("chat") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    for key, default in CHAT_DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(default, bool):
            out[key] = value if isinstance(value, bool) else default
        elif key == "trigger":
            out[key] = normalize_trigger(value) or default
        elif key == "muted":
            out[key] = ([sanitize_name(v) for v in value if isinstance(v, str) and v.strip()]
                        if isinstance(value, list) else [])
    return out


# ----------------------------------------------------------- command parse

ChatCommand = namedtuple("ChatCommand", "kind to reply text")
ChatCommand.__doc__ = """kind: "send" | "list" | "mute" | "unmute" | "shell".
to: tuple of recipient tokens (names or "#id"); empty = everyone.
reply: True for `m -r` (send to the last private sender).
text: the message (send), the user (mute/unmute) or the shell command
(shell, i.e. the line with the `\\m` escape removed)."""


def chat_usage(trigger=DEFAULT_TRIGGER, running=False):
    t = ("/" + trigger) if running else trigger
    return (f"usage: {t} [-p <user[,user...]>] [--] <message>  |  {t} -r <message>  |  {t} -l  |  "
            f"{t} -m <user>  |  {t} -u <user>   (quote names with spaces; #<id> picks by id)")


def _read_user_list(s):
    """Reads `a`, `"Jo Smith"`, `a,#3,'x y'` off the front of s. Returns
    (items, rest) or None if malformed."""
    items = []
    i = 0
    n = len(s)
    while True:
        if i >= n:
            return None
        if s[i] in "\"'":
            q = s[i]
            end = s.find(q, i + 1)
            if end == -1:
                return None
            item = s[i + 1:end]
            i = end + 1
        else:
            j = i
            while j < n and not s[j].isspace() and s[j] != ",":
                j += 1
            item = s[i:j]
            i = j
        item = item.strip()
        if not item:
            return None
        items.append(item)
        if i < n and s[i] == ",":
            i += 1
            continue
        break
    if i < n and not s[i].isspace():
        return None
    return items, s[i:].lstrip()


def _drop_dashdash(text):
    """`-- text` -> `text` (the optional end-of-options marker)."""
    if text == "--":
        return ""
    if text.startswith("--") and text[2].isspace():
        return text[2:].lstrip()
    return text


def parse_chat_command(command, trigger=DEFAULT_TRIGGER, running=False):
    """Parses one Terminal line.

    Idle prompt:   `m [-p <users>] [--] <message>`, `m -r <message>`, `m -l`,
                   `m -m <user>`, `m -u <user>`; `\\m ...` is a shell command
                   that really is named m.
    While running: only `/m ...` (same grammar) is chat; anything else
                   belongs to the process's stdin.

    Returns a ChatCommand, None when the line isn't chat at all, or raises
    ValueError(usage) for a malformed chat command (`m` alone, `-p`
    without a user or message, ...)."""
    usage = chat_usage(trigger, running)
    if running:
        head = "/" + trigger
        if not (command.startswith(head) and len(command) > len(head) and command[len(head)].isspace()):
            return None
        rest = command[len(head):].strip()
    else:
        if command.startswith("\\" + trigger) and (len(command) == len(trigger) + 1
                                                    or command[len(trigger) + 1].isspace()):
            return ChatCommand("shell", (), False, command[1:])
        if not (command == trigger or (command.startswith(trigger) and command[len(trigger)].isspace())):
            return None
        rest = command[len(trigger):].strip()
    if not rest:
        raise ValueError(usage)

    parts = rest.split(None, 1)
    word = parts[0]
    after = parts[1].strip() if len(parts) > 1 else ""

    if word == "--":
        if not after:
            raise ValueError(usage)
        return ChatCommand("send", (), False, after)
    if word == "-l":
        if after:
            raise ValueError(usage)
        return ChatCommand("list", (), False, "")
    if word in ("-m", "-u"):
        parsed = _read_user_list(after) if after else None
        if not parsed or len(parsed[0]) != 1 or parsed[1]:
            raise ValueError(usage)
        return ChatCommand("mute" if word == "-m" else "unmute", (), False, parsed[0][0])
    if word == "-r":
        after = _drop_dashdash(after)
        if not after:
            raise ValueError(usage)
        return ChatCommand("send", (), True, after)
    if word == "-p":
        parsed = _read_user_list(after) if after else None
        if not parsed:
            raise ValueError(usage)
        users, message = parsed
        message = _drop_dashdash(message)
        if not message:
            raise ValueError(usage)
        return ChatCommand("send", tuple(users), False, message)
    return ChatCommand("send", (), False, rest)


# ------------------------------------------------------------- completion

def completion_context(line, trigger=DEFAULT_TRIGGER):
    """If `line` (the text before the cursor) is `m -p <partial users>`
    with the cursor still inside the user list, returns
    (fixed_prefix, fragment, quote): the text before the fragment being
    completed (and before its opening quote), the fragment itself (without
    the quote) and the opening quote char ('' if none). Otherwise None."""
    m = re.match(rf"^({re.escape(trigger)}\s+-p\s+)(.*)$", line, re.S)
    if not m:
        return None
    head, arg = m.groups()
    seg_start = 0
    quote = ""
    for i, ch in enumerate(arg):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'" and i == seg_start:
            quote = ch
        elif ch == ",":
            seg_start = i + 1
        elif ch.isspace():
            return None
    fragment = arg[seg_start:]
    if fragment[:1] in ("\"", "'"):
        if not quote:  # closed quote followed by end of line: nothing to add
            return None
        fragment = fragment[1:]
    return head + arg[:seg_start], fragment, quote


def format_name_for_command(name, quote="", close=True):
    """A user name as it must be typed after -p."""
    needs = any(c.isspace() for c in name) or quote
    if not needs:
        return name
    q = quote or "\""
    return q + name + (q if close else "")


# --------------------------------------------------------------- code refs

_REF_RE = re.compile(
    r"(?<![\w/:.\-@\\#])"
    r"(?P<file>[A-Za-z0-9_][\w.\-/]*\.[A-Za-z0-9]{1,8})?"
    r":(?P<line>[1-9]\d{0,5})(?::\d{1,4})?(?![\w])"
)


def find_code_refs(text):
    """[(start, end, file_or_None, line_number)] for every `:42` /
    `file.py:42` token in text. The file part must look like a file name
    (have an extension), which keeps times (10:30), URLs (host:8080) and
    prose ("note:42") from turning into links."""
    refs = []
    for m in _REF_RE.finditer(text):
        refs.append((m.start(), m.end(), m.group("file"), int(m.group("line"))))
    return refs


def adapt_color(color, light_background):
    """Peer colors are picked for dark UIs; on a light console background
    they're darkened so the name stays readable."""
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, TypeError, IndexError):
        return None
    if light_background:
        r, g, b = (int(c * 0.62) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"

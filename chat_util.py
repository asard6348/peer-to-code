"""Pure helpers for the Chat panel (no Tk, no sockets), shared by
net/server.py, net/client.py and editor.py so that every layer applies the
*same* rules and so they can be unit-tested on their own.

  - sanitize_text / sanitize_name: what a peer is allowed to put in front of
    another user. Anything that could be interpreted by a terminal emulator
    (ESC and the rest of the C0/C1 control range) or could fake the layout of
    a chat line (newlines, bidi overrides, "->", brackets) is removed or
    flattened.
  - parse_chat_input: the grammar of the Chat panel's input box.
  - chat_settings: coerces whatever is in the config's "chat" section into
    sane types, so a hand-edited or old config can never break the UI.
  - completion_context / find_code_refs: Tab-completion and :LINE links.
"""

import re
import unicodedata
from collections import namedtuple

CHAT_MAX_LEN = 2000
NAME_MAX_LEN = 32

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
    "show_timestamps": False,
    "notify": True,
    "notify_bell": False,
    "do_not_disturb": False,
    "muted": [],
    "show_join_leave": True,
    "color_names": True,
    "view_shared": True,
}


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
        elif key == "muted":
            out[key] = ([sanitize_name(v) for v in value if isinstance(v, str) and v.strip()]
                        if isinstance(value, list) else [])
    return out


# ----------------------------------------------------------- command parse

ChatCommand = namedtuple("ChatCommand", "kind to reply text")
ChatCommand.__doc__ = """kind: "send" | "list" | "mute" | "unmute" | "help".
to: tuple of recipient tokens (names or "#id"); empty = everyone.
reply: True for `/r` (send to the last private sender).
text: the message (send) or the user (mute/unmute)."""

CHAT_HELP = (
    "Type a message and press Enter to send it to everyone.",
    "/p <user[,user...]> <message>   send privately (quote names with spaces; #<id> picks by id)",
    "/r <message>                    reply to the last private message",
    "/who                            list who is here",
    "/mute <user>, /unmute <user>    hide or show someone's messages",
    "//text                          send text that starts with a slash",
)

_COMMAND_ALIASES = {
    "p": "p", "msg": "p", "w": "p",
    "r": "r", "reply": "r",
    "who": "who", "l": "who", "list": "who",
    "mute": "mute", "unmute": "unmute",
    "help": "help", "?": "help",
}
_SLASH_WORD_RE = re.compile(r"^/([A-Za-z?]+)(?:\s+(.*))?$", re.S)


def chat_usage(command=None):
    """One-line usage for a malformed command."""
    return {
        "p": "usage: /p <user[,user...]> [--] <message>   (quote names with spaces; #<id> picks by id)",
        "r": "usage: /r <message>",
        "who": "usage: /who",
        "mute": "usage: /mute <user>",
        "unmute": "usage: /unmute <user>",
    }.get(command, "type /help for the chat commands")


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


def parse_chat_input(line):
    """Parses one line typed into the Chat panel's input box.

    Plain text is a message to everyone. A line starting with `/` and a
    command word is a command:

        /p <users> [--] <message>   private message (also /msg, /w)
        /r <message>                reply to the last private sender
        /who                        who is here (also /l, /list)
        /mute <user>, /unmute <user>
        /help                       (also /?)
        //text                      the literal message `/text`

    A slash line that isn't a command word at all - `/usr/bin/env`, `/ 2` -
    is sent as the message it looks like.

    Returns a ChatCommand, None for an empty line, or raises
    ValueError(usage) for a malformed or unknown command."""
    line = (line or "").strip()
    if not line:
        return None
    if line.startswith("//"):
        return ChatCommand("send", (), False, line[1:])
    m = _SLASH_WORD_RE.match(line)
    if not m:
        return ChatCommand("send", (), False, line)
    word, after = m.group(1).lower(), (m.group(2) or "").strip()
    cmd = _COMMAND_ALIASES.get(word)
    if cmd is None:
        raise ValueError(f"unknown command /{word} - {chat_usage()}")
    if cmd == "help":
        return ChatCommand("help", (), False, "")
    if cmd == "who":
        if after:
            raise ValueError(chat_usage("who"))
        return ChatCommand("list", (), False, "")
    if cmd in ("mute", "unmute"):
        parsed = _read_user_list(after) if after else None
        if not parsed or len(parsed[0]) != 1 or parsed[1]:
            raise ValueError(chat_usage(cmd))
        return ChatCommand(cmd, (), False, parsed[0][0])
    if cmd == "r":
        after = _drop_dashdash(after)
        if not after:
            raise ValueError(chat_usage("r"))
        return ChatCommand("send", (), True, after)
    parsed = _read_user_list(after) if after else None      # cmd == "p"
    if not parsed:
        raise ValueError(chat_usage("p"))
    users, message = parsed
    message = _drop_dashdash(message)
    if not message:
        raise ValueError(chat_usage("p"))
    return ChatCommand("send", tuple(users), False, message)


# ------------------------------------------------------------- completion

def completion_context(line):
    """If `line` (the text before the cursor) is `/p <partial users>` with
    the cursor still inside the user list, returns (fixed_prefix, fragment,
    quote): the text before the fragment being completed (and before its
    opening quote), the fragment itself (without the quote) and the opening
    quote char ('' if none). Otherwise None."""
    m = re.match(r"^(/(?:p|msg|w)\s+)(.*)$", line, re.S | re.I)
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
    """A user name as it must be typed after /p."""
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

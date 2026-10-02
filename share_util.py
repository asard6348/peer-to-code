"""Pure helpers (no Tk, no sockets) for opt-in shared run output.

What is shared, and what is not:
  * Only what the runner's process wrote to stdout/stderr, in the order the
    runner saw it. Never stdin, never environment variables, never any
    input from a viewer. Nothing flows from a viewer to the runner at all.
  * The run's title is the program/script name only - not its arguments
    (command lines often carry tokens).

Wire format (see net/transport.py RUN_START / RUN_OUTPUT / RUN_END): every
chunk of a run has a per-run sequence number starting at 1, and RUN_END
carries the next number, so a viewer can put reliable-but-unordered
datagrams back in order and knows when the run is complete.

Escape handling: output is untrusted text. sanitize_output() removes every
terminal control/escape sequence except plain colour codes (SGR, `ESC[..m`),
and render_sgr() turns those into (text, style) pairs for the app's own
renderer, honouring only a fixed whitelist (16 foreground colours, bold,
reset). Nothing is ever handed to a terminal emulator.
"""

import os
import re
import time

SHARE_CHUNK_MAX = 8192                  # chars of output per RUN_OUTPUT message
SHARE_RUN_MAX_BYTES = 256 * 1024        # most output relayed per run; the rest is dropped
SHARE_BUFFER_MAX_BYTES = 64 * 1024      # server-side catch-up buffer per runner
SHARE_TITLE_MAX = 80
SHARE_MAX_SEGS = 64
SHARE_RATE_BURST = 30                   # server token bucket: chunks per runner
SHARE_RATE_PER_SEC = 20.0               # (the 80 ms poll loop sends at most ~12.5/s)
SHARE_PENDING_MAX = 256                 # viewer reorder window before skipping a gap
SHARE_STALL_SECONDS = 3.0

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_DCS_ETC = re.compile(r"\x1b[PX^_][^\x1b]*(?:\x1b\\)?")
_CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
_ESC_OTHER = re.compile(r"\x1b[ -/]*[0-~]?")
_INCOMPLETE_TAIL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*|[PX^_][^\x1b]*)?$")


def sanitize_output(text):
    """Strips everything a terminal could act on, keeping line structure and
    plain SGR colour sequences: OSC/DCS strings, every CSI sequence except
    `m`, stray ESC, C0/C1 controls (BEL, backspace, NUL, 0x9b CSI...), bidi
    and zero-width format characters. CRLF and lone CR become newlines (a
    progress bar redraws as separate lines instead of overwriting - nothing
    here can move a cursor)."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _OSC.sub("", text)
    text = _DCS_ETC.sub("", text)

    def csi(m):
        params, inter, final = m.groups()
        if final == "m" and not inter and re.fullmatch(r"[0-9;]{0,40}", params):
            return m.group(0)
        return ""
    text = _CSI.sub(csi, text)
    # Whatever ESC is still left is not part of a sequence we keep.
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            m = _CSI.match(text, i)
            if m and m.group(3) == "m" and not m.group(2) and re.fullmatch(r"[0-9;]{0,40}", m.group(1)):
                out.append(m.group(0))
                i = m.end()
                continue
            m = _ESC_OTHER.match(text, i)
            i = m.end() if m and m.end() > i else i + 1
            continue
        o = ord(ch)
        if ch in "\n":
            out.append(ch)
        elif ch == "\t":
            out.append("    ")
        elif o < 0x20 or 0x7f <= o <= 0x9f or ch in "\u2028\u2029" or 0x202a <= o <= 0x202e \
                or 0x2066 <= o <= 0x2069 or 0x200b <= o <= 0x200f or o == 0xfeff:
            pass
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def split_incomplete_escape(text):
    """(complete, carry): holds back a trailing, not yet finished escape
    sequence so a sequence split across two chunks is never half-sent."""
    m = _INCOMPLETE_TAIL.search(text)
    if m and len(text) - m.start() <= 64:
        return text[:m.start()], text[m.start():]
    return text, ""


# --------------------------------------------------------------- rendering

def render_sgr(text, state=(None, False)):
    """Splits sanitized text into [(text, (fg, bold))] runs and returns
    (runs, new_state). fg is 0-15 (ANSI 30-37 / 90-97) or None; bold bool.
    Only these codes are honoured: 0 (reset), 1/22 (bold on/off), 30-37,
    90-97, 39 (default fg). Everything else - 256-colour, truecolor,
    backgrounds, italics, blink - is parsed (so its parameters aren't
    misread) and ignored."""
    fg, bold = state
    runs = []
    pos = 0
    for m in re.finditer(r"\x1b\[([0-9;]*)m", text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], (fg, bold)))
        params = [int(p) if p else 0 for p in (m.group(1) or "0").split(";")]
        i = 0
        while i < len(params):
            c = params[i]
            if c == 0:
                fg, bold = None, False
            elif c == 1:
                bold = True
            elif c == 22:
                bold = False
            elif 30 <= c <= 37:
                fg = c - 30
            elif 90 <= c <= 97:
                fg = c - 90 + 8
            elif c == 39:
                fg = None
            elif c in (38, 48):                 # extended colour: skip its arguments
                if i + 1 < len(params) and params[i + 1] == 5:
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2:
                    i += 4
            i += 1
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], (fg, bold)))
    return [r for r in runs if r[0]], (fg, bold)


# ------------------------------------------------------ wire normalization

def _int(value, default=None, lo=0, hi=10 ** 9):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


def normalize_segs(segs):
    """[["o", text], ["e", text], ...] -> sanitized list (max SHARE_MAX_SEGS
    segments, SHARE_CHUNK_MAX chars in total), dropping junk entries."""
    out, budget = [], SHARE_CHUNK_MAX
    if not isinstance(segs, list):
        return out
    for seg in segs[:SHARE_MAX_SEGS]:
        if not (isinstance(seg, (list, tuple)) and len(seg) == 2 and seg[0] in ("o", "e")):
            continue
        text = sanitize_output(seg[1])[:budget]
        if text:
            out.append([seg[0], text])
            budget -= len(text)
        if budget <= 0:
            break
    return out


def segs_bytes(segs):
    return sum(len(t.encode("utf-8", "replace")) for _s, t in segs)


# --------------------------------------------------------- runner-side batch

class ShareStreamer:
    """Turns the runner's poll-loop output into RUN_OUTPUT chunk payloads.

    add([(is_err, char), ...]) is called once per 80 ms poll with whatever
    arrived in that tick (not per character). It merges consecutive
    characters of the same stream into segments - keeping stdout/stderr in
    arrival order - holds back a half-received escape sequence, splits to
    SHARE_CHUNK_MAX, numbers the chunks, and stops after the per-run byte
    cap (emitting one {"truncated": True} chunk). close() returns the
    sequence number for RUN_END."""

    def __init__(self, run, title, max_bytes=SHARE_RUN_MAX_BYTES):
        self.run = run
        self.title = title
        self.seq = 0
        self.sent = 0
        self.max_bytes = max_bytes
        self.truncated = False
        self._carry = {False: "", True: ""}

    def add(self, pairs):
        """-> list of (seq, segs_or_None, truncated_flag) ready to send."""
        if self.truncated or not pairs:
            return []
        segs = []
        for is_err, ch in pairs:
            stream = "e" if is_err else "o"
            if segs and segs[-1][0] == stream:
                segs[-1][1].append(ch)
            else:
                segs.append([stream, [ch]])
        merged = []
        for stream, chars in segs:
            text = self._carry[stream == "e"] + "".join(chars)
            complete, self._carry[stream == "e"] = split_incomplete_escape(text)
            complete = sanitize_output(complete)
            if complete:
                if merged and merged[-1][0] == stream:
                    merged[-1][1] += complete
                else:
                    merged.append([stream, complete])
        out = []
        chunk, size = [], 0
        for stream, text in merged:
            while text:
                room = SHARE_CHUNK_MAX - size
                piece, text = text[:room], text[room:]
                if chunk and chunk[-1][0] == stream:
                    chunk[-1][1] += piece
                else:
                    chunk.append([stream, piece])
                size += len(piece)
                if size >= SHARE_CHUNK_MAX:
                    out.append(chunk)
                    chunk, size = [], 0
        if chunk:
            out.append(chunk)
        result = []
        for chunk in out:
            nbytes = segs_bytes(chunk)
            if self.sent + nbytes > self.max_bytes:
                self.seq += 1
                self.truncated = True
                result.append((self.seq, None, True))
                break
            self.sent += nbytes
            self.seq += 1
            result.append((self.seq, chunk, False))
        return result

    def end_seq(self):
        return self.seq + 1


# ------------------------------------------------------- viewer-side reorder

class RunAssembler:
    """Puts one run's chunks back in order. feed(seq, item) returns the items
    now ready, in sequence, with duplicates dropped; items arriving early
    wait. If the sender's cap truncated the run, later chunks are discarded
    and the END item is released immediately. release_stalled() lets the
    caller skip a gap that never fills (a dead sender) instead of waiting
    forever."""

    def __init__(self, from_seq=1):
        self.next_seq = from_seq
        self.pending = {}
        self.first_pending_at = None
        self.truncated = False
        self.ended = False
        self.sgr_state = (None, False)

    def feed(self, seq, item):
        if self.ended or seq < self.next_seq or seq in self.pending:
            return []
        if self.truncated and not item.get("end"):
            return []
        self.pending[seq] = item
        if len(self.pending) > SHARE_PENDING_MAX:
            return self.release_stalled(force=True)
        return self._drain()

    def _drain(self):
        ready = []
        while self.next_seq in self.pending:
            item = self.pending.pop(self.next_seq)
            self.next_seq += 1
            ready.append(item)
            if item.get("truncated"):
                self.truncated = True
                # data after the cap was never relayed; only END can still come
                for s in [s for s, it in self.pending.items() if not it.get("end")]:
                    del self.pending[s]
            if item.get("end"):
                self.ended = True
                break
        if self.truncated and not self.ended:
            end = next((s for s, it in self.pending.items() if it.get("end")), None)
            if end is not None:
                ready.append(self.pending.pop(end))
                self.ended = True
        if not self.pending:
            self.first_pending_at = None
        elif self.first_pending_at is None:
            self.first_pending_at = time.monotonic()
        return ready

    def release_stalled(self, force=False):
        if not self.pending:
            return []
        if not force and (self.first_pending_at is None
                          or time.monotonic() - self.first_pending_at < SHARE_STALL_SECONDS):
            return []
        self.next_seq = min(self.pending)
        self.first_pending_at = None
        return [{"gap": True}] + self._drain()


# ------------------------------------------------------- terminal commands

def parse_share_command(line, running=False):
    """`share on|off|view` or bare `share` (status) at the idle prompt; the
    same with a leading `/` while a process runs. Returns the action
    ("on", "off", "view", "status"), None if the line isn't a share command,
    or raises ValueError(usage) for `share <something else>`.
    `\\share` is left alone (it runs a program named share)."""
    head = "/share" if running else "share"
    stripped = line.strip()
    if stripped == head:
        return "status"
    if not stripped.startswith(head + " "):
        return None
    arg = stripped[len(head):].strip().lower()
    if arg in ("on", "off", "view"):
        return arg
    raise ValueError(f"usage: {head} on | {head} off | {head} view | {head}")


SHARE_WARNING = ("Output you share can contain secrets (tokens, passwords, personal paths). "
                 "Everyone in the session will see the output of your runs while sharing is on; "
                 "they never see your input or environment, and cannot send you anything.")


def share_title(args, shell):
    """The program/script name for a run - never its arguments."""
    if isinstance(args, (list, tuple)):
        # The program, plus a script name if one follows - never options or
        # further arguments (they are where tokens and passwords live).
        names = [os.path.basename(str(args[0]))] if args and args[0] else []
        if len(args) > 1 and args[1] and not str(args[1]).startswith("-"):
            names.append(os.path.basename(str(args[1])))
        title = " ".join(names)
    else:
        parts = str(args).split()
        while parts and re.match(r"^[A-Za-z_]\w*=", parts[0]):   # `TOKEN=abc prog`: never share the assignment
            parts.pop(0)
        title = os.path.basename(parts[0]) if parts else "command"
        if len(parts) > 1:
            title += " ..."
    from chat_util import sanitize_text
    return sanitize_text(title, SHARE_TITLE_MAX) or "command"

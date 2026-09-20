import contextlib
import time

import ot

LOCAL = "local"
REPLACE_WINDOW = 0.3


def invert(op, doc):
    inv = ot.Op()
    idx = 0
    for c in op.ops:
        if isinstance(c, str):
            inv.delete(len(c))
        elif c > 0:
            inv.retain(c)
            idx += c
        else:
            n = -c
            inv.insert(doc[idx:idx + n])
            idx += n
    return inv


def map_position(pos, op):
    old = 0
    new = 0
    for c in op.ops:
        if isinstance(c, str):
            new += len(c)
        elif c > 0:
            if old + c > pos:
                return new + pos - old
            old += c
            new += c
        else:
            n = -c
            if old + n > pos:
                return new
            old += n
    return new + pos - old


def shape_of(op):
    comps = op.ops
    i = 0
    pos = 0
    if comps and isinstance(comps[0], int) and comps[0] > 0:
        pos = comps[0]
        i = 1
    if i >= len(comps):
        return None
    core = comps[i]
    tail = comps[i + 1:]
    if len(tail) > 1 or (tail and not (isinstance(tail[0], int) and tail[0] > 0)):
        return None
    if isinstance(core, str):
        return ("insert", pos, len(core))
    if core < 0:
        return ("delete", pos, -core)
    return None


class _Entry:
    __slots__ = ("op", "time")

    def __init__(self, op, time):
        self.op = op
        self.time = time


class _Stack:
    __slots__ = ("undo", "redo", "kind", "pos", "n", "time", "open", "batch", "batch_entry")

    def __init__(self):
        self.undo = []
        self.redo = []
        self.kind = None
        self.pos = None
        self.n = 0
        self.time = 0.0
        self.open = False
        self.batch = False
        self.batch_entry = None


class UndoHistory:
    def __init__(self, max_local=1000, max_peer=200, group_delay=1.0, clock=time.monotonic):
        self.max_local = max_local
        self.max_peer = max_peer
        self.group_delay = group_delay
        self._clock = clock
        self._stacks = {}
        self.desyncs = 0

    def reset(self):
        self._stacks.clear()

    def _stack(self, author):
        stack = self._stacks.get(author)
        if stack is None:
            stack = self._stacks[author] = _Stack()
        return stack

    def close_unit(self, author=LOCAL):
        stack = self._stacks.get(author)
        if stack is not None:
            stack.open = False

    @contextlib.contextmanager
    def batch(self, author=LOCAL):
        stack = self._stack(author)
        if stack.batch:
            yield
            return
        stack.open = False
        stack.batch = True
        stack.batch_entry = None
        try:
            yield
        finally:
            stack = self._stack(author)
            stack.batch = False
            stack.batch_entry = None
            stack.open = False

    def record(self, author, op, doc_before):
        if op.is_noop():
            return
        try:
            self._transform_others(op, (author,))
            stack = self._stack(author)
            if author == LOCAL:
                stack.redo.clear()
            self._push(stack, author, invert(op, doc_before), shape_of(op))
        except ValueError:
            self._desync()

    def undo(self, doc):
        stack = self._stacks.get(LOCAL)
        if stack is None:
            return None
        entry = self._take(stack.undo, doc)
        if entry is None:
            return None
        op = entry.op
        try:
            stack.redo.append(_Entry(invert(op, doc), self._clock()))
            stack.open = False
            self._transform_others(op, (LOCAL,))
        except ValueError:
            self._desync()
        return op

    def redo(self, doc):
        stack = self._stacks.get(LOCAL)
        if stack is None:
            return None
        entry = self._take(stack.redo, doc)
        if entry is None:
            return None
        op = entry.op
        try:
            self._push(stack, LOCAL, invert(op, doc), None, force_new=True)
            self._transform_others(op, (LOCAL,))
        except ValueError:
            self._desync()
        return op

    def undo_peer(self, author, doc):
        if author == LOCAL:
            return None
        stack = self._stacks.get(author)
        if stack is None:
            return None
        entry = self._take(stack.undo, doc)
        if entry is None:
            return None
        op = entry.op
        try:
            stack.open = False
            self._transform_others(op, (author, LOCAL))
            mine = self._stack(LOCAL)
            mine.redo.clear()
            self._push(mine, LOCAL, invert(op, doc), None, force_new=True)
        except ValueError:
            self._desync()
        return op

    def can_undo(self):
        return self._has_live(LOCAL, "undo")

    def can_redo(self):
        return self._has_live(LOCAL, "redo")

    def peer_authors(self):
        found = []
        for key, stack in self._stacks.items():
            if key == LOCAL:
                continue
            while stack.undo and stack.undo[-1].op.is_noop():
                stack.undo.pop()
            if stack.undo:
                found.append((stack.undo[-1].time, key))
        found.sort(key=lambda item: item[0], reverse=True)
        return [key for _time, key in found]

    def latest_peer(self):
        authors = self.peer_authors()
        return authors[0] if authors else None

    def _has_live(self, author, which):
        stack = self._stacks.get(author)
        if stack is None:
            return False
        entries = stack.undo if which == "undo" else stack.redo
        while entries and entries[-1].op.is_noop():
            entries.pop()
        return bool(entries)

    def _desync(self):
        self.desyncs += 1
        self.reset()

    def _take(self, entries, doc):
        while entries:
            entry = entries.pop()
            if entry.op.is_noop():
                continue
            if entry.op.base_len != len(doc):
                self._desync()
                return None
            return entry
        return None

    def _transform_others(self, op, skip):
        for key, stack in list(self._stacks.items()):
            if key in skip:
                continue
            self._transform_list(stack.undo, op)
            self._transform_list(stack.redo, op)
            if stack.pos is not None:
                stack.pos = map_position(stack.pos, op)

    @staticmethod
    def _transform_list(entries, op):
        if not entries:
            return
        rest = op
        dead = False
        for entry in reversed(entries):
            entry.op, rest = ot.transform(entry.op, rest)
            if entry.op.is_noop():
                dead = True
        if dead:
            entries[:] = [e for e in entries if not e.op.is_noop()]

    def _push(self, stack, author, inv, shape, force_new=False):
        now = self._clock()
        merge = False
        if not force_new:
            if stack.batch:
                merge = stack.batch_entry is not None and bool(stack.undo) and stack.undo[-1] is stack.batch_entry
            elif stack.open and stack.undo and shape is not None and now - stack.time <= self.group_delay:
                kind, pos, n = shape
                if kind == stack.kind:
                    if kind == "insert":
                        merge = pos == stack.pos
                    else:
                        merge = pos == stack.pos or pos + n == stack.pos
                elif kind == "insert" and stack.kind == "delete" and stack.n > 1:
                    merge = pos == stack.pos and now - stack.time <= REPLACE_WINDOW
        if merge:
            top = stack.undo[-1]
            top.op = ot.compose(inv, top.op)
            top.time = now
        else:
            entry = _Entry(inv, now)
            stack.undo.append(entry)
            limit = self.max_local if author == LOCAL else self.max_peer
            if len(stack.undo) > limit:
                del stack.undo[0]
            if stack.batch and not force_new:
                stack.batch_entry = entry
        stack.time = now
        if force_new:
            stack.open = False
            stack.kind = None
            stack.pos = None
            stack.n = 0
            return
        stack.open = not stack.batch
        if shape is None:
            stack.kind = None
            stack.pos = None
            stack.n = 0
        else:
            kind, pos, n = shape
            stack.kind = kind
            stack.n = n
            stack.pos = pos + n if kind == "insert" else pos

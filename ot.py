"""Plaintext operational transformation, modeled after the classic ot.js approach.

An operation is a list of components applied left-to-right against a document:
  int > 0   -> retain that many characters
  int < 0   -> delete that many characters (magnitude)
  str       -> insert that string at the current cursor
"""


class Op:
    __slots__ = ("ops", "base_len", "target_len")

    def __init__(self):
        self.ops = []
        self.base_len = 0
        self.target_len = 0

    def retain(self, n):
        if n == 0:
            return self
        self.base_len += n
        self.target_len += n
        if self.ops and isinstance(self.ops[-1], int) and self.ops[-1] > 0:
            self.ops[-1] += n
        else:
            self.ops.append(n)
        return self

    def insert(self, s):
        if not s:
            return self
        self.target_len += len(s)
        if self.ops and isinstance(self.ops[-1], str):
            self.ops[-1] += s
        elif self.ops and isinstance(self.ops[-1], int) and self.ops[-1] < 0:
            if len(self.ops) > 1 and isinstance(self.ops[-2], str):
                self.ops[-2] += s
            else:
                self.ops.insert(len(self.ops) - 1, s)
        else:
            self.ops.append(s)
        return self

    def delete(self, n):
        if n == 0:
            return self
        if n > 0:
            n = -n
        self.base_len -= n
        if self.ops and isinstance(self.ops[-1], int) and self.ops[-1] < 0:
            self.ops[-1] += n
        else:
            self.ops.append(n)
        return self

    def is_noop(self):
        return len(self.ops) == 0 or (len(self.ops) == 1 and isinstance(self.ops[0], int) and self.ops[0] > 0)

    def apply(self, doc):
        if self.base_len != len(doc):
            raise ValueError(f"op base length {self.base_len} does not match document length {len(doc)}")
        out = []
        idx = 0
        for c in self.ops:
            if isinstance(c, int) and c > 0:
                out.append(doc[idx:idx + c])
                idx += c
            elif isinstance(c, str):
                out.append(c)
            else:
                idx += -c
        return "".join(out)

    def to_json(self):
        return self.ops

    @staticmethod
    def from_json(data):
        o = Op()
        for c in data:
            if isinstance(c, str):
                o.insert(c)
            elif c > 0:
                o.retain(c)
            else:
                o.delete(c)
        return o

    def __repr__(self):
        return f"Op({self.ops!r})"


def _next(ops, i):
    return ops[i] if i < len(ops) else None


def compose(a: Op, b: Op) -> Op:
    if a.target_len != b.base_len:
        raise ValueError("compose: length mismatch")
    result = Op()
    ops1, ops2 = a.ops, b.ops
    i1 = i2 = 0
    op1, op2 = _next(ops1, i1), _next(ops2, i2)
    i1 += 1
    i2 += 1
    while op1 is not None or op2 is not None:
        if isinstance(op1, int) and op1 < 0:
            result.delete(op1)
            op1 = _next(ops1, i1)
            i1 += 1
            continue
        if isinstance(op2, str):
            result.insert(op2)
            op2 = _next(ops2, i2)
            i2 += 1
            continue
        if op1 is None or op2 is None:
            raise ValueError("compose: operations are not composable")
        if isinstance(op1, int) and op1 > 0 and isinstance(op2, int) and op2 > 0:
            if op1 > op2:
                result.retain(op2)
                op1 -= op2
                op2 = _next(ops2, i2)
                i2 += 1
            elif op1 == op2:
                result.retain(op1)
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                result.retain(op1)
                op2 -= op1
                op1 = _next(ops1, i1)
                i1 += 1
        elif isinstance(op1, str) and isinstance(op2, int) and op2 < 0:
            if len(op1) > -op2:
                op1 = op1[-op2:]
                op2 = _next(ops2, i2)
                i2 += 1
            elif len(op1) == -op2:
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                op2 += len(op1)
                op1 = _next(ops1, i1)
                i1 += 1
        elif isinstance(op1, str) and isinstance(op2, int) and op2 > 0:
            if len(op1) > op2:
                result.insert(op1[:op2])
                op1 = op1[op2:]
                op2 = _next(ops2, i2)
                i2 += 1
            elif len(op1) == op2:
                result.insert(op1)
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                result.insert(op1)
                op2 -= len(op1)
                op1 = _next(ops1, i1)
                i1 += 1
        elif isinstance(op1, int) and op1 > 0 and isinstance(op2, int) and op2 < 0:
            if op1 > -op2:
                m = -op2
                op1 += op2
                op2 = _next(ops2, i2)
                i2 += 1
            elif op1 == -op2:
                m = op1
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                m = op1
                op2 += op1
                op1 = _next(ops1, i1)
                i1 += 1
            result.delete(m)
        else:
            raise ValueError(f"compose: incompatible components {op1!r} {op2!r}")
    return result


def transform(a: Op, b: Op):
    """Given two ops built against the same base doc, return (a', b') such that
    apply(apply(doc,a),b') == apply(apply(doc,b),a')."""
    if a.base_len != b.base_len:
        raise ValueError("transform: base length mismatch")
    aprime, bprime = Op(), Op()
    ops1, ops2 = a.ops, b.ops
    i1 = i2 = 0
    op1, op2 = _next(ops1, i1), _next(ops2, i2)
    i1 += 1
    i2 += 1
    while op1 is not None or op2 is not None:
        if isinstance(op1, str):
            aprime.insert(op1)
            bprime.retain(len(op1))
            op1 = _next(ops1, i1)
            i1 += 1
            continue
        if isinstance(op2, str):
            aprime.retain(len(op2))
            bprime.insert(op2)
            op2 = _next(ops2, i2)
            i2 += 1
            continue
        if op1 is None or op2 is None:
            raise ValueError("transform: operations are not the same length")
        if isinstance(op1, int) and op1 > 0 and isinstance(op2, int) and op2 > 0:
            if op1 > op2:
                m = op2
                op1 -= op2
                op2 = _next(ops2, i2)
                i2 += 1
            elif op1 == op2:
                m = op2
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                m = op1
                op2 -= op1
                op1 = _next(ops1, i1)
                i1 += 1
            aprime.retain(m)
            bprime.retain(m)
        elif isinstance(op1, int) and op1 < 0 and isinstance(op2, int) and op2 < 0:
            if -op1 > -op2:
                op1 -= op2
                op2 = _next(ops2, i2)
                i2 += 1
            elif op1 == op2:
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                op2 -= op1
                op1 = _next(ops1, i1)
                i1 += 1
        elif isinstance(op1, int) and op1 < 0 and isinstance(op2, int) and op2 > 0:
            if -op1 > op2:
                m = op2
                op1 += op2
                op2 = _next(ops2, i2)
                i2 += 1
            elif -op1 == op2:
                m = op2
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                m = -op1
                op2 += op1
                op1 = _next(ops1, i1)
                i1 += 1
            aprime.delete(m)
        elif isinstance(op1, int) and op1 > 0 and isinstance(op2, int) and op2 < 0:
            if op1 > -op2:
                m = -op2
                op1 += op2
                op2 = _next(ops2, i2)
                i2 += 1
            elif op1 == -op2:
                m = op1
                op1 = _next(ops1, i1)
                i1 += 1
                op2 = _next(ops2, i2)
                i2 += 1
            else:
                m = op1
                op2 += op1
                op1 = _next(ops1, i1)
                i1 += 1
            bprime.delete(m)
        else:
            raise ValueError(f"transform: incompatible components {op1!r} {op2!r}")
    return aprime, bprime


def diff_to_op(old: str, new: str) -> Op:
    """Build a minimal retain/insert/delete op turning old into new, based on a
    common-prefix/common-suffix diff (cheap, fine for interactive single-user edits)."""
    prefix = 0
    max_prefix = min(len(old), len(new))
    while prefix < max_prefix and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    max_suffix = min(len(old), len(new)) - prefix
    while suffix < max_suffix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    op = Op()
    op.retain(prefix)
    op.delete(len(old) - prefix - suffix)
    op.insert(new[prefix:len(new) - suffix])
    op.retain(suffix)
    return op

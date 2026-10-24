"""Text file loading and saving that keeps line endings, BOMs and existing data safe."""

import codecs
import os
import shutil
import tempfile

_UTF8_BOM = codecs.BOM_UTF8
MAX_BYTES = 32 * 1024 * 1024


def read_text(path):
    """Return (text, eol, bom). Text uses "\\n" only; eol is the file's dominant line ending.

    Raises OSError (also for files over MAX_BYTES) or UnicodeDecodeError."""
    if os.path.getsize(path) > MAX_BYTES:
        raise OSError(f"File is larger than {MAX_BYTES // 2**20} MB")
    with open(path, "rb") as f:
        data = f.read()
    bom = data.startswith(_UTF8_BOM)
    text = data[len(_UTF8_BOM):].decode("utf-8") if bom else data.decode("utf-8")
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    eol = "\r\n" if crlf > max(lf, cr) else "\r" if cr > max(lf, crlf) else "\n"
    if crlf or cr:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, eol, bom


def write_text(path, text, eol="\n", bom=False):
    """Atomically write text; falls back to an in-place write where replacing is not possible.

    Raises OSError, or ValueError if the text cannot be encoded as UTF-8."""
    if eol != "\n":
        text = text.replace("\n", eol)
    data = text.encode("utf-8")
    if bom:
        data = _UTF8_BOM + data
    target = os.path.realpath(path)         # keep symlinks intact
    directory = os.path.dirname(target) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".p2c-", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(target):
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
        tmp = None
        return
    except OSError:
        pass        # read-only directory, file held open on Windows, odd filesystem: write in place
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    with open(target, "wb") as f:
        f.write(data)

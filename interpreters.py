"""Finding, parsing and checking the command used to run a file.

An "interpreter" here is a command line, not a file path: `python3 -u`,
`go run`, `"C:\\Program Files\\nodejs\\node.exe" --trace-warnings`. Whatever
the person types is split into arguments and the file being run is appended
as the last one (see split_command / EditorApp.action_run).
"""
import os
import shlex
import shutil
import sys

_IS_WINDOWS = sys.platform.startswith("win")

# language code (see syntax.EXTENSION_LANGUAGE) -> commands to try, in order.
# Only the first word of each is looked up on PATH. Python is handled
# separately in detect() because it needs the "am I a frozen app" check.
RUNNERS = {
    "bash": ["bash", "sh"],
    "javascript": ["node", "nodejs", "bun"],
    "typescript": ["tsx", "ts-node", "bun"],
    "ruby": ["ruby"],
    "php": ["php"],
    "perl": ["perl"],
    "lua": ["lua", "luajit", "lua5.4", "lua5.3"],
    "r": ["Rscript"],
    "julia": ["julia"],
    "go": ["go run"],
    "groovy": ["groovy"],
    "elixir": ["elixir"],
    "erlang": ["escript"],
    "dart": ["dart run"],
    "swift": ["swift"],
    "java": ["java"],
    "scala": ["scala"],
    "haskell": ["runghc", "runhaskell"],
    "clojure": ["clojure", "clj"],
    "lisp": ["sbcl --script", "clisp"],
    "ocaml": ["ocaml"],
    "tcl": ["tclsh"],
    "powershell": ["pwsh", "powershell"],
    "batch": ["cmd /c"],
    "nim": ["nim r"],
    "zig": ["zig run"],
    "d": ["rdmd"],
    "prolog": ["swipl"],
    "fsharp": ["dotnet fsi"],
    "csharp": ["dotnet-script"],
}


def is_frozen():
    """True in a packaged build (PyInstaller etc.), where sys.executable is
    the app itself rather than a Python interpreter."""
    return bool(getattr(sys, "frozen", False))


def _quote_path(path):
    if not any(ch.isspace() for ch in path):
        return path
    return f'"{path}"' if _IS_WINDOWS else shlex.quote(path)


def split_command(cmd):
    """Turns the interpreter text into an argument list ([] if empty).
    A bare path that happens to contain spaces and exists as a file is
    kept whole, so an unquoted `C:\\Program Files\\...\\python.exe` works."""
    cmd = (cmd or "").strip()
    if not cmd:
        return []
    whole = os.path.expanduser(cmd)
    if os.path.isfile(whole):
        return [whole]
    try:
        if _IS_WINDOWS:
            parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p
                     for p in shlex.split(cmd, posix=False)]
        else:
            parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    if parts:
        parts[0] = os.path.expanduser(parts[0])
    return parts


def _resolve_executable(exe, cwd=None):
    if os.path.sep in exe or (os.path.altsep and os.path.altsep in exe):
        if not os.path.isabs(exe) and cwd:
            exe = os.path.join(cwd, exe)
    return shutil.which(exe)


def _same_file(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def is_self_executable(cmd, cwd=None):
    """True when the command starts the packaged app itself - never a
    usable interpreter (running a script through it just opens another
    copy of the editor)."""
    if not is_frozen():
        return False
    argv = split_command(cmd)
    if not argv:
        return False
    resolved = _resolve_executable(argv[0], cwd) or argv[0]
    return _same_file(resolved, sys.executable)


def is_valid(cmd, cwd=None):
    """True if the command's program exists and is executable."""
    argv = split_command(cmd)
    if not argv:
        return False
    if is_self_executable(cmd, cwd):
        return False
    return _resolve_executable(argv[0], cwd) is not None


def detect(language):
    """Best available runner for `language` as a command string, or "" if
    nothing suitable is installed (or there is no such thing for it)."""
    if language == "python":
        candidates = []
        if not is_frozen() and sys.executable:
            candidates.append(_quote_path(sys.executable))
        candidates += ["python", "python3", "py"] if _IS_WINDOWS else ["python3", "python"]
    else:
        candidates = RUNNERS.get(language, [])
    for cand in candidates:
        if is_valid(cand):
            return cand
    return ""

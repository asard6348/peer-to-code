import bisect
import builtins
import keyword
import re

AUTO_COLOR_THEME = "auto"

COLOR_THEMES = {
    "auto": {
        "label": "Auto",
        "description": "Follows the editor background: Daybreak for bright backgrounds, Midnight for dark ones.",
        # No colors of its own - resolve_auto_theme()/_resolve_active_theme()
        # stand in "idle" or "vivid"'s colors for this at lookup time,
        # based on the editor background last reported via
        # set_editor_background(). Left empty (rather than a copy of
        # either palette) so nothing ever reads stale colors from here.
        "colors": {},
    },
    "idle": {
        "label": "Daybreak",
        "description": "Bright, high-contrast colors. Pairs naturally with the Daybreak color theme.",
        "colors": {
            "keyword": {"foreground": "#ff7700"},
            "softkeyword": {"foreground": "#ff7700"},
            "builtin": {"foreground": "#900090"},
            "string": {"foreground": "#00aa00"},
            "comment": {"foreground": "#dd0000"},
            "definition": {"foreground": "#0000ff"},
            "error_tok": {"background": "#ff7777"},
        },
    },
    "vivid": {
        "label": "Midnight",
        "description": "A softer, saturated palette for dark backgrounds. Pairs naturally with the Midnight color theme.",
        "colors": {
            "keyword": {"foreground": "#c678dd"},
            "softkeyword": {"foreground": "#c678dd"},
            "builtin": {"foreground": "#61afef"},
            "string": {"foreground": "#98c379"},
            "comment": {"foreground": "#7f848e"},
            "definition": {"foreground": "#56b6c2"},
            "error_tok": {"foreground": "#1e1f22", "background": "#e06c75"},
        },
    },
    "custom": {
        "label": "Custom",
        "description": "Your own color for each token category.",
        "colors": {
            "keyword": {"foreground": "#ff7700"},
            "softkeyword": {"foreground": "#ff7700"},
            "builtin": {"foreground": "#900090"},
            "string": {"foreground": "#00aa00"},
            "comment": {"foreground": "#dd0000"},
            "definition": {"foreground": "#0000ff"},
            "error_tok": {"background": "#ff7777"},
        },
    },
}
DEFAULT_COLOR_THEME = AUTO_COLOR_THEME
# Names a saved syntax palette preset can never use, since they're the
# built-in entries above (mirrors theme.RESERVED_PRESET_NAMES).
RESERVED_PALETTE_NAMES = frozenset({"auto", "idle", "vivid", "custom"})
TAG_NAMES = ("keyword", "softkeyword", "builtin", "string", "comment", "definition", "error_tok")
TAG_LABELS = {
    "keyword": "Keywords (if, def, import)",
    "softkeyword": "Soft keywords (match, case, type)",
    "builtin": "Built-in names (print, len, str)",
    "string": "Strings",
    "comment": "Comments",
    "definition": "Definitions (name after def/class/fn)",
    "error_tok": "Invalid/unrecognized token",
}
TAG_COLOR_ATTR = {name: ("background" if name == "error_tok" else "foreground") for name in TAG_NAMES}

_active_color_theme = DEFAULT_COLOR_THEME
_editor_bg = "#1e1f22"  # placeholder until set_editor_background() is called with a real one


def set_color_theme(name):
    """Switches which of COLOR_THEMES the highlighter uses from here on -
    callers still need to re-run configure_tags()+highlight() afterward
    (or just re-open/re-highlight the buffer) for it to actually show up,
    same as any other tag_configure change."""
    global _active_color_theme
    if name in COLOR_THEMES:
        _active_color_theme = name


def get_color_theme():
    return _active_color_theme


def set_editor_background(hex_color):
    """Records the editor's current background color so the "auto" palette
    can decide, next time colors are looked up, whether Light or Vivid
    suits it better. Callers pass this in whenever the editor background
    changes - at startup and on every live theme apply - so "auto" always
    reflects the background that's actually on screen, not a stale one."""
    global _editor_bg
    if hex_color:
        _editor_bg = hex_color


def brightness(hex_color):
    """Perceived brightness of a #rrggbb color, from 0 (black) to 1
    (white), via the standard luma weighting (green reads brighter to the
    eye than red, which in turn reads brighter than blue)."""
    try:
        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    except (TypeError, ValueError, IndexError):
        return 0.0
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def resolve_auto_theme(edit_bg=None):
    """Which built-in palette ("idle" a.k.a. Light, or "vivid") the "auto"
    syntax theme currently resolves to: Light once the background is at
    least half-bright, Vivid below that. Defaults to the last background
    reported via set_editor_background(); callers previewing an
    as-yet-unsaved background (Settings' Theme tab) can pass one in
    directly instead."""
    bg = edit_bg if edit_bg is not None else _editor_bg
    return "idle" if brightness(bg) >= 0.5 else "vivid"


def set_custom_colors(overrides):
    """Rebuilds the "custom" theme's colors from `overrides`
    ({tag_name: {"foreground": "#rrggbb"} and/or {"background": "#rrggbb"}}),
    falling back to "idle"'s value for any tag not (yet) customized so
    "Custom" always starts from a sane, fully-colored baseline rather than
    partially blank. Doesn't switch the active theme to "custom" itself -
    callers (Settings' Advanced dialog, and editor.py restoring it from
    the saved config at startup) do that explicitly."""
    base = COLOR_THEMES["idle"]["colors"]
    COLOR_THEMES["custom"]["colors"] = {
        name: dict(overrides.get(name) or base.get(name, {})) for name in TAG_NAMES
    }


def sync_saved_palettes(saved_presets):
    """Rebuilds COLOR_THEMES' user-saved entries (any key not one of
    RESERVED_PALETTE_NAMES) from `saved_presets`
    ({name: {tag_name: {"foreground": ...} and/or {"background": ...}}}) -
    called once at startup and again whenever Settings saves or cancels a
    Save As/Rename/Remove, so a rename or removal takes effect right away
    and no stale entry is left selectable. Mirrors set_custom_colors()'s
    per-tag fallback so a saved preset is always fully colored even if it
    predates a tag category being added."""
    base = COLOR_THEMES["idle"]["colors"]
    for key in [k for k in COLOR_THEMES if k not in RESERVED_PALETTE_NAMES]:
        del COLOR_THEMES[key]
    for name, colors in (saved_presets or {}).items():
        COLOR_THEMES[name] = {
            "label": name,
            "description": "Your own saved syntax palette.",
            "colors": {tag: dict((colors or {}).get(tag) or base.get(tag, {})) for tag in TAG_NAMES},
        }


def active_palette_colors():
    """The TAG_NAMES-keyed color dict actually in effect right now -
    resolving "auto" to whichever of "idle"/"vivid" it currently stands
    in for. Used by Settings' Save As to snapshot "whatever's on screen
    right now" into a new named preset, whatever theme/preset produced it."""
    return dict(_palette())


def _resolve_active_theme():
    """The real COLOR_THEMES key backing whatever's active - "auto" isn't
    a real palette itself, it stands in for whichever of "idle"/"vivid"
    resolve_auto_theme() currently picks."""
    if _active_color_theme != AUTO_COLOR_THEME:
        return _active_color_theme
    return resolve_auto_theme()


def _palette():
    return COLOR_THEMES[_resolve_active_theme()]["colors"]


KEYWORDS = set(keyword.kwlist)
SOFT_KEYWORDS = set(getattr(keyword, "softkwlist", []))
BUILTIN_NAMES = {name for name in dir(builtins) if not name.startswith("_")} - KEYWORDS

EXTENSION_LANGUAGE = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ksh": "bash",
    ".c": "cpp", ".h": "cpp", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "csharp",
    ".lua": "lua",
    ".rs": "rust",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".php": "php", ".phtml": "php",
    ".rb": "ruby", ".rbw": "ruby",
    ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin",
    ".pl": "perl", ".pm": "perl",
    ".r": "r",
    ".hs": "haskell", ".lhs": "haskell",
    ".scala": "scala", ".sc": "scala",
    ".dart": "dart",
    ".m": "objc", ".mm": "objc",
    ".sql": "sql",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".css": "css",
    ".xml": "xml", ".xsl": "xml", ".xsd": "xml", ".svg": "xml",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".jsonc": "json",
    ".md": "markdown", ".markdown": "markdown",
    ".mk": "makefile",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".jl": "julia",
    ".f90": "fortran", ".f95": "fortran", ".f": "fortran", ".for": "fortran",
    ".pas": "pascal", ".pp": "pascal", ".dpr": "pascal",
    ".adb": "ada", ".ads": "ada",
    ".asm": "asm", ".s": "asm",
    ".groovy": "groovy", ".gvy": "groovy", ".gradle": "groovy",
    ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hrl": "erlang",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".lisp": "lisp", ".lsp": "lisp", ".scm": "lisp", ".ss": "lisp",
    ".fs": "fsharp", ".fsx": "fsharp",
    ".ini": "ini", ".cfg": "ini",
    ".toml": "toml",
    ".cmake": "cmake",
    ".bat": "batch", ".cmd": "batch",
    ".zig": "zig",
    ".nim": "nim", ".nims": "nim",
    ".d": "d",
    ".ml": "ocaml", ".mli": "ocaml",
    ".pro": "prolog", ".plg": "prolog",
    ".tcl": "tcl",
    ".glsl": "glsl", ".vert": "glsl", ".frag": "glsl", ".hlsl": "glsl",
    ".proto": "protobuf",
    ".graphql": "graphql", ".gql": "graphql",
    ".conf": "nginx",
}

LANGUAGE_LABELS = {
    "python": "Python",
    "bash": "Shell",
    "cpp": "C/C++",
    "csharp": "C#",
    "lua": "Lua",
    "rust": "Rust",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "java": "Java",
    "go": "Go",
    "php": "PHP",
    "ruby": "Ruby",
    "swift": "Swift",
    "kotlin": "Kotlin",
    "perl": "Perl",
    "r": "R",
    "haskell": "Haskell",
    "scala": "Scala",
    "dart": "Dart",
    "objc": "Objective-C",
    "sql": "SQL",
    "html": "HTML",
    "css": "CSS",
    "xml": "XML",
    "yaml": "YAML",
    "json": "JSON",
    "markdown": "Markdown",
    "makefile": "Makefile",
    "dockerfile": "Dockerfile",
    "powershell": "PowerShell",
    "julia": "Julia",
    "fortran": "Fortran",
    "pascal": "Pascal",
    "ada": "Ada",
    "asm": "Assembly",
    "groovy": "Groovy",
    "elixir": "Elixir",
    "erlang": "Erlang",
    "clojure": "Clojure",
    "lisp": "Lisp/Scheme",
    "fsharp": "F#",
    "ini": "INI",
    "toml": "TOML",
    "cmake": "CMake",
    "batch": "Batch",
    "zig": "Zig",
    "nim": "Nim",
    "d": "D",
    "ocaml": "OCaml",
    "prolog": "Prolog",
    "tcl": "Tcl",
    "glsl": "GLSL/HLSL",
    "protobuf": "Protocol Buffers",
    "graphql": "GraphQL",
    "nginx": "Nginx Config",
    None: "Plain Text",
}


def _detect_by_basename(filename):
    """A handful of common files are identified by exact name rather than
    extension (Kate does the same for these) - Makefile and Dockerfile
    have no extension at all in normal use."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if base in ("makefile", "gnumakefile"):
        return "makefile"
    if base in ("dockerfile",) or base.startswith("dockerfile."):
        return "dockerfile"
    return None


def detect_language(filename, default="python"):
    """None (unrecognized extension) means plaintext - no highlighting at
    all. A brand-new, not-yet-saved buffer (falsy `filename`) uses
    `default` instead - callers pass in the user's configured default
    new-file language (Settings > General) rather than this function
    assuming Python for everyone."""
    if not filename:
        return default
    by_name = _detect_by_basename(filename)
    if by_name:
        return by_name
    for ext, lang in EXTENSION_LANGUAGE.items():
        if filename.lower().endswith(ext):
            return lang
    return None


def language_label(language):
    return LANGUAGE_LABELS.get(language, "Plain Text")


def configure_tags(text_widget):
    palette = _palette()
    for name in TAG_NAMES:
        opts = palette.get(name, {})
        text_widget.tag_configure(name, foreground=opts.get("foreground", ""), background=opts.get("background", ""))
    text_widget.tag_raise("sel")


def highlight(text_widget, language="python"):
    """Re-highlights the whole buffer for the given language. Unrecognized/
    unsupported languages (language is None) get no color tags at all."""
    for tag in TAG_NAMES:
        text_widget.tag_remove(tag, "1.0", "end")
    if language is None:
        return
    source = text_widget.get("1.0", "end-1c")
    if not source.strip():
        return
    if language == "python":
        _highlight_python(text_widget, source)
    elif language in _REGEX_LANGS:
        _highlight_regex(text_widget, source, language)


_PY_STRING_PREFIX = re.compile(r"(?:[rRbBuUfF]{1,2})(?=[\"'])")
_PY_NAME = re.compile(r"[^\W\d]\w*")
_PY_NUMBER = re.compile(r"\.?\d(?:[\w]|\.(?!\.))*")


def _py_scan_string(src, i, quote, triple, is_f, n):
    """Returns the index just past the string literal whose body starts at
    `i` (right after its opening quote). Never fails: an unterminated
    single-quote string ends at the end of its line, an unterminated
    triple-quoted one at the end of the buffer - the way every editor
    colors a string that's still being typed - so the half-typed literal
    itself is colored and nothing after it is affected."""
    depth = 0
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if triple:
            if src.startswith(quote * 3, i) and depth == 0:
                return i + 3
        else:
            if c == "\n":
                return i
            if c == quote and depth == 0:
                return i + 1
        if is_f:
            if c == "{":
                if depth == 0 and src.startswith("{{", i):
                    i += 2
                    continue
                depth += 1
            elif c == "}" and depth > 0:
                depth -= 1
            elif depth > 0 and c in "\"'" and not (c == quote and not triple):
                # A string nested inside a replacement field: {d["k"]}
                q3 = src.startswith(c * 3, i)
                i = _py_scan_string(src, i + (3 if q3 else 1), c, q3, False, n)
                continue
        i += 1
    return n


def _highlight_python(text_widget, source):
    """Hand-written scanner instead of tokenize: tokenize raises on any
    half-typed construct, and recovering by re-tokenizing the remainder as a
    fresh chunk loses the indent stack, so the next dedented line raises
    again and gets skipped (left uncolored). This scanner has no error
    states, so every line is colored independently of what's broken above."""
    n = len(source)
    line_starts = [0]
    pos = source.find("\n")
    while pos != -1:
        line_starts.append(pos + 1)
        pos = source.find("\n", pos + 1)

    def idx(off):
        row = bisect.bisect_right(line_starts, off) - 1
        return f"{row + 1}.{off - line_starts[row]}"

    def add(tag, a, b):
        if b > a:
            text_widget.tag_add(tag, idx(a), idx(b))

    i = 0
    expect_definition = False
    prev_dot = False
    while i < n:
        c = source[i]
        if c in " \t\r\n\f\\":
            i += 1
            continue
        if c == "#":
            j = source.find("\n", i)
            j = n if j == -1 else j
            add("comment", i, j)
            i = j
            continue
        start = i
        is_f = False
        m = _PY_STRING_PREFIX.match(source, i)
        if m or c in "\"'":
            if m:
                is_f = "f" in m.group().lower()
                i = m.end()
            quote = source[i]
            triple = source.startswith(quote * 3, i)
            i = _py_scan_string(source, i + (3 if triple else 1), quote, triple, is_f, n)
            add("string", start, i)
            expect_definition = prev_dot = False
            continue
        m = _PY_NAME.match(source, i)
        if m:
            word = m.group()
            if expect_definition:
                tag = "definition"
            elif word in KEYWORDS:
                tag = "keyword"
            elif word in SOFT_KEYWORDS:
                tag = "softkeyword"
            elif not prev_dot and word in BUILTIN_NAMES:
                tag = "builtin"
            else:
                tag = None
            if tag:
                add(tag, i, m.end())
            expect_definition = word in ("def", "class")
            prev_dot = False
            i = m.end()
            continue
        m = _PY_NUMBER.match(source, i)
        if m:
            i = m.end()
            expect_definition = prev_dot = False
            continue
        prev_dot = c == "." and source[i - 1:i] != "." and source[i + 1:i + 2] != "."
        expect_definition = False
        i += 1


_BASH_KEYWORDS = {
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "function", "in", "select", "time", "break", "continue",
    "return", "local", "export", "readonly", "declare", "unset", "shift",
    "trap", "exit", "eval", "exec", "set", "source",
}
_BASH_BUILTINS = {
    "echo", "cd", "pwd", "printf", "read", "alias", "unalias", "type",
    "which", "let", "test", "true", "false",
}

_C_FAMILY_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while", "_Bool", "_Complex",
}
_CPP_KEYWORDS = _C_FAMILY_KEYWORDS | {
    "class", "public", "private", "protected", "virtual", "friend",
    "template", "typename", "namespace", "using", "new", "delete", "this",
    "try", "catch", "throw", "operator", "explicit", "mutable", "bool",
    "true", "false", "nullptr", "constexpr", "decltype", "static_assert",
    "thread_local", "noexcept", "override", "final", "export", "constinit",
    "consteval", "concept", "requires", "co_await", "co_return", "co_yield",
}

_CSHARP_KEYWORDS = {
    "abstract", "as", "async", "await", "base", "bool", "break", "byte",
    "case", "catch", "char", "checked", "class", "const", "continue",
    "decimal", "default", "delegate", "do", "double", "else", "enum",
    "event", "explicit", "extern", "false", "finally", "fixed", "float",
    "for", "foreach", "get", "goto", "if", "implicit", "in", "int",
    "interface", "internal", "is", "lock", "long", "namespace", "new",
    "null", "object", "operator", "out", "override", "params", "private",
    "protected", "public", "readonly", "ref", "return", "sbyte", "sealed",
    "set", "short", "sizeof", "stackalloc", "static", "string", "struct",
    "switch", "this", "throw", "true", "try", "typeof", "uint", "ulong",
    "unchecked", "unsafe", "ushort", "using", "value", "var", "virtual",
    "void", "volatile", "where", "while", "yield", "partial", "nameof",
}

_LUA_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for",
    "function", "goto", "if", "in", "local", "nil", "not", "or", "repeat",
    "return", "then", "true", "until", "while",
}
_LUA_BUILTINS = {
    "print", "pairs", "ipairs", "type", "tostring", "tonumber", "require",
    "pcall", "error", "setmetatable", "getmetatable", "select", "table",
    "string", "math", "os", "io",
}

_RUST_KEYWORDS = {
    "as", "async", "await", "break", "const", "continue", "crate", "dyn",
    "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in",
    "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
    "self", "Self", "static", "struct", "super", "trait", "true", "type",
    "unsafe", "use", "where", "while", "union", "async", "yield",
}
_RUST_BUILTINS = {
    "String", "Vec", "Option", "Some", "None", "Result", "Ok", "Err", "Box",
    "println", "print", "vec", "format", "panic", "assert", "assert_eq",
}

_CPP_STRING = r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])\''
_CPP_COMMENT = r'/\*[\s\S]*?\*/|//[^\n]*'
_CPP_PREPROC = r'^[ \t]*#[ \t]*\w+.*$'
_C_STYLE_NUMBER = r'\b0[xX][0-9a-fA-F]+\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?[a-zA-Z]*\b'
_HASH_COMMENT = r'#[^\n]*'
_SEMI_COMMENT = r';[^\n]*'
_DQ_SQ_STRING = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
_TRIPLE_OR_DQ_STRING = r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"'

# --- The rest of these mirror the hand-picked keyword sets above: enough
# of each language's core syntax to make highlighting useful, not a
# from-the-spec exhaustive list. Extra Kate-style languages beyond the
# original bash/cpp/csharp/lua/rust set. ---

_JS_KEYWORDS = {
    "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "export", "extends", "finally",
    "for", "function", "if", "import", "in", "instanceof", "new",
    "return", "super", "switch", "this", "throw", "try", "typeof", "var",
    "void", "while", "with", "yield", "let", "static", "get", "set", "of",
    "async", "await", "null", "true", "false", "undefined",
}
_JS_BUILTINS = {
    "console", "Math", "JSON", "Object", "Array", "String", "Number",
    "Boolean", "Promise", "Map", "Set", "Symbol", "Error", "Date",
    "RegExp", "NaN", "Infinity", "window", "document", "globalThis",
    "require", "module", "exports",
}
_TS_KEYWORDS = _JS_KEYWORDS | {
    "interface", "type", "enum", "implements", "private", "public",
    "protected", "readonly", "namespace", "declare", "abstract", "as",
    "is", "keyof", "infer", "satisfies",
}

_JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch",
    "char", "class", "const", "continue", "default", "do", "double",
    "else", "enum", "extends", "final", "finally", "float", "for",
    "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private",
    "protected", "public", "return", "short", "static", "strictfp",
    "super", "switch", "synchronized", "this", "throw", "throws",
    "transient", "try", "void", "volatile", "while", "var", "record",
    "sealed", "permits", "yield", "true", "false", "null",
}
_JAVA_BUILTINS = {
    "String", "System", "Object", "Integer", "Double", "Boolean", "List",
    "Map", "Set", "ArrayList", "HashMap", "Exception", "Thread",
    "Override",
}

_GO_KEYWORDS = {
    "break", "case", "chan", "const", "continue", "default", "defer",
    "else", "fallthrough", "for", "func", "go", "goto", "if", "import",
    "interface", "map", "package", "range", "return", "select",
    "struct", "switch", "type", "var",
}
_GO_BUILTINS = {
    "len", "cap", "make", "new", "append", "copy", "delete", "panic",
    "recover", "print", "println", "true", "false", "nil", "iota",
    "error", "string", "int", "int8", "int16", "int32", "int64", "uint",
    "uint8", "uint16", "uint32", "uint64", "float32", "float64", "byte",
    "rune", "bool",
}

_PHP_KEYWORDS = {
    "abstract", "and", "array", "as", "break", "callable", "case",
    "catch", "class", "clone", "const", "continue", "declare",
    "default", "do", "echo", "else", "elseif", "empty", "endif",
    "endforeach", "endwhile", "extends", "final", "finally", "fn",
    "for", "foreach", "function", "global", "goto", "if", "implements",
    "include", "include_once", "instanceof", "insteadof", "interface",
    "isset", "list", "match", "namespace", "new", "or", "print",
    "private", "protected", "public", "require", "require_once",
    "return", "static", "switch", "throw", "trait", "try", "unset",
    "use", "var", "while", "xor", "yield", "true", "false", "null",
}
_PHP_BUILTINS = {
    "strlen", "count", "array_map", "array_filter", "isset", "empty",
    "print_r", "var_dump", "implode", "explode", "in_array",
}

_RUBY_KEYWORDS = {
    "begin", "end", "if", "unless", "then", "elsif", "else", "case",
    "when", "while", "until", "for", "in", "do", "def", "class",
    "module", "return", "yield", "break", "next", "redo", "retry",
    "raise", "rescue", "ensure", "self", "super", "nil", "true",
    "false", "and", "or", "not", "require", "require_relative",
    "attr_accessor", "attr_reader", "attr_writer", "private",
    "protected", "public", "lambda", "proc",
}
_RUBY_BUILTINS = {
    "puts", "print", "p", "gets", "Array", "Hash", "String", "Integer",
    "Float", "Symbol", "Proc", "Kernel", "Comparable", "Enumerable",
}

_SWIFT_KEYWORDS = {
    "associatedtype", "class", "deinit", "enum", "extension",
    "fileprivate", "func", "import", "init", "inout", "internal", "let",
    "open", "operator", "private", "protocol", "public", "rethrows",
    "static", "struct", "subscript", "typealias", "var", "break",
    "case", "continue", "default", "defer", "do", "else", "fallthrough",
    "for", "guard", "if", "in", "repeat", "return", "switch", "where",
    "while", "as", "Any", "catch", "false", "is", "nil", "self", "Self",
    "super", "throw", "throws", "true", "try", "async", "await",
}
_SWIFT_BUILTINS = {
    "print", "String", "Int", "Double", "Float", "Bool", "Array",
    "Dictionary", "Set", "Optional", "Error",
}

_KOTLIN_KEYWORDS = {
    "as", "break", "class", "continue", "do", "else", "false", "for",
    "fun", "if", "in", "interface", "is", "null", "object", "package",
    "return", "super", "this", "throw", "true", "try", "typealias",
    "typeof", "val", "var", "when", "while", "by", "catch",
    "constructor", "finally", "get", "import", "init", "set", "where",
    "companion", "const", "data", "enum", "inline", "inner", "internal",
    "lateinit", "open", "operator", "override", "private", "protected",
    "public", "sealed", "suspend", "vararg",
}
_KOTLIN_BUILTINS = {
    "println", "print", "listOf", "mapOf", "setOf", "arrayOf", "String",
    "Int", "Double", "Float", "Boolean", "List", "Map", "Set",
}

_PERL_KEYWORDS = {
    "my", "our", "local", "sub", "if", "elsif", "else", "unless",
    "while", "until", "for", "foreach", "do", "return", "last", "next",
    "redo", "package", "use", "require", "no", "qw", "print", "defined",
    "undef", "bless", "ref", "wantarray", "eval", "die", "and", "or",
    "not", "xor",
}

_R_KEYWORDS = {
    "if", "else", "repeat", "while", "function", "for", "in", "next",
    "break", "TRUE", "FALSE", "NULL", "Inf", "NaN", "NA",
    "NA_integer_", "NA_real_", "NA_character_",
}
_R_BUILTINS = {
    "print", "cat", "paste", "paste0", "c", "list", "vector",
    "data.frame", "matrix", "sapply", "lapply", "vapply", "mapply",
    "library", "require", "summary", "str",
}

_HASKELL_KEYWORDS = {
    "case", "class", "data", "default", "deriving", "do", "else",
    "foreign", "if", "import", "in", "infix", "infixl", "infixr",
    "instance", "let", "module", "newtype", "of", "then", "type",
    "where",
}

_SCALA_KEYWORDS = {
    "abstract", "case", "catch", "class", "def", "do", "else",
    "extends", "false", "final", "finally", "for", "forSome", "if",
    "implicit", "import", "lazy", "match", "new", "null", "object",
    "override", "package", "private", "protected", "return", "sealed",
    "super", "this", "throw", "trait", "true", "try", "type", "val",
    "var", "while", "with", "yield", "given", "using", "enum",
    "extension",
}

_DART_KEYWORDS = {
    "abstract", "as", "assert", "async", "await", "break", "case",
    "catch", "class", "const", "continue", "covariant", "default",
    "deferred", "do", "dynamic", "else", "enum", "export", "extends",
    "extension", "external", "factory", "false", "final", "finally",
    "for", "Function", "get", "hide", "if", "implements", "import",
    "in", "interface", "is", "library", "mixin", "new", "null", "on",
    "operator", "part", "rethrow", "return", "set", "show", "static",
    "super", "switch", "sync", "this", "throw", "true", "try",
    "typedef", "var", "void", "while", "with", "yield", "late",
    "required",
}
_DART_BUILTINS = {
    "print", "String", "int", "double", "bool", "List", "Map", "Set",
    "Object", "Future", "Stream",
}

_OBJC_KEYWORDS = _C_FAMILY_KEYWORDS | {
    "self", "super", "nil", "YES", "NO", "id", "instancetype", "BOOL",
    "IBOutlet", "IBAction", "strong", "weak", "nonatomic", "atomic",
    "readonly", "readwrite", "nonnull", "nullable",
}

_SQL_KEYWORDS = {
    "select", "insert", "update", "delete", "from", "where", "join",
    "inner", "outer", "left", "right", "full", "on", "group", "by",
    "order", "having", "limit", "offset", "into", "values", "set",
    "create", "table", "alter", "drop", "index", "view", "trigger",
    "procedure", "function", "begin", "end", "if", "else", "case",
    "when", "then", "null", "not", "and", "or", "in", "exists",
    "between", "like", "as", "distinct", "union", "all", "primary",
    "key", "foreign", "references", "default", "constraint", "cascade",
    "transaction", "commit", "rollback", "grant", "revoke",
}

_POWERSHELL_KEYWORDS = {
    "begin", "break", "catch", "class", "continue", "data", "define",
    "do", "dynamicparam", "else", "elseif", "end", "exit", "filter",
    "finally", "for", "foreach", "from", "function", "if", "in",
    "param", "process", "return", "switch", "throw", "trap", "try",
    "until", "using", "var", "while", "workflow",
}
_POWERSHELL_BUILTINS = {
    "Write-Host", "Write-Output", "Get-ChildItem", "Get-Content",
    "Set-Content", "Select-Object", "Where-Object", "ForEach-Object",
    "New-Object", "Invoke-Expression",
}

_JULIA_KEYWORDS = {
    "function", "end", "if", "else", "elseif", "for", "while", "break",
    "continue", "return", "begin", "do", "try", "catch", "finally",
    "module", "using", "import", "export", "struct", "mutable",
    "abstract", "type", "const", "global", "local", "let", "quote",
    "macro", "in", "where", "true", "false", "nothing",
}
_JULIA_BUILTINS = {"println", "print", "push!", "pop!", "length", "size", "map", "filter", "reduce"}

_FORTRAN_KEYWORDS = {
    "program", "end", "subroutine", "function", "module", "use",
    "implicit", "none", "integer", "real", "double", "precision",
    "character", "logical", "complex", "dimension", "allocatable",
    "parameter", "if", "then", "else", "elseif", "endif", "do",
    "enddo", "while", "continue", "call", "return", "stop", "write",
    "read", "print", "format", "common", "data", "save", "type",
    "contains", "interface",
}

_PASCAL_KEYWORDS = {
    "program", "uses", "begin", "end", "var", "const", "type",
    "procedure", "function", "if", "then", "else", "case", "of",
    "while", "do", "for", "to", "downto", "repeat", "until", "record",
    "array", "set", "file", "packed", "with", "goto", "label", "div",
    "mod", "and", "or", "not", "xor", "in", "nil", "true", "false",
    "class", "constructor", "destructor", "inherited", "interface",
    "implementation", "unit", "try", "except", "finally", "raise",
}

_ADA_KEYWORDS = {
    "abort", "abs", "abstract", "accept", "access", "aliased", "all",
    "and", "array", "at", "begin", "body", "case", "constant",
    "declare", "delay", "delta", "digits", "do", "else", "elsif",
    "end", "entry", "exception", "exit", "for", "function", "generic",
    "goto", "if", "in", "interface", "is", "limited", "loop", "mod",
    "new", "not", "null", "of", "or", "others", "out", "overriding",
    "package", "pragma", "private", "procedure", "protected", "raise",
    "range", "record", "rem", "renames", "requeue", "return",
    "reverse", "select", "separate", "some", "subtype", "synchronized",
    "tagged", "task", "terminate", "then", "type", "until", "use",
    "when", "while", "with", "xor",
}

_ASM_KEYWORDS = {
    "mov", "push", "pop", "call", "ret", "jmp", "je", "jne", "jg",
    "jl", "jge", "jle", "cmp", "test", "add", "sub", "mul", "div",
    "inc", "dec", "lea", "nop", "int", "syscall", "and", "or", "xor",
    "not", "shl", "shr", "section", "global", "extern", "db", "dw",
    "dd", "dq", "segment", "proc", "endp",
}
_ASM_BUILTINS = {
    "eax", "ebx", "ecx", "edx", "esi", "edi", "esp", "ebp", "rax",
    "rbx", "rcx", "rdx", "rsi", "rdi", "rsp", "rbp", "al", "ah", "bl",
    "bh",
}

_GROOVY_KEYWORDS = _JAVA_KEYWORDS | {"def", "trait"}

_ELIXIR_KEYWORDS = {
    "def", "defmodule", "defp", "do", "end", "if", "unless", "else",
    "case", "cond", "when", "fn", "true", "false", "nil", "import",
    "alias", "require", "use", "raise", "try", "rescue", "after",
    "catch", "receive", "for", "with",
}

_ERLANG_KEYWORDS = {
    "module", "export", "import", "begin", "end", "case", "of", "if",
    "let", "when", "fun", "receive", "after", "try", "catch", "throw",
    "andalso", "orelse", "not", "and", "or", "band", "bor", "bxor",
    "bnot",
}

_CLOJURE_KEYWORDS = {
    "def", "defn", "defn-", "defmacro", "let", "if", "do", "fn",
    "loop", "recur", "when", "cond", "case", "ns", "require", "import",
    "true", "false", "nil",
}

_LISP_KEYWORDS = {
    "define", "lambda", "let", "let*", "letrec", "if", "cond", "case",
    "begin", "set!", "quote", "quasiquote", "unquote", "do", "and",
    "or", "not", "else",
}

_FSHARP_KEYWORDS = {
    "let", "mutable", "if", "then", "else", "elif", "match", "with",
    "function", "fun", "type", "module", "namespace", "open", "rec",
    "and", "or", "not", "do", "for", "in", "while", "try", "finally",
    "exception", "raise", "new", "member", "static", "abstract",
    "interface", "inherit", "yield", "async", "use",
}

_CMAKE_KEYWORDS = {
    "add_executable", "add_library", "add_subdirectory",
    "cmake_minimum_required", "project", "set", "if", "else",
    "elseif", "endif", "foreach", "endforeach", "function",
    "endfunction", "macro", "endmacro", "target_link_libraries",
    "include", "find_package", "option", "message",
}

_BATCH_KEYWORDS = {
    "echo", "set", "if", "else", "for", "goto", "call", "exit",
    "pause", "rem", "cls", "setlocal", "endlocal", "shift",
}

_ZIG_KEYWORDS = {
    "const", "var", "fn", "pub", "return", "if", "else", "while",
    "for", "break", "continue", "struct", "enum", "union", "error",
    "try", "catch", "defer", "errdefer", "comptime", "import",
    "export", "extern", "test", "switch", "async", "await", "suspend",
    "resume", "null", "undefined", "true", "false",
}

_NIM_KEYWORDS = {
    "proc", "func", "method", "template", "macro", "let", "var",
    "const", "type", "if", "elif", "else", "case", "of", "while",
    "for", "in", "break", "continue", "return", "discard", "import",
    "export", "block", "try", "except", "finally", "raise", "object",
    "ref", "tuple", "enum", "true", "false", "nil",
}

_D_KEYWORDS = _C_FAMILY_KEYWORDS | {
    "class", "interface", "import", "module", "public", "private",
    "protected", "package", "override", "abstract", "final",
    "immutable", "auto", "out", "ref", "scope", "pure", "nothrow",
    "template", "mixin", "alias", "true", "false", "null", "this",
    "super", "new", "delete", "try", "catch", "finally", "throw",
}

_OCAML_KEYWORDS = {
    "let", "rec", "and", "in", "fun", "function", "match", "with",
    "if", "then", "else", "begin", "end", "type", "module", "struct",
    "sig", "open", "exception", "try", "raise", "mutable", "of",
    "true", "false", "for", "to", "downto", "while", "do", "done",
}

_TCL_KEYWORDS = {
    "proc", "set", "if", "else", "elseif", "while", "for", "foreach",
    "switch", "return", "break", "continue", "global", "upvar",
    "uplevel", "catch", "expr", "puts", "source", "package",
    "namespace", "variable",
}

_GLSL_KEYWORDS = _C_FAMILY_KEYWORDS | {
    "in", "out", "uniform", "varying", "attribute", "layout",
    "precision", "highp", "mediump", "lowp", "discard", "vec2", "vec3",
    "vec4", "mat2", "mat3", "mat4", "sampler2D", "samplerCube", "true",
    "false",
}

_PROTO_KEYWORDS = {
    "syntax", "package", "import", "option", "message", "service",
    "rpc", "returns", "repeated", "optional", "required", "enum",
    "oneof", "map", "true", "false",
}

_GRAPHQL_KEYWORDS = {
    "query", "mutation", "subscription", "fragment", "on", "type",
    "interface", "union", "enum", "input", "schema", "scalar",
    "directive", "extend", "implements", "true", "false", "null",
}

_NGINX_KEYWORDS = {
    "server", "location", "upstream", "listen", "server_name", "root",
    "index", "proxy_pass", "include", "if", "return", "rewrite",
    "error_page", "worker_processes", "events", "http",
}

_MAKEFILE_KEYWORDS = {
    "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif", "include",
    "define", "endef", "export", "unexport", "override", "vpath",
}

_DOCKERFILE_KEYWORDS = {
    "FROM", "RUN", "CMD", "LABEL", "MAINTAINER", "EXPOSE", "ENV",
    "ADD", "COPY", "ENTRYPOINT", "VOLUME", "USER", "WORKDIR", "ARG",
    "ONBUILD", "STOPSIGNAL", "HEALTHCHECK", "SHELL",
}

_LANG_SPECS = {
    "bash": {
        "keywords": _BASH_KEYWORDS,
        "builtins": _BASH_BUILTINS,
        "comment": r'#[^\n]*',
        "string": r'"(?:\\.|[^"\\])*"|\'[^\']*\'',
        "variable": r'\$\{[^}]*\}|\$\w+',
        "number": r'\b\d+\b',
        "definition_keywords": {"function"},
    },
    "cpp": {
        "keywords": _CPP_KEYWORDS,
        "comment": _CPP_COMMENT,
        "string": _CPP_STRING,
        "preproc": _CPP_PREPROC,
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "struct", "union", "enum", "namespace"},
    },
    "csharp": {
        "keywords": _CSHARP_KEYWORDS,
        "comment": _CPP_COMMENT,
        "string": r'@"(?:[^"]|"")*"|\$"(?:\\.|[^"\\])*"|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])\'',
        "decorator_pat": r'^[ \t]*\[[A-Za-z_][\w.]*(?:\([^\]]*\))?\]',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "struct", "interface", "enum", "namespace", "delegate"},
    },
    "lua": {
        "keywords": _LUA_KEYWORDS,
        "builtins": _LUA_BUILTINS,
        "comment": r'--\[\[[\s\S]*?\]\]|--[^\n]*',
        "string": r'\[\[[\s\S]*?\]\]|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        "number": r'\b0[xX][0-9a-fA-F]+\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?\b',
        "definition_keywords": {"function"},
    },
    "rust": {
        "keywords": _RUST_KEYWORDS,
        "builtins": _RUST_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": r'r#*"[\s\S]*?"#*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'',
        "decorator_pat": r'#!?\[[^\]]*\]',
        "number": r'\b0[xX][0-9a-fA-F]+\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?(?:f32|f64|i8|i16|i32|i64|i128|isize|u8|u16|u32|u64|u128|usize)?\b',
        "definition_keywords": {"fn", "struct", "trait", "enum", "mod", "type"},
    },
    "javascript": {
        "keywords": _JS_KEYWORDS,
        "builtins": _JS_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": r'`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        "decorator_pat": r'@[A-Za-z_][\w.]*',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"function", "class"},
    },
    "typescript": {
        "keywords": _TS_KEYWORDS,
        "builtins": _JS_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": r'`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        "decorator_pat": r'@[A-Za-z_][\w.]*',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"function", "class", "interface", "type", "enum"},
    },
    "java": {
        "keywords": _JAVA_KEYWORDS,
        "builtins": _JAVA_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": _CPP_STRING,
        "decorator_pat": r'@[A-Za-z_][\w.]*',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "interface", "enum", "record"},
    },
    "go": {
        "keywords": _GO_KEYWORDS,
        "builtins": _GO_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": r'`[^`]*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"func", "type", "struct", "interface"},
    },
    "php": {
        "keywords": _PHP_KEYWORDS,
        "builtins": _PHP_BUILTINS,
        "comment": r'//[^\n]*|#[^\n]*|/\*[\s\S]*?\*/',
        "string": _DQ_SQ_STRING,
        "variable": r'\$\w+',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "interface", "trait", "function"},
    },
    "ruby": {
        "keywords": _RUBY_KEYWORDS,
        "builtins": _RUBY_BUILTINS,
        "comment": _HASH_COMMENT,
        "string": _DQ_SQ_STRING,
        "variable": r'@{1,2}\w+|\$\w+',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"def", "class", "module"},
    },
    "swift": {
        "keywords": _SWIFT_KEYWORDS,
        "builtins": _SWIFT_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"',
        "decorator_pat": r'@[A-Za-z_]\w*',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "struct", "enum", "protocol", "func", "extension"},
    },
    "kotlin": {
        "keywords": _KOTLIN_KEYWORDS,
        "builtins": _KOTLIN_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": _TRIPLE_OR_DQ_STRING,
        "decorator_pat": r'@[A-Za-z_][\w.]*',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "interface", "object", "fun"},
    },
    "perl": {
        "keywords": _PERL_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": _DQ_SQ_STRING,
        "variable": r'[\$\@\%]\{?\w+\}?',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"sub", "package"},
    },
    "r": {
        "keywords": _R_KEYWORDS,
        "builtins": _R_BUILTINS,
        "comment": _HASH_COMMENT,
        "string": _DQ_SQ_STRING,
        "number": _C_STYLE_NUMBER,
    },
    "haskell": {
        "keywords": _HASKELL_KEYWORDS,
        "comment": r'--[^\n]*|\{-[\s\S]*?-\}',
        "string": r'"(?:\\.|[^"\\])*"',
        "number": _C_STYLE_NUMBER,
    },
    "scala": {
        "keywords": _SCALA_KEYWORDS,
        "comment": _CPP_COMMENT,
        "string": _TRIPLE_OR_DQ_STRING,
        "decorator_pat": r'@[A-Za-z_][\w.]*',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "object", "trait", "def"},
    },
    "dart": {
        "keywords": _DART_KEYWORDS,
        "builtins": _DART_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        "decorator_pat": r'@[A-Za-z_][\w.]*',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "enum", "mixin", "extension"},
    },
    "objc": {
        "keywords": _OBJC_KEYWORDS,
        "comment": _CPP_COMMENT,
        "string": r'@?"(?:\\.|[^"\\])*"',
        "preproc": _CPP_PREPROC,
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"interface", "implementation", "protocol"},
    },
    "sql": {
        "keywords": _SQL_KEYWORDS,
        "comment": r'--[^\n]*|/\*[\s\S]*?\*/',
        "string": r"'(?:''|[^'])*'",
        "number": _C_STYLE_NUMBER,
    },
    "html": {
        "comment": r'<!--[\s\S]*?-->',
        "string": r'"[^"]*"|\'[^\']*\'',
    },
    "css": {
        "comment": r'/\*[\s\S]*?\*/',
        "string": r'"[^"]*"|\'[^\']*\'',
        "number": _C_STYLE_NUMBER,
    },
    "xml": {
        "comment": r'<!--[\s\S]*?-->',
        "string": r'"[^"]*"|\'[^\']*\'',
    },
    "yaml": {
        "comment": _HASH_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"|\'[^\']*\'',
        "number": _C_STYLE_NUMBER,
    },
    "json": {
        "keywords": {"true", "false", "null"},
        "string": r'"(?:\\.|[^"\\])*"',
        "number": r'-?\b\d+\.?\d*(?:[eE][+-]?\d+)?\b',
    },
    "markdown": {
        "string": r'`[^`\n]*`',
    },
    "makefile": {
        "keywords": _MAKEFILE_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"|\'[^\']*\'',
        "variable": r'\$\([^)]*\)|\$\{[^}]*\}|\$[@^<*%?+]',
        "number": _C_STYLE_NUMBER,
    },
    "dockerfile": {
        "keywords": _DOCKERFILE_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": _DQ_SQ_STRING,
        "number": _C_STYLE_NUMBER,
    },
    "powershell": {
        "keywords": _POWERSHELL_KEYWORDS,
        "builtins": _POWERSHELL_BUILTINS,
        "comment": r'#[^\n]*|<#[\s\S]*?#>',
        "string": _DQ_SQ_STRING,
        "variable": r'\$\w+',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"function", "class"},
    },
    "julia": {
        "keywords": _JULIA_KEYWORDS,
        "builtins": _JULIA_BUILTINS,
        "comment": r'#=[\s\S]*?=#|#[^\n]*',
        "string": _TRIPLE_OR_DQ_STRING,
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"function", "struct", "module", "macro"},
    },
    "fortran": {
        "keywords": _FORTRAN_KEYWORDS,
        "comment": r'![^\n]*',
        "string": r'"[^"]*"|\'[^\']*\'',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"program", "subroutine", "function", "module", "type"},
    },
    "pascal": {
        "keywords": _PASCAL_KEYWORDS,
        "comment": r'\{[\s\S]*?\}|\(\*[\s\S]*?\*\)|//[^\n]*',
        "string": r"'(?:[^']|'')*'",
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"procedure", "function", "class", "unit"},
    },
    "ada": {
        "keywords": _ADA_KEYWORDS,
        "comment": r'--[^\n]*',
        "string": r'"[^"]*"',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"procedure", "function", "package", "type"},
    },
    "asm": {
        "keywords": _ASM_KEYWORDS,
        "builtins": _ASM_BUILTINS,
        "comment": r';[^\n]*',
        "string": _DQ_SQ_STRING,
        "number": r'\b0[xX][0-9a-fA-F]+\b|\b\d+\b',
    },
    "groovy": {
        "keywords": _GROOVY_KEYWORDS,
        "builtins": _JAVA_BUILTINS,
        "comment": _CPP_COMMENT,
        "string": r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'[^\']*\'',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "interface", "trait", "def"},
    },
    "elixir": {
        "keywords": _ELIXIR_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": _TRIPLE_OR_DQ_STRING,
        "variable": r'@\w+',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"def", "defmodule", "defp"},
    },
    "erlang": {
        "keywords": _ERLANG_KEYWORDS,
        "comment": r'%[^\n]*',
        "string": r'"(?:\\.|[^"\\])*"',
        "number": _C_STYLE_NUMBER,
    },
    "clojure": {
        "keywords": _CLOJURE_KEYWORDS,
        "comment": _SEMI_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"def", "defn", "defmacro"},
    },
    "lisp": {
        "keywords": _LISP_KEYWORDS,
        "comment": _SEMI_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"define"},
    },
    "fsharp": {
        "keywords": _FSHARP_KEYWORDS,
        "comment": r'\(\*[\s\S]*?\*\)|//[^\n]*',
        "string": _TRIPLE_OR_DQ_STRING,
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"let", "type", "module"},
    },
    "ini": {
        "comment": r';[^\n]*|#[^\n]*',
        "string": r'"[^"]*"|\'[^\']*\'',
    },
    "toml": {
        "keywords": {"true", "false"},
        "comment": _HASH_COMMENT,
        "string": _TRIPLE_OR_DQ_STRING,
        "number": _C_STYLE_NUMBER,
    },
    "cmake": {
        "keywords": _CMAKE_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"',
        "variable": r'\$\{[^}]*\}',
        "number": _C_STYLE_NUMBER,
    },
    "batch": {
        "keywords": _BATCH_KEYWORDS,
        "comment": r'(?:rem|REM)\b[^\n]*|::[^\n]*',
        "string": r'"[^"]*"',
        "variable": r'%\w+%|%~?\d',
        "number": r'\b\d+\b',
    },
    "zig": {
        "keywords": _ZIG_KEYWORDS,
        "comment": r'//[^\n]*',
        "string": r'"(?:\\.|[^"\\])*"',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"fn", "struct", "enum", "union"},
    },
    "nim": {
        "keywords": _NIM_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": _TRIPLE_OR_DQ_STRING,
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"proc", "func", "method", "type"},
    },
    "d": {
        "keywords": _D_KEYWORDS,
        "comment": r'/\+[\s\S]*?\+/|/\*[\s\S]*?\*/|//[^\n]*',
        "string": r'"(?:\\.|[^"\\])*"|`[^`]*`',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"class", "struct", "interface", "enum", "template"},
    },
    "ocaml": {
        "keywords": _OCAML_KEYWORDS,
        "comment": r'\(\*[\s\S]*?\*\)',
        "string": r'"(?:\\.|[^"\\])*"',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"let", "type", "module"},
    },
    "prolog": {
        "comment": r'%[^\n]*|/\*[\s\S]*?\*/',
        "string": r'"[^"]*"|\'[^\']*\'',
        "number": _C_STYLE_NUMBER,
    },
    "tcl": {
        "keywords": _TCL_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"',
        "variable": r'\$\w+|\$\{[^}]*\}',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"proc"},
    },
    "glsl": {
        "keywords": _GLSL_KEYWORDS,
        "comment": _CPP_COMMENT,
        "string": r'"[^"]*"',
        "preproc": _CPP_PREPROC,
        "number": _C_STYLE_NUMBER,
    },
    "protobuf": {
        "keywords": _PROTO_KEYWORDS,
        "comment": _CPP_COMMENT,
        "string": r'"(?:\\.|[^"\\])*"',
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"message", "service", "enum"},
    },
    "graphql": {
        "keywords": _GRAPHQL_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": _TRIPLE_OR_DQ_STRING,
        "number": _C_STYLE_NUMBER,
        "definition_keywords": {"type", "interface", "enum", "input"},
    },
    "nginx": {
        "keywords": _NGINX_KEYWORDS,
        "comment": _HASH_COMMENT,
        "string": r'"[^"]*"|\'[^\']*\'',
        "variable": r'\$\w+',
        "number": _C_STYLE_NUMBER,
    },
}


def _compile_lang(spec):
    parts = []
    if "comment" in spec:
        # An unclosed block comment runs to the end of the buffer.
        fallback = r"|/\*[\s\S]*" if r"/\*" in spec["comment"] else ""
        parts.append(f"(?P<comment>{spec['comment']}{fallback})")
    if "string" in spec:
        # Second alternative: a half-typed string. Only tried where the
        # real pattern failed, so it colors an unclosed quote to the end
        # of its line instead of leaving it uncolored - and, by consuming
        # it, keeps its contents from being lexed as code.
        fallback = "".join(
            f"|{q}[^{q}\\n]*" for q in ('"', "'") if q in spec["string"]
        )
        parts.append(f"(?P<string>{spec['string']}{fallback})")
    if "decorator_pat" in spec:
        parts.append(f"(?P<decorator>{spec['decorator_pat']})")
    if "preproc" in spec:
        parts.append(f"(?P<preproc>{spec['preproc']})")
    if "variable" in spec:
        parts.append(f"(?P<variable>{spec['variable']})")
    parts.append(f"(?P<number>{spec.get('number', _C_STYLE_NUMBER)})")
    parts.append(r"(?P<name>[A-Za-z_]\w*)")
    return re.compile("|".join(parts), re.MULTILINE)


for _spec in _LANG_SPECS.values():
    try:
        _spec["_compiled"] = _compile_lang(_spec)
    except re.error:
        _spec["_compiled"] = None

_REGEX_LANGS = {lang for lang, spec in _LANG_SPECS.items() if spec.get("_compiled")}

_KIND_TO_TAG = {"comment": "comment", "string": "string"}


def _highlight_regex(text_widget, source, language):
    spec = _LANG_SPECS[language]
    pattern = spec["_compiled"]
    keywords = spec.get("keywords", ())
    builtins_ = spec.get("builtins", ())
    definition_keywords = spec.get("definition_keywords", ())
    try:
        matches = list(pattern.finditer(source))
    except re.error:
        return
    expect_definition = False
    for m in matches:
        kind = m.lastgroup
        text = m.group()
        if not text:
            continue
        tag = _KIND_TO_TAG.get(kind)
        if kind == "name":
            if expect_definition:
                tag = "definition"
            elif text in keywords:
                tag = "keyword"
            elif text in builtins_:
                tag = "builtin"
            else:
                tag = None
        if tag:
            text_widget.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")

        if kind == "name":
            expect_definition = False if expect_definition else text in definition_keywords
        elif kind != "comment":
            expect_definition = False

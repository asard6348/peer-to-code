import builtins
import io
import keyword
import re
import tokenize

COLOR_THEMES = {
    "idle": {
        "label": "Classic",
        "description": "Bright, high-contrast colors.",
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
        "label": "Vivid",
        "description": "A softer, saturated palette for dark backgrounds.",
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
DEFAULT_COLOR_THEME = "idle"
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


def _palette():
    return COLOR_THEMES[_active_color_theme]["colors"]


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
}

LANGUAGE_LABELS = {
    "python": "Python",
    "bash": "Shell",
    "cpp": "C/C++",
    "csharp": "C#",
    "lua": "Lua",
    "rust": "Rust",
    None: "Plain Text",
}


def detect_language(filename, default="python"):
    """None (unrecognized extension) means plaintext - no highlighting at
    all. A brand-new, not-yet-saved buffer (falsy `filename`) uses
    `default` instead - callers pass in the user's configured default
    new-file language (Settings > General) rather than this function
    assuming Python for everyone."""
    if not filename:
        return default
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


_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


def _highlight_python(text_widget, source):
    tokens = _tokenize_best_effort(source)
    expect_definition = False
    prev_is_dot = False
    i, n = 0, len(tokens)
    while i < n:
        ttype, tstr, (srow, scol), (erow, ecol), _line = tokens[i]
        if not tstr:
            i += 1
            continue

        if _FSTRING_START is not None and ttype == _FSTRING_START:
            start = (srow, scol)
            end = (erow, ecol)
            depth = 1
            i += 1
            while i < n and depth > 0:
                jtype, jstr, _jstart, jend, _jline = tokens[i]
                if jtype == _FSTRING_START:
                    depth += 1
                elif jtype == _FSTRING_END:
                    depth -= 1
                end = jend
                i += 1
            text_widget.tag_add("string", f"{start[0]}.{start[1]}", f"{end[0]}.{end[1]}")
            expect_definition = False
            prev_is_dot = False
            continue

        tag = None
        if ttype == tokenize.COMMENT:
            tag = "comment"
        elif ttype == tokenize.STRING:
            tag = "string"
        elif ttype == tokenize.NAME:
            if expect_definition:
                tag = "definition"
            elif tstr in KEYWORDS:
                tag = "keyword"
            elif tstr in SOFT_KEYWORDS:
                tag = "softkeyword"
            elif not prev_is_dot and tstr in BUILTIN_NAMES:
                tag = "builtin"
        elif ttype == tokenize.ERRORTOKEN:
            tag = "error_tok"
        if tag:
            text_widget.tag_add(tag, f"{srow}.{scol}", f"{erow}.{ecol}")

        if ttype not in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT):
            expect_definition = (ttype == tokenize.NAME and tstr in ("def", "class"))
            prev_is_dot = (ttype == tokenize.OP and tstr == ".")
        i += 1


def _tokenize_best_effort(source):
    """Real tokenize() raises on unterminated strings / bad indentation, which
    is the normal state of a buffer mid-edit. Collect whatever tokens it
    manages before that so highlighting degrades gracefully rather than
    freezing on invalid-but-in-progress code."""
    out = []
    try:
        gen = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in gen:
            out.append(tok)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        pass
    except StopIteration:
        pass
    return out


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
}


def _compile_lang(spec):
    parts = []
    if "comment" in spec:
        parts.append(f"(?P<comment>{spec['comment']})")
    if "string" in spec:
        parts.append(f"(?P<string>{spec['string']})")
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

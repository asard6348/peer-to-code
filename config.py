import json
import os
import re
import sys
from pathlib import Path

APP_DIRNAME = "PeerToCode"

MAX_RECENT_PROJECTS = 8

DEFAULTS = {
    "connection": {
        "name": "",
        "address": "127.0.0.1:5123",
        "working_dir": "",
        "recent_projects": [],
        "p2p_address": "",
        "p2p_join_address": "",
    },
    "ui": {
        "explorer_visible": True,
        "console_visible": True,
        "last_tab": "Open",
        "window_size": "",
        "window_position": "",
        "save_window_position": True,
        "save_window_size": True,
    },
    "editor": {
        "default_new_file_language": "python",
        "run_commands": {},
        "syntax_theme": "vivid",
        "custom_syntax_colors": {},
        "word_wrap": False,
        "terminal_messages": {
            "finished": True,
            "stopped": True,
            "interrupted": True,
        },
    },
    "shortcuts": {
        "new_file": "<Control-n>",
        "open_file": "<Control-o>",
        "save_file": "<Control-s>",
        "save_as": "<Control-Shift-S>",
        "find": "<Control-f>",
        "replace": "<Control-h>",
        "select_all": "<Control-a>",
        "run": "<F5>",
        "stop": "<Shift-F5>",
        "toggle_comment": "<Control-slash>",
        "duplicate_line": "<Control-d>",
        "delete_line": "<Control-Shift-K>",
        "move_line_up": "<Alt-Up>",
        "move_line_down": "<Alt-Down>",
        "indent": "<Control-bracketright>",
        "dedent": "<Control-bracketleft>",
        "undo_peer": "<Alt-z>",
    },
    "output_shortcuts": {
        "console_interrupt": "<Control-c>",
    },
    "theme": {
        "bg": "#1e1f22",
        "panel_bg": "#2b2d30",
        "edit_bg": "#26282b",
        "gutter_bg": "#26282b",
        "gutter_fg": "#5c6370",
        "fg": "#dcdfe4",
        "muted_fg": "#8a8f98",
        "sel_bg": "#3a4048",
        "console_bg": "#18191b",
        "accent": "#4a9eff",
        "font_family": "Consolas",
        "font_size": 11,
    },
}

_override_path = None


def set_config_path(path):
    """Force a specific config file location (e.g. from a --config flag)."""
    global _override_path
    _override_path = Path(path).expanduser() if path else None


def default_config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIRNAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRNAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "peer-to-code"


def get_config_path() -> Path:
    if _override_path:
        return _override_path
    env = os.environ.get("PEER_TO_CODE_CONFIG")
    if env:
        return Path(env).expanduser()
    return default_config_dir() / "config.json"


def _deep_merge(base, override):
    result = {}
    for key, value in base.items():
        if isinstance(value, dict):
            sub_override = override.get(key)
            if not isinstance(sub_override, dict):
                sub_override = {}
            result[key] = _deep_merge(value, sub_override)
        elif key in override:
            result[key] = override[key]
        else:
            result[key] = value
    for key, value in override.items():
        if key not in result:
            result[key] = value
    return result


def load_config():
    path = get_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    cfg = _deep_merge(DEFAULTS, data)
    _migrate_window_geometry(cfg)
    return cfg


def _migrate_window_geometry(cfg):
    """Older configs stored one combined "window_geometry" string
    ("WxH+X+Y"). Splits it into the separate window_size/window_position
    fields (each independently save-able/deletable now) the first time an
    old config is loaded, then drops the old key."""
    ui = cfg.get("ui", {})
    old = ui.pop("window_geometry", None)
    if not old or ui.get("window_size") or ui.get("window_position"):
        return
    m = re.match(r"^(\d+x\d+)([+-]\d+[+-]\d+)$", old)
    if m:
        ui["window_size"], ui["window_position"] = m.group(1), m.group(2)


def save_config(cfg):
    path = get_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
        tmp.replace(path)
        return True
    except OSError:
        return False


def add_recent_project(cfg, path):
    """Move `path` to the front of cfg's recent-projects list, deduped and
    capped at MAX_RECENT_PROJECTS. Mutates and returns the list in place."""
    recents = cfg.setdefault("connection", {}).setdefault("recent_projects", [])
    path = str(path)
    recents[:] = [p for p in recents if p != path]
    recents.insert(0, path)
    del recents[MAX_RECENT_PROJECTS:]
    return recents


def remove_recent_project(cfg, path):
    """Drops `path` from cfg's recent-projects list, if present."""
    recents = cfg.setdefault("connection", {}).setdefault("recent_projects", [])
    recents[:] = [p for p in recents if p != str(path)]
    return recents

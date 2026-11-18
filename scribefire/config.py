"""Configuration handling for Scribefire.

The config lives as a single JSON file in the per-OS user config directory
(resolved with platformdirs).  Nothing secret is ever stored here - the LLM
API key lives in the OS keychain (see :mod:`scribefire.keychain`).
"""

from __future__ import annotations

import copy
import json
import os
import platform
import shutil
import threading
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # pragma: no cover - trivial import shim
    from platformdirs import user_config_dir, user_cache_dir, user_log_dir
except Exception:  # pragma: no cover - platformdirs is a hard dependency
    def _fallback(kind: str, appname: str) -> str:
        home = Path.home()
        if platform.system() == "Darwin":
            return str(home / "Library" / "Application Support" / appname)
        if platform.system() == "Windows":
            base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
            return str(Path(base) / appname / kind.capitalize())
        return str(home / ".config" / appname)

    def user_config_dir(appname: str = None, **_: Any) -> str:
        return _fallback("config", appname or "scribefire")

    def user_cache_dir(appname: str = None, **_: Any) -> str:
        return _fallback("cache", appname or "scribefire")

    def user_log_dir(appname: str = None, **_: Any) -> str:
        return _fallback("log", appname or "scribefire")


APP_NAME = "Scribefire"
APP_SLUG = "scribefire"
#: Name used before the product was renamed.  An install made under the
#: old name is migrated into the new directory the first time the app runs,
#: so the user keeps their settings, their cached models and their logs.
LEGACY_APP_NAME = "Likhari"

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"


def _legacy_dir(kind: str) -> Path:
    """Directory the app used under its previous name."""
    if kind == "config":
        return Path(user_config_dir(LEGACY_APP_NAME, appauthor=False))
    if kind == "cache":
        return Path(user_cache_dir(LEGACY_APP_NAME, appauthor=False))
    return Path(user_log_dir(LEGACY_APP_NAME, appauthor=False))


def _migrate(target: Path, kind: str) -> Path:
    """Move a pre-rename directory to the new app directory, exactly once.

    Runs before anything is read or written, so an existing install keeps its
    settings, its downloaded models and its log history across the rename.
    Any failure here is ignored: the app must still start.
    """
    try:
        if target.exists():
            return target
        legacy = _legacy_dir(kind)
        if legacy.exists() and legacy != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(target))
    except Exception:
        pass
    return target


def config_dir() -> Path:
    """Directory holding the Scribefire config file."""
    return _migrate(Path(user_config_dir(APP_NAME, appauthor=False)), "config")


def cache_dir() -> Path:
    """Directory used to cache downloaded models."""
    return _migrate(Path(user_cache_dir(APP_NAME, appauthor=False)), "cache")


def log_dir() -> Path:
    """Directory used for the error log."""
    return _migrate(Path(user_log_dir(APP_NAME, appauthor=False)), "log")


def config_path() -> Path:
    return config_dir() / "config.json"


def model_cache_dir() -> Path:
    """Where Whisper and Vosk models are downloaded to."""
    return cache_dir() / "models"


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

#: Hotkey defaults differ per platform: Right Alt on Windows, Right Option
#: (which is the same physical key) on macOS.
DEFAULT_HOTKEY = "alt_r"

EXAMPLE_DICTIONARY: List[List[str]] = [
    ["pie torch", "PyTorch"],
    ["cloud", "Claude"],
    ["zero flow", "ZeroFlow"],
    ["jason", "JSON"],
    ["postman", "Postman"],
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "hotkey": DEFAULT_HOTKEY,
    "preroll_ms": 500,
    "hud_enabled": True,
    "live_preview": False,
    "start_on_login": False,
    "mute": False,
    "microphone": None,          # device id or name, None = system default
    "whisper_model": "base",     # tiny | base | small | medium
    "compute_type": "int8",
    "device_type": "auto",       # auto | cpu | cuda
    "cleanup": {
        "mode": "auto",          # auto | groq | ollama | rule
        "groq_base_url": "https://api.groq.com/openai/v1",
        "groq_model": "openai/gpt-oss-20b",
        "groq_timeout": 3.0,
        "ollama_base_url": "http://localhost:11434",
        "ollama_model": "llama3.2:3b",
        "ollama_timeout": 10.0,
    },
    "dictionary": copy.deepcopy(EXAMPLE_DICTIONARY),
    "inject": {
        "paste_delay_ms": 60,
        "clipboard_confirm_ms": 60,
        "clipboard_retries": 5,
        "restore_clipboard": True,
        "restore_delay_ms": 400,
    },
    "stats": {
        "words_today": 0,
        "words_total": 0,
        "day": "",
    },
    "logging": {
        "level": "INFO",
    },
}


def default_config() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *overlay* into a copy of *base*, recursing into nested dicts."""
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# Config object
# --------------------------------------------------------------------------

class Config:
    """Thread-safe-ish wrapper around the JSON config.

    The settings window mutates a *copy* and calls :meth:`save` when the user
    hits Save; runtime components read live values through this object.
    """

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[Path] = None):
        self._data = _deep_merge(default_config(), data or {})
        self._path = Path(path) if path else config_path()
        self._lock = threading.RLock()

    # -- persistence ---------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = Path(path) if path else config_path()
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    data = raw
            except Exception:
                # A corrupt config must never stop the app from starting.
                # Keep a copy so the user can inspect it, then start clean.
                try:
                    backup = path.with_suffix(".json.corrupt")
                    path.replace(backup)
                except Exception:
                    pass
                data = {}
        return cls(data, path=path)

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else self._path
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = json.dumps(self._data, indent=2, sort_keys=False, ensure_ascii=False)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
        return target

    # -- raw access ----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def snapshot(self) -> Dict[str, Any]:
        return self.data

    def get(self, path: str, default: Any = None) -> Any:
        """Read a dotted path, e.g. ``cleanup.groq_model``."""
        with self._lock:
            node: Any = self._data
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def set(self, path: str, value: Any) -> None:
        with self._lock:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = copy.deepcopy(value)

    def update(self, values: Dict[str, Any]) -> None:
        """Merge a flat dotted-path mapping into the config."""
        for key, value in values.items():
            self.set(key, value)

    def replace(self, values: Dict[str, Any]) -> None:
        """Replace the whole config payload (already-merged with defaults)."""
        with self._lock:
            self._data = _deep_merge(default_config(), values)

    # -- hotkey --------------------------------------------------------

    @property
    def hotkey(self) -> str:
        return str(self.get("hotkey", DEFAULT_HOTKEY))

    @hotkey.setter
    def hotkey(self, value: str) -> None:
        self.set("hotkey", value)

    @property
    def preroll_ms(self) -> int:
        return int(self.get("preroll_ms", 500))

    @preroll_ms.setter
    def preroll_ms(self, value: int) -> None:
        self.set("preroll_ms", max(0, min(1000, int(value))))

    # -- hud / preview -------------------------------------------------

    @property
    def hud_enabled(self) -> bool:
        return bool(self.get("hud_enabled", True))

    @hud_enabled.setter
    def hud_enabled(self, value: bool) -> None:
        self.set("hud_enabled", bool(value))

    @property
    def live_preview(self) -> bool:
        return bool(self.get("live_preview", False))

    @live_preview.setter
    def live_preview(self, value: bool) -> None:
        self.set("live_preview", bool(value))

    @property
    def start_on_login(self) -> bool:
        return bool(self.get("start_on_login", False))

    @start_on_login.setter
    def start_on_login(self, value: bool) -> None:
        self.set("start_on_login", bool(value))

    @property
    def muted(self) -> bool:
        return bool(self.get("mute", False))

    @muted.setter
    def muted(self, value: bool) -> None:
        self.set("mute", bool(value))

    # -- transcription -------------------------------------------------

    @property
    def whisper_model(self) -> str:
        return str(self.get("whisper_model", "base"))

    @whisper_model.setter
    def whisper_model(self, value: str) -> None:
        self.set("whisper_model", str(value))

    @property
    def compute_type(self) -> str:
        return str(self.get("compute_type", "int8"))

    @property
    def device_type(self) -> str:
        return str(self.get("device_type", "auto"))

    @property
    def microphone(self) -> Optional[str]:
        return self.get("microphone", None)

    @microphone.setter
    def microphone(self, value: Optional[str]) -> None:
        self.set("microphone", value)

    # -- cleanup -------------------------------------------------------

    @property
    def cleanup(self) -> Dict[str, Any]:
        return self.get("cleanup", {}) or {}

    @property
    def cleanup_mode(self) -> str:
        return str(self.cleanup.get("mode", "auto"))

    # -- dictionary ----------------------------------------------------

    @property
    def dictionary(self) -> List[List[str]]:
        raw = self.get("dictionary", []) or []
        pairs: List[List[str]] = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append([str(item[0]), str(item[1])])
        return pairs

    @dictionary.setter
    def dictionary(self, value: Iterable[Iterable[str]]) -> None:
        pairs = [[str(a), str(b)] for a, b in value]
        self.set("dictionary", pairs)

    # -- inject --------------------------------------------------------

    @property
    def inject(self) -> Dict[str, Any]:
        return self.get("inject", {}) or {}

    # -- stats ---------------------------------------------------------

    def add_words(self, count: int) -> None:
        """Accumulate word counts for the footer display."""
        with self._lock:
            stats = self._data.setdefault("stats", {})
            today = date.today().isoformat()
            if stats.get("day") != today:
                stats["day"] = today
                stats["words_today"] = 0
            stats["words_today"] = int(stats.get("words_today", 0)) + count
            stats["words_total"] = int(stats.get("words_total", 0)) + count

    @property
    def stats(self) -> Dict[str, Any]:
        return self.get("stats", {}) or {}


# --------------------------------------------------------------------------
# Hotkey names
# --------------------------------------------------------------------------

#: Logical hotkey name -> (windows pynput key, macos pynput key).
HOTKEY_ALIASES: Dict[str, Tuple[str, str]] = {
    "alt_r": ("alt_r", "alt_r"),        # Right Alt / Right Option
    "alt_l": ("alt_l", "alt_l"),
    "shift_r": ("shift_r", "shift_r"),
    "shift_l": ("shift_l", "shift_l"),
    "ctrl_r": ("ctrl_r", "ctrl_r"),
    "ctrl_l": ("ctrl_l", "ctrl_l"),
    "cmd_r": ("cmd_r", "cmd_r"),
    "cmd_l": ("cmd_l", "cmd_l"),
    "f1": ("f1", "f1"),
    "f2": ("f2", "f2"),
    "f3": ("f3", "f3"),
    "f4": ("f4", "f4"),
    "f5": ("f5", "f5"),
    "f6": ("f6", "f6"),
    "f7": ("f7", "f7"),
    "f8": ("f8", "f8"),
    "f9": ("f9", "f9"),
    "f10": ("f10", "f10"),
    "f11": ("f11", "f11"),
    "f12": ("f12", "f12"),
}


def hotkey_choices() -> List[str]:
    return list(HOTKEY_ALIASES.keys())


def resolve_hotkey(name: str) -> str:
    """Map a logical hotkey name to the pynput key name for this OS."""
    key = (name or DEFAULT_HOTKEY).strip().lower()
    if key in HOTKEY_ALIASES:
        win, mac = HOTKEY_ALIASES[key]
        return mac if IS_MACOS else win
    return key

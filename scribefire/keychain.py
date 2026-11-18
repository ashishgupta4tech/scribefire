"""Secret storage in the OS keychain.

The LLM API key never goes into the config file.  It lives in the Windows
Credential Manager or the macOS Keychain via ``keyring``.  If ``keyring`` is
not installed the key falls back to a separate ``credentials.json`` in the
config directory with owner-only permissions - never inside ``config.json``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional

from .config import config_dir
from .logs import get_logger

log = get_logger("keychain")

SERVICE = "scribefire"
#: Service name used before the rename.  A key stored there is moved to the
#: new service the first time it is read, so nobody has to re-enter it.
LEGACY_SERVICE = "likhari"
ACCOUNT = "groq-api-key"

_FALLBACK_FILE = "credentials.json"


def _fallback_path() -> Path:
    return config_dir() / _FALLBACK_FILE


def _read_fallback() -> Optional[str]:
    path = _fallback_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = data.get("groq_api_key")
        return str(value) if value else None
    except Exception:
        return None


def _write_fallback(value: Optional[str]) -> bool:
    path = _fallback_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"groq_api_key": value or ""}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        if os.name == "posix":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return True
    except Exception as exc:
        log.error("could not write the keychain fallback file: %s", exc)
        return False


def get_api_key() -> str:
    """Return the stored API key, or an empty string."""
    try:
        import keyring

        value = keyring.get_password(SERVICE, ACCOUNT)
        if value:
            return value
        # Pre-rename installs stored the key under the old service name.
        legacy = keyring.get_password(LEGACY_SERVICE, ACCOUNT)
        if legacy:
            try:
                keyring.set_password(SERVICE, ACCOUNT, legacy)
            except Exception:
                pass
            return legacy
    except Exception as exc:
        log.debug("keyring unavailable (%s), using the file fallback", exc)
    return _read_fallback() or ""


def set_api_key(value: Optional[str]) -> bool:
    """Store (or clear) the API key.  Never touches the config file."""
    value = (value or "").strip()
    try:
        import keyring

        if value:
            keyring.set_password(SERVICE, ACCOUNT, value)
        else:
            try:
                keyring.delete_password(SERVICE, ACCOUNT)
            except Exception:
                pass
        # Keep the fallback in sync so both paths agree.
        _write_fallback(value or None)
        return True
    except Exception as exc:
        log.warning("keyring unavailable (%s), using the file fallback", exc)
        return _write_fallback(value or None)


def has_api_key() -> bool:
    return bool(get_api_key())

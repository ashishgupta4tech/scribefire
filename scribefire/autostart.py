"""Start-on-login support.

Windows: a value under ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.
macOS:   a LaunchAgent plist in ``~/Library/LaunchAgents``.
"""

from __future__ import annotations

import os
import plistlib
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .logs import get_logger

log = get_logger("autostart")

APP_NAME = "Scribefire"
BUNDLE_ID = "com.scribefire.app"

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command() -> list:
    """The command that starts Scribefire at login."""
    entry = Path(sys.argv[0]).resolve()
    if getattr(sys, "frozen", False):
        return [str(entry)]
    if IS_WINDOWS:
        pythonw = entry.with_name("pythonw.exe")
        if pythonw.exists():
            return [str(pythonw), "-m", "scribefire"]
        return [sys.executable, "-m", "scribefire"]
    return [sys.executable, "-m", "scribefire"]


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

def _windows_set(enabled: bool) -> bool:
    try:
        import winreg
    except Exception as exc:
        log.error("winreg is not available: %s", exc)
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _WINDOWS_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            if enabled:
                command = subprocess.list2cmdline(_launch_command())
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as exc:
        log.error("could not update the Windows autostart entry: %s", exc)
        return False


def _windows_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------
# macOS
# --------------------------------------------------------------------------

def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % BUNDLE_ID)


def _macos_set(enabled: bool) -> bool:
    path = _macos_plist_path()
    try:
        if not enabled:
            if path.exists():
                path.unlink()
                subprocess.run(["launchctl", "unload", str(path)], capture_output=True, timeout=10)
            return True

        payload = {
            "Label": BUNDLE_ID,
            "ProgramArguments": _launch_command(),
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump(payload, fh)
        subprocess.run(["launchctl", "load", str(path)], capture_output=True, timeout=10)
        return True
    except Exception as exc:
        log.error("could not update the macOS LaunchAgent: %s", exc)
        return False


def _macos_enabled() -> bool:
    return _macos_plist_path().exists()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def set_start_on_login(enabled: bool) -> bool:
    """Enable or disable starting Scribefire at login."""
    if IS_WINDOWS:
        return _windows_set(bool(enabled))
    if IS_MACOS:
        return _macos_set(bool(enabled))
    log.info("start-on-login is not supported on this platform")
    return False


def is_start_on_login_enabled() -> bool:
    if IS_WINDOWS:
        return _windows_enabled()
    if IS_MACOS:
        return _macos_enabled()
    return False


def describe() -> str:
    if IS_WINDOWS:
        return "HKCU\\%s\\%s" % (_WINDOWS_RUN_KEY, APP_NAME)
    if IS_MACOS:
        return str(_macos_plist_path())
    return "not supported on this platform"

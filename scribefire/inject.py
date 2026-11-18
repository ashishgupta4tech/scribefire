"""Insert dictated text at the cursor of whatever app has focus.

The paste sequence has two classic timing bugs that this module exists to
avoid:

1.  The clipboard write must actually land before the paste shortcut is
    sent, otherwise the paste inserts whatever was on the clipboard before.
    We read the clipboard back and retry the write until it matches.
2.  The original clipboard may only be restored *after* the paste has
    completed - restoring it immediately after sending the shortcut throws
    the dictated text away before the target app reads it.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from .logs import get_logger

log = get_logger("inject")

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"


# --------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------

def _pyperclip():
    try:
        import pyperclip

        return pyperclip
    except Exception as exc:  # pragma: no cover - depends on the platform
        log.error("pyperclip is not available: %s", exc)
        return None


def read_clipboard() -> Optional[str]:
    clipboard = _pyperclip()
    if clipboard is None:
        return None
    try:
        return clipboard.paste()
    except Exception as exc:
        log.warning("could not read the clipboard: %s", exc)
        return None


def write_clipboard(text: str) -> bool:
    clipboard = _pyperclip()
    if clipboard is None:
        return False
    try:
        clipboard.copy(text)
        return True
    except Exception as exc:
        log.warning("could not write to the clipboard: %s", exc)
        return False


def _clipboard_matches(text: str) -> bool:
    clipboard = _pyperclip()
    if clipboard is None:
        return False
    try:
        current = clipboard.paste()
    except Exception:
        return False
    if current == text:
        return True
    # Some clipboard managers normalise trailing whitespace.
    return isinstance(current, str) and current.rstrip("\n") == text.rstrip("\n")


def _write_clipboard_confirmed(text: str, confirm_ms: int = 60, retries: int = 5) -> bool:
    """Write *text* to the clipboard and confirm it actually landed."""
    for attempt in range(max(1, retries)):
        if not write_clipboard(text):
            return False
        # Give the clipboard subsystem a moment to settle, then verify.
        time.sleep(max(0, confirm_ms) / 1000.0)
        if _clipboard_matches(text):
            log.info("clipboard write confirmed on attempt %d", attempt + 1)
            return True
        log.warning("clipboard write did not land (attempt %d), retrying", attempt + 1)
    log.error("clipboard write never confirmed - refusing to paste")
    return False


# --------------------------------------------------------------------------
# Paste shortcut
# --------------------------------------------------------------------------

def _paste_windows() -> bool:
    """Send Ctrl+V using SendInput."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    ULONG_PTR = ctypes.POINTER(wintypes.ULONG)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    # The union must contain every member of the real INPUT structure:
    # its size is what sizeof(INPUT) is derived from, and SendInput checks
    # the size it is given against the one it expects (40 bytes on x64).
    class INPUTUNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("data", INPUTUNION)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_V = 0x56

    def key(vk: int, up: bool) -> None:
        flags = KEYEVENTF_KEYUP if up else 0
        event = INPUT(type=INPUT_KEYBOARD)
        event.data.ki = KEYBDINPUT(
            wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None
        )
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            raise OSError(
                "SendInput delivered %d of 1 events (error %d)"
                % (sent, ctypes.windll.kernel32.GetLastError())
            )

    def keybd_fallback(vk: int, up: bool) -> None:
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)

    try:
        key(VK_CONTROL, False)
        key(VK_V, False)
        time.sleep(0.02)
        key(VK_V, True)
        key(VK_CONTROL, True)
        return True
    except Exception as exc:
        log.warning("SendInput failed (%s) - trying keybd_event", exc)
        try:
            keybd_fallback(VK_CONTROL, False)
            keybd_fallback(VK_V, False)
            time.sleep(0.02)
            keybd_fallback(VK_V, True)
            keybd_fallback(VK_CONTROL, True)
            return True
        except Exception as exc2:
            log.error("could not send the paste shortcut: %s", exc2)
            return False


def _paste_macos() -> bool:
    """Send Cmd+V using pynput, falling back to osascript."""
    try:
        from pynput.keyboard import Controller, Key

        controller = Controller()
        with controller.pressed(Key.cmd):
            controller.press("v")
            controller.release("v")
        return True
    except Exception as exc:
        log.warning("pynput paste failed (%s), trying osascript", exc)

    if shutil.which("osascript"):
        script = 'tell application "System Events" to keystroke "v" using command down'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            return True
        except Exception as exc:
            log.error("osascript paste failed: %s", exc)
    return False


def send_paste_shortcut() -> bool:
    """Send the platform's paste shortcut to the focused application."""
    if IS_MACOS:
        return _paste_macos()
    if IS_WINDOWS:
        return _paste_windows()
    log.error("unsupported platform: %s", sys.platform)
    return False


# --------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------

class TextInjector:
    """Pastes text at the cursor of the focused application."""

    def __init__(self, config=None, logger=None):
        self.config = config
        self.log = logger or log
        self._lock = threading.RLock()
        self._generation = 0

    def _settings(self):
        if self.config is None:
            return {
                "paste_delay_ms": 60,
                "clipboard_confirm_ms": 60,
                "clipboard_retries": 5,
                "restore_clipboard": True,
                "restore_delay_ms": 400,
            }
        settings = dict(self.config.inject or {})
        settings.setdefault("paste_delay_ms", 60)
        settings.setdefault("clipboard_confirm_ms", 60)
        settings.setdefault("clipboard_retries", 5)
        settings.setdefault("restore_clipboard", True)
        settings.setdefault("restore_delay_ms", 400)
        return settings

    def inject(self, text: str) -> bool:
        """Insert *text* at the cursor.  Returns True when it was pasted."""
        if not text:
            return False
        settings = self._settings()
        delay_ms = int(settings.get("paste_delay_ms", 60))
        confirm_ms = int(settings.get("clipboard_confirm_ms", 60))
        retries = int(settings.get("clipboard_retries", 5))
        restore = bool(settings.get("restore_clipboard", True))
        restore_delay_ms = int(settings.get("restore_delay_ms", 400))

        with self._lock:
            self._generation += 1
            generation = self._generation

        original = read_clipboard() if restore else None

        if not _write_clipboard_confirmed(text, confirm_ms=confirm_ms, retries=retries):
            self.log.error("aborting paste: the clipboard write did not land")
            return False

        # Let the target application settle before the keystroke lands.
        time.sleep(max(0, delay_ms) / 1000.0)

        if not send_paste_shortcut():
            self.log.error("paste shortcut could not be sent")
            return False

        # The target application reads the clipboard when it handles the paste
        # event, which happens *after* this keystroke was delivered.  Putting
        # the old clipboard back too early makes the application paste the
        # old contents instead - the second classic timing bug.  So the
        # restore is deferred and happens on a background thread.
        if restore and original is not None:
            thread = threading.Thread(
                target=self._restore_later,
                args=(generation, original, restore_delay_ms),
                name="scribefire-clipboard-restore",
                daemon=True,
            )
            thread.start()

        self.log.info("pasted %d characters", len(text))
        return True

    def _restore_later(self, generation: int, original: str, delay_ms: int) -> None:
        """Put the user's clipboard back once the paste had time to happen."""
        time.sleep(max(0, delay_ms) / 1000.0)
        with self._lock:
            if generation != self._generation:
                # A newer dictation has already overwritten the clipboard.
                return
        try:
            write_clipboard(original)
        except Exception as exc:
            self.log.warning("could not restore the clipboard: %s", exc)

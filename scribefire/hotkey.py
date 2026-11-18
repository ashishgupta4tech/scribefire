"""Global push-to-talk hotkey listener.

Uses ``pynput`` so the key works while any other application has focus.  The
key is *not* swallowed: pynput does not suppress events, so the target
application still receives the key press exactly as it would normally.
"""

from __future__ import annotations

import platform
import threading
from typing import Callable, Optional

from .config import IS_MACOS, resolve_hotkey
from .logs import get_logger, guarded

log = get_logger("hotkey")


# --------------------------------------------------------------------------
# macOS permission detection
# --------------------------------------------------------------------------

MACOS_ACCESSIBILITY_HELP = (
    "Scribefire needs Accessibility permission to see the hotkey.\n"
    "  System Settings > Privacy & Security > Accessibility\n"
    "  Add/enable Python (or the Scribefire app) there, then relaunch Scribefire."
)

MACOS_INPUT_MONITORING_HELP = (
    "Scribefire needs Input Monitoring permission to see the hotkey.\n"
    "  System Settings > Privacy & Security > Input Monitoring\n"
    "  Add/enable Python (or the Scribefire app) there, then relaunch Scribefire."
)


def check_macos_permissions(verbose: bool = True) -> Optional[bool]:
    """Best-effort check of the macOS privacy permissions Scribefire needs.

    Returns ``True`` when both permissions look granted, ``False`` when a
    missing permission was detected, and ``None`` when the check cannot be
    performed on this machine.
    """
    if not IS_MACOS:
        return None

    granted = True
    checked = False

    # Accessibility: AXIsProcessTrusted()
    try:  # pragma: no cover - macOS only
        import Quartz  # type: ignore

        fn = getattr(Quartz, "AXIsProcessTrusted", None)
        if fn is not None:
            checked = True
            if not fn():
                granted = False
                if verbose:
                    print("\n[permissions] " + MACOS_ACCESSIBILITY_HELP + "\n")
    except Exception:
        pass

    # Input Monitoring: CGPreflightListenEventAccess() (macOS 10.15+)
    try:  # pragma: no cover - macOS only
        import Quartz  # type: ignore

        fn = getattr(Quartz, "CGPreflightListenEventAccess", None)
        if fn is not None:
            checked = True
            if not fn():
                granted = False
                if verbose:
                    print("\n[permissions] " + MACOS_INPUT_MONITORING_HELP + "\n")
    except Exception:
        pass

    if verbose and not checked:
        log.info("could not verify macOS permissions programmatically")
    return granted if checked else None


# --------------------------------------------------------------------------
# Key resolution
# --------------------------------------------------------------------------

def resolve_key_object(key_name: str):
    """Return the ``pynput`` key object for a logical hotkey name."""
    from pynput import keyboard

    resolved = resolve_hotkey(key_name)
    key_obj = getattr(keyboard.Key, resolved, None)
    if key_obj is not None:
        return key_obj

    # macOS pynput sometimes exposes only the generic name.
    if resolved.endswith("_r"):
        fallback = getattr(keyboard.Key, resolved[:-2], None)
        if fallback is not None:
            return fallback
    return None


def _key_matches(key, target) -> bool:
    if key is None or target is None:
        return False
    if key == target:
        return True
    # Some layouts report Right Alt as alt_gr (ctrl_r + alt_r).
    try:
        from pynput import keyboard

        if target == keyboard.Key.alt_r and key == keyboard.Key.alt_gr:
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# Listener
# --------------------------------------------------------------------------

class HotkeyListener:
    """Fires ``on_press`` / ``on_release`` when the hotkey goes down / up."""

    def __init__(
        self,
        key_name: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        logger=None,
    ):
        self.key_name = key_name
        self.on_press = on_press
        self.on_release = on_release
        self.log = logger or log
        self._listener = None
        self._held = False
        self._lock = threading.RLock()
        self._started = False

    # -- lifecycle -----------------------------------------------------

    def start(self) -> bool:
        if self._started:
            return True
        try:
            from pynput import keyboard
        except Exception as exc:
            self.log.error("pynput is not available: %s", exc)
            return False

        target = resolve_key_object(self.key_name)
        if target is None:
            self.log.error("unknown hotkey %r", self.key_name)
            return False

        @guarded("hotkey")
        def _on_press(key):
            if not _key_matches(key, target):
                return
            with self._lock:
                if self._held:
                    return  # auto-repeat
                self._held = True
            self.log.info("hotkey down")
            try:
                self.on_press()
            except Exception:
                self.log.exception("hotkey press handler failed")

        @guarded("hotkey")
        def _on_release(key):
            if not _key_matches(key, target):
                return
            with self._lock:
                if not self._held:
                    return
                self._held = False
            self.log.info("hotkey up")
            try:
                self.on_release()
            except Exception:
                self.log.exception("hotkey release handler failed")

        try:
            self._listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
            self._listener.daemon = True
            self._listener.start()
            self._started = True
            self.log.info("hotkey listener started (%s)", self.key_name)
            return True
        except Exception as exc:
            self.log.error("could not start hotkey listener: %s", exc)
            return False

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._held = False
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    # -- state ---------------------------------------------------------

    def is_held(self) -> bool:
        with self._lock:
            return self._held


def default_hotkey_label() -> str:
    if platform.system() == "Darwin":
        return "Right Option"
    return "Right Alt"

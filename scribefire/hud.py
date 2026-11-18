"""Platform-neutral facade over the two HUD backends.

``hud_macos`` uses AppKit (an ``NSPanel``), ``hud_windows`` uses a Tkinter
overlay with ``WS_EX_NOACTIVATE``.  Both draw with the shared geometry in
:mod:`scribefire.hud_common`, so the wave math exists exactly once.
"""

from __future__ import annotations

import platform
import sys
from typing import Optional

from .logs import get_logger

log = get_logger("hud")

HUD_STATES = ("listening", "processing", "done")


def is_macos() -> bool:
    return platform.system() == "Darwin"


def create_hud(config=None, logger=None, root=None):
    """Create the HUD overlay for this platform.

    *root* is the shared Tk root (Windows only).  Passing it keeps the app
    on a single Tk interpreter - the settings window and the overlay share
    one.

    Returns ``None`` when the backend cannot be imported - the app stays
    fully usable without the overlay.
    """
    if is_macos():
        try:
            from .hud_macos import HudOverlay

            return HudOverlay(config, logger=logger or log)
        except Exception as exc:
            log.error("the macOS HUD could not be created: %s", exc)
            return None
    try:
        from .hud_windows import HudOverlay
    except Exception as exc:
        log.error("the Windows HUD could not be created: %s", exc)
        return None

    if root is not None:
        try:
            return HudOverlay(config, logger=logger or log, root=root)
        except TypeError:
            log.info("the Windows HUD does not accept a shared root")
    return HudOverlay(config, logger=logger or log)

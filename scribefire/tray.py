"""System tray icon for Scribefire.

Threading rules (these matter on macOS, where a mistake fails *silently*):

* ``pystray`` must run on the **main** thread - :meth:`TrayIcon.run` blocks
  there and owns the main loop for the whole application.
* The hotkey listener runs on a **background** thread.  It therefore calls
  :meth:`TrayIcon.update_status` / :meth:`TrayIcon.update_state` from that
  background thread.  Both are thread safe and never raise.
* The menu callbacks are executed by pystray, potentially on the main thread.
  They must not block, so every callback body is guarded with ``try/except``
  and the controller is expected to marshal its own work.

The icon image is generated at runtime with Pillow - no binary icon files are
shipped.
"""

from __future__ import annotations

import platform
import threading
from typing import Any, Callable, Dict, Optional

from PIL import Image, ImageDraw

from .logs import get_logger

try:  # pragma: no cover - pystray is a hard dependency on desktop builds
    import pystray
except Exception:  # pragma: no cover - keeps the module importable headless
    pystray = None  # type: ignore[assignment]

log = get_logger("tray")

IS_MACOS = platform.system() == "Darwin"

#: Accent colour per application state.
STATE_COLORS: Dict[str, tuple] = {
    "idle": (128, 134, 142),
    "recording": (214, 58, 58),
    "processing": (232, 168, 48),
}

#: The states the tray understands.  Anything else falls back to ``idle``.
VALID_STATES = ("idle", "recording", "processing")

ICON_SIZE = 64


class TrayIcon:
    """A ``pystray`` status icon bound to the application controller.

    Parameters
    ----------
    controller:
        The application object.  It must provide ``status_text()``,
        ``state``, ``toggle_mute()``, ``open_settings()`` and ``quit()``.
    icon_provider:
        Optional callable ``icon_provider(state) -> PIL.Image.Image`` used
        instead of the built-in generated icon.
    no_tray:
        Headless mode.  :meth:`run` returns immediately and no icon is
        created.
    """

    def __init__(
        self,
        controller: Any,
        icon_provider: Optional[Callable[[str], Image.Image]] = None,
        no_tray: bool = False,
    ):
        self._controller = controller
        self._icon_provider = icon_provider
        self._no_tray = bool(no_tray)

        self._lock = threading.RLock()
        self._status = "Ready"
        self._state = "idle"
        self._muted = False
        self._icon: Any = None
        self._active = False
        self._stopped = threading.Event()

        self._status = self._read_controller_status()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while the tray icon is live."""
        with self._lock:
            return self._active

    def run(self) -> None:
        """Create the icon and block until :meth:`stop` is called.

        Must be called from the **main** thread on macOS - pystray's
        AppKit backend needs the main thread for its event loop.  In headless
        mode (``no_tray=True``) this returns immediately.
        """
        if self._no_tray:
            log.info("headless mode: no tray icon will be created")
            return

        if pystray is None:  # pragma: no cover - depends on the install
            log.error("pystray is not available - running without a tray icon")
            return

        if not threading.current_thread() is threading.main_thread():
            log.warning("TrayIcon.run() was not called on the main thread; "
                        "this can misbehave on macOS")

        try:
            image = self._build_image(self._state)
            menu = self._build_menu()
            icon = pystray.Icon("scribefire", image, self._status, menu)
            with self._lock:
                self._icon = icon
                self._active = True
            log.info("tray icon started")
            icon.run()
        except Exception:
            log.exception("the tray icon stopped with an error")
        finally:
            with self._lock:
                self._active = False
                self._icon = None
            self._stopped.set()

    def stop(self) -> None:
        """Stop the tray icon.  Safe to call from any thread."""
        self._stopped.set()
        with self._lock:
            icon = self._icon
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            log.exception("could not stop the tray icon")

    def update_status(self, text: str) -> None:
        """Set the status line shown in the menu (and the icon tooltip).

        Safe to call from any thread and never raises.
        """
        try:
            with self._lock:
                self._status = str(text or "")
            self._refresh()
        except Exception:
            log.exception("could not update the tray status")

    def update_state(self, state: str) -> None:
        """Change the tray state: ``idle``, ``recording`` or ``processing``.

        Safe to call from any thread and never raises.
        """
        try:
            state = str(state or "idle").strip().lower()
            if state not in VALID_STATES:
                state = "idle"
            with self._lock:
                self._state = state
            self._apply_icon()
            self._refresh()
        except Exception:
            log.exception("could not update the tray state")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_controller_status(self) -> str:
        try:
            text = self._controller.status_text()
            return str(text or "Ready")
        except Exception:
            log.exception("could not read the controller status")
            return "Ready"

    def _is_muted(self) -> bool:
        """Best-effort read of the current mute state."""
        for attribute in ("muted", "is_muted"):
            value = getattr(self._controller, attribute, None)
            if isinstance(value, bool):
                return value
        config = getattr(self._controller, "config", None)
        if isinstance(config, dict) and "mute" in config:
            return bool(config["mute"])
        value = getattr(config, "muted", None)
        if isinstance(value, bool):
            return value
        return self._muted

    # -- image ---------------------------------------------------------

    def _build_image(self, state: str) -> Image.Image:
        """Return the icon image for *state*."""
        provider = self._icon_provider
        if provider is not None:
            try:
                image = provider(state)
                if image is not None:
                    return image
            except Exception:
                log.exception("the icon provider failed - using the built-in icon")
        return self._default_icon(state)

    @staticmethod
    def _default_icon(state: str) -> Image.Image:
        """Draw a rounded square in the accent colour of *state*."""
        size = ICON_SIZE
        accent = STATE_COLORS.get(state, STATE_COLORS["idle"])
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=accent + (255,))

        # A tiny microphone glyph, drawn so no binary asset is needed.
        white = (255, 255, 255, 255)
        draw.ellipse((26, 11, 38, 23), fill=white)
        draw.rounded_rectangle((29, 23, 35, 39), radius=3, fill=white)
        draw.arc((22, 21, 42, 43), start=0, end=180, fill=white, width=3)
        draw.line((32, 43, 32, 49), fill=white, width=3)
        draw.line((25, 49, 39, 49), fill=white, width=3)
        return image

    # -- menu ----------------------------------------------------------

    def _build_menu(self) -> Any:
        """Build the tray menu.

        The item texts are dynamic callables so the menu can be refreshed
        with :meth:`pystray.Icon.update_menu` instead of being rebuilt.
        """
        if pystray is None:  # pragma: no cover
            return None

        def status_text(_item: Any = None) -> str:
            with self._lock:
                return self._status or "Ready"

        def mute_text(_item: Any = None) -> str:
            return "Unmute" if self._is_muted() else "Mute"

        return pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(mute_text, self._on_toggle_mute),
            pystray.MenuItem("Open Settings...", self._on_open_settings),
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _on_tray_thread(self, action: Callable[[], None]) -> None:
        """Run *action* on the thread that owns the tray icon.

        On macOS the AppKit objects may only be touched from the main
        thread, so the call is queued onto the main operation queue.  On
        every other platform pystray's own update calls are thread safe and
        are executed directly.
        """
        if IS_MACOS:
            try:
                from Foundation import NSOperationQueue

                NSOperationQueue.mainQueue().addOperationWithBlock_(action)
                return
            except Exception:
                log.debug("could not queue the tray update on the main thread")
        action()

    def _refresh(self) -> None:
        """Push the current status into the menu and the tooltip."""
        with self._lock:
            icon = self._icon
            status = self._status
        if icon is None:
            return

        def _apply() -> None:
            try:
                icon.update_menu()
            except Exception:
                log.exception("could not update the tray menu")
            try:
                icon.title = status
            except Exception:
                log.exception("could not update the tray tooltip")

        self._on_tray_thread(_apply)

    def _apply_icon(self) -> None:
        """Push the icon image for the current state onto the tray icon."""
        with self._lock:
            icon = self._icon
            state = self._state
            active = self._active
        if icon is None:
            return

        try:
            image = self._build_image(state)
        except Exception:
            log.exception("could not build the tray icon image")
            return

        def _apply() -> None:
            try:
                icon.icon = image
            except Exception:
                log.exception("could not update the tray icon image")

        if active and IS_MACOS:
            self._on_tray_thread(_apply)
        else:
            _apply()

    # -- callbacks -----------------------------------------------------

    def _on_toggle_mute(self) -> None:
        try:
            self._muted = not self._is_muted()
            self._controller.toggle_mute()
        except Exception:
            log.exception("the mute toggle failed")
        try:
            self._refresh()
        except Exception:
            log.exception("could not refresh the tray menu")

    def _on_open_settings(self) -> None:
        try:
            self._controller.open_settings()
        except Exception:
            log.exception("opening the settings window failed")

    def _on_quit(self) -> None:
        try:
            self._controller.quit()
        except Exception:
            log.exception("quitting through the tray failed")

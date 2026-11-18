"""Windows HUD overlay backend for Scribefire (Tkinter + ctypes).

The overlay is a borderless, always-on-top, *non-activating* Tk window that
floats above every other application and draws the "pen writing an ink
stroke" animation plus an optional live-transcription preview line.

Two rules drive the whole design:

**1. The overlay must never take focus.**  If the HUD became the active
window, the user's text field would lose the caret and the dictated text
would be pasted nowhere - silently, with no error anywhere.  The window is
therefore created with ``overrideredirect(True)`` (no title bar, no taskbar
entry) and, once it exists, its extended style is patched through ``ctypes``
with ``WS_EX_NOACTIVATE`` (the window can never become the foreground
window) and ``WS_EX_TOOLWINDOW`` (it never appears in the taskbar or in
``Alt+Tab``).  ``WS_EX_TRANSPARENT`` is added as well so that mouse clicks
pass straight through the HUD.  The window is kept on top with
``SetWindowPos(HWND_TOPMOST, ..., SWP_NOACTIVATE)`` - never with ``lift()``
and never with ``focus_force()``.  As a last-resort safety net the previous
foreground window is restored if the overlay ever stole the focus.

**2. Tk must only be touched from one thread.**  ``set_rms``,
``set_state``, ``set_preview_text`` and ``clear_preview`` are called from
the audio callback thread and from the transcription worker.  They never
touch Tk: they push a command onto a :class:`queue.Queue` and a Tk
``after()`` loop on the UI thread drains it.  Touching a Tk widget from a
worker thread corrupts the interpreter and usually deadlocks, so this
separation is mandatory, not stylistic.

Because the caller (``scribefire/main.py``) triggers ``show()`` from the
hotkey listener thread, the Tk interpreter lives on a *dedicated* internal
thread (``scribefire-hud-tk``) that owns the root window and pumps it.  The
overlay records the thread that called ``show()`` first and refuses a
``show()`` from a different thread, so a caller can never end up with two
threads fighting over one Tk interpreter.

Tk has no alpha channel, so the translucent look of the macOS backend is
approximated: the rounded HUD plate is a solid dark fill with a slightly
lighter 1px border, and every fading/pulsing colour is pre-blended against
that plate colour.  The window itself is fully transparent outside the
plate thanks to Windows colour-key transparency (``-transparentcolor``).

All wave geometry, colours and preview-text truncation live in
:mod:`scribefire.hud_common`; this module only turns those numbers into Tk
drawing commands.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from typing import Any, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import font as tkfont

from .hud_common import (
    BACKGROUND_COLOR,
    COMPACT_SIZE,
    CORNER_RADIUS,
    EXPANDED_SIZE,
    INK_COLOR,
    INK_DIM_COLOR,
    NIB_COLOR,
    NIB_HEIGHT,
    NIB_WIDTH,
    PREVIEW_TEXT_COLOR,
    PREVIEW_TEXT_FONT_SIZE,
    REDRAW_FPS,
    STATE_DONE,
    STATE_LISTENING,
    STATE_PROCESSING,
    TEXT_FPS,
    TEXT_HEIGHT,
    WAVE_HEIGHT_COMPACT,
    WAVE_HEIGHT_EXPANDED,
    WAVE_SAMPLES,
    build_wave_points,
    fade_opacity,
    nib_position,
    pulse_opacity,
    truncate_from_left,
)

#: Module level logger - deliberately *not* imported from ``scribefire.logs``
#: to avoid a circular import.
log = logging.getLogger("scribefire.hud")


# --------------------------------------------------------------------------
# Windows constants
# --------------------------------------------------------------------------

#: Magic colour used for colour-key transparency.  It must never be used for
#: anything else, otherwise a rectangle or a halo would show up against a
#: light or a dark desktop.
MAGIC_COLOR = "#010203"

WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

GWL_EXSTYLE = -20

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

HWND_TOPMOST = -1

# --------------------------------------------------------------------------
# Local layout constants
# --------------------------------------------------------------------------

#: Horizontal padding of the wave strip inside the HUD (left and right).
WAVE_PAD_X = 8.0

#: Horizontal padding of the preview text line inside the HUD.
TEXT_PAD_X = 12.0

#: Distance of the HUD bottom edge from the bottom of the visible screen.
SCREEN_MARGIN_BOTTOM = 72

#: How often the command queue is drained, in milliseconds.
PUMP_INTERVAL_MS = 10

#: How long a public call waits for the UI thread to acknowledge it.
COMMAND_TIMEOUT = 3.0

#: Font family used for the live-preview line.
PREVIEW_FONT_FAMILY = "Segoe UI"


# --------------------------------------------------------------------------
# Win32 helpers
# --------------------------------------------------------------------------

_win32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None


def _win32_ready() -> bool:
    """True when the Win32 API could be loaded (i.e. we are on Windows)."""
    return _win32 is not None


def _apply_no_activate(hwnd: int) -> None:
    """Make *hwnd* impossible to activate and hide it from the taskbar.

    The extended style is read back first so that styles already set by Tk
    (e.g. ``WS_EX_APPWINDOW``) survive.
    """
    if not _win32_ready() or not hwnd:
        return
    try:
        current = int(_win32.GetWindowLongW(hwnd, GWL_EXSTYLE))
    except Exception:  # pragma: no cover - defensive
        current = 0
    style = current | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
    try:
        _win32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:  # pragma: no cover - defensive
        pass


def _set_topmost_no_activate(hwnd: int) -> None:
    """Keep *hwnd* above every other window without activating it."""
    if not _win32_ready() or not hwnd:
        return
    try:
        _win32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except Exception:  # pragma: no cover - defensive
        pass


def _foreground_window() -> int:
    """The handle of the window that currently has the keyboard focus."""
    if not _win32_ready():
        return 0
    try:
        return int(_win32.GetForegroundWindow())
    except Exception:  # pragma: no cover - defensive
        return 0


def _restore_foreground(previous: int, overlay: int = 0) -> None:
    """Give the focus back to *previous* if the overlay stole it.

    ``overlay`` is the overlay's own window handle.  When it is given and the
    foreground window is neither ``previous`` nor ``overlay``, some *other*
    application legitimately owns the focus and nothing has to be done.

    ``SetForegroundWindow`` alone is refused by Windows whenever the calling
    process is not the foreground process (the foreground-lock rule), so the
    input queues of the current thread and of the target window's thread are
    temporarily attached - that is the documented way around the lock.
    """
    if not _win32_ready() or not previous:
        return
    try:
        current = int(_win32.GetForegroundWindow())
    except Exception:  # pragma: no cover - defensive
        return
    if current == previous:
        return
    if overlay and current != overlay:
        # Another application took the focus in the meantime - leave it alone.
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        current_thread = int(kernel32.GetCurrentThreadId())
        target_thread = int(_win32.GetWindowThreadProcessId(previous, None))
        attached = False
        if target_thread and target_thread != current_thread:
            attached = bool(_win32.AttachThreadInput(current_thread, target_thread, True))
        try:
            _win32.SetForegroundWindow(previous)
            _win32.SetFocus(previous)
        finally:
            if attached:
                _win32.AttachThreadInput(current_thread, target_thread, False)
    except Exception:  # pragma: no cover - defensive
        pass


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------


def _hex(rgb: Sequence[int]) -> str:
    """``(r, g, b)`` -> ``"#rrggbb"``."""
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _lerp_rgb(
    first: Sequence[int], second: Sequence[int], amount: float
) -> Tuple[int, int, int]:
    """Linear interpolation between two RGB colours."""
    amount = max(0.0, min(1.0, float(amount)))
    return tuple(  # type: ignore[return-value]
        int(round(first[index] + (second[index] - first[index]) * amount))
        for index in range(3)
    )


def _composite(
    foreground: Sequence[int], background: Sequence[int], alpha: float
) -> Tuple[int, int, int]:
    """Blend *foreground* over *background* with the given *alpha*.

    Tk cannot draw translucent colours, so an alpha value is simulated by
    pre-blending the colour against the opaque plate it is drawn on.
    """
    alpha = max(0.0, min(1.0, float(alpha)))
    return _lerp_rgb(background, foreground, alpha)


# --------------------------------------------------------------------------
# Model (UI thread only)
# --------------------------------------------------------------------------


class _HudModel:
    """The live HUD state.

    Every field is written and read on the UI thread only: the worker
    threads push their values onto the overlay's queue, the UI thread
    copies them into this model and the drawing code reads the model.
    """

    __slots__ = ("state", "rms", "preview", "state_started")

    def __init__(self) -> None:
        self.state = STATE_LISTENING
        self.rms: List[float] = []
        self.preview = ""
        self.state_started = time.monotonic()

    @property
    def elapsed(self) -> float:
        """Seconds spent in the current state."""
        return time.monotonic() - self.state_started

    @property
    def opacity(self) -> float:
        """Global opacity of the drawing for the current state."""
        if self.state == STATE_PROCESSING:
            return pulse_opacity(self.elapsed)
        if self.state == STATE_DONE:
            return fade_opacity(self.elapsed)
        return 1.0

    @property
    def fade_complete(self) -> bool:
        """True once the ``done`` fade has run all the way down to zero."""
        return self.state == STATE_DONE and fade_opacity(self.elapsed) <= 0.0


# --------------------------------------------------------------------------
# Public overlay
# --------------------------------------------------------------------------


class HudOverlay:
    """The Windows HUD overlay.

    Public API (identical to the macOS backend)::

        show() / hide() / destroy()
        set_state("listening" | "processing" | "done")
        set_rms(list_of_floats_oldest_first)
        set_preview_text(text) / clear_preview()
        visible

    ``set_rms``, ``set_state``, ``set_preview_text`` and ``clear_preview``
    are safe to call from any thread; they never touch Tk directly.
    """

    def __init__(self, config: Any, logger: Optional[logging.Logger] = None) -> None:
        self._config = config
        self.log = logger or log

        self._lock = threading.RLock()

        # Commands pushed by the worker threads, drained by the UI thread.
        self._queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

        # The live model.  Only the UI thread mutates it; the lock guards
        # the state that is shared between the threads.
        self._model = _HudModel()

        self._root: Optional[tk.Tk] = None
        self._canvas: Optional[tk.Canvas] = None
        self._font: Optional[tkfont.Font] = None

        self._tk_thread: Optional[threading.Thread] = None
        self._tk_ready = threading.Event()
        self._tk_error: Optional[BaseException] = None

        #: Thread that called :meth:`show` first.  The Tk interpreter may
        #: only ever be driven from one caller thread.
        self._owner_thread: Optional[int] = None

        self._destroyed = False
        self._visible = False
        self._show_preview = False
        self._size = (int(COMPACT_SIZE[0]), int(COMPACT_SIZE[1]))

        # Re-usable canvas items (avoids destroying and recreating ~160
        # items on every frame, which makes the stroke flicker).
        self._line_items: List[int] = []
        self._dot_items: List[int] = []
        self._body_item: Optional[int] = None
        self._nib_item: Optional[int] = None
        self._text_item: Optional[int] = None

        self._last_text: Optional[str] = None
        self._last_text_at = 0.0

    # -- configuration helpers ------------------------------------------

    def _hud_enabled(self) -> bool:
        """True when the HUD is enabled in the configuration."""
        try:
            return bool(self._config.hud_enabled)
        except Exception:
            pass
        try:
            return bool(self._config.get("hud_enabled", True))
        except Exception:
            return True

    def _live_preview(self) -> bool:
        """True when the live-transcription preview line is enabled."""
        try:
            return bool(self._config.live_preview)
        except Exception:
            pass
        try:
            return bool(self._config.get("live_preview", False))
        except Exception:
            return False

    # -- thread plumbing -------------------------------------------------

    def _ensure_tk_thread(self) -> bool:
        """Start the internal Tk thread (once) and wait for the root.

        Returns ``True`` when the Tk interpreter is up and running.
        """
        with self._lock:
            if self._destroyed:
                return False
            thread = self._tk_thread
            if thread is None:
                thread = threading.Thread(
                    target=self._tk_main, name="scribefire-hud-tk", daemon=True
                )
                self._tk_thread = thread
                thread.start()
        self._tk_ready.wait(timeout=COMMAND_TIMEOUT)
        return self._root is not None

    def _post(self, command: str, payload: Any = None, wait: bool = False) -> bool:
        """Send *command* to the UI thread.

        The call never touches Tk itself; the UI thread drains the queue
        from its ``after()`` loop.  Commands are dropped while the Tk
        thread does not exist yet - there is nothing to draw them on and
        an unbounded queue would grow for ever.
        """
        if self._destroyed and command != "destroy":
            return False
        with self._lock:
            if self._tk_thread is None and command not in ("show", "destroy"):
                return False
        event = threading.Event() if wait else None
        self._queue.put((command, (payload, event)))
        if event is None:
            return True
        return event.wait(timeout=COMMAND_TIMEOUT)

    # -- the UI thread ---------------------------------------------------

    def _tk_main(self) -> None:
        """Body of the dedicated Tk thread: build the root and pump it."""
        try:
            root = tk.Tk()
        except Exception as exc:  # pragma: no cover - no display available
            self._tk_error = exc
            self.log.error("could not create the HUD window: %s", exc)
            self._tk_ready.set()
            return

        try:
            self._root = root
            # Start hidden: the window is only shown by an explicit show().
            root.withdraw()
            # No title bar, no taskbar entry.
            root.overrideredirect(True)
            # Colour-key transparency: everything that is painted in the
            # magic colour becomes fully transparent.
            root.configure(bg=MAGIC_COLOR)
            try:
                root.attributes("-transparentcolor", MAGIC_COLOR)
            except Exception:  # pragma: no cover - very old Tk
                self.log.warning("colour-key transparency is not available")
            root.wm_attributes("-topmost", True)

            canvas = tk.Canvas(
                root,
                bg=MAGIC_COLOR,
                highlightthickness=0,
                bd=0,
                relief="flat",
            )
            canvas.pack(fill="both", expand=True)
            self._canvas = canvas

            self._font = tkfont.Font(
                family=PREVIEW_FONT_FAMILY, size=PREVIEW_TEXT_FONT_SIZE
            )

            self._apply_no_activate_styles()
            root.update_idletasks()
        except Exception as exc:  # pragma: no cover - defensive
            self._tk_error = exc
            self.log.exception("could not prepare the HUD window")
            self._tk_ready.set()
            return

        self._tk_ready.set()
        self.log.debug("the HUD Tk thread is running")

        try:
            self._schedule_pump()
            self._schedule_tick()
            root.mainloop()
        except Exception:  # pragma: no cover - defensive
            self.log.exception("the HUD Tk thread stopped")
        finally:
            self._visible = False
            # The interpreter has to be torn down by the thread that created
            # it, otherwise Tcl aborts the process at exit with
            # "Tcl_AsyncDelete: async handler deleted by the wrong thread".
            # Dropping every cross-thread reference here makes the local
            # ``root`` the last owner, so it is finalised in this thread.
            try:
                root.destroy()
            except Exception:  # pragma: no cover - defensive
                pass
            with self._lock:
                self._root = None
                self._canvas = None
                self._font = None

    def _apply_no_activate_styles(self) -> None:
        """Patch the extended window style of the overlay window.

        ``WS_EX_NOACTIVATE`` makes it impossible for the HUD to become the
        foreground window, ``WS_EX_TOOLWINDOW`` keeps it out of the taskbar
        and the Alt+Tab list.
        """
        for hwnd in self._window_handles():
            _apply_no_activate(hwnd)
            _set_topmost_no_activate(hwnd)

    def _window_handles(self) -> List[int]:
        """The wrapper window and the inner Tk window of the overlay.

        On Windows the real top-level window is the *parent* of the window
        returned by ``winfo_id()``; both get the extended style so that
        neither of them can be activated.
        """
        root = self._root
        handles: List[int] = []
        if root is None:
            return handles
        try:
            inner = int(root.winfo_id())
        except Exception:  # pragma: no cover - defensive
            return handles
        if inner:
            handles.append(inner)
        if _win32_ready() and inner:
            try:
                parent = int(_win32.GetParent(inner))
            except Exception:  # pragma: no cover - defensive
                parent = 0
            if parent and parent not in handles:
                handles.insert(0, parent)
        return handles

    def _primary_handle(self) -> int:
        """The handle of the real top-level overlay window."""
        handles = self._window_handles()
        return handles[0] if handles else 0

    def _schedule_pump(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            root.after(PUMP_INTERVAL_MS, self._pump)
        except Exception:  # pragma: no cover - defensive
            pass

    def _schedule_tick(self) -> None:
        """Schedule the next redraw, capped at ``hud_common.REDRAW_FPS``."""
        root = self._root
        if root is None:
            return
        try:
            root.after(int(1000 / REDRAW_FPS), self._tick)
        except Exception:  # pragma: no cover - defensive
            pass

    def _pump(self) -> None:
        """Drain the command queue.  Always runs on the UI thread."""
        while True:
            try:
                command, (payload, event) = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(command, payload)
            except Exception:  # pragma: no cover - defensive
                self.log.exception("the HUD command %r failed", command)
            finally:
                if event is not None:
                    event.set()
        self._schedule_pump()

    def _handle(self, command: str, payload: Any) -> None:
        """Apply a single command.  UI thread only."""
        if command == "show":
            self._show_on_ui_thread()
        elif command == "hide":
            self._hide_on_ui_thread()
        elif command == "destroy":
            self._destroy_on_ui_thread()
        elif command == "state":
            self._model.state = str(payload)
            self._model.state_started = time.monotonic()
        elif command == "rms":
            self._model.rms = list(payload or [])
        elif command == "preview":
            self._model.preview = str(payload or "")

    # -- lifecycle -------------------------------------------------------

    def show(self) -> None:
        """Create the window (if needed) and display it without taking focus."""
        if self._destroyed:
            return
        if not self._hud_enabled():
            self.hide()
            return

        caller = threading.get_ident()
        with self._lock:
            owner = self._owner_thread
            if owner is None:
                self._owner_thread = caller
            elif owner != caller:
                raise RuntimeError(
                    "HudOverlay.show() was called from thread %d but the "
                    "overlay was first shown from thread %d; the Tk "
                    "interpreter may only be driven from one thread."
                    % (caller, owner)
                )

        if not self._ensure_tk_thread():
            self.log.error("the HUD window is not available")
            return
        self._post("show", wait=True)

    def hide(self) -> None:
        """Hide the overlay without destroying it."""
        if self._destroyed:
            return
        with self._lock:
            running = self._tk_thread is not None
        if not running:
            return
        self._post("hide", wait=True)

    def destroy(self) -> None:
        """Release the window.  The overlay is unusable afterwards."""
        with self._lock:
            if self._destroyed:
                return
            self._destroyed = True
            thread = self._tk_thread
        if thread is None:
            self._visible = False
            return
        self._post("destroy", wait=True)
        if thread is not threading.current_thread():
            thread.join(timeout=COMMAND_TIMEOUT)
        self._visible = False
        self.log.debug("HUD destroyed")

    @property
    def visible(self) -> bool:
        """True while the overlay is on screen."""
        return bool(self._visible)

    # -- state (thread safe) ---------------------------------------------

    def set_state(self, state: str) -> None:
        """Set the HUD state: ``"listening"``, ``"processing"`` or ``"done"``."""
        value = str(state or STATE_LISTENING)
        self._post("state", value)

    def set_rms(self, rms_buffer: Sequence[float]) -> None:
        """Feed the rolling RMS window (oldest sample first)."""
        values: List[float] = []
        try:
            source = list(rms_buffer)[-WAVE_SAMPLES:]
        except TypeError:
            source = []
        for raw in source:
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                values.append(0.0)
        self._post("rms", values)

    def set_preview_text(self, text: str) -> None:
        """Set the live transcription preview line."""
        self._post("preview", str(text or ""))

    def clear_preview(self) -> None:
        """Remove the live transcription preview line."""
        self._post("preview", "")

    # -- UI thread implementation ----------------------------------------

    def _show_on_ui_thread(self) -> None:
        """Create/update and display the overlay.  UI thread only."""
        root = self._root
        canvas = self._canvas
        if root is None or canvas is None:
            return

        preview = self._live_preview()
        self._show_preview = preview
        size = EXPANDED_SIZE if preview else COMPACT_SIZE
        self._size = (int(size[0]), int(size[1]))

        # A fresh stroke: drop anything that was queued before show().
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        model = self._model
        model.state = STATE_LISTENING
        model.state_started = time.monotonic()
        model.rms = []
        model.preview = ""
        self._last_text = None

        self._layout(self._size)
        self._position(self._size)

        previous_foreground = _foreground_window()

        root.deiconify()
        # The extended style has to be re-applied after deiconify(): Tk can
        # reset parts of it while the window is (re)shown.
        self._apply_no_activate_styles()

        try:
            root.update_idletasks()
        except Exception:  # pragma: no cover - defensive
            pass

        self._apply_no_activate_styles()

        # Safety net: if the overlay ever became the foreground window, give
        # the focus straight back to the application the user is working in.
        handle = self._primary_handle()
        if previous_foreground and handle:
            _restore_foreground(previous_foreground, handle)

        self._visible = True
        self.log.debug("HUD shown (%s)", "expanded" if preview else "compact")

    def _hide_on_ui_thread(self) -> None:
        """Hide the overlay.  UI thread only."""
        root = self._root
        self._visible = False
        if root is None:
            return
        try:
            root.withdraw()
        except Exception:  # pragma: no cover - defensive
            self.log.exception("could not hide the HUD window")

    def _destroy_on_ui_thread(self) -> None:
        """Destroy the root and end the Tk main loop.  UI thread only."""
        root = self._root
        self._visible = False
        if root is None:
            return
        try:
            root.destroy()
        except Exception:  # pragma: no cover - defensive
            pass
        self._root = None
        self._canvas = None

    # -- layout ----------------------------------------------------------

    def _layout(self, size: Tuple[int, int]) -> None:
        """Resize the window and rebuild the static canvas items."""
        root = self._root
        canvas = self._canvas
        if root is None or canvas is None:
            return
        width, height = int(size[0]), int(size[1])
        try:
            root.geometry("%dx%d" % (width, height))
            canvas.configure(width=width, height=height)
        except Exception:  # pragma: no cover - defensive
            pass
        self._draw_body(width, height)

    def _position(self, size: Tuple[int, int]) -> None:
        """Bottom-centre the HUD on the screen the user is working on."""
        root = self._root
        if root is None:
            return
        width, height = int(size[0]), int(size[1])
        try:
            screen_width = int(root.winfo_screenwidth())
            screen_height = int(root.winfo_screenheight())
        except Exception:  # pragma: no cover - defensive
            return
        x = max(0, (screen_width - width) // 2)
        y = max(0, screen_height - height - SCREEN_MARGIN_BOTTOM)
        try:
            root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        except Exception:  # pragma: no cover - defensive
            pass

    def _draw_body(self, width: int, height: int) -> None:
        """Draw the rounded HUD plate.

        Tk has no alpha channel, so the translucent dark plate of the macOS
        backend is approximated with a solid dark fill plus a slightly
        lighter 1px border.
        """
        canvas = self._canvas
        if canvas is None:
            return

        plate = tuple(BACKGROUND_COLOR[:3])
        border = _lerp_rgb(plate, (255, 255, 255), 0.16)
        points = self._rounded_rect_points(
            0.0, 0.0, float(width), float(height), float(CORNER_RADIUS)
        )
        if self._body_item is None:
            self._body_item = canvas.create_polygon(
                points,
                smooth=True,
                splinesteps=24,
                fill=_hex(plate),
                outline=_hex(border),
                width=1,
            )
        else:
            canvas.coords(self._body_item, *points)
            canvas.itemconfig(
                self._body_item, fill=_hex(plate), outline=_hex(border), width=1
            )

    @staticmethod
    def _rounded_rect_points(
        x0: float, y0: float, x1: float, y1: float, radius: float
    ) -> List[float]:
        """Points of a rounded rectangle drawn as a smoothed polygon.

        The corner points are duplicated so that the spline stays straight
        along the edges and only rounds the corners.
        """
        radius = max(0.0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        return [
            x0 + radius, y0,
            x1 - radius, y0,
            x1, y0,
            x1, y0 + radius,
            x1, y1 - radius,
            x1, y1,
            x1 - radius, y1,
            x0 + radius, y1,
            x0, y1,
            x0, y1 - radius,
            x0, y0 + radius,
            x0, y0,
        ]

    # -- drawing ---------------------------------------------------------

    def _tick(self) -> None:
        """One redraw tick.  Always on the UI thread."""
        if self._destroyed:
            return
        self._draw()
        self._schedule_tick()

    def _draw(self) -> None:
        """Draw one frame of the HUD.  UI thread only."""
        canvas = self._canvas
        if canvas is None:
            return

        model = self._model
        if model.fade_complete:
            # The "done" fade has run out: hide the HUD.
            if self._visible:
                self._hide_on_ui_thread()
            return

        width, height = self._size
        wave_height = (
            WAVE_HEIGHT_EXPANDED if self._show_preview else WAVE_HEIGHT_COMPACT
        )
        self._draw_wave(width, wave_height, model)
        if self._show_preview:
            self._draw_preview(width, height, wave_height, model)
        else:
            self._hide_preview()

    def _draw_wave(self, width: int, wave_height: int, model: _HudModel) -> None:
        """Draw the ink stroke plus the pen nib.

        ``build_wave_points`` returns the newest sample at the right edge
        (that is where the pen nib is) and older samples trail off to the
        left, so the stroke reads as being written right to left.  Tk's y
        axis grows downwards, which is exactly the orientation the shared
        geometry uses, so no flipping is needed here.
        """
        canvas = self._canvas
        if canvas is None:
            return

        strip_width = width - 2.0 * WAVE_PAD_X
        if strip_width <= 0.0:
            return

        points = build_wave_points(model.rms, strip_width, wave_height)
        opacity = model.opacity
        plate = tuple(BACKGROUND_COLOR[:3])
        count = len(points)

        line_count = 0
        dot_count = 0
        previous: Optional[Tuple[float, float]] = None

        for index, (x, y, thickness) in enumerate(points):
            position = (WAVE_PAD_X + x, y)
            # Older ink is dimmer, which makes the right-to-left scroll of
            # the stroke readable.
            age = index / float(max(1, count - 1))
            color = self._ink_color(age, opacity, plate)
            if previous is not None:
                self._stroke_segment(line_count, previous, position, thickness, color)
                line_count += 1
            else:
                self._ink_dot(dot_count, position, thickness, color)
                dot_count += 1
            previous = position

        # A round cap on every joint keeps the thick stroke gap free where
        # the amplitude changes quickly.
        for index, (x, y, thickness) in enumerate(points):
            if index == 0:
                continue
            age = index / float(max(1, count - 1))
            color = self._ink_color(age, opacity, plate)
            self._ink_dot(dot_count, (WAVE_PAD_X + x, y), thickness, color)
            dot_count += 1

        self._hide_extra_items(self._line_items, line_count)
        self._hide_extra_items(self._dot_items, dot_count)

        # The pen nib sits at the right end of the stroke and bobs with the
        # current amplitude.  While processing the pen has "lifted away",
        # so it is not drawn at all.
        if model.state == STATE_PROCESSING:
            self._hide_nib()
            return

        nib_x, nib_y = nib_position(model.rms, strip_width, wave_height)
        nib_alpha = 1.0 if model.state == STATE_LISTENING else opacity
        self._draw_nib(WAVE_PAD_X + nib_x, nib_y, nib_alpha, plate)

    def _ink_color(self, age: float, opacity: float, plate: Sequence[int]) -> str:
        """Colour of one ink segment: fresh ink is bright, old ink is dim."""
        rgb = _lerp_rgb(INK_DIM_COLOR[:3], INK_COLOR[:3], age)
        alpha = (INK_DIM_COLOR[3] + (INK_COLOR[3] - INK_DIM_COLOR[3]) * age) / 255.0
        return _hex(_composite(rgb, plate, alpha * opacity))

    def _stroke_segment(
        self,
        index: int,
        start: Tuple[float, float],
        end: Tuple[float, float],
        thickness: float,
        color: str,
    ) -> None:
        """Draw one segment of the stroke as a rounded, thick line.

        Overlapping round-capped segments (instead of a hairline polyline)
        keep the stroke smooth where the amplitude changes quickly.
        """
        canvas = self._canvas
        if canvas is None:
            return
        if index < len(self._line_items):
            item = self._line_items[index]
            canvas.coords(item, start[0], start[1], end[0], end[1])
            canvas.itemconfig(
                item, fill=color, width=max(0.5, float(thickness)), state="normal"
            )
            return
        item = canvas.create_line(
            start[0],
            start[1],
            end[0],
            end[1],
            fill=color,
            width=max(0.5, float(thickness)),
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )
        self._line_items.append(item)

    def _ink_dot(
        self,
        index: int,
        position: Tuple[float, float],
        thickness: float,
        color: str,
    ) -> None:
        """Draw a round dot, used to cap the stroke and fill the joints."""
        canvas = self._canvas
        if canvas is None:
            return
        radius = max(0.5, float(thickness)) / 2.0
        x, y = position
        if index < len(self._dot_items):
            item = self._dot_items[index]
            canvas.coords(item, x - radius, y - radius, x + radius, y + radius)
            canvas.itemconfig(item, fill=color, outline=color, state="normal")
            return
        item = canvas.create_oval(
            x - radius,
            y - radius,
            x + radius,
            y + radius,
            fill=color,
            outline=color,
        )
        self._dot_items.append(item)

    @staticmethod
    def _hide_extra_items(items: List[int], used: int) -> None:
        """Hide the canvas items that are no longer needed."""
        for item in items[used:]:
            try:
                item.state = "hidden"
            except Exception:  # pragma: no cover - defensive
                pass

    def _draw_nib(
        self, x: float, y: float, alpha: float, plate: Sequence[int]
    ) -> None:
        """Draw the pen nib: a small filled triangle pointing at the ink."""
        canvas = self._canvas
        if canvas is None or alpha <= 0.0:
            self._hide_nib()
            return
        half_width = NIB_WIDTH / 2.0
        half_height = NIB_HEIGHT / 2.0
        points = [
            x - half_width,
            y - half_height,
            x + half_width,
            y - half_height,
            x,
            y + half_height,
        ]
        color = _hex(_composite(NIB_COLOR[:3], plate, (NIB_COLOR[3] / 255.0) * alpha))
        if self._nib_item is None:
            self._nib_item = canvas.create_polygon(points, fill=color, outline=color)
        else:
            canvas.coords(self._nib_item, *points)
            canvas.itemconfig(self._nib_item, fill=color, outline=color, state="normal")

    def _hide_nib(self) -> None:
        """Hide the pen nib (it has "lifted away")."""
        if self._nib_item is None:
            return
        try:
            self._nib_item.state = "hidden"
        except Exception:  # pragma: no cover - defensive
            pass

    # -- preview text ----------------------------------------------------

    def _measure(self, text: str) -> float:
        """Width of *text* in px, measured by Tk.

        This is the ``measure`` callable handed to
        :func:`scribefire.hud_common.truncate_from_left`; the truncation
        algorithm itself stays in ``hud_common``.
        """
        if not text:
            return 0.0
        font = self._font
        if font is None:
            return float(len(text)) * 6.0
        try:
            return float(font.measure(text))
        except Exception:  # pragma: no cover - defensive
            return float(len(text)) * 6.0

    def _draw_preview(
        self, width: int, height: int, wave_height: int, model: _HudModel
    ) -> None:
        """Draw the single live-preview line, below the wave strip.

        The text is truncated from the left by ``hud_common`` so the most
        recently spoken words stay visible.
        """
        canvas = self._canvas
        if canvas is None:
            return

        plate = tuple(BACKGROUND_COLOR[:3])
        max_width = width - 2.0 * TEXT_PAD_X
        text = model.preview or ""
        visible = truncate_from_left(text, self._measure, max_width)

        now = time.monotonic()
        # Partial results arrive far faster than the screen refreshes: the
        # text is redrawn at most TEXT_FPS times per second.
        if visible != self._last_text and (
            now - self._last_text_at >= 1.0 / float(TEXT_FPS)
        ):
            self._last_text = visible
            self._last_text_at = now
            self._set_text_item(visible, width, height, wave_height)

        # The colour follows the state (the text fades out with the stroke).
        if self._text_item is not None:
            alpha = (PREVIEW_TEXT_COLOR[3] / 255.0) * model.opacity
            color = _hex(_composite(PREVIEW_TEXT_COLOR[:3], plate, alpha))
            try:
                self._text_item.configure(fill=color)
            except Exception:  # pragma: no cover - defensive
                pass

    def _set_text_item(
        self, text: str, width: int, height: int, wave_height: int
    ) -> None:
        """Create or update the single preview text item."""
        canvas = self._canvas
        if canvas is None:
            return
        region_height = max(0, height - wave_height)
        centre_y = wave_height + max(0.0, (region_height - TEXT_HEIGHT) / 2.0) + (
            TEXT_HEIGHT / 2.0
        )
        if self._text_item is None:
            self._text_item = canvas.create_text(
                TEXT_PAD_X,
                centre_y,
                text=text,
                anchor="w",
                fill=_hex(PREVIEW_TEXT_COLOR[:3]),
                font=self._font,
            )
        else:
            canvas.coords(self._text_item, TEXT_PAD_X, centre_y)
            self._text_item.configure(text=text, state="normal")

    def _hide_preview(self) -> None:
        """Hide the preview line (compact HUD)."""
        if self._text_item is None:
            return
        try:
            self._text_item.state = "hidden"
        except Exception:  # pragma: no cover - defensive
            pass
        self._last_text = None

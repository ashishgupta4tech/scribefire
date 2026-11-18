"""macOS HUD overlay backend for Scribefire (PyObjC / AppKit).

The overlay is a borderless, non-activating :class:`NSPanel` that floats
above every other application and draws the "pen writing an ink stroke"
animation plus an optional live-transcription preview line.

Two rules drive the whole design:

**1. The overlay must never take focus.**  If the HUD became the key window,
the user's text field would lose the caret and the dictated text would be
pasted nowhere - silently, with no error anywhere.  The panel therefore uses
the ``Borderless | NonactivatingPanel`` style mask, overrides
``canBecomeKeyWindow``/``canBecomeMainWindow`` to return ``False``, ignores
mouse events completely and is shown with ``orderFrontRegardless_`` (never
``makeKeyAndOrderFront_``).  The host application is never activated from
here; it is expected to run as an accessory app
(``NSApplicationActivationPolicyAccessory``), which the *caller* sets up.

**2. All AppKit work happens on the main thread.**  ``set_rms``,
``set_state`` and ``set_preview_text`` are called from the audio callback
thread and from the transcription worker.  They never touch AppKit: they
store the pending value under a lock and hop to the main thread, where the
redraw timer picks it up.  Updating an ``NSView`` from a worker thread
silently hangs the app, so this separation is mandatory, not stylistic.

All wave geometry, colours and preview-text truncation live in
:mod:`scribefire.hud_common`; this module only turns those numbers into AppKit
drawing commands.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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

try:  # pragma: no cover - depends on the host platform
    import AppKit  # type: ignore
    import Quartz  # type: ignore
    from Foundation import NSMakePoint, NSMakeRect  # type: ignore

    _PYOBJC_AVAILABLE = True
except Exception:  # pragma: no cover - importing this module on Windows must not fail
    AppKit = None  # type: ignore
    Quartz = None  # type: ignore
    NSMakePoint = None  # type: ignore
    NSMakeRect = None  # type: ignore
    _PYOBJC_AVAILABLE = False


# --------------------------------------------------------------------------
# Local layout constants
# --------------------------------------------------------------------------

#: Horizontal padding of the wave strip inside the HUD (left and right).
WAVE_PAD_X = 8.0

#: Horizontal padding of the preview text line inside the HUD.
TEXT_PAD_X = 12.0

#: Distance of the HUD bottom edge from the bottom of the visible screen area.
SCREEN_MARGIN_BOTTOM = 72.0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _nscolor(rgba: Sequence[int]) -> Any:
    """Build an ``NSColor`` from an RGBA tuple of 0..255 values."""
    red, green, blue, alpha = rgba
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
        red / 255.0, green / 255.0, blue / 255.0, alpha / 255.0
    )


def _window_style_mask() -> int:
    """``Borderless | NonactivatingPanel`` - the HUD must not steal focus."""
    borderless = getattr(AppKit, "NSWindowStyleMaskBorderless", None)
    if borderless is None:
        borderless = getattr(AppKit, "NSBorderlessWindowMask", 0)
    nonactivating = getattr(AppKit, "NSWindowStyleMaskNonactivatingPanel", None)
    if nonactivating is None:
        nonactivating = getattr(AppKit, "NSNonactivatingPanel", 0)
    return int(borderless or 0) | int(nonactivating or 0)


def _collection_behavior() -> int:
    """Join all spaces, follow full-screen apps, stay out of the window cycle."""
    flags = 0
    for name in (
        "NSWindowCollectionBehaviorCanJoinAllSpaces",
        "NSWindowCollectionBehaviorFullScreenAuxiliary",
        "NSWindowCollectionBehaviorIgnoresCycle",
        "NSWindowCollectionBehaviorStationary",
    ):
        flags |= int(getattr(AppKit, name, 0) or 0)
    return flags


def _active_screen() -> Any:
    """The screen the mouse cursor is on, falling back to the main screen."""
    screens = AppKit.NSScreen.screens()
    if not screens:
        return None
    try:
        mouse = AppKit.NSEvent.mouseLocation()
        for screen in screens:
            if screen.frame().containsPoint_(mouse):
                return screen
    except Exception:
        pass
    main = AppKit.NSScreen.mainScreen()
    return main if main is not None else screens[0]


_PREVIEW_FONT = None


def _preview_font() -> Any:
    """The (cached) system font used for the live-preview line."""
    global _PREVIEW_FONT
    if _PREVIEW_FONT is None:
        _PREVIEW_FONT = AppKit.NSFont.systemFontOfSize_(float(PREVIEW_TEXT_FONT_SIZE))
    return _PREVIEW_FONT


def _measure_text(text: str) -> float:
    """Width of *text* in points, measured by AppKit.

    This is the ``measure`` callable handed to
    :func:`scribefire.hud_common.truncate_from_left`; the truncation algorithm
    itself stays in ``hud_common``.
    """
    if not text:
        return 0.0
    try:
        attributes = {AppKit.NSFontAttributeName: _preview_font()}
        attributed = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            text, attributes
        )
        return float(attributed.size().width)
    except Exception:  # pragma: no cover - defensive, drawing must not die
        return float(len(text)) * 6.0


# --------------------------------------------------------------------------
# Model (main-thread only state)
# --------------------------------------------------------------------------


class _HudModel:
    """The live HUD state.

    Every field is written and read on the main thread only: the worker
    threads put their values into the overlay's pending dict, the main
    thread copies them into this model and the view reads the model while
    drawing.
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
# AppKit classes
# --------------------------------------------------------------------------

if _PYOBJC_AVAILABLE:

    class _NonActivatingPanel(AppKit.NSPanel):
        """An ``NSPanel`` that refuses to become the key or main window.

        This is the last line of defence against the HUD stealing the caret
        from the field the user is typing in.
        """

        def canBecomeKeyWindow(self):
            # Never take the keyboard focus away from the frontmost app.
            return False

        def canBecomeMainWindow(self):
            return False

    class _MainThreadDispatcher(AppKit.NSObject):
        """Runs a callable on the main thread.

        Used as the target of ``performSelectorOnMainThread:withObject:
        wait:`` so that worker threads never touch AppKit themselves.
        """

        def runOnMainThread_(self, block):
            if block is None:
                return
            try:
                block()
            except Exception:  # pragma: no cover - defensive
                log.exception("HUD main-thread block failed")

    class _HudTimerTarget(AppKit.NSObject):
        """Target for the redraw ``NSTimer``.

        Kept separate from :class:`HudOverlay` so that the timer -> target ->
        overlay reference chain cannot form a retain cycle; the target only
        holds a weak reference to the overlay.
        """

        owner = None

        def initWithOwner_(self, owner):
            self = self.init()
            if self is None:  # pragma: no cover - ObjC allocation failure
                return None
            self.owner = weakref.ref(owner)
            return self

        def tick_(self, timer):
            owner_ref = self.owner
            owner = owner_ref() if owner_ref is not None else None
            if owner is None:
                try:
                    timer.invalidate()
                except Exception:
                    pass
                return
            owner._on_timer(timer)

    class _HudView(AppKit.NSView):
        """Draws the HUD: scrolling ink stroke, pen nib and preview text.

        The view never becomes first responder, so it cannot pull the
        keyboard focus away from the application the user is working in.
        """

        def initWithModel_(self, model):
            rect = NSMakeRect(
                0.0, 0.0, float(COMPACT_SIZE[0]), float(COMPACT_SIZE[1])
            )
            self = self.initWithFrame_(rect)
            if self is None:  # pragma: no cover - ObjC allocation failure
                return None
            self._model = model
            self._show_preview = False
            try:
                self.setWantsLayer_(True)
                layer = self.layer()
                if layer is not None:
                    layer.setOpaque_(False)
                    layer.setBackgroundColor_(AppKit.NSColor.clearColor().CGColor())
            except Exception:  # pragma: no cover - layer backing is optional
                pass
            return self

        # -- focus safety -------------------------------------------------

        def acceptsFirstResponder(self):
            return False

        def canBecomeKeyView(self):
            return False

        # -- drawing ------------------------------------------------------

        def drawRect_(self, rect):
            context = AppKit.NSGraphicsContext.currentContext()
            if context is not None:
                try:
                    context.setShouldAntialias_(True)
                    context.setShouldSmoothFonts_(True)
                except Exception:  # pragma: no cover - older AppKit
                    pass

            model = getattr(self, "_model", None)
            if model is None:
                return

            bounds = self.bounds()
            width = float(bounds.size.width)
            height = float(bounds.size.height)
            if width <= 0.0 or height <= 0.0:
                return

            self._draw_background(width, height)

            wave_height = float(
                WAVE_HEIGHT_EXPANDED if self._show_preview else WAVE_HEIGHT_COMPACT
            )
            self._draw_wave(width, height, wave_height, model)

            if self._show_preview:
                self._draw_preview(width, height, wave_height, model)

        def _draw_background(self, width, height):
            """The rounded, dark translucent plate behind everything else."""
            path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0.0, 0.0, width, height),
                float(CORNER_RADIUS),
                float(CORNER_RADIUS),
            )
            _nscolor(BACKGROUND_COLOR).set()
            path.fill()

        def _draw_wave(self, width, height, wave_height, model):
            """The ink stroke plus the pen nib.

            ``build_wave_points`` returns top-down coordinates whose origin is
            the top-left of the wave strip; the view is not flipped, so the y
            axis is mirrored here.
            """
            strip_width = width - 2.0 * WAVE_PAD_X
            if strip_width <= 0.0:
                return

            points = build_wave_points(model.rms, strip_width, wave_height)
            if not points:
                return

            opacity = model.opacity
            count = len(points)
            previous: Optional[Tuple[float, float]] = None

            for index, (x, y, thickness) in enumerate(points):
                point = (WAVE_PAD_X + x, height - y)
                # Older ink is dimmer, which makes the right-to-left scroll
                # of the stroke readable.
                age = index / float(max(1, count - 1))
                if previous is not None:
                    self._stroke_segment(previous, point, thickness, age, opacity)
                else:
                    self._ink_dot(point, thickness, age, opacity)
                previous = point

            # The pen nib sits at the right end of the stroke and bobs with
            # the current amplitude.  While processing the pen has "lifted
            # away", so it is not drawn at all.
            if model.state != STATE_PROCESSING:
                nib_x, nib_y = nib_position(model.rms, strip_width, wave_height)
                nib_alpha = opacity if model.state != STATE_LISTENING else 1.0
                self._draw_nib(WAVE_PAD_X + nib_x, height - nib_y, nib_alpha)

        def _ink_color(self, age: float, opacity: float):
            """Ink colour for a segment: fresh ink is bright, old ink dim."""
            red = INK_DIM_COLOR[0] + (INK_COLOR[0] - INK_DIM_COLOR[0]) * age
            green = INK_DIM_COLOR[1] + (INK_COLOR[1] - INK_DIM_COLOR[1]) * age
            blue = INK_DIM_COLOR[2] + (INK_COLOR[2] - INK_DIM_COLOR[2]) * age
            alpha = (INK_DIM_COLOR[3] + (INK_COLOR[3] - INK_DIM_COLOR[3]) * age) / 255.0
            return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                red / 255.0,
                green / 255.0,
                blue / 255.0,
                max(0.0, min(1.0, alpha * opacity)),
            )

        def _stroke_segment(self, start, end, thickness, age, opacity):
            """Draw one segment of the stroke as a rounded, thick line.

            Overlapping round-capped segments (instead of a hairline
            polyline) keep the stroke smooth where the amplitude changes
            quickly.
            """
            path = AppKit.NSBezierPath.bezierPath()
            path.setLineWidth_(max(0.5, float(thickness)))
            path.setLineCapStyle_(AppKit.NSLineCapStyleRound)
            path.setLineJoinStyle_(AppKit.NSLineJoinStyleRound)
            path.moveToPoint_(NSMakePoint(start[0], start[1]))
            path.lineToPoint_(NSMakePoint(end[0], end[1]))
            self._ink_color(age, opacity).set()
            path.stroke()

        def _ink_dot(self, point, thickness, age, opacity):
            """A round dot, used to cap the stroke and fill joints."""
            radius = max(0.5, float(thickness)) / 2.0
            path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(
                    point[0] - radius,
                    point[1] - radius,
                    radius * 2.0,
                    radius * 2.0,
                )
            )
            self._ink_color(age, opacity).set()
            path.fill()

        def _draw_nib(self, x, y, alpha):
            """The pen nib: a small filled triangle pointing down at the ink."""
            if alpha <= 0.0:
                return
            half_width = NIB_WIDTH / 2.0
            half_height = NIB_HEIGHT / 2.0
            path = AppKit.NSBezierPath.bezierPath()
            path.moveToPoint_(NSMakePoint(x - half_width, y + half_height))
            path.lineToPoint_(NSMakePoint(x + half_width, y + half_height))
            path.lineToPoint_(NSMakePoint(x, y - half_height))
            path.closePath()
            if alpha >= 1.0:
                color = _nscolor(NIB_COLOR)
            else:
                color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                    NIB_COLOR[0] / 255.0,
                    NIB_COLOR[1] / 255.0,
                    NIB_COLOR[2] / 255.0,
                    (NIB_COLOR[3] / 255.0) * alpha,
                )
            color.set()
            path.fill()

        def _draw_preview(self, width, height, wave_height, model):
            """The single live-preview line, below the wave strip.

            The text is truncated from the left by ``hud_common`` so the most
            recently spoken words stay visible.
            """
            text = model.preview or ""
            if not text:
                return

            max_width = width - 2.0 * TEXT_PAD_X
            visible = truncate_from_left(text, _measure_text, max_width)
            if not visible:
                return

            font = _preview_font()
            attributed = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                visible,
                {
                    AppKit.NSFontAttributeName: font,
                    # PREVIEW_TEXT_COLOR already carries the reduced alpha
                    # that marks the text as provisional.
                    AppKit.NSForegroundColorAttributeName: _nscolor(PREVIEW_TEXT_COLOR),
                },
            )

            region_height = height - wave_height
            line_height = font.ascender() - font.descender()
            y = (region_height - line_height) / 2.0 - font.descender()
            attributed.drawAtPoint_(NSMakePoint(TEXT_PAD_X, y))


# --------------------------------------------------------------------------
# Public overlay
# --------------------------------------------------------------------------


class HudOverlay:
    """The macOS HUD overlay.

    Public API (identical to the Windows backend)::

        show() / hide() / destroy()
        set_state("listening" | "processing" | "done")
        set_rms(list_of_floats_oldest_first)
        set_preview_text(text) / clear_preview()
        visible

    ``set_rms``, ``set_state``, ``set_preview_text`` and ``clear_preview``
    are safe to call from any thread; they never touch AppKit directly.
    """

    def __init__(self, config: Any, logger: Optional[logging.Logger] = None) -> None:
        self._config = config
        self.log = logger or log

        self._lock = threading.Lock()
        self._pending: Dict[str, Any] = {}
        self._apply_scheduled = False

        self._model = _HudModel()
        self._panel: Any = None
        self._view: Any = None
        self._timer: Any = None
        self._timer_target: Any = None
        self._dispatcher: Any = None
        self._destroyed = False

    # -- configuration helpers ------------------------------------------

    def _hud_enabled(self) -> bool:
        try:
            return bool(self._config.hud_enabled)
        except Exception:
            return True

    def _live_preview(self) -> bool:
        try:
            return bool(self._config.live_preview)
        except Exception:
            return False

    # -- main thread plumbing -------------------------------------------

    def _run_on_main(self, func: Callable[[], None]) -> None:
        """Run *func* on the main thread.

        AppKit is not thread safe: touching a view from a worker thread
        hangs the app without an error.  Everything that touches AppKit goes
        through here.
        """
        if not _PYOBJC_AVAILABLE:
            self.log.error("PyObjC is not available - the macOS HUD cannot be used")
            return
        if threading.current_thread() is threading.main_thread():
            func()
            return

        dispatcher = self._dispatcher_target()
        if dispatcher is not None:
            try:
                dispatcher.performSelectorOnMainThread_withObject_wait_(
                    "runOnMainThread:", func, False
                )
                return
            except Exception:  # pragma: no cover - defensive
                self.log.exception("performSelectorOnMainThread failed")
        try:
            # Fallback: libdispatch, if PyObjC exposes it.
            Quartz.dispatch_async(Quartz.dispatch_get_main_queue(), func)
        except Exception:  # pragma: no cover - defensive
            self.log.exception("could not marshal work to the main thread")

    def _dispatcher_target(self) -> Any:
        """The (cached) ``performSelectorOnMainThread`` target."""
        if self._dispatcher is None:
            dispatcher = _MainThreadDispatcher.alloc().init()
            if dispatcher is None:  # pragma: no cover - ObjC allocation failure
                return None
            self._dispatcher = dispatcher
        return self._dispatcher

    def _schedule_apply(self) -> None:
        """Ask the main thread to consume the pending values.

        The redraw timer would pick them up anyway, but hopping to the main
        thread makes a state change visible immediately.
        """
        if not _PYOBJC_AVAILABLE:
            return
        with self._lock:
            if self._apply_scheduled:
                return
            self._apply_scheduled = True

        def _apply() -> None:
            with self._lock:
                self._apply_scheduled = False
            self._apply_pending()

        self._run_on_main(_apply)

    def _apply_pending(self) -> None:
        """Copy the pending values into the model.  Main thread only."""
        with self._lock:
            if not self._pending:
                return
            pending = self._pending
            self._pending = {}

        model = self._model
        if "state" in pending:
            model.state = pending["state"]
            model.state_started = time.monotonic()
        if "rms" in pending:
            model.rms = pending["rms"]
        if "preview" in pending:
            model.preview = pending["preview"]

    # -- lifecycle -------------------------------------------------------

    def show(self) -> None:
        """Create the panel (if needed) and show it without taking focus."""
        if self._destroyed:
            return
        if not self._hud_enabled():
            self.hide()
            return
        self._run_on_main(self._show_on_main)

    def _show_on_main(self) -> None:
        if self._destroyed:
            return

        preview = self._live_preview()
        size = EXPANDED_SIZE if preview else COMPACT_SIZE
        width = float(size[0])
        height = float(size[1])

        if self._view is None:
            view = self._create_panel(width, height)
        else:
            view = self._view
            self._resize_panel(width, height)
        if view is None:
            return

        view._show_preview = preview

        # A fresh stroke: drop anything queued before show().
        with self._lock:
            self._pending.clear()
        model = self._model
        model.state = STATE_LISTENING
        model.state_started = time.monotonic()
        model.rms = []
        model.preview = ""

        self._position_panel(width, height)

        panel = self._panel
        if panel is None:
            return
        # orderFrontRegardless_ shows the window *without* activating the
        # application.  makeKeyAndOrderFront_ is deliberately never used.
        panel.orderFrontRegardless_(None)

        self._start_timer()
        self.log.debug("HUD shown (%s)", "expanded" if preview else "compact")

    def hide(self) -> None:
        """Hide the overlay without destroying it."""
        if self._destroyed:
            return
        self._run_on_main(self._hide_on_main)

    def _hide_on_main(self) -> None:
        self._stop_timer()
        panel = self._panel
        if panel is None:
            return
        try:
            panel.orderOut_(None)
        except Exception:  # pragma: no cover - defensive
            self.log.exception("could not hide the HUD panel")

    def destroy(self) -> None:
        """Stop the timer and release the window.  Unusable afterwards."""
        self._destroyed = True
        self._run_on_main(self._destroy_on_main)

    def _destroy_on_main(self) -> None:
        self._stop_timer()
        panel = self._panel
        self._panel = None
        self._view = None
        if panel is None:
            return
        for action in (
            lambda: panel.orderOut_(None),
            lambda: panel.setContentView_(None),
            lambda: panel.close(),
        ):
            try:
                action()
            except Exception:  # pragma: no cover - defensive
                pass
        self.log.debug("HUD destroyed")

    @property
    def visible(self) -> bool:
        """True while the overlay is on screen."""
        panel = self._panel
        if panel is None:
            return False
        try:
            return bool(panel.isVisible())
        except Exception:  # pragma: no cover - defensive
            return False

    # -- state (thread safe) ---------------------------------------------

    def set_state(self, state: str) -> None:
        """Set the HUD state: ``"listening"``, ``"processing"`` or ``"done"``."""
        value = str(state or STATE_LISTENING)
        with self._lock:
            self._pending["state"] = value
        self._schedule_apply()

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
        with self._lock:
            self._pending["rms"] = values
        self._schedule_apply()

    def set_preview_text(self, text: str) -> None:
        """Set the live transcription preview line."""
        with self._lock:
            self._pending["preview"] = str(text or "")
        self._schedule_apply()

    def clear_preview(self) -> None:
        """Remove the live transcription preview line."""
        with self._lock:
            self._pending["preview"] = ""
        self._schedule_apply()

    # -- panel -----------------------------------------------------------

    def _create_panel(self, width: float, height: float) -> Any:
        """Build the borderless, non-activating panel and its drawing view."""
        rect = NSMakeRect(0.0, 0.0, width, height)
        panel = _NonActivatingPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            _window_style_mask(),
            AppKit.NSBackingStoreBuffered,
            False,
        )
        if panel is None:  # pragma: no cover - ObjC allocation failure
            self.log.error("could not create the HUD panel")
            return None

        # Float above every other application.
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        # Transparent: the plate is drawn by the view itself.
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(False)
        # The HUD cannot be clicked, hovered or focused.
        panel.setIgnoresMouseEvents_(True)
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        try:
            panel.setBecomesKeyOnlyIfNeeded_(True)
        except Exception:  # pragma: no cover - defensive
            pass
        panel.setCollectionBehavior_(_collection_behavior())

        view = _HudView.alloc().initWithModel_(self._model)
        if view is None:  # pragma: no cover - ObjC allocation failure
            return None
        view.setFrame_(rect)
        view.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
        )
        panel.setContentView_(view)

        self._panel = panel
        self._view = view
        return view

    def _resize_panel(self, width: float, height: float) -> None:
        panel = self._panel
        if panel is None:
            return
        rect = NSMakeRect(0.0, 0.0, width, height)
        try:
            panel.setFrame_display_(rect, True)
        except Exception:  # pragma: no cover - defensive
            panel.setFrame_(rect)
        view = self._view
        if view is not None:
            view.setFrame_(rect)

    def _position_panel(self, width: float, height: float) -> None:
        """Bottom-centre the HUD on the screen the user is working on."""
        panel = self._panel
        if panel is None:
            return
        screen = _active_screen()
        if screen is None:  # pragma: no cover - no display attached
            return
        frame = screen.visibleFrame()
        x = frame.origin.x + (frame.size.width - width) / 2.0
        y = frame.origin.y + SCREEN_MARGIN_BOTTOM
        rect = NSMakeRect(x, y, width, height)
        try:
            panel.setFrame_display_(rect, True)
        except Exception:  # pragma: no cover - defensive
            panel.setFrame_(rect)

    # -- timer -----------------------------------------------------------

    def _start_timer(self) -> None:
        """Start the redraw timer (capped at ``hud_common.REDRAW_FPS``)."""
        if self._timer is not None:
            return
        target = _HudTimerTarget.alloc().initWithOwner_(self)
        if target is None:  # pragma: no cover - ObjC allocation failure
            return
        timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / float(REDRAW_FPS),
            target,
            "tick:",
            None,
            True,
        )
        if timer is None:  # pragma: no cover - defensive
            return
        # Keep a strong reference: without it the timer is deallocated and
        # the HUD freezes on the first frame.
        self._timer = timer
        self._timer_target = target

    def _stop_timer(self) -> None:
        timer = self._timer
        self._timer = None
        self._timer_target = None
        if timer is None:
            return
        try:
            timer.invalidate()
        except Exception:  # pragma: no cover - defensive
            pass

    def _on_timer(self, timer: Any) -> None:
        """One redraw tick.  Always on the main thread."""
        if self._destroyed:
            return
        self._apply_pending()

        view = self._view
        panel = self._panel
        if view is None or panel is None:
            return
        try:
            if not panel.isVisible():
                return
        except Exception:  # pragma: no cover - defensive
            return

        if view._model.fade_complete:
            # The "done" fade has run out: hide the HUD.
            try:
                panel.orderOut_(None)
            except Exception:  # pragma: no cover - defensive
                pass
            return

        view.needsDisplay = True

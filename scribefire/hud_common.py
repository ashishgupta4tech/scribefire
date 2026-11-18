"""Platform-neutral HUD geometry, colour and layout math.

Both the macOS (AppKit) and Windows (Tkinter) overlay backends import this
module and *only* implement drawing.  The wave geometry and the preview-text
truncation math live here exactly once - they must never be duplicated per
platform.
"""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Sizes
# --------------------------------------------------------------------------

#: Number of RMS samples kept in the rolling buffer.
WAVE_SAMPLES = 80

#: Compact HUD (no preview): pen + wave only.
COMPACT_SIZE: Tuple[int, int] = (140, 40)

#: Expanded HUD (live preview on): wave on top, preview text below.
EXPANDED_SIZE: Tuple[int, int] = (420, 84)

#: Height reserved for the wave portion of the expanded HUD.
WAVE_HEIGHT_COMPACT = 34
WAVE_HEIGHT_EXPANDED = 44

#: Height reserved for the preview text line.
TEXT_HEIGHT = 26

CORNER_RADIUS = 10

# --------------------------------------------------------------------------
# Colours (RGBA, 0-255)
# --------------------------------------------------------------------------

INK_COLOR: Tuple[int, int, int, int] = (233, 240, 255, 235)
INK_DIM_COLOR: Tuple[int, int, int, int] = (170, 185, 215, 200)
NIB_COLOR: Tuple[int, int, int, int] = (255, 214, 140, 255)
#: The plate is nearly opaque on purpose.  A light translucent plate turns
#: into washed-out grey over a light desktop, and the pale ink then has
#: almost no contrast left.
BACKGROUND_COLOR: Tuple[int, int, int, int] = (16, 18, 26, 215)
PREVIEW_TEXT_COLOR: Tuple[int, int, int, int] = (226, 232, 245, 165)
PREVIEW_TEXT_FONT_SIZE = 12

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

#: Redraw cap for the waveform.
REDRAW_FPS = 30
FRAME_INTERVAL = 1.0 / REDRAW_FPS

#: Redraw cap for preview text (partial results arrive faster than this).
TEXT_FPS = 15
TEXT_FRAME_INTERVAL = 1.0 / TEXT_FPS

#: Fade-out duration when the HUD reaches the "done" state.
FADE_OUT_SECONDS = 0.45

#: Idle (processing) pulse period for the frozen stroke.
PROCESSING_PULSE_SECONDS = 1.1

# --------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------

STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_PROCESSING = "processing"
STATE_DONE = "done"

HUD_STATES = (STATE_IDLE, STATE_LISTENING, STATE_PROCESSING, STATE_DONE)


# --------------------------------------------------------------------------
# Wave geometry
# --------------------------------------------------------------------------

#: A drawn sample: (x, y, thickness) in HUD-local pixels.
WavePoint = Tuple[float, float, float]

#: RMS value that maps to a full-height wave.  Normal speaking volume at a
#: typical microphone gain sits around 0.03-0.10 RMS, so this keeps speech in
#: the middle of the range and lets loud peaks reach the top.  Zero RMS maps
#: to exactly zero, so a silent microphone draws a perfectly flat line.  No
#: floor is applied - a flat line during silence is useful diagnostic
#: information.
WAVE_REFERENCE_RMS = 0.12

#: Stroke thickness in px at zero and at full amplitude.
WAVE_THICKNESS_MIN = 1.1
WAVE_THICKNESS_MAX = 2.6

#: Horizontal gap between the HUD edge and the pen nib.
NIB_MARGIN = 10.0

#: Pen nib size in px.
NIB_WIDTH = 4.0
NIB_HEIGHT = 13.0


def normalise_amplitude(rms: float, reference: float = WAVE_REFERENCE_RMS) -> float:
    """Map a raw RMS value to a 0..1 amplitude.

    Zero RMS maps to exactly zero, so a silent microphone draws a perfectly
    flat line.  No floor is applied - a flat line during silence is useful
    diagnostic information.
    """
    if rms is None:
        return 0.0
    try:
        value = float(rms)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0.0:
        return 0.0
    if reference <= 0:
        reference = WAVE_REFERENCE_RMS
    normalised = value / reference
    if normalised > 1.0:
        normalised = 1.0
    # Gentle power curve: quiet speech stays visible without a hard floor.
    return normalised ** 0.65


def build_wave_points(
    rms_buffer: Sequence[float],
    width: float,
    height: float,
    samples: int = WAVE_SAMPLES,
    reference: float = WAVE_REFERENCE_RMS,
) -> List[WavePoint]:
    """Turn a rolling RMS buffer into drawable ``(x, y, thickness)`` points.

    The newest sample sits at the right edge of the HUD (that is where the
    pen nib is) and older samples trail off to the left, so the stroke reads
    as being written continuously from right to left.

    *rms_buffer* is ordered oldest -> newest.
    """
    if width <= 0 or height <= 0:
        return []

    values = list(rms_buffer)[-samples:] if samples > 0 else list(rms_buffer)
    if not values:
        return []

    count = len(values)
    spacing = width / float(max(1, samples - 1))
    middle = height / 2.0
    # Keep the wave inside the strip even at full amplitude.
    travel = middle * 0.86

    points: List[WavePoint] = []
    for index, raw in enumerate(values):
        distance_from_right = (count - 1 - index) * spacing
        x = width - distance_from_right
        amplitude = normalise_amplitude(raw, reference)
        y = middle - (amplitude * travel)
        thickness = WAVE_THICKNESS_MIN + (WAVE_THICKNESS_MAX - WAVE_THICKNESS_MIN) * amplitude
        points.append((x, y, thickness))
    return points


def nib_position(
    rms_buffer: Sequence[float],
    width: float,
    height: float,
    reference: float = WAVE_REFERENCE_RMS,
) -> Tuple[float, float]:
    """Return the ``(x, y)`` centre of the pen nib for the newest sample."""
    amplitude = 0.0
    if rms_buffer:
        amplitude = normalise_amplitude(rms_buffer[-1], reference)
    middle = height / 2.0
    travel = middle * 0.86
    x = width - NIB_MARGIN
    y = middle - (amplitude * travel)
    return x, y


def pulse_opacity(elapsed: float, period: float = PROCESSING_PULSE_SECONDS) -> float:
    """Soft 0.45..1.0 pulse used while the HUD is in the processing state."""
    if period <= 0:
        return 1.0
    phase = (elapsed % period) / period
    return 0.45 + 0.55 * (0.5 - 0.5 * math.cos(phase * 2.0 * math.pi))


def fade_opacity(elapsed: float, duration: float = FADE_OUT_SECONDS) -> float:
    """Linear 1.0 -> 0.0 fade used by the done state."""
    if duration <= 0:
        return 0.0
    if elapsed >= duration:
        return 0.0
    return max(0.0, 1.0 - (elapsed / duration))


# --------------------------------------------------------------------------
# Preview text layout
# --------------------------------------------------------------------------

ELLIPSIS = "\u2026"


def text_width(text: str, measure: Callable[[str], float]) -> float:
    """Width of *text* in px, using a platform supplied *measure* callable."""
    if not text:
        return 0.0
    try:
        return float(measure(text))
    except Exception:
        return 0.0


def truncate_from_left(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    ellipsis: str = ELLIPSIS,
) -> str:
    """Keep the tail of *text* visible, prefixing an ellipsis when cut.

    The preview line scrolls horizontally so the most recently spoken words
    stay on screen; when the text is wider than the HUD it is truncated from
    the left.
    """
    if not text:
        return ""
    if max_width <= 0:
        return ""
    if text_width(text, measure) <= max_width:
        return text

    ellipsis_width = text_width(ellipsis, measure)
    if ellipsis_width >= max_width:
        return ellipsis

    budget = max_width - ellipsis_width
    # Binary search for the longest suffix that still fits the budget.
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[len(text) - mid:] if mid else ""
        if mid == 0:
            best = candidate
            low = mid + 1
            continue
        if text_width(candidate, measure) <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return (ellipsis + best) if best else ellipsis


def preview_layout(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    ellipsis: str = ELLIPSIS,
) -> Tuple[str, float]:
    """Return ``(visible_text, width)`` for the single-line preview."""
    visible = truncate_from_left(text, measure, max_width, ellipsis=ellipsis)
    return visible, text_width(visible, measure)

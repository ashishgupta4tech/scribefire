"""Generated tray icons.

The icons are drawn at runtime with Pillow so no binary image files have to
ship with the project.  One icon per app state: idle, recording, processing.
"""

from __future__ import annotations

from typing import Dict

from .logs import get_logger

log = get_logger("icons")

#: Accent colour per state.
STATE_COLORS: Dict[str, tuple] = {
    "idle": (107, 114, 128),
    "recording": (239, 68, 68),
    "processing": (245, 158, 11),
    "muted": (55, 65, 81),
}

STATE_ICONS = ("idle", "recording", "processing")


def state_icon(state: str, size: int = 64):
    """Return a Pillow RGBA image for *state*."""
    from PIL import Image, ImageDraw

    state = (state or "idle").lower()
    if state not in STATE_COLORS:
        state = "idle"
    colour = STATE_COLORS[state]

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = max(2, size // 8)
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=max(3, size // 6),
        fill=colour + (255,),
    )

    # A small white nib in the middle, matching the HUD's pen.
    centre = size / 2.0
    nib = size * 0.22
    draw.polygon(
        [
            (centre, centre - nib),
            (centre + nib * 0.55, centre + nib),
            (centre - nib * 0.55, centre + nib),
        ],
        fill=(255, 255, 255, 235),
    )
    return image

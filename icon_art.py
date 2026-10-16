"""Tray icon artwork.

Two calm states, one visual language:

* Idle  - a warm charcoal disc with a thin quota ring and the provider letter.
          The ring sweeps clockwise from 12 o'clock; its length is the
          remaining quota, its colour shifts only when attention is needed.
* Live  - while a model is generating, the same full-size disc lights up in
          the provider's accent colour and carries the current TPS number.
          Dark disc -> lit disc is readable even at 16 px.

Both states fill the whole tray slot and share one serif family (Sitka Small,
falling back to Cambria / Palatino / Georgia) for letters and numerals.

Everything is drawn at 4x on a 256 px canvas and reduced once with Lanczos so
strokes stay crisp at the 16/20/24/32 px sizes Windows actually displays.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from providers import SPECS

SIZE = 64            # Pixel size handed to pystray (tests and Shell expect 64).
CANVAS = 256         # Supersampled drawing canvas.

# Warm, low-chroma palette: ivory on charcoal, accents kept quiet.
INK = (38, 37, 34, 255)            # #262622 disc
IVORY = (240, 238, 230, 255)       # #F0EEE6 letters


def _mix(base, top, amount):
    """Opaque blend; PIL draw calls replace pixels rather than compositing."""
    return tuple(round(b + (t - b) * amount) for b, t in zip(base[:3], top[:3])) + (255,)


INK_EDGE = _mix(INK, IVORY, 0.10)  # hairline that lifts the disc off dark taskbars
TRACK = _mix(INK, IVORY, 0.17)     # unfilled ring
MUTED = (164, 159, 148, 255)       # unknown state
AMBER = (229, 168, 75, 255)        # < 50 %
ROSE = (226, 92, 92, 255)          # < 20 %  (clay now belongs to Claude's accent)
CLAY = ROSE                        # legacy name for the danger colour
# Accents live in the provider registry; letters map to them.
ACCENTS = {spec.letter: spec.accent for spec in SPECS}
TILE_TEXT = (31, 30, 28, 255)      # dark numerals on a lit tile


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    dirs += [Path("/usr/share/fonts/msttcore"), Path("/usr/share/fonts/liberation-serif"),
             Path("/usr/share/fonts/liberation-sans"), Path("/usr/share/fonts/google-noto")]
    return dirs


def _load(candidates: tuple[tuple[str, str | None], ...], size: int):
    for name, variation in candidates:
        for folder in _font_dirs():
            path = folder / name
            if not path.is_file():
                continue
            try:
                face = ImageFont.truetype(str(path), size)
                if variation:
                    face.set_variation_by_name(variation)
                return face
            except (OSError, ValueError):
                continue
    return ImageFont.load_default(size)


# One elegant serif family for letters and numerals so both states read as a
# set.  Sitka (Windows 10+) has an optical "Small" cut drawn for tiny sizes;
# Cambria / Palatino / Georgia are fallbacks shipped with every Windows.
# All of these use lining figures except Georgia, which is therefore last.
SERIF = (
    ("SitkaVF.ttf", "Small Bold"),
    ("SitkaB.ttc", None),
    ("cambriab.ttf", None),
    ("palab.ttf", None),
    ("georgiab.ttf", None),
    ("LiberationSerif-Bold.ttf", None),
    ("LiberationSans-Bold.ttf", None),
)


@lru_cache(maxsize=64)
def letter_font(size: int):
    return _load(SERIF, size)


@lru_cache(maxsize=64)
def number_font(size: int):
    return _load(SERIF, size)


def accent(letter: str) -> tuple[int, int, int, int]:
    return ACCENTS.get(letter, IVORY)


def ring_color(percent: float | None, letter: str) -> tuple[int, int, int, int]:
    if percent is None:
        return MUTED
    if percent >= 50:
        return accent(letter)
    if percent >= 20:
        return AMBER
    return CLAY


def _hex(rgba: tuple[int, int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgba[:3])


def color_for(percent: float | None, letter: str = "C") -> str:
    """Hex colour for a quota level (kept for callers that want a swatch)."""
    return _hex(ring_color(percent, letter))


def _centered_text(draw: ImageDraw.ImageDraw, text: str, face, box, fill, optical_dy: float = 0.0) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=face)
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    draw.text((cx - (left + right) / 2, cy - (top + bottom) / 2 + optical_dy), text, font=face, fill=fill)


def _fit(text: str, loader, max_w: float, max_h: float, start: int, draw) -> ImageFont.FreeTypeFont:
    for size in range(start, 24, -4):
        face = loader(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=face)
        if right - left <= max_w and bottom - top <= max_h:
            return face
    return loader(24)


def _finish(canvas: Image.Image) -> Image.Image:
    return canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def _stale_badge(canvas: Image.Image) -> None:
    """Amber dot at the top-right with a transparent knock-out gap."""
    cx, cy, r, gap = 214, 42, 36, 14
    knock = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(knock).ellipse((cx - r - gap, cy - r - gap, cx + r + gap, cy + r + gap), fill=255)
    alpha = canvas.getchannel("A")
    alpha.paste(0, (0, 0), knock)
    canvas.putalpha(alpha)
    ImageDraw.Draw(canvas).ellipse((cx - r, cy - r, cx + r, cy + r), fill=AMBER)


# Shared geometry: both states are full-bleed discs so the icon uses the
# whole tray slot.  The ring hugs the edge; the glyph gets the rest.
DISC = (0, 0, CANVAS - 1, CANVAS - 1)
RING_W = 26
RING_BOX = (RING_W / 2 + 1, RING_W / 2 + 1, CANVAS - RING_W / 2 - 2, CANVAS - RING_W / 2 - 2)


def _ring(draw: ImageDraw.ImageDraw, value: float | None, color, track) -> None:
    box, width = RING_BOX, RING_W
    # PIL strokes ellipses inward from the box, so expand by half the width.
    outer = (box[0] - width / 2, box[1] - width / 2, box[2] + width / 2, box[3] + width / 2)
    draw.ellipse(outer, outline=track, width=width)
    if value is None:
        return
    value = max(0.0, min(100.0, value))
    if value >= 99.5:
        draw.ellipse(outer, outline=color, width=width)
        return
    if value <= 0:
        return
    sweep = max(12.0, 360.0 * value / 100.0)
    start, end = -90.0, -90.0 + sweep
    draw.arc(outer, start, end, fill=color, width=width)
    radius = (box[2] - box[0]) / 2
    cx = (box[0] + box[2]) / 2
    for angle in (start, end):  # round caps
        x = cx + radius * math.cos(math.radians(angle))
        y = cx + radius * math.sin(math.radians(angle))
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=color)


def quota_icon(remaining: float | None, letter: str, stale: bool = False) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(DISC, fill=INK)
    value = None if remaining is None else max(0.0, min(100.0, remaining))
    _ring(draw, value, ring_color(value, letter), TRACK)

    label, fill = (letter, IVORY) if remaining is not None else ("?", MUTED)
    face = _fit(label, letter_font, 176, 168, 212, draw)
    _centered_text(draw, label, face, (0, 0, CANVAS, CANVAS), fill)
    if stale:
        _stale_badge(canvas)
    return _finish(canvas)


def tps_text(value: float) -> str:
    if value < 1000:
        return str(round(value))
    return f"{value / 1000:.0f}k" if value < 10_000 else "9k+"


def live_icon(tps: float, letter: str) -> Image.Image:
    """Same disc, lit in the provider accent, carrying the TPS number."""
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    color = accent(letter)
    draw.ellipse(DISC, fill=color)
    text = tps_text(tps)
    # Widest chord available inside the hairline at numeral height.
    wide = len(text) >= 3
    face = _fit(text, number_font, 230 if wide else 212, 132 if wide else 176, 220, draw)
    _centered_text(draw, text, face, (0, 0, CANVAS, CANVAS), TILE_TEXT)
    return _finish(canvas)

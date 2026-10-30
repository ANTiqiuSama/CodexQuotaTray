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


TRACK = _mix(INK, IVORY, 0.17)     # unfilled ring
MUTED = (164, 159, 148, 255)       # unknown state
AMBER = (229, 168, 75, 255)        # < 50 %
ROSE = (226, 92, 92, 255)          # < 20 %  (clay now belongs to Claude's accent)
CLAY = ROSE                        # legacy name for the danger colour
# Accents live in the provider registry; letters map to them.
ACCENTS = {spec.letter: spec.accent for spec in SPECS}
TILE_TEXT = (31, 30, 28, 255)      # dark numerals on a lit tile
POOL_BLUES = ((35, 90, 230, 255), (40, 212, 236, 255))
WINDOW_SHADES = {"C": (ACCENTS["C"], (38, 140, 168, 255)),
                 "A": (ACCENTS["A"], (240, 196, 150, 255))}
PAID = (247, 201, 72, 255)


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


# Sources drawn in their own TPS colour at every quota level, so the idle ring,
# the panel bars and the lit TPS disc never disagree (Gemini's pools often sit
# below 50 % and used to flip between lavender and amber).
UNIFORM_LETTERS = {"G"}


def ring_color(percent: float | None, letter: str) -> tuple[int, int, int, int]:
    if percent is None:
        return MUTED
    if letter in UNIFORM_LETTERS:
        return accent(letter)
    if percent >= 50:
        return accent(letter)
    if percent >= 20:
        return AMBER
    return CLAY


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


def _pace_tick(draw: ImageDraw.ImageDraw, marker: float) -> None:
    """A short ivory notch across the ring: where the arc 'should' end now.
    An arc that stops before the notch means spending faster than planned."""
    angle = math.radians(-90.0 + 360.0 * max(0.0, min(100.0, marker)) / 100.0)
    radius = (RING_BOX[2] - RING_BOX[0]) / 2
    centre = (RING_BOX[0] + RING_BOX[2]) / 2
    inner, outer = radius - RING_W * 0.5, radius + RING_W * 0.62
    points = [(centre + r * math.cos(angle), centre + r * math.sin(angle)) for r in (inner, outer)]
    draw.line(points, fill=INK, width=30)
    draw.line(points, fill=IVORY, width=13)


RESET_BLUE = (112, 182, 255, 255)   # "a global reset looks likely" badge


def _reset_badge(canvas: Image.Image) -> None:
    """Sky-blue dot at the top-left (opposite the stale dot), knocked out."""
    cx, cy, r, gap = 42, 42, 38, 14
    knock = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(knock).ellipse((cx - r - gap, cy - r - gap, cx + r + gap, cy + r + gap), fill=255)
    alpha = canvas.getchannel("A")
    alpha.paste(0, (0, 0), knock)
    canvas.putalpha(alpha)
    ImageDraw.Draw(canvas).ellipse((cx - r, cy - r, cx + r, cy + r), fill=RESET_BLUE)


@lru_cache(maxsize=128)
def quota_icon(remaining: float | None, letter: str, stale: bool = False, marker: float | None = None,
               reset_hint: bool = False) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(DISC, fill=INK)
    value = None if remaining is None else max(0.0, min(100.0, remaining))
    _ring(draw, value, ring_color(value, letter), TRACK)
    if marker is not None and value is not None:
        _pace_tick(draw, marker)

    label, fill = (letter, IVORY) if remaining is not None else ("?", MUTED)
    face = _fit(label, letter_font, 176, 168, 212, draw)
    _centered_text(draw, label, face, (0, 0, CANVAS, CANVAS), fill)
    if stale:
        _stale_badge(canvas)
    if reset_hint:
        _reset_badge(canvas)
    return _finish(canvas)


def tps_text(value: float) -> str:
    if value < 1000:
        return str(round(value))
    return f"{value / 1000:.0f}k" if value < 10_000 else "9k+"


@lru_cache(maxsize=64)
def live_icon(tps: float, letter: str, reset_hint: bool = False) -> Image.Image:
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
    if reset_hint:
        _reset_badge(canvas)
    return _finish(canvas)


@lru_cache(maxsize=128)
def ring_icon(values: tuple, letter: str, stale: bool = False, marker: float | None = None,
              reset_hint: bool = False, colors: tuple | None = None) -> Image.Image:
    """Keep the original disc and glyph; draw two windows in the outer ring.

    The left and right halves both fill from the top towards the bottom.  A
    short gap at each pole separates the two windows even at tray size.
    """
    values = values[:2] or (None,)
    if len(values) == 1:
        return quota_icon(values[0], letter, stale, marker, reset_hint)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(DISC, fill=INK)
    outer = (0, 0, CANVAS - 1, CANVAS - 1)
    shades = colors or WINDOW_SHADES.get(letter, (accent(letter), accent(letter)))
    for index, value in enumerate(values):
        # PIL angles increase clockwise.  The left half is drawn backwards.
        lo, hi = ((96, 264) if index == 0 else (-84, 84))
        draw.arc(outer, lo, hi, fill=TRACK, width=RING_W)
        if value is None or value <= 0:
            continue
        sweep = 168 * min(100.0, float(value)) / 100
        color = shades[index] if colors or value >= 50 or letter in UNIFORM_LETTERS else ring_color(value, letter)
        start, end = (hi - sweep, hi) if index == 0 else (lo, lo + sweep)
        draw.arc(outer, start, end, fill=color, width=RING_W)
    label, fill = (letter, IVORY) if any(value is not None for value in values) else ("?", MUTED)
    face = _fit(label, letter_font, 176, 168, 212, draw)
    _centered_text(draw, label, face, (0, 0, CANVAS, CANVAS), fill)
    if stale:
        _stale_badge(canvas)
    if reset_hint:
        _reset_badge(canvas)
    return _finish(canvas)


@lru_cache(maxsize=64)
def waiting_icon(hours: int, letter: str, reset_hint: bool = False) -> Image.Image:
    """Old-style dark disc and empty source-coloured ring, with red reset hours."""
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(DISC, fill=INK)
    _ring(draw, None, MUTED, _mix(INK, accent(letter), .38))
    text = str(max(1, int(hours)))
    wide = len(text) >= 3
    face = _fit(text, number_font, 230 if wide else 212, 132 if wide else 176, 220, draw)
    _centered_text(draw, text, face, (0, 0, CANVAS, CANVAS), CLAY)
    if reset_hint:
        _reset_badge(canvas)
    return _finish(canvas)

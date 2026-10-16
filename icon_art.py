"""Tray icon artwork.

Two calm states, one visual language:

* Idle  - a warm charcoal disc with a thin quota ring and the provider letter.
          The ring sweeps clockwise from 12 o'clock; its length is the
          remaining quota, its colour shifts only when attention is needed.
* Live  - while a model is generating, the disc becomes a lit rounded tile in
          the provider's accent colour carrying the current TPS number.
          A shape change (circle -> tile) is readable even at 16 px.

Everything is drawn at 4x on a 256 px canvas and reduced once with Lanczos so
strokes stay crisp at the 16/20/24/32 px sizes Windows actually displays.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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
CLAY = (217, 119, 87, 255)         # < 20 %, Claude clay
ACCENTS = {
    "C": (126, 201, 166, 255),     # Codex  - sage mint
    "G": (203, 208, 216, 255),     # Grok   - pale silver
    "M": (164, 176, 245, 255),     # Gemini - periwinkle
}
TILE_TEXT = (31, 30, 28, 255)      # dark numerals on a lit tile


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    dirs += [Path("/usr/share/fonts/liberation-sans"), Path("/usr/share/fonts/google-noto")]
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


@lru_cache(maxsize=64)
def letter_font(size: int):
    return _load((
        ("SegUIVar.ttf", "Display Semibold"),
        ("segoeuisb.ttf", None),
        ("segoeuib.ttf", None),
        ("LiberationSans-Bold.ttf", None),
        ("arialbd.ttf", None),
    ), size)


@lru_cache(maxsize=64)
def number_font(size: int):
    # Bahnschrift (Windows 10+) is a DIN-style variable face; its condensed
    # cut lets three digits sit comfortably inside a 16 px icon.
    return _load((
        ("bahnschrift.ttf", "SemiBold Condensed"),
        ("bahnschrift.ttf", "Bold"),
        ("segoeuib.ttf", None),
        ("LiberationSans-Bold.ttf", None),
    ), size)


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
    cx, cy, r, gap = 210, 46, 34, 14
    knock = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(knock).ellipse((cx - r - gap, cy - r - gap, cx + r + gap, cy + r + gap), fill=255)
    alpha = canvas.getchannel("A")
    alpha.paste(0, (0, 0), knock)
    canvas.putalpha(alpha)
    ImageDraw.Draw(canvas).ellipse((cx - r, cy - r, cx + r, cy + r), fill=AMBER)


def quota_icon(remaining: float | None, letter: str, stale: bool = False) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((2, 2, 253, 253), fill=INK, outline=INK_EDGE, width=5)

    ring_box, ring_w = (17, 17, 238, 238), 28
    draw.ellipse(ring_box, outline=TRACK, width=ring_w)
    if remaining is not None:
        value = max(0.0, min(100.0, remaining))
        color = ring_color(value, letter)
        if value >= 99.5:
            draw.ellipse(ring_box, outline=color, width=ring_w)
        elif value > 0:
            sweep = max(12.0, 360.0 * value / 100.0)
            start, end = -90.0, -90.0 + sweep
            draw.arc(ring_box, start, end, fill=color, width=ring_w)
            # Round caps keep the arc from looking chopped at small sizes.
            radius = (ring_box[2] - ring_box[0]) / 2 - ring_w / 2
            for angle in (start, end):
                x = 127.5 + radius * math.cos(math.radians(angle))
                y = 127.5 + radius * math.sin(math.radians(angle))
                draw.ellipse((x - ring_w / 2, y - ring_w / 2, x + ring_w / 2, y + ring_w / 2), fill=color)

    label, fill = (letter, IVORY) if remaining is not None else ("?", MUTED)
    face = letter_font(150)
    _centered_text(draw, label, face, (0, 0, CANVAS, CANVAS), fill, optical_dy=2)
    if stale:
        _stale_badge(canvas)
    return _finish(canvas)


def tps_text(value: float) -> str:
    if value < 1000:
        return str(round(value))
    return f"{value / 1000:.0f}k" if value < 10_000 else "9k+"


def live_icon(tps: float, letter: str) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((4, 4, 251, 251), radius=64, fill=accent(letter))
    text = tps_text(tps)
    face = _fit(text, number_font, 214, 168, 200, draw)
    _centered_text(draw, text, face, (0, 0, CANVAS, CANVAS), TILE_TEXT)
    return _finish(canvas)

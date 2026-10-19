"""Hover detail card: data model and PIL renderer.

The card shares the tray icons' palette (warm charcoal, ivory type, quiet
accents) and is drawn at the monitor's real DPI, then handed to a layered
window by hover_details. Rendering is pure PIL so it can be tested anywhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from icon_art import AMBER, CLAY, IVORY, RESET_BLUE, accent, ring_color

WIDTH = 340                      # Logical pixels; multiplied by DPI scale.
PAD = 18
MARGIN = 14                      # Transparent room for the soft shadow.
RADIUS = 14

SURFACE = (36, 35, 32, 250)
BORDER = (240, 238, 230, 30)
DIVIDER = (240, 238, 230, 22)
TRACK = (240, 238, 230, 30)
TEXT = IVORY
SECONDARY_TEXT = (196, 191, 180, 255)
MUTED_TEXT = (142, 137, 127, 255)
TONES = {
    "ok": (150, 201, 172, 255),
    "idle": SECONDARY_TEXT,
    "unknown": MUTED_TEXT,
    "warn": AMBER,
    "danger": CLAY,
}


@dataclass(frozen=True)
class CardModel:
    letter: str
    title: str
    status: str
    status_tone: str                       # "live" | "stale" | "error" | "idle"
    remaining: float | None
    used: float | None
    window: str
    bars: tuple[tuple[str, float | None], ...] = ()
    forecast_text: str = ""
    forecast_tone: str = "unknown"
    metrics: tuple[tuple[str, str], ...] = ()
    footer: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    error: str | None = None
    banner: str = ""                       # prominent one-off hint (e.g. a likely global reset)
    text: str = field(default="", compare=False)

    def as_text(self) -> str:
        return self.text


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    dirs.append(Path("/usr/share/fonts/google-noto-cjk"))  # Preview hosts only.
    return dirs


def _load(candidates, size: int):
    for name, index, variation in candidates:
        for folder in _font_dirs():
            path = folder / name
            if not path.is_file():
                continue
            try:
                face = ImageFont.truetype(str(path), size, index=index)
                if variation:
                    face.set_variation_by_name(variation)
                return face
            except (OSError, ValueError):
                continue
    return ImageFont.load_default(size)


# Microsoft YaHei UI (index 1 of the collection) covers CJK and Latin alike.
@lru_cache(maxsize=32)
def ui_font(size: int, weight: str = "regular"):
    if weight == "bold":
        return _load((("msyhbd.ttc", 1, None), ("msyhbd.ttc", 0, None), ("NotoSansSC-Medium.otf", 0, None)), size)
    return _load((("msyh.ttc", 1, None), ("msyh.ttc", 0, None), ("NotoSansSC-Regular.otf", 0, None)), size)


@lru_cache(maxsize=16)
def figure_font(size: int):
    return _load((("SegUIVar.ttf", 0, "Display Semibold"), ("segoeuisb.ttf", 0, None),
                  ("msyhbd.ttc", 1, None), ("NotoSansSC-Medium.otf", 0, None)), size)


def _wrap(draw, text: str, face, width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            if draw.textlength(line + char, font=face) <= width or not line:
                line += char
            else:
                lines.append(line.rstrip())
                line = char.lstrip()
        lines.append(line)
    return lines


def _with_alpha(color, alpha: int):
    return color[:3] + (alpha,)


class _Canvas:
    def __init__(self, scale: float):
        self.s = scale
        self.width = round(WIDTH * scale)
        self.image = Image.new("RGBA", (self.width, round(900 * scale)), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def px(self, value: float) -> int:
        return round(value * self.s)

    def font(self, size: float, weight: str = "regular"):
        return ui_font(max(6, self.px(size)), weight)

    def text(self, xy, text: str, face, fill, anchor="la"):
        self.draw.text((self.px(xy[0]), self.px(xy[1])), text, font=face, fill=fill, anchor=anchor)

    def line_height(self, face) -> float:
        top, bottom = face.getbbox("国Ag")[1::2]
        return (bottom - top) / self.s * 1.45


def render_card(model: CardModel, scale: float = 1.0) -> Image.Image:
    """Return a premultiplication-ready RGBA image including shadow margin."""
    c = _Canvas(scale)
    color = accent(model.letter)
    inner = WIDTH - 2 * PAD
    y = PAD

    # Header: accent dot, title, status pill.
    title_font = c.font(14, "bold")
    c.draw.ellipse((c.px(PAD), c.px(y + 5), c.px(PAD + 8), c.px(y + 13)), fill=color)
    c.text((PAD + 16, y + 9), model.title, title_font, TEXT, "lm")
    pill_font = c.font(11)
    pill_fg = {"live": (31, 30, 28, 255), "stale": AMBER, "error": CLAY}.get(model.status_tone, SECONDARY_TEXT)
    pill_bg = {"live": color, "stale": _with_alpha(AMBER, 40), "error": _with_alpha(CLAY, 40)}.get(model.status_tone, (240, 238, 230, 20))
    pill_w = c.draw.textlength(model.status, font=pill_font) / scale + 18
    right = WIDTH - PAD
    c.draw.rounded_rectangle((c.px(right - pill_w), c.px(y), c.px(right), c.px(y + 18)), radius=c.px(9), fill=pill_bg)
    c.text((right - pill_w / 2, y + 9), model.status, pill_font, pill_fg, "mm")
    y += 32

    # Hero figure: remaining percentage of the binding window.
    figure = figure_font(c.px(38))
    hero = "—" if model.remaining is None else f"{model.remaining:.0f}"
    hero_color = TEXT if model.remaining is None else ring_color(model.remaining, model.letter)
    if model.remaining is not None and model.remaining >= 50:
        hero_color = TEXT
    c.text((PAD - 1, y + 40), hero, figure, hero_color, "ls")
    hero_w = c.draw.textlength(hero, font=figure) / scale
    if model.remaining is not None:
        c.text((PAD + hero_w + 3, y + 40), "%", c.font(16, "bold"), hero_color, "ls")
        hero_w += c.draw.textlength("%", font=c.font(16, "bold")) / scale + 3
    caption = "剩余" + (f" · {model.window}" if model.window else "")
    c.text((PAD + hero_w + 8, y + 38), caption, c.font(12), SECONDARY_TEXT, "ls")
    if model.used is not None:
        c.text((right, y + 38), f"已用 {model.used:.0f}%", c.font(12), MUTED_TEXT, "rs")
    y += 54

    # Window bars.
    label_font = c.font(11)
    for label, value in model.bars:
        c.text((PAD, y), label, label_font, SECONDARY_TEXT, "lt")
        c.text((right, y), "未知" if value is None else f"{value:.0f}%", label_font, SECONDARY_TEXT, "rt")
        y += 19
        c.draw.rounded_rectangle((c.px(PAD), c.px(y), c.px(right), c.px(y + 6)), radius=c.px(3), fill=TRACK)
        if value is not None and value > 0:
            end = PAD + max(6.0, inner * min(100.0, value) / 100)
            c.draw.rounded_rectangle((c.px(PAD), c.px(y), c.px(end), c.px(y + 6)), radius=c.px(3), fill=ring_color(value, model.letter))
        y += 16

    # Forecast.
    if model.forecast_text:
        y += 4
        tone = TONES.get(model.forecast_tone, MUTED_TEXT)
        face = c.font(12)
        lines = _wrap(c.draw, model.forecast_text, face, c.px(inner - 14))
        lh = c.line_height(face)
        c.draw.ellipse((c.px(PAD), c.px(y + lh / 2 - 3), c.px(PAD + 6), c.px(y + lh / 2 + 3)), fill=tone)
        for line in lines:
            c.text((PAD + 14, y + lh / 2), line, face, tone if model.forecast_tone in ("warn", "danger") else TEXT, "lm")
            y += lh
        y += 6

    # Banner: tinted box, shown only for noteworthy events.
    if model.banner:
        y += 4
        face = c.font(12, "bold")
        lines = _wrap(c.draw, model.banner, face, c.px(inner - 20))
        lh = c.line_height(face)
        box_h = lh * len(lines) + 14
        c.draw.rounded_rectangle((c.px(PAD), c.px(y), c.px(right), c.px(y + box_h)), radius=c.px(8),
                                 fill=_with_alpha(RESET_BLUE, 38), outline=_with_alpha(RESET_BLUE, 110), width=max(1, c.px(1)))
        for index, line in enumerate(lines):
            c.text((PAD + 10, y + 7 + lh * index + lh / 2), line, face, RESET_BLUE, "lm")
        y += box_h + 8

    def divider():
        nonlocal y
        y += 6
        c.draw.line((c.px(PAD), c.px(y), c.px(right), c.px(y)), fill=DIVIDER, width=max(1, c.px(1)))
        y += 12

    # Metrics grid, two columns.
    if model.metrics:
        divider()
        col = inner / 2
        value_font = c.font(14, "bold")
        for index, (label, value) in enumerate(model.metrics):
            x = PAD + (index % 2) * col
            row_y = y + (index // 2) * 44
            c.text((x, row_y), label, c.font(11), MUTED_TEXT, "lt")
            c.text((x, row_y + 17), value, value_font, TEXT, "lt")
        y += ((len(model.metrics) + 1) // 2) * 44

    # Footer: reset, update, sources, errors.
    small = c.font(11)
    lh = c.line_height(small)
    blocks = [(line, SECONDARY_TEXT) for line in model.footer]
    blocks += [(line, MUTED_TEXT) for line in model.notes]
    if model.error:
        blocks.append((model.error, CLAY))
    if blocks:
        divider()
        for text, fill in blocks:
            for line in _wrap(c.draw, text, small, c.px(inner)):
                c.text((PAD, y + lh / 2), line, small, fill, "lm")
                y += lh
    y += PAD - 4

    height = c.px(y)
    card = c.image.crop((0, 0, c.width, height))
    return _compose(card, scale)


@lru_cache(maxsize=8)
def _shadow(width: int, height: int, scale: float) -> Image.Image:
    margin = round(MARGIN * scale)
    shadow = Image.new("RGBA", (width + 2 * margin, height + 2 * margin), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (margin, margin + round(3 * scale), margin + width, margin + height + round(3 * scale)),
        radius=round(RADIUS * scale), fill=(0, 0, 0, 120))
    return shadow.filter(ImageFilter.GaussianBlur(round(7 * scale)))


def _compose(content: Image.Image, scale: float) -> Image.Image:
    width, height = content.size
    margin = round(MARGIN * scale)
    radius = round(RADIUS * scale)
    image = _shadow(width, height, scale).copy()
    surface = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(surface).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius,
        fill=SURFACE, outline=BORDER, width=max(1, round(scale)))
    surface.alpha_composite(content)
    image.alpha_composite(surface, (margin, margin))
    return image


def text_card(text: str) -> CardModel:
    """Minimal card for callers that only have plain text."""
    lines = text.splitlines() or [""]
    return CardModel("C", lines[0], "详情", "idle", None, None, "", notes=tuple(lines[1:]), text=text)

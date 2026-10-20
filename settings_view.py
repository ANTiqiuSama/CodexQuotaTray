"""Pictures for the settings window, drawn with Pillow in the panel's style.

Tk only lays these out; everything visual lives here so it renders the same on
every Windows build (and can be previewed without a display)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from PIL import Image, ImageDraw

from detail_card import MUTED_TEXT, SECONDARY_TEXT, TEXT, ui_font, figure_font
from icon_art import AMBER, CLAY, accent, ring_color

# Window palette (Tk wants hex).
BG = "#1b1a18"
SIDEBAR = "#22211e"
SURFACE = "#2a2925"
SURFACE_HI = "#33322d"
BORDER = "#3a3833"
INK = "#f0eee6"
INK_2 = "#c4bfb4"
INK_3 = "#8e897f"
FIELD = "#201f1c"

CARD_W, CARD_H = 318, 214      # three quota rows; cards with fewer keep the same size for an even grid
CARD_RGB = (42, 41, 37, 255)
CARD_EDGE = (58, 56, 51, 255)


def mix(top, amount, base=(42, 41, 37)):
    """Opaque blend onto the card: draw calls replace pixels, they do not composite."""
    return tuple(round(b + (t - b) * amount) for b, t in zip(base[:3], top[:3])) + (255,)


IVORY_RGB = (240, 238, 230)
TRACK_RGB = mix(IVORY_RGB, .11)
RULE = mix(IVORY_RGB, .07)
TONE_COLORS = {"danger": CLAY, "warn": AMBER, "ok": SECONDARY_TEXT, "idle": MUTED_TEXT, "unknown": MUTED_TEXT}
PILL = {"live": None, "idle": (mix(IVORY_RGB, .09), SECONDARY_TEXT), "stale": (mix(AMBER, .2), AMBER),
        "error": (mix(CLAY, .2), CLAY)}


def hex_color(rgba) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgba[:3])


def countdown(timestamp: float | None, now: float | None = None) -> str:
    if not timestamp:
        return ""
    delta = int(timestamp - (now or time.time()))
    if delta <= 0:
        return "已到重置时间"
    days, rest = divmod(delta, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} 天 {hours} 小时后重置"
    if hours:
        return f"{hours} 小时 {rest // 60} 分后重置"
    return f"{max(1, rest // 60)} 分钟后重置"


def window_name(minutes: int | None) -> str:
    if not minutes:
        return "额度"
    if minutes == 10080:
        return "每周"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


@dataclass(frozen=True)
class SourceCard:
    provider: str
    letter: str
    name: str
    status: str
    tone: str                       # live | idle | stale | error
    rows: tuple                     # (label, remaining %, reset text)
    tps: float | None
    rate60: float
    active: int
    forecast: str
    forecast_tone: str
    note: str = ""                  # e.g. the read error


def quota_rows(snapshot, now: float | None = None) -> tuple:
    """Up to three (label, remaining, reset) rows: model pools or the two windows."""
    if snapshot is None:
        return ()
    rows = []
    if snapshot.pools and len(snapshot.pools) > 1:
        for pool in snapshot.pools[:3]:
            rows.append((str(pool.get("label") or pool.get("key") or "额度"), pool.get("remaining_percent"),
                         countdown(pool.get("resets_at"), now)))
    else:
        rows.append((window_name(snapshot.window_minutes), snapshot.remaining_percent, countdown(snapshot.resets_at, now)))
        if snapshot.secondary_window_minutes or snapshot.secondary_remaining_percent is not None:
            rows.append((window_name(snapshot.secondary_window_minutes), snapshot.secondary_remaining_percent,
                         countdown(snapshot.secondary_resets_at, now)))
    return tuple(rows)


def _rounded(size, radius, fill, outline=None, scale=1.0):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(image).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=round(radius * scale), fill=fill,
                                            outline=outline, width=max(1, round(scale)))
    return image


def _fit(draw, text, face, limit):
    if draw.textlength(text, font=face) <= limit:
        return text
    while text and draw.textlength(text + "…", font=face) > limit:
        text = text[:-1]
    return text + "…"


def source_card(card: SourceCard, icon: Image.Image | None, scale: float = 1.0, hover: bool = False) -> Image.Image:
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    color = accent(card.letter)
    image = _rounded((px(CARD_W), px(CARD_H)), 12, (47, 46, 41, 255) if hover else CARD_RGB,
                     tuple(round(c * .55 + e * .45) for c, e in zip(color[:3], CARD_EDGE[:3])) + (255,) if hover else CARD_EDGE, s)
    draw = ImageDraw.Draw(image)
    # Header: the live tray icon, name and a status pill.
    if icon is not None:
        image.alpha_composite(icon.convert("RGBA").resize((px(34), px(34)), Image.LANCZOS), (px(16), px(14)))
    else:
        draw.ellipse((px(16), px(14), px(50), px(48)), fill=color)
    title = ui_font(px(14), "bold")
    small, tiny = ui_font(px(11)), ui_font(px(10))
    pill_w = draw.textlength(card.status, font=small) / s + 18
    draw.text((px(60), px(31)), _fit(draw, card.name, title, px(CARD_W - 60 - pill_w - 24)), font=title, fill=TEXT, anchor="lm")
    fill, ink = PILL.get(card.tone) or ((*color[:3], 255), (31, 30, 28, 255))
    draw.rounded_rectangle((px(CARD_W - 16 - pill_w), px(22), px(CARD_W - 16), px(40)), radius=px(9), fill=fill)
    draw.text((px(CARD_W - 16 - pill_w / 2), px(31)), card.status, font=small, fill=ink, anchor="mm")

    # Quota rows: label · reset, value, bar in the same colour as the tray ring.
    top = 62
    rows = list(card.rows[:3]) or [("额度", None, card.note or "正在读取")]
    for index in range(3):
        y = top + index * 34
        if index >= len(rows):
            continue
        label, value, reset = rows[index]
        draw.text((px(16), px(y)), label, font=small, fill=SECONDARY_TEXT, anchor="lt")
        if reset:
            draw.text((px(16 + draw.textlength(label, font=small) / s + 8), px(y + 1)), reset, font=tiny, fill=MUTED_TEXT, anchor="lt")
        number = "—" if value is None else f"{value:.0f}%"
        shade = ring_color(value, card.letter) if value is not None else MUTED_TEXT
        draw.text((px(CARD_W - 16), px(y - 1)), number, font=figure_font(px(14)), fill=shade, anchor="rt")
        bar_top = y + 18
        draw.rounded_rectangle((px(16), px(bar_top), px(CARD_W - 16), px(bar_top + 5)), radius=px(2.5), fill=TRACK_RGB)
        if value:
            right = 16 + (CARD_W - 32) * max(0.0, min(100.0, value)) / 100
            draw.rounded_rectangle((px(16), px(bar_top), px(max(20, right)), px(bar_top + 5)), radius=px(2.5), fill=shade)

    # Rates.
    y = 166
    draw.line((px(16), px(y - 6), px(CARD_W - 16), px(y - 6)), fill=RULE, width=max(1, round(s)))
    tps = card.tps or 0.0
    big = figure_font(px(20))
    number = f"{tps:.0f}"
    draw.text((px(16), px(y + 15)), number, font=big, fill=color if card.tps else TEXT, anchor="ls")
    after = 16 + draw.textlength(number, font=big) / s + 4
    draw.text((px(after), px(y + 15)), "tok/s", font=tiny, fill=SECONDARY_TEXT, anchor="ls")
    meta = f"60 秒 {card.rate60:.1f} tok/s · {card.active} 个会话"
    draw.text((px(CARD_W - 16), px(y + 15)), meta, font=tiny, fill=MUTED_TEXT, anchor="rs")
    forecast = card.forecast or card.note or "正在积累用量，稍后给出预测"
    tint = TONE_COLORS.get(card.forecast_tone, MUTED_TEXT) if card.forecast else MUTED_TEXT
    draw.text((px(16), px(y + 34)), _fit(draw, forecast, tiny, px(CARD_W - 32)), font=tiny, fill=tint, anchor="lm")
    return image


def activity_chart(events: Iterable[Any], now: float, letters: dict[str, str], scale: float = 1.0,
                   width: int = 652, height: int = 150, minutes: int = 30) -> Image.Image:
    """Output tokens per minute over the last half hour, stacked by source."""
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    image = _rounded((px(width), px(height)), 12, CARD_RGB, CARD_EDGE, s)
    draw = ImageDraw.Draw(image)
    small, tiny = ui_font(px(11)), ui_font(px(10))
    buckets: list[dict[str, int]] = [dict() for _ in range(minutes)]
    start = now - minutes * 60
    for event in events:
        ended = getattr(event, "ended_at", 0)
        if not (start <= ended <= now) or not getattr(event, "count_in_rate", True):
            continue
        index = min(minutes - 1, int((ended - start) // 60))
        provider = getattr(event, "provider", "other")
        if provider in letters:
            buckets[index][provider] = buckets[index].get(provider, 0) + int(getattr(event, "output_tokens", 0) or 0)
    total = sum(sum(bucket.values()) for bucket in buckets)
    draw.text((px(16), px(14)), "最近 30 分钟输出", font=ui_font(px(13), "bold"), fill=TEXT, anchor="lt")
    draw.text((px(width - 16), px(15)), f"共 {total:,} tokens", font=small, fill=SECONDARY_TEXT, anchor="rt")
    left, right, top, bottom = 16, width - 16, 42, height - 30
    peak = max([sum(bucket.values()) for bucket in buckets] + [1])
    for fraction in (0.5, 1.0):
        y = bottom - (bottom - top) * fraction
        draw.line((px(left), px(y), px(right), px(y)), fill=RULE, width=max(1, round(s)))
    slot = (right - left) / minutes
    for index, bucket in enumerate(buckets):
        x0 = left + index * slot + 1.5
        x1 = x0 + slot - 3
        base = bottom
        if not bucket:
            draw.rounded_rectangle((px(x0), px(bottom - 2), px(x1), px(bottom)), radius=px(1), fill=TRACK_RGB)
            continue
        for provider in letters:
            amount = bucket.get(provider, 0)
            if not amount:
                continue
            h = max(2.0, (bottom - top) * amount / peak)
            draw.rounded_rectangle((px(x0), px(base - h), px(x1), px(base)), radius=px(1.5), fill=accent(letters[provider]))
            base -= h
    draw.text((px(left), px(height - 14)), "30 分钟前", font=tiny, fill=MUTED_TEXT, anchor="lm")
    draw.text((px(right), px(height - 14)), "现在", font=tiny, fill=MUTED_TEXT, anchor="rm")
    # Legend for the sources that produced output.
    seen = [provider for provider in letters if any(provider in bucket for bucket in buckets)]
    x = right - 70
    for provider in reversed(seen):
        name = {"codex": "Codex", "grok": "Grok", "gemini": "Gemini", "claude": "Claude", "mimo": "MiMo"}.get(provider, provider)
        w = draw.textlength(name, font=tiny) / s
        x -= w + 20
        draw.ellipse((px(x), px(height - 18), px(x + 8), px(height - 10)), fill=accent(letters[provider]))
        draw.text((px(x + 12), px(height - 14)), name, font=tiny, fill=SECONDARY_TEXT, anchor="lm")
    if not total:
        draw.text((px(width / 2), px((top + bottom) / 2)), "这段时间没有输出", font=small, fill=MUTED_TEXT, anchor="mm")
    return image


def agent_rows(agents: list[dict], letters: dict[str, str], scale: float = 1.0, width: int = 652, limit: int = 8) -> Image.Image:
    """The agents the tray currently sees, one line each."""
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    shown = agents[:limit]
    height = 46 + max(1, len(shown)) * 38 + 10
    image = _rounded((px(width), px(height)), 12, CARD_RGB, CARD_EDGE, s)
    draw = ImageDraw.Draw(image)
    small, tiny = ui_font(px(12)), ui_font(px(10))
    draw.text((px(16), px(14)), "当前任务", font=ui_font(px(13), "bold"), fill=TEXT, anchor="lt")
    live = sum(1 for agent in agents if agent.get("live"))
    draw.text((px(width - 16), px(15)), f"{live} 个工作中 · 共 {len(agents)} 个", font=ui_font(px(11)), fill=SECONDARY_TEXT, anchor="rt")
    if not shown:
        draw.text((px(width / 2), px(46 + 19)), "暂无工作中的 agent", font=small, fill=MUTED_TEXT, anchor="mm")
    for index, agent in enumerate(shown):
        y = 46 + index * 38
        if index:
            draw.line((px(16), px(y), px(width - 16), px(y)), fill=RULE, width=max(1, round(s)))
        color = accent(letters.get(agent.get("provider"), "?"))
        cy = y + 19
        if agent.get("live"):
            draw.ellipse((px(16), px(cy - 6), px(28), px(cy + 6)), fill=mix(color, .25))
        draw.ellipse((px(19), px(cy - 3), px(25), px(cy + 3)), fill=color if agent.get("live") else mix(color, .45))
        title = str(agent.get("title") or agent.get("thread_title") or agent.get("folder") or agent.get("source") or "会话")
        draw.text((px(38), px(cy - 7)), _fit(draw, title, small, px(width * .5)), font=small, fill=TEXT, anchor="lm")
        where = " · ".join(part for part in (agent.get("source"), agent.get("folder"), "herdr " + agent["pane"] if agent.get("pane") else "") if part)
        draw.text((px(38), px(cy + 9)), _fit(draw, where, tiny, px(width * .5)), font=tiny, fill=MUTED_TEXT, anchor="lm")
        doing = str(agent.get("doing") or ("工作中" if agent.get("live") else "刚完成"))
        tps = agent.get("tps")
        draw.text((px(width - 16), px(cy - 7)), f"{tps:.0f} tok/s" if tps else "—", font=ui_font(px(11)), fill=color if tps else MUTED_TEXT, anchor="rm")
        draw.text((px(width - 16), px(cy + 9)), _fit(draw, doing, tiny, px(width * .35)), font=tiny,
                  fill=SECONDARY_TEXT if agent.get("live") else MUTED_TEXT, anchor="rm")
    return image


def health_tone(status: str) -> str:
    if status in ("已接入",):
        return "ok"
    if any(word in status for word in ("失败", "不可", "未运行")):
        return "error"
    return "idle"

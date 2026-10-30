"""Pictures for the settings window, drawn with Pillow in the panel's style.

Tk only lays these out; everything visual lives here so it renders the same on
every Windows build (and can be previewed without a display)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from PIL import Image, ImageDraw

from detail_card import MUTED_TEXT, SECONDARY_TEXT, TEXT, ui_font, figure_font
from forecast import reset_distance
from icon_art import AMBER, CLAY, PAID, accent, ring_color

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
GREEN = "#7ec9a6"               # primary actions and the selected page
WARN = "#e5a84b"

CARD_W, CARD_H = 240, 200      # three to a row; every card keeps the three-row height for an even grid
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
        "error": (mix(CLAY, .2), CLAY), "paid": (mix(PAID, .2), PAID), "blocked": (mix(CLAY, .2), CLAY)}
ROW_INK = {"paid": PAID, "blocked": CLAY}


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
    if 524_000 <= minutes <= 528_000:
        return "年度"
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
    tone: str                       # live | idle | stale | error | paid | blocked
    rows: tuple                     # (title, remaining %, caption, tone, figure): the panel's header columns
    tps: float | None
    rate60: float
    active: int
    forecast: str
    forecast_tone: str
    note: str = ""                  # e.g. the read error
    refresh: str = ""               # when this source is read next, e.g. "12 秒后刷新 · 悬停"


def quota_rows(snapshot, now: float | None = None) -> tuple:
    """Up to three (title, remaining, caption, tone, figure) rows, written like the
    panel's columns: the time left to the reset on top, the window or pool beside it."""
    if snapshot is None:
        return ()
    now = now or time.time()
    rows = []
    if snapshot.pools and len(snapshot.pools) > 1:
        windows = [(pool.get("resets_at"), pool.get("remaining_percent"), str(pool.get("label") or pool.get("key") or "额度"))
                   for pool in snapshot.pools[:3]]
    else:
        windows = [(snapshot.resets_at, snapshot.remaining_percent, window_name(snapshot.window_minutes))]
        if snapshot.secondary_window_minutes or snapshot.secondary_remaining_percent is not None:
            windows.append((snapshot.secondary_resets_at, snapshot.secondary_remaining_percent,
                            window_name(snapshot.secondary_window_minutes)))
    for stamp, value, name in windows:
        left = reset_distance(stamp, now)
        rows.append((left or name, value, name if left else "", "", None))
    return tuple(rows)


def refresh_text(seconds: float | None, reason: str) -> str:
    """"12 秒后刷新 · 悬停" for the card footer."""
    if seconds is None:
        return ""
    if reason == "正在刷新":
        return "正在刷新"
    seconds = round(seconds)
    when = "即将刷新" if seconds <= 1 else (f"{seconds} 秒后刷新" if seconds < 90 else f"{round(seconds / 60)} 分钟后刷新")
    return f"{when} · {reason}" if reason else when


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


ROW_H = 31


def card_height(slots: int) -> int:
    """Cards in one grid row share the height of the one with most quota rows."""
    return CARD_H - (3 - max(1, min(3, slots))) * ROW_H


def source_card(card: SourceCard, icon: Image.Image | None, scale: float = 1.0, hover: bool = False, slots: int = 3) -> Image.Image:
    """One source as the hover panel shows it: same columns, tones and colours."""
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    color = accent(card.letter)
    edge = tuple(round(c * .55 + e * .45) for c, e in zip(color[:3], CARD_EDGE[:3])) + (255,) if hover else CARD_EDGE
    image = _rounded((px(CARD_W), px(card_height(slots))), 12, (47, 46, 41, 255) if hover else CARD_RGB, edge, s)
    draw = ImageDraw.Draw(image)
    if icon is not None:
        image.alpha_composite(icon.convert("RGBA").resize((px(28), px(28)), Image.LANCZOS), (px(14), px(14)))
    else:
        draw.ellipse((px(14), px(14), px(42), px(42)), fill=color)
    title, small, tiny = ui_font(px(13), "bold"), ui_font(px(10)), ui_font(px(9))
    pill_w = draw.textlength(card.status, font=tiny) / s + 14
    fill, ink = PILL.get(card.tone) or ((*color[:3], 255), (31, 30, 28, 255))
    draw.rounded_rectangle((px(CARD_W - 14 - pill_w), px(19), px(CARD_W - 14), px(37)), radius=px(9), fill=fill)
    draw.text((px(CARD_W - 14 - pill_w / 2), px(28)), card.status, font=tiny, fill=ink, anchor="mm")
    draw.text((px(50), px(28)), _fit(draw, card.name, title, px(CARD_W - 50 - pill_w - 22)), font=title, fill=TEXT, anchor="lm")

    rows = list(card.rows[:3]) or [("额度", None, card.note or "正在读取", "", None)]
    for index, row in enumerate(rows):
        label, value, caption, tone, figure = (tuple(row) + ("", "", None))[:5]
        y = 54 + index * ROW_H
        ink = ROW_INK.get(tone)
        draw.text((px(14), px(y)), _fit(draw, str(label), small, px(CARD_W * .52)), font=small, fill=SECONDARY_TEXT, anchor="lt")
        if caption:
            x = 14 + draw.textlength(_fit(draw, str(label), small, px(CARD_W * .52)), font=small) / s + 6
            draw.text((px(x), px(y + 1)), _fit(draw, str(caption), tiny, px(CARD_W - 76 - x)), font=tiny, fill=ink or MUTED_TEXT, anchor="lt")
        number = figure if figure else ("—" if value is None else f"{value:.0f}%")
        shade = ink or (ring_color(value, card.letter) if value is not None else MUTED_TEXT)
        draw.text((px(CARD_W - 14), px(y - 2)), number, font=figure_font(px(13)), fill=shade, anchor="rt")
        bar = y + 17
        draw.rounded_rectangle((px(14), px(bar), px(CARD_W - 14), px(bar + 4)), radius=px(2), fill=TRACK_RGB)
        if tone == "paid":
            draw.rounded_rectangle((px(14), px(bar), px(CARD_W - 14), px(bar + 4)), radius=px(2), fill=PAID)
        elif value:
            right = 14 + (CARD_W - 28) * max(0.0, min(100.0, value)) / 100
            draw.rounded_rectangle((px(14), px(bar), px(max(18, right)), px(bar + 4)), radius=px(2), fill=shade)

    y = card_height(slots) - 50
    draw.line((px(14), px(y), px(CARD_W - 14), px(y)), fill=RULE, width=max(1, round(s)))
    if card.provider != "gemini":
        tps = card.tps or 0.0
        big = figure_font(px(16))
        number = f"{tps:.0f}"
        draw.text((px(14), px(y + 22)), number, font=big, fill=color if card.tps else TEXT, anchor="ls")
        after = 14 + draw.textlength(number, font=big) / s + 3
        draw.text((px(after), px(y + 22)), "tok/s", font=tiny, fill=SECONDARY_TEXT, anchor="ls")
    meta = card.refresh or f"{card.active} 个会话"
    draw.text((px(CARD_W - 14), px(y + 21)), _fit(draw, meta, tiny, px(CARD_W * .62)), font=tiny, fill=MUTED_TEXT, anchor="rs")
    forecast = card.forecast or card.note or "正在积累用量，稍后给出预测"
    tint = TONE_COLORS.get(card.forecast_tone, MUTED_TEXT) if card.forecast else MUTED_TEXT
    draw.text((px(14), px(y + 37)), _fit(draw, forecast, tiny, px(CARD_W - 28)), font=tiny, fill=tint, anchor="lm")
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
        if agent.get("provider") != "gemini":
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

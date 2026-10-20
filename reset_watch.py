"""Community forecasts of OpenAI's early, global Codex usage resets.

OpenAI's Codex team occasionally resets everyone's Codex usage limits ahead of
schedule (announced by @thsottiaux on X).  Independent community trackers
publish a 24 h / 48 h probability for the next such reset.  This module reads
those public, key-free JSON endpoints; it sends no account data.  The figures
are community estimates, not an OpenAI schedule.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ENABLED = (os.environ.get("CODEX_QUOTA_RESET_WATCH") or "1").strip().lower() not in ("0", "off", "false", "no")
try:
    ALERT_PERCENT = max(1, min(100, int(os.environ.get("CODEX_QUOTA_RESET_ALERT") or 60)))
except ValueError:
    ALERT_PERCENT = 60
POLL_SECONDS = 30 * 60           # normal cadence
POLL_SECONDS_HIGH = 10 * 60      # while a reset looks likely
STALE_SECONDS = 3 * 3600         # older forecasts are not shown as current
USER_AGENT = "CodexQuotaTray (+https://github.com/ANTiqiuSama/CodexQuotaTray)"


@dataclass
class ResetOutlook:
    source: str
    page: str
    prob_24: int | None
    prob_48: int | None
    last_reset_at: float | None = None
    last_reset_id: str | None = None
    last_reset_text: str = ""
    promise: str = ""
    fetched_at: float = 0.0

    def level(self, now: float | None = None) -> str:
        """"high" (24 h chance at or above the alert), "watch" (48 h chance at
        or above it), "low", or "unknown" when the forecast is missing/old."""
        now = now or time.time()
        if self.prob_24 is None or now - self.fetched_at > STALE_SECONDS:
            return "unknown"
        if self.prob_24 >= ALERT_PERCENT:
            return "high"
        if (self.prob_48 or 0) >= ALERT_PERCENT:
            return "watch"
        return "low"


def _when(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")[:32]).timestamp()
    except ValueError:
        # Nanosecond fractions ("…04:18:22.032775575Z") exceed fromisoformat.
        text = str(value)
        if "." in text:
            head, tail = text.split(".", 1)
            zone = "+00:00" if tail.endswith("Z") else ""
            try:
                return datetime.fromisoformat(head + "." + tail.rstrip("Z")[:6] + zone).timestamp()
            except ValueError:
                return None
        return None


def _percent(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(max(0.0, min(100.0, number))))


def parse_willreset(data: dict) -> ResetOutlook:
    promise = str(data.get("promise_quote") or "") if data.get("promise_active") else ""
    return ResetOutlook("willreset.com", "https://willreset.com/", _percent(data.get("prob_24")), _percent(data.get("prob_48")),
                        _when(data.get("last_reset_at")), str(data.get("last_reset_id") or "") or None,
                        str(data.get("last_reset_text") or "")[:80], promise[:80])


def parse_codex_reset(data: dict) -> ResetOutlook:
    probabilities = data.get("probabilities") or {}
    hint = data.get("tease_signal") or {}
    quote = ((hint.get("post") or {}).get("quote") or "") if hint else ""
    last = data.get("last_reset_at")
    return ResetOutlook("codex-reset.com", "https://codex-reset.com/", _percent(probabilities.get("rounded_24h")),
                        _percent(probabilities.get("rounded_48h")), _when(last), str(last) if last else None, "", str(quote)[:80])


def parse_willcodexreset(data: dict) -> ResetOutlook:
    data = data.get("data") or {}
    resets = [event for event in data.get("events") or [] if event.get("kind") == "reset"]
    latest = max(resets, key=lambda event: str(event.get("occurredAt") or ""), default=None)
    return ResetOutlook("willcodexreset.com", "https://willcodexreset.com/", _percent(data.get("probability24h")),
                        _percent(data.get("probability48h")), _when(latest.get("occurredAt")) if latest else None,
                        str(latest.get("id")) if latest else None, str(latest.get("title") or "")[:80] if latest else "")


# Small documented payloads first; willcodexreset.com's radar is ~1 MB.
SOURCES: list[tuple[str, Callable[[dict], ResetOutlook]]] = [
    ("https://willreset.com/api/notify", parse_willreset),
    ("https://codex-reset.com/api/forecast", parse_codex_reset),
    ("https://willcodexreset.com/api/reset-radar", parse_willcodexreset),
]


def _get_json(url: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(4_000_000).decode("utf-8"))


def fetch_outlook(get: Callable[[str], dict] = _get_json, now: float | None = None) -> tuple[ResetOutlook | None, list[str]]:
    """First source that answers with a usable probability; plus errors seen."""
    errors = []
    for url, parse in SOURCES:
        try:
            outlook = parse(get(url))
        except Exception as error:   # network, JSON or layout change: try the next source
            errors.append(f"{url}: {type(error).__name__}: {error}")
            continue
        if outlook.prob_24 is None:
            errors.append(f"{url}: no probability")
            continue
        outlook.fetched_at = now or time.time()
        return outlook, errors
    return None, errors


def load(path: Path) -> ResetOutlook | None:
    try:
        return ResetOutlook(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save(path: Path, outlook: ResetOutlook) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(outlook), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def ago_text(moment: float | None, now: float) -> str:
    if not moment:
        return "未知"
    hours = max(0.0, now - moment) / 3600
    if hours < 1:
        return "不到 1 小时前"
    if hours < 48:
        return f"{hours:.0f} 小时前"
    return f"{hours / 24:.1f} 天前"


def panel_line(outlook: ResetOutlook | None, now: float) -> str:
    """Always show the current community probabilities above the panel title."""
    if outlook is None:
        return "GPT 社区重置预测 · 正在读取"
    if outlook.level(now) == "unknown":
        return "GPT 社区重置预测 · 数据待更新"
    second = "—" if outlook.prob_48 is None else f"{outlook.prob_48}%"
    return f"GPT 社区重置预测 · 24h {outlook.prob_24}% · 48h {second}"


def card_lines(outlook: ResetOutlook | None, remaining: float | None, now: float) -> tuple[str, list[str]]:
    """(banner, notes) for the Codex card.  The banner is only set when a
    global reset looks likely within 24 hours."""
    if outlook is None:
        return "", []
    level = outlook.level(now)
    if level == "unknown":
        return "", [f"社区重置预测暂不可用（{outlook.source}，{ago_text(outlook.fetched_at, now)}）"]
    banner = ""
    if level == "high":
        banner = f"Codex 可能即将全局重置：24 小时内概率 {outlook.prob_24}%"
        if remaining is not None and remaining < 30:
            banner += f"；当前仅剩 {remaining:.0f}%，可考虑等重置后再大量使用"
    notes = [f"社区重置预测（{outlook.source}）：24 小时 {outlook.prob_24}% · 48 小时 {outlook.prob_48 if outlook.prob_48 is not None else '—'}%",
             f"上次全局重置：{ago_text(outlook.last_reset_at, now)}" + (f"（{outlook.last_reset_text}）" if outlook.last_reset_text else "")]
    if outlook.promise:
        notes.append(f"@thsottiaux 最新信号：“{outlook.promise}”")
    notes.append("社区估计，非 OpenAI 官方时间表")
    return banner, notes

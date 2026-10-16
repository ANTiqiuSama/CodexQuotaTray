"""Event-driven quota refresh instead of a fixed 5-minute poll.

Triggers, per provider:
* startup / manual refresh / wake from sleep  -> now
* a generation just finished                   -> after a short settle delay
* still generating                             -> at most every ACTIVE seconds
* the reset time of any window passes          -> a few seconds after it
* read failed                                  -> 30 s, 1, 2, 5 min backoff
* otherwise idle                               -> IDLE seconds (long)
Grok's and Claude's readers launch a whole CLI session, so their intervals are wider.
"""
from __future__ import annotations

import math
import threading
from typing import Any, Iterable

from providers import SPECS

# Per-provider intervals come from the registry (providers.py).
IDLE = {spec.key: spec.idle_seconds for spec in SPECS}
ACTIVE = {spec.key: spec.active_seconds for spec in SPECS}
SETTLE = {spec.key: spec.settle_seconds for spec in SPECS}
RETRY = (30, 60, 120, 300)
RESET_DELAY = 5
MAX_SLEEP = 30.0


class RefreshScheduler:
    def __init__(self, providers: Iterable[str], now: float):
        self.lock = threading.Lock()
        self.providers = list(providers)
        self.due = {p: now for p in self.providers}
        self.reason = {p: "启动" for p in self.providers}
        self.last_done = {p: 0.0 for p in self.providers}
        self.failures = {p: 0 for p in self.providers}
        self.active = {p: False for p in self.providers}
        self.running: set[str] = set()

    def _forward(self, provider: str, when: float, reason: str) -> bool:
        if when < self.due[provider]:
            self.due[provider] = when
            self.reason[provider] = reason
            return True
        return False

    def request(self, providers: Iterable[str] | None, now: float, reason: str = "手动刷新") -> None:
        with self.lock:
            for provider in providers or self.providers:
                self._forward(provider, now, reason)

    def clock_jump(self, now: float) -> None:
        self.request(None, now, "唤醒后刷新")

    def take_due(self, now: float) -> list[str]:
        with self.lock:
            ready = [p for p in self.providers if p not in self.running and self.due[p] <= now]
            for provider in ready:
                self.running.add(provider)
                self.due[provider] = math.inf
            return ready

    def completed(self, provider: str, snapshot: Any, now: float) -> None:
        with self.lock:
            self.running.discard(provider)
            self.last_done[provider] = now
            if getattr(snapshot, "error", None):
                self.failures[provider] += 1
                delay = RETRY[min(self.failures[provider], len(RETRY)) - 1]
                self.due[provider] = now + min(delay, IDLE[provider])
                self.reason[provider] = "重试"
            else:
                self.failures[provider] = 0
                interval = ACTIVE[provider] if self.active[provider] else IDLE[provider]
                self.due[provider] = now + interval
                self.reason[provider] = "使用中" if self.active[provider] else "空闲"
            for reset in (getattr(snapshot, "resets_at", None), getattr(snapshot, "secondary_resets_at", None)):
                if reset and reset > now:
                    self._forward(provider, reset + RESET_DELAY, "额度重置")

    def activity(self, provider: str, active: bool, finished: bool, now: float) -> bool:
        """Feed live generation state; returns True when a refresh moved earlier."""
        with self.lock:
            was = self.active[provider]
            self.active[provider] = active
            moved = False
            if active and not was:
                moved |= self._forward(provider, max(now + 60, self.last_done[provider] + ACTIVE[provider]), "使用中")
            if finished or (was and not active):
                moved |= self._forward(provider, now + SETTLE[provider], "生成结束")
            return moved

    def next_wake(self, now: float) -> float:
        with self.lock:
            pending = [self.due[p] for p in self.providers if p not in self.running]
        if not pending:
            return MAX_SLEEP
        return max(0.2, min(MAX_SLEEP, min(pending) - now))

    def describe(self, provider: str, now: float) -> str:
        with self.lock:
            if provider in self.running:
                return "正在刷新"
            due, reason = self.due[provider], self.reason[provider]
        if due == math.inf:
            return "等待刷新"
        left = due - now
        if left <= 1:
            return f"即将刷新（{reason}）"
        minutes = left / 60
        when = f"{int(left)} 秒" if left < 60 else (f"{minutes:.0f} 分钟" if minutes < 90 else f"{minutes / 60:.1f} 小时")
        return f"下次刷新约 {when}后（{reason}）"

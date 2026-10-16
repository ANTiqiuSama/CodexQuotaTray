from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cli_usage import parse_grok_usage, run_agy_usage, run_temporary_usage


CACHE_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray" / "state.json"


@dataclass
class QuotaSnapshot:
    provider: str
    label: str
    window_minutes: int | None
    used_percent: float | None
    remaining_percent: float | None
    resets_at: int | None
    updated_at: float | None
    stale: bool
    error: str | None
    # Optional second window (Codex reports a weekly limit next to the 5 h one).
    secondary_window_minutes: int | None = None
    secondary_used_percent: float | None = None
    secondary_remaining_percent: float | None = None
    secondary_resets_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def display_remaining(self) -> float | None:
        """The binding window: whichever has less quota left."""
        values = [v for v in (self.remaining_percent, self.secondary_remaining_percent) if v is not None]
        return min(values) if values else None


LABELS = {"codex": ("Codex", None), "grok": ("Grok", 10080), "gemini": ("Gemini（5 小时）", 300)}
SECONDARY_FIELDS = ("secondary_window_minutes", "secondary_used_percent", "secondary_remaining_percent", "secondary_resets_at")


def placeholder(provider: str, error: str = "正在读取…") -> QuotaSnapshot:
    label, window = LABELS[provider]
    return _unknown(provider, label, window, error)


def _unknown(provider: str, label: str, window_minutes: int | None, error: str) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider=provider,
        label=label,
        window_minutes=window_minutes,
        used_percent=None,
        remaining_percent=None,
        resets_at=None,
        updated_at=time.time(),
        stale=False,
        error=error,
    )


def _codex_executable() -> str | None:
    configured = os.environ.get("CODEX_QUOTA_CODEX_EXE")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("codex")
    if found:
        return found
    root = Path.home() / "AppData/Local/OpenAI/Codex/bin"
    candidates = sorted(root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0]) if candidates else None


class _JsonRpcProcess:
    def __init__(self, command: list[str]):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._lock = threading.Lock()
        self._messages: dict[int | str, dict[str, Any]] = {}
        self._events: dict[int | str, threading.Event] = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = message.get("id")
            if key is None:
                continue
            with self._lock:
                self._messages[key] = message
                event = self._events.get(key)
            if event:
                event.set()

    def request(self, method: str, params: Any, timeout: float = 8.0) -> dict[str, Any]:
        request_id = int(time.time_ns())
        event = threading.Event()
        with self._lock:
            self._events[request_id] = event
        assert self.process.stdin is not None
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        with self._lock:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        if not event.wait(timeout):
            raise TimeoutError(f"{method} timed out")
        with self._lock:
            message = self._messages.pop(request_id)
            self._events.pop(request_id, None)
        if "error" in message:
            raise RuntimeError(str(message["error"].get("message", message["error"])))
        return message.get("result", {})

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


def _percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    return max(0.0, min(100.0, float(value)))


def parse_codex_rate_limits(bucket: dict[str, Any]) -> QuotaSnapshot:
    window = bucket.get("primary") or {}
    used_value = _percent(window.get("usedPercent"))
    if used_value is None:
        raise ValueError("Codex 额度响应缺少 usedPercent")
    secondary = bucket.get("secondary") or {}
    secondary_used = _percent(secondary.get("usedPercent")) if isinstance(secondary, dict) else None
    return QuotaSnapshot(
        provider="codex",
        label="Codex",
        window_minutes=window.get("windowDurationMins"),
        used_percent=used_value,
        remaining_percent=100.0 - used_value,
        resets_at=window.get("resetsAt"),
        updated_at=time.time(),
        stale=False,
        error=None,
        secondary_window_minutes=secondary.get("windowDurationMins") if secondary_used is not None else None,
        secondary_used_percent=secondary_used,
        secondary_remaining_percent=None if secondary_used is None else 100.0 - secondary_used,
        secondary_resets_at=secondary.get("resetsAt") if secondary_used is not None else None,
    )


def get_codex_quota() -> QuotaSnapshot:
    executable = _codex_executable()
    if not executable:
        return _unknown("codex", "Codex", None, "未找到 codex.exe")
    rpc: _JsonRpcProcess | None = None
    try:
        rpc = _JsonRpcProcess([executable, "app-server", "--stdio"])
        rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "CodexQuotaTray", "title": "Codex Quota Tray", "version": "1.0.0"},
                "capabilities": {"optOutNotificationMethods": ["account/rateLimits/updated"]},
            },
        )
        result = rpc.request("account/rateLimits/read", None)
        buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits", {})}
        bucket = buckets.get("codex") or result.get("rateLimits") or {}
        return parse_codex_rate_limits(bucket)
    except Exception as exc:
        return _unknown("codex", "Codex", None, f"读取失败：{type(exc).__name__}: {exc}")
    finally:
        if rpc:
            rpc.close()


def get_grok_quota() -> QuotaSnapshot:
    executable = os.environ.get("CODEX_QUOTA_GROK_EXE") or str(Path.home() / ".grok/bin/grok.exe")
    try:
        text = run_temporary_usage(executable, ["--minimal", "--no-alt-screen", "--no-memory", "--no-subagents", "--disable-web-search"])
        parsed = parse_grok_usage(text)
        return QuotaSnapshot("grok", "Grok", parsed["window_minutes"], parsed["used_percent"], parsed["remaining_percent"], parsed["resets_at"], time.time(), False, None)
    except Exception as exc:
        return _unknown("grok", "Grok", 10080, f"读取 /usage 失败：{type(exc).__name__}: {exc}")


def get_gemini_quota() -> QuotaSnapshot:
    executable = os.environ.get("CODEX_QUOTA_GEMINI_EXE") or shutil.which("agy") or str(Path.home() / "AppData/Local/agy/bin/agy.exe")
    try:
        parsed = run_agy_usage(executable)
        return QuotaSnapshot("gemini", "Gemini（5 小时）", parsed["window_minutes"], parsed["used_percent"], parsed["remaining_percent"], parsed["resets_at"], time.time(), False, None)
    except Exception as exc:
        return _unknown("gemini", "Gemini（5 小时）", 300, f"读取 /usage 失败：{type(exc).__name__}: {exc}")


def _reader(provider: str):
    # Resolved at call time so tests and callers can patch the module attribute.
    return {"codex": get_codex_quota, "grok": get_grok_quota, "gemini": get_gemini_quota}[provider]


def keep_last_good(snapshot: QuotaSnapshot, old: QuotaSnapshot | None) -> QuotaSnapshot:
    """A failed read keeps the last valid numbers and marks them stale."""
    if snapshot.error and old and old.used_percent is not None:
        snapshot.used_percent = old.used_percent
        snapshot.remaining_percent = old.remaining_percent
        snapshot.resets_at = old.resets_at
        snapshot.window_minutes = old.window_minutes or snapshot.window_minutes
        snapshot.updated_at = old.updated_at
        for name in SECONDARY_FIELDS:
            setattr(snapshot, name, getattr(old, name))
        snapshot.stale = True
    return snapshot


def collect(providers, previous: dict[str, QuotaSnapshot] | None = None) -> dict[str, QuotaSnapshot]:
    """Read the given providers in parallel; total time is the slowest one."""
    previous = previous or {}
    providers = list(providers)
    if not providers:
        return {}
    with ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="quota") as pool:
        futures = {provider: pool.submit(_reader(provider)) for provider in providers}
    results = {}
    for provider, future in futures.items():
        try:
            snapshot = future.result()
        except Exception as exc:  # Readers already catch; this is a last resort.
            snapshot = placeholder(provider, f"读取失败：{type(exc).__name__}: {exc}")
        results[provider] = keep_last_good(snapshot, previous.get(provider))
    return results


def collect_all(previous: dict[str, QuotaSnapshot] | None = None) -> dict[str, QuotaSnapshot]:
    return collect(("codex", "grok", "gemini"), previous)


def dump_snapshot(snapshots: dict[str, QuotaSnapshot]) -> str:
    payload = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "providers": [snapshot.to_dict() for snapshot in snapshots.values()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_cached_snapshots() -> dict[str, QuotaSnapshot]:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        result: dict[str, QuotaSnapshot] = {}
        for item in payload.get("providers", []):
            snapshot = QuotaSnapshot(
                provider=str(item["provider"]),
                label=str(item["label"]),
                window_minutes=item.get("window_minutes"),
                used_percent=item.get("used_percent"),
                remaining_percent=item.get("remaining_percent"),
                resets_at=item.get("resets_at"),
                updated_at=item.get("updated_at"),
                stale=True,
                error=None,
                **{name: item.get(name) for name in SECONDARY_FIELDS},
            )
            result[snapshot.provider] = snapshot
        return result
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {}


def save_cached_snapshots(snapshots: dict[str, QuotaSnapshot]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(dump_snapshot(snapshots), encoding="utf-8")
        temporary.replace(CACHE_PATH)
    except OSError:
        pass

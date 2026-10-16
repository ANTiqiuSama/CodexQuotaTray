from __future__ import annotations

import json
import ctypes
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

try:
    from winpty import PTY
except ImportError:
    PTY = None


ANSI_RE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
GROK_WEEKLY_RE = re.compile(r"weekly\s+limit\s*[:：-]?\s*(\d+(?:\.\d+)?)\s*%", re.I)
GROK_READY_RE = re.compile(r"minimal\s*·\s*/help|\d+(?:\.\d+)?[km]?\s*/\s*\d+(?:\.\d+)?[km]?\s*\(\d+%\)", re.I)
RESET_PATTERNS = (
    re.compile(r"next\s+reset\s*[:：]\s*(.+)", re.I),
    re.compile(r"resets?\s+(?:at|in)?\s*[:：]?\s*(.+)", re.I),
)


def clean_terminal_text(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "\n")


def parse_percent(text: str, labels: tuple[str, ...]) -> float | None:
    normalized = clean_terminal_text(text).lower()
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：-]?\s*([^\n]*)", normalized)
        if match:
            percent = PERCENT_RE.search(match.group(1))
            if percent:
                return max(0.0, min(100.0, float(percent.group(1))))
    return None


def parse_reset(text: str) -> int | None:
    normalized = clean_terminal_text(text)
    for pattern in RESET_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        value = match.group(1).strip().split("\n", 1)[0].strip()
        for fmt in ("%B %d, %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(value, fmt)
                if parsed.year == 1900:
                    parsed = parsed.replace(year=datetime.now().year)
                    if parsed.timestamp() < time.time():
                        parsed = parsed.replace(year=parsed.year + 1)
                return int(parsed.timestamp())
            except ValueError:
                continue
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return None


def _create_terminal():
    own_console = False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handles = [kernel32.GetStdHandle(value) for value in (-10, -11, -12)]
        if any(handle in (None, ctypes.c_void_p(-1).value) for handle in handles):
            if not kernel32.AllocConsole():
                raise RuntimeError("无法初始化隐藏的 Grok 额度读取终端")
            own_console = True
            user32 = ctypes.windll.user32
            user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.ShowWindow(kernel32.GetConsoleWindow(), 0)
    try:
        return PTY(160, 40, timeout=5000), own_console
    except BaseException:
        if own_console:
            ctypes.windll.kernel32.FreeConsole()
        raise


def run_temporary_usage(executable: str, args: list[str], timeout: float = 25.0) -> str:
    """Open a temporary PTY session, issue `/usage`, and return its visible text."""
    if PTY is None:
        raise RuntimeError("缺少 pywinpty，无法读取 CLI /usage 临时窗口")

    # The native PTY constructor needs standard console handles even when
    # called by a windowed EXE. Create and release a hidden console as needed.
    terminal, own_console = _create_terminal()
    captured: list[str] = []
    started_at = time.monotonic()
    deadline = started_at + timeout
    last_request_at = None
    first_quota_at = None
    try:
        if not terminal.spawn(executable, subprocess.list2cmdline(args)):
            raise RuntimeError("无法启动 Grok 额度读取会话")
        while time.monotonic() < deadline:
            captured.append(terminal.read(blocking=False))
            current = clean_terminal_text("".join(captured))
            now = time.monotonic()
            if GROK_WEEKLY_RE.search(current):
                if first_quota_at is None:
                    first_quota_at = now
                if parse_reset(current) is not None or now - first_quota_at >= 1.2:
                    return current
            elif (GROK_READY_RE.search(current) or now - started_at >= 4.0) and (
                last_request_at is None or now - last_request_at >= 2.0
            ):
                # /usage before session initialization reports "unavailable".
                # Wait for the prompt, then retry only this local slash command.
                terminal.write("/usage")
                time.sleep(0.1)
                terminal.write("\r")
                last_request_at = time.monotonic()
            if not terminal.isalive():
                raise RuntimeError("Grok 额度读取会话提前退出，请检查 CLI 登录状态")
            time.sleep(0.1)
        raise TimeoutError(f"Grok 会话在 {timeout:g} 秒内未返回 Weekly limit")
    finally:
        try:
            terminal.write("\x03")
            terminal.write("\x03")
        except Exception:
            pass
        try:
            if terminal.isalive():
                os.kill(terminal.pid, signal.SIGTERM)
        except Exception:
            pass
        if own_console:
            ctypes.windll.kernel32.FreeConsole()


def parse_cli_percent(text: str, labels: tuple[str, ...], window_minutes: int) -> dict[str, Any]:
    used = parse_percent(text, labels)
    remaining = None if used is None else max(0.0, min(100.0, 100.0 - used))
    return {
        "used_percent": used,
        "remaining_percent": remaining,
        "resets_at": parse_reset(text),
        "window_minutes": window_minutes,
    }


def parse_grok_usage(text: str) -> dict[str, Any]:
    clean = clean_terminal_text(text)
    match = GROK_WEEKLY_RE.search(clean)
    if not match:
        raise ValueError("Grok /usage 未找到 Weekly limit")
    used = max(0.0, min(100.0, float(match.group(1))))
    return {
        "used_percent": used,
        "remaining_percent": max(0.0, min(100.0, 100.0 - used)),
        "resets_at": parse_reset(clean),
        "window_minutes": 10080,
    }


def parse_agy_usage_json(payload: dict[str, Any]) -> dict[str, Any]:
    groups = payload.get("command", {}).get("data", {}).get("groups", [])
    for group in groups:
        if group.get("name") != "Gemini Models":
            continue
        for bucket in group.get("buckets", []):
            if bucket.get("id") != "gemini-5h":
                continue
            remaining = float(bucket["remaining_fraction"]) * 100.0
            remaining = max(0.0, min(100.0, remaining))
            reset_text_value = bucket.get("reset_time")
            reset_at = None
            if reset_text_value:
                reset_at = int(datetime.fromisoformat(reset_text_value.replace("Z", "+00:00")).timestamp())
            return {
                "used_percent": max(0.0, min(100.0, 100.0 - remaining)),
                "remaining_percent": remaining,
                "resets_at": reset_at,
                "window_minutes": 300,
            }
    raise ValueError("Agy /usage JSON 未找到 gemini-5h")


def run_agy_usage(executable: str, timeout: float = 10.0) -> dict[str, Any]:
    env = os.environ.copy()
    env["AGY_DISABLE_AUTO_UPDATE"] = "1"
    process = subprocess.run(
        [executable, "--print", "/usage", "--output-format", "json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=env,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"agy 退出码 {process.returncode}")
    payload = json.loads(process.stdout)
    if payload.get("status") != "SUCCESS":
        raise RuntimeError(str(payload.get("response") or "agy /usage 未成功"))
    return parse_agy_usage_json(payload)

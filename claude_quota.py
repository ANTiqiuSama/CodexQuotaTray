"""Keep Claude's native PTY outside the tray's refresh thread/process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import psutil

from cli_usage import CLAUDE_ARGS, CLAUDE_PROBE, parse_claude_usage, run_temporary_usage


def no_quota_reason(executable: str) -> str | None:
    """Use the noninteractive status first; older CLIs can still use /usage."""
    try:
        result = subprocess.run([executable, "auth", "status"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        status = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not isinstance(status, dict):
        return None
    if status.get("loggedIn") is False:
        return "Claude Code 未登录，未找到订阅额度"
    if str(status.get("authMethod", "")).lower() in {"api_key", "api-key"}:
        return "API Key 计费，未找到订阅额度"
    if str(status.get("subscriptionType", "")).lower() in {"free", "none"}:
        return "未找到订阅额度（当前 Claude 账号无订阅）"
    return None


def stop_worker(process: subprocess.Popen) -> None:
    # Only this disposable worker's descendants; never an existing Claude session.
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    process.kill()
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass
    process.wait(timeout=5)


def read_usage(executable: str) -> dict:
    reason = no_quota_reason(executable)
    if reason:
        raise ValueError(reason)
    folder = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray/work"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="claude-quota-", dir=folder) as temporary:
        destination = Path(temporary) / "result.json"
        command = ([sys.executable, "--claude-quota-worker"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-X", "utf8", str(Path(__file__).resolve())])
        command += [executable, str(destination)]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            stop_worker(process)
            raise TimeoutError("Claude Code 额度读取在 45 秒内未完成（含隐藏终端初始化）") from None
        result = json.loads(destination.read_text(encoding="utf-8"))
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["quota"]


def worker(executable: str, destination: str) -> int:
    try:
        text = run_temporary_usage(executable, CLAUDE_ARGS, timeout=30, probe=CLAUDE_PROBE)
        result, code = {"quota": parse_claude_usage(text)}, 0
    except Exception as error:
        result, code = {"error": f"{type(error).__name__}: {error}"}, 1
    # Only parsed quota numbers or an error; never persist the terminal screen.
    Path(destination).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(worker(*sys.argv[1:]))

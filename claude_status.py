"""Claude Code's documented status-line rate limits, without OAuth access."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import providers


def data_dir() -> Path:
    return providers._local_appdata() / "CodexQuotaTray"


def bridge_installed() -> bool:
    try:
        settings = json.loads((providers.claude_home() / "settings.json").read_text(encoding="utf-8-sig"))
        command = (settings.get("statusLine") or {}).get("command", "")
        return "codexquotatray-statusline.ps1" in command and (data_dir() / "codexquotatray-statusline.ps1").is_file()
    except (OSError, ValueError, AttributeError):
        return False


def install() -> None:
    """Explicit installation. Preserve unrelated settings and custom status lines."""
    if os.name != "nt":
        raise OSError("当前实时额度接入脚本适用于 Windows")
    path = providers.claude_home() / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    previous = settings.get("statusLine") or {}
    command = previous.get("command", "")
    if previous and "codexquotatray-statusline.ps1" not in command:
        raise ValueError("已有自定义 Claude statusLine，保留现有配置；请手动接入 claude_status.ps1")
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    receiver = folder / "codexquotatray-statusline.ps1"
    receiver.write_bytes((Path(__file__).resolve().parent / "claude_status.ps1").read_bytes())
    settings["statusLine"] = {"type": "command", "command":
        f'powershell.exe -NoLogo -NoProfile -NonInteractive -File "{receiver.as_posix()}"'}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".quota-tray.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def latest(now: float | None = None, folder: Path | None = None) -> dict | None:
    """Newest numeric-only native account report, at most a day old."""
    now = now or time.time()
    candidates = []
    try:
        paths = sorted((folder or data_dir() / "claude-status").glob("*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)[:128]
    except OSError:
        return None
    for path in paths:
        try:
            if path.stat().st_size > 16384:
                continue
            row = json.loads(path.read_text(encoding="utf-8-sig"))
            at = float(row["updated_at"])
            if not math.isfinite(at) or not now - 86400 <= at <= now + 30:
                continue
            windows = {}
            for name in ("five_hour", "seven_day"):
                value = row.get("rate_limits", {}).get(name)
                if not isinstance(value, dict) or isinstance(value.get("used_percentage"), bool):
                    continue
                used = float(value["used_percentage"])
                reset = value.get("resets_at")
                reset = int(reset) if reset is not None else None
                if not math.isfinite(used) or not 0 <= used <= 100 or (reset is not None and reset <= now):
                    continue
                windows[name] = {"used_percentage": used, "resets_at": reset}
            if windows:
                candidates.append({"updated_at": at, "rate_limits": windows})
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    return max(candidates, key=lambda item: item["updated_at"], default=None)

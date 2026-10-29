"""Claude Code's documented status-line rate limits, without OAuth access."""
from __future__ import annotations

import json
import math
import os
import time
import sys
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


def desktop_roots() -> list[Path]:
    override = os.environ.get("CODEX_QUOTA_CLAUDE_DESKTOP_DIR")
    if override:
        return [Path(override)]
    roots = []
    if sys.platform == "darwin":
        roots.append(Path.home() / "Library/Application Support/Claude")
    else:
        if os.environ.get("APPDATA"):
            roots.append(Path(os.environ["APPDATA"]) / "Claude")
        if os.environ.get("LOCALAPPDATA"):
            packages = Path(os.environ["LOCALAPPDATA"]) / "Packages"
            roots.extend(path / "LocalCache/Roaming/Claude" for path in packages.glob("Claude_*"))
    return roots


def recent_desktop_usage(now: float, seconds: float = 1800, roots: list[Path] | None = None) -> float:
    """Evidence of recent Desktop usage, never process presence or a login alone.

    Read only numeric quota history. Compare within the same opaque account
    scope; do not return, log or retain that scope's identity.
    """
    latest_use = 0.0
    for root in desktop_roots() if roots is None else roots:
        path = root / "plan-usage-history.json"
        try:
            if path.stat().st_size > 2_000_000:
                continue
            rows = json.loads(path.read_text(encoding="utf-8"))["samples"]
            previous = {}
            for row in rows[-2000:]:
                at = float(row["t"]) / 1000
                scope = row.get("org")
                values = {key: float(value) for key, value in (row.get("u") or {}).items()
                          if key in ("fh", "sd") and not isinstance(value, bool)}
                if not scope or not values or not math.isfinite(at) or any(not math.isfinite(v) or not 0 <= v <= 100 for v in values.values()):
                    continue
                old = previous.get(scope)
                if old and old[0] < at and now - seconds <= at <= now + 30 and any(
                        key in old[1] and value > old[1][key] for key, value in values.items()):
                    latest_use = max(latest_use, at)
                previous[scope] = (at, values)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    return latest_use

"""Single registry of quota providers.

Adding a provider means one entry here plus a quota reader in
`quota_sources.py` (and, optionally, a local token reader in
`tps_sources.py`).  Everything else - tray icons, menus, refresh intervals,
launchers and accent colours - is derived from this table.

Which providers are shown:
* `CODEX_QUOTA_PROVIDERS=codex,grok,gemini,claude` picks an explicit list;
* Claude is opt-in through the tray menu or an explicit environment list;
* otherwise every provider with `default=True` is shown, plus detected MiMo.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))


def claude_choice() -> bool | None:
    """A saved menu choice overrides auto-detection and the environment list."""
    value = _read(_local_appdata() / "CodexQuotaTray/claude-enabled.txt").strip()
    return {"enabled": True, "disabled": False}.get(value)


def set_claude_enabled(enabled: bool) -> None:
    path = _local_appdata() / "CodexQuotaTray/claude-enabled.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("enabled" if enabled else "disabled", encoding="utf-8")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def find_claude() -> Path | None:
    """Claude Code: native installer, npm shim, or an explicit override."""
    override = os.environ.get("CODEX_QUOTA_CLAUDE_EXE")
    if override:
        return Path(override)
    candidates = [
        Path.home() / ".local/bin/claude.exe",
        Path.home() / ".claude/local/claude.exe",
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "npm/claude.cmd",
    ]
    found = shutil.which("claude")
    if found:
        candidates.insert(0, Path(found))
    return next((path for path in candidates if path.is_file()), None)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def claude_routes_to_mimo() -> bool:
    """Claude Code pointed at MiMo's Anthropic-compatible endpoint has no
    Claude subscription to read; its sessions are counted as MiMo instead."""
    if "xiaomimimo" in os.environ.get("ANTHROPIC_BASE_URL", ""):
        return True
    return any("xiaomimimo" in _read(claude_home() / name) for name in ("settings.json", "settings.local.json"))


def _claude_installed() -> bool:
    if claude_routes_to_mimo():
        return False
    return find_claude() is not None or (claude_home() / "projects").is_dir()


def _mimo_used() -> bool:
    data = _local_appdata() / "CodexQuotaTray"
    if (data / "mimo-session.bin").is_file() or (data / "mimo.json").is_file():
        return True
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if "xiaomimimo" in _read(codex_home / "config.toml") or claude_routes_to_mimo():
        return True
    return any((root / "mimocode").is_dir() for root in (Path.home() / ".local/share", _local_appdata()))


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    letter: str                      # glyph on the idle tray icon
    label: str                       # display name (menus, cards)
    window_minutes: int | None       # primary quota window, None = reported by the source
    accent: tuple[int, int, int, int]
    idle_seconds: int                # refresh interval while unused
    active_seconds: int              # refresh interval while generating
    settle_seconds: int              # refresh this long after a generation ends
    launch: tuple[str, Callable[[], Path | str | None]]  # (kind, target resolver)
    default: bool = True
    detect: Callable[[], bool] | None = None
    source_note: str = ""
    # Used up: check rarely, then often shortly before the reset.
    exhausted_seconds: int | None = None
    near_reset_seconds: int = 30 * 60   # start checking more often this long before the reset
    near_reset_poll: int = 3 * 60       # ... at this interval (every minute in the last 5 min)
    # Repeated failures, or an account without this quota: check rarely.
    give_up_seconds: int | None = None

    def available(self) -> bool:
        return self.default or bool(self.detect and self.detect())


SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "codex", "C", "Codex", None, (126, 201, 166, 255), 5 * 60, 60, 5,
        ("aumid", lambda: "OpenAI.Codex_2p2nqsd0c76g0!App"),
        source_note="Codex App Server account/rateLimits/read（5 小时 + 每周）",
    ),
    ProviderSpec(
        "grok", "G", "Grok", 10080, (203, 208, 216, 255), 30 * 60, 10 * 60, 20,
        ("cli", lambda: Path.home() / ".grok/bin/grok.exe"),
        source_note="临时 Grok CLI 会话 /usage（每周）",
        exhausted_seconds=3 * 3600,
    ),
    ProviderSpec(
        "gemini", "M", "Gemini（5 小时）", 300, (164, 176, 245, 255), 5 * 60, 2 * 60, 8,
        ("gui", lambda: _local_appdata() / "Programs/antigravity/Antigravity.exe"),
        source_note="agy --print /usage（gemini-5h 桶）",
    ),
    ProviderSpec(
        # Claude Code's /usage reports a 5-hour session and a weekly window.
        # Its reader opens a whole CLI session, so it refreshes like Grok.
        "claude", "A", "Claude", 300, (217, 119, 87, 255), 30 * 60, 10 * 60, 20,
        ("cli", find_claude),
        default=False, detect=lambda: claude_choice() is True,
        source_note="临时 Claude Code 会话 /usage（当前会话 5 小时 + 每周）",
        give_up_seconds=12 * 3600,
    ),
    ProviderSpec(
        # Token Plan: one Credits pool per billing period (a whole year for
        # annual plans).  The console read is a cheap HTTPS call; local token
        # records move the figure in between (see mimo_usage.py).
        "mimo", "X", "Xiaomi MiMo", None, (255, 112, 30, 255), 20 * 60, 5 * 60, 30,
        ("url", lambda: "https://platform.xiaomimimo.com/console/plan-manage"),
        default=False, detect=_mimo_used,
        source_note="MiMo 控制台套餐用量（登录后只读）+ 本机 Codex / Claude Code / MiMo Code 记录折算 Credits",
    ),
)
BY_KEY = {spec.key: spec for spec in SPECS}
BY_LETTER = {spec.letter: spec for spec in SPECS}


def get(key: str) -> ProviderSpec:
    return BY_KEY[key]


def enabled_keys(env: str | None = None) -> list[str]:
    """Ordered provider keys to show in the tray."""
    raw = os.environ.get("CODEX_QUOTA_PROVIDERS") if env is None else env
    wanted = ({part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()}
              if raw else {spec.key for spec in SPECS if spec.available()})
    choice = claude_choice()
    if choice is True:
        wanted.add("claude")
    elif choice is False:
        wanted.discard("claude")
    return [spec.key for spec in SPECS if spec.key in wanted]

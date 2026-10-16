"""Single registry of quota providers.

Adding a provider means one entry here plus a quota reader in
`quota_sources.py` (and, optionally, a local token reader in
`tps_sources.py`).  Everything else - tray icons, menus, refresh intervals,
launchers and accent colours - is derived from this table.

Which providers are shown:
* `CODEX_QUOTA_PROVIDERS=codex,grok,gemini,claude` picks an explicit list;
* otherwise every provider with `default=True` is shown, plus those whose
  `detect()` finds the client installed on this machine (Claude Code).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))


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


def _claude_installed() -> bool:
    return find_claude() is not None or (claude_home() / "projects").is_dir()


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

    def available(self) -> bool:
        return self.default or bool(self.detect and self.detect())


SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "codex", "C", "Codex", None, (126, 201, 166, 255), 15 * 60, 3 * 60, 8,
        ("aumid", lambda: "OpenAI.Codex_2p2nqsd0c76g0!App"),
        source_note="Codex App Server account/rateLimits/read（5 小时 + 每周）",
    ),
    ProviderSpec(
        "grok", "G", "Grok", 10080, (203, 208, 216, 255), 30 * 60, 10 * 60, 20,
        ("cli", lambda: Path.home() / ".grok/bin/grok.exe"),
        source_note="临时 Grok CLI 会话 /usage（每周）",
    ),
    ProviderSpec(
        "gemini", "M", "Gemini（5 小时）", 300, (164, 176, 245, 255), 15 * 60, 3 * 60, 8,
        ("gui", lambda: _local_appdata() / "Programs/antigravity/Antigravity.exe"),
        source_note="agy --print /usage（gemini-5h 桶）",
    ),
    ProviderSpec(
        # Claude Code's /usage reports a 5-hour session and a weekly window.
        # Its reader opens a whole CLI session, so it refreshes like Grok.
        "claude", "A", "Claude", 300, (217, 119, 87, 255), 30 * 60, 10 * 60, 20,
        ("cli", find_claude),
        default=False, detect=_claude_installed,
        source_note="临时 Claude Code 会话 /usage（当前会话 5 小时 + 每周）",
    ),
)
BY_KEY = {spec.key: spec for spec in SPECS}
BY_LETTER = {spec.letter: spec for spec in SPECS}


def get(key: str) -> ProviderSpec:
    return BY_KEY[key]


def enabled_keys(env: str | None = None) -> list[str]:
    """Ordered provider keys to show in the tray."""
    raw = os.environ.get("CODEX_QUOTA_PROVIDERS") if env is None else env
    if raw:
        wanted = [part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()]
        return [spec.key for spec in SPECS if spec.key in wanted]
    return [spec.key for spec in SPECS if spec.available()]

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image

from detail_card import CardModel
from forecast import Forecast, QuotaHistory, duration_text
from hover_details import HoverDetails, HoverIcon
from icon_art import color_for, live_icon, quota_icon  # noqa: F401  (color_for re-exported)
from quota_sources import (
    QuotaSnapshot,
    collect,
    dump_snapshot,
    get_claude_quota,
    get_grok_quota,
    load_cached_snapshots,
    placeholder,
    save_cached_snapshots,
)
import providers as provider_registry
from refresh_scheduler import RefreshScheduler
from tps_sources import PERFORMANCE_PATH, TpsMonitor, performance_signature, save_performance


APP_NAME = "CodexQuotaTray"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP_NAME
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PERFORMANCE_SAVE_SECONDS = 60
# Shown providers (key -> icon letter), in registry order; see providers.py.
PROVIDERS = {key: provider_registry.get(key).letter for key in provider_registry.enabled_keys()}
LAUNCHERS = {spec.key: spec.launch for spec in provider_registry.SPECS}
log = logging.getLogger(APP_NAME)


def make_provider_icon(snapshot: QuotaSnapshot, letter: str) -> Image.Image:
    # The ring follows the binding window (e.g. Codex weekly when it is lower).
    return quota_icon(snapshot.display_remaining, letter, stale=snapshot.stale)


def make_tps_icon(performance: dict, letter: str) -> Image.Image:
    return live_icon(performance.get("display_tps", 0) or 0, letter)


def make_display_icon(snapshot: QuotaSnapshot, letter: str, performance: dict) -> Image.Image:
    return make_tps_icon(performance, letter) if performance.get("active_sessions", 0) and performance.get("display_tps") is not None else make_provider_icon(snapshot, letter)


def window_label(minutes: int | None) -> str:
    if not minutes:
        return ""
    if minutes == 10080:
        return "每周"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def reset_text(timestamp: int | None) -> str:
    if not timestamp:
        return "重置时间未知"
    moment = datetime.fromtimestamp(timestamp).astimezone()
    offset = moment.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}"
    return moment.strftime("%Y-%m-%d %H:%M") + f"（{zone}）"


def reset_countdown(timestamp: int | None) -> str:
    if not timestamp:
        return "未知"
    delta = int(timestamp - time.time())
    if delta <= 0:
        return "已到重置时间"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    return f"{hours} 小时 {minutes} 分"


def quota_line(snapshot: QuotaSnapshot) -> str:
    if snapshot.remaining_percent is None:
        status = "未知/获取失败"
    else:
        status = f"剩余 {snapshot.remaining_percent:.0f}% · 已用 {snapshot.used_percent:.0f}%"
        if snapshot.secondary_remaining_percent is not None:
            status += f" · {window_label(snapshot.secondary_window_minutes) or '第二窗口'}剩余 {snapshot.secondary_remaining_percent:.0f}%"
    stale = " · 旧数据" if snapshot.stale else ""
    return f"{snapshot.label}: {status}{stale}"


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    runner = windowless if windowless.is_file() else interpreter
    return f'"{runner}" -X utf8 "{Path(__file__).resolve()}"'


class QuotaTray:
    # Class-level so test doubles built with __new__ share the same guarantees.
    ui_lock = threading.RLock()
    refresh_lock = threading.Lock()
    history: QuotaHistory | None = None
    scheduler: RefreshScheduler | None = None

    def __init__(self) -> None:
        # Show cached values immediately; reading the CLIs can take ~25 s.
        cached = load_cached_snapshots()
        self.snapshots = {provider: cached.get(provider) or placeholder(provider) for provider in PROVIDERS}
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.history = QuotaHistory()
        self.scheduler = RefreshScheduler(PROVIDERS, time.time())
        self.tps_monitor = TpsMonitor()
        self.performance = self.tps_monitor.snapshot(time.time())
        self.last_samples: dict[str, float | None] = {}
        self.saved_signature: str | None = None
        self.saved_at = 0.0
        self.migrate_startup()
        self.icons = {
            provider: HoverIcon(
                f"{APP_NAME}-{provider}",
                make_display_icon(self.snapshots[provider], letter, self.performance["providers"][provider]),
                self.tooltip(provider),
                menu=self.build_menu(provider),
            )
            for provider, letter in PROVIDERS.items()
        }

    # ----- text and card content -------------------------------------------------

    def forecasts(self, provider: str) -> list[tuple[str, Forecast]]:
        snapshot = self.snapshots[provider]
        if self.history is None:
            return []
        now = time.time()
        result = [(window_label(snapshot.window_minutes), self.history.forecast_for(snapshot, now))]
        if snapshot.secondary_remaining_percent is not None:
            result.append((window_label(snapshot.secondary_window_minutes) or "第二窗口",
                           self.history.forecast_for(snapshot, now, secondary=True)))
        return result

    def headline_forecast(self, provider: str) -> tuple[str, str]:
        """The most urgent forecast, prefixed with its window when there are two."""
        items = self.forecasts(provider)
        if not items:
            return "", "unknown"
        rank = {"danger": 0, "warn": 1, "ok": 2, "idle": 3, "unknown": 4}
        label, chosen = min(items, key=lambda item: (rank[item[1].tone], item[1].exhausts_at or float("inf")))
        prefix = f"{label}额度：" if len(items) > 1 and label else ""
        return prefix + chosen.text, chosen.tone

    def forecast_line(self, provider: str) -> str:
        text, _ = self.headline_forecast(provider)
        return f"预测：{text}" if text else ""

    def hover_text(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        data = self.performance["providers"][provider]
        lines = [data["label"], quota_line(snapshot)]
        if self.forecast_line(provider):
            lines.append(self.forecast_line(provider))
        lines += ["", self.performance_line(provider),
            self.output_rate_line(provider), f"活动会话：{data['active_sessions']}", "",
            self.menu_reset(provider), self.menu_countdown(provider), self.menu_updated(provider)]
        if snapshot.error:
            lines.extend(("", "读取状态：" + snapshot.error))
        return "\n".join(lines)

    def hover_card(self, provider: str) -> CardModel:
        snapshot = self.snapshots[provider]
        data = self.performance["providers"][provider]
        letter = PROVIDERS[provider]
        now = time.time()
        live = data.get("active_sessions", 0) > 0
        tps = data.get("display_tps")
        if live:
            status, tone = ("生成中" + (f" · {tps:.0f} tok/s" if tps is not None else "")), "live"
        elif snapshot.stale:
            status, tone = "旧数据", "stale"
        elif snapshot.error and snapshot.remaining_percent is None:
            status, tone = "读取失败", "error"
        else:
            status, tone = "空闲", "idle"
        primary_label = window_label(snapshot.window_minutes) or "当前"
        bars = [(f"{primary_label}窗口", snapshot.remaining_percent)]
        binding = primary_label if snapshot.window_minutes else ""
        used = snapshot.used_percent
        if snapshot.secondary_remaining_percent is not None:
            secondary_label = window_label(snapshot.secondary_window_minutes) or "第二"
            bars.append((f"{secondary_label}窗口", snapshot.secondary_remaining_percent))
            if snapshot.remaining_percent is None or snapshot.secondary_remaining_percent < snapshot.remaining_percent:
                binding, used = secondary_label, snapshot.secondary_used_percent
        forecast_text, forecast_tone = self.headline_forecast(provider)
        value = data.get("generation_tps")
        metrics = (
            ("生成 TPS", "—" if value is None else f"{value:.1f} tok/s"),
            ("60 秒输出", f"{data.get('output_tps_60', 0):.1f} tok/s"),
            ("活动会话", str(data.get("active_sessions", 0))),
            ("60 秒 tokens", str(data.get("output_tokens_60", 0))),
        )
        footer = [self.reset_summary(primary_label if snapshot.secondary_remaining_percent is not None else "", snapshot.resets_at, now)]
        if snapshot.secondary_remaining_percent is not None:
            footer.append(self.reset_summary(window_label(snapshot.secondary_window_minutes) or "第二窗口", snapshot.secondary_resets_at, now))
        updated = datetime.fromtimestamp(snapshot.updated_at).strftime("%H:%M:%S") if snapshot.updated_at else "未知"
        plan = self.scheduler.describe(provider, now) if self.scheduler else ""
        footer.append(f"更新 {updated}" + (f" · {plan}" if plan else ""))
        notes = []
        if value is not None:
            basis = "、".join(data.get("basis") or [])
            sample = datetime.fromtimestamp(data["last_sample_at"]).strftime("%H:%M:%S") if data.get("last_sample_at") else ""
            notes.append(f"来源：{basis}" + (f"（样本 {sample}）" if sample else ""))
        return CardModel(letter, data["label"], status, tone, snapshot.display_remaining, used, binding,
            tuple(bars), forecast_text, forecast_tone, metrics, tuple(footer), tuple(notes),
            snapshot.error, text=self.hover_text(provider))

    @staticmethod
    def reset_summary(label: str, timestamp: int | None, now: float) -> str:
        name = f"{label}重置" if label else "重置"
        if not timestamp:
            return f"{name}时间未知"
        when = datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")
        tail = f"{duration_text(timestamp - now)}后" if timestamp > now else "已到重置时间，等待刷新"
        return f"{name}  {when} · {tail}"

    def tooltip(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        performance = self.performance["providers"][provider]
        value = performance.get("generation_tps")
        generation = "暂无完整计数" if value is None else f"{value:.1f}"
        return f"{quota_line(snapshot)}\n最近TPS {generation} · 60秒 {performance['output_tps_60']:.1f} tok/s\n{self.menu_reset(provider)}"[:127]

    def build_menu(self, provider: str) -> pystray.Menu:
        return pystray.Menu(
            # Each build_menu call owns its provider closure. pystray counts
            # defaulted positional parameters too and rejects more than two.
            pystray.MenuItem("打开对应程序", lambda _icon, _item: self.open_provider(provider), default=True),
            pystray.MenuItem(lambda _: "立即刷新", self.request_refresh),
            pystray.MenuItem("额度详情", lambda _icon, _item: self.open_details(provider)),
            pystray.MenuItem("本机全部 AI 速率", self.open_all_performance),
            pystray.MenuItem(lambda _: self.menu_quota(provider), None, enabled=False),
            pystray.MenuItem(lambda _: self.forecast_line(provider) or "预测：暂无", None, enabled=False),
            pystray.MenuItem(lambda _: self.menu_reset(provider), None, enabled=False),
            pystray.MenuItem(lambda _: self.menu_countdown(provider), None, enabled=False),
            pystray.MenuItem(lambda _: self.menu_updated(provider), None, enabled=False),
            pystray.MenuItem(lambda _: "打开诊断信息", self.open_diagnostics),
            pystray.MenuItem(
                lambda _: "开机自启",
                self.toggle_startup,
                checked=lambda _: self.startup_enabled(),
            ),
            pystray.MenuItem(lambda _: "退出全部", self.exit_app),
        )

    def menu_quota(self, provider: str) -> str:
        return quota_line(self.snapshots[provider])

    def menu_reset(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        return f"重置时间：{reset_text(snapshot.resets_at)}"

    def menu_countdown(self, provider: str) -> str:
        return f"距离重置：{reset_countdown(self.snapshots[provider].resets_at)}"

    def menu_updated(self, provider: str) -> str:
        updated = self.snapshots[provider].updated_at
        if not updated:
            return "更新时间：未知"
        return "更新时间：" + datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M:%S")

    def open_details(self, provider: str) -> None:
        snapshot = self.snapshots[provider]
        lines = [quota_line(snapshot)]
        for label, item in self.forecasts(provider):
            lines.append(f"预测{f'（{label}）' if label else ''}：{item.text}")
        lines += ["", self.performance_line(provider), self.output_rate_line(provider), "", self.menu_reset(provider), self.menu_countdown(provider)]
        if snapshot.secondary_resets_at:
            lines.append(f"{window_label(snapshot.secondary_window_minutes)}重置：{reset_text(snapshot.secondary_resets_at)}")
        lines += ["", self.menu_updated(provider)]
        if self.scheduler:
            lines.append(self.scheduler.describe(provider, time.time()))
        if snapshot.error:
            lines.extend(("", "读取状态：" + snapshot.error))
        threading.Thread(
            target=ctypes.windll.user32.MessageBoxW,
            args=(None, "\n".join(lines), f"{snapshot.label} · 额度详情", 0),
            daemon=True,
        ).start()

    def performance_line(self, provider: str) -> str:
        data = self.performance["providers"][provider]
        value = data.get("generation_tps")
        if value is None:
            return "TPS：等待完整流式计数" if provider == "codex" else "TPS：客户端未提供原生数值"
        basis = "、".join(data.get("basis") or [])
        sample = datetime.fromtimestamp(data["last_sample_at"]).strftime("%H:%M:%S") if data.get("last_sample_at") else "未知"
        return f"最近生成 TPS：{value:.1f} tok/s（{sample}）\n来源：{basis}"

    def output_rate_line(self, provider: str) -> str:
        data = self.performance["providers"][provider]
        return f"最近60秒输出速率：{data['output_tps_60']:.1f} tok/s（{data['output_tokens_60']} 输出tokens）"

    def open_all_performance(self, _icon=None, _item=None) -> None:
        sections = [f"本机合计 · 最近60秒：{self.performance['total_output_tps_60']:.1f} tok/s", ""]
        for provider, data in self.performance["providers"].items():
            sections.extend((data["label"], self.performance_line(provider), self.output_rate_line(provider), ""))
        sections.append("检测到的软件：")
        sections.extend(f"{item['name']}：{item['status']}" for item in self.performance.get("software", []))
        sections.extend(("", self.performance["measurement_note"]))
        threading.Thread(target=ctypes.windll.user32.MessageBoxW, args=(None, "\n".join(sections), "本机 AI · TPS", 0), daemon=True).start()

    # ----- icon updates ----------------------------------------------------------

    def update_ui(self) -> None:
        save_cached_snapshots(self.snapshots)
        for provider, letter in PROVIDERS.items():
            self.update_icon(provider, letter)
            self.icons[provider].update_menu()

    def update_icon(self, provider: str, letter: str) -> None:
        # The refresh worker and the 1 s TPS loop both land here. pystray
        # replaces the native HICON on assignment, so serialise the updates
        # (this also keeps the cached FreeType faces single-threaded).
        with self.ui_lock:
            icon = self.icons[provider]
            if getattr(icon, "details_visible", False):
                # Keep the Shell's hover target stable. The independent detail
                # window still reads live values from self.performance.
                return
            image = make_display_icon(self.snapshots[provider], letter, self.performance["providers"][provider])
            # Icon assignment always sends NIM_MODIFY, even for identical pixels.
            # Avoid needless Shell redraws; hover detail updates use a separate HWND.
            if icon.icon != image:
                icon.icon = image
            icon.title = self.tooltip(provider)

    # ----- refreshing ------------------------------------------------------------

    def refresh_providers(self, providers) -> None:
        providers = list(providers)
        if not providers:
            return
        with self.refresh_lock:
            results = collect(providers, self.snapshots)
            now = time.time()
            snapshots = dict(self.snapshots)
            snapshots.update(results)
            self.snapshots = snapshots
            if self.history is not None:
                if any([self.history.record(snapshot) for snapshot in results.values()]):
                    self.history.save()
            if self.scheduler is not None:
                for provider, snapshot in results.items():
                    self.scheduler.completed(provider, snapshot, now)
        self.update_ui()

    def refresh(self) -> None:
        """Synchronous refresh of every provider (used by checks and tests)."""
        self.refresh_providers(PROVIDERS)

    def request_refresh(self, _icon=None, _item=None) -> None:
        if self.scheduler is None:
            threading.Thread(target=self.refresh, daemon=True).start()
            return
        self.scheduler.request(None, time.time())
        self.wake.set()

    def refresh_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                due = self.scheduler.take_due(time.time())
                if due:
                    self.refresh_providers(due)
            except Exception:
                log.exception("quota refresh failed")
                for provider in PROVIDERS:  # Never leave a provider stuck "running".
                    if provider in self.scheduler.running:
                        self.scheduler.completed(provider, placeholder(provider, "刷新异常"), time.time())
            self.wake.wait(self.scheduler.next_wake(time.time()))
            self.wake.clear()

    def feed_scheduler(self, performance: dict, now: float) -> None:
        moved = False
        for provider in self.scheduler.providers:
            data = performance["providers"].get(provider)
            if data is None:
                continue
            sample = data.get("last_sample_at")
            previous = self.last_samples.get(provider)
            finished = sample is not None and previous is not None and sample > previous
            if sample is not None:
                self.last_samples[provider] = sample
            elif provider not in self.last_samples:
                self.last_samples[provider] = None
            moved |= self.scheduler.activity(provider, data.get("active_sessions", 0) > 0, finished, now)
        if moved:
            self.wake.set()

    def save_performance_if_changed(self, performance: dict, now: float) -> None:
        signature = performance_signature(performance)
        if signature == self.saved_signature and now - self.saved_at < PERFORMANCE_SAVE_SECONDS:
            return
        save_performance(performance)
        self.saved_signature, self.saved_at = signature, now

    def tps_loop(self) -> None:
        last_wall = time.time()
        failures = 0
        while not self.stop_event.wait(1):
            now = time.time()
            try:
                if now - last_wall > 120:
                    # Sleep/hibernate: quotas may have reset meanwhile.
                    self.scheduler.clock_jump(now)
                    self.wake.set()
                last_wall = now
                self.performance = self.tps_monitor.poll(now)
                self.save_performance_if_changed(self.performance, now)
                self.feed_scheduler(self.performance, now)
                for provider, letter in PROVIDERS.items():
                    if self.icons[provider].visible:
                        self.update_icon(provider, letter)
                failures = 0
            except Exception:
                # Any unexpected error used to end this thread silently and
                # freeze the TPS display; log (rate-limited) and keep going.
                failures += 1
                if failures in (1, 10) or failures % 300 == 0:
                    log.exception("TPS loop error (%d in a row)", failures)

    # ----- actions ---------------------------------------------------------------

    def open_provider(self, provider: str) -> None:
        kind, resolve = LAUNCHERS[provider]
        label = provider_registry.get(provider).label
        try:
            target = resolve()
            if kind == "aumid":
                launched = ctypes.windll.shell32.ShellExecuteW(None, "open", f"shell:AppsFolder\\{target}", None, None, 1)
                if launched <= 32:
                    raise OSError(f"无法启动 {label}（错误 {launched}）")
                return
            if target is None or not Path(target).is_file():
                raise FileNotFoundError(f"未找到 {label} 客户端：{target or '未安装'}")
            path = Path(target)
            flags = subprocess.CREATE_NEW_CONSOLE if kind == "cli" else 0
            # A CLI opened from its install folder would work on that folder;
            # Claude Code and similar start in the user's home instead.
            cwd = path.parent if provider == "grok" else Path.home()
            command = ["cmd.exe", "/k", str(path)] if path.suffix.lower() in (".cmd", ".bat") else [str(path)]
            subprocess.Popen(command, cwd=str(cwd), creationflags=flags, close_fds=True)
        except OSError as error:
            ctypes.windll.user32.MessageBoxW(None, str(error), f"{label} · 无法打开", 0x10)

    def diagnostic_text(self) -> str:
        sections = [dump_snapshot(self.snapshots), "", "数据源边界："]
        sections += [f"- {provider_registry.get(p).label}：{provider_registry.get(p).source_note}" for p in PROVIDERS]
        for item in self.snapshots.values():
            if item.error:
                sections.append(f"- {item.label}：{item.error}")
        if self.scheduler:
            now = time.time()
            sections += ["", "刷新计划："] + [f"- {p}：{self.scheduler.describe(p, now)}" for p in PROVIDERS]
        sections += ["", "额度预测："] + [f"- {p}：{self.forecast_line(p) or '暂无'}" for p in PROVIDERS]
        return "\n".join(sections)

    def open_diagnostics(self, _icon=None, _item=None) -> None:
        path = DATA_DIR / "diagnostics.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.diagnostic_text(), encoding="utf-8")
        os.startfile(path)  # type: ignore[attr-defined]

    # Start with Windows through HKCU\\...\\Run: no console flash, and from
    # source it uses pythonw -X utf8 instead of the .py file association.
    @staticmethod
    def startup_path() -> Path:
        """Legacy Startup-folder script written by earlier versions."""
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{APP_NAME}.cmd"

    @staticmethod
    def _run_value() -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, APP_NAME)[0]
        except OSError:
            return None

    @staticmethod
    def _set_run_value(command: str | None) -> None:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if command is None:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)

    def startup_enabled(self) -> bool:
        return self._run_value() is not None or self.startup_path().is_file()

    def migrate_startup(self) -> None:
        try:
            legacy = self.startup_path()
            if legacy.is_file():
                self._set_run_value(startup_command())
                legacy.unlink()
        except (OSError, KeyError, ImportError):
            log.warning("startup migration skipped", exc_info=True)

    def toggle_startup(self, _icon=None, _item=None) -> None:
        enabled = self.startup_enabled()
        self._set_run_value(None if enabled else startup_command())
        self.startup_path().unlink(missing_ok=True)

    def exit_app(self, _icon=None, _item=None) -> None:
        self.stop_event.set()
        self.wake.set()
        for icon in self.icons.values():
            icon.stop()

    def run(self) -> None:
        threading.Thread(target=self.refresh_loop, name="quota-refresh", daemon=True).start()
        threading.Thread(target=self.tps_loop, name="tps-reader", daemon=True).start()
        hover = HoverDetails(self.icons, self.hover_card, self.stop_event)
        threading.Thread(target=hover.run, name="hover-details", daemon=True).start()
        threads = [
            threading.Thread(target=icon.run, name=f"tray-{provider}", daemon=True)
            for provider, icon in self.icons.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


def acquire_single_instance():
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}-36d9a73f")
    if not handle or kernel32.GetLastError() == 183:
        return None
    return handle


def setup_logging() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(DATA_DIR / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    except OSError:
        pass


def check_claude(destination: Path, raw: bool = False) -> int:
    """Read Claude Code /usage once; `--raw` also saves the cleaned screen
    (emails redacted) so the parser can be checked against a new CLI version."""
    import re
    from cli_usage import CLAUDE_ARGS, CLAUDE_PROBE, run_temporary_usage
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshot = get_claude_quota()
    destination.write_text(dump_snapshot({"claude": snapshot}), encoding="utf-8")
    if raw:
        executable = provider_registry.find_claude()
        try:
            text = run_temporary_usage(str(executable), CLAUDE_ARGS, timeout=30.0, probe=CLAUDE_PROBE) if executable else "未找到 Claude Code"
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
        text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
        destination.with_suffix(".raw.txt").write_text(text, encoding="utf-8")
    return 1 if snapshot.error else 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--check-hover":
        from hover_check import check_hover_runtime
        tray = QuotaTray.__new__(QuotaTray)
        cached = load_cached_snapshots()
        tray.snapshots = {provider: cached.get(provider) or placeholder(provider) for provider in PROVIDERS}
        tray.performance = json.loads(PERFORMANCE_PATH.read_text(encoding="utf-8"))
        tray.history = QuotaHistory()
        return check_hover_runtime(tray.hover_card("codex"), Path(sys.argv[2]).resolve())
    if len(sys.argv) == 3 and sys.argv[1] == "--check-tps":
        monitor = TpsMonitor()
        snapshot = monitor.poll()
        time.sleep(1)
        snapshot = monitor.poll()
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--check-grok":
        snapshot = get_grok_quota()
        destination = Path(sys.argv[2]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(dump_snapshot({"grok": snapshot}), encoding="utf-8")
        return 1 if snapshot.error else 0
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--check-claude":
        return check_claude(Path(sys.argv[2]).resolve(), raw=len(sys.argv) == 4 and sys.argv[3] == "--raw")
    handle = acquire_single_instance()
    if handle is None:
        return 0
    setup_logging()
    QuotaTray().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

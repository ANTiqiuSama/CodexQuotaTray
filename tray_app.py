from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image

from hover_details import HoverDetails, HoverIcon
from icon_art import color_for, live_icon, quota_icon  # noqa: F401  (color_for re-exported)

from quota_sources import (
    QuotaSnapshot,
    collect_all,
    dump_snapshot,
    get_grok_quota,
    load_cached_snapshots,
    save_cached_snapshots,
)
from tps_sources import PERFORMANCE_PATH, TpsMonitor, save_performance


APP_NAME = "CodexQuotaTray"
REFRESH_SECONDS = 300
PROVIDERS = {
    "codex": "C",
    "grok": "G",
    "gemini": "M",
}
LAUNCHERS = {
    "codex": ("aumid", "OpenAI.Codex_2p2nqsd0c76g0!App"),
    "grok": ("cli", Path.home() / ".grok" / "bin" / "grok.exe"),
    "gemini": ("gui", Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Programs" / "antigravity" / "Antigravity.exe"),
}


def resource_path(path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / path


def make_icon(snapshots: dict[str, QuotaSnapshot]) -> Image.Image:
    image = Image.new("RGBA", (192, 64), (0, 0, 0, 0))
    for index, (provider, letter) in enumerate(PROVIDERS.items()):
        image.alpha_composite(make_provider_icon(snapshots[provider], letter), (index * 64, 0))
    return image


def make_provider_icon(snapshot: QuotaSnapshot, letter: str) -> Image.Image:
    return quota_icon(snapshot.remaining_percent, letter, stale=snapshot.stale)


def make_tps_icon(performance: dict, letter: str) -> Image.Image:
    return live_icon(performance.get("display_tps", 0) or 0, letter)


def make_display_icon(snapshot: QuotaSnapshot, letter: str, performance: dict) -> Image.Image:
    return make_tps_icon(performance, letter) if performance.get("active_sessions", 0) and performance.get("display_tps") is not None else make_provider_icon(snapshot, letter)


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
    hours, rem = divmod(delta, 3600)
    minutes = rem // 60
    return f"{hours} 小时 {minutes} 分"


def quota_line(snapshot: QuotaSnapshot) -> str:
    if snapshot.remaining_percent is None:
        status = "未知/获取失败"
    else:
        status = f"剩余 {snapshot.remaining_percent:.0f}% · 已用 {snapshot.used_percent:.0f}%"
    stale = " · 旧数据" if snapshot.stale else ""
    return f"{snapshot.label}: {status}{stale}"


class QuotaTray:
    def __init__(self) -> None:
        previous = load_cached_snapshots()
        self.snapshots = collect_all(previous)
        save_cached_snapshots(self.snapshots)
        self.stop_event = threading.Event()
        self.refreshing = False
        self.tps_monitor = TpsMonitor()
        self.performance = self.tps_monitor.poll()
        save_performance(self.performance)
        self.icons = {
            provider: HoverIcon(
                f"{APP_NAME}-{provider}",
                make_display_icon(self.snapshots[provider], letter, self.performance["providers"][provider]),
                self.tooltip(provider),
                menu=self.build_menu(provider),
            )
            for provider, letter in PROVIDERS.items()
        }

    def hover_text(self, provider: str) -> str:
        snapshot = self.snapshots[provider]
        data = self.performance["providers"][provider]
        lines = [data["label"], quota_line(snapshot), "", self.performance_line(provider),
            self.output_rate_line(provider), f"活动会话：{data['active_sessions']}", "",
            self.menu_reset(provider), self.menu_countdown(provider), self.menu_updated(provider)]
        if snapshot.error:
            lines.extend(("", "读取状态：" + snapshot.error))
        return "\n".join(lines)

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
        lines = [quota_line(snapshot), "", self.performance_line(provider), self.output_rate_line(provider), "", self.menu_reset(provider), self.menu_countdown(provider), "", self.menu_updated(provider)]
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

    def update_ui(self) -> None:
        save_cached_snapshots(self.snapshots)
        for provider, letter in PROVIDERS.items():
            icon = self.icons[provider]
            self.update_icon(provider, letter)
            icon.update_menu()

    def update_icon(self, provider: str, letter: str) -> None:
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

    def refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        try:
            self.snapshots = collect_all(self.snapshots)
            self.update_ui()
        finally:
            self.refreshing = False

    def request_refresh(self, _icon=None, _item=None) -> None:
        threading.Thread(target=self.refresh, daemon=True).start()

    def open_provider(self, provider: str) -> None:
        kind, target = LAUNCHERS[provider]
        try:
            if kind == "aumid":
                launched = ctypes.windll.shell32.ShellExecuteW(None, "open", f"shell:AppsFolder\\{target}", None, None, 1)
                if launched <= 32:
                    raise OSError(f"无法启动 Codex（错误 {launched}）")
                return
            path = Path(target)
            if not path.is_file():
                raise FileNotFoundError(str(path))
            flags = subprocess.CREATE_NEW_CONSOLE if kind == "cli" else 0
            subprocess.Popen([str(path)], cwd=str(path.parent), creationflags=flags, close_fds=True)
        except OSError as error:
            ctypes.windll.user32.MessageBoxW(None, str(error), f"{PROVIDERS[provider]} · 无法打开", 0x10)

    def diagnostic_text(self) -> str:
        sections = [dump_snapshot(self.snapshots), "", "数据源边界：", "- Codex：Codex App Server account/rateLimits/read"]
        for item in self.snapshots.values():
            if item.error:
                sections.append(f"- {item.label}：{item.error}")
        return "\n".join(sections)

    def open_diagnostics(self, _icon=None, _item=None) -> None:
        path = Path.home() / "AppData/Local" / APP_NAME / "diagnostics.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.diagnostic_text(), encoding="utf-8")
        os.startfile(path)  # type: ignore[attr-defined]

    def startup_enabled(self) -> bool:
        return self.startup_path().is_file()

    @staticmethod
    def startup_path() -> Path:
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{APP_NAME}.cmd"

    def toggle_startup(self, _icon=None, _item=None) -> None:
        path = self.startup_path()
        if path.is_file():
            path.unlink()
            return
        executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
        path.write_text(f'@echo off\r\nstart "" "{executable}"\r\n', encoding="utf-8")

    def exit_app(self, _icon=None, _item=None) -> None:
        self.stop_event.set()
        for icon in self.icons.values():
            icon.stop()

    def run(self) -> None:
        worker = threading.Thread(target=self.refresh_loop, daemon=True)
        worker.start()
        threading.Thread(target=self.tps_loop, name="tps-reader", daemon=True).start()
        hover = HoverDetails(self.icons, self.hover_text, self.stop_event)
        threading.Thread(target=hover.run, name="hover-details", daemon=True).start()
        threads = [
            threading.Thread(target=icon.run, name=f"tray-{provider}", daemon=True)
            for provider, icon in self.icons.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def refresh_loop(self) -> None:
        while not self.stop_event.wait(REFRESH_SECONDS):
            self.refresh()

    def tps_loop(self) -> None:
        while not self.stop_event.wait(1):
            try:
                self.performance = self.tps_monitor.poll()
                save_performance(self.performance)
                for provider, letter in PROVIDERS.items():
                    icon = self.icons[provider]
                    if icon.visible:
                        self.update_icon(provider, letter)
            except (OSError, ValueError, TypeError):
                continue


def acquire_single_instance():
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}-36d9a73f")
    if not handle or kernel32.GetLastError() == 183:
        return None
    return handle


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--check-hover":
        from hover_check import check_hover_runtime
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = load_cached_snapshots()
        tray.performance = json.loads(PERFORMANCE_PATH.read_text(encoding="utf-8"))
        return check_hover_runtime(tray.hover_text("codex"), Path(sys.argv[2]).resolve())
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
    handle = acquire_single_instance()
    if handle is None:
        return 0
    QuotaTray().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

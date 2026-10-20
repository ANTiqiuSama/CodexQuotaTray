import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import pystray
from PIL import Image

import tray_app
from tray_app import PROVIDERS, QuotaTray, reset_countdown, startup_command
from hover_details import HoverDetails, HoverIcon, TrackingTooltip, is_window_covered, tooltip_position, wintypes
from refresh_scheduler import RefreshScheduler
from quota_sources import QuotaSnapshot


class TrayMenuTests(unittest.TestCase):
    def setUp(self):
        # Exercise the real pystray menu validation without quota requests.
        self.tray = QuotaTray.__new__(QuotaTray)
        self.tray.open_provider = Mock()
        self.tray.open_details = Mock()
        self.tray.request_refresh = Mock()

    def test_all_provider_menus_construct_and_default_clicks_stay_bound(self):
        menus = {provider: self.tray.build_menu(provider) for provider in PROVIDERS}
        icon = Mock(spec=pystray.Icon)
        # Invoke after ALL menus are built to catch accidental late binding.
        for provider, menu in menus.items():
            with self.subTest(provider=provider):
                defaults = [item for item in menu.items if item.default]
                self.assertEqual(len(defaults), 1)
                self.assertEqual(defaults[0].text, "打开对应程序")
                menu(icon)
        self.assertEqual(self.tray.open_provider.call_args_list,
                         [call(provider) for provider in PROVIDERS])
        self.tray.request_refresh.assert_not_called()

    def test_context_menu_refresh_and_details_still_dispatch(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                menu = self.tray.build_menu(provider)
                items = {item.text: item for item in menu.items[:4]}
                items["额度详情"](None)
                self.tray.open_details.assert_called_with(provider)
                refresh = items["立即刷新"]
                refresh(None)
        self.assertEqual(self.tray.request_refresh.call_count, len(PROVIDERS))

    def test_claude_menu_choice_persists_and_requests_clean_restart(self):
        self.tray.exit_app = Mock()
        for name in ("menu_quota", "forecast_line", "menu_reset", "menu_countdown", "menu_updated", "menu_reset_outlook"):
            setattr(self.tray, name, Mock(return_value=""))
        for enabled in (False, True):
            active = {"codex": "C", **({"claude": "A"} if enabled else {})}
            with self.subTest(enabled=enabled), patch.object(tray_app, "PROVIDERS", active), \
                    patch.object(tray_app.provider_registry, "set_claude_enabled") as save:
                item = next(item for item in self.tray.build_menu("codex").items
                            if item.text == "Claude 额度检测（订阅开通后启用）")
                self.assertEqual(item.checked, enabled)
                item(None)
                save.assert_called_once_with(not enabled)
                self.assertTrue(self.tray.restart_requested)
        self.assertEqual(self.tray.exit_app.call_count, 2)


class HoverTests(unittest.TestCase):
    def test_dwell_live_updates_switch_and_leave(self):
        popup = Mock()
        text = Mock(return_value="first sample")
        hover = HoverDetails({}, text, threading.Event())
        rect = (1600, 1030, 1624, 1054)
        hover.update(popup, "codex", rect, 10)
        hover.update(popup, "codex", rect, 10.3)
        popup.show.assert_not_called()
        hover.update(popup, "codex", rect, 10.5)
        popup.show.assert_called_once_with("first sample", rect)
        text.return_value = "second sample"
        popup.hide.reset_mock()
        hover.update(popup, "codex", rect, 11.5)
        popup.show.assert_called_with("second sample", rect)
        popup.hide.assert_not_called()
        hover.update(popup, "grok", rect, 12)
        popup.hide.assert_called_once()
        popup.show.reset_mock()
        hover.update(popup, None, None, 12.5)
        hover.update(popup, "grok", rect, 13)
        popup.show.assert_not_called()

    def test_position_supports_both_taskbar_edges_and_negative_monitors(self):
        cases = [
            ((1600, 1030, 1624, 1054), (520, 240), (0, 0, 1920, 1024)),
            ((20, 0, 44, 24), (520, 240), (0, 56, 1920, 1080)),
            ((-200, -100, -176, -76), (520, 240), (-3840, -2160, 0, 0)),
            ((-3830, -2100, -3806, -2076), (520, 240), (-3840, -2160, 0, 0)),
        ]
        for anchor, size, work in cases:
            with self.subTest(anchor=anchor):
                x, y = tooltip_position(anchor, size, work)
                self.assertGreaterEqual(x, work[0])
                self.assertGreaterEqual(y, work[1])
                self.assertLessEqual(x + size[0], work[2])
                self.assertLessEqual(y + size[1], work[3])

    def test_v4_mouse_and_keyboard_activation_dispatch_once(self):
        opened = Mock()
        icon = HoverIcon("hover-test", Image.new("RGBA", (16, 16)),
            menu=pystray.Menu(pystray.MenuItem("Open", opened, default=True)))
        icon._version4 = True
        with patch.object(icon, "update_menu"):
            for event in (0x0202, 0x0400, 0x0202, 0x0401):
                icon._on_notify(0, (37 << 16) | event)
        self.assertEqual(opened.call_count, 2)

    def test_v4_context_menu_maps_to_existing_pystray_handler(self):
        icon = HoverIcon("hover-menu-test", Image.new("RGBA", (16, 16)))
        icon._version4 = True
        with patch.object(pystray.Icon, "_on_notify") as legacy:
            icon._on_notify(0, (37 << 16) | 0x007B)
            legacy.assert_called_once_with(0, 0x0205)

    def test_shell_hover_events_disambiguate_shared_overflow_rectangles(self):
        icon = HoverIcon("hover-events-test", Image.new("RGBA", (16, 16)))
        icon._version4 = True
        icon._on_notify(0, (37 << 16) | 0x0406)
        self.assertTrue(icon.hover_open)
        popup = Mock()
        hover = HoverDetails({}, lambda _: "Grok details", threading.Event())
        hover.update(popup, "grok", (10, 10, 34, 34), 10, dwell=0)
        popup.show.assert_called_once_with("Grok details", (10, 10, 34, 34))
        icon._on_notify(0, (37 << 16) | 0x0407)
        self.assertFalse(icon.hover_open)

    def test_correct_native_id_is_sent_when_registering_and_querying(self):
        icon = HoverIcon("hover-id-test", Image.new("RGBA", (16, 16)))
        icon._hwnd = 123
        with patch("hover_details.win32.Shell_NotifyIcon", return_value=True) as send:
            self.assertTrue(icon._message(4, 0, uVersion=4))
        args = send.call_args.args
        self.assertEqual(args[1].uID, icon.icon_id)
        self.assertEqual(args[1].uVersion, 4)
        icon._hwnd = None

    def test_unchanged_pixels_do_not_rebuild_native_icons(self):
        tray = QuotaTray.__new__(QuotaTray)
        image = Image.new("RGBA", (64, 64), "green")
        icon = Mock()
        icon.details_visible = False
        icon.icon = image
        tray.icons = {"codex": icon}
        tray.snapshots = {"codex": None}
        tray.performance = {"providers": {"codex": {}}}
        tray.tooltip = Mock(return_value="details")
        with patch("tray_app.make_display_icon", return_value=image.copy()):
            tray.update_icon("codex", "C")
        self.assertIs(icon.icon, image)
        with patch("tray_app.make_display_icon", return_value=Image.new("RGBA", (64, 64), "red")):
            tray.update_icon("codex", "C")
        self.assertNotEqual(icon.icon, image)

    def test_shell_popup_close_during_refresh_does_not_close_our_hover(self):
        icon = SimpleNamespace(_version4=True, hover_open=False)
        hover = HoverDetails({"codex": icon}, lambda _: "live", threading.Event())
        rect = (10, 10, 34, 66)
        with patch("hover_details.icon_rect", return_value=rect):
            self.assertEqual(hover.target_at(wintypes.POINT(20, 30)), (None, None))
            hover.candidate = "codex"
            self.assertEqual(hover.target_at(wintypes.POINT(20, 30)), ("codex", rect))
            self.assertEqual(hover.target_at(wintypes.POINT(40, 30)), (None, None))

    def test_hover_freezes_shell_updates_then_resumes(self):
        tray = QuotaTray.__new__(QuotaTray)
        image = Image.new("RGBA", (64, 64), "green")
        icon = SimpleNamespace(icon=image, title="previous", details_visible=True)
        tray.icons, tray.snapshots = {"codex": icon}, {"codex": None}
        tray.performance = {"providers": {"codex": {}}}
        tray.tooltip = Mock(return_value="latest")
        with patch("tray_app.make_display_icon", return_value=Image.new("RGBA", (64, 64), "red")) as render:
            tray.update_icon("codex", "C")
            render.assert_not_called()
            self.assertEqual(icon.title, "previous")
            icon.details_visible = False
            tray.update_icon("codex", "C")
            render.assert_called_once()
            self.assertEqual(icon.title, "latest")

    def test_steady_popup_does_not_repeat_showwindow_or_change_tracking_position(self):
        popup = TrackingTooltip.__new__(TrackingTooltip)
        popup.hwnd, popup.visible, popup.text = 123, True, "same text"
        popup.send = Mock()
        def window_rect(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 20, 50
            pointer._obj.right, pointer._obj.bottom = 220, 150
            return True
        def monitor_info(_monitor, pointer):
            pointer._obj.rcWork = wintypes.RECT(0, 0, 1000, 1000)
            return True
        with patch("hover_details.user.GetWindowRect", side_effect=window_rect), \
             patch("hover_details.user.GetMonitorInfoW", side_effect=monitor_info), \
             patch("hover_details.user.MonitorFromRect", return_value=1), \
             patch("hover_details.is_window_covered", return_value=False), \
             patch("hover_details.user.IsWindowVisible", return_value=True), \
             patch("hover_details.user.SetWindowPos") as move:
            for _ in range(20):
                popup.show("same text", (200, 158, 220, 182))
        popup.send.assert_not_called()
        move.assert_not_called()

    def test_coverage_uses_zorder_even_for_static_windows_ignored_by_hit_testing(self):
        def fullscreen_rect(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 0, 0
            pointer._obj.right, pointer._obj.bottom = 1920, 1080
            return True
        with patch("hover_details.user.GetWindow", side_effect=[456, 0]), \
             patch("hover_details.user.IsWindowVisible", return_value=True), \
             patch("hover_details.user.GetWindowLongW", return_value=0), \
             patch("hover_details.user.GetWindowRect", side_effect=fullscreen_rect):
            self.assertTrue(is_window_covered(123, wintypes.RECT(1120, 800, 1640, 1024)))
        with patch("hover_details.user.GetWindow", side_effect=[456, 0]), \
             patch("hover_details.user.IsWindowVisible", return_value=True), \
             patch("hover_details.user.GetWindowLongW", return_value=0x20):
            self.assertFalse(is_window_covered(123, wintypes.RECT(1120, 800, 1640, 1024)))

    def test_hover_details_include_data_beyond_shell_127_character_limit(self):
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {"codex": QuotaSnapshot("codex", "Codex", 10080,
            13, 87, 1800000000, 1792132045, False, None)}
        tray.performance = {"providers": {"codex": {"label": "GPT / Codex",
            "active_sessions": 2, "generation_tps": 58.5, "output_tps_60": 12.0,
            "output_tokens_60": 720, "last_sample_at": 1792132045,
            "basis": ["客户端流式统计：真实输出计数，包含思考和工具输出"]}}}
        text = tray.hover_text("codex")
        for expected in ("58.5 tok/s", "720 输出tokens", "活动会话：2", "剩余 87%",
                         "来源：", "重置时间：", "距离重置：", "更新时间："):
            self.assertIn(expected, text)
        self.assertGreater(len(text), 127)


class FeatureIntegrationTests(unittest.TestCase):
    def tray(self):
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {"codex": QuotaSnapshot("codex", "Codex", 300, 13, 87, 1800000000, 1792132045,
            False, None, 10080, 71, 29, 1800400000)}
        tray.performance = {"providers": {"codex": {"label": "GPT / Codex", "active_sessions": 1,
            "generation_tps": 58.5, "display_tps": 58.5, "output_tps_60": 12.0, "output_tokens_60": 720,
            "last_sample_at": 1792132045, "basis": ["客户端流式统计"]}}}
        return tray

    def test_weekly_countdown_uses_days(self):
        with patch("tray_app.time.time", return_value=0):
            self.assertEqual(reset_countdown(5 * 86400 + 3 * 3600 + 60), "5 天 3 小时")
            self.assertEqual(reset_countdown(2 * 3600 + 300), "2 小时 5 分")

    def test_startup_command_never_relies_on_file_association(self):
        with patch.object(tray_app.sys, "frozen", False, create=True):
            self.assertIn("-X utf8", startup_command())

    def test_finished_generation_wakes_refresh_worker(self):
        tray = self.tray()
        tray.scheduler = RefreshScheduler(["codex"], 0)
        tray.scheduler.take_due(0)
        tray.scheduler.completed("codex", tray.snapshots["codex"], 400)
        tray.wake = threading.Event()
        tray.last_samples = {"codex": 100.0}
        data = tray.performance["providers"]["codex"]
        data["last_sample_at"] = 200.0
        tray.feed_scheduler(tray.performance, 500)
        self.assertTrue(tray.wake.is_set())
        self.assertLessEqual(tray.scheduler.due["codex"], 510)

class IconArtTests(unittest.TestCase):
    def test_every_state_renders_a_64px_rgba_icon(self):
        from icon_art import live_icon, quota_icon
        images = [quota_icon(value, letter, stale) for letter in "CGM"
                  for value in (None, 0, 12, 42, 87, 100) for stale in (False, True)]
        images += [live_icon(value, letter) for letter in "CGM" for value in (0, 7, 58, 143, 2500, 99999)]
        for image in images:
            self.assertEqual((image.mode, image.size), ("RGBA", (64, 64)))
            self.assertIsNotNone(image.getbbox())

    def test_ring_length_and_colour_follow_remaining_quota(self):
        from icon_art import AMBER, CLAY, accent, quota_icon, ring_color
        self.assertEqual(ring_color(87, "C"), accent("C"))
        self.assertEqual(ring_color(42, "C"), AMBER)
        self.assertEqual(ring_color(12, "C"), CLAY)
        self.assertNotEqual(quota_icon(87, "C").tobytes(), quota_icon(42, "C").tobytes())
        # Stale data adds a badge without changing the quota reading.
        self.assertNotEqual(quota_icon(87, "C").tobytes(), quota_icon(87, "C", stale=True).tobytes())

    def test_large_tps_values_stay_short(self):
        from icon_art import tps_text
        self.assertEqual([tps_text(v) for v in (7.4, 143, 2500, 99999)], ["7", "143", "2k", "9k+"])


if __name__ == "__main__":
    unittest.main()


class TrayOrderTests(unittest.TestCase):
    @staticmethod
    def snap(provider, remaining, reset, second=None, second_reset=None):
        from quota_sources import QuotaSnapshot
        return QuotaSnapshot(provider, provider, 300, None if remaining is None else 100 - remaining, remaining, reset,
                             1.0, False, None, 10080 if second is not None else None,
                             None if second is None else 100 - second, second, second_reset)

    def test_soonest_refresh_first_unknown_next_exhausted_last(self):
        import tray_app
        snapshots = {
            "codex": self.snap("codex", 80, 5000, second=40, second_reset=9000),   # weekly window binds
            "grok": self.snap("grok", 0, 1000),                                     # used up
            "gemini": self.snap("gemini", 50, 3000),
            "claude": self.snap("claude", None, None),
            "mimo": self.snap("mimo", 60, 99999, second=5, second_reset=2000),      # daily budget is not a refresh
        }
        self.assertEqual(tray_app.tray_order(snapshots, list(snapshots)), ["gemini", "codex", "mimo", "claude", "grok"])

    def test_reorder_assigns_providers_to_icons_by_screen_position(self):
        tray = QuotaTray.__new__(QuotaTray)

        class FakeIcon:
            details_visible = hover_open = False
            def __init__(self, name, left):
                self.name, self.left, self.menu = name, left, None
        # Explorer shows the second-created icon on the left.
        slots = [FakeIcon("slot0", 200), FakeIcon("slot1", 100), FakeIcon("slot2", None)]
        tray.slots = slots
        tray.icons = {"codex": slots[0], "grok": slots[1], "gemini": slots[2]}
        tray.shown_order, tray.reordered_at = [], 0.0
        tray.build_menu = lambda provider: f"menu-{provider}"
        tray.update_icon = Mock()
        tray.snapshots = {"codex": self.snap("codex", 50, 900), "grok": self.snap("grok", 0, 100),
                          "gemini": self.snap("gemini", 50, 300)}
        rect = lambda icon: None if icon.left is None else (icon.left, 0, icon.left + 24, 24)
        with patch.object(tray_app, "PROVIDERS", {"codex": "C", "grok": "G", "gemini": "M"}), \
                patch.object(tray_app, "TRAY_ORDER_MODE", "auto"), patch.object(tray_app.hover_details, "icon_rect", rect):
            self.assertTrue(tray.maybe_reorder(1000.0))
            # Left to right on screen: slot1, slot0, then the unplaced slot2.
            self.assertEqual({p: i.name for p, i in tray.icons.items()}, {"gemini": "slot1", "codex": "slot0", "grok": "slot2"})
            self.assertEqual(slots[1].menu, "menu-gemini")
            self.assertEqual(tray.shown_order, ["gemini", "codex", "grok"])
            self.assertFalse(tray.maybe_reorder(2000.0))          # already right: nothing changes
            tray.snapshots["codex"] = self.snap("codex", 50, 50)
            self.assertFalse(tray.maybe_reorder(1010.0))          # too soon after the last change
            slots[0].hover_open = True
            self.assertFalse(tray.maybe_reorder(3000.0))          # pointer is on an icon
            slots[0].hover_open = False
            self.assertTrue(tray.maybe_reorder(3000.0))
            self.assertEqual(tray.icons["codex"].name, "slot1")
            slots[0].left, slots[1].left = 100, 200               # user dragged the icons
            self.assertTrue(tray.maybe_reorder(4000.0))
            self.assertEqual(tray.icons["codex"].name, "slot0")


class CodexLocalLimitTests(unittest.TestCase):
    def test_session_record_replaces_older_snapshot_and_limit_error_requests_refresh(self):
        tray = QuotaTray.__new__(QuotaTray)
        old = QuotaSnapshot("codex", "Codex", 300, 20.0, 80.0, 5000, 100.0, False, None)
        tray.snapshots = {"codex": old}
        tray.history = None
        tray.update_ui = Mock()
        tray.scheduler = Mock()
        tray.wake = threading.Event()
        limits = {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": 9000}}
        tray.tps_monitor = SimpleNamespace(codex_rate_limits=(200.0, limits), codex_limit_hit_at=0.0)
        tray.update_codex_locally(200.0 + 6 * 3600 + 1)
        self.assertIs(tray.snapshots["codex"], old)   # records older than 6 hours are ignored
        tray.update_codex_locally(201.0)
        self.assertEqual(tray.snapshots["codex"].remaining_percent, 0.0)
        tray.update_ui.assert_called_once()
        tray.update_codex_locally(202.0)              # same record: no second update
        tray.update_ui.assert_called_once()
        tray.scheduler.request.assert_not_called()
        tray.tps_monitor.codex_limit_hit_at = 205.0
        tray.update_codex_locally(206.0)
        tray.scheduler.request.assert_called_once_with(["codex"], 209.0, "额度用尽")
        self.assertTrue(tray.wake.is_set())
        tray.update_codex_locally(207.0)
        tray.scheduler.request.assert_called_once()


class AgentPanelTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        import agent_tasks
        self.folder = tempfile.TemporaryDirectory()
        tray = QuotaTray.__new__(QuotaTray)
        tray.snapshots = {provider: tray_app.placeholder(provider) for provider in PROVIDERS}
        tray.performance = {"providers": {provider: {"label": provider, "display_tps": 42.0} for provider in PROVIDERS}}
        tray.agent_groups = agent_tasks.AgentGroups(Path(self.folder.name) / "groups.json")
        tray.summarizer = agent_tasks.Summarizer(None)
        tray.clusters = {}
        tray.tps_monitor = SimpleNamespace(schedules=[{"key": "Codex:日报", "title": "日报", "prompt": "整理今日日报", "last_at": 0.0, "next_at": 0.0}])

        def agent(key, provider, live=True, parent="", schedule=""):
            return {"key": key, "provider": provider, "source": "", "model": "", "title": f"任务{key}", "thread_title": "",
                    "request": "", "doing": "运行测试", "live": live, "at": 1.0, "parent": parent, "role": "", "folder": "",
                    "tps": None, "schedule": schedule}
        tray.agents = [agent("g1", "grok"), agent("c1", "codex"), agent("c2", "codex", parent="c1"), agent("c3", "codex", live=False),
                       agent("s1", "codex", schedule="origin:巡检")]
        self.tray = tray

    def tearDown(self):
        self.folder.cleanup()

    def test_panel_for_every_provider_with_focus_first_and_schedules_apart(self):
        idle = self.tray.panel("gemini")
        self.assertNotEqual(idle.status_tone, "live")
        self.assertEqual(len(idle.bubbles), 3)
        model = self.tray.panel("codex")
        self.assertEqual(model.status, "2 个在工作")
        self.assertNotIn("s1", [key for bubble in model.bubbles for key in bubble.keys])
        self.assertEqual([(chip.title, chip.running) for chip in model.schedules], [("任务s1", True), ("日报", False)])
        self.assertEqual(model.schedules[1].when, "待运行")
        self.assertEqual(model.tps, 42.0)
        self.assertEqual([bubble.focus for bubble in model.bubbles], [True, True, False])
        group = next(bubble for bubble in model.bubbles if bubble.members)
        self.assertEqual(len(group.members), 2)
        self.assertTrue(model.quota)

    def test_claude_without_subscription_keeps_the_tooltip(self):
        free = tray_app.placeholder("claude")
        free.error, free.remaining_percent = "未找到订阅额度（当前 Claude 账号无订阅）", None
        self.assertFalse(tray_app.has_panel("claude", free))
        self.assertTrue(tray_app.has_panel("grok", free))
        self.assertTrue(tray_app.has_panel("claude", tray_app.placeholder("claude")))
        self.tray.snapshots["claude"] = free
        self.assertIsNone(self.tray.panel("claude"))

    def test_merge_and_split_through_clusters(self):
        model = self.tray.panel("codex")
        solo = next(b.key for b in model.bubbles if b.key == "c3")
        merged = self.tray.merge(solo, "g1")
        self.assertTrue(merged.startswith("group:"))
        self.assertIsNone(self.tray.merge("missing", "g1"))
        model = self.tray.panel("codex")
        self.assertIn(merged, [b.key for b in model.bubbles])
        self.assertEqual(sorted(self.tray.split(merged)), ["c3", "g1"])
        self.assertIsNone(self.tray.split("c3"))

    def test_open_panel_hands_drops_to_the_tray_and_closes_after_grace(self):
        panel = Mock()
        panel.provider = "codex"
        panel.closing_at = None
        panel.scene.dragging = None
        panel.contains.return_value = True
        panel.frame.return_value = [("merge", "a", "b")]
        agents = Mock()
        agents.merge.return_value = "group:ab"
        hover = HoverDetails({}, Mock(), threading.Event(), agents=agents)
        point = wintypes.POINT(5, 5)
        hover.drive_panel(panel, point, None, None, False, False, 10.0)
        agents.merge.assert_called_once_with("a", "b")
        panel.scene.absorb.assert_called_once_with("a", "b", "group:ab", 10.0)
        panel.frame.return_value = []
        panel.contains.return_value = False
        hover.drive_panel(panel, point, None, None, False, False, 10.2)
        panel.close.assert_not_called()
        hover.drive_panel(panel, point, None, None, False, False, 10.0 + hover.PANEL_GRACE + 0.1)
        panel.close.assert_called_once()

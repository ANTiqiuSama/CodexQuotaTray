import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import settings_view as view
from icon_art import AMBER, accent, quota_icon, ring_color
from quota_sources import QuotaSnapshot


def snapshot(**extra):
    now = time.time()
    base = dict(provider="gemini", label="Gemini", window_minutes=300, used_percent=60, remaining_percent=40,
                resets_at=int(now + 3600), updated_at=now, stale=False, error=None)
    base.update(extra)
    return QuotaSnapshot(**base)


class SettingsViewTests(unittest.TestCase):
    def test_overview_adds_columns_as_space_becomes_available(self):
        from settings_window import overview_columns
        self.assertEqual(overview_columns(650, 200, 10, 5), 3)
        self.assertEqual(overview_columns(830, 200, 10, 5), 4)
        self.assertEqual(overview_columns(1040, 200, 10, 5), 5)
        self.assertEqual(overview_columns(2000, 200, 10, 5), 5)
        self.assertEqual(overview_columns(200, 200, 10, 0), 1)
    def test_resize_reflows_once_after_resize_and_ignores_child_events(self):
        import settings_window
        from unittest.mock import Mock
        window = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
        window.root = Mock()
        window.root.state.return_value = 'normal'
        window.root.after.side_effect = ['first', 'second']
        window._resize_after = None
        window._resize_size = None
        window.refresh_live = Mock()
        window._on_resize(SimpleNamespace(widget=object(), width=800, height=600))
        window.root.after.assert_not_called()
        window._on_resize(SimpleNamespace(widget=window.root, width=800, height=600))
        window._on_resize(SimpleNamespace(widget=window.root, width=900, height=600))
        window.root.after_cancel.assert_called_once_with('first')
        self.assertEqual(window.root.after.call_count, 2)
        window._finish_resize()
        window.refresh_live.assert_called_once()
        self.assertIsNone(window._resize_after)

    def test_dark_titlebar_does_not_run_for_non_windows(self):
        import settings_window
        with patch.object(settings_window.os, 'name', 'posix'):
            self.assertFalse(settings_window.apply_dark_titlebar(object()))

    def test_switch_page_unmaps_hidden_pages_to_avoid_resize_relayout(self):
        import settings_window
        from unittest.mock import Mock
        window = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
        first, second = Mock(), Mock()
        first.winfo_manager.return_value = 'place'
        second.winfo_manager.return_value = ''
        window.pages = {'overview': first, 'tasks': second}
        window.nav = {}
        window.bodies = {'tasks': SimpleNamespace(canvas=Mock())}
        window.refresh_live = Mock()
        window.show_page('tasks')
        first.place_forget.assert_called_once()
        second.place.assert_called_once_with(relx=0, rely=0, relwidth=1, relheight=1)
        window.refresh_live.assert_called_once()

    def test_gemini_panel_uses_full_width_for_quota_without_tps(self):
        import agent_panel
        model = agent_panel.PanelModel('gemini', 'G', 'Gemini', '空闲',
            (('Argon', 30), ('Claude/GPT', 40), ('Gemini', 50)), 99, ())
        self.assertFalse(model.header[-1])
        self.assertEqual(agent_panel._header_model(model.header).provider, 'gemini')
        self.assertEqual(len(agent_panel.meter_columns(3, False)), 3)
        self.assertGreater(agent_panel.meter_columns(3, False)[0][1], agent_panel.meter_columns(3, True)[0][1])
        agent_panel.background(model.header, 120, 1.0)

    def test_gemini_always_uses_its_tps_colour(self):
        self.assertEqual(accent("G"), ring_color(40, "G"))
        self.assertEqual(accent("G"), ring_color(10, "G"))
        self.assertEqual(AMBER, ring_color(40, "C"))          # other sources keep the warning colours

    def test_quota_rows_are_written_like_the_panel_columns(self):
        now = time.time()
        pools = [{"key": "argon", "label": "Argon", "remaining_percent": 40, "resets_at": now + 4000},
                 {"key": "opus", "label": "Claude/GPT", "remaining_percent": 85}]
        rows = view.quota_rows(snapshot(pools=pools), now)
        self.assertEqual([("1小时7分钟", 40, "Argon"), ("Claude/GPT", 85, "")], [row[:3] for row in rows])
        two = view.quota_rows(snapshot(provider="codex", resets_at=int(now + 3600), secondary_window_minutes=10080,
                                       secondary_remaining_percent=38), now)
        self.assertEqual([("1小时", "5 小时"), ("每周", "")], [(row[0], row[2]) for row in two])   # no weekly reset known
        self.assertEqual(view.window_name(525600), "年度")
        self.assertEqual(view.refresh_text(12.4, "悬停"), "12 秒后刷新 · 悬停")
        self.assertEqual(view.refresh_text(300, "空闲"), "5 分钟后刷新 · 空闲")
        self.assertEqual(view.refresh_text(0, "正在刷新"), "正在刷新")
        self.assertEqual(view.refresh_text(None, ""), "")

    def test_pictures_render_at_scale(self):
        card = view.SourceCard("gemini", "G", "Gemini", "1 个在工作", "live", view.quota_rows(snapshot()), 142.0, 11.0, 1,
                               "按当前速度可用到重置", "ok")
        self.assertEqual((round(view.CARD_W * 1.5), round(view.CARD_H * 1.5)), view.source_card(card, quota_icon(40, "G"), 1.5).size)
        # A grid row of one-window sources is shorter: no empty band under a single bar.
        self.assertEqual(view.card_height(1), view.CARD_H - 2 * view.ROW_H)
        self.assertEqual(view.source_card(card, None, 1.0, slots=1).size, (view.CARD_W, view.card_height(1)))
        paid = view.SourceCard("codex", "C", "Codex", "可用购买额度", "paid",
                               (("1小时", 0.0, "购买额度", "paid", "59.8K"), ("5天3小时", 70.0, "每周", "", None)), 0.0, 0.0, 0, "", "warn")
        self.assertIsNotNone(view.source_card(paid, None, 1.25))
        now = time.time()
        events = [SimpleNamespace(ended_at=now - 100, provider="gemini", output_tokens=500, count_in_rate=True)]
        self.assertEqual((652, 150), view.activity_chart(events, now, {"gemini": "G"}).size)
        agents = [{"key": "a", "provider": "gemini", "title": "补测试", "live": True, "doing": "运行测试", "tps": 50.0, "pane": "w1:p1"}]
        self.assertGreater(view.agent_rows(agents, {"gemini": "G"}).size[1], 60)

    def test_cards_follow_the_panel_and_tray_icon(self):
        import settings_window
        icon = quota_icon(40, "G")
        app = SimpleNamespace(snapshots={"gemini": snapshot()},
                              performance={"providers": {"gemini": {"display_tps": 99.0, "output_tps_60": 3.0, "active_sessions": 1}}},
                              icons={"gemini": SimpleNamespace(icon=icon)},
                              panel=lambda provider: SimpleNamespace(status="1 个在工作", status_tone="live", forecast="够用", forecast_tone="ok"))
        [(card, image)] = settings_window.build_cards(app)
        self.assertIs(image, icon)
        self.assertEqual(("G", "1 个在工作", None, 1), (card.letter, card.status, card.tps, card.active))

    def test_cards_use_the_panel_columns_in_tray_order_with_the_refresh_plan(self):
        import settings_window
        from refresh_scheduler import RefreshScheduler
        now = time.time()
        codex = snapshot(provider="codex", label="Codex", remaining_percent=0.0, used_percent=100.0,
                         secondary_window_minutes=10080, secondary_remaining_percent=70.0)
        model = SimpleNamespace(status="可用购买额度", status_tone="paid", forecast="5 小时额度已用完", forecast_tone="warn",
                                quota=(("1小时", 0.0, "购买额度"), ("5天3小时", 70.0, "")),
                                tone_of=lambda slot: "paid" if slot == 0 else "",
                                figure_of=lambda slot: ("59.8", "K") if slot == 0 else None)
        scheduler = RefreshScheduler(("codex", "claude"), now)
        scheduler.take_due(now)
        for provider in ("codex", "claude"):
            scheduler.completed(provider, SimpleNamespace(error=None, resets_at=None, secondary_resets_at=None), now)
        app = SimpleNamespace(snapshots={"codex": codex, "claude": snapshot(provider="claude", label="Claude Code")},
                              performance={"providers": {}}, icons={}, scheduler=scheduler,
                              panel=lambda provider: model if provider == "codex" else None)
        cards = [card for card, _ in settings_window.build_cards(app, now)]
        self.assertEqual([card.provider for card in cards], ["claude", "codex"])          # the tray's order
        self.assertEqual(cards[1].rows, (("1小时", 0.0, "购买额度", "paid", "59.8K"), ("5天3小时", 70.0, "每周", "", None)))
        self.assertRegex(cards[1].refresh, r"^\d+ (秒|分钟)后刷新 · 空闲$")
        self.assertEqual(scheduler.plan("codex", now), (scheduler.due["codex"] - now, "空闲"))
        self.assertEqual(scheduler.plan("grok", now), (None, ""))

    def test_paragraphs_wrap_chinese_by_character_and_keep_latin_words_whole(self):
        import settings_window
        measure = len                                   # one unit per character
        text = "Grok 不在 AI Stupid Level 的榜单上，所以改用 LiveBench 最新公开榜单"
        lines = settings_window.wrap_text(text, measure, 16).split("\n")
        self.assertTrue(all(len(line) <= 16 for line in lines))
        self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))
        self.assertEqual(lines[1], "Stupid Level 的榜单")                    # Chinese fills the line, not cut at its space
        self.assertTrue(all(word in " ".join(lines).split() or word in "".join(lines) for word in ("Stupid", "LiveBench")))
        self.assertFalse(any(line.endswith(("Stu", "LiveBe")) for line in lines))   # a Latin word moves whole
        self.assertEqual(settings_window.PAGE_ALIASES["mimo"], "sources")
        self.assertEqual({key for key, _ in settings_window.PAGES}, {"overview", "sources", "iq", "tasks", "general"})

    def test_preferences_round_trip_and_apply(self):
        import tray_app
        folder = Path(tempfile.mkdtemp())
        herdr = SimpleNamespace(enabled=True, status="已接入", rows=[1])
        tray = SimpleNamespace(tps_monitor=SimpleNamespace(herdr=herdr))
        with patch.object(tray_app, "PREFERENCES_PATH", folder / "settings.json"), patch.dict("os.environ", {}, clear=False) as env:
            env.pop("CODEX_QUOTA_HERDR", None)
            (folder / "settings.json").write_text('{"herdr": true, "tray_order": "auto"}', encoding="utf-8")
            tray_app.QuotaTray.save_preferences(SimpleNamespace(apply_preferences=lambda prefs: tray_app.QuotaTray.apply_preferences(tray, prefs)),
                                                {"herdr": False})
            self.assertEqual({"herdr": False}, tray_app.load_preferences())   # retired tray_order dropped
            self.assertFalse(herdr.enabled)
            self.assertFalse(hasattr(tray_app, "TRAY_ORDER_MODE"))
            tray_app.QuotaTray.apply_preferences(tray, {"tray_order": "auto", "herdr": True})
            self.assertTrue(herdr.enabled)


class SourceSwitchTests(unittest.TestCase):
    def test_states_explain_every_source_and_saved_switches_win(self):
        import providers
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"LOCALAPPDATA": root}):
            states = {item["key"]: item for item in providers.provider_states("codex,grok")}
            self.assertEqual({"codex", "grok", "gemini", "claude", "mimo"}, set(states))
            self.assertFalse(states["claude"]["enabled"])
            self.assertIn("未包含", states["claude"]["reason"])
            providers.set_provider_choices({"claude": True, "grok": False})
            states = {item["key"]: item for item in providers.provider_states("codex,grok")}
            self.assertTrue(states["claude"]["enabled"])
            self.assertEqual("手动开启", states["claude"]["reason"])
            self.assertFalse(states["grok"]["enabled"])
            self.assertEqual(["codex", "claude"], providers.enabled_keys("codex,grok"))
            providers.set_provider_choices({"grok": None})
            self.assertNotIn("grok", providers.provider_choices())

    def test_mimo_manual_plan_from_settings_form(self):
        import json
        import mimo_usage
        self.assertEqual(49.2e9, mimo_usage.parse_credits("49.2B"))
        self.assertEqual(4.92e10, mimo_usage.parse_credits("492亿"))
        self.assertIsNone(mimo_usage.parse_credits("abc"))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "mimo.json"
            mimo_usage.save_manual_plan("49.2B", "2027年6月1日", "1.5B", "Max", path=path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("2027-06-01", saved["period_end"])
            self.assertEqual(1.5e9, saved["used_credits"])
            with self.assertRaises(ValueError):
                mimo_usage.save_manual_plan("", "2027-06-01", path=path)
            with self.assertRaises(ValueError):
                mimo_usage.save_manual_plan("1B", "next year", path=path)
            mimo_usage.remove_manual_plan(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

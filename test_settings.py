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

    def test_quota_rows_prefer_model_pools(self):
        pools = [{"key": "argon", "label": "Argon", "remaining_percent": 40, "resets_at": time.time() + 4000},
                 {"key": "opus", "label": "Claude/GPT", "remaining_percent": 85}]
        rows = view.quota_rows(snapshot(pools=pools))
        self.assertEqual(["Argon", "Claude/GPT"], [row[0] for row in rows])
        self.assertIn("后重置", rows[0][2])
        two = view.quota_rows(snapshot(provider="codex", secondary_window_minutes=10080, secondary_remaining_percent=38))
        self.assertEqual(["5 小时", "每周"], [row[0] for row in two])

    def test_pictures_render_at_scale(self):
        card = view.SourceCard("gemini", "G", "Gemini", "1 个在工作", "live", view.quota_rows(snapshot()), 142.0, 11.0, 1,
                               "按当前速度可用到重置", "ok")
        self.assertEqual((round(view.CARD_W * 1.5), round(view.CARD_H * 1.5)), view.source_card(card, quota_icon(40, "G"), 1.5).size)
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

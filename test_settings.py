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
        self.assertEqual(("G", "1 个在工作", 99.0, 1), (card.letter, card.status, card.tps, card.active))

    def test_preferences_round_trip_and_apply(self):
        import tray_app
        folder = Path(tempfile.mkdtemp())
        herdr = SimpleNamespace(enabled=True, status="已接入", rows=[1])
        tray = SimpleNamespace(tps_monitor=SimpleNamespace(herdr=herdr))
        with patch.object(tray_app, "PREFERENCES_PATH", folder / "settings.json"), patch.dict("os.environ", {}, clear=False) as env:
            env.pop("CODEX_QUOTA_TRAY_ORDER", None)
            env.pop("CODEX_QUOTA_HERDR", None)
            tray_app.QuotaTray.save_preferences(SimpleNamespace(apply_preferences=lambda prefs: tray_app.QuotaTray.apply_preferences(tray, prefs)),
                                                {"herdr": False, "tray_order": "fixed"})
            self.assertEqual({"herdr": False, "tray_order": "fixed"}, tray_app.load_preferences())
            self.assertFalse(herdr.enabled)
            self.assertEqual("fixed", tray_app.TRAY_ORDER_MODE)
            tray_app.QuotaTray.apply_preferences(tray, {"tray_order": "auto", "herdr": True})
            self.assertTrue(herdr.enabled)
            self.assertEqual("auto", tray_app.TRAY_ORDER_MODE)


if __name__ == "__main__":
    unittest.main()

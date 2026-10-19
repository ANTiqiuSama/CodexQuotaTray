import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reset_watch
from reset_watch import ResetOutlook, card_lines, fetch_outlook, parse_codex_reset, parse_willcodexreset, parse_willreset

WILLRESET = {"answer": "promised", "fetched_at": "2026-10-19T04:18:22.032775575Z", "last_reset_at": "2026-10-17T21:12:21Z",
             "last_reset_id": "2111566005684289735", "last_reset_text": "Direct usage reset", "prob_24": 71, "prob_48": 81,
             "promise_active": True, "promise_quote": "Done", "status": "none"}
CODEX_RESET = {"probabilities": {"rounded_24h": 45, "rounded_48h": 70}, "last_reset_at": "2026-10-17T20:41:01.000Z",
               "tease_signal": {"post": {"quote": "We'll see"}}}
RADAR = {"code": 0, "data": {"probability24h": 12, "probability48h": 30, "events": [
    {"id": "a", "kind": "reset", "occurredAt": "2026-10-15T06:14:00.000Z", "title": "Direct usage reset"},
    {"id": "b", "kind": "reset", "occurredAt": "2026-10-17T21:12:00.000Z", "title": "Banked reset"},
    {"id": "c", "kind": "tibo", "occurredAt": "2026-10-19T04:13:18.000Z", "title": "@thsottiaux"}]}}


class ResetWatchTests(unittest.TestCase):
    def test_parsers(self):
        a = parse_willreset(WILLRESET)
        self.assertEqual((a.prob_24, a.prob_48, a.promise, a.last_reset_text), (71, 81, "Done", "Direct usage reset"))
        self.assertAlmostEqual(a.last_reset_at, 1792271541.0)
        b = parse_codex_reset(CODEX_RESET)
        self.assertEqual((b.prob_24, b.prob_48, b.promise), (45, 70, "We'll see"))
        c = parse_willcodexreset(RADAR)
        self.assertEqual((c.prob_24, c.last_reset_id, c.last_reset_text), (12, "b", "Banked reset"))

    def test_falls_back_to_next_source(self):
        def get(url):
            if "willreset.com" in url:
                raise OSError("offline")
            return CODEX_RESET
        outlook, errors = fetch_outlook(get, now=100.0)
        self.assertEqual(outlook.source, "codex-reset.com")
        self.assertEqual(outlook.fetched_at, 100.0)
        self.assertEqual(len(errors), 1)
        none, errors = fetch_outlook(lambda url: {}, now=100.0)
        self.assertIsNone(none)
        self.assertEqual(len(errors), 3)

    def test_levels_and_staleness(self):
        now = time.time()
        make = lambda p24, p48, at=now: ResetOutlook("s", "p", p24, p48, fetched_at=at)
        self.assertEqual(make(71, 81).level(now), "high")
        self.assertEqual(make(40, 70).level(now), "watch")
        self.assertEqual(make(10, 20).level(now), "low")
        self.assertEqual(make(90, 95, now - 4 * 3600).level(now), "unknown")

    def test_card_banner_only_when_high_and_mentions_low_quota(self):
        now = time.time()
        high = ResetOutlook("willreset.com", "p", 71, 81, now - 3600, "x", "Direct usage reset", "Done", now)
        banner, notes = card_lines(high, 12.0, now)
        self.assertIn("71%", banner)
        self.assertIn("12%", banner)
        self.assertTrue(any("非 OpenAI 官方" in note for note in notes))
        banner, _ = card_lines(ResetOutlook("s", "p", 20, 30, fetched_at=now), 12.0, now)
        self.assertEqual(banner, "")
        self.assertEqual(card_lines(None, 50.0, now), ("", []))


class TrayResetTests(unittest.TestCase):
    def test_cached_high_forecast_notifies_after_fresh_startup_read_once(self):
        import tray_app
        now = time.time()
        high = ResetOutlook("s", "p", 71, 81, now - 86400, "r1", fetched_at=now)
        performance = {"providers": {key: {} for key in tray_app.PROVIDERS}}
        with patch.object(reset_watch, "load", return_value=high), \
                patch.object(reset_watch, "save"), patch.object(reset_watch, "ALERT_PERCENT", 1), \
                patch.object(reset_watch, "ENABLED", True), \
                patch.object(tray_app, "load_cached_snapshots", return_value={}), \
                patch.object(tray_app, "QuotaHistory"), patch.object(tray_app, "TpsMonitor") as monitor, \
                patch.object(tray_app, "HoverIcon"), patch.object(tray_app, "make_display_icon"), \
                patch.object(tray_app.QuotaTray, "migrate_startup"), \
                patch.object(tray_app.QuotaTray, "tooltip", return_value=""), \
                patch.object(tray_app.QuotaTray, "build_menu", return_value=None):
            monitor.return_value.snapshot.return_value = performance
            tray = tray_app.QuotaTray()
            tray.notify, tray.update_ui = Mock(), Mock()
            tray.apply_reset_outlook(high, now)
            tray.apply_reset_outlook(high, now + 1)
            self.assertEqual(tray.notify.call_count, 1)
            self.assertEqual(tray.notify.call_args[0][1], "Codex 可能即将全局重置")

    def make_tray(self):
        import tray_app
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        tray.snapshots = {"codex": SimpleNamespace(display_remaining=10.0)}
        tray.scheduler = Mock()
        tray.wake = Mock()
        tray.update_ui = Mock()
        tray.notify = Mock()
        tray.reset_outlook = None
        tray.reset_alerted = "unknown"
        return tray

    def test_rising_edge_notifies_once_and_new_reset_refreshes(self):
        import tray_app
        tray_app.RESET_PATH = tray_app.Path("/tmp/cqt-reset-test.json")
        tray = self.make_tray()
        now = time.time()
        high = ResetOutlook("s", "p", 75, 85, now - 86400, "r1", fetched_at=now)
        tray.apply_reset_outlook(high, now)
        tray.apply_reset_outlook(high, now + 600)
        self.assertEqual(tray.notify.call_count, 1)
        self.assertIn("10%", tray.notify.call_args[0][2])
        tray.scheduler.request.assert_not_called()
        done = ResetOutlook("s", "p", 5, 10, now - 60, "r2", "Direct usage reset", fetched_at=now)
        tray.apply_reset_outlook(done, now + 1200)
        tray.scheduler.request.assert_called_once_with(["codex"], now + 1200, "全局重置")
        self.assertEqual(tray.notify.call_args[0][1], "Codex 已全局重置")

    def test_icon_badge_changes_pixels(self):
        from icon_art import quota_icon
        self.assertNotEqual(quota_icon(40, "C").tobytes(), quota_icon(40, "C", reset_hint=True).tobytes())

    def test_reset_badge_is_preserved_while_showing_tps(self):
        from icon_art import RESET_BLUE
        from tray_app import make_display_icon
        performance = {"active_sessions": 1, "display_tps": 50.0}
        normal = make_display_icon(SimpleNamespace(), "C", performance)
        alert = make_display_icon(SimpleNamespace(), "C", performance, reset_hint=True)
        self.assertFalse(normal.tobytes() == alert.tobytes(), "TPS state lost the reset badge")
        self.assertEqual(alert.getpixel((10, 10))[:3], RESET_BLUE[:3])


if __name__ == "__main__":
    unittest.main()

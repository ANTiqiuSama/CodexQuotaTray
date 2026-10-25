import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reset_watch
from reset_watch import ResetOutlook, fetch_outlook, panel_line, parse_codex_reset, parse_willcodexreset, parse_willreset

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
    def test_panel_probability_is_visible_even_below_alert_threshold(self):
        now = time.time()
        outlook = ResetOutlook("s", "p", 22, 39, fetched_at=now)
        self.assertEqual(reset_watch.panel_line(outlook, now), "Codex 重置 22%")
        self.assertIn("读取中", reset_watch.panel_line(None, now))
        self.assertEqual(reset_watch.panel_line(outlook, now + reset_watch.STALE_SECONDS + 1), "Codex 重置 —")

    def test_parsers(self):
        a = parse_willreset(WILLRESET)
        self.assertEqual((a.prob_24, a.prob_48, a.promise, a.last_reset_text), (71, 81, "Done", "Direct usage reset"))
        self.assertAlmostEqual(a.last_reset_at, 1792271541.0)
        b = parse_codex_reset(CODEX_RESET)
        self.assertEqual((b.prob_24, b.prob_48, b.promise), (45, 70, "We'll see"))
        c = parse_willcodexreset(RADAR)
        self.assertEqual((c.prob_24, c.last_reset_id, c.last_reset_text), (12, "b", "Banked reset"))

    def test_every_source_counts_and_failures_are_reported(self):
        def get(url):
            if "willreset.com" in url:
                raise OSError("offline")
            return CODEX_RESET if "codex-reset" in url else RADAR
        outlook, errors = fetch_outlook(get, now=100.0)
        self.assertEqual(outlook.source, "2 个来源")
        self.assertEqual((outlook.prob_24, outlook.prob_48), (round((45 + 12) / 2), 50))
        self.assertEqual([r["source"] for r in outlook.readings], ["codex-reset.com", "willcodexreset.com"])
        self.assertEqual(outlook.fetched_at, 100.0)
        self.assertEqual(len(errors), 1)
        self.assertEqual(panel_line(outlook, 100.0), "Codex 重置 28%")
        self.assertFalse(reset_watch.announced(outlook, 100.0))
        single, _ = fetch_outlook(lambda url: CODEX_RESET if "codex-reset" in url else {}, now=100.0)
        self.assertEqual((single.source, single.prob_24), ("codex-reset.com", 45))
        none, errors = fetch_outlook(lambda url: {}, now=100.0)
        self.assertIsNone(none)
        self.assertEqual(len(errors), 3)

    def test_official_promise_raises_the_forecast_until_a_reset_follows(self):
        promised = dict(CODEX_RESET, last_reset_at="2026-10-20T06:33:48.000Z",
                        probabilities={"rounded_24h": 22, "rounded_48h": 39, "signal_percent": 93},
                        official_signal={"at": "2026-10-22T06:55:22.000Z", "summary": "it's reset day today\nmore",
                                         "score": {"band": "promise"}})
        parsed = parse_codex_reset(promised)
        self.assertEqual((parsed.prob_24, parsed.signal_percent), (22, 93))
        self.assertIn("reset day", parsed.promise)
        soon = parsed.signal_at + 3600
        outlook, _ = fetch_outlook(lambda url: promised if "codex-reset" in url else {}, now=soon)
        self.assertEqual((outlook.prob_24, outlook.prob_48), (93, 93))
        self.assertEqual(panel_line(outlook, soon), "Codex 重置 93%")
        self.assertTrue(reset_watch.announced(outlook, soon))
        self.assertEqual(reset_watch.effective(parsed, None, parsed.signal_at + 2 * 86400)[:2], (22, 39))   # a day later
        self.assertEqual(reset_watch.effective(parsed, parsed.signal_at + 60, soon)[:2], (22, 39))         # reset happened

    def test_levels_and_staleness(self):
        now = time.time()
        make = lambda p24, p48, at=now: ResetOutlook("s", "p", p24, p48, fetched_at=at)
        self.assertEqual(make(71, 81).level(now), "high")
        self.assertEqual(make(40, 70).level(now), "watch")
        self.assertEqual(make(10, 20).level(now), "low")
        self.assertEqual(make(90, 95, now - 4 * 3600).level(now), "unknown")

    def test_panel_line_shows_probabilities_or_state(self):
        now = time.time()
        high = ResetOutlook("willreset.com", "p", 71, 81, now - 3600, "x", "Direct usage reset", "Done", now)
        self.assertEqual(panel_line(high, now), "Codex 重置 71%")
        self.assertEqual(panel_line(ResetOutlook("s", "p", 20, None, fetched_at=now), now), "Codex 重置 20%")
        self.assertIn("读取中", panel_line(None, now))
        self.assertEqual(panel_line(ResetOutlook("s", "p", 20, 30, fetched_at=now - 5 * 3600), now), "Codex 重置 —")


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
        self.assertEqual(tray.notify.call_count, 1)   # the refresh itself announces the reset

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


class QuotaResetNoticeTests(unittest.TestCase):
    @staticmethod
    def snap(remaining, reset, second=None, second_reset=None, error=None, provider="grok"):
        from quota_sources import QuotaSnapshot
        return QuotaSnapshot(provider, provider, 10080 if provider == "grok" else 300, None if remaining is None else 100 - remaining,
                             remaining, reset, 1.0, False, error, 10080 if second is not None else None,
                             None if second is None else 100 - second, second, second_reset)

    def test_reset_lines(self):
        from tray_app import reset_changes
        self.assertEqual(reset_changes(self.snap(0, 1000), self.snap(100, 605800), 1010), ["每周窗口：0% → 100%"])
        self.assertEqual(reset_changes(self.snap(3, 5000), self.snap(100, 23000), 1000), ["每周窗口：3% → 100%（提前重置）"])
        self.assertEqual(reset_changes(self.snap(40, 1000), self.snap(45, 1000), 900), [])       # small gain
        self.assertEqual(reset_changes(self.snap(0, 1000), self.snap(None, None, error="x"), 1010), [])
        codex = reset_changes(self.snap(5, 1000, 60, 9000, provider="codex"), self.snap(100, 19000, 62, 9000, provider="codex"), 1010)
        self.assertEqual(codex, ["5 小时窗口：5% → 100%"])

    def test_announce_only_for_selected_providers(self):
        import tray_app
        tray = tray_app.QuotaTray.__new__(tray_app.QuotaTray)
        tray.notify = Mock()
        tray.reset_outlook = None
        tray.announce_resets("grok", self.snap(0, 1000), self.snap(100, 605800), 1010)
        tray.notify.assert_called_once()
        self.assertEqual(tray.notify.call_args[0][1], "Grok 额度已重置")
        tray.notify.reset_mock()
        tray.announce_resets("gemini", self.snap(0, 1000), self.snap(100, 605800), 1010)
        tray.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()

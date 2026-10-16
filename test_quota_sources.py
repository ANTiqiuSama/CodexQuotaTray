import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cli_usage
import quota_sources
from cli_usage import parse_agy_usage_json, parse_cli_percent, parse_grok_usage, parse_reset, run_temporary_usage


class QuotaTests(unittest.TestCase):
    def sample(self, remaining=40.0, stale=False, error=None):
        return quota_sources.QuotaSnapshot(
            "sample", "Sample", 300, 100 - remaining, remaining, int(time.time()) + 100,
            time.time(), stale, error,
        )

    def test_unknown_does_not_invent_values(self):
        value = quota_sources._unknown("sample", "Sample", 300, "not available")
        self.assertIsNone(value.used_percent)
        self.assertIsNone(value.remaining_percent)
        self.assertIn("not available", value.error)

    def test_gemini_label_and_window(self):
        value = quota_sources.get_gemini_quota()
        self.assertEqual(value.window_minutes, 300)
        self.assertIn("5 小时", value.label)

    def test_failure_preserves_old_value_and_marks_stale(self):
        old = self.sample(42)
        original_codex = quota_sources.get_codex_quota
        quota_sources.get_codex_quota = lambda: quota_sources._unknown(
            "codex", "Codex", None, "read failed"
        )
        try:
            result = quota_sources.collect_all({"codex": old})["codex"]
        finally:
            quota_sources.get_codex_quota = original_codex
        self.assertEqual(result.remaining_percent, 42)
        self.assertTrue(result.stale)
        self.assertIsNotNone(result.error)

    def test_dump_snapshot_redacts_account_identity(self):
        text = quota_sources.dump_snapshot({"sample": self.sample()})
        self.assertIn("Sample", text)
        self.assertNotIn("accountId", text)
        self.assertNotIn("token", text.lower())

    def test_cli_usage_parser_reads_grok_percentage_and_reset(self):
        text = "Weekly limit: 56%\nNext reset: September 28, 11:53"
        parsed = parse_grok_usage(text)
        self.assertEqual(parsed["used_percent"], 56.0)
        self.assertEqual(parsed["remaining_percent"], 44.0)
        self.assertIsNotNone(parsed["resets_at"])

    def test_grok_waits_for_slow_session_and_retries_unavailable_usage(self):
        clock = [0.0]
        queries = []
        terminal = Mock()
        terminal.spawn.return_value = True
        terminal.isalive.return_value = True
        terminal.pid = 123

        def read(blocking=False):
            self.assertFalse(blocking)
            if queries and queries[-1] >= 6:
                return "Weekly limit: 17%\nNext reset: October 5, 11:53"
            if queries:
                return "Session usage is unavailable until the session starts."
            return "Signing in… starting your session."

        def write(text):
            if text == "/usage":
                queries.append(clock[0])

        terminal.read.side_effect = read
        terminal.write.side_effect = write
        with patch.object(cli_usage, "_create_terminal", return_value=(terminal, False)), patch.object(
            cli_usage.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(cli_usage.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), patch.object(cli_usage.os, "kill") as kill:
            text = run_temporary_usage("grok.exe", ["--minimal"], timeout=10)
        self.assertEqual(len(queries), 2)
        self.assertGreaterEqual(queries[0], 4)
        self.assertEqual(parse_grok_usage(text)["remaining_percent"], 83)
        kill.assert_called_once_with(123, cli_usage.signal.SIGTERM)

    def test_grok_timeout_does_not_use_context_percent_or_leak_terminal_output(self):
        clock = [0.0]
        terminal = Mock()
        terminal.spawn.return_value = True
        terminal.isalive.return_value = True
        terminal.pid = 123
        terminal.read.return_value = "minimal · /help 2.2K /256K (1%) identity-must-stay-private"
        with patch.object(cli_usage, "_create_terminal", return_value=(terminal, False)), patch.object(
            cli_usage.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(cli_usage.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), patch.object(cli_usage.os, "kill") as kill:
            with self.assertRaisesRegex(TimeoutError, "Weekly limit") as caught:
                run_temporary_usage("grok.exe", ["--minimal"], timeout=1)
        self.assertNotIn("identity", str(caught.exception))
        kill.assert_called_once_with(123, cli_usage.signal.SIGTERM)

    def test_grok_initializes_hidden_console_only_when_standard_handles_are_missing(self):
        kernel32 = Mock()
        kernel32.GetStdHandle.return_value = None
        kernel32.GetConsoleWindow.return_value = 456
        kernel32.AllocConsole.return_value = 1
        user32 = Mock()
        with patch.object(cli_usage.ctypes.windll, "kernel32", kernel32), patch.object(
            cli_usage.ctypes.windll, "user32", user32
        ), patch.object(cli_usage, "PTY", return_value=Mock()):
            _, owned = cli_usage._create_terminal()
            self.assertTrue(owned)
            kernel32.AllocConsole.assert_called_once()
            user32.ShowWindow.assert_called_once_with(456, 0)
            kernel32.reset_mock()
            kernel32.GetStdHandle.return_value = 123
            _, owned = cli_usage._create_terminal()
            self.assertFalse(owned)
            kernel32.AllocConsole.assert_not_called()

    def test_grok_ignores_session_context_and_handles_terminal_escape_sequences(self):
        text = "2.2K /256K (1%)\nWeekly \x1b[32mlimit:\x1b[0m 17%\nNext reset: October 5, 11:53"
        self.assertEqual(parse_grok_usage(text)["remaining_percent"], 83)
        with self.assertRaises(ValueError):
            parse_grok_usage("2.2K /256K (1%)")

    def test_cli_usage_parser_handles_gemini_five_hour_label(self):
        text = "5-hour limit: 25% used\nNext reset: 2026-09-23 14:00"
        parsed = parse_cli_percent(text, ("5-hour limit", "used"), 300)
        self.assertEqual(parsed["remaining_percent"], 75.0)
        self.assertEqual(parse_reset(text), parsed["resets_at"])

    def test_agy_usage_json_reads_gemini_five_hour_bucket(self):
        payload = {
            "status": "SUCCESS",
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"id": "gemini-weekly", "remaining_fraction": 0.88},
                                {
                                    "id": "gemini-5h",
                                    "remaining_fraction": 0.625,
                                    "reset_time": "2026-09-23T06:00:00Z",
                                },
                            ],
                        }
                    ]
                }
            },
        }
        parsed = parse_agy_usage_json(payload)
        self.assertEqual(parsed["window_minutes"], 300)
        self.assertEqual(parsed["remaining_percent"], 62.5)
        self.assertEqual(parsed["used_percent"], 37.5)
        self.assertEqual(parsed["resets_at"], 1790143200)

    def test_cached_snapshots_are_marked_stale(self):
        original = quota_sources.CACHE_PATH
        temporary = Path(original).with_name("state-test.json")
        quota_sources.CACHE_PATH = temporary
        try:
            quota_sources.save_cached_snapshots({"sample": self.sample(42)})
            loaded = quota_sources.load_cached_snapshots()["sample"]
            self.assertEqual(loaded.remaining_percent, 42)
            self.assertTrue(loaded.stale)
        finally:
            quota_sources.CACHE_PATH = original
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

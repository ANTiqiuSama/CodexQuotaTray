import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cli_usage
import cli_quota
import json
import subprocess
import tempfile
import quota_sources
from cli_usage import parse_agy_usage_json, parse_cli_percent, parse_grok_usage, parse_reset, run_temporary_usage


class GrokIsolationTests(unittest.TestCase):
    def test_native_startup_hang_is_bounded_and_preserves_last_good_quota(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("worker", 35)
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"LOCALAPPDATA": folder}), \
                patch.object(cli_quota, "no_quota_reason") as claude_status, \
                patch.object(cli_quota.subprocess, "Popen", return_value=process), \
                patch.object(cli_quota, "stop_worker") as stop:
            snapshot = quota_sources.get_grok_quota()
            self.assertIn("35 秒", snapshot.error)
            self.assertIn("隐藏终端初始化", snapshot.error)
            process.wait.assert_called_once_with(timeout=35)
            stop.assert_called_once_with(process)
            claude_status.assert_not_called()
            old = quota_sources.QuotaSnapshot("grok", "Grok", 10080, 30, 70, 2_000_000, 1_000_000, False, None)
            kept = quota_sources.keep_last_good(snapshot, old)
            self.assertEqual(kept.remaining_percent, 70)
            self.assertTrue(kept.stale)

    def test_repeated_frozen_reads_get_fresh_workers_and_transfer_cli_reset(self):
        commands = []
        def start(command, **kwargs):
            commands.append(command)
            self.assertEqual(command[1], "--grok-quota-worker")
            Path(command[-1]).write_text(json.dumps({"quota": {"remaining_percent": 70},
                "cli": {"next_reset": "Next reset: October 27, 11:02"}}), encoding="utf-8")
            return Mock()
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"LOCALAPPDATA": folder}), \
                patch.object(cli_quota.sys, "frozen", True, create=True), \
                patch.object(cli_quota.subprocess, "Popen", side_effect=start):
            for _ in range(2):
                raw = {}
                self.assertEqual(cli_quota.read_usage("grok.exe", "grok", raw)["remaining_percent"], 70)
                self.assertEqual(raw["next_reset"], "Next reset: October 27, 11:02")
            self.assertNotEqual(commands[0][-1], commands[1][-1])
            self.assertEqual(list((Path(folder) / "CodexQuotaTray/work").iterdir()), [])

    def test_grok_worker_exports_only_quota_lines_without_identity(self):
        text = "private@example.test\nWeekly limit: 30%\nNext reset: October 27, 11:02\nSECRET-TOKEN"
        with tempfile.TemporaryDirectory() as folder, patch.object(cli_quota, "run_temporary_usage", return_value=text):
            destination = Path(folder) / "result.json"
            self.assertEqual(cli_quota.worker("grok.exe", str(destination), "grok"), 0)
            result = destination.read_text(encoding="utf-8")
            self.assertNotIn("private@", result)
            self.assertNotIn("SECRET-TOKEN", result)
            parsed = json.loads(result)
            self.assertEqual(parsed["quota"]["remaining_percent"], 70)
            self.assertEqual(parsed["cli"]["next_reset"], "Next reset: October 27, 11:02")

    def test_timeout_cleanup_targets_only_the_owned_worker_tree(self):
        process = Mock(pid=1234)
        children = [Mock(), Mock()]
        with patch.object(cli_quota.psutil, "Process") as lookup:
            lookup.return_value.children.return_value = children
            cli_quota.stop_worker(process)
        lookup.assert_called_once_with(1234)
        lookup.return_value.children.assert_called_once_with(recursive=True)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        for child in children:
            child.kill.assert_called_once_with()


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

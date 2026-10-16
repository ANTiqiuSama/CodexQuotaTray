"""Claude readiness: registry, /usage parsing and probe, transcript reader."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import cli_usage
import providers
import quota_sources
from cli_usage import CLAUDE_PROBE, claude_usage_complete, parse_claude_reset, parse_claude_usage, run_temporary_usage
from icon_art import ACCENTS, ROSE, accent, quota_icon
from refresh_scheduler import IDLE, SETTLE, RefreshScheduler
from tps_sources import TpsMonitor

USAGE = (
    "\x1b[2m Settings:  Status   Config   \x1b[1mUsage\x1b[0m  (tab to cycle)\n\n"
    " Current session\n \x1b[36m█████████▌\x1b[0m                         19% used\n Resets 5pm (Asia/Shanghai)\n\n"
    " Current week (all models)\n ███▌                                7% used\n Resets Oct 23, 3:30pm (Asia/Shanghai)\n\n"
    " Current week (Opus)\n                                     0% used\n"
)
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)  # 14:00 in Shanghai


class RegistryTests(unittest.TestCase):
    def test_explicit_list_keeps_registry_order(self):
        self.assertEqual(providers.enabled_keys("claude, codex"), ["codex", "claude"])
        self.assertEqual(providers.enabled_keys("grok;nope"), ["grok"])

    def test_claude_is_shown_only_when_installed(self):
        with patch.object(providers, "_claude_installed", return_value=False):
            spec = providers.ProviderSpec(**{**providers.get("claude").__dict__, "detect": providers._claude_installed})
            self.assertFalse(spec.available())
        with tempfile.TemporaryDirectory() as home, patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": home, "CODEX_QUOTA_CLAUDE_EXE": ""}):
            with patch.object(providers.shutil, "which", return_value=None), patch.object(providers.Path, "home", return_value=Path(home)):
                self.assertNotIn("claude", providers.enabled_keys(""))
                (Path(home) / "projects").mkdir()
                self.assertIn("claude", providers.enabled_keys(""))

    def test_everything_derives_from_registry(self):
        self.assertEqual(quota_sources.LABELS["claude"], ("Claude", 300))
        self.assertEqual(IDLE["claude"], IDLE["grok"])
        self.assertEqual(SETTLE["claude"], 20)
        self.assertEqual(accent("A"), providers.get("claude").accent)
        self.assertEqual(set(ACCENTS), {"C", "G", "M", "A"})
        # Danger is no longer Claude's clay, so a low Claude ring stays distinct.
        self.assertNotEqual(ROSE, accent("A"))
        self.assertEqual(quota_icon(50, "A").size, (64, 64))
        scheduler = RefreshScheduler(["codex", "claude"], 0)
        self.assertEqual(scheduler.take_due(0), ["codex", "claude"])


class UsageParsingTests(unittest.TestCase):
    def test_session_and_week_windows(self):
        with patch.object(cli_usage, "datetime", wraps=datetime) as fake:
            fake.now.side_effect = lambda tz=None: NOW
            result = parse_claude_usage(USAGE)
        self.assertEqual((result["used_percent"], result["remaining_percent"], result["window_minutes"]), (19, 81, 300))
        self.assertEqual((result["secondary_used_percent"], result["secondary_window_minutes"]), (7, 10080))
        self.assertEqual(result["resets_at"], int(datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(result["secondary_resets_at"], int(datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc).timestamp()))
        self.assertTrue(claude_usage_complete(USAGE))
        self.assertFalse(claude_usage_complete(USAGE.split("Current week")[0]))

    def test_reset_formats(self):
        stamp = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp())
        self.assertEqual(parse_claude_reset("5pm (UTC)", NOW), stamp(2026, 10, 16, 17))
        self.assertEqual(parse_claude_reset("3am (UTC)", NOW), stamp(2026, 10, 17, 3))      # already past -> tomorrow
        self.assertEqual(parse_claude_reset("12am (UTC)", NOW), stamp(2026, 10, 17, 0))
        self.assertEqual(parse_claude_reset("Oct 23 at 9:15am (UTC)", NOW), stamp(2026, 10, 23, 9, 15))
        self.assertEqual(parse_claude_reset("Thu, Oct 23, 3pm (UTC)", NOW), stamp(2026, 10, 23, 15))
        self.assertEqual(parse_claude_reset("Jan 2, 1pm (UTC)", NOW), stamp(2027, 1, 2, 13))
        self.assertIsNone(parse_claude_reset("soon", NOW))

    def test_percent_left_and_missing_week(self):
        result = parse_claude_usage("Current session\n 60% left\n")
        self.assertEqual(result["used_percent"], 40)
        self.assertNotIn("secondary_used_percent", result)

    def test_api_key_accounts_get_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "API Key"):
            parse_claude_usage("Total cost: $0.12\nUsage by model: ...")

    def test_snapshot_carries_both_windows(self):
        with patch.object(providers, "find_claude", return_value=Path("claude.exe")), \
                patch.object(quota_sources, "run_temporary_usage", return_value=USAGE):
            snapshot = quota_sources.get_claude_quota()
        self.assertIsNone(snapshot.error)
        self.assertEqual((snapshot.remaining_percent, snapshot.secondary_remaining_percent, snapshot.display_remaining), (81, 93, 81))

    def test_missing_cli_is_reported_not_raised(self):
        with patch.object(providers, "find_claude", return_value=None):
            snapshot = quota_sources.get_claude_quota()
        self.assertIn("CODEX_QUOTA_CLAUDE_EXE", snapshot.error)


class ProbeTests(unittest.TestCase):
    def run_probe(self, screens, executable="claude.exe"):
        clock, writes = [0.0], []
        terminal = Mock(pid=321)
        terminal.spawn.return_value = True
        terminal.isalive.return_value = True
        terminal.read.side_effect = lambda blocking=False: screens(writes, clock[0])
        terminal.write.side_effect = writes.append
        with tempfile.TemporaryDirectory() as data, patch.dict("os.environ", {"LOCALAPPDATA": data}), \
                patch.object(cli_usage, "PTY", object()), \
                patch.object(cli_usage, "_create_terminal", return_value=(terminal, False)), \
                patch.object(cli_usage.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(cli_usage.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                patch.object(cli_usage.os, "kill"):
            text = run_temporary_usage(executable, ["--strict-mcp-config"], timeout=20, probe=CLAUDE_PROBE)
            return text, writes, terminal

    def test_trust_prompt_is_answered_then_usage_read_in_probe_folder(self):
        def screens(writes, now):
            if "/usage" in writes:
                return USAGE
            if "\r" in writes:
                return "? for shortcuts"
            return "Do you trust the files in this folder?" if now > 0.5 else ""
        text, writes, terminal = self.run_probe(screens)
        self.assertTrue(claude_usage_complete(text))
        self.assertEqual(writes[0], "\r")          # trust answered once, before /usage
        self.assertEqual(writes.count("/usage"), 1)
        cwd = terminal.spawn.call_args[0][2]
        self.assertTrue(cwd.endswith("claude-probe"))

    def test_npm_shim_runs_through_cmd(self):
        _, _, terminal = self.run_probe(lambda writes, now: USAGE if "/usage" in writes else "? for shortcuts", "C:/npm/claude.cmd")
        app, cmdline = terminal.spawn.call_args[0][:2]
        self.assertTrue(app.lower().endswith("cmd.exe"))
        self.assertIn("claude.cmd", cmdline)


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.folder = self.home / ".claude/projects/C--work-demo"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "sess-1.jsonl"
        self.path.write_text("")
        self.monitor = TpsMonitor(home=self.home)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, *records):
        with self.path.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    @staticmethod
    def assistant(mid, output, moment, stop=None, model="claude-sonnet-4-5"):
        iso = datetime.fromtimestamp(moment, timezone.utc).isoformat().replace("+00:00", "Z")
        return {"type": "assistant", "timestamp": iso, "sessionId": "sess-1",
                "message": {"id": mid, "model": model, "stop_reason": stop, "content": [{"type": "text", "text": "SECRET"}],
                            "usage": {"input_tokens": 900, "cache_read_input_tokens": 5000, "output_tokens": output}}}

    def test_streamed_blocks_count_once_with_final_usage(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_1", 12, now - 4), self.assistant("msg_1", 300, now - 2))
        snapshot = self.monitor.poll(now)
        claude = snapshot["providers"]["claude"]
        self.assertEqual(claude["active_sessions"], 1)           # still pending: generating
        self.assertEqual(claude["output_tokens_60"], 0)
        self.write(self.assistant("msg_1", 420, now - 1, stop="end_turn"))
        claude = self.monitor.poll(now)["providers"]["claude"]
        self.assertEqual(claude["output_tokens_60"], 420)         # not 12+300+420, no input/cache
        self.assertIsNone(claude["generation_tps"])                # no native TPS -> none invented
        self.assertNotIn("SECRET", json.dumps(self.monitor.snapshot(now), ensure_ascii=False))

    def test_next_record_finalises_previous_message_and_sidechains_are_read(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_a", 50, now - 10, stop="tool_use"),
                   {"type": "user", "timestamp": datetime.fromtimestamp(now - 8, timezone.utc).isoformat(), "message": {"content": []}})
        sub = self.folder / "sess-1/subagents/agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps(self.assistant("msg_b", 70, now - 5, stop="end_turn")) + "\n")
        self.monitor.last_discovery = 0
        self.monitor.last_slow_discovery = 0
        self.monitor.poll(now)
        claude = self.monitor.poll(now + 0.1)["providers"]["claude"]
        self.assertEqual(claude["output_tokens_60"], 120)
        self.assertEqual(self.monitor.health["Claude Code"]["status"], "已接入")

    def test_quiet_unfinished_message_is_flushed_and_activity_cleared(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_x", 80, now - 1))
        self.monitor.poll(now)
        later = now + 100
        claude = self.monitor.poll(later)["providers"]["claude"]
        self.assertEqual(claude["active_sessions"], 0)
        self.assertEqual(sum(e.output_tokens for e in self.monitor.events if e.provider == "claude"), 80)

    def test_synthetic_messages_are_ignored(self):
        now = time.time()
        self.monitor.poll(now)
        self.write(self.assistant("msg_s", 0, now - 1, stop="end_turn", model="<synthetic>"))
        self.assertEqual(self.monitor.poll(now)["providers"]["claude"]["output_tokens_60"], 0)


if __name__ == "__main__":
    unittest.main()

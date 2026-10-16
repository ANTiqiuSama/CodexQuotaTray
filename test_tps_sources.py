import json
import sqlite3
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tps_sources import JsonlReader, Observation, TpsMonitor, key, logged_model, proto_fields


def vint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def pv(tag, value):
    return vint(tag * 8) + vint(value)


def pb(tag, value):
    return vint(tag * 8 + 2) + vint(len(value)) + value


def stamp(value):
    whole = int(value)
    return pv(1, whole) + pv(2, round((value - whole) * 1e9))


class TpsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(__file__).parent / "work/tps-tests" / str(uuid.uuid4())
        self.home.mkdir(parents=True)
        self.now = time.time()
        self.monitor = TpsMonitor(home=self.home)
        self.monitor.started_at = self.now
        self.monitor.context["s"] = {"model": "gpt-6.1-sol", "source": "Codex"}

    def event(self, response="r", output=120, when=None, thread="s"):
        return {"timestamp": self.now - 1 if when is None else when, "type": "token_usage_record", "payload": {"thread_id": thread, "response_id": response, "usage": {"output_tokens": output, "input_tokens": 99999, "cached_input_tokens": 99999, "reasoning_output_tokens": 100}}}

    def test_counts_output_once_and_does_not_add_input_cache_or_reasoning(self):
        event = self.event()
        self.monitor._codex_record(event, "s", self.now)
        self.monitor._codex_record(event, "s", self.now)
        self.monitor._codex_record({"timestamp": self.now, "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"output_tokens": 120}, "total_token_usage": {"output_tokens": 120}}}}, "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["output_tokens_60"], 120)
        self.assertEqual(data["output_tps_60"], 2)

    def test_fork_replay_is_not_new_generation(self):
        self.monitor.context["fork"] = {"model": "grok-4.7", "source": "Codex"}
        self.monitor._codex_record(self.event(thread="s"), "fork", self.now)
        self.assertFalse(self.monitor.events)

    def test_request_duration_is_not_presented_as_client_native_tps(self):
        self.monitor._codex_record({"timestamp":self.now-11,"type":"event_msg","payload":{"type":"task_started"}}, "s", self.now)
        self.monitor._codex_record(self.event(output=100), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["basis"], ["客户端暂未提供可核验TPS"])

    def test_missing_duration_stays_unknown_and_sixty_second_rate_remains_valid(self):
        self.monitor._codex_record(self.event(), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["display_basis"], "客户端暂未取得TPS")
        self.assertIsNone(data["display_tps"])

    def test_old_completion_does_not_clear_new_inflight_request(self):
        self.monitor.activity["s"] = ("codex", self.now, "请求中")
        self.monitor._codex_record(self.event(when=self.now - 10), "s", self.now)
        self.monitor._codex_record({"timestamp": self.now - 5, "type": "event_msg", "payload": {"type": "task_complete"}}, "s", self.now)
        self.assertIn("s", self.monitor.activity)

    def test_finished_generation_recovers_idle_after_three_seconds(self):
        self.monitor.activity["s"] = ("codex", self.now - 1, "刚完成生成")
        self.monitor.last_discovery = self.now
        self.assertEqual(self.monitor.poll(self.now + 1)["providers"]["codex"]["active_sessions"], 1)
        self.assertEqual(self.monitor.poll(self.now + 4)["providers"]["codex"]["active_sessions"], 0)

    def test_window_counts_all_sessions_and_expires_without_interpolating(self):
        self.monitor.add(Observation("1", "Codex", "a", "codex", "gpt", self.now - 59, 60), self.now)
        self.monitor.add(Observation("2", "Codex", "b", "codex", "gpt", self.now - 1, 120), self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["total_output_tps_60"], 3)
        self.assertEqual(self.monitor.snapshot(self.now + 3)["total_output_tps_60"], 2)

    def test_grok_elapsed_time_does_not_replace_native_tps(self):
        record = {"timestamp": self.now - 1, "params": {"update": {"sessionUpdate": "turn_completed", "prompt_id": "p", "elapsed_ms": 10000, "usage": {"modelUsage": {"grok-4.7": {"outputTokens": 200, "reasoningTokens": 100, "apiDurationMs": 2000}}}}}}
        self.monitor._grok_record(record, "g", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["grok"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["output_tokens_60"], 200)

    def test_grok_future_turn_completion_does_not_double_count_counter(self):
        folder = self.home / "grok/g"
        folder.mkdir(parents=True)
        path = folder / "updates.jsonl"
        path.write_text("", encoding="utf-8")
        usage = {"updatedAt": self.now, "session": {"modelUsage": {"grok-4.7": {"outputTokens": 1000}}}}
        folder.joinpath("usage.json").write_text(json.dumps(usage))
        self.monitor._grok_usage_counter(path, self.now)
        usage["updatedAt"] = self.now + 1
        usage["session"]["modelUsage"]["grok-4.7"]["outputTokens"] = 1100
        folder.joinpath("usage.json").write_text(json.dumps(usage))
        self.monitor._grok_usage_counter(path, self.now + 1)
        record = {"timestamp": self.now + 1, "params": {"update": {"sessionUpdate": "turn_completed", "prompt_id": "p", "usage": {"modelUsage": {"grok-4.7": {"outputTokens": 100, "apiDurationMs": 1000}}}}}}
        self.monitor._grok_record(record, "g", self.now + 1)
        self.assertEqual(self.monitor.snapshot(self.now + 1)["providers"]["grok"]["output_tokens_60"], 100)

    def test_grok_first_token_detects_activity_without_waiting_for_turn_end(self):
        self.monitor._grok_activity({"type": "first_token", "ts": self.now}, "g", self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["grok"]["active_sessions"], 1)

    def test_isolated_gemini_bridge_copy_is_not_counted_again(self):
        self.monitor.add(Observation("native", "Antigravity CLI · Codex隔离配置", "a", "gemini", "gemini", self.now - 2, 200, 2, "流式"), self.now)
        self.monitor.add(Observation("copy", "Codex · Gemini桥接回传", "b", "gemini", "gemini", self.now - 1, 200, 4, "API平均"), self.now)
        data = self.monitor.snapshot(self.now)["providers"]["gemini"]
        self.assertEqual(data["output_tokens_60"], 200)
        self.assertIsNone(data["generation_tps"])

    def make_agy_db(self, corrupt_total=False):
        folder = self.home / ".gemini/antigravity-cli/conversations"
        folder.mkdir(parents=True)
        path = folder / "a.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY,data BLOB)")
        db.execute("CREATE TABLE steps(idx INTEGER,status INTEGER,metadata BLOB)")
        usage = pv(3, 51 if corrupt_total else 50) + pv(9, 40) + pv(10, 10) + pb(11, b"response-a")
        model = pb(4, usage) + pb(11, stamp(20)) + pb(12, stamp(2)) + pb(19, b"gemini-3.8-flash")
        db.execute("INSERT INTO gen_metadata VALUES(0,?)", (pb(1, model),))
        metadata = pb(1, stamp(self.now - 3)) + pb(7, stamp(self.now - 1)) + pb(9, pb(11, b"response-a"))
        db.execute("INSERT INTO steps VALUES(0,3,?)", (metadata,))
        db.commit()
        db.close()
        return path

    def test_antigravity_preserves_counts_without_estimating_tps(self):
        self.make_agy_db()
        self.monitor._antigravity(self.now)
        data = self.monitor.snapshot(self.now)["providers"]["gemini"]
        self.assertIsNone(data["generation_tps"])
        self.assertEqual(data["output_tokens_60"], 50)

    def test_client_native_tps_is_copied_exactly_and_latest_value_wins(self):
        self.monitor.add(Observation("native-1", "Grok CLI 原生统计", "g", "grok", "grok", self.now - 2, 200, None, "原生", False, 110.4), self.now)
        self.monitor.add(Observation("native-2", "Grok CLI 原生统计", "g", "grok", "grok", self.now - 1, 250, None, "原生", False, 124.4), self.now)
        data = self.monitor.snapshot(self.now)["providers"]["grok"]
        self.assertEqual(data["generation_tps"], 124.4)
        self.assertEqual(data["output_tokens_60"], 0)

    def test_codex_stream_uses_real_output_count_and_client_item_time(self):
        self.monitor.client_stream_starts["s"] = [self.now - 3, self.now - 2, self.now - 1]
        self.monitor._codex_record(self.event(output=120, when=self.now), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["generation_tps"], 40)
        self.assertIn("真实输出token", data["basis"][0])

    def test_codex_total_output_includes_tool_arguments_and_is_labelled(self):
        self.monitor.client_stream_starts["s"] = [self.now - 3, self.now - 1]
        self.monitor._codex_record({"timestamp":self.now-1,"type":"response_item","payload":{"type":"custom_tool_call","input":"PRIVATE_CANARY"}}, "s", self.now)
        self.monitor._codex_record(self.event(output=120, when=self.now), "s", self.now)
        data = self.monitor.snapshot(self.now)["providers"]["codex"]
        self.assertEqual(data["generation_tps"], 40)
        self.assertIn("工具调用输出", data["basis"][0])

    def test_codex_client_logs_keep_concurrent_sessions_separate(self):
        self.monitor.codex_root.mkdir(parents=True)
        db = sqlite3.connect(self.monitor.codex_root / "logs_2.sqlite")
        db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,feedback_log_body TEXT,thread_id TEXT)")
        for i, (session, age) in enumerate((("s", 3), ("other", 1), (None, 5)), 1):
            moment = self.now - age
            db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?)", (i, int(moment), round((moment-int(moment))*1e9), "codex_core::stream_events_utils", 'Output item item_type="reasoning" item_id="PRIVATE_ID"', session))
        db.commit()
        db.close()
        self.monitor._codex_logs(self.now)
        self.assertEqual(set(self.monitor.client_stream_starts), {"s", "other"})
        self.monitor._codex_record(self.event(when=self.now), "s", self.now)
        self.assertAlmostEqual(self.monitor.snapshot(self.now)["providers"]["codex"]["generation_tps"], 40)

    def test_stream_span_model_keeps_unlisted_gpt_session_on_codex_icon(self):
        self.monitor.codex_root.mkdir(parents=True)
        db = sqlite3.connect(self.monitor.codex_root / "logs_2.sqlite")
        db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,feedback_log_body TEXT,thread_id TEXT)")
        moment = self.now - 1
        body = 'turn{model=gpt-6-luna}: Output item item_type="reasoning" item_id="item"'
        db.execute(
            "INSERT INTO logs VALUES(?,?,?,?,?,?)",
            (1, int(moment), 0, "codex_core::stream_events_utils", body, "missing-thread"),
        )
        db.commit()
        db.close()
        self.assertEqual(logged_model(body), "gpt-6-luna")
        self.monitor._codex_logs(self.now)
        self.assertEqual(self.monitor.activity["missing-thread"][0], "codex")
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["codex"]["active_sessions"], 1)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["other"]["active_sessions"], 0)

    def test_codex_next_response_excludes_prior_stream_start(self):
        self.monitor.completed_at["s"] = self.now - 2
        self.monitor.client_stream_starts["s"] = [self.now - 10, self.now - 1]
        self.monitor._codex_record(self.event(when=self.now), "s", self.now)
        self.assertEqual(self.monitor.snapshot(self.now)["providers"]["codex"]["generation_tps"], 120)

    def test_antigravity_mismatched_proto_totals_are_skipped(self):
        self.make_agy_db(corrupt_total=True)
        self.monitor._antigravity(self.now)
        self.assertFalse(self.monitor.events)

    def test_malformed_proto_is_rejected(self):
        with self.assertRaises(ValueError):
            proto_fields(b"\x0a\xff\xff")

    def test_jsonl_partial_line_is_counted_once_when_finished(self):
        path = self.home / "a.jsonl"
        path.write_bytes(b'{"x":1}\n{"x":')
        reader = JsonlReader(path)
        self.assertEqual(list(reader.read()), [{"x": 1}])
        with path.open("ab") as output:
            output.write(b'2}\n')
        self.assertEqual(list(reader.read()), [{"x": 2}])
        self.assertFalse(list(reader.read()))

    def test_jsonl_idle_partial_record_does_not_reopen_and_resumes_on_append(self):
        path = self.home / "idle.jsonl"
        path.write_bytes(b'{"x":1}\n{"x":')
        reader = JsonlReader(path)
        self.assertEqual(list(reader.read()), [{"x": 1}])
        with patch.object(Path, "open", side_effect=AssertionError("idle file reopened")):
            self.assertEqual(list(reader.read()), [])
            self.assertEqual(list(reader.read()), [])
        with path.open("ab") as output:
            output.write(b'2}\n')
        self.assertEqual(list(reader.read()), [{"x": 2}])

    def test_jsonl_truncation_to_empty_discards_partial_before_append(self):
        path = self.home / "truncated.jsonl"
        path.write_bytes(b'{"old":1}\n{"old":')
        reader = JsonlReader(path)
        self.assertEqual(list(reader.read()), [{"old": 1}])
        path.write_bytes(b'')
        self.assertEqual(list(reader.read()), [])
        path.write_bytes(b'{"new":2}\n')
        self.assertEqual(list(reader.read()), [{"new": 2}])

    def test_grok_usage_json_is_parsed_only_after_it_changes(self):
        folder = self.home / "grok/u"
        folder.mkdir(parents=True)
        path = folder / "updates.jsonl"
        path.write_text("", encoding="utf-8")
        usage = folder / "usage.json"
        usage.write_text("{}")
        self.assertTrue(self.monitor._usage_changed(path))
        self.assertFalse(self.monitor._usage_changed(path))
        usage.write_text('{"session": {}}')
        self.assertTrue(self.monitor._usage_changed(path))

    def test_old_histories_are_not_registered_and_idle_readers_are_released(self):
        import os
        old = self.home / "old.jsonl"
        old.write_text("{}\n")
        os.utime(old, (self.now - 3 * 86400, self.now - 3 * 86400))
        self.assertFalse(self.monitor._recent(old, self.now))
        fresh = self.home / "fresh.jsonl"
        fresh.write_text("{}\n")
        self.assertTrue(self.monitor._recent(fresh, self.now))
        self.monitor._register(old, "old", "Codex")
        self.monitor._register(fresh, "fresh", "Codex")
        self.monitor.client_stream_starts["gone"] = [self.now - 3600]
        self.monitor._forget_idle(self.now)
        self.assertNotIn(old, self.monitor.readers)
        self.assertIn(fresh, self.monitor.readers)
        self.assertNotIn("gone", self.monitor.client_stream_starts)
        self.assertNotIn("old", self.monitor.context)
        self.assertIn("fresh", self.monitor.context)

    def test_process_scan_is_throttled(self):
        with patch("tps_sources.running_software", return_value=[]) as scan:
            self.monitor.discover(self.now)
            self.monitor.discover(self.now + 2)
            self.monitor.discover(self.now + 31)
        self.assertEqual(scan.call_count, 2)

    def test_snapshot_does_not_persist_private_message_or_request_id(self):
        value = self.event(response="private-response-id")
        value["payload"]["response"] = "PRIVATE_PROMPT_CANARY"
        self.monitor._codex_record(value, "s", self.now)
        output = json.dumps(self.monitor.snapshot(self.now))
        self.assertNotIn("PRIVATE_PROMPT_CANARY", output)
        self.assertNotIn("private-response-id", output)
        self.assertNotIn("PRIVATE_PROMPT_CANARY", key("PRIVATE_PROMPT_CANARY"))


if __name__ == "__main__":
    unittest.main()

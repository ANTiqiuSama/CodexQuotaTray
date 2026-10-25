import tempfile
import unittest
import urllib.error
from pathlib import Path

import agent_panel
import model_iq

PAYLOAD = {"success": True, "period": "24h", "data": [
    {"id": "1", "name": "claude-opus-4-7-20260101", "provider": "anthropic", "currentScore": 72, "trend": "down",
     "status": "warning", "confidenceLower": 68.1, "confidenceUpper": 75.9},
    {"id": "2", "name": "gpt-5.6-sol", "provider": "openai", "currentScore": 75, "trend": "up", "status": "good",
     "confidenceLower": 71.2, "confidenceUpper": 79.0},
    {"id": "3", "name": "gpt-5.6-mini", "provider": "openai", "currentScore": 61, "trend": "stable", "status": "good"},
    {"id": "4", "name": "gemini-3-pro-preview", "provider": "google", "currentScore": 70, "trend": "stable", "status": "good"},
    {"id": "5", "name": "claude-sonnet-4-5", "provider": "anthropic", "currentScore": 66, "status": "critical"},
]}
DAY = 1_790_000_000.0 - 1_790_000_000.0 % 86400 + 3600      # 01:00 UTC


class ModelIqTests(unittest.TestCase):
    def test_parse_sorts_best_first_and_names_are_compact(self):
        scores = model_iq.parse(PAYLOAD)
        self.assertEqual([s.name for s in scores][:2], ["gpt-5.6-sol", "claude-opus-4-7-20260101"])
        self.assertEqual([s.short for s in scores[:3]], ["GPT-5.6 Sol", "Opus 4.7", "Gemini 3 Pro"])
        self.assertEqual(model_iq.score_text(scores[0]), "75↑")
        self.assertEqual(model_iq.score_text(scores[1]), "72↓")
        self.assertEqual(model_iq.score_tone(scores[1]), "warn")
        self.assertEqual(model_iq.score_tone(next(s for s in scores if s.status == "critical")), "danger")
        with self.assertRaises(ValueError):
            model_iq.parse({"error": "api_key_required"})

    def test_default_is_the_three_best_models_and_choice_keeps_order(self):
        state = model_iq.IqState(models=[m.__dict__ for m in model_iq.parse(PAYLOAD)], fetched_at=DAY)
        self.assertEqual([m.name for m in model_iq.hover_models(state, None)],      # default: the top three
                         ["gpt-5.6-sol", "claude-opus-4-7-20260101", "gemini-3-pro-preview"])
        chosen = ["gemini-3-pro-preview", "gpt-5.6-mini", "missing-model", "claude-sonnet-4-5"]
        self.assertEqual([m.name for m in model_iq.hover_models(state, chosen)],
                         ["gemini-3-pro-preview", "gpt-5.6-mini", "claude-sonnet-4-5"])
        self.assertEqual(model_iq.hover_models(None, None), [])

    def test_daily_budget_is_kept_across_restarts_and_429_stops_for_the_day(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "iq.json"
            state = model_iq.load_state(path)
            self.assertTrue(model_iq.due(state, DAY))
            model_iq.read(state, "asl_live_test", DAY, get=lambda key: PAYLOAD)
            model_iq.save_state(path, state)
            state = model_iq.load_state(path)                       # restart: no extra call
            self.assertEqual(len(state.scores()), 5)
            self.assertFalse(model_iq.due(state, DAY + 600))
            self.assertTrue(model_iq.due(state, DAY + model_iq.READ_SECONDS))
            moment = DAY
            for _ in range(model_iq.AUTO_CALLS_PER_DAY - 1):
                moment += model_iq.READ_SECONDS
                model_iq.read(state, "k" * 10, moment, get=lambda key: PAYLOAD)
            self.assertEqual(model_iq.calls_today(state, moment), model_iq.AUTO_CALLS_PER_DAY)
            self.assertFalse(model_iq.due(state, moment + model_iq.READ_SECONDS - 1))  # automatic budget spent
            self.assertEqual(model_iq.remaining_today(state, moment), model_iq.DAILY_LIMIT - model_iq.AUTO_CALLS_PER_DAY)

            def limited(key):
                raise urllib.error.HTTPError(model_iq.API_URL, 429, "quota", {}, None)
            model_iq.read(state, "k" * 10, moment + 60, get=limited)
            self.assertEqual(model_iq.remaining_today(state, moment + 60), 0)
            self.assertIn("用完", state.error)
            self.assertEqual(len(state.scores()), 5)                # the last scores stay
            tomorrow = DAY + 86400
            self.assertTrue(model_iq.due(state, tomorrow))

    def test_a_failing_key_waits_the_full_interval(self):
        state = model_iq.IqState()

        def denied(key):
            raise urllib.error.HTTPError(model_iq.API_URL, 401, "no", {}, None)
        model_iq.read(state, "k" * 10, DAY, get=denied)
        self.assertIn("Key", state.error)
        self.assertFalse(model_iq.due(state, DAY + 600))
        self.assertEqual(model_iq.calls_today(state, DAY), 1)

    def test_config_round_trip_and_key_check(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cfg.json"
            self.assertEqual(model_iq.load_config(path), {"key": "", "shown": None})
            model_iq.save_config(path, " asl_live_abcdef123 ", ["a", "b", "c", "d"])
            self.assertEqual(model_iq.load_config(path), {"key": "asl_live_abcdef123", "shown": ["a", "b", "c"]})
            model_iq.save_config(path, "", None)
            self.assertIsNone(model_iq.load_config(path)["shown"])
            with self.assertRaises(ValueError):
                model_iq.save_config(path, "not a key!", None)

    def test_banner_shows_short_reset_chance_and_models_and_opens_the_iq_page(self):
        model = agent_panel.PanelModel("codex", "C", "GPT / Codex", "空闲", (("5 小时", 70, ""),), 0, (), "idle",
                                       "Codex 重置 44%", "", "unknown", intel=(("GPT-5.6 Sol", "75↑", "ok"), ("Opus 4.7", "72↓", "warn")))
        for alert in ("", "官方预告"):
            scene = agent_panel.Scene()
            scene.set_model(agent_panel.replace(model, banner_alert=alert), 0)
            self.assertIsNotNone(scene.render(1.5))
        note_only = agent_panel.replace(model, banner="", intel=(), intel_note="未设置")
        self.assertEqual(agent_panel.header_offset(note_only), 32)
        scene = agent_panel.Scene()
        scene.set_model(model, 0)
        x0, y0, x1, y1 = agent_panel.banner_box()
        point = ((x0 + x1) / 2, (y0 + y1) / 2)
        scene.step(0.1, point, True, False)
        events = scene.step(0.12, point, False, False)
        self.assertIn(("settings", "iq"), events)


if __name__ == "__main__":
    unittest.main()

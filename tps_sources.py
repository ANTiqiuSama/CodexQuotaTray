"""Passive, numeric-only readers for local AI generation records.

Antigravity's numeric wire fields follow the independently verified Tokscale
database layout. No credential files, model prompts, or response text are saved.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil


PERFORMANCE_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexQuotaTray/performance.json"
PROVIDER_NAMES = {"codex": "GPT / Codex", "grok": "Grok", "gemini": "Gemini", "other": "其他模型"}


def number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def timestamp(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    result = number(value)
    if result is not None and result > 10_000_000_000:
        result /= 1000
    return result


def provider_for(model: str) -> str:
    model = model.lower()
    if "gemini" in model:
        return "gemini"
    if "grok" in model:
        return "grok"
    if "gpt" in model and "gpt-oss" not in model or "codex" in model:
        return "codex"
    return "other"


def logged_model(body: str) -> str:
    match = re.search(r"model=([A-Za-z0-9_.:/+-]{1,80})", body or "")
    return match.group(1) if match else ""


def key(*parts: Any) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


def readonly_db(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)


def proto_fields(data: bytes) -> dict[int, list[int | bytes]]:
    """Bounded protobuf wire reader; opaque strings are never interpreted."""
    result: dict[int, list[int | bytes]] = {}
    position = 0
    if len(data) > 8_000_000:
        raise ValueError("protobuf record too large")

    def varint() -> int:
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError("truncated varint")
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("invalid varint")

    while position < len(data):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError("invalid field")
        if wire == 0:
            value: int | bytes = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else 8 if wire == 1 else 4
            if position + length > len(data):
                raise ValueError("truncated field")
            value = data[position:position + length]
            position += length
        else:
            raise ValueError("unsupported wire type")
        result.setdefault(field, []).append(value)
    return result


def proto_message(data: bytes, field: int) -> bytes:
    value = proto_fields(data).get(field, [b""])[0]
    return value if isinstance(value, bytes) else b""


def proto_int(data: bytes, field: int, default=0) -> int:
    value = proto_fields(data).get(field, [default])[0]
    return value if isinstance(value, int) and value < 2**63 else default


def proto_seconds(data: bytes, *, epoch=False) -> float | None:
    if not data:
        return None
    seconds = proto_int(data, 1)
    nanos = proto_int(data, 2)
    if nanos >= 1_000_000_000 or epoch and not 1_000_000_000 < seconds < 4_000_000_000:
        return None
    value = seconds + nanos / 1e9
    return value if epoch or value <= 86400 else None


@dataclass(frozen=True)
class Observation:
    identity: str
    source: str
    session: str
    provider: str
    model: str
    ended_at: float
    output_tokens: int
    generation_seconds: float | None = None
    basis: str = "缺少生成耗时"
    count_in_rate: bool = True
    native_tps: float | None = None


class JsonlReader:
    def __init__(self, path: Path, initial_bytes=1_500_000):
        self.path = path
        self.position = max(0, path.stat().st_size - initial_bytes)
        self.skip_first = self.position > 0
        self.pending = b""
        self.skipped = 0

    def read(self):
        size = self.path.stat().st_size
        if size < self.position:
            self.position = 0
            self.pending = b""
            self.skip_first = False
        # Idle histories vastly outnumber active streams. Avoid opening each
        # file and allocating an 8 MB read buffer when already at EOF.
        if size == self.position:
            return
        with self.path.open("rb") as stream:
            stream.seek(self.position)
            chunk = stream.read(8_000_000)
            self.position = stream.tell()
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        if len(self.pending) > 8_000_000:
            self.pending = b""
            self.skip_first = True
            self.skipped += 1
        if self.skip_first and lines:
            lines.pop(0)
            self.skip_first = False
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value
            except (ValueError, UnicodeDecodeError):
                self.skipped += 1


class TpsMonitor:
    def __init__(self, home: Path | None = None):
        self.home = home or Path.home()
        self.codex_root = self.home / ".codex" if home else Path(os.environ.get("CODEX_HOME", self.home / ".codex"))
        self.grok_root = self.home / ".grok" if home else Path(os.environ.get("GROK_HOME", self.home / ".grok"))
        self.antigravity_bridge = (self.home / "AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/codex-grok-bridge/app/src/antigravity.mjs").is_file()
        self.readers: dict[Path, JsonlReader] = {}
        self.context: dict[str, dict[str, Any]] = {}
        self.events: deque[Observation] = deque()
        self.seen: set[str] = set()
        self.completed_at: dict[str, float] = {}
        self.client_stream_starts: dict[str, list[float]] = {}
        self.file_sessions: dict[Path, str] = {}
        self.started_at = time.time()
        self.grok_counters: dict[tuple[str, str], int] = {}
        self.grok_native_reader = JsonlReader(self.grok_root / "logs/unified.jsonl") if (self.grok_root / "logs/unified.jsonl").is_file() else None
        self.activity: dict[str, tuple[str, float, str]] = {}
        self.last_discovery = 0.0
        self.last_log_id = 0
        self.database_versions: dict[Path, tuple[int, int]] = {}
        self.health: dict[str, dict[str, Any]] = {}
        self.legacy_totals: dict[str, int] = {}
        self.software: list[dict[str, Any]] = []

    def add(self, observation: Observation, now: float):
        if observation.identity in self.seen or not now - 86400 <= observation.ended_at <= now + 5:
            return
        if observation.output_tokens < 0 or observation.output_tokens > 100_000_000:
            return
        self.seen.add(observation.identity)
        self.events.append(observation)

    def _register(self, path: Path, session: str, source: str, model="", provider=""):
        if path not in self.readers and path.is_file():
            self.readers[path] = JsonlReader(path)
        self.context.setdefault(session, {}).update({"source": source, "model": model, "provider": provider})
        self.file_sessions[path] = session

    def discover(self, now: float):
        state = self.codex_root / "state_5.sqlite"
        if state.is_file():
            try:
                with readonly_db(state) as db:
                    rows = db.execute("SELECT id,rollout_path,model,model_provider FROM threads ORDER BY updated_at DESC LIMIT 512").fetchall()
                for session, filename, model, provider in rows:
                    if filename and Path(filename).is_file():
                        self._register(Path(filename), session, "Codex", model or "", provider or "")
                self.health["Codex"] = {"status": "已接入", "sessions": len(rows)}
            except (OSError, sqlite3.Error) as error:
                self.health["Codex"] = {"status": "读取失败", "error": type(error).__name__}
        else:
            for path in self.codex_root.glob("sessions/*/*/*/rollout-*.jsonl"):
                if path.stat().st_mtime > now - 86400:
                    self._register(path, path.stem.split("T", 1)[-1], "Codex")
        grok_paths = list(self.grok_root.glob("sessions/*/*/updates.jsonl"))
        for path in grok_paths:
            self._register(path, path.parent.name, "Grok CLI", "grok")
            if path.with_name("events.jsonl").is_file():
                self._register(path.with_name("events.jsonl"), path.parent.name, "Grok CLI", "grok")
        self.health["Grok CLI"] = {"status": "已接入" if grok_paths else "未发现记录", "sessions": len(grok_paths)}
        self.software = running_software()
        self.last_discovery = now

    def _codex_logs(self, now: float):
        path = self.codex_root / "logs_2.sqlite"
        if not path.is_file():
            return
        with readonly_db(path) as db:
            maximum = db.execute("SELECT max(id) FROM logs").fetchone()[0] or 0
            begin = self.last_log_id or max(0, maximum - 20000)
            rows = db.execute("SELECT id,ts,ts_nanos,target,feedback_log_body,thread_id FROM logs WHERE id>? AND target='codex_core::stream_events_utils' ORDER BY id", (begin,)).fetchall()
        for _, seconds, nanos, target, body, session in rows:
            if not session:
                continue
            moment = seconds + nanos / 1e9
            if moment < now - 600:
                continue
            context = self.context.setdefault(session, {})
            observed = logged_model(body or "")
            model = str(context.get("model") or "")
            # Threads absent from the recent threads table still carry model=
            # in the client span. Without it, live GPT sessions become "other"
            # and the Codex icon never switches to its TPS number.
            if observed and (not model or provider_for(model) == "other" and provider_for(observed) != "other"):
                model = observed
                context["model"] = observed
                context.setdefault("source", "Codex")
            provider = provider_for(model)
            # These client stream events carry a thread ID. Outgoing UI delta
            # logs do not, so they cannot safely be matched across concurrent chats.
            match = re.search(r'Output item item_type="([^"]+)" item_id="([^"]+)"', body or "")
            if match and "handle_output_item_done:" not in body:
                points = self.client_stream_starts.setdefault(session, [])
                points.append(moment)
                self.client_stream_starts[session] = [point for point in points if point > now - 600]
                if moment > self.completed_at.get(session, 0):
                    self.activity[session] = (provider, moment, "生成中")
        self.last_log_id = maximum

    def _codex_record(self, record: dict, session: str, now: float):
        moment = timestamp(record.get("timestamp"))
        if moment is None:
            return
        payload = record.get("payload") or {}
        context = self.context[session]
        kind = record.get("type")
        if kind == "response_item":
            item_type = payload.get("type")
            if item_type in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call", "image_generation_call", "computer_call"):
                context["response_has_tool_call"] = True
            if item_type == "reasoning":
                context["response_has_reasoning"] = True
            return
        if kind == "turn_context":
            context["model"] = str(payload.get("model") or context.get("model") or "unknown")
            return
        event = payload.get("type")
        if event == "task_started" and moment > now - 600:
            self.activity[session] = (provider_for(context.get("model", "")), moment, "客户端工作中")
        if event in ("task_complete", "turn_aborted", "error") and self.activity.get(session, (None, 0, None))[1] <= moment:
            self.activity.pop(session, None)
        if kind == "token_usage_record":
            if payload.get("thread_id") and payload["thread_id"] != session:
                return  # A fork's replay belongs to the original thread.
            usage = payload.get("usage") or {}
            output = number(usage.get("output_tokens"))
            if output is None:
                return
            response = payload.get("response_id")
            identity = key("response", response) if response else key("codex", session, moment, output)
            previous = self.completed_at.get(session, 0)
            model = context.get("model") or "unknown"
            bridge_copy = provider_for(model) == "gemini" and context.get("provider") == "grok" and self.antigravity_bridge
            source = "Codex · Gemini桥接回传" if bridge_copy else "Codex"
            client_tps = None
            basis = "客户端暂未取得完整流式计数"
            if provider_for(model) == "codex":
                points = [point for point in self.client_stream_starts.get(session, []) if previous < point <= moment]
                if points and moment > points[0]:
                    client_tps = round(output / (moment - points[0]), 1)
                    basis = "客户端流式统计（真实输出token数÷首个输出项至计数完成的时间，含思考和工具调用输出）"
            self.add(Observation(identity, source, session, provider_for(model), model, moment, int(output), None, basis, True, client_tps), now)
            context["response_has_tool_call"] = False
            context["response_has_reasoning"] = False
            self.completed_at[session] = max(moment, previous)
            if self.activity.get(session, (None, 0, None))[1] <= moment:
                if moment > now - 3:
                    self.activity[session] = (provider_for(model), moment, "刚完成生成")
                else:
                    self.activity.pop(session, None)
            context["modern_usage"] = True
        elif event == "token_count" and not context.get("modern_usage"):
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or {}
            total = number((info.get("total_token_usage") or {}).get("output_tokens"))
            output = number(usage.get("output_tokens"))
            if total is None or output is None:
                return
            previous = self.legacy_totals.get(session)
            self.legacy_totals[session] = int(total)
            if previous is not None and total <= previous:
                return
            model = context.get("model") or "unknown"
            self.add(Observation(key("legacy", session, total), "Codex", session, provider_for(model), model, moment, int(output)), now)

    def _grok_record(self, record: dict, session: str, now: float):
        update = (record.get("params") or {}).get("update") or {}
        moment = timestamp(record.get("timestamp"))
        if moment is None:
            return
        kind = update.get("sessionUpdate")
        if kind in ("agent_thought_chunk", "agent_message_chunk"):
            self.activity[session] = ("grok", moment, "生成中")
        if kind != "turn_completed":
            return
        self.activity.pop(session, None)
        usage = update.get("usage") or {}
        models = usage.get("modelUsage") or {"grok": usage}
        for model, value in models.items():
            output = number(value.get("outputTokens"))
            if output is None:
                continue
            self.add(Observation(key("grok", session, update.get("prompt_id") or moment, model), "Grok CLI", session, provider_for(model), model, moment, int(output), None, "读取CLI输出计数；不推算TPS", moment <= self.started_at), now)
            if moment > now - 3:
                self.activity[session] = (provider_for(model), moment, "刚完成生成")

    def _grok_usage_counter(self, path: Path, now: float):
        usage_path = path.with_name("usage.json")
        if not usage_path.is_file():
            return
        value = json.loads(usage_path.read_text(encoding="utf-8"))
        moment = timestamp(value.get("updatedAt"))
        models = (value.get("session") or {}).get("modelUsage") or {}
        for model, usage in models.items():
            total = number(usage.get("outputTokens"))
            if total is None:
                continue
            counter_key = (path.parent.name, model)
            previous = self.grok_counters.get(counter_key)
            self.grok_counters[counter_key] = int(total)
            if previous is None:
                summary_path = path.with_name("summary.json")
                if summary_path.is_file():
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    created = timestamp(summary.get("created_at") or summary.get("createdAt"))
                    if created and created >= self.started_at:
                        previous = 0
            if previous is not None and moment and total > previous:
                self.add(Observation(key("grok-counter", *counter_key, total), "Grok CLI", counter_key[0], provider_for(model), model, moment, int(total - previous)), now)

    def _grok_activity(self, record: dict, session: str, now: float):
        moment = timestamp(record.get("ts"))
        if moment is None or moment < now - 600:
            return
        kind = record.get("type")
        if kind in ("loop_started", "first_token"):
            self.activity[session] = ("grok", moment, "生成中")
        elif kind == "turn_ended" and self.activity.get(session, (None, 0, None))[1] <= moment:
            if moment > now - 3:
                self.activity[session] = ("grok", moment, "刚完成生成")
            else:
                self.activity.pop(session, None)

    def _grok_native_tps(self, now: float):
        if self.grok_native_reader is None:
            return
        for record in self.grok_native_reader.read():
            if record.get("msg") != "shell.turn.inference_done":
                continue
            data = record.get("ctx") or {}
            tps = number(data.get("tokens_per_sec"))
            output = number(data.get("completion_tokens"))
            ended = timestamp(record.get("ts"))
            if tps is None or output is None or ended is None:
                continue
            session = str(record.get("sid") or "grok-client")
            self.add(Observation(key("grok-native", session, data.get("loop_index"), ended), "Grok CLI 原生统计", session, "grok", "grok", ended, int(output), None, "CLI原生 tokens_per_sec", False, tps), now)

    def _antigravity(self, now: float):
        roots = [(self.home / ".gemini/antigravity/conversations", "Antigravity 桌面"), (self.home / ".gemini/antigravity-cli/conversations", "Antigravity CLI")]
        temporary = Path(os.environ.get("TEMP", self.home / "AppData/Local/Temp"))
        for profile in temporary.glob("codex-agy-model-*"):
            roots.append((profile / "profile/antigravity-cli/conversations", "Antigravity CLI · Codex隔离配置"))
        for root, source in roots:
            paths = list(root.glob("*.db"))
            good = 0
            skipped = 0
            for path in paths:
                version = (path.stat().st_mtime_ns, path.with_name(path.name + "-wal").stat().st_mtime_ns if path.with_name(path.name + "-wal").exists() else 0)
                if self.database_versions.get(path) == version:
                    good += 1
                    continue
                try:
                    with readonly_db(path) as db:
                        rows = db.execute("SELECT idx,data FROM gen_metadata ORDER BY idx DESC LIMIT 300").fetchall()
                        steps = db.execute("SELECT idx,status,metadata FROM steps ORDER BY idx DESC LIMIT 3000").fetchall()
                except sqlite3.Error:
                    skipped += 1
                    continue
                by_response: dict[bytes, float] = {}
                by_index: dict[int, float] = {}
                for _, status, metadata in steps:
                    try:
                        completed = proto_seconds(proto_message(metadata, 7), epoch=True)
                        created = proto_seconds(proto_message(metadata, 1), epoch=True)
                        moment = completed or created
                        association = proto_message(metadata, 20)
                        generation_index = proto_int(association, 3, -1) if association else -1
                        response = proto_message(proto_message(metadata, 9), 11)
                        if moment:
                            if response:
                                by_response[response] = max(moment, by_response.get(response, 0))
                            if generation_index >= 0:
                                by_index[generation_index] = max(moment, by_index.get(generation_index, 0))
                        if status not in (3, 4, 5) and not completed and generation_index >= 0 and created and created > now - 600:
                            self.activity[path.stem] = ("gemini", created, "请求中")
                    except (ValueError, TypeError):
                        skipped += 1
                for index, blob in reversed(rows):
                    try:
                        chat_model = proto_message(blob, 1)
                        usage = proto_message(chat_model, 4)
                        total = proto_int(usage, 3, -1)
                        text = proto_int(usage, 9)
                        thoughts = proto_int(usage, 10)
                        if total < 0 or total != text + thoughts:
                            skipped += 1
                            continue
                        response = proto_message(usage, 11)
                        ended = by_response.get(response) or by_index.get(index)
                        if ended is None:
                            skipped += 1
                            continue
                        model = proto_message(chat_model, 19).decode("utf-8", errors="replace") or proto_message(chat_model, 21).decode("utf-8", errors="replace") or "unknown"
                        if len(model) > 100:
                            model = "unknown"
                        identity = key("response", response.decode("utf-8", errors="replace")) if response else key("antigravity", path.stem, index)
                        self.add(Observation(identity, source, path.stem, provider_for(model), model, ended, total, None, "客户端未提供原生TPS；保留输出计数"), now)
                        if ended > now - 3:
                            self.activity[path.stem] = (provider_for(model), ended, "刚完成生成")
                    except (ValueError, TypeError):
                        skipped += 1
                self.database_versions[path] = version
                good += 1
            self.health[source] = {"status": "已接入" if good else "未发现记录", "sessions": good, "skipped": skipped}

    def poll(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        if now - self.last_discovery >= 2:
            self.discover(now)
        try:
            self._codex_logs(now)
        except (OSError, sqlite3.Error):
            self.health["Codex 流式日志"] = {"status": "不可读取"}
        for path, reader in list(self.readers.items()):
            session = self.file_sessions[path]
            try:
                if self.context[session].get("source") == "Grok CLI":
                    session = path.parent.name
                    for record in reader.read():
                        if path.name == "updates.jsonl":
                            self._grok_record(record, session, now)
                        else:
                            self._grok_activity(record, session, now)
                    if path.name == "updates.jsonl":
                        self._grok_usage_counter(path, now)
                else:
                    records = list(reader.read())
                    if any(r.get("type") == "token_usage_record" for r in records):
                        self.context[session]["modern_usage"] = True
                    for record in records:
                        self._codex_record(record, session, now)
            except (OSError, ValueError, TypeError):
                self.health[self.context.get(session or "", {}).get("source", "记录读取")] = {"status": "部分记录不可读取"}
        try:
            self._antigravity(now)
        except (OSError, sqlite3.Error):
            self.health["Antigravity"] = {"status": "部分数据库不可读取"}
        try:
            self._grok_native_tps(now)
        except (OSError, ValueError, TypeError):
            self.health["Grok 原生速率"] = {"status": "暂不可读取"}
        while self.events and self.events[0].ended_at < now - 86400:
            self.seen.discard(self.events.popleft().identity)
        self.activity = {s: v for s, v in self.activity.items() if now - v[1] < (3 if v[2] == "刚完成生成" else 600)}
        return self.snapshot(now)

    def snapshot(self, now: float) -> dict[str, Any]:
        # The installed Codex bridge emits a second copy of agy's usage. Only
        # compare its known isolated agy channel, one-to-one, with exact output
        # counts and near-identical completion time; unrelated CLI runs remain.
        copies = [e for e in self.events if e.source == "Codex · Gemini桥接回传"]
        native = [e for e in self.events if e.source == "Antigravity CLI · Codex隔离配置"]
        duplicate_ids = set()
        available = list(native)
        for copy in copies:
            match = next((e for e in available if e.output_tokens == copy.output_tokens and e.provider == copy.provider and abs(e.ended_at - copy.ended_at) <= 3), None)
            if match:
                duplicate_ids.add(copy.identity)
                available.remove(match)
        providers = {}
        for provider, label in PROVIDER_NAMES.items():
            events = [e for e in self.events if e.provider == provider and e.identity not in duplicate_ids]
            recent = [e for e in events if e.count_in_rate and now - 60 <= e.ended_at <= now]
            samples = [e for e in events if now - 300 <= e.ended_at <= now and e.native_tps is not None]
            latest = max(samples, key=lambda e:e.ended_at) if samples else None
            active = sum(1 for value in self.activity.values() if value[0] == provider)
            output = sum(e.output_tokens for e in recent)
            generation = latest.native_tps if latest else None
            providers[provider] = {"label": label, "active_sessions": active, "generation_tps": generation, "output_tps_60": round(output / 60, 2), "output_tokens_60": output, "generation_samples": len(samples), "basis": [latest.basis] if latest else ["客户端暂未提供可核验TPS"], "models": sorted({e.model for e in samples}), "sources": sorted({e.source for e in events}), "last_sample_at": latest.ended_at if latest else None, "display_tps": generation, "display_basis": latest.basis if latest else "客户端暂未取得TPS"}
        return {"generated_at": now, "window_seconds": 60, "providers": providers, "total_output_tps_60": round(sum(p["output_tokens_60"] for p in providers.values()) / 60, 2), "software": self.software, "source_health": self.health, "measurement_note": "Grok直读CLI原生TPS；Codex使用客户端真实输出token数÷首个输出项至计数完成的流式时间，含思考及工具调用输出，完成时结算。使用时显示最近完成的TPS，缺少完整计数时保留额度；不使用字符数或近似分词器。60秒输出速率为已报告输出数之和÷60，不计输入/缓存，不重复加思考token。"}


def running_software() -> list[dict[str, Any]]:
    names = {"chatgpt.exe": "ChatGPT / Codex 桌面", "codex.exe": "Codex 引擎", "grok.exe": "Grok CLI", "agy.exe": "Antigravity CLI", "antigravity.exe": "Antigravity 桌面", "gemini.exe": "Gemini CLI", "claude.exe": "Claude Code", "cursor.exe": "Cursor", "ollama.exe": "Ollama", "lm studio.exe": "LM Studio", "copilot.exe": "Copilot CLI", "opencode.exe": "OpenCode"}
    counts: dict[str, int] = {}
    for process in psutil.process_iter(["name"]):
        name = (process.info.get("name") or "").lower()
        if name in names:
            counts[names[name]] = counts.get(names[name], 0) + 1
        elif name in ("node.exe", "node"):
            try:
                command = " ".join(process.cmdline()).lower().replace("\\", "/")
                for marker, app in (("@google/gemini-cli", "Gemini CLI"), ("@anthropic-ai/claude-code", "Claude Code"), ("@qwen-code", "Qwen Code"), ("@github/copilot", "Copilot CLI")):
                    if marker in command:
                        counts[app] = counts.get(app, 0) + 1
                        break
            except (psutil.Error, OSError):
                pass
    supported = {"ChatGPT / Codex 桌面", "Codex 引擎", "Grok CLI", "Antigravity CLI", "Antigravity 桌面"}
    return [{"name": name, "processes": count, "status": "读取本地记录" if name in supported else "暂无可核验的本地TPS信号"} for name, count in sorted(counts.items())]


def save_performance(snapshot: dict[str, Any]) -> None:
    PERFORMANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PERFORMANCE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(PERFORMANCE_PATH)

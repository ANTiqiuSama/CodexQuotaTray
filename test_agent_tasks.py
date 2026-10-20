import io
import json
import sqlite3
from contextlib import closing
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import agent_panel
import agent_tasks
from agent_tasks import AgentGroups, AgentNotes, Cluster, Summarizer, clean, describe, tool_action
from tps_sources import TpsMonitor


def agent(key, provider="codex", live=True, title="", parent="", doing="", at=100.0):
    return {"key": key, "provider": provider, "source": "Codex", "model": "", "title": title, "thread_title": "",
            "request": title, "doing": doing, "live": live, "at": at, "parent": parent, "role": "", "folder": "", "tps": None}


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + ".000Z"


class TaskTextTests(unittest.TestCase):
    def test_antigravity_pascal_case_tool_arguments(self):
        self.assertEqual(tool_action("view_file", {"AbsolutePath": "D:/repo/agent_panel.py"}), "阅读 agent_panel.py")
        self.assertEqual(tool_action("replace_file_content", {"TargetFile": "D:/repo/tray_app.py"}), "编辑 tray_app.py")
        self.assertEqual(tool_action("run_command", {"CommandLine": "$env:FOO='x'; cd D:/repo; python -m unittest test_agent_tasks"}), "运行测试")

    def test_request_becomes_a_short_task_phrase(self):
        self.assertEqual(clean("请帮我修一下 C:\\repo\\tray_app.py 里的悬停bug，然后跑测试"), "修一下 tray_app.py 里的悬停bug")
        self.assertEqual(clean("<system-reminder>ignore</system-reminder>好的。实现气泡合并动画"), "实现气泡合并动画")
        self.assertEqual(clean("```py\nprint(1)\n```\nPlease add Grok TPS"), "add Grok TPS")
        self.assertEqual(clean("继续"), "")

    def test_tool_calls_become_verbs(self):
        self.assertEqual(tool_action("exec_command", '{"cmd": "cd x && python -m unittest test_a"}'), "运行测试")
        self.assertEqual(tool_action("apply_patch", "*** Begin Patch\n*** Update File: src/hover.py\n"), "编辑 hover.py")
        self.assertEqual(tool_action("Bash", {"command": "git status"}), "git status")
        self.assertEqual(tool_action("Read", {"file_path": "/a/b/readme.md"}), "阅读 readme.md")
        self.assertEqual(tool_action("Task", {"description": "x"}), "派发子任务")
        self.assertEqual(tool_action("mcp__github__create_pr", {}), "调用 github")


class NotesTests(unittest.TestCase):
    def test_current_codex_user_records_and_tool_wait_keep_task_live(self):
        notes = AgentNotes()
        notes.codex("a", {"type":"event_msg","timestamp":stamp(100),"payload":{"type":"task_started"}})
        notes.codex("a", {"type":"response_item","timestamp":stamp(101),"payload":{
            "type":"message","role":"user","content":[{"type":"input_text","text":"请检查气泡布局与拖动合并"}]}})
        notes.codex("a", {"type":"response_item","timestamp":stamp(102),"payload":{
            "type":"function_call","name":"exec_command","arguments":json.dumps({"cmd":"python -m unittest test_agent_tasks"})}})
        context={"a":{"source":"Codex","model":"gpt-6.1-sol"}}
        active={"a":("codex",103,"刚完成生成")}
        result=notes.export(active,context,{},110,lambda _:"codex")
        self.assertEqual((result[0]["title"],result[0]["doing"],result[0]["live"]),
                         ("检查气泡布局与拖动合并","运行测试",True))
        notes.codex("a", {"type":"event_msg","timestamp":stamp(111),"payload":{"type":"task_complete"}})
        result=notes.export({},context,{},112,lambda _:"codex")
        self.assertFalse(result[0]["live"])

    def test_codex_follow_up_keeps_the_task_and_tracks_steps(self):
        notes = AgentNotes()
        notes.codex("s", {"type": "session_meta", "payload": {"cwd": "C:/w/CodexQuotaTray",
                     "source": {"subagent": {"thread_spawn": {"parent_thread_id": "main", "agent_nickname": "Ada"}}}}})
        notes.codex("s", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "实现气泡面板"}})
        notes.codex("s", {"type": "event_msg", "timestamp": stamp(101), "payload": {"type": "user_message", "message": "继续"}})
        notes.codex("s", {"type": "response_item", "timestamp": stamp(102), "payload": {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"pytest"}'}})
        note = notes.notes["s"]
        self.assertEqual((note.prompt, note.action, note.parent, note.role), ("实现气泡面板", "运行测试", "main", "Ada"))
        exported = notes.export({"s": ("codex", 102, "生成中")}, {"s": {"source": "Codex", "model": "gpt-5"}}, {}, 110)
        self.assertEqual(exported[0]["folder"], "CodexQuotaTray")
        self.assertEqual(exported[0]["doing"], "运行测试")

    def test_claude_subagent_is_live_without_its_own_activity_and_lingers_dim(self):
        notes = AgentNotes()
        notes.claude("agent-1", {"type": "user", "timestamp": stamp(100), "cwd": "/p", "message": {"content": "检查 README 链接"}}, "main")
        notes.claude("agent-1", {"type": "assistant", "timestamp": stamp(101), "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {}}]}})
        context = {"main": {"source": "Claude Code", "model": ""}}
        live = notes.export({"main": ("claude", 101, "生成中")}, context, {}, 120)
        sub = next(item for item in live if item["key"] == "agent-1")
        self.assertTrue(sub["live"])
        self.assertEqual((sub["provider"], sub["doing"], sub["parent"]), ("claude", "检索代码", "main"))
        later = notes.export({}, context, {}, 101 + agent_tasks.LIVE_SECONDS + 5)
        self.assertEqual([(item["key"], item["live"], item["doing"]) for item in later if item["key"] == "agent-1"], [("agent-1", False, "刚完成")])
        self.assertEqual(notes.export({}, context, {}, 101 + agent_tasks.RECENT_SECONDS + 5), [])

    def test_grok_prompt_chunks_are_joined(self):
        notes = AgentNotes()
        for text in ("修复 Grok ", "重置时间解析"):
            notes.grok("g", {"timestamp": stamp(100), "params": {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}}}})
        notes.grok("g", {"timestamp": stamp(101), "params": {"update": {"sessionUpdate": "tool_call", "kind": "read", "title": "Read"}}})
        self.assertEqual((notes.notes["g"].prompt, notes.notes["g"].action), ("修复 Grok 重置时间解析", "阅读文件"))

    def test_monitor_reads_codex_rollout_and_thread_title(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            now = time.time()
            rollout = home / ".codex/sessions/2026/01/01/rollout-x.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text("\n".join(json.dumps(r) for r in [
                {"type": "session_meta", "timestamp": stamp(now - 5), "payload": {"cwd": "D:/proj"}},
                {"type": "event_msg", "timestamp": stamp(now - 4), "payload": {"type": "task_started"}},
                {"type": "event_msg", "timestamp": stamp(now - 4), "payload": {"type": "user_message", "message": "继续"}},
            ]) + "\n", encoding="utf-8")
            with closing(sqlite3.connect(home / ".codex/state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, model TEXT, model_provider TEXT, updated_at INT, title TEXT, cwd TEXT)")
                db.execute("INSERT INTO threads VALUES ('t1', ?, 'gpt-5', 'openai', 1, '整理发布说明', 'D:/proj')", (str(rollout),))
            db.close()
            monitor = TpsMonitor(home)
            monitor.poll(now)
            monitor.last_discovery = 0
            monitor.poll(now + 0.5)
            agents = monitor.agents(now + 0.5)
            self.assertEqual([(a["key"], a["title"], a["live"], a["provider"], a["folder"]) for a in agents],
                             [("t1", "整理发布说明", True, "codex", "proj")])


class ScheduleTests(unittest.TestCase):
    def test_native_codex_database_and_definition_files_ignore_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)
            native=home/'.codex/sqlite/codex-dev.db'
            native.parent.mkdir(parents=True)
            with closing(sqlite3.connect(native)) as db, db:
                db.execute('CREATE TABLE automations (id TEXT,name TEXT,prompt TEXT,status TEXT,last_run_at INT,next_run_at INT)')
                db.execute("INSERT INTO automations VALUES ('watch','额度巡检','检查额度是否恢复','ACTIVE',1900000000000,1900003600000)")
            memory=home/'.codex/automations/deleted/memory.md'
            memory.parent.mkdir(parents=True)
            memory.write_text('已经删除的旧任务运行笔记',encoding='utf-8')
            found=agent_tasks.read_schedules(home/'.codex',home/'.claude')
            self.assertEqual([x['id'] for x in found],['watch'])
            self.assertEqual((found[0]['last_at'],found[0]['next_at']),(1900000000,1900003600))

    def test_real_heartbeat_envelope_is_turn_scoped_and_never_a_bubble(self):
        notes=AgentNotes()
        schedules=[{'key':'Codex:额度巡检','id':'watch','title':'额度巡检','prompt':'检查额度是否恢复'}]
        context={'thread':{'source':'Codex','model':'gpt-6.1-sol'}}
        notes.ask('thread','修改面板动画',100)
        notes.codex('thread',{'type':'event_msg','timestamp':stamp(110),'payload':{'type':'task_started'}})
        text='<heartbeat><automation_id>watch</automation_id><instructions>检查额度是否恢复</instructions></heartbeat>'
        notes.codex('thread',{'type':'response_item','timestamp':stamp(111),'payload':{
            'type':'message','role':'user','content':[{'type':'input_text','text':text}]}})
        active=notes.export({'thread':('codex',112,'生成中')},context,{},112,schedules=schedules)
        self.assertEqual(active[0]['schedule'],'Codex:额度巡检')
        self.assertIn('heartbeat:watch',active[0]['origin'])
        self.assertTrue(agent_tasks.schedule_chips(schedules,active,112)[0]['running'])
        self.assertEqual([a for a in active if not a['schedule']],[])
        notes.codex('thread',{'type':'event_msg','timestamp':stamp(115),'payload':{'type':'task_complete'}})
        finished=notes.export({},context,{},116,schedules=schedules)
        self.assertEqual(finished,[])
        self.assertFalse(agent_tasks.schedule_chips(schedules,finished,116)[0]['running'])
        notes.codex('thread',{'type':'event_msg','timestamp':stamp(120),'payload':{'type':'task_started'}})
        notes.ask('thread','手动检查气泡颜色',121)
        manual=notes.export({'thread':('codex',121,'生成中')},context,{},122,schedules=schedules)
        self.assertEqual(manual[0]['schedule'],'')
        self.assertNotIn('heartbeat:',manual[0]['origin'])

    def test_definitions_from_claude_tasks_and_codex_automation_table(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            task = home / ".claude/scheduled-tasks/daily-digest/SKILL.md"
            task.parent.mkdir(parents=True)
            task.write_text("---\nname: daily-digest\ndescription: 汇总昨日提交\n---\n汇总昨天的提交并写日报", encoding="utf-8")
            (home / ".codex").mkdir()
            with closing(sqlite3.connect(home / ".codex/state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE automations (id TEXT, name TEXT, prompt TEXT, status TEXT, next_run_at INT)")
                db.execute("INSERT INTO automations VALUES ('a1', '依赖巡检', '检查依赖是否有安全更新', 'ACTIVE', 1900000000)")
                db.execute("INSERT INTO automations VALUES ('a2', '旧任务', '不再运行', 'PAUSED', 0)")
                db.execute("CREATE TABLE automation_runs (id TEXT, prompt TEXT)")
                db.execute("INSERT INTO automation_runs VALUES ('r1', '某次运行')")
            found = agent_tasks.read_schedules(home / ".codex", home / ".claude")
        self.assertEqual(sorted((item["title"], item["prompt"]) for item in found),
                         [("daily-digest", "汇总昨天的提交并写日报"), ("依赖巡检", "检查依赖是否有安全更新")])
        self.assertEqual(next(item for item in found if item["source"] == "Codex")["next_at"], 1900000000)

    def test_scheduled_runs_are_flagged_and_dropped_once_finished(self):
        notes = AgentNotes()
        schedules = [{"key": "Codex:依赖巡检", "title": "依赖巡检", "prompt": "检查依赖是否有安全更新"}]
        notes.codex("run", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "检查依赖是否有安全更新"}})
        notes.codex("cron", {"type": "session_meta", "payload": {"originator": "codex_exec", "source": "automation"}})
        notes.codex("cron", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "清理日志"}})
        notes.codex("me", {"type": "event_msg", "timestamp": stamp(100), "payload": {"type": "user_message", "message": "实现气泡"}})
        context = {key: {"source": "Codex", "model": "gpt-5"} for key in ("run", "cron", "me")}
        live = {key: ("codex", 100, "生成中") for key in context}
        flagged = {a["key"]: a["schedule"] for a in notes.export(live, context, {}, 101, schedules=schedules)}
        self.assertEqual(flagged, {"run": "Codex:依赖巡检", "cron": "origin:清理日志", "me": ""})
        finished = notes.export({}, context, {}, 130, schedules=schedules)
        self.assertEqual([a["key"] for a in finished], ["me"])
        chips = agent_tasks.schedule_chips(schedules, notes.export(live, context, {}, 101, schedules=schedules), 101)
        self.assertEqual([(c["title"], c["running"]) for c in chips], [("依赖巡检", True), ("清理日志", True)][:2])

    def test_same_request_in_sessions_far_apart_is_recurring(self):
        notes = AgentNotes()
        for key, moment in (("a", 1000), ("b", 1000 + agent_tasks.RECUR_SPREAD + 1), ("c", 1000)):
            notes.ask(key, "汇总每日数据报表" if key != "c" else "别的事情", moment)
        notes.ask("burst1", "同时派发的任务", 5000)
        notes.ask("burst2", "同时派发的任务", 5010)
        self.assertEqual(list(notes.recurring()), ["汇总每日数据报表"])


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "groups.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_spawned_agents_group_with_their_parent_automatically(self):
        agents = [agent("main", title="重构悬停面板"), agent("c1", parent="main", title="写测试"),
                  agent("c2", parent="main", title="改文档"), agent("solo", title="别的事")]
        clusters = AgentGroups(self.path).compose(agents)
        keys = sorted(sorted(m["key"] for m in c.members) for c in clusters)
        self.assertEqual(keys, [["c1", "c2", "main"], ["solo"]])
        group = next(c for c in clusters if len(c.members) == 3)
        self.assertTrue(group.key.startswith("auto:"))
        self.assertEqual(describe(group)[0], "重构悬停面板")

    def test_siblings_group_even_when_parent_is_idle(self):
        clusters = AgentGroups(self.path).compose([agent("c1", parent="p"), agent("c2", parent="p")])
        self.assertEqual(len(clusters), 1)

    def test_merge_persists_and_split_detaches_auto_links(self):
        agents = [agent("a", title="修复额度卡片显示"), agent("b", title="额度卡片测试"), agent("c", parent="a")]
        groups = AgentGroups(self.path)
        clusters = {next(iter(m["key"] for m in c.members)): c for c in groups.compose(agents)}
        first = next(c for c in groups.compose(agents) if any(m["key"] == "a" for m in c.members))
        second = next(c for c in groups.compose(agents) if any(m["key"] == "b" for m in c.members))
        merged = groups.merge(first, second)
        again = AgentGroups(self.path).compose(agents)
        self.assertEqual([(c.key, sorted(m["key"] for m in c.members), c.manual) for c in again], [(merged, ["a", "b", "c"], True)])
        self.assertTrue(clusters)
        AgentGroups(self.path).split(again[0])
        self.assertEqual(sorted(len(c.members) for c in AgentGroups(self.path).compose(agents)), [1, 1, 1])

    def test_group_description_counts_parallel_parts(self):
        cluster = Cluster("group:x", (agent("a", title="额度卡片重构", doing="编辑 card.py"), agent("b", title="额度卡片测试", doing="运行测试"),
                                      agent("c", live=False, title="其他")), True)
        title, doing, detail = describe(cluster)
        self.assertIn(title, ("额度卡片重构", "额度卡片测试"))
        self.assertEqual(doing, "2 路并行 · 编辑 card.py、运行测试")
        self.assertIn("/", detail)


class SummarizerTests(unittest.TestCase):
    def test_settings_save_reload_and_disable_preserves_local_key(self):
        from settings_window import load_summary, save_summary
        with tempfile.TemporaryDirectory() as folder, patch.object(agent_tasks, "SUMMARY_CONFIG", Path(folder) / "summary.json"), patch.dict("os.environ", {}, clear=True):
            config = {"url": "https://token-plan-cn.xiaomimimo.com/v1", "key": "test-only-key", "model": "mimo-v2.6-flash"}
            save_summary(config, True)
            self.assertEqual(agent_tasks.summary_config()["url"], config["url"] + "/chat/completions")
            save_summary(config, False)
            self.assertIsNone(agent_tasks.summary_config())
            self.assertEqual(load_summary()["key"], "test-only-key")
            self.assertFalse((Path(folder) / "summary.tmp").exists())
            with self.assertRaises(ValueError):
                save_summary({**config, "url": "bad-url"}, True)
            self.assertFalse(load_summary()["enabled"])

    def test_live_action_updates_even_when_summary_is_cached(self):
        member=agent("a",title="验证布局",doing="运行测试")
        self.assertEqual(describe(Cluster("a",(member,)),{"title":"布局验证","doing":"阅读代码"})[1],"运行测试")

    def test_mimo_uses_native_auth_and_disables_thinking(self):
        observed = []
        class Reply(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.close()
        def opener(request, timeout):
            observed.append(request)
            return Reply(json.dumps({"choices": [{"message": {"content": '{"title":"验证气泡","doing":"检查接口"}'}}]}).encode())
        summarizer = Summarizer({"url":"https://token-plan-sgp.xiaomimimo.com/v1/chat/completions",
                                 "key":"test-key", "model":"mimo-v2.6-flash"}, opener)
        result = summarizer.request(Cluster("a", (agent("a", title="验证气泡交互"),)))
        self.assertEqual(result["title"], "验证气泡")
        self.assertEqual(observed[0].get_header("Api-key"), "test-key")
        self.assertIsNone(observed[0].get_header("Authorization"))
        payload = json.loads(observed[0].data)
        self.assertEqual(payload["thinking"], {"type":"disabled"})
        self.assertEqual(payload["max_completion_tokens"], 160)
        self.assertNotIn("max_tokens", payload)

    def test_disabled_without_configuration(self):
        self.assertEqual(Summarizer(None).lookup(Cluster("a", (agent("a", title="x"),))), ({}, False))

    def test_group_summary_runs_in_background_and_is_cached(self):
        calls = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            content = '{"title":"悬停面板改版","doing":"并行开发中","parts":{"_one_a":"界面","_one_b":"测试"}}'
            return Reply(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        summarizer = Summarizer({"url": "https://x/v1/chat/completions", "key": "k", "model": "m"}, opener)
        cluster = Cluster("group:g", (agent("item_one_a", title="界面"), agent("item_one_b", title="测试")), True)
        first, busy = summarizer.lookup(cluster)
        self.assertTrue(busy or first)
        deadline = time.time() + 5
        while summarizer.pending and time.time() < deadline:
            time.sleep(0.01)
        result, busy = summarizer.lookup(cluster)
        self.assertFalse(busy)
        self.assertEqual(result["title"], "悬停面板改版")
        self.assertEqual(result["parts"], {"item_one_a": "界面", "item_one_b": "测试"})
        self.assertEqual(describe(cluster, result)[:2], ("悬停面板改版", "并行开发中"))
        self.assertEqual(len(calls), 1)
        self.assertIn("同一个任务", calls[0]["messages"][0]["content"])


def bubble(key, members=(), doing="运行测试"):
    return agent_panel.BubbleModel(key, f"任务 {key}", doing, "C", True, "", members)


def panel(*bubbles):
    return agent_panel.PanelModel("codex", "C", "GPT / Codex", "2 个在工作", (("5 小时", 70.0), ("每周", 40.0)), 80.0, bubbles)


class SceneTests(unittest.TestCase):
    def test_gear_release_and_right_click_open_settings(self):
        scene = agent_panel.Scene()
        model = panel()
        scene.set_model(model, 0)
        moment = self.settle(scene)
        point = agent_panel.gear_center(model, scene.field.value)
        self.assertEqual(scene.step(moment + .01, point, True, False), [])
        self.assertEqual(scene.step(moment + .02, point, False, False), [("settings",)])
        self.assertEqual(scene.step(moment + .03, point, False, True), [("settings",)])
        scene.step(moment + .04, point, False, False)
        scene.step(moment + .05, point, True, False)
        self.assertEqual(scene.step(moment + .06, None, False, False), [])
        self.assertIsNotNone(scene.render(1.5))

    def settle(self, scene, start=0.0, frames=90, pointer=None, left=False):
        moment = start
        for _ in range(frames):
            moment += 1 / 60
            scene.step(moment, pointer, left, False)
        return moment

    def test_layout_never_overlaps_footprints(self):
        bubbles = [bubble(str(index), doing="编辑 some_long_file_name.py") for index in range(7)] + [bubble("g", (("C", True),) * 4)]
        homes, height = agent_panel.pack(bubbles)
        boxes = []
        for b in bubbles:
            x, y = homes[b.key]
            boxes.append((x - b.half_width, y - b.radius, x + b.half_width, y + b.radius + agent_panel.LABEL))
        for index, a in enumerate(boxes):
            self.assertGreaterEqual(a[0], agent_panel.PAD - 0.01)
            self.assertLessEqual(a[2], agent_panel.WIDTH - agent_panel.PAD + 0.01)
            self.assertLessEqual(a[3], agent_panel.FIELD_TOP + height)
            for other in boxes[index + 1:]:
                self.assertFalse(min(a[2], other[2]) > max(a[0], other[0]) and min(a[3], other[3]) > max(a[1], other[1]))

    def test_dragging_onto_another_bubble_requests_merge(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("a"), bubble("b")), 0.0)
        moment = self.settle(scene)
        a, b = scene.bodies["a"], scene.bodies["b"]
        start, end = (a.x, a.y), (b.x, b.y)
        scene.step(moment + 0.01, start, True, False)
        events = []
        for index in range(1, 31):
            p = index / 30
            events += scene.step(moment + 0.01 + index / 60, (start[0] + (end[0] - start[0]) * p, start[1] + (end[1] - start[1]) * p), True, False)
        self.assertEqual(scene.target, "b")
        events += scene.step(moment + 0.6, end, False, False)
        self.assertEqual(events, [("merge", "a", "b")])
        scene.absorb("a", "b", "group:ab", moment + 0.6)
        scene.set_model(panel(bubble("group:ab", (("C", True), ("C", True)))), moment + 0.6)
        self.assertEqual(list(scene.bodies), ["group:ab"])
        self.assertEqual([g.kind for g in scene.ghosts], ["absorb"])
        later = self.settle(scene, moment + 0.6, 60)
        self.assertAlmostEqual(scene.bodies["group:ab"].radius.value, bubble("g", (1, 2)).radius, delta=0.5)
        self.assertFalse(scene.ghosts)
        self.assertGreater(later, moment)

    def test_click_without_moving_or_drop_on_empty_space_does_not_merge(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("a"), bubble("b")), 0.0)
        moment = self.settle(scene)
        a = scene.bodies["a"]
        events = scene.step(moment + 0.02, (a.x, a.y), True, False) + scene.step(moment + 0.04, (a.x, a.y), False, False)
        self.assertEqual(events, [])
        home = (a.x, a.y)
        scene.step(moment + 0.06, home, True, False)
        events = []
        for index in range(1, 20):
            events += scene.step(moment + 0.06 + index / 60, (home[0], home[1] + 40), True, False)
        events += scene.step(moment + 0.5, (home[0], home[1] + 40), False, False)
        self.assertEqual(events, [])
        self.settle(scene, moment + 0.5, 120)
        self.assertAlmostEqual(scene.bodies["a"].y, home[1], delta=1.0)

    def test_right_click_splits_only_grouped_bubbles_and_members_burst_out(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(bubble("solo"), bubble("g", (("C", True), ("G", True)))), 0.0)
        moment = self.settle(scene)
        solo, group = scene.bodies["solo"], scene.bodies["g"]
        self.assertEqual(scene.step(moment + 0.02, (solo.x, solo.y), False, True), [("settings",)])
        scene.step(moment + 0.04, None, False, False)
        self.assertEqual(scene.step(moment + 0.06, (group.x, group.y), False, True), [("split", "g")])
        origin = (group.x, group.y)
        scene.burst("g", ["m1", "m2"], moment + 0.06)
        scene.set_model(panel(bubble("solo"), bubble("m1"), bubble("m2")), moment + 0.06)
        self.assertLess(abs(scene.bodies["m1"].x - origin[0]), 10)
        self.assertEqual([g.kind for g in scene.ghosts], ["pop"])

    def test_client_regrouping_keeps_the_same_bubble(self):
        scene = agent_panel.Scene()
        scene.set_model(panel(agent_panel.BubbleModel("main", "任务", "运行测试", "C", keys=("main",))), 0.0)
        body = scene.bodies["main"]
        self.settle(scene)
        grouped = agent_panel.BubbleModel("auto:main", "任务", "2 路并行", "C", members=(("C", True),) * 2, keys=("main", "child"))
        scene.set_model(panel(grouped), 2.0)
        self.assertIs(scene.bodies["auto:main"], body)
        self.assertEqual(scene.ghosts, [])

    def test_frames_render_with_effects(self):
        scene = agent_panel.Scene()
        busy = agent_panel.BubbleModel("g", "长长的任务名称需要截断显示", "2 路并行", "C", True, "细节", (("C", True), ("G", False)), True)
        scene.set_model(panel(bubble("a"), busy), 0.0)
        moment = self.settle(scene, frames=10)
        scene.ripples.append(agent_panel.Ripple(100, 150, 30, moment, "C"))
        image = scene.render(1.25)
        self.assertEqual(image.width, round(agent_panel.WIDTH * 1.25) + 2 * round(agent_panel.MARGIN * 1.25))
        self.assertGreater(image.getbbox()[3], round(agent_panel.FIELD_TOP * 1.25))
        scene.set_model(panel(), moment)
        self.settle(scene, moment, 30)
        self.assertIsNotNone(scene.render(1.0))

    def test_header_figures_count_up_and_follow_new_readings(self):
        scene = agent_panel.Scene()
        model = agent_panel.PanelModel("codex", "C", "Codex", "空闲", (("5 小时", 80.0, "1 小时后重置"), ("每周", None, "")),
                                       30.0, (), "idle")
        scene.set_model(model, 0.0)
        self.assertEqual(scene.meters[0].value, 0.0)
        self.assertTrue(scene.animating)
        moment = self.settle(scene, frames=120)
        self.assertAlmostEqual(scene.meters[0].value, 80.0, delta=0.5)
        self.assertAlmostEqual(scene.rate.value, 30.0, delta=0.5)
        self.assertEqual(scene.meters[1].target, 0.0)
        # Values are not part of the cached background; only labels and reset texts are.
        self.assertNotIn("80.0", repr(model.header))
        scene.set_model(agent_panel.PanelModel("codex", "C", "Codex", "空闲", (("5 小时", 60.0, ""), ("每周", None, "")),
                                               30.0, (), "idle"), moment)
        self.settle(scene, moment, 120)
        self.assertAlmostEqual(scene.meters[0].value, 60.0, delta=0.5)
        self.assertIsNotNone(scene.render(1.0))


if __name__ == "__main__":
    unittest.main()

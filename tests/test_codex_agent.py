import json
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from industplatform import codex_agent, mcp
from industplatform.storage import RunStore
from industplatform.workflow import Workers, accept_request, catalog


class CodexAgentTest(unittest.TestCase):
    def stream(self, hard_limit):
        self.clock = 0
        lines = queue.Queue()
        self.addCleanup(lines.put, b"")
        patcher = patch.multiple(codex_agent, APP_SERVER_IDLE_TIMEOUT=10)
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = patch.object(codex_agent.time, "monotonic", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        process = SimpleNamespace(stdout=SimpleNamespace(readline=lambda _: lines.get()))
        events = codex_agent._read_events(process, max_duration=hard_limit)
        self.addCleanup(events.close)
        return events, lines.put

    def test_stream_heartbeats_and_rpc_responses_reset_idle_timeout(self):
        events, emit = self.stream(40)
        for self.clock, frame in (
            (0, {"method": "item/completed"}),
            (6, {"method": "heartbeat"}),
            (12, {"id": 9, "result": {}}),
            (18, {"method": "thread/status/changed"}),
        ):
            emit(json.dumps(frame).encode())
            self.assertEqual(next(events), frame)
        emit(b"")
        self.assertEqual(list(events), [])

    def test_stream_without_responses_hits_idle_threshold(self):
        events, emit = self.stream(40)
        idle = []

        def wait():
            try:
                next(events)
            except codex_agent.RunTimedOut as exc:
                idle.append(exc)
            finally:
                completed.set()

        emit(b'{"method":"item/completed"}')
        next(events)
        # Neither a truncated record nor a JSON object without an RPC envelope is activity.
        self.clock = 9
        emit(b"{truncated")
        emit(b"{}")
        completed = threading.Event()
        watcher = threading.Thread(target=wait, daemon=True)
        watcher.start()
        self.assertFalse(completed.wait(0.03))
        self.clock = 10
        self.assertTrue(completed.wait(1))
        self.assertEqual(idle[0].diagnostic["code"], "network_idle_timeout")

    def test_active_stream_still_respects_stage_total_limit(self):
        events, emit = self.stream(25)
        for self.clock in (0, 6, 12, 18, 24):
            emit(b'{"method":"heartbeat"}')
            self.assertEqual(next(events)["method"], "heartbeat")
        self.clock = 25
        with self.assertRaises(codex_agent.RunTimedOut) as budget:
            next(events)
        self.assertEqual(budget.exception.diagnostic["code"], "stage_max_duration_exceeded")

    def test_resumed_thread_compacts_before_starting_new_turn(self):
        thread_id = "fixture-thread"
        thread = {"id": thread_id, "ephemeral": False, "status": {"type": "idle"}, "canAcceptDirectInput": True,
                  "turns": [{"items": [{"type": "agentMessage", "text": "x" * 500}]}]}
        self.assertFalse(codex_agent._context_needs_compaction({**thread, "ephemeral": True}, 100))
        self.assertFalse(codex_agent._context_needs_compaction({**thread, "status": {"type": "active"}}, 100))
        self.assertFalse(codex_agent._context_needs_compaction({**thread, "turns": [
            *thread["turns"], {"items": [{"type": "contextCompaction"}]}]}, 100))
        first = [{"id": 1, "result": {}}, {"id": 2, "result": {"thread": thread}}]
        origin = {"threadId": thread_id, "turnId": "compact"}
        compact = [
            {"id": 7, "result": {}},
            {"method": "turn/started", "params": {**origin, "turn": {"id": "compact"}}},
            {"method": "item/completed", "params": {**origin, "item": {"id": "marker", "type": "contextCompaction"}}},
            {"method": "turn/completed", "params": {**origin, "turn": {"id": "compact", "status": "completed"}}},
        ]
        last = [{"id": 3, "result": {"turn": {"id": "execute"}}},
                {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"status": "completed"}}}]
        unsupported = [{"id": 7, "error": {"code": -32601, "message": "Unknown method"}}]
        for response in (compact, unsupported):
            with self.subTest(response=response[0]), \
                    patch.object(codex_agent, "_read_events", return_value=iter(first + response + last)), \
                    patch.object(codex_agent.codex_local, "send_rpc") as send:
                events = list(codex_agent._app_server_events(None, "续跑", "read-only", "never", "user", None, None,
                    existing_thread=thread_id, context_window=100))
                self.assertEqual([message.args[1]["method"] for message in send.call_args_list],
                                 ["initialize", "initialized", "thread/resume", "thread/compact/start", "turn/start"])
                self.assertEqual([event["kind"] for event in events if "kind" in event],
                                 ["compaction"] if response == compact else [])
                self.assertEqual(events[-1]["type"], "turn.completed")

    def test_agent_is_explicit_and_mcp_cannot_recursively_launch_it(self):
        request = {"route": "codex-agent", "cohort": "offline", "prompt": "Find QIF evidence"}
        with patch.dict("os.environ", {"INDUST_GUI_CODEX": "0"}):
            self.assertFalse(catalog()["agent_available"])
            with self.assertRaises(ValueError):
                accept_request(request)
        with patch.dict("os.environ", {"INDUST_GUI_CODEX": "1"}):
            with patch.object(codex_agent.shutil, "which", return_value="/fixture/codex"):
                self.assertIn("codex-agent", [route["id"] for route in catalog()["routes"]])
                self.assertEqual(accept_request(request)[2], request["prompt"])
                with self.assertRaises(ValueError):
                    accept_request(dict(request, prompt=" "))
        with self.assertRaises(ValueError):
            mcp.run_tool("http://127.0.0.1:1", "start_run", request)
        with patch.dict("os.environ", {"INDUST_CODEX_GUI_MCP": "1"}):
            with self.assertRaises(ValueError):
                mcp.run_tool("http://127.0.0.1:1", "start_run", {"route": "demo-cnc", "cohort": "live"})

    def test_app_server_fixture_keeps_public_status_and_summary(self):
        run_id = "2" * 32
        thread = "fixture-thread"
        items = [
            {"id": "reasoning", "type": "reasoning", "content": ["not part of the result"]},
            {
                "id": "mcp", "type": "mcpToolCall", "server": "industplatform", "tool": "start_run", "status": "completed",
                "arguments": {"prompt": "not persisted", "route": "demo-cnc"},
                "result": {"content": [{"type": "text", "text": json.dumps({"id": run_id, "route": "demo-cnc"})}]},
            },
            {"id": "response", "type": "agentMessage", "text": "结论：first-credential-value"},
        ]
        fixture = [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": thread}}},
            {"id": 3, "result": {"turn": {"id": "fixture-turn"}}},
            {"method": "item/commandExecution/outputDelta", "params": {
                "threadId": "worker-thread", "turnId": "worker-turn", "itemId": "shell",
                "delta": "输出 first-credential-value",
            }},
            *[{"method": "item/completed", "params": {"threadId": thread, "item": item}} for item in items],
            {"method": "turn/completed", "params": {"threadId": thread, "turn": {"status": "completed"}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex"
            handshake = ["read -r init", "read -r notify; read -r thread", "read -r turn"]
            script = "#!/bin/sh\n"
            for index, entry in enumerate(fixture):
                if index < len(handshake):
                    script += handshake[index] + "\n"
                script += "cat <<'FIXTURE'\n" + json.dumps(entry) + "\nFIXTURE\n"
            executable.write_text(script)
            executable.chmod(0o700)
            events = []
            with patch.object(codex_agent, "executable", return_value=str(executable)):
                with patch.dict("os.environ", {"FIXTURE_API_KEY": "first-credential-value"}):
                    result = codex_agent.execute("test only", 18765, lambda *event: events.append(event))
        self.assertEqual(result, "结论：[已隐藏凭证]")
        self.assertEqual(events, [
            ("harness.thread", {"type": "harness.thread", "thread": thread}, None),
            ("harness.item", {"type": "harness.item", "thread": "worker-thread", "turn": "worker-turn",
                               "item": "shell", "label": "命令输出", "preview": "输出 [已隐藏凭证]"}, None),
            ("tool", "派发工作流", run_id),
            ("harness.item", {"type": "harness.item", "thread": thread, "turn": None,
                               "item": "mcp", "label": "派发工作流", "preview": '{"route": "demo-cnc"}',
                               "duration_ms": None}, None),
        ])

    def test_existing_database_is_migrated_without_dropping_standard_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE runs(id TEXT, status TEXT, sequence INTEGER, body TEXT)")
                conn.execute("INSERT INTO runs VALUES(?, ?, ?, ?)", (
                    "legacy", "queued", 1, '{"id":"legacy","route":"demo-cnc","status":"queued"}',
                ))
            store = RunStore(path)
            agent = store.create("codex-agent", "offline", "待处理")
            self.assertEqual(store.claim(agent_only=True)["id"], agent["id"])
            self.assertEqual(store.claim(agent_only=False)["id"], "legacy")

    def test_agent_wait_keeps_one_worker_open_for_nested_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            ready = threading.Event()

            def fixture(prompt, port, on_event, stop):
                nested = store.create("demo-cnc", "offline", "", 77)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if store.get(nested["id"])["status"] == "completed":
                        on_event("tool", "派发工作流", nested["id"])
                        ready.set()
                        return "题包已准备"
                    time.sleep(0.03)
                raise codex_agent.AgentError("嵌套任务未完成")

            with patch.object(codex_agent, "execute", side_effect=fixture):
                workers = Workers(store, count=1, port=18765)
                agent = store.create("codex-agent", "offline", "生成离线题包")
                workers.start()
                try:
                    self.assertTrue(ready.wait(4))
                    deadline = time.monotonic() + 1
                    while store.get(agent["id"])["status"] == "running" and time.monotonic() < deadline:
                        time.sleep(0.03)
                    finished = store.get(agent["id"])
                    self.assertEqual(finished["status"], "completed")
                    self.assertEqual(finished["result"]["agent_summary"], "题包已准备")
                    self.assertEqual(len(finished["result"]["agent_run_ids"]), 1)
                finally:
                    workers.close()

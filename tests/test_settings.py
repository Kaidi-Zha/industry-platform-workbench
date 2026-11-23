import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from industplatform import api_agent, codex_agent, workflow
from industplatform.settings import DEFAULTS, Settings
from industplatform.storage import RunStore


class SettingsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "workbench.sqlite"
        self.store = RunStore(self.path)
        self.settings = Settings(self.store)

    def test_updates_survive_restart_and_report_storage(self):
        self.assertEqual(self.settings.get(), DEFAULTS)
        changes = {
            "default_model": "a" * 32, "density": "compact", "resume_last_chat": True,
            "max_model_turns": 2, "max_tool_calls": 1, "wait_seconds": 1,
            "allow_keyword_tools": False, "agent_guidance": "核" * 1200,
        }
        self.assertEqual(self.settings.update(changes), {**DEFAULTS, **changes})
        self.settings.update({"theme": "dark"})
        restarted = Settings(RunStore(self.path))
        self.assertEqual(restarted.get(), {**DEFAULTS, **changes, "theme": "dark"})

        first = self.store.create(api_agent.ROUTE, "offline", "first turn")
        first["status"] = "completed"
        self.store.update(first)
        self.store.create(api_agent.ROUTE, "offline", "second turn", conversation_id=first["id"])
        self.store.create("demo-cnc", "offline", "fixture")
        storage = restarted.storage()
        self.assertEqual(storage["database_path"], str(self.path.resolve()))
        self.assertGreaterEqual(storage["database_bytes"], self.path.stat().st_size)
        self.assertEqual(storage["run_count"], 3)
        self.assertEqual(storage["conversation_count"], 1)

    def test_invalid_changes_are_atomic(self):
        for changes in (
            [],
            {"theme": "dark", "unknown": True},
            {"theme": "dark", "history_turns": True},
            {"max_child_runs": -1},
            {"max_authored_tasks": 3},
            {"max_model_turns": 1},
            {"max_model_turns": 9},
            {"max_tool_calls": 0},
            {"max_tool_calls": 13},
            {"wait_seconds": 0},
            {"wait_seconds": 16},
            {"allow_keyword_tools": 0},
            {"agent_guidance": "核" * 1201},
            {"agent_guidance": None},
            {"resume_last_chat": 1},
            {"send_shortcut": "return"},
            {"default_model": "local-model-name"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.settings.update(changes)
            self.assertEqual(self.settings.get(), DEFAULTS)

    def test_native_history_limit(self):
        first = self.store.create(api_agent.ROUTE, "offline", "first turn")
        first["status"] = "completed"
        first["result"]["agent_run_ids"] = ["1" * 32]
        self.store.update(first)
        second = self.store.create(api_agent.ROUTE, "offline", "second turn", conversation_id=first["id"])
        second["status"] = "completed"
        second["result"]["agent_run_ids"] = ["2" * 32]
        self.store.update(second)
        third = self.store.create(api_agent.ROUTE, "offline", "third turn", conversation_id=first["id"])

        third["agent_options"] = {"history_turns": 1}
        prompt, related = workflow._agent_prompt(self.store, third)
        self.assertNotIn("first turn", prompt)
        self.assertIn("second turn", prompt)
        self.assertEqual(related, ["2" * 32])
        third["agent_options"]["history_turns"] = 0
        self.assertEqual(workflow._agent_prompt(self.store, third), ("third turn", []))

        third["route"] = codex_agent.ROUTE
        prompt, related = workflow._agent_prompt(self.store, third)
        self.assertIn("first turn", prompt)
        self.assertEqual(related, ["1" * 32, "2" * 32])

    def test_native_agent_omits_disabled_tools_and_keeps_related_results(self):
        child_id = "1" * 32
        child = {"id": child_id, "route": "demo-cnc", "status": "completed"}
        author = Mock()

        def generate(profile, system, prompt, *, tools, execute, **options):
            names = {tool["name"] for tool in tools}
            self.assertNotIn("start_run", names)
            self.assertNotIn("author_cnc_task", names)
            self.assertIn("没有可用", execute("start_run", {"route": "demo-cnc"}))
            self.assertIn("没有可用", execute("author_cnc_task", {}))
            self.assertEqual(execute("get_run", {"id": child_id})["id"], child_id)
            return "已读上轮结果"

        with patch.object(api_agent.mcp, "run_tool", return_value=child) as run_tool:
            prompt, system, tools, execute, _ = api_agent.tool_session(
                {}, "继续核验", 9999, Mock(), on_task=author, prior_run_ids=[child_id],
                max_child_runs=0, max_authored_tasks=0,
            )
            generate({}, system, prompt, tools=tools, execute=execute)
        author.assert_not_called()
        run_tool.assert_called_once_with("http://127.0.0.1:9999", "get_run", {"id": child_id})

    def test_native_agent_enforces_a_lower_positive_limit(self):
        child = {"id": "1" * 32, "route": "demo-cnc", "status": "completed"}
        author = Mock()
        task = {"id": "task", "title": "首件检验", "difficulty": "未评级", "verification": "offline_verified"}
        args = {key: "fixture" for key in ("title", "scenario", "requirements_csv", "measurements_csv")}

        def generate(profile, system, prompt, *, tools, execute, **options):
            self.assertEqual(execute("start_run", {"route": "demo-cnc"})["id"], child["id"])
            self.assertIn("派发上限", execute("start_run", {"route": "demo-cnc"}))
            self.assertEqual(execute("author_cnc_task", args)["id"], task["id"])
            self.assertIn("任务生成上限", execute("author_cnc_task", args))
            return "已完成"

        with patch.object(api_agent.mcp, "run_tool", return_value=child) as run_tool, \
             patch.object(api_agent.agent_tasks, "author", return_value=task):
            prompt, system, tools, execute, _ = api_agent.tool_session(
                {}, "提交采集并编制题目", 9999, Mock(), on_task=author,
                max_child_runs=1, max_authored_tasks=1,
            )
            generate({}, system, prompt, tools=tools, execute=execute)
        author.assert_called_once_with(task)
        run_tool.assert_called_once()

    def test_native_workflow_enforces_queued_settings(self):
        child = self.store.create("demo-cnc", "offline", "fixture")
        first = self.store.create(api_agent.ROUTE, "offline", "first")
        first["status"] = "completed"
        first["result"]["agent_run_ids"] = [child["id"]]
        self.store.update(first)
        profile = {"api_key": "fixture-secret"}
        run = self.store.create(api_agent.ROUTE, "offline", "继续核验", conversation_id=first["id"],
                                agent_options={
                                    "max_model_turns": 3, "max_tool_calls": 4, "wait_seconds": 2,
                                    "allow_keyword_tools": False,
                                    "agent_guidance": "列出证据来源 " + profile["api_key"],
                                })

        def run_codex(prompt, port, on_event, stop, *, profile, guidance, tools, max_turns):
            tools, execute = tools
            self.assertEqual(max_turns, 3)
            schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
            self.assertIn("get_catalog", schemas)
            self.assertNotIn("get_rules", schemas)
            self.assertNotIn("search_keywords", schemas)
            self.assertEqual(schemas["wait_run"]["properties"]["timeout_seconds"]["maximum"], 2)
            self.assertNotIn("Forest", guidance)
            self.assertNotIn("列出证据来源", guidance)
            self.assertIn("继续核验", prompt)
            self.assertIn("列出证据来源 [已隐藏凭证]", prompt)
            self.assertNotIn(profile["api_key"], prompt)
            self.assertIn("没有可用", execute("get_rules", {}))
            self.assertIn("没有可用", execute("search_keywords", {"q": "CNC"}))
            self.assertIn("参数无效", execute("wait_run", {"id": child["id"], "timeout_seconds": 3}))
            self.assertEqual(execute("wait_run", {"id": child["id"]})["id"], child["id"])
            self.assertIn("工具调用上限", execute("get_run", {"id": child["id"]}))
            return "已核验"

        with patch.object(api_agent.mcp, "run_tool", return_value=child) as run_tool, \
             patch.object(codex_agent, "execute", side_effect=run_codex):
            workflow.run_agent(self.store, run, 9999, None, connection=profile)

        run_tool.assert_called_once_with("http://127.0.0.1:9999", "wait_run",
                                         {"id": child["id"], "timeout_seconds": 2})
        self.assertEqual(run["status"], "completed")

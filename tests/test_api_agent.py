import json
import unittest
from unittest.mock import Mock, patch

from industplatform import api_agent


class ApiAgentTest(unittest.TestCase):
    def test_agent_uses_only_bounded_public_workflows(self):
        profile = {"api_key": "fixture-model-credential"}
        child_id = "1" * 32
        catalog = {
            "routes": [
                {"id": "demo-cnc"}, {"id": "registered-route"},
                {"id": "codex-agent"}, {"id": "api-agent"},
            ],
            "keyword_count": 123,
            "cohorts": [{"id": "offline"}, {"id": "live"}],
        }
        child = {
            "id": child_id, "route": "demo-cnc", "status": "completed",
            "prompt": "private prompt", "benchmark_url": "local artifact",
            "result": {
                "assets": [{"title": profile["api_key"]}],
                "metrics": {"passed": 1},
                "tasks": [{"reference": "private answer"}],
                "evaluations": ["unexpected detail"],
            },
        }
        local_tools = Mock(side_effect=[catalog, child, child, dict(child, id="3" * 32, route="registered-route")])
        record = Mock()

        def generate(received_profile, system, prompt, *, tools, execute, **options):
            self.assertIs(received_profile, profile)
            self.assertNotIn(profile["api_key"], prompt)
            self.assertIn("Forest", system)
            schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
            self.assertIn("export_benchmark", schemas)
            self.assertNotIn("cohort", schemas["start_run"]["properties"])
            self.assertEqual(
                schemas["start_run"]["properties"]["route"]["enum"], ["demo-cnc", "registered-route"],
            )
            public_catalog = execute("get_catalog", {})
            self.assertEqual(set(public_catalog), {"routes", "keyword_count"})
            self.assertEqual(public_catalog["routes"], catalog["routes"][:2])
            for args in [
                {"route": "demo-cnc", "cohort": "live"},
                {"route": "api-agent"},
                {"route": "demo-cnc", "url": "https://example.org"},
                {"route": "demo-cnc", "prompt": profile["api_key"]},
            ]:
                self.assertNotIn(child_id, execute("start_run", args))
            self.assertIn("本轮派发", execute("get_run", {"id": "2" * 32}))
            execute("start_run", {"route": "demo-cnc", "seed": 0})
            response = execute("wait_run", {"id": child_id})
            self.assertEqual(response["id"], child_id)
            self.assertEqual(response["result"]["metrics"], {"passed": 1})
            encoded = json.dumps(response, ensure_ascii=False)
            for hidden in (profile["api_key"], "private prompt", "private answer", "local artifact", "evaluations"):
                self.assertNotIn(hidden, encoded)
            execute("start_run", {"route": "registered-route"})
            self.assertIn("派发上限", execute("start_run", {"route": "demo-cnc"}))
            self.assertIn("本轮派发", execute("export_benchmark", {"id": "2" * 32}))
            self.assertIn("参数无效", execute("wait_run", {"id": child_id, "timeout_seconds": 30}))
            self.assertIn("工具调用上限", execute("get_rules", {}))
            return "结果 " + profile["api_key"]

        with patch.object(api_agent.sources, "catalog", return_value=catalog), \
             patch.object(api_agent.mcp, "run_tool", local_tools):
            prompt, system, tools, execute, _ = api_agent.tool_session(
                profile, "核查 " + profile["api_key"], 9999, record,
            )
            self.assertIn("[已隐藏凭证]", prompt)
            generate(profile, system, prompt, tools=tools, execute=execute)

        self.assertEqual(local_tools.call_count, 4)
        self.assertEqual(local_tools.call_args_list[1].args, (
            "http://127.0.0.1:9999", "start_run", {"route": "demo-cnc", "seed": 0, "cohort": "offline"},
        ))
        self.assertEqual(local_tools.call_args_list[2].args[2], {
            "id": child_id, "timeout_seconds": api_agent.MAX_WAIT_SECONDS,
        })
        record.assert_any_call("tool", "派发工作流", child_id)
        record.assert_any_call("tool", "等待工作流", child_id)

    def test_tool_error_does_not_echo_profile_secret(self):
        profile = {"api_key": "fixture-model-credential"}
        error = ValueError("错误 " + profile["api_key"])
        with patch.object(api_agent.mcp, "run_tool", side_effect=error):
            _, _, _, execute, _ = api_agent.tool_session(profile, "核查", 9999, Mock())
            self.assertEqual(execute("get_rules", {}), "错误 [已隐藏凭证]")

    def test_agent_persists_only_public_cnc_task_data(self):
        requirements = "characteristic_id,revision,unit,lower_limit,upper_limit\nWIDTH,B,mm,9.98,10.02\n"
        measurements = "characteristic_id,revision,unit,value\nWIDTH,B,um,9980\n"
        params = {
            "title": "公差边界",
            "scenario": "检查工装更换后的首件。",
            "requirements_csv": requirements,
            "measurements_csv": measurements,
        }
        create_task = Mock()

        def generate(profile, system, prompt, *, tools, execute, **options):
            self.assertIn("author_cnc_task", {tool["name"] for tool in tools})
            invalid = {**params, "measurements_csv": "wrong,headers"}
            self.assertIn("应包含这些列", execute("author_cnc_task", invalid))
            self.assertIn("参数无效", execute("author_cnc_task", {**params, "reference": "injected"}))
            receipt = execute("author_cnc_task", params)
            self.assertEqual(set(receipt), {"id", "title", "difficulty", "verification"})
            self.assertEqual(receipt["verification"], "offline_verified")
            self.assertIn("相同输入", execute("author_cnc_task", params))
            return "已生成题目"

        prompt, system, tools, execute, _ = api_agent.tool_session(
            {}, "编制 CNC 工程预设题", 9999, Mock(), on_task=create_task,
        )
        generate({}, system, prompt, tools=tools, execute=execute)

        create_task.assert_called_once()
        task = create_task.call_args.args[0]
        self.assertEqual(task["data_origin"], "synthetic")
        self.assertIn("未评级", task["difficulty"])
        self.assertEqual(task["inputs"], {"requirements.csv": requirements, "measurements.csv": measurements})
        self.assertNotIn("reference", task)
        self.assertNotIn("findings", task)

    def test_related_runs_remain_readable_across_turns(self):
        child_id = "1" * 32
        child = {"id": child_id, "status": "needs_review", "route": "registered-source"}

        def generate(profile, system, prompt, *, tools, execute, **options):
            result = execute("get_run", {"id": child_id})
            self.assertEqual(result["link"], "/#tasks/run/" + child_id)
            self.assertIn("本对话", execute("get_run", {"id": "2" * 32}))

        with patch.object(api_agent.mcp, "run_tool", return_value=child) as run_tool:
            prompt, system, tools, execute, _ = api_agent.tool_session(
                {}, "继续核验", 9999, Mock(), prior_run_ids=[child_id],
            )
            generate({}, system, prompt, tools=tools, execute=execute)

        run_tool.assert_called_once()

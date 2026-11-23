import json
import io
import unittest
from unittest.mock import Mock, patch

from industplatform import codex_gateway as gateway, codex_protocol as wire, provider_api as api


class CodexProviderTest(unittest.TestCase):
    tools = [{"type": "namespace", "name": "mcp__industplatform", "tools": [
        {"type": "function", "name": "get_rules", "parameters": {"type": "object"}},
    ]}]
    name = "mcp__industplatform__get_rules"

    def test_claude_replays_signed_blocks_and_matches_tool_result(self):
        adapter = wire.Translation(api.ANTHROPIC)
        initial = {"input": [{"role": "user", "content": "Read rules"}], "tools": self.tools}
        request, identities = adapter.request(initial, "claude-sonnet-4-6")
        self.assertEqual(request["tools"][0]["name"], self.name)
        original = [
            {"type": "thinking", "thinking": "fixture", "signature": "signed"},
            {"type": "tool_use", "name": self.name, "id": "toolu_1", "input": {}},
        ]
        response = adapter.response({"content": original, "stop_reason": "tool_use"}, identities, "claude-sonnet-4-6")
        item = response["output"][0]
        self.assertEqual((item["name"], item["namespace"]), ("get_rules", "mcp__industplatform"))
        next_turn = {**initial, "input": initial["input"] + [item, {
            "type": "function_call_output", "call_id": item["call_id"], "output": "rules",
        }]}
        followup, _ = adapter.request(next_turn, "claude-sonnet-4-6")
        self.assertEqual(followup["messages"][-2]["content"], original)
        self.assertEqual(followup["messages"][-1]["content"], [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "rules"},
        ])

    def test_gemini_preserves_parallel_signed_parts_with_missing_vendor_ids(self):
        adapter = wire.Translation(api.GEMINI)
        initial = {"input": [{"role": "user", "content": "Read rules"}], "tools": self.tools}
        _, identities = adapter.request(initial, "gemini-2.5-pro")
        parts = [
            {"functionCall": {"name": self.name, "args": {}}, "thoughtSignature": "signed"},
            {"functionCall": {"name": self.name, "args": {}}},
        ]
        reply = {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
        response = adapter.response(reply, identities, "gemini-2.5-pro")
        calls = response["output"]
        self.assertNotEqual(calls[0]["call_id"], calls[1]["call_id"])
        outputs = [{"type": "function_call_output", "call_id": item["call_id"], "output": str(i)}
                   for i, item in enumerate(calls)]
        followup, _ = adapter.request({**initial, "input": initial["input"] + calls + outputs}, "gemini-2.5-pro")
        self.assertEqual(followup["contents"][-2]["parts"], parts)
        self.assertEqual(followup["contents"][-1]["parts"], [
            {"functionResponse": {"name": self.name, "response": {"output": "0"}}},
            {"functionResponse": {"name": self.name, "response": {"output": "1"}}},
        ])

    def test_responses_namespace_translation_keeps_upstream_replay(self):
        tools, aliases = wire.flatten_response_tools({"tools": self.tools})
        self.assertEqual(tools[0]["name"], self.name)
        output = [
            {"type": "reasoning", "id": "rs_1", "content": "upstream-only fixture"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": self.name},
        ]
        tool_call = wire.unflatten_item(output[1], aliases)
        self.assertEqual(tool_call["namespace"], "mcp__industplatform")
        g = gateway.Gateway({"protocol": api.OPENAI, "model": "deepseek-flash"}, None, 2)
        g._remember({"id": "resp_1", "output": output})
        result = {"type": "function_call_output", "call_id": "call_1", "output": "rules"}
        self.assertEqual(g._restore_history([tool_call, result]), [*output, result])

    def test_gateway_configuration_passes_only_ephemeral_auth_and_disables_astra_delegation(self):
        profile = {"protocol": api.OPENAI, "model": "gpt-6.2-astra", "api_key": "fixture-private-key"}
        provider = gateway.Gateway(profile, ([], Mock()), 2)
        provider.port = 1234
        settings = provider.settings()
        self.assertEqual(settings["model"], profile["model"])
        self.assertFalse(settings["features.multi_agent"])
        self.assertFalse(settings["features.multi_agent_v2"])
        self.assertIn(gateway.MODEL_TOKEN, settings["shell_environment_policy.exclude"])
        self.assertNotIn(profile["api_key"], json.dumps(settings))
        self.assertNotIn(profile["api_key"], provider.environment().values())

    def test_scoped_delegation_keeps_the_phase_tool_filter_on_both_connections(self):
        primary = {"protocol": api.OPENAI, "model": "primary"}
        worker = {"protocol": api.OPENAI, "model": "worker"}
        provider = gateway.Gateway(primary, ([{"name": "upsert_process_node"}], Mock()), 3, scoped=True,
                                   worker=worker, collaboration=True)
        offered = [
            {"type": "namespace", "name": "mcp__industplatform", "tools": [
                {"name": "upsert_process_node"}, {"name": "upsert_benchmark_task"}]},
            {"type": "namespace", "name": "collaboration", "tools": [
                {"name": "spawn_agent"}, {"name": "unexpected_tool"}]},
            {"type": "function", "name": "exec_command"},
        ]
        with patch.object(api, "_transport", return_value=(Mock(), "https://example.com/v1", {})):
            for session, model, expected in ((provider, "primary", ["upsert_process_node", "spawn_agent"]),
                                             (provider.worker, "worker", ["upsert_process_node"])):
                with patch.object(session, "_openai_request") as outgoing:
                    session.response(Mock(), {"model": model, "tools": offered}, "/responses")
                    sent = outgoing.call_args.args[1]["tools"]
                    self.assertEqual([tool["name"] for namespace in sent for tool in namespace["tools"]], expected)

    def test_deepseek_responses_receives_native_agent_messages(self):
        user = {"type": "message", "role": "user", "content": "Verify the handoff"}
        delegated = {"type": "agent_message", "id": "msg_agent_1", "author": "/root",
                     "recipient": "/root/worker", "content": [
                         {"type": "input_text", "text": "Message Type: NEW_TASK\nPayload:"},
                         {"type": "encrypted_content", "encrypted_content": "Calculate 131 × 17."},
                     ]}
        for host in ("https://api.deepseek.com", "https://api.openai.com/v1"):
            with self.subTest(host=host):
                reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "provider-specific"}
                body = {"model": "deepseek-flash", "input": [user, delegated, reasoning], "tools": self.tools}
                stream = io.BytesIO(b"".join(wire.sse([("response.completed", {
                    "type": "response.completed",
                    "response": {"id": "resp_fixture", "output": [wire.message("2227")]},
                })])))
                stream.headers = Mock()
                stream.headers.get_content_type.return_value = "text/event-stream"
                opener = Mock()
                opener.open.return_value = stream
                g = gateway.Gateway({"protocol": api.OPENAI, "model": "deepseek-flash"}, None, 2)
                g._openai_request(Mock(), body, "/responses", (opener, host, {}), 10)
                sent = json.loads(opener.open.call_args.args[0].data)["input"]
                projected = {"type": "message", "role": "user", "content": [{"type": "input_text",
                    "text": "[Codex agent message from /root to /root/worker]\nMessage Type: NEW_TASK\nPayload:\nCalculate 131 × 17."}]}
                self.assertEqual(sent, [user, projected if host == "https://api.deepseek.com" else delegated, reasoning])

    def test_deepseek_rejects_empty_agent_messages(self):
        empty = {"type": "agent_message", "author": "/root", "recipient": "/root/worker",
                 "content": [{"type": "input_text", "text": " "}]}
        with self.assertRaisesRegex(api.ProviderError, "没有可读文本"):
            wire.deepseek_input([empty])

    def test_translated_call_uses_codex_budget(self):
        profile = {"protocol": api.GEMINI, "model": "gemini-2.5-pro"}
        g = gateway.Gateway(profile, None, 1)
        handler = Mock()
        payload = {"model": profile["model"], "input": "Answer this"}
        reply = {"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}
        with patch.object(api, "_transport", return_value=(Mock(), "https://example.test", {})), \
             patch.object(api, "_post", return_value=reply):
            g.response(handler, payload, "/responses")
            with self.assertRaisesRegex(api.ProviderError, "模型请求上限"):
                g.response(handler, payload, "/responses")
        self.assertEqual(handler.send_sse.call_args.args[0]["output"][0]["content"][0]["text"], "OK")

    def test_ultra_preserves_hosted_history_but_returns_only_root_text(self):
        profile = {"protocol": api.OPENAI, "model": "gpt-6.2-sol",
                   "multi_agent": {"enabled": True, "max_concurrent_subagents": 2}}
        g = gateway.Gateway(profile, None, 2)
        output = [
            {**wire.message("worker", "final_answer"), "agent": {"agent_name": "/root/work"}},
            {"type": "multi_agent_call", "id": "mac_1", "action": "spawn_agent"},
            {**wire.message("root", "final_answer"), "agent": {"agent_name": "/root"}},
        ]
        raw = io.BytesIO(b"".join(wire.sse([("response.completed", {
            "response": {"id": "resp_1", "status": "completed", "output": output},
        })])))
        raw.headers = Mock()
        raw.headers.get_content_type.return_value = "text/event-stream"
        opener = Mock()
        opener.open.return_value = raw
        handler = Mock()
        g._openai_request(handler, {"model": profile["model"], "input": []}, "/responses",
                          (opener, "https://api.openai.com/v1", {}), 10)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Openai-beta"), "responses_multi_agent=v1")
        self.assertEqual(json.loads(request.data)["multi_agent"], profile["multi_agent"])
        self.assertEqual(handler.send_sse.call_args.args[0]["output"], [output[-1]])
        self.assertEqual(g._restore_history([output[-1]]), output)

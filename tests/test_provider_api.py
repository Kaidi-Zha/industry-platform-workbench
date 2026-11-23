"""Exercise the native wire formats without external provider calls."""

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

from industplatform import provider_api as api


SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
TOOL = {"name": "lookup", "description": "Look up a known value", "inputSchema": SCHEMA}


class ProviderApiTests(unittest.TestCase):
    def setUp(self):
        opener_patch = patch.object(api.urllib.request, "build_opener")
        self.build_opener = opener_patch.start()
        self.addCleanup(opener_patch.stop)
        self.opener = self.build_opener.return_value

    def replies(self, *replies):
        self.opener.open.side_effect = [
            io.BytesIO(json.dumps(reply).encode()) for reply in replies
        ]

    def profile(self, protocol, **fields):
        return {
            "protocol": protocol, "api_key": "test-key", "model": "model-name", **fields,
        }

    def requests(self):
        return [
            (call.args[0].full_url, dict(call.args[0].header_items()), json.loads(call.args[0].data))
            for call in self.opener.open.call_args_list
        ]

    def test_openai_stateless_multiple_items_and_results(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
        preamble = {"type": "message", "id": "msg_1", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Looking up."}]}
        call = {"type": "function_call", "id": "fc_1", "name": "lookup",
                "call_id": "call_1", "arguments": '{"q":"A"}'}
        self.replies(
            {"output": [reasoning, preamble, call]},
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Done"}]}]},
        )
        execute = Mock(return_value={"answer": 42})
        self.assertEqual(api.generate(self.profile(api.OPENAI), "System", "Question",
                                      tools=[TOOL], execute=execute, max_turns=2), "Done")
        execute.assert_called_once_with("lookup", {"q": "A"})
        (url, headers, first), (_, _, second) = self.requests()
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertNotIn("Openai-beta", headers)
        self.assertNotIn("multi_agent", first)
        self.assertEqual(first["instructions"], "System")
        self.assertFalse(first["store"])
        self.assertEqual(first["include"], ["reasoning.encrypted_content"])
        self.assertEqual(first["tools"], [{
            "type": "function", "name": TOOL["name"], "description": TOOL["description"],
            "parameters": SCHEMA, "strict": False,
        }])
        self.assertEqual(second["input"][1:4], [reasoning, preamble, call])
        self.assertEqual(second["input"][4], {
            "type": "function_call_output", "call_id": "call_1", "output": '{"answer":42}',
        })

    def test_multi_agent_requires_direct_openai_and_supported_model(self):
        for model in ("gpt-6.2-sol", "gpt-6.1-sol-2026-11-01", "gpt-5.6",
                      "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            with self.subTest(model=model):
                self.assertTrue(api.supports_multi_agent(api.OPENAI, model, "https://API.OPENAI.COM:443/v1/"))
        for model in ("gpt-6.2-astra", "gpt-6.2-Astra-2026-11-01", "gpt-5.5", "models/gpt-6.2-sol"):
            with self.subTest(model=model):
                self.assertFalse(api.supports_multi_agent(api.OPENAI, model, ""))
        for base_url in ("https://example.test/v1", "https://api.openai.com/proxy/v1",
                         "https://api.openai.com:444/v1", "https://user@api.openai.com/v1"):
            with self.subTest(base_url=base_url):
                self.assertFalse(api.supports_multi_agent(api.OPENAI, "gpt-6.2-sol", base_url))
        self.assertFalse(api.supports_multi_agent(api.ANTHROPIC, "gpt-6.2-sol", ""))

    def test_multi_agent_validates_configuration(self):
        profile = (api.OPENAI, "gpt-6.2-sol", "")
        self.assertEqual(api.normalize_multi_agent({"enabled": True}, *profile),
                         {"enabled": True, "max_concurrent_subagents": 3})
        self.assertEqual(api.normalize_multi_agent({"enabled": True, "max_concurrent_subagents": 128}, *profile),
                         {"enabled": True, "max_concurrent_subagents": 128})
        self.assertIsNone(api.normalize_multi_agent({"enabled": False}, api.ANTHROPIC, "model", ""))
        for value in (True, {}, {"enabled": 1}, {"enabled": True, "max_concurrent_subagents": True},
                      {"enabled": True, "max_concurrent_subagents": 0},
                      {"enabled": True, "max_concurrent_subagents": "3"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.normalize_multi_agent(value, *profile)
        with self.assertRaises(api.ProviderError):
            api.generate(self.profile(api.OPENAI, model="gpt-6.2-astra", multi_agent={"enabled": True}), "", "Test")
        self.opener.open.assert_not_called()

    def test_multi_agent_replays_full_history_and_returns_root_final_text(self):
        root = {"agent_name": "/root"}
        child = {"agent_name": "/root/researcher"}
        hosted = {"type": "multi_agent_call", "action": "spawn_agent", "arguments": "{}",
                  "call_id": "spawn", "agent": root}
        caller = {"type": "direct"}
        call = {"type": "function_call", "name": "lookup", "call_id": "first",
                "arguments": '{"q":"A"}', "agent": child, "caller": caller}
        other_call = {"type": "function_call", "name": "lookup", "call_id": "second",
                      "arguments": '{"q":"B"}', "agent": root}
        commentary = {"type": "message", "phase": "commentary", "agent": root,
                      "content": [{"type": "output_text", "text": "Progress update"}]}
        child_final = {"type": "message", "phase": "final_answer", "agent": child,
                       "content": [{"type": "output_text", "text": "Subagent finding"}]}
        root_final = {"type": "message", "phase": "final_answer", "agent": root,
                      "content": [{"type": "output_text", "text": "Synthesis"}]}
        first_output = [hosted, commentary, call, other_call]
        self.replies({"output": first_output},
                     {"output": [commentary, child_final, root_final]})
        execute = Mock(return_value={"answer": 42})
        profile = self.profile(api.OPENAI, model="gpt-6.2-sol", reasoning_effort="high",
                               multi_agent={"enabled": True, "max_concurrent_subagents": 5})
        self.assertEqual(api.generate(profile, "", "Question", tools=[TOOL], execute=execute, max_turns=2),
                         "Synthesis")
        self.assertEqual([call.args for call in execute.call_args_list],
                         [("lookup", {"q": "A"}), ("lookup", {"q": "B"})])
        for call in self.opener.open.call_args_list:
            self.assertGreater(call.kwargs["timeout"], api.REQUEST_TIMEOUT)
            self.assertLessEqual(call.kwargs["timeout"], api.GENERATION_TIMEOUT)
        for url, headers, payload in self.requests():
            self.assertEqual(url, "https://api.openai.com/v1/responses")
            self.assertEqual(headers["Openai-beta"], "responses_multi_agent=v1")
            self.assertEqual(payload["multi_agent"], profile["multi_agent"])
            self.assertEqual(payload["reasoning"], {"effort": "high"})
            self.assertFalse(payload["store"])
        continuation = self.requests()[1][2]["input"]
        self.assertEqual(continuation, [
            {"role": "user", "content": "Question"}, *first_output,
            {"type": "function_call_output", "call_id": "first", "output": '{"answer":42}', "caller": caller},
            {"type": "function_call_output", "call_id": "second", "output": '{"answer":42}'},
        ])

    def test_anthropic_exchanges_full_assistant_and_user_results(self):
        blocks = [
            {"type": "thinking", "thinking": "internal", "signature": "opaque"},
            {"type": "text", "text": "Looking up."},
            {"type": "tool_use", "name": "lookup", "input": {"q": "A"}, "id": "toolu_1"},
            {"type": "tool_use", "name": "lookup", "input": {"q": "B"}, "id": "toolu_2"},
        ]
        self.replies(
            {"content": blocks, "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": "Done"}], "stop_reason": "end_turn"},
        )
        execute = Mock(side_effect=[{"answer": 1}, {"answer": 2}])
        self.assertEqual(api.generate(self.profile(api.ANTHROPIC), "System", "Question",
                                      tools=[TOOL], execute=execute, max_turns=2), "Done")
        (url, headers, first), (_, _, second) = self.requests()
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["X-api-key"], "test-key")
        self.assertEqual(headers["Anthropic-version"], "2023-06-01")
        self.assertEqual(first["system"], "System")
        self.assertEqual(first["max_tokens"], 2048)
        self.assertEqual(first["tools"][0]["input_schema"], SCHEMA)
        self.assertEqual(second["messages"][1:], [
            {"role": "assistant", "content": blocks},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"answer":1}'},
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": '{"answer":2}'},
            ]},
        ])

    def test_gemini_preserves_signed_parts_and_matches_call_ids(self):
        parts = [
            {"functionCall": {"id": "A", "name": "lookup", "args": {"q": "A"}},
             "thoughtSignature": "opaque"},
            {"functionCall": {"name": "lookup", "args": {"q": "B"}}},
        ]
        self.replies(
            {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]},
            {"candidates": [{"content": {"parts": [
                {"text": "internal", "thought": True}, {"text": "Done"},
            ]}}]},
        )
        execute = Mock(side_effect=[{"answer": 1}, 2])
        profile = self.profile(api.GEMINI, model="models/model-name")
        self.assertEqual(api.generate(profile, "System", "Question", tools=[TOOL],
                                      execute=execute, max_turns=2, max_tokens=512), "Done")
        (url, headers, first), (_, _, second) = self.requests()
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/model-name:generateContent")
        self.assertEqual(headers["X-goog-api-key"], "test-key")
        self.assertEqual(first["systemInstruction"], {"parts": [{"text": "System"}]})
        self.assertEqual(first["generationConfig"], {"maxOutputTokens": 512})
        self.assertEqual(first["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"], SCHEMA)
        self.assertEqual(second["contents"][1:], [
            {"role": "model", "parts": parts},
            {"role": "user", "parts": [
                {"functionResponse": {"id": "A", "name": "lookup", "response": {"answer": 1}}},
                {"functionResponse": {"name": "lookup", "response": {"result": 2}}},
            ]},
        ])

    def test_text_only_local_request_ignores_proxy_environment(self):
        self.replies({"content": [{"type": "text", "text": "text"}]})
        profile = self.profile(api.ANTHROPIC, base_url="http://127.0.0.1:8000/root", api_key="")
        self.assertEqual(api.generate(profile, "System", "Question"), "text")
        [(url, headers, body)] = self.requests()
        self.assertEqual(url, "http://127.0.0.1:8000/root/messages")
        self.assertNotIn("X-api-key", headers)
        self.assertNotIn("tools", body)
        proxy, redirects = self.build_opener.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirects.redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_http_status_keeps_url_and_provider_message_private(self):
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://example.com/private", 401, "provider message", {}, io.BytesIO(b"private key"),
        )
        with self.assertRaisesRegex(api.ProviderError, "^模型服务认证失败（HTTP 401）。") as failure:
            api.generate(self.profile(api.GEMINI), "", "Question")
        self.assertTrue(failure.exception.__suppress_context__)

    def test_never_executes_a_call_on_the_final_turn(self):
        self.replies({"output": [{"type": "function_call", "name": "lookup",
                                  "call_id": "A", "arguments": "{}"}]})
        execute = Mock()
        with self.assertRaises(api.ProviderError):
            api.generate(self.profile(api.OPENAI), "", "Question", tools=[TOOL], execute=execute)
        execute.assert_not_called()

    def test_unknown_tool_prevents_all_calls_in_the_batch(self):
        self.replies({"output": [
            {"type": "function_call", "name": "lookup", "call_id": "A", "arguments": "{}"},
            {"type": "function_call", "name": "unknown", "call_id": "B", "arguments": "{}"},
        ]})
        execute = Mock()
        with self.assertRaises(api.ProviderError):
            api.generate(self.profile(api.OPENAI), "", "Question", tools=[TOOL],
                         execute=execute, max_turns=2)
        execute.assert_not_called()

    def test_cancellation_and_size_limits(self):
        with self.assertRaisesRegex(api.ProviderError, "已取消"):
            api.generate(self.profile(api.OPENAI), "", "Question", cancelled=lambda: True)
        with patch.object(api, "MAX_REQUEST_BYTES", 1), self.assertRaisesRegex(api.ProviderError, "大小限制"):
            api.generate(self.profile(api.OPENAI), "", "Question")
        self.opener.open.assert_not_called()
        with patch.object(api, "MAX_REPLY_BYTES", 1), self.assertRaisesRegex(api.ProviderError, "读取范围"):
            self.replies({"output": []})
            api.generate(self.profile(api.OPENAI), "", "Question")

    def test_rejects_invalid_gemini_path_and_remote_cleartext(self):
        for profile in (
            self.profile(api.GEMINI, model="models/../other"),
            self.profile(api.OPENAI, base_url="http://example.com"),
        ):
            with self.subTest(profile=profile), self.assertRaises(api.ProviderError):
                api.generate(profile, "", "Question")
        self.opener.open.assert_not_called()

    def test_discovery_uses_native_headers_paths_and_pagination(self):
        self.replies(
            {"data": [{"id": "claude-sonnet-4-6", "display_name": "Sonnet"}],
             "has_more": True, "last_id": "claude-sonnet-4-6"},
            {"data": [{"id": "claude-opus-4-6", "display_name": "test-key"},
                      {"id": "test-key"}, {"id": "../../invalid"}], "has_more": False},
        )
        result = api.discover_models(self.profile(api.ANTHROPIC))
        self.assertEqual([(row["id"], row["name"]) for row in result["models"]], [
            ("claude-sonnet-4-6", "Sonnet"), ("claude-opus-4-6", "claude-opus-4-6"),
        ])
        self.assertIn("max", result["models"][0]["reasoning_efforts"])
        self.assertFalse(result["truncated"])
        first, second = (call.args[0] for call in self.opener.open.call_args_list)
        self.assertEqual(first.method, "GET")
        self.assertEqual(first.get_header("X-api-key"), "test-key")
        self.assertEqual(first.get_header("Anthropic-version"), "2023-06-01")
        url = urllib.parse.urlsplit(second.full_url)
        self.assertEqual(url.path, "/v1/models")
        self.assertEqual(urllib.parse.parse_qs(url.query)["after_id"], ["claude-sonnet-4-6"])

    def test_gemini_discovery_filters_and_stops_repeated_cursor(self):
        self.replies(
            {"models": [
                {"name": "models/gemini-3-pro-preview", "displayName": "Gemini",
                 "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
            ], "nextPageToken": "page+2"},
            {"models": [], "nextPageToken": "page+2"},
        )
        result = api.discover_models(self.profile(api.GEMINI))
        self.assertEqual(result, {"models": [
            {"id": "gemini-3-pro-preview", "name": "Gemini", "reasoning_efforts": ["", "low", "high"]},
        ], "truncated": True})
        first, second = (call.args[0] for call in self.opener.open.call_args_list)
        self.assertEqual(first.get_header("X-goog-api-key"), "test-key")
        self.assertIn("pageToken=page%2B2", second.full_url)
        self.assertNotIn("test-key", second.full_url)

    def test_openai_discovery_caps_size_without_following_upstream_links(self):
        self.replies({"data": [{"id": "gpt-5.2"}, {"id": "gpt-4.1"}],
                      "next": "https://other.example/v1/models"})
        with patch.object(api, "MAX_DISCOVERED_MODELS", 1):
            result = api.discover_models(self.profile(api.OPENAI))
        self.assertEqual([model["id"] for model in result["models"]], ["gpt-5.2"])
        self.assertTrue(result["truncated"])
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.opener.open.assert_called_once()

    def test_explicit_reasoning_uses_each_native_payload(self):
        cases = [
            (api.OPENAI, "gpt-5.2", "xhigh",
             {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Done"}]}]},
             {"reasoning": {"effort": "xhigh"}}),
            (api.ANTHROPIC, "claude-sonnet-4-6", "high",
             {"content": [{"type": "text", "text": "Done"}]},
             {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
            (api.GEMINI, "gemini-3-flash-preview", "minimal",
             {"candidates": [{"content": {"parts": [{"text": "Done"}]}}]},
             {"generationConfig": {"maxOutputTokens": 2048, "thinkingConfig": {"thinkingLevel": "MINIMAL"}}}),
            (api.GEMINI, "gemini-2.5-flash", "medium",
             {"candidates": [{"content": {"parts": [{"text": "Done"}]}}]},
             {"generationConfig": {"maxOutputTokens": 2048, "thinkingConfig": {"thinkingBudget": 1024}}}),
        ]
        for protocol, model, effort, reply, expected in cases:
            with self.subTest(protocol=protocol, model=model):
                self.opener.reset_mock()
                self.replies(reply)
                profile = self.profile(protocol, model=model, reasoning_effort=effort)
                self.assertEqual(api.generate(profile, "", "Question"), "Done")
                [(_, _, body)] = self.requests()
                self.assertEqual({name: body[name] for name in expected}, expected)

    def test_official_preset_protocol_mappings(self):
        cases = [
            (api.ANTHROPIC, "https://api.anthropic.com", "/v1/messages", "claude-haiku-4-5", "high",
             {"content": [{"type": "text", "text": "OK"}]},
             {"thinking": {"type": "enabled", "budget_tokens": 4096}}),
            (api.ANTHROPIC, "https://api.deepseek.com/anthropic", "/v1/messages", "deepseek-flash", "none",
             {"content": [{"type": "text", "text": "OK"}]},
             {"thinking": {"type": "disabled"}}),
            (api.CHAT, "https://generativelanguage.googleapis.com/v1beta/openai/", "/chat/completions", "gemini-4-hydrogen", "xhigh",
             {"choices": [{"message": {"content": "OK"}}]}, {"reasoning_effort": "xhigh"}),
        ]
        for protocol, base, path, model, effort, reply, expected in cases:
            with self.subTest(model=model):
                self.opener.reset_mock()
                self.replies(reply)
                profile = self.profile(protocol, base_url=base, model=model, reasoning_effort=effort)
                self.assertEqual(api.generate(profile, "", "Question"), "OK")
                [(url, _, body)] = self.requests()
                self.assertEqual(url, base.rstrip("/") + path)
                self.assertEqual({key: body[key] for key in expected}, expected)
                if model == "claude-haiku-4-5":
                    self.assertGreater(body["max_tokens"], body["thinking"]["budget_tokens"])
                    self.assertNotIn("output_config", body)

    def test_unsupported_native_reasoning_rejected_before_request(self):
        for protocol, model, effort in (
            (api.OPENAI, "gpt-4.1", "high"),
            (api.ANTHROPIC, "claude-sonnet-4-6", "xhigh"),
            (api.GEMINI, "gemini-3-pro-preview", "medium"),
        ):
            with self.subTest(protocol=protocol), self.assertRaisesRegex(api.ProviderError, "推理强度"):
                api.generate(self.profile(protocol, model=model, reasoning_effort=effort), "", "Question")
        self.opener.open.assert_not_called()

    def test_deepseek_discovery_offers_only_canonical_efforts(self):
        self.replies({"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-flash"}]})
        profile = self.profile(api.OPENAI, base_url="https://api.deepseek.com")
        self.assertEqual(api.discover_models(profile)["models"], [
            {"id": model, "name": model, "reasoning_efforts": ["", "none", "low", "high", "max"]}
            for model in ("deepseek-v4-pro", "deepseek-flash")
        ])
        self.assertIn("xhigh", api.reasoning_efforts(api.OPENAI, "deepseek-v4-pro", "https://gateway.example/v1"))

    def test_discovery_reports_declared_efforts_separately_from_fallback(self):
        self.replies(
            {"data": [{"id": "deepseek-flash", "effort": {"supported_levels": ["high", "max"]}}]},
            {"data": [
                {"id": "claude-new", "capabilities": {"effort": {
                    "supported": True, "low": {"supported": False}, "xhigh": {"supported": True},
                }}},
                {"id": "claude-opus-4-6", "capabilities": {"effort": {"supported": False}}},
                {"id": "claude-sonnet-4-6", "capabilities": {"effort": {"supported": True}}},
            ]},
            {"models": [
                {"name": "models/gemini-3-pro-preview", "supportedGenerationMethods": ["generateContent"],
                 "thinking": False},
                {"name": "models/gemini-3-flash-preview", "supportedGenerationMethods": ["generateContent"],
                 "thinking": True},
            ]},
        )
        [deepseek] = api.discover_models(self.profile(api.OPENAI, base_url="https://api.deepseek.com"))["models"]
        self.assertEqual(deepseek["discovered_reasoning_levels"], ["none", "high", "max"])
        self.assertEqual(deepseek["reasoning_efforts"], ["", "none", "high", "max"])
        declared, disabled, fallback = api.discover_models(self.profile(api.ANTHROPIC))["models"]
        self.assertEqual(declared["discovered_reasoning_levels"], ["xhigh"])
        self.assertEqual(declared["reasoning_efforts"], ["", "xhigh"])
        self.assertEqual(disabled["discovered_reasoning_levels"], [])
        self.assertEqual(disabled["reasoning_efforts"], [""])
        self.assertNotIn("discovered_reasoning_levels", fallback)
        self.assertIn("max", fallback["reasoning_efforts"])
        disabled, fallback = api.discover_models(self.profile(api.GEMINI))["models"]
        self.assertEqual(disabled["discovered_reasoning_levels"], [])
        self.assertEqual(disabled["reasoning_efforts"], [""])
        self.assertNotIn("discovered_reasoning_levels", fallback)
        self.assertIn("high", fallback["reasoning_efforts"])

    def test_gpt_62_documented_efforts_are_available_without_metadata(self):
        for model in ("gpt-6.2-sol", "gpt-6.2-astra"):
            with self.subTest(model=model):
                self.assertEqual(api.reasoning_efforts(api.OPENAI, model),
                                 ("", "low", "medium", "high", "xhigh", "max"))

    def test_generation_validates_effort_against_override_then_discovery(self):
        profile = self.profile(api.ANTHROPIC, model="claude-sonnet-4-6", reasoning_effort="xhigh",
                               discovered_reasoning_levels=["xhigh"])
        for rejected in (dict(profile, reasoning_effort="medium"), dict(profile, reasoning_level_override=[]),
                         dict(profile, reasoning_level_override=["ultra"])):
            with self.assertRaisesRegex(api.ProviderError, "推理强度"):
                api.generate(rejected, "", "Question")
        self.opener.open.assert_not_called()
        reply = {"content": [{"type": "text", "text": "Done"}]}
        self.replies(reply, reply)
        self.assertEqual(api.generate(profile, "", "Question"), "Done")
        manual = dict(profile, reasoning_effort="low", reasoning_level_override=["low"])
        self.assertEqual(api.generate(manual, "", "Question"), "Done")
        self.assertEqual([body["output_config"] for _, _, body in self.requests()],
                         [{"effort": "xhigh"}, {"effort": "low"}])

    def test_manual_effort_levels_are_strict_and_preserve_canonical_order(self):
        self.assertEqual(api.normalize_effort_levels(api.OPENAI, "https://api.deepseek.com",
                                                     ["medium", "minimal", "low", "xhigh"]), ["high", "low"])
        for invalid in ("low", [""], [None], ["ultra"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                api.normalize_effort_levels(api.OPENAI, "", invalid)

    def test_deepseek_sends_canonical_efforts_and_omits_default(self):
        for effort, expected in (("", None), ("none", "none"), ("minimal", "low"), ("medium", "high"),
                                 ("xhigh", "high"), ("max", "max")):
            with self.subTest(effort=effort):
                self.opener.reset_mock()
                self.replies({"output": [{"type": "message", "content": [{"type": "output_text", "text": "Done"}]}]})
                profile = self.profile(api.OPENAI, base_url="https://api.deepseek.com", model="deepseek-v4-pro",
                                       reasoning_effort=effort)
                self.assertEqual(api.generate(profile, "", "Question"), "Done")
                [(url, _, body)] = self.requests()
                self.assertEqual(url, "https://api.deepseek.com/responses")
                self.assertEqual(body.get("reasoning"), {"effort": expected} if expected is not None else None)


if __name__ == "__main__":
    unittest.main()

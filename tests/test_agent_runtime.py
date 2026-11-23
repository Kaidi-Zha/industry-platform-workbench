import json
import os
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from industplatform import codex_agent, codex_gateway, codex_local, domain_libraries, guarded_workflow, provider_api, scope_guard, workflow
from industplatform.agent_runtime import AgentRuntimeRouter, CodexAppServerAdapter
from industplatform.model_profiles import ModelProfiles
from industplatform.server import RequestHandler, chat_turn
from industplatform.settings import Settings
from industplatform.storage import RunStore


MODEL = {"id": "fixture-catalog-id", "model": "gpt-fixture", "displayName": "Codex Fixture", "isDefault": True,
         "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}],
         "defaultReasoningEffort": "medium"}
FIXTURE = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli 0.166.0")
    raise SystemExit

options = {}
for index, flag in enumerate(sys.argv[:-1]):
    if flag == "-c":
        key, value = sys.argv[index + 1].split("=", 1)
        options[key] = json.loads(value)
local_server = next((name.split(".")[1] for name in options if name.startswith("mcp_servers.industplatform_")), "")

def log(row):
    with Path(os.environ["INDUST_RUNTIME_TRACE"]).open("a", encoding="utf-8") as stream:
        print(json.dumps(row, ensure_ascii=False), file=stream)

def emit(row):
    print(json.dumps(row), flush=True)

log({"pid": os.getpid(), "home": os.environ["CODEX_HOME"], "sqlite_home": os.environ["CODEX_SQLITE_HOME"],
     "api_key": "OPENAI_API_KEY" in os.environ, "override_url": "OPENAI_BASE_URL" in os.environ,
     "credential_override": "cli_auth_credentials_store" in options,
     "other_mcp_disabled": options.get("mcp_servers.personal.enabled") is False,
     "project_mcp_disabled": options.get("mcp_servers.industplatform.enabled") is False and
                             bool(options.get("mcp_servers.industplatform.command"))})

for line in sys.stdin:
    request = json.loads(line)
    log(request)
    method = request.get("method")
    params = request.get("params") or {}
    identity = request.get("id")
    if identity is None:
        continue
    if method == "account/read":
        account = os.environ.get("INDUST_RUNTIME_ACCOUNT")
        kind = "apiKey" if account == "api" else "chatgpt"
        result = {"account": None if account == "none" else {
            "type": kind, "email": "fixture@example.test", "planType": "plus", "private": "fixture-credential"}}
    elif method == "model/list":
        emit({"method": "account/updated", "params": {}})
        emit({"id": identity + 1000, "result": {"data": []}})
        pages = json.loads(os.environ.get("INDUST_RUNTIME_CATALOG", "null"))
        result = pages[params.get("cursor") or ""] if pages is not None else {"data": [__MODEL__], "nextCursor": None}
        if "error" in result:
            emit({"id": identity, "error": result["error"]})
            continue
    elif method == "config/read":
        servers = {local_server: {"enabled": True}} if local_server else {}
        if options.get("mcp_servers.personal.enabled") is not False or os.environ.get("INDUST_RUNTIME_FOREIGN"):
            servers["personal"] = {"enabled": True}
        if params.get("cwd") and options.get("mcp_servers.industplatform.enabled") is not False:
            servers["industplatform"] = {"enabled": True, "command": "repo-fixture"}
        result = {"config": {"model_provider": options["model_provider"],
                             "chatgpt_base_url": options["chatgpt_base_url"],
                             "openai_base_url": options["openai_base_url"], "mcp_servers": servers}}
    elif method == "thread/start":
        result = {"thread": {"id": "a" * 36, "ephemeral": not os.environ.get("INDUST_RUNTIME_PERSISTENT")},
                  "model": params["model"], "modelProvider": "openai",
                  "sandbox": {"type": "readOnly", "networkAccess": False}}
    elif method == "turn/start":
        if os.environ.get("INDUST_RUNTIME_BAD_PARAMS"):
            emit({"id": identity, "error": {"code": -32602, "message": "fixture parameter rejected"}})
            continue
        if os.environ.get("INDUST_RUNTIME_EXIT"):
            print("fixture startup diagnostic authorization=fixture-private-key", file=sys.stderr, flush=True)
            raise SystemExit(23)
        emit({"id": identity, "result": {"turn": {"id": "turn-1"}}})
        if os.environ.get("INDUST_RUNTIME_CHILD"):
            child = "fixture-child"
            emit({"method": "thread/started", "params": {"thread": {"id": child, "parentThreadId": "a" * 36}}})
            call = {"type": "mcpToolCall", "id": "child-tool", "server": local_server, "tool": "list_domains"}
            emit({"method": "item/started", "params": {"threadId": child, "turnId": "child-turn", "item": call}})
            emit({"method": "item/completed", "params": {"threadId": child, "turnId": "child-turn", "item": {
                **call, "status": "completed", "durationMs": 37}}})
            emit({"method": "item/completed", "params": {"threadId": "foreign-thread", "turnId": "foreign-turn", "item": {
                **call, "status": "completed", "durationMs": 99}}})
            emit({"method": "turn/completed", "params": {"threadId": child, "turn": {"status": "completed"}}})
        if os.environ.get("INDUST_RUNTIME_TURN_FAILURE"):
            origin = {"threadId": "a" * 36, "turnId": "turn-1"}
            delta = {**origin, "itemId": "reasoning-1", "summaryIndex": 0}
            emit({"method": "item/reasoning/textDelta", "params": {**delta, "delta": "private chain of thought"}})
            emit({"method": "item/reasoning/summaryTextDelta", "params": {**delta, "delta": "先读取资料库"}})
            emit({"method": "item/completed", "params": {**origin, "item": {
                "id": "reasoning-1", "type": "reasoning", "summary": ["复用已有资料库并核对范围。"],
                "content": ["private reasoning body"]}}})
            error = {"message": "Reconnecting…", "additionalDetails": "401 Unauthorized, wss://api.openai.com/v1/responses, missing api.responses.write",
                     "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 401}}}
            emit({"method": "error", "params": {**origin, "willRetry": True, "error": error}})
            emit({"method": "turn/completed", "params": {**origin, "turn": {"status": "failed", "error": error}}})
            continue
        if os.environ.get("INDUST_RUNTIME_APPROVE"):
            emit({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {
                "command": "echo fixture", "threadId": "a" * 36, "availableDecisions": ["accept", "decline"]}})
            log(json.loads(sys.stdin.readline()))
        emit({"method": "item/completed", "params": {"threadId": "a" * 36,
               "item": {"type": "agentMessage", "text": "订阅执行完成"}}})
        emit({"method": "turn/completed", "params": {"threadId": "a" * 36, "turn": {"status": "completed"}}})
        continue
    else:
        result = {}
    emit({"id": identity, "result": result})
""".replace("__MODEL__", repr(MODEL))


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = RunStore(self.root / "workbench.sqlite3")
        self.settings = Settings(self.store)
        self.profiles = ModelProfiles()
        self.handler = object.__new__(RequestHandler)
        self.handler.server = SimpleNamespace(store=self.store, settings=self.settings, profiles=self.profiles)

    def local_fixture(self):
        binary = self.root / "codex"
        binary.write_text(FIXTURE, encoding="utf-8")
        binary.chmod(0o700)
        self.native_home = self.root / ".codex"
        self.native_home.mkdir()
        self.native_config = self.native_home / "config.toml"
        self.native_config.write_text("# existing configuration\n", encoding="utf-8")
        self.trace = self.root / "rpc.jsonl"
        self.settings.update({"codex_executable": str(binary)})
        self.detector = codex_local.CodexLocalDetector()
        @contextmanager
        def private_mcp(profile, *, tools, max_turns, scoped, **_):
            gateway = codex_gateway.Gateway(profile, tools, max_turns, scoped=scoped)
            gateway.port = 18888
            yield gateway

        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.native_home), "OPENAI_API_KEY": "fixture-private-key",
            "OPENAI_BASE_URL": "https://wrong.example.test", "INDUST_RUNTIME_TRACE": str(self.trace)})
        for context in (environment, patch.object(codex_local, "DETECTOR", self.detector),
                        patch.object(codex_local, "prepare_codex_state", return_value=self.root),
                        patch.object(codex_gateway, "serve", side_effect=private_mcp)):
            context.start()
            self.addCleanup(context.stop)
        return binary

    def messages(self):
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_probes_and_locks_native_transport(self):
        binary = self.local_fixture()
        self.assertEqual(self.detector.cached(str(binary))["status"], "unchecked")
        probe = self.detector.detect(str(binary))
        status = self.detector.cached(str(binary))
        self.assertEqual((probe.version, status["status"], status["plan"]), ("0.166.0", "ready_subscription", "plus"))
        self.assertEqual((status["binary_path"], status["catalog_source"]), (str(binary), "app-server model/list"))
        self.assertEqual(status["version_notice"], "")
        self.assertNotIn("fixture-credential", json.dumps(status))
        self.assertEqual(status["models"][0]["reasoning_efforts"], ["medium", "high"])
        code, run = self.handler.write_route("/api/runs", {"execution_mode": codex_local.SUBSCRIPTION,
            "prompt": "复核工序", "subscription_model_id": "gpt-fixture", "reasoning_effort": "high"})
        self.assertEqual(code, 202)
        self.assertEqual(chat_turn(run)["subscription_model"]["id"], MODEL["id"])
        self.assertNotIn("fixture@example.test", json.dumps(run))
        runtime = AgentRuntimeRouter.for_run(self.store, self.store.get(run["id"]))
        with patch.dict(os.environ, {"INDUST_RUNTIME_APPROVE": "1"}):
            inbox = codex_agent.ApprovalInbox()
            stop = threading.Event()
            decisions = []
            def reviewer():
                for _ in range(200):
                    if approval := inbox.snapshot():
                        decisions.append(approval["can_approve"])
                        inbox.resolve(approval["id"], "decline")
                        return
                    stop.wait(.01)
                stop.set()
            review_thread = threading.Thread(target=reviewer, daemon=True)
            review_thread.start()
            answer = runtime.execute("test", 18765, lambda *_: None, stop,
                tools=(guarded_workflow.ROUTER_TOOLS, lambda *_: {}), scoped=True, approvals=inbox,
                permissions={"approval_mode": "ask"})
            review_thread.join(timeout=3)
        self.assertEqual((answer, decisions), ("订阅执行完成", [False]))
        launches = [row for row in self.messages() if "home" in row]
        self.assertTrue(all(row["home"] == str(self.native_home.resolve()) for row in launches))
        self.assertTrue(all(row["sqlite_home"] != row["home"] and not row["api_key"] and not row["override_url"]
                            and not row["credential_override"] for row in launches))
        self.assertTrue(launches[-1]["other_mcp_disabled"])
        self.assertTrue(launches[-1]["project_mcp_disabled"])
        messages = self.messages()
        thread = next(row["params"] for row in messages if row.get("method") == "thread/start")
        self.assertEqual((thread["ephemeral"], thread["modelProvider"], thread["model"]), (True, "openai", "gpt-fixture"))
        self.assertFalse(thread["allowProviderModelFallback"])
        self.assertEqual((thread["sandbox"], thread["approvalPolicy"]), ("read-only", "on-request"))
        turn = next(row["params"] for row in messages if row.get("method") == "turn/start")
        self.assertEqual((turn["effort"], turn["serviceTierForTurn"]), ("high", "default"))
        self.assertEqual(turn["summary"], "auto")
        self.assertEqual(codex_local.NATIVE_SETTINGS["openai_base_url"], "https://chatgpt.com/backend-api/codex")
        self.assertIn({"id": "approval", "result": {"decision": "decline"}}, messages)
        self.assertEqual(self.native_config.read_text(), "# existing configuration\n")
        self.assertFalse((self.native_home / "sessions").exists())

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_scope_failure_keeps_public_summary_and_explicit_cause(self):
        self.local_fixture()
        with patch.dict(os.environ, {"INDUST_RUNTIME_TURN_FAILURE": "1"}):
            status, run = self.handler.write_route("/api/runs", {"execution_mode": codex_local.SUBSCRIPTION,
                "prompt": "查看 PCB 资料库", "subscription_model_id": MODEL["id"]})
            self.assertEqual(status, 202)
            workflow.process_run(self.store, self.store.claim(), 18765)
        saved = chat_turn(self.store.get(run["id"]))
        self.assertEqual(saved["status"], "needs_review")
        self.assertIn("api.responses.write", saved["error"])
        self.assertTrue(saved["can_resume"])
        stage, = saved["result"]["stage_timeline"]
        self.assertEqual((stage["stage_key"], stage["status"], stage["activity"]["state"]), ("routing", "failed", "retrying"))
        summaries = [item["preview"] for item in stage["events"] if item["kind"] == "reasoning"]
        self.assertEqual(summaries, ["复用已有资料库并核对范围。"])
        self.assertEqual(stage["thread_id"], "a" * 36)
        self.assertEqual(stage["error"]["http_status"], 401)
        self.assertEqual(stage["error"]["kind"], "stage_error")
        self.assertNotIn("private", json.dumps(stage))

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_reports_json_rpc_and_stderr_exit_diagnostics(self):
        self.local_fixture()
        cases = (("INDUST_RUNTIME_BAD_PARAMS", -32602, "method", "turn/start"),
                 ("INDUST_RUNTIME_EXIT", "CODEX_APP_SERVER_EXIT", "exit_code", 23))
        for flag, code, field, value in cases:
            with self.subTest(flag=flag), patch.dict(os.environ, {flag: "1"}):
                _, run = self.handler.write_route("/api/runs", {"execution_mode": codex_local.SUBSCRIPTION,
                    "prompt": "复核", "subscription_model_id": MODEL["id"]})
                workflow.process_run(self.store, self.store.claim(), 18765)
                stage, = chat_turn(self.store.get(run["id"]))["result"]["stage_timeline"]
                self.assertEqual(stage["status"], "failed")
                self.assertEqual((stage["error"]["code"], stage["error"][field]), (code, value))
                if field == "exit_code":
                    self.assertIn("fixture startup diagnostic", stage["error"]["stderr"])
                    self.assertNotIn("fixture-private-key", stage["error"]["stderr"])

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_attributes_owned_child_tools_only(self):
        self.local_fixture()
        probe = self.detector.detect(self.settings.get()["codex_executable"])
        adapter = CodexAppServerAdapter(self.settings.get(), probe.models[0])
        events = []
        with patch.dict(os.environ, {"INDUST_RUNTIME_CHILD": "1"}):
            adapter.execute("fixture", 18765, lambda *event: events.append(event), None,
                            tools=(guarded_workflow.ROUTER_TOOLS, lambda *_: {}), scoped=True)
        tools = [event for name, event, _ in events if name == "harness.item" and event.get("kind") == "tool"]
        self.assertEqual([(item["status"], item["subagent_id"], item["parent_turn_id"]) for item in tools],
                         [("running", "fixture-child", "turn-1"), ("completed", "fixture-child", "turn-1")])
        self.assertNotIn("foreign-thread", json.dumps(events))

    def test_model_retry_api_keeps_the_conversation_and_scope(self):
        stage = scope_guard.STAGES[1]
        with closing(self.store.connect()) as db, db:
            domain = domain_libraries.register(db, "PCB")
        run = self.store.create(codex_agent.ROUTE, "offline", "复核 PCB 工序",
                                scope_request={"domain_id": str(domain), "target": {"stage": stage}, "read_only": True})

        def fail_execution(_, _port, _event, _stop, **options):
            tools = {row["name"] for row in options["tools"][0]}
            if "list_domains" in tools:
                return json.dumps({"target_stage": stage, "domain_id": str(domain), "router_confidence": .99})
            raise codex_agent.AgentError("模拟模型连接中断", code="CONNECTION_LOST")

        with patch.object(codex_agent, "execute", side_effect=fail_execution):
            workflow.process_run(self.store, self.store.claim(), 18765)
        previous = self.store.get(run["id"])
        attempt = previous["result"]["stage_timeline"][-1]
        profile = self.profiles.save({"protocol": provider_api.OPENAI, "name": "Fixture", "model": "retry-fixture",
                                      "base_url": "https://example.test/v1", "api_key": "fixture-retry-key"})
        self.handler.server.workers = workflow.Workers(self.store, count=0)
        code, queued = self.handler.write_route("/api/runs/" + run["id"] + "/scope",
            {"action": "retry_stage", "attempt_id": attempt["id"], "execution_mode": codex_local.API_GATEWAY,
             "model_profile_id": profile["id"]})
        self.assertEqual((code, queued["status"]), (202, "queued"))
        self.assertEqual(queued["execution_scope"]["contract_id"], previous["execution_scope"]["contract_id"])
        self.assertNotIn("fixture-retry-key", json.dumps(queued))
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "只读复核完成"}]}]}
        with patch.object(provider_api, "_post", return_value=response) as provider:
            workflow.process_run(self.store, self.store.claim(), 18765, profiles=self.profiles)
        final = self.store.get(run["id"])
        self.assertEqual(provider.call_args.args[3]["model"], "retry-fixture")
        self.assertEqual((final["status"], final["conversation_id"]), ("completed", previous["conversation_id"]))
        self.assertEqual([entry["stage_key"] for entry in final["result"]["stage_timeline"]],
                         ["routing", "stage_2", "stage_2"])

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_automatic_discovery_uses_the_highest_semver(self):
        oldest = self.local_fixture()
        variants = []
        for version in ("0.167.0", "0.171.0-alpha.9", "0.171.0-alpha.13", "0.171.0+test"):
            candidate = self.root / version / "codex"
            candidate.parent.mkdir()
            candidate.write_text(FIXTURE.replace("codex-cli 0.166.0", "codex-cli " + version), encoding="utf-8")
            candidate.chmod(0o700)
            variants.append(candidate)
        paths = [oldest, *variants[:-1]]
        with patch.object(codex_local, "_candidate_paths", return_value=paths):
            first = self.detector.detect()
            self.assertEqual((first.binary, first.version), (str(variants[-2]), "0.171.0-alpha.13"))
            self.assertEqual(first.public()["version_notice"], "")
            paths.append(variants[-1])
            self.assertIs(self.detector.detect(), first)
            refreshed = self.detector.detect(refresh=True)
            self.assertEqual((refreshed.binary, refreshed.version), (str(variants[-1]), "0.171.0+test"))
            self.assertEqual(codex_local.find_binary(str(oldest)), codex_local.CodexInstallation(str(oldest), "0.166.0"))

    def test_candidates_include_all_path_entries_and_installed_daemon_release(self):
        first, second = self.root / "first", self.root / "second"
        native = self.root / "native"
        name = "codex.exe" if os.name == "nt" else "codex"
        release = native / "packages/app-server-daemon/releases/0.167.0/bin" / name
        release.parent.mkdir(parents=True)
        release.touch()
        with patch.object(os, "get_exec_path", return_value=[str(first), str(second)]), \
                patch.object(codex_local, "native_home", return_value=native):
            candidates = codex_local._candidate_paths(self.root, name)
        self.assertEqual(candidates[:2], [first / name, second / name])
        self.assertIn(release, candidates)

    def test_subscription_upgrade_notice_uses_the_minimum_cli_version(self):
        for version, needs_upgrade in (("0.165.9", True), ("0.166.0-alpha.1", True),
                                       ("0.166.0", False), ("0.171.0-alpha.13", False), ("", False)):
            with self.subTest(version=version):
                status = codex_local.NativeProbe("ready_subscription", version=version).public()
                self.assertEqual(bool(status["version_notice"]), needs_upgrade)

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_catalog_paginates_and_maps_both_effort_shapes(self):
        self.local_fixture()
        alternate = {"id": "alternative", "model": "gpt-6.2-astra", "displayName": "Second page model",
                     "isDefault": False, "defaultReasoningEffort": "low", "supportedReasoningEfforts": ["none", "low", "low"]}
        hidden = {**MODEL, "id": "hidden", "hidden": True}
        pages = {"": {"data": [MODEL, hidden], "nextCursor": "page-2"},
                 "page-2": {"data": [alternate], "nextCursor": None}}
        with patch.dict(os.environ, {"INDUST_RUNTIME_CATALOG": json.dumps(pages)}):
            probe = CodexAppServerAdapter(self.settings.get()).fetch_available_models()
        first, second = probe.public()["models"]
        self.assertEqual(first, {"id": MODEL["id"], "model": MODEL["model"], "name": MODEL["displayName"],
                                 "is_default": True, "reasoning_efforts": ["medium", "high"], "default_effort": "medium"})
        self.assertEqual(second, {"id": alternate["id"], "model": alternate["model"], "name": alternate["displayName"],
                                  "is_default": False, "reasoning_efforts": ["none", "low"], "default_effort": "low"})
        self.assertEqual(codex_local.select_model(probe, "alternative", None)["reasoning_effort"], "low")
        with self.assertRaisesRegex(codex_local.LocalCodexError, "不支持"):
            codex_local.select_model(probe, "alternative", "high")
        requests = [row for row in self.messages() if row.get("method")]
        self.assertEqual([row["method"] for row in requests[:3]], ["initialize", "initialized", "account/read"])
        self.assertEqual([row["params"] for row in requests if row["method"] == "model/list"], [
            {"cursor": cursor, "limit": 100, "includeHidden": False} for cursor in (None, "page-2")
        ])

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_force_refresh_uses_a_new_app_server(self):
        self.local_fixture()
        adapter = CodexAppServerAdapter(self.settings.get())
        before = adapter.fetch_available_models()
        pages = {"": {"data": [{**MODEL, "displayName": "New live directory"}]}}
        with patch.dict(os.environ, {"INDUST_RUNTIME_CATALOG": json.dumps(pages)}):
            self.assertIs(adapter.fetch_available_models(), before)
            refreshed = adapter.fetch_available_models(force_refresh=True)
        self.assertEqual(refreshed.models[0]["name"], "New live directory")
        launches = [row["pid"] for row in self.messages() if "pid" in row]
        self.assertEqual(len(launches), 2)
        self.assertNotEqual(*launches)

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_catalog_auth_required_clears_previous_and_partial_models(self):
        binary = self.local_fixture()
        adapter = CodexAppServerAdapter(self.settings.get())
        self.assertTrue(adapter.fetch_available_models().models)
        pages = {"": {"data": [MODEL], "nextCursor": "page-2"},
                 "page-2": {"error": {"code": -32001, "message": "Request rejected", "data": {"code": "auth_required"}}}}
        with patch.dict(os.environ, {"INDUST_RUNTIME_CATALOG": json.dumps(pages)}):
            self.assertEqual(adapter.fetch_available_models(force_refresh=True).status, "unauthenticated")
        state = self.detector.cached(str(binary))
        self.assertEqual(state["models"], [])
        self.assertNotIn("email", state)
        self.assertIn("登录", state["message"])

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_catalog_errors_and_missing_runtime_are_empty(self):
        self.local_fixture()
        adapter = CodexAppServerAdapter(self.settings.get())
        for page in ({"error": {"code": -32603, "message": "fixture internal error"}}, {"data": [], "nextCursor": None}):
            with self.subTest(page=page), patch.dict(os.environ, {"INDUST_RUNTIME_CATALOG": json.dumps({"": page})}):
                status = adapter.fetch_available_models(force_refresh=True).public()
                self.assertEqual(status["models"], [])
                self.assertIn("模型", status["catalog_error"])
        with patch.dict(os.environ, {"INDUST_RUNTIME_ACCOUNT": "none"}):
            state = adapter.fetch_available_models(force_refresh=True).public()
            self.assertEqual((state["status"], state["models"]), ("unauthenticated", []))
        with patch.object(codex_local, "find_binary", return_value=None):
            state = adapter.fetch_available_models(force_refresh=True).public()
            self.assertEqual((state["status"], state["models"]), ("cli_missing", []))

    @unittest.skipIf(os.name == "nt", "portable protocol fixture is an executable Python script")
    def test_subscription_rejects_changed_auth_or_isolation_before_user_prompt(self):
        binary = self.local_fixture()
        probe = self.detector.detect(str(binary))
        self.assertEqual(probe.status, "ready_subscription")
        adapter = AgentRuntimeRouter(codex_local.SUBSCRIPTION, preferences=self.settings.get())
        for flags, message in (({"INDUST_RUNTIME_ACCOUNT": "api"}, "API Key"),
                               ({"INDUST_RUNTIME_FOREIGN": "1"}, "隔离"),
                               ({"INDUST_RUNTIME_PERSISTENT": "1"}, "临时会话")):
            with self.subTest(flags=flags), patch.dict(os.environ, flags), self.assertRaisesRegex(codex_agent.AgentError, message):
                adapter.execute("not sent", 18765, lambda *_: None, None, tools=([], lambda *_: {}), scoped=True)
        self.assertFalse(any(row.get("method") == "turn/start" for row in self.messages()))

    def test_api_mode_routes_stage_tools_without_codex_and_preserves_context(self):
        with closing(self.store.connect()) as db, db:
            domain = domain_libraries.register(db, "PCB")
            db.execute("INSERT INTO process_nodes(domain_id,id,title,position) VALUES(?,?,?,?)", (domain, "cam", "CAM 核对", 1))
        codex_turn = self.store.create("codex-agent", "offline", "上一轮订阅任务",
                                      execution_mode=codex_local.SUBSCRIPTION,
                                      subscription_model={"id": "gpt-fixture", "name": "Codex Fixture", "reasoning_effort": "high"})
        claimed = self.store.claim()
        claimed["status"] = "completed"
        self.store.update(claimed)
        self.settings.update({"default_execution_mode": codex_local.SUBSCRIPTION})
        profile = self.profiles.save({"protocol": provider_api.OPENAI, "name": "Fixture", "model": "remote-fixture",
                                      "base_url": "https://example.test/v1", "api_key": "fixture-gateway-key"})
        stage = scope_guard.STAGES[1]
        def reply(text):
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

        def provider(_opener, url, _headers, payload, *_unused, **_options):
            self.assertEqual(url, "https://example.test/v1/responses")
            self.assertEqual(payload["model"], "remote-fixture")
            names = {entry["name"] for entry in payload["tools"]}
            activity = self.store.get(run["id"])["result"]["stage_timeline"][-1]["activity"]
            self.assertEqual(activity["state"], "waiting")
            if "list_domains" in names:
                self.assertEqual(names, {entry["name"] for entry in guarded_workflow.ROUTER_TOOLS})
                response = reply(json.dumps({"target_stage": stage, "domain_id": str(domain), "router_confidence": .99,
                                         "granularity": "PROCESS_NODE_ONLY", "allowed_process_node_ids": ["cam"],
                                         "rationale": "修订指定工序"}))
                response["output"].insert(0, {"type": "reasoning", "encrypted_content": "private-encrypted-replay",
                    "summary": [{"type": "summary_text", "text": "按指定工序识别范围。"}]})
                return response
            self.assertIn("upsert_process_node", names)
            self.assertNotIn("upsert_benchmark_task", names)
            if payload["input"][-1].get("type") == "function_call_output":
                self.assertEqual(json.loads(payload["input"][-1]["output"]), {"id": "cam", "title": "CAM 入厂核对"})
                return reply("已更新指定节点")
            return {"output": [{"type": "function_call", "name": "upsert_process_node", "call_id": "update-cam",
                "arguments": json.dumps({"domain_id": str(domain), "process_node_id": "cam", "patch_json": '{"title":"CAM 入厂核对"}'})}]}

        with patch.object(codex_local.DETECTOR, "detect", side_effect=AssertionError("unexpected account probe")), \
                patch("subprocess.Popen", side_effect=AssertionError("unexpected child process")), \
                patch.object(provider_api, "_post", side_effect=provider):
            status, run = self.handler.write_route("/api/runs", {"execution_mode": codex_local.API_GATEWAY,
                "model_profile_id": profile["id"], "prompt": "修改 CAM 工序名称",
                "conversation_id": codex_turn["conversation_id"], "scope_request": {"domain_id": str(domain),
                    "target": {"stage": stage, "process_node_id": "cam"}}})
            self.assertEqual(status, 202)
            workflow.process_run(self.store, self.store.claim(), 18765, connections=self.profiles.take(run["id"]))
        updated = self.store.get(run["id"])
        self.assertEqual((updated["status"], updated["execution_mode"]), ("completed", codex_local.API_GATEWAY))
        self.assertEqual(updated["result"]["scope_receipts"][0]["changes"][0]["tool"], "upsert_process_node")
        stages = updated["result"]["stage_timeline"]
        items = [event for stage in stages for event in stage["events"]]
        self.assertEqual([row["kind"] for row in items], ["reasoning", "tool"])
        self.assertEqual([row["stage_key"] for row in items], ["routing", "stage_2"])
        self.assertEqual(items[0]["preview"], "按指定工序识别范围。")
        self.assertNotIn("private-encrypted-replay", json.dumps(items))
        self.assertEqual(len(self.store.conversation(codex_turn["conversation_id"])), 2)
        self.assertEqual(self.settings.get()["default_execution_mode"], codex_local.SUBSCRIPTION)


    def test_provider_hidden_tools_trigger_audit_and_stop_before_next_call(self):
        with closing(self.store.connect()) as db, db:
            domain = domain_libraries.register(db, "PCB 制造")
            db.execute("INSERT INTO process_nodes(domain_id,id,title,position) VALUES(?,?,?,?)",
                       (domain, "cam", "CAM 核对", 1))
        profile = self.profiles.save({"protocol": provider_api.OPENAI, "name": "Fixture", "model": "remote-fixture",
                                      "base_url": "https://example.test/v1", "api_key": "fixture-gateway-key"})
        stage = scope_guard.STAGES[1]
        route_response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(
            {"target_stage": stage, "domain_id": str(domain), "router_confidence": .99, "rationale": "工艺图谱"}
        )}]}]}
        calls = ["save_raw_source", "promote_verified_ref", "enqueue_frontier", "upsert_process_node"]
        output = [{"type": "function_call", "name": name, "call_id": f"call-{i}", "arguments": json.dumps({
            "domain_id": str(domain), "process_node_id": "cam", "patch_json": '{"title":"不可修改"}',
        })} for i, name in enumerate(calls)]
        with patch.object(provider_api, "_post", side_effect=[route_response, {"output": output}]) as provider:
            status, run = self.handler.write_route("/api/runs", {"execution_mode": codex_local.API_GATEWAY,
                "model_profile_id": profile["id"], "prompt": "修改 CAM 工序名称", "scope_request": {
                    "domain_id": str(domain), "target": {"stage": stage, "process_node_id": "cam"}}})
            self.assertEqual(status, 202)
            workflow.process_run(self.store, self.store.claim(), 18765, connections=self.profiles.take(run["id"]))
        self.assertEqual(provider.call_count, 2)
        visible = {tool["name"] for tool in provider.call_args.args[3]["tools"]}
        self.assertFalse(visible & set(calls[:3]))
        self.assertIn(calls[-1], visible)
        failed = self.store.get(run["id"])["result"]["stage_timeline"][-1]
        self.assertEqual((failed["status"], failed["error"]["code"]), ("failed", scope_guard.GUARD_ABORT_CODE))
        self.assertEqual([entry["tool"] for entry in failed["receipt"]["rejections"]], calls[:3])
        self.assertEqual(failed["receipt"]["changes"], [])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT title FROM process_nodes WHERE id='cam'").fetchone()[0], "CAM 核对")

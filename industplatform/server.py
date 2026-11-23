"""HTTP API and static dashboard for a single local platform instance."""

import json
import logging
import re
import threading
import webbrowser
from contextlib import ExitStack, closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from . import __version__, codex_agent, codex_local, collection, industrial_bench, library, metrology
from . import asset_index, attachments, domain_export, domain_libraries, frontier, guarded_workflow, local_import, sandbox_verifier, workbench
from . import scope_guard, stage_timeline
from .agent_runtime import CodexAppServerAdapter
from .collaboration import COLLABORATION_PATH, IMPORTS_PATH, Collaboration, checkout
from .credentials import CredentialError
from .data_location import copy_database, location_file, persist_database, resolve_database
from .evaluation import EvaluationError, TIERS
from .forest import search_keywords
from .model_profiles import ModelProfiles, public_profile
from .provider_api import ProviderError
from .scope_guard import COLLECTION_STOPS, ENTITY_ID, EXECUTION_PACES, STAGES, validate_stage_queue
from .settings import AGENT_PREFERENCES, APPROVAL_MODES, PERMISSION_PREFERENCES, Settings
from .storage import AGENT_ROUTES, API_AGENT_ROUTE, RunStore
from .workflow import Workers, accept_request, benchmark_ready, benchmark_zip, catalog

logger = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
PAGES = {
    "/": ("workbench.html", "text/html; charset=utf-8"),
    "/static/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/static/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
    "/static/workbench-scope.js": ("workbench-scope.js", "text/javascript; charset=utf-8"),
    "/static/workbench-timeline.js": ("workbench-timeline.js", "text/javascript; charset=utf-8"),
    "/static/workbench-ui.js": ("workbench-ui.js", "text/javascript; charset=utf-8"),
    "/static/workbench-models.js": ("workbench-models.js", "text/javascript; charset=utf-8"),
    "/static/workbench-records.js": ("workbench-records.js", "text/javascript; charset=utf-8"),
    "/static/workbench-views.js": ("workbench-views.js", "text/javascript; charset=utf-8"),
    "/static/workbench-graph.js": ("workbench-graph.js", "text/javascript; charset=utf-8"),
    "/static/workbench-graph-layout.js": ("workbench-graph-layout.js", "text/javascript; charset=utf-8"),
    "/static/workbench-graph-render.js": ("workbench-graph-render.js", "text/javascript; charset=utf-8"),
    "/static/workbench-router.js": ("workbench-router.js", "text/javascript; charset=utf-8"),
    "/static/workbench-settings.js": ("workbench-settings.js", "text/javascript; charset=utf-8"),
    "/static/workbench-collaboration.js": ("workbench-collaboration.js", "text/javascript; charset=utf-8"),
    "/static/workbench-import.js": ("workbench-import.js", "text/javascript; charset=utf-8"),
    "/static/workbench-overview.js": ("workbench-overview.js", "text/javascript; charset=utf-8"),
    "/static/workbench-benchmarks.js": ("workbench-benchmarks.js", "text/javascript; charset=utf-8"),
    "/static/vendor/markdown-it-15.0.3.mjs": ("vendor/markdown-it-15.0.3.mjs", "text/javascript; charset=utf-8"),
    "/static/vendor/dagre-3.1.3.mjs": ("vendor/dagre-3.1.3.mjs", "text/javascript; charset=utf-8"),
    "/static/tokens.css": ("tokens.css", "text/css; charset=utf-8"),
}
SHELL_PATH = re.compile(r"/(?:global|domains)(?:/[^/]*){0,6}\Z")
RUN_PATH = re.compile(r"/api/runs/([0-9a-f]{32})(/benchmark)?\Z")
CONVERSATION_PATH = re.compile(r"/api/conversations/([0-9a-f]{32})\Z")
RUN_CONTROL_PATH = re.compile(r"/api/runs/([0-9a-f]{32})/control\Z")
RUN_APPROVAL_PATH = re.compile(r"/api/runs/([0-9a-f]{32})/approval\Z")
RUN_STEER_PATH = re.compile(r"/api/runs/([0-9a-f]{32})/steer\Z")
RUN_SCOPE_PATH = re.compile(r"/api/runs/([0-9a-f]{32})/scope\Z")
SCOPE_EXPORT_PATH = re.compile(r"/api/workbench/scope-exports/([0-9a-f]{32})\Z")
PLAN_REVISION_PATH = re.compile(r"/api/conversations/([0-9a-f]{32})/plan\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
PROFILE_PATH = "/api/model-profiles"
MODEL_ACTION_PATH = PROFILE_PATH + "/model"
SETTINGS_PATH = "/api/settings"
CODEX_LOCAL_PATH = "/api/codex-local"
STORAGE_PATH = "/api/storage/move"
REFERENCES_PATH = "/api/references"
DOMAINS_PATH = "/api/workbench/domains"
NODES_PATH = "/api/workbench/nodes"
SCHEMA_PATH = "/api/workbench/schema"
SOURCES_PATH = "/api/workbench/sources"
REFERENCE_PATH = "/api/workbench/reference"
FRONTIER_PATH = "/api/workbench/frontier"
VERIFIER_PATH = "/api/workbench/verifier"
TASK_VALUE_PATH = "/api/workbench/task-value"
DOMAIN_APPROVALS_PATH = "/api/workbench/network-approvals"
ATTACHMENTS_PATH = "/api/workbench/attachments"
VERIFIER_SAMPLE_PATH = "/api/workbench/verifier-sample"
BENCHMARKS_PATH = "/api/benchmarks"
BENCHMARK_CASES_PATH = BENCHMARKS_PATH + "/cases"
BENCHMARK_TEMPLATES_PATH = BENCHMARKS_PATH + "/templates"
PROVIDER_ACTIONS = (PROFILE_PATH + "/test", PROFILE_PATH + "/discover", PROFILE_PATH + "/probe")
WRITE_PATHS = ("/api/runs", SETTINGS_PATH, CODEX_LOCAL_PATH, STORAGE_PATH, PROFILE_PATH, PROFILE_PATH + "/remove", MODEL_ACTION_PATH,
               REFERENCES_PATH, BENCHMARKS_PATH, BENCHMARK_CASES_PATH, BENCHMARK_TEMPLATES_PATH,
               DOMAINS_PATH, NODES_PATH, SCHEMA_PATH, FRONTIER_PATH, VERIFIER_PATH, TASK_VALUE_PATH, DOMAIN_APPROVALS_PATH,
               COLLABORATION_PATH, IMPORTS_PATH)


class WorkspaceBusy(ValueError):
    """The original workspace has unfinished jobs."""


def workspace(server, path):
    store = RunStore(path)
    profiles = ModelProfiles(store)
    settings = Settings(store)
    def audit_agent_finish():
        collaboration = getattr(server, "collaboration", None)
        if collaboration and collaboration.store is store:
            collaboration.audit_run()

    workers = Workers(store, port=server.server_port, profiles=profiles, on_agent_finish=audit_agent_finish)
    return store, profiles, settings, workers


def move_workspace(server, destination):
    collaboration = getattr(server, "collaboration", None)
    importer = getattr(server, "importer", None)
    with ExitStack() as locks:
        if collaboration:
            if not collaboration.lock.acquire(blocking=False):
                raise WorkspaceBusy("请等待资料同步或发布完成后更换存储位置")
            locks.callback(collaboration.lock.release)
        if importer:
            for lock in (importer.lock, importer.upload_lock):
                if not lock.acquire(blocking=False):
                    raise WorkspaceBusy("请等待正在整理的本地资料完成后更换存储位置")
                locks.callback(lock.release)
        _move_workspace(server, destination)


def _move_workspace(server, destination):
    if not isinstance(destination, str):
        raise ValueError("请输入新数据库文件的绝对路径")
    if Path(destination).resolve() == location_file(server.anchor).resolve():
        raise ValueError("目标路径与工作台定位文件冲突")
    if server.store.has_active_runs():
        raise WorkspaceBusy("请等待正在排队或执行的任务结束后再更换存储位置")
    target = copy_database(server.store.path, destination)
    replacement = workspace(server, target)
    replacement[-1].start()
    try:
        persist_database(server.anchor, target)
    except (OSError, RuntimeError, ValueError):
        replacement[-1].close()
        raise
    server.workers.close()
    server.store, server.profiles, server.settings, server.workers = replacement
    if collaboration := getattr(server, "collaboration", None):
        collaboration.close()
        server.collaboration = Collaboration(server.store, collaboration.repo, maintainer=collaboration.maintainer)
        server.collaboration.start()
    if importer := getattr(server, "importer", None):
        importer.close()
        server.importer = local_import.Importer(server.store, server.profiles, server.settings, server.server_port)
        server.importer.start()


def native_approval_models(binary):
    return [{"id": item["model"], "name": item["name"]}
            for item in codex_local.DETECTOR.cached(binary)["models"]
            if not any("astra" in item[key].lower() for key in ("id", "model", "name"))]


def selected_subscription_model(preferences, payload):
    identity = payload.get("subscription_model_id", preferences["default_codex_model"])
    effort = payload.get("reasoning_effort")
    if not isinstance(identity, str) or (identity and not codex_local.MODEL_ID.fullmatch(identity)):
        raise ValueError("请选择有效的订阅模型")
    if effort is not None and (not isinstance(effort, str) or not codex_local.EFFORT.fullmatch(effort)):
        raise ValueError("请选择有效的推理等级")
    probe = CodexAppServerAdapter(preferences).fetch_available_models()
    return codex_local.select_model(probe, identity, effort, preferences["disabled_codex_models"])


def retry_stage(server, identity, payload):
    attempt = payload.get("attempt_id")
    if not isinstance(attempt, str) or not re.fullmatch(r"(?:[0-9a-f]{32}|archive-[0-9]+)", attempt):
        raise ValueError("请选择要重试的阶段")
    if set(payload) - {"action", "attempt_id", "execution_mode", "model_profile_id", "subscription_model_id",
                       "reasoning_effort", "later_stages"}:
        raise ValueError("重试参数无效")
    mode = payload.get("execution_mode")
    model_selection = None
    if mode == codex_local.SUBSCRIPTION and "model_profile_id" not in payload and payload.get("subscription_model_id"):
        model_selection = {"execution_mode": mode,
                           "subscription_model": selected_subscription_model(server.settings.get(), payload)}
    elif mode == codex_local.API_GATEWAY and "subscription_model_id" not in payload:
        profile = payload.get("model_profile_id")
        if not isinstance(profile, str) or not profile:
            raise ValueError("请选择已配置的 API 模型")
        model_selection = {"execution_mode": mode,
                           "model_profile": public_profile(server.profiles.get(profile, run_options=payload))}
    elif set(payload) - {"action", "attempt_id", "later_stages"}:
        raise ValueError("请选择有效的重试模型")
    later = validate_stage_queue(payload["later_stages"]) if "later_stages" in payload else None
    run = server.workers.control(identity, "resume", stage_attempt=attempt, model_selection=model_selection, later_stages=later)
    return chat_turn(run, store=server.store)


def chat_turn(run, approval=None, *, store=None):
    fields = ("id", "status", "route", "prompt", "created_at", "model_profile", "error", "plan", "restarts", "research",
              "auto_benchmark", "plan_version", "plan_reason", "delegation", "execution_scope",
              "scope_domain_id", "scope_domain_name", "scoped_workflow", "updated_at", "execution_mode", "subscription_model")
    turn = {key: run[key] for key in fields if key in run}
    if run.get("scoped_workflow"):
        turn["collection_stop"] = run.get("scope_request", {}).get("collection_stop", "task")
    turn["can_resume"] = RunStore.can_resume(run)
    turn["approval_mode"] = run.get("agent_options", {}).get("approval_mode", "never")
    turn["sandbox_mode"] = (("read-only" if run.get("scope_request", {}).get("read_only") else "workspace-write")
                            if run.get("scoped_workflow") else run.get("agent_options", {}).get("sandbox_mode"))
    if approval:
        turn["pending_approval"] = approval
    result = run.get("result", {})
    turn["result"] = {key: result[key] for key in
                      ("agent_events", "agent_summary", "agent_run_ids", "coverage", "subagents",
                       "native_thread_id", "tokens", "harness_items", "harness_activity", "frontier", "domain_library", "scope_receipts",
                       "scope_continue", "rollout_threads") if key in result}
    if timeline := stage_timeline.public(run):
        turn["result"]["stage_timeline"] = timeline
        if tokens := sum(stage.get("tokens", 0) for stage in timeline):
            turn["result"]["tokens"] = tokens
        for key in ("agent_events", "agent_summary", "harness_items", "harness_activity", "rollout_threads", "subagents"):
            turn["result"].pop(key, None)
    receipts = result.get("scope_receipts", ())
    legacy = [report["contract_id"] for report in receipts if report.get("rejected") and "rejections" not in report]
    if store and legacy:
        with closing(store.connect()) as db:
            rows = {identity: scope_guard.rejection_details(db, identity) for identity in legacy}
        turn["result"]["scope_receipts"] = [
            {**report, "rejections": rows[report["contract_id"]]} if report["contract_id"] in rows else report for report in receipts]
        for stage in timeline:
            report = stage.get("receipt")
            if report and "rejections" not in report and report["contract_id"] in rows:
                stage["receipt"] = {**report, "rejections": rows[report["contract_id"]]}
    turn["result"]["tasks"] = [{"id": task["id"], "title": task["title"]} for task in result.get("tasks", [])]
    turn["steps"] = guarded_workflow.public_steps(run)
    if run.get("auto_benchmark"):
        turn["benchmark_ready"] = benchmark_ready(run)
        if "sample" in result:
            turn["result"]["sample"] = result["sample"]
    return turn


class WorkbenchHTTPServer(ThreadingHTTPServer):
    request_queue_size = 64  # Browsers request the module graph in parallel.


class RequestHandler(BaseHTTPRequestHandler):
    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}

    def send_bytes(self, status, content, content_type, download=None, *, etag=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status != 304:
            self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, no-cache" if etag else "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(download))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status, value):
        self.send_bytes(
            status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
        )

    def redirect(self, target):
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_settings(self):
        preferences = self.server.settings.get()
        model_catalog = self.server.profiles.catalog()
        storage = self.server.settings.storage()
        storage.update(profile_count=len(model_catalog["profiles"]), credential_store=model_catalog["credential_store"])
        return {"preferences": preferences, "storage": storage,
                "native_approval_models": native_approval_models(preferences["codex_executable"])}

    def do_GET(self):
        if not self.allowed_host():
            self.send_json(403, {"error": "请通过本机平台地址访问"})
            return
        try:
            location = urlsplit(self.path)
            path = location.path
            if path in ("/health", "/api/workbench/tools") or path in PAGES or SHELL_PATH.fullmatch(path):
                self.read_route(location)
            else:
                with self.server.workspace_lock:
                    self.read_route(location)
        except Exception:
            logger.exception("GET failure")
            self.send_json(500, {"error": "平台读取失败，请查看服务日志"})

    def read_route(self, location=None):
        location = location or urlsplit(self.path)
        path = location.path
        if path in PAGES or SHELL_PATH.fullmatch(path):
            filename, content_type = PAGES.get(path, PAGES["/"])
            file = STATIC / filename
            etag = None
            if path.startswith("/static/"):
                stat = file.stat()
                etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
                if etag in (tag.strip() for tag in self.headers.get("If-None-Match", "").split(",")):
                    return self.send_bytes(304, b"", content_type, etag=etag)
            self.send_bytes(200, file.read_bytes(), content_type, etag=etag)
        elif path in ("/operations", "/operations/", "/index.html"):
            incoming = parse_qs(location.query)
            scope = next((values[0] for key in ("workspace", "domain") if
                          (values := incoming.get(key)) and re.fullmatch(r"[1-9]\d{0,15}", values[0])), None)
            query = {"domain": scope} if scope else {}
            if incoming.get("bench", [""])[0] in ("case", "eval"):
                query["bench"] = incoming["bench"][0]
            self.redirect("/" + ("?" + urlencode(query) if query else ""))
        elif path == "/health":
            self.send_json(200, {"status": "ready", "version": __version__, "profile": "industplatform-codex"})
        elif path == "/api/catalog":
            self.send_json(200, catalog(self.server.profiles))
        elif path == "/api/workbench/tools":
            self.send_json(200, {"tools": sandbox_verifier.tools()})
        elif path == COLLABORATION_PATH:
            options = parse_qs(location.query)
            self.send_json(200, self.server.collaboration.snapshot(details=options.get("detail") == ["1"],
                                                                  target=options.get("run", [""])[0][:32]))
        elif path == IMPORTS_PATH:
            try:
                params = parse_qs(location.query)
                with closing(self.server.store.connect()) as db:
                    result = local_import.details(db, params.get("batch", [""])[0], int(params.get("offset", ["0"])[0]))
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_json(200, result)
        elif path in ("/api/workbench/scope-options", "/api/workbench/scope-tasks"):
            params = parse_qs(location.query)
            identity = params.get("domain", [""])[0]
            if identity and not re.fullmatch(r"[1-9][0-9]{0,15}", identity):
                return self.send_json(400, {"error": "领域编号无效"})
            try:
                if path.endswith("scope-tasks"):
                    if not identity:
                        raise ValueError("请选择工业领域")
                    view = params.get("view", [""])[0]
                    if view == "bindings":
                        value = guarded_workflow.domain_task_bindings(self.server.store, int(identity))
                    else:
                        value = guarded_workflow.domain_tasks(self.server.store, int(identity),
                            item=params.get("item", [""])[0][:200], summary=view == "summary")
                else:
                    value = guarded_workflow.scope_options(self.server.store, int(identity) if identity else None)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_json(200, value)
        elif match := SCOPE_EXPORT_PATH.fullmatch(path):
            with closing(self.server.store.connect()) as db:
                row = db.execute("SELECT kind,columns_json,body FROM workflow_export_snapshots WHERE id=?", (match[1],)).fetchone()
            if not row:
                return self.send_json(404, {"error": "找不到这份导出快照"})
            try:
                content, media = domain_export.encode(json.loads(row[2]), json.loads(row[1]), row[0])
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_bytes(200, content, media, download=f"benchmark-{match[1][:8]}.{row[0]}")
        elif path == "/api/workbench/export":
            params = parse_qs(location.query)
            identity, kind = params.get("domain", [""])[0], params.get("format", [""])[0]
            try:
                if not re.fullmatch(r"[1-9][0-9]{0,17}", identity) or kind not in ("jsonl", "parquet"):
                    raise ValueError("请选择领域和导出格式")
                content, media = domain_export.generate(self.server.store, int(identity), kind)
            except (ValueError, OverflowError) as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_bytes(200, content, media, download=f"domain-{identity}-benchmark.{kind}")
        elif path in ("/api/workbench/assets", "/api/workbench/asset-evaluation", "/api/workbench/batch-export"):
            params = parse_qs(location.query)
            domains = params.get("domain", [])
            try:
                if any(not re.fullmatch(r"[1-9][0-9]{0,15}", value) or int(value) > 2**53 - 1 for value in domains):
                    raise ValueError("领域编号无效")
                if path.endswith("batch-export"):
                    content = domain_export.batch(self.server.store, list(dict.fromkeys(map(int, domains))))
                    return self.send_bytes(200, content, "application/zip", download="industrial-benchmarks.zip")
                if len(domains) > 1:
                    raise ValueError("请选择一个领域")
                domain = int(domains[0]) if domains else None
                if path.endswith("asset-evaluation"):
                    result = asset_index.evaluation(self.server.store, domain, params.get("item", [""])[0])
                else:
                    result = asset_index.read(self.server.store, params.get("kind", ["sources"])[0], domain=domain,
                        query=params.get("q", [""])[0][:120], offset=int(params.get("offset", ["0"])[0]),
                        filters={key: params.get(key, [""])[0][:120] for key in asset_index.FILTERS})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_json(200, result)
        elif path in ("/api/workbench", SCHEMA_PATH, SOURCES_PATH, REFERENCE_PATH, VERIFIER_PATH, TASK_VALUE_PATH, DOMAIN_APPROVALS_PATH):
            params = parse_qs(location.query)
            value = params.get("domain", [""])[0]
            try:
                if (value or path not in ("/api/workbench", SCHEMA_PATH)) and not re.fullmatch(r"[1-9][0-9]{0,17}", value):
                    raise ValueError("领域编号无效")
                domain_id = int(value) if value else None
                store = self.server.store
                if path == SCHEMA_PATH:
                    result = workbench.export_schema(store, domain_id)
                elif path == VERIFIER_PATH:
                    result = sandbox_verifier.read(store, domain_id, params.get("task", [""])[0])
                elif path == TASK_VALUE_PATH:
                    result = workbench.task_value(store, domain_id, params.get("task", [""])[0])
                elif path == DOMAIN_APPROVALS_PATH:
                    result = {"destinations": workbench.domain_approvals(store, domain_id)}
                elif path == REFERENCE_PATH:
                    reference = params.get("item", [""])[0]
                    if not ENTITY_ID.fullmatch(reference):
                        raise ValueError("请选择文献")
                    result = workbench.reference_detail(store, domain_id, reference)
                elif path == SOURCES_PATH:
                    result = workbench.source_page(store, domain_id, params.get("kind", ["references"])[0],
                                                   params.get("q", [""])[0][:120], int(params.get("offset", ["0"])[0]),
                                                   item=params.get("item", [""])[0][:200], stage=params.get("stage", [""])[0],
                                                   node=params.get("node", [""])[0], tier=params.get("tier", [""])[0],
                                                   channel=params.get("channel", [""])[0])
                else:
                    thread = params.get("thread", [""])[0]
                    if thread and not RUN_ID.fullmatch(thread):
                        raise ValueError("执行线程编号无效")
                    level = params.get("level", [""])[0] or None
                    if level not in (None, "global", "domain") or level == "domain" and not domain_id:
                        raise ValueError("请选择有效的工作区")
                    result = workbench.overview(store, domain_id, thread=thread or None, level=level,
                                                include_import_coverage=params.get("view", ["graph"])[0] == "graph")
                    result["monitor"] = []
                    for thread in result["threads"]:
                        if thread["status"] in workbench.ACTIVE:
                            run = store.latest_turn(thread["id"])
                            if run:
                                approval = self.server.workers.pending_approval(run["id"])
                                result["monitor"].append({"thread": thread, "turn": chat_turn(run, approval, store=store)})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_json(200, result)
        elif path == PROFILE_PATH:
            self.send_json(200, self.server.profiles.catalog())
        elif path == SETTINGS_PATH:
            self.send_json(200, self.read_settings())
        elif path == CODEX_LOCAL_PATH:
            self.send_json(200, codex_local.DETECTOR.cached(self.server.settings.get()["codex_executable"]))
        elif path == "/api/rules":
            self.send_json(200, {"rules": metrology.rules()})
        elif path == "/api/keywords":
            params = parse_qs(location.query)
            query = params.get("q", [""])[0][:280]
            try:
                limit = max(1, min(100, int(params.get("limit", ["15"])[0])))
            except ValueError:
                self.send_json(400, {"error": "limit 必须为整数"})
                return
            self.send_json(200, {"keywords": search_keywords(query, limit)})
        elif path == "/api/runs":
            self.send_json(200, {"runs": self.server.store.recent()})
        elif path == "/api/conversations":
            scope = parse_qs(location.query).get("domain", [""])[0][:100]
            self.send_json(200, {"conversations": self.server.store.conversations(scope or None)})
        elif match := CONVERSATION_PATH.fullmatch(path):
            store = self.server.store
            turns = store.conversation(match[1])
            if not turns:
                with closing(store.connect()) as db:
                    imported = local_import.task_turn(db, match[1])
                if imported:
                    self.send_json(200, {"id": match[1], "turns": [imported], "plan_history": []})
                else:
                    self.send_json(404, {"error": "未找到这个任务"})
            else:
                collaboration = getattr(self.server, "collaboration", None)
                candidates = collaboration.conversation_candidates(match[1]) if collaboration else set()
                self.send_json(200, {"id": match[1], "turns": [
                    {**chat_turn(turn, self.server.workers.pending_approval(turn["id"]), store=store),
                     "can_publish": turn["id"] in candidates} for turn in turns
                ],
                                     "plan_history": store.plan_history(match[1])})
        elif path == REFERENCES_PATH + "/domains":
            self.send_json(200, {"domains": collection.reference_domains(self.server.store)})
        elif path == REFERENCES_PATH:
            params = parse_qs(location.query)
            run_id = params.get("run", [""])[0]
            try:
                if run_id and not RUN_ID.fullmatch(run_id):
                    raise ValueError("运行 ID 无效")
                result = collection.references(
                    self.server.store, domain=params.get("domain", [""])[0][:100],
                    query=params.get("q", [""])[0][:120], offset=int(params.get("offset", ["0"])[0]),
                    limit=max(1, min(30, int(params.get("limit", ["20"])[0]))), run_id=run_id,
                )
            except (ValueError, OverflowError) as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_json(200, result)
        elif path == "/api/library":
            params = parse_qs(location.query)
            try:
                catalog_page = library.read(self.server.store, params.get("kind", [""])[0],
                                            query=params.get("q", [""])[0],
                                            offset=int(params.get("offset", ["0"])[0]),
                                            domain=params.get("domain", [""])[0][:100])
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            else:
                self.send_json(200, catalog_page)
        elif path == BENCHMARKS_PATH:
            self.send_json(200, {
                "cases": industrial_bench.cases(self.server.store),
                "templates": industrial_bench.templates(self.server.store),
                "variants": industrial_bench.VARIANTS, "inputs": industrial_bench.INPUTS,
                "panel": [{key: profile[key] for key in ("id", "name", "model", "protocol", "tier", "base_url")}
                          for profile in self.server.profiles.catalog()["profiles"] if profile["tier"] in TIERS and profile["enabled"]],
            })
        elif match := RUN_PATH.fullmatch(path):
            run = self.server.store.get(match[1])
            if run is None:
                self.send_json(404, {"error": "未找到运行记录"})
            elif not match[2]:
                self.send_json(200, {**run, "domain_id": workbench.run_domain(self.server.store, run), "benchmark_ready": benchmark_ready(run)})
            elif not benchmark_ready(run):
                self.send_json(409, {"error": "当前运行还未产出 Benchmark"})
            else:
                self.send_bytes(
                    200, benchmark_zip(run), "application/zip",
                    download="industplatform-{}.zip".format(run["id"][:8]),
                )
        elif not path.startswith(("/api/", "/static/")) and path not in ("/api", "/static") and (
            "." not in path.rsplit("/", 1)[-1] or "text/html" in self.headers.get("Accept", "")
        ):
            self.redirect("/#scope")
        else:
            self.send_json(404, {"error": "未找到页面或接口"})

    def do_POST(self):
        origin = self.headers.get("Origin")
        path = urlsplit(self.path).path
        if not self.allowed_host() or (origin and origin != "http://" + self.headers["Host"]):
            self.send_json(403, {"error": "仅接受本机工作台提交的请求"})
        elif path == IMPORTS_PATH + "/file":
            self.read_import_file()
        elif path in (ATTACHMENTS_PATH, VERIFIER_SAMPLE_PATH):
            try:
                params = parse_qs(urlsplit(self.path).query)
                size = int(self.headers.get("Content-Length", 0))
                domain_id = int(params.get("domain", ["0"])[0])
                limit = sandbox_verifier.MAX_SAMPLE_BYTES if path == VERIFIER_SAMPLE_PATH else attachments.MAX_SIZE
                minimum = 0 if path == VERIFIER_SAMPLE_PATH else 1
                if (self.headers.get_content_type() != "application/octet-stream"
                        or not minimum <= size <= limit or not 1 <= domain_id <= 2**53 - 1):
                    raise ValueError(f"请选择领域和 {limit // (1024 * 1024)} MiB 以内的样本")
                content = self.rfile.read(size)
                if len(content) != size:
                    raise ValueError("附件上传未完成")
                with self.server.workspace_lock:
                    save = sandbox_verifier.upload_sample if path == VERIFIER_SAMPLE_PATH else attachments.save
                    result = save(self.server.store, domain_id, params.get("name", [""])[0], content)
                self.send_json(201, result)
            except (ValueError, OSError) as exc:
                self.send_json(400, {"error": str(exc) if isinstance(exc, ValueError) else "无法保存本机附件"})
        elif (path not in WRITE_PATHS + PROVIDER_ACTIONS and not RUN_APPROVAL_PATH.fullmatch(path)
              and not RUN_CONTROL_PATH.fullmatch(path) and not PLAN_REVISION_PATH.fullmatch(path)
              and not RUN_STEER_PATH.fullmatch(path) and not RUN_SCOPE_PATH.fullmatch(path)):
            self.send_json(404, {"error": "未找到接口"})
        elif self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "需要 application/json"})
        else:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                limit = 128 * 1024 if path in (BENCHMARK_CASES_PATH, VERIFIER_PATH) else 32768
                if not 1 <= length <= limit:
                    raise ValueError(f"请求体应为 1 至 {limit} 字节")
                try:
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise ValueError("请求体必须为有效 JSON") from None
                if not isinstance(payload, dict):
                    raise ValueError("请求参数必须为 JSON 对象")
                if path in PROVIDER_ACTIONS:
                    # Provider I/O uses a snapshot of the active store. It does not block local settings.
                    with self.server.workspace_lock:
                        profiles = self.server.profiles
                    if path.endswith("/test"):
                        result = (profiles.test_provider(payload["provider_id"]) if "provider_id" in payload
                                  else profiles.test(payload.get("id")))
                    elif path.endswith("/discover"):
                        result = profiles.discover(payload)
                    else:
                        result = profiles.probe(payload)
                    self.send_json(200, result or {"ok": True})
                elif path == CODEX_LOCAL_PATH:
                    preferences = self.server.settings.get()
                    binary = preferences["codex_executable"]
                    action = payload.get("action")
                    if action == "login":
                        self.send_json(200, {"auth_url": codex_local.DETECTOR.begin_login(binary)})
                    elif action == "detect":
                        CodexAppServerAdapter(preferences).fetch_available_models(force_refresh=payload.get("refresh") is True)
                        self.send_json(200, codex_local.DETECTOR.cached(binary))
                    else:
                        raise ValueError("请选择有效的本机 Codex 操作")
                elif path == VERIFIER_PATH and payload.get("action") == "trial":
                    with self.server.workspace_lock:
                        store = self.server.store
                    self.send_json(200, sandbox_verifier.trial(store, payload.get("domain_id"), payload.get("variant"),
                                                              payload.get("task", ""), payload.get("version")))
                elif path == VERIFIER_PATH and payload.get("action") == "inspect_image":
                    self.send_json(200, sandbox_verifier.inspect_image(payload.get("image")))
                else:
                    with self.server.workspace_lock:
                        status, result = self.write_route(path, payload)
                    self.send_json(status, result)
            except CredentialError as exc:
                self.send_json(503, {"error": str(exc)})
            except WorkspaceBusy as exc:
                self.send_json(409, {"error": str(exc)})
            except (ValueError, EvaluationError) as exc:
                self.send_json(400, {"error": str(exc)})
            except codex_local.LocalCodexError as exc:
                self.send_json(409, {"error": str(exc)})
            except ProviderError as exc:
                self.send_json(502, {"error": str(exc)})
            except Exception:
                logger.exception("POST failure")
                self.send_json(500, {"error": "任务提交失败，请查看服务日志"})

    def read_import_file(self):
        params = parse_qs(urlsplit(self.path).query)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if (self.headers.get_content_type() != "application/octet-stream"
                    or not 1 <= size <= local_import.MAX_FILE_BYTES):
                raise ValueError("请选择 16 MiB 以内的本地文件")
            with self.server.workspace_lock:
                importer = self.server.importer
                if not importer.upload_lock.acquire(blocking=False):
                    return self.send_json(409, {"error": "正在读取其他本地文件，请重试"})
            try:
                self.connection.settimeout(30)
                content = self.rfile.read(size)
                if len(content) != size:
                    raise ValueError("本地文件读取未完成")
                result = importer.accept_file(params.get("batch", [""])[0], int(params.get("index", ["-1"])[0]),
                                              unquote(self.headers.get("X-Indust-File-Name", "")), content,
                                              unquote(self.headers.get("X-Indust-Source-URL", "")))
            finally:
                importer.upload_lock.release()
        except (ValueError, OSError) as exc:
            self.send_json(400, {"error": str(exc) if isinstance(exc, ValueError) else "本地文件读取失败，请重试"})
        else:
            self.send_json(201, result)

    def write_route(self, path, payload):
        profiles = self.server.profiles
        settings = self.server.settings
        if path == COLLABORATION_PATH:
            return 202, self.server.collaboration.submit(payload)
        if path == IMPORTS_PATH:
            return 202, self.server.importer.submit(payload)
        if path == DOMAINS_PATH:
            if "action" in payload:
                return 200, domain_libraries.manage(self.server.store, payload)
            return 201, workbench.add_domain(self.server.store, payload)
        if path == NODES_PATH:
            return (200, workbench.change_process_node(self.server.store, payload)) if payload.get("action") else (
                201, workbench.add_process_node(self.server.store, payload))
        if path == VERIFIER_PATH:
            return 200, sandbox_verifier.save(self.server.store, payload.get("domain_id"), payload.get("version"),
                                              payload.get("spec"), payload.get("task", ""))
        if path == TASK_VALUE_PATH:
            domain_id = payload.get("domain_id")
            if type(domain_id) is not int or not 1 <= domain_id <= 2**53 - 1:
                raise ValueError("请选择工业领域")
            return 200, workbench.task_value(self.server.store, domain_id, payload.get("task"), payload)
        if path == DOMAIN_APPROVALS_PATH:
            domain_id, destination = payload.get("domain_id"), payload.get("destination")
            if (type(domain_id) is not int or not 1 <= domain_id <= 2**53 - 1
                    or not isinstance(destination, str) or len(destination) > 270):
                raise ValueError("请选择要撤销的领域目标")
            return 200, {"destinations": workbench.domain_approvals(self.server.store, domain_id, revoke=destination)}
        if path == SCHEMA_PATH:
            return 200, workbench.save_export_schema(self.server.store, payload)
        if path == FRONTIER_PATH:
            domain, node, identity = payload.get("domain_id"), payload.get("node_id"), payload.get("id")
            if type(domain) is not int or not 1 <= domain <= 2**53 - 1 or not isinstance(node, str) or len(node) > 120:
                raise ValueError("请选择所属领域和工序")
            if payload.get("action") == "retry" and isinstance(identity, str) and RUN_ID.fullmatch(identity):
                frontier.retry(self.server.store, domain, node, identity)
            elif payload.get("action") == "assess":
                wave, note, reference = payload.get("round"), payload.get("note"), payload.get("reference_id")
                if (type(wave) is not int or not 1 <= wave <= 2**31 or not isinstance(identity, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", identity) or not isinstance(note, str)
                        or not note.strip() or len(note) > 500 or
                        (reference and (not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f]{64}", reference)))):
                    raise ValueError("请输入核验依据和对应的来源版本")
                frontier.assess(self.server.store, domain, node, wave, identity, note=note.strip(), reference_id=reference)
            else:
                raise ValueError("请选择采集队列操作")
            return 200, frontier.status(self.server.store, domain, node)
        if match := RUN_SCOPE_PATH.fullmatch(path):
            if payload.get("action") == "retry_stage":
                return 202, retry_stage(self.server, match[1], payload)
            return 202, chat_turn(guarded_workflow.confirm(self.server.store, self.server.profiles, match[1], payload),
                                  store=self.server.store)
        if match := RUN_CONTROL_PATH.fullmatch(path):
            action = payload.get("action")
            if not isinstance(action, str):
                raise ValueError("请选择暂停、继续或取消")
            return 200, chat_turn(self.server.workers.control(match[1], action), store=self.server.store)
        if match := RUN_APPROVAL_PATH.fullmatch(path):
            correction = payload.get("correction", "")
            if not isinstance(correction, str) or len(correction) > 2000:
                raise ValueError("纠正意见最多 2000 字符")
            self.server.workers.decide_approval(match[1], payload.get("id"), payload.get("decision"), correction.strip())
            return 200, {"ok": True}
        if match := RUN_STEER_PATH.fullmatch(path):
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise ValueError("请输入 1–4000 字符的纠偏指令")
            running = self.server.store.get(match[1])
            if running and running.get("scoped_workflow"):
                raise ValueError("请选择暂停或取消，再提交要重新定界的指令")
            self.server.workers.steer(match[1], text.strip())
            return 202, {"queued": True}
        if match := PLAN_REVISION_PATH.fullmatch(path):
            expected = payload.get("plan_version")
            if type(expected) is not int or expected < 0:
                raise ValueError("采集计划版本无效")
            return 200, self.server.store.confirm_plan(
                match[1], collection.plan(payload.get("plan")), payload.get("workflow_mode"),
                payload.get("reason"), expected,
            )
        if path == BENCHMARK_CASES_PATH:
            return 201, industrial_bench.register(self.server.store, payload)
        if path == BENCHMARK_TEMPLATES_PATH:
            return 201, industrial_bench.save_template(self.server.store, payload)
        if path == BENCHMARKS_PATH:
            return 202, self.queue_benchmark(payload)
        if path == REFERENCES_PATH:
            run_id = payload.get("run_id")
            if run_id is not None and (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
                raise ValueError("运行 ID 无效")
            return 200, collection.save_reference(
                self.server.store, {key: value for key, value in payload.items() if key != "run_id"}, run_id,
            )
        if path == STORAGE_PATH:
            try:
                move_workspace(self.server, payload.get("path"))
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            return 200, self.read_settings()
        if path in (PROFILE_PATH, PROFILE_PATH + "/remove", MODEL_ACTION_PATH):
            if path == MODEL_ACTION_PATH:
                if payload.get("action") == "remove":
                    removed_ids = profiles.remove(payload.get("id"), keep_provider=True)
                    response = {"ok": True}
                elif payload.get("action") == "enabled":
                    item = profiles.set_enabled(payload.get("id"), payload.get("enabled"))
                    removed_ids = set() if item["enabled"] else {item["id"]}
                    response = {"profile": item}
                else:
                    raise ValueError("请选择模型操作")
            elif path == PROFILE_PATH + "/remove":
                if "provider_id" in payload and not isinstance(payload["provider_id"], str):
                    raise ValueError("请选择已保存的提供商连接")
                removed_ids = profiles.remove(payload.get("id"), provider_id=payload.get("provider_id"))
                response = {"ok": True}
            elif "models" in payload or "provider_id" in payload:
                provider, removed_ids = profiles.save_provider(payload)
                response = {"provider": provider}
            else:
                return 200, {"profile": profiles.save(payload)}
            preferences = settings.get()
            reset = {key: "" for key in ("default_model", "default_worker_model", "approval_model_api")
                     if preferences[key] in removed_ids}
            if "provider" in response:
                unavailable = {item["id"] for item in response["provider"]["models"]
                               if not item["enabled"] or "astra" in item["model"].lower()}
                reset.update({key: "" for key in ("default_worker_model", "approval_model_api")
                              if preferences[key] in unavailable})
            if reset:
                settings.update(reset)
            return 200, response
        if path == SETTINGS_PATH:
            selectors = ("default_model", "approval_model_api", "default_worker_model")
            if any(payload.get(key) for key in selectors):
                usable = {item["id"]: item for item in profiles.catalog()["profiles"] if item["enabled"]}
                for key in selectors:
                    selected = payload.get(key)
                    if not selected or key == "default_model" and selected == "@codex":
                        continue
                    if not isinstance(selected, str) or selected not in usable:
                        raise ValueError("请选择已启用的模型连接")
                    if key != "default_model" and "astra" in usable[selected]["model"].lower():
                        raise ValueError("请选择可用的非 Astra 模型")
            native_reviewer = payload.get("approval_model_native")
            if native_reviewer and not any(
                item["id"] == native_reviewer for item in native_approval_models(settings.get()["codex_executable"])
            ):
                raise ValueError("请选择本机 Codex 目录中的审批模型")
            return 200, {"preferences": settings.update(payload)}
        execution_mode = payload.get("execution_mode")
        if "execution_mode" in payload:
            if execution_mode not in codex_local.MODES:
                raise ValueError("请选择有效的执行模式")
            route = codex_agent.ROUTE if execution_mode == codex_local.SUBSCRIPTION else API_AGENT_ROUTE
            if payload.get("route", route) != route:
                raise ValueError("执行模式与任务路由不一致")
            if execution_mode == codex_local.SUBSCRIPTION and any(
                field in payload for field in ("model_profile_id", "multi_agent", "delegation")
            ):
                raise ValueError("本地订阅仅接受订阅模型配置")
            if execution_mode == codex_local.API_GATEWAY and "subscription_model_id" in payload:
                raise ValueError("API 模型网关须使用已配置的服务商模型")
            if "fork_run_id" in payload or "delegation" in payload:
                raise ValueError("当前执行模式从工作台检查点开始，请提交新的后续指令")
            payload = {**payload, "route": route}
        if "multi_agent" in payload and payload.get("route") != API_AGENT_ROUTE:
            raise ValueError("仅 API Agent 任务支持多代理配置")
        panel = profiles.panel() if payload.get("cohort") == "live" else None
        arguments = accept_request(payload, native_panel=panel is not None)
        public_research = payload.get("public_research")
        if public_research is not None and type(public_research) is not bool:
            raise ValueError("持续补查选项须为布尔值")
        auto_benchmark = payload.get("auto_benchmark")
        if auto_benchmark is not None and type(auto_benchmark) is not bool:
            raise ValueError("自动构题选项须为布尔值")
        resolve_domain = payload.get("resolve_domain", False)
        if type(resolve_domain) is not bool or (resolve_domain and (
            public_research is not True or "plan" not in payload or any(payload.get(key) for key in
                ("attachments", "frontier_node", "fork_run_id", "conversation_id"))
        )):
            raise ValueError("自动匹配领域适用于新建研究任务")
        scope = None
        domain_hint = ""
        if "plan" in payload:
            if arguments[0] not in AGENT_ROUTES:
                raise ValueError("采集计划须由 Agent 执行")
            scope = collection.plan(payload["plan"], resolve_domain=resolve_domain)
            if resolve_domain:
                workspace = workbench.overview(self.server.store)["domains"]
                domain_hint = next((item["name"] for item in workspace if item["name"] == scope["domain"]), "")
                scope["domain"] = ""
        graph_mode = payload.get("graph_mode", False)
        if type(graph_mode) is not bool or (graph_mode and (not scope or public_research is not True)):
            raise ValueError("自动工艺图谱须确认领域和公域调研范围")
        plan_version = payload.get("plan_version")
        if plan_version is not None and (type(plan_version) is not int or plan_version < 1):
            raise ValueError("采集计划版本无效")
        parent_run_id = payload.get("parent_run_id")
        if parent_run_id is not None and (not isinstance(parent_run_id, str) or not RUN_ID.fullmatch(parent_run_id)):
            raise ValueError("父任务 ID 无效")
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and (not isinstance(conversation_id, str) or not RUN_ID.fullmatch(conversation_id)):
            raise ValueError("对话 ID 无效")
        options = None
        connection = None
        subscription_model = None
        if "approval_mode" in payload and (arguments[0] not in AGENT_ROUTES or payload["approval_mode"] not in APPROVAL_MODES):
            raise ValueError("请选择有效的任务审批方式")
        if "sandbox_mode" in payload and (arguments[0] not in AGENT_ROUTES
                                          or payload["sandbox_mode"] not in ("read-only", "workspace-write")):
            raise ValueError("请选择有效的任务沙箱")
        if arguments[0] in AGENT_ROUTES:
            preferences = settings.get()
            options = {key: preferences[key] for key in PERMISSION_PREFERENCES}
            if "approval_mode" in payload:
                options["approval_mode"] = payload["approval_mode"]
            if "sandbox_mode" in payload:
                options["sandbox_mode"] = payload["sandbox_mode"]
            if arguments[0] == API_AGENT_ROUTE:
                options.update({key: preferences[key] for key in AGENT_PREFERENCES})
                connection = profiles.get(payload.get("model_profile_id"), run_options=payload)
                if execution_mode == codex_local.API_GATEWAY:
                    options["approval_mode"] = "never"
                elif options["approval_mode"] == "auto_review":
                    reviewer_id = options["approval_model_api"]
                    reviewer = profiles.get(reviewer_id) if reviewer_id else connection
                    if "astra" in reviewer["model"].lower():
                        raise ValueError("Astra 系列不能用作审批模型，请在权限设置中选择其他模型")
                    if reviewer_id:
                        connection = {**connection, "approval_profile": reviewer}
                    options["approval_model_label"] = reviewer["model"]
            else:
                if execution_mode == codex_local.SUBSCRIPTION:
                    subscription_model = selected_subscription_model(preferences, payload)
                if options["approval_mode"] == "auto_review":
                    options["approval_model_label"] = options["approval_model_native"] or "Codex 默认"
        fork_native_thread = None
        attached = None
        if payload.get("attachments"):
            provided = payload.get("scope_request")
            domain_id = provided.get("domain_id") if isinstance(provided, dict) else None
            name = (scope or {}).get("domain")
            if domain_id and type(domain_id) in (str, int):
                name = next((item["name"] for item in workbench.overview(self.server.store)["domains"]
                             if str(item["id"]) == str(domain_id)), None)
            if not name or arguments[0] not in AGENT_ROUTES:
                raise ValueError("附件须关联已确认领域")
            attached = attachments.resolve(self.server.store, name, payload["attachments"])
        frontier_target = None
        if "frontier_node" in payload:
            node = payload["frontier_node"]
            if (not scope or public_research is not True or arguments[0] not in AGENT_ROUTES
                    or not isinstance(node, str) or len(node) > 120):
                raise ValueError("穷尽模式要求已确认的采集计划和所属工序")
            workspaces = workbench.overview(self.server.store)["domains"]
            workspace_domain = next((item for item in workspaces if item["name"] == scope["domain"]), None)
            if not workspace_domain:
                raise ValueError("请先建立工业领域")
            frontier_target = (workspace_domain["id"], node)
            frontier.status(self.server.store, *frontier_target)
        if source_id := payload.get("fork_run_id"):
            if (not isinstance(source_id, str) or not RUN_ID.fullmatch(source_id)
                    or conversation_id or not scope or arguments[0] not in AGENT_ROUTES):
                raise ValueError("请选择同一领域中可分支的已结束会话")
            source_run = self.server.store.get(source_id)
            if (not source_run or source_run["status"] not in ("completed", "needs_review")
                    or source_run.get("plan", {}).get("domain") != scope["domain"]
                    or source_run["route"] != arguments[0]
                    or (source_run.get("model_profile") or {}).get("id") != (connection or {}).get("id")
                    or not source_run["result"].get("native_thread_id")):
                raise ValueError("分支须使用原领域和原模型连接")
            fork_native_thread = source_run["result"]["native_thread_id"]
        scope_request = None
        if arguments[0] in AGENT_ROUTES:
            supplied = payload.get("scope_request", {})
            if not isinstance(supplied, dict) or set(supplied) - {"domain_id", "target", "stage_queue", "execution_pace", "collection_stop"}:
                raise ValueError("执行范围提示无效")
            identity = supplied.get("domain_id") or (frontier_target or (None,))[0]
            if not identity:
                name = domain_hint or (scope or {}).get("domain")
                identity = next((item["id"] for item in workbench.overview(self.server.store)["domains"]
                                 if item["name"] == name), None) if name else None
            if identity is not None and (type(identity) not in (int, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", str(identity))):
                raise ValueError("范围提示的领域编号无效")
            target = supplied.get("target") or {}
            if not isinstance(target, dict) or set(target) - {"stage", "process_node_id", "task_id"}:
                raise ValueError("范围提示对象无效")
            if target and (target.get("stage") not in STAGES or any(
                not isinstance(value, str) or not ENTITY_ID.fullmatch(value) for key, value in target.items() if key != "stage"
            )):
                raise ValueError("请选择有效的阶段和目标")
            if not target and frontier_target:
                target = {"stage": STAGES[2], "process_node_id": frontier_target[1]}
            scope_request = {"domain_id": str(identity) if identity else None, "target": target}
            if "stage_queue" in supplied:
                queue = validate_stage_queue(supplied["stage_queue"])
                if target and target["stage"] not in queue:
                    raise ValueError("目标必须属于所选环节")
                scope_request["stage_queue"] = list(queue)
            if "execution_pace" in supplied:
                if supplied["execution_pace"] not in EXECUTION_PACES:
                    raise ValueError("请选择有效的推进节奏")
                scope_request["execution_pace"] = supplied["execution_pace"]
            if "collection_stop" in supplied:
                if supplied["collection_stop"] not in COLLECTION_STOPS:
                    raise ValueError("请选择有效的采集停止策略")
                scope_request["collection_stop"] = supplied["collection_stop"]
            if payload.get("sandbox_mode") == "read-only":
                scope_request["read_only"] = True
        delegation = codex_agent.delegation_options(payload.get("delegation"), connection)
        if delegation:
            worker_id = settings.get()["default_worker_model"]
            worker = profiles.get(worker_id) if worker_id else connection
            if "astra" in worker["model"].lower():
                raise ValueError("请选择非 Astra 的默认子代理模型")
            connection = {**connection, "worker_profile": worker}
        run = profiles.enqueue(self.server.store, arguments, profile=connection, panel=panel,
                               conversation_id=conversation_id, agent_options=options,
                               plan=scope, parent_run_id=parent_run_id, public_research=public_research,
                               auto_benchmark=auto_benchmark, plan_version=plan_version,
                               plan_reason=payload.get("plan_reason", ""),
                               delegation=delegation,
                               fork_native_thread=fork_native_thread, frontier_target=frontier_target, attachments=attached,
                               graph_mode=graph_mode, resolve_domain=resolve_domain, domain_hint=domain_hint, scope_request=scope_request,
                               execution_mode=execution_mode, subscription_model=subscription_model)
        return 202, run

    def queue_benchmark(self, payload):
        store, profiles = self.server.store, self.server.profiles
        case = industrial_bench.case(store, payload.get("case_id"))
        layout = industrial_bench.template(store, payload.get("template_id"), payload.get("template_version"))
        if not industrial_bench.matches_template(case, layout):
            raise ValueError("请选择同领域、相同验证器的模板")
        cohort = payload.get("cohort", "offline")
        mode = payload.get("variant", "as_recorded")
        if not isinstance(mode, str) or mode not in industrial_bench.VARIANTS or cohort not in ("live", "offline"):
            raise ValueError("请检查验收方式和测试工况")
        industrial_bench.oracle(industrial_bench.variant(case, mode), layout["recipient"])
        assignment = payload.get("assignment", "")
        if not isinstance(assignment, str) or len(assignment) > 800:
            raise ValueError("任务要求最多 800 字符")
        metadata = collection.fields(payload.get("metadata", {}), dict.fromkeys(layout["fields"], 500), set(), "输出字段")
        panel = None
        case_send_confirmed = False
        panel_mode = payload.get("panel_mode", "calibration")
        if panel_mode not in ("calibration", "probe"):
            raise ValueError("请选择盲测类型")
        repetitions = payload.get("repetitions", 1)
        if type(repetitions) is not int or not 1 <= repetitions <= 3:
            raise ValueError("每个模型可重复 1 至 3 次")
        if cohort == "live":
            case_send_confirmed = bool(
                case.get("input_run_id") and payload.get("confirm_case_permission") is True
                and any(item["permission"] == "stated" for item in case["references"])
            )
            if payload.get("confirm_model_send") is not True or not (case["share_allowed"] or case_send_confirmed):
                raise ValueError("请先核对案例使用许可并确认向模型发送题面与输入")
            ids = payload.get("profile_ids")
            if (not isinstance(ids, list) or len(ids) * repetitions > 18 or not ids
                    or any(not isinstance(value, str) or not RUN_ID.fullmatch(value) for value in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError("请选择不同的已保存模型连接，总次数最多 18")
            panel = {tier: [] for tier in TIERS}
            outgoing = json.dumps([case["inputs"], case["scenario"], layout, assignment, metadata], ensure_ascii=False)
            for identity in ids:
                profile = profiles.get(identity)
                if profile["tier"] not in panel:
                    raise ValueError("所选连接必须设置评测梯度")
                if len(profile["api_key"]) >= 8 and profile["api_key"] in outgoing:
                    raise ValueError("题面或输入含模型凭据，请移除后再提交")
                panel[profile["tier"]].append(profile)
            if panel_mode == "calibration" and not all(panel.values()):
                raise ValueError("轻量、均衡和旗舰每档至少选择一个连接")
        specification = {"case_id": case["id"], "domain": case["domain"], "template_id": layout["id"], "template_version": layout["version"],
                         "variant": mode, "metadata": metadata, "assignment": assignment.strip(),
                         "repetitions": repetitions, "case_send_confirmed": case_send_confirmed, "panel_mode": panel_mode}
        parent = payload.get("parent_run_id")
        if parent is not None and (not isinstance(parent, str) or not RUN_ID.fullmatch(parent)):
            raise ValueError("父任务 ID 无效")
        return profiles.enqueue(store, (industrial_bench.ROUTE, cohort, case["title"], None),
                                panel=panel, benchmark=specification, parent_run_id=parent)


def serve(port, database, *, browser=False, maintainer=False):
    anchor = Path(database).resolve()
    with WorkbenchHTTPServer(("127.0.0.1", port), RequestHandler) as server:
        server.anchor = anchor
        server.workspace_lock = threading.RLock()
        server.store, server.profiles, server.settings, server.workers = workspace(server, resolve_database(anchor))
        server.collaboration = Collaboration(server.store, checkout(), maintainer=maintainer)
        server.importer = local_import.Importer(server.store, server.profiles, server.settings, server.server_port)
        server.workers.start()
        server.collaboration.start()
        server.importer.start()
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"industplatform {__version__} → {url}", flush=True)
        if browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.importer.close()
            server.collaboration.close()
            server.workers.close()

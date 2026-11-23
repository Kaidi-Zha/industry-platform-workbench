"""Run workbench turns using the installed Codex CLI."""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext, suppress
from itertools import chain
from pathlib import Path
from uuid import uuid4

from run_codex import ROOT, codex_args, config_args
from . import codex_gateway, codex_local, codex_permissions
from .codex_runtime import codex_environment, prepare_codex_state
from .scope_guard import GUARD_ABORT_CODE, STAGE_TOOL_LIMIT_CODE

ROUTE = "codex-agent"
ROUTE_DETAILS = {
    "id": ROUTE,
    "label": "Codex Agent",
    "description": "调用本机 Codex 和项目 Skills，按任务需求使用工业 MCP 工具。",
}
RUN_TIMEOUT = 180
APP_SERVER_IDLE_TIMEOUT = 120
SCOPED_STAGE_TIMEOUT = 1200
STARTUP_TIMEOUT = 25
APPROVAL_METHODS = frozenset(("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                              "item/permissions/requestApproval"))
APP_SERVER_START_LOCK = threading.Lock()
MAX_EVENT_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_TOOL_EVENTS = 60
MAX_SUMMARY_CHARS = 8000
MAX_REASONING_CHARS = 1600
RUN_ID = re.compile(r"[0-9a-f]{32}")
CREDENTIAL = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|(?i:bearer)\s+[A-Za-z0-9._-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
TOOL_LABELS = {
    "get_catalog": "读取采集入口",
    "get_rules": "读取检验规则",
    "search_keywords": "检索 Forest 词表",
    "save_reference": "保存 Reference",
    "list_references": "查询 Reference Library",
    "authority_sources": "查看权威入口",
    "web_search": "检索公开来源",
    "read_public_source": "核验公开原文",
    "coverage_status": "查看覆盖缺口",
    "set_coverage_target": "更新覆盖目标",
    "record_evidence": "入库核对过的证据",
    "finish_research_round": "记录本轮补查",
    "select_benchmark_source": "选定题包实样来源",
    "record_process_graph": "登记工序与证据连线",
    "start_run": "派发工作流",
    "get_run": "读取执行结果",
    "wait_run": "等待工作流",
    "export_benchmark": "导出 Benchmark",
    "inspect_measurements": "复核检验数据",
    "author_cnc_task": "生成 CNC 检验题",
    "benchmark_cases": "查看可构题的工业案例",
    "register_industrial_case": "登记有来源的工业案例",
    "build_industrial_benchmark": "构造并提交工业题包",
}
TOOL_PREVIEW_FIELDS = {"query", "url", "target", "domain", "scenario", "stage", "kind", "route", "format", "id", "limit"}


class AgentError(RuntimeError):
    """An operator-facing explanation of an incomplete Codex run."""

    def __init__(self, message, *, code=None, detail=""):
        super().__init__(message)
        self.diagnostic = {}
        if code is not None:
            self.diagnostic["code"] = code
        if detail:
            self.diagnostic["detail"] = detail


class RunTimedOut(AgentError):
    """The current Codex run reached its execution deadline."""


class TurnBudgetExhausted(AgentError):
    """Codex used its model-call budget before completing the current batch."""


class RunSignal(threading.Event):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.active_turn = None
        self.steering = queue.Queue(maxsize=8)

    def set_turn(self, target):
        with self.lock:
            self.active_turn = target

    def steer(self, text):
        with self.lock:
            if self.is_set() or not self.active_turn:
                raise ValueError("当前任务尚无可纠偏的执行轮")
            try:
                self.steering.put_nowait((self.active_turn, text))
            except queue.Full:
                raise ValueError("已有纠偏指令在等待发送") from None


class ApprovalInbox:
    """One pending App Server request for an active workbench run."""

    def __init__(self, network_policy=None):
        self.lock = threading.Condition()
        self.pending = None
        self.decision = None
        self.network_policy = network_policy
        self.correction = ""

    def snapshot(self):
        with self.lock:
            return self.pending if self.decision is None else None

    def resolve(self, identity, decision, correction=""):
        with self.lock:
            if not self.pending or self.pending["id"] != identity or self.decision is not None:
                raise ValueError("这条审批请求已结束，请查看任务的最新状态")
            if decision not in ("accept", "decline", "domain") or (decision in ("accept", "domain") and not self.pending["can_approve"]):
                raise ValueError("这项操作不支持所选审批决定")
            if decision == "domain":
                target = self.pending.get("network_scope")
                if not target or not self.network_policy or not self.network_policy(target, "remember"):
                    raise ValueError("本领域授权仅适用于明确的 HTTPS 目标")
                decision = "accept"
            self.decision = decision
            self.correction = correction if decision == "decline" else ""
            self.lock.notify_all()

    def ask(self, details, stop):
        with self.lock:
            self.pending = {"id": uuid4().hex, **details}
            try:
                while self.decision is None and not (stop and stop.is_set()):
                    self.lock.wait(.2)
                if stop and stop.is_set():
                    raise AgentError("Codex 运行已停止")
                return self.decision, self.correction
            finally:
                self.pending = self.decision = None
                self.correction = ""


def executable(configured_model=False):
    if configured_model or os.environ.get("INDUST_GUI_CODEX") == "1":
        return shutil.which("codex")
    return None


DELEGATION_ROLES = {
    "industrial_sources": "来源采集（公开资料与项目 MCP，关闭 Shell）",
    "industrial_review": "证据复核（原文、许可与覆盖，关闭 Shell）",
    "industrial_verifier": "离线核验（项目 MCP 和父任务允许的 Shell）",
}


@contextmanager
def worker_role_settings(gateway, roles):
    if not gateway or not gateway.worker or not roles:
        yield {}
        return
    profile = gateway.worker.profile
    model_settings = {"model": codex_gateway.WORKER_ALIAS}
    effort = profile.get("reasoning_effort")
    if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        model_settings["model_reasoning_effort"] = effort
    if context := profile.get("capabilities", {}).get("context_window"):
        model_settings["model_context_window"] = context
    settings = {}
    with tempfile.TemporaryDirectory(dir=prepare_codex_state(), prefix="worker-roles-") as directory:
        for role in roles:
            if role == "worker":
                original = 'name = "worker"\ndescription = "执行交给子代理的工程任务。"\n'
            else:
                original = (ROOT / ".codex" / "agents" / (role + ".toml")).read_text(encoding="utf-8")
            target = Path(directory) / (role + ".toml")
            header = "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in model_settings.items())
            target.write_text(header + "\n" + original, encoding="utf-8")
            settings[f"agents.{role}.config_file"] = str(target)
        yield settings


def delegation_options(value, profile):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != {"limit", "roles"}:
        raise ValueError("子代理配置无效")
    limit, roles = value["limit"], value["roles"]
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("子代理并发上限须为 1 至 4")
    if (not isinstance(roles, list) or not roles
            or any(not isinstance(role, str) or role not in DELEGATION_ROLES for role in roles)
            or len(set(roles)) != len(roles)):
        raise ValueError("请选择可用的委派角色")
    if not profile or "astra" in profile["model"].lower() or profile.get("multi_agent"):
        raise ValueError("原生委派仅支持已连接的非 Astra 模型，且须关闭 Ultra")
    return {"limit": limit, "roles": roles}


def _read_events(process, stop=None, tick=None, *, max_duration=None, waits_for_approval=False):
    messages = queue.Queue(maxsize=8)
    finished = threading.Event()
    last_activity = started_at = time.monotonic()
    approval_finished_at = started_at

    def enqueue(message):
        while not finished.is_set():
            try:
                messages.put(message, timeout=0.1)
                return
            except queue.Full:
                pass

    def read():
        nonlocal last_activity
        total = 0
        try:
            while not finished.is_set():
                line = process.stdout.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    enqueue(None)
                    return
                total += len(line)
                if len(line) > MAX_EVENT_BYTES or total > MAX_STREAM_BYTES:
                    enqueue(AgentError("Codex 事件超过读取上限"))
                    return
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(event, dict) or not (
                    isinstance(event.get("method"), str) and event["method"]
                    or "id" in event and ("result" in event or "error" in event)
                ):
                    continue
                last_activity = time.monotonic()
                enqueue(event)
        except (OSError, ValueError):
            enqueue(AgentError("无法读取 Codex 执行状态"))

    source = threading.Thread(target=read, daemon=True, name="indust-codex-output")
    source.start()
    max_duration = RUN_TIMEOUT if max_duration is None else max_duration
    hard_deadline = started_at + max_duration
    try:
        while True:
            if tick:
                tick()
            if stop is not None and stop.is_set():
                raise AgentError("服务关闭，Codex 运行已停止")
            idle_deadline = max(last_activity, approval_finished_at) + APP_SERVER_IDLE_TIMEOUT
            remaining = min(idle_deadline, hard_deadline) - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = messages.get(timeout=min(.2, remaining))
            except queue.Empty:
                continue
            if event is None:
                return
            if isinstance(event, AgentError):
                raise event
            dispatched = time.monotonic()
            yield event
            # Human approval time is external to model and transport deadlines.
            if waits_for_approval and "id" in event and event.get("method") in APPROVAL_METHODS:
                approval_finished_at = time.monotonic()
                hard_deadline += approval_finished_at - dispatched
        if idle_deadline < hard_deadline:
            raise RunTimedOut(f"Codex 连续 {APP_SERVER_IDLE_TIMEOUT} 秒未返回任何 App Server 事件，连接或进程可能已卡住",
                              code="network_idle_timeout", detail=f"App Server idle timeout: {APP_SERVER_IDLE_TIMEOUT}s")
        raise RunTimedOut(f"当前阶段达到 {max_duration} 秒总保护上限", code="stage_max_duration_exceeded",
                          detail=f"App Server stage duration limit: {max_duration}s")
    finally:
        finished.set()


def _related_run(item):
    if item.get("tool") not in {"start_run", "get_run", "wait_run"}:
        return None
    result = item.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return None
    try:
        payload = json.loads(content[0].get("text", ""))
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("route") == ROUTE:
        return None
    run_id = payload.get("id")
    return run_id if isinstance(run_id, str) and RUN_ID.fullmatch(run_id) else None


def _redact(text):
    text = CREDENTIAL.sub("[已隐藏凭证]", text)
    for name, value in os.environ.items():
        if len(value) >= 8 and name.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
            text = text.replace(value, "[已隐藏凭证]")
    return text[:MAX_SUMMARY_CHARS]


def execute(prompt, port, on_event, stop=None, *, profile=None, guidance="", tools=None, max_turns=None,
            permissions=None, delegation=None, approvals=None, existing_thread=None, fork_thread=False,
            scoped=False, ephemeral=False, subscription=None, guard_check=None, max_duration=None):
    binary = subscription["binary"] if subscription else executable(configured_model=True) if profile else executable()
    if not binary:
        raise AgentError("请安装 Codex CLI 并确认 codex 在 PATH 内")
    if ephemeral and (existing_thread or fork_thread):
        raise AgentError("临时会话须从新线程启动")
    if subscription and (profile or not tools or not ephemeral):
        raise AgentError("订阅会话须使用独立的临时线程与工作台工具")
    if scoped:
        if tools is None or existing_thread or fork_thread:
            raise AgentError("作用域会话须使用独立线程和专属工具")
        permissions = {**(permissions or {}), "sandbox_mode": "read-only", "workspace_network_access": False}
        if subscription and permissions.get("approval_mode") == "auto_review":
            permissions["approval_mode"] = "never"
    permissions = permissions or {}
    automatic = permissions.get("approval_mode") == "auto_review"
    reviewer = (profile.get("approval_profile") or profile) if automatic and profile else None
    reviewer_model = reviewer["model"] if reviewer else (
        permissions.get("approval_model_native") if automatic else None
    )
    if reviewer_model and "astra" in reviewer_model.lower():
        raise AgentError("Astra 系列不能用作审批模型，请选择其他模型")
    try:
        native = delegation_options(delegation, profile)
        delegation_settings = {"agents.enabled": bool(native)}
        if native:
            delegation_settings.update({
                "features.multi_agent_v2": True,
                "agents.max_concurrent_threads_per_session": native["limit"],
            })
            for role in native["roles"]:
                delegation_settings[f"agents.{role}.description"] = DELEGATION_ROLES[role]
                delegation_settings[f"agents.{role}.config_file"] = str(ROOT / ".codex" / "agents" / (role + ".toml"))
            roles = "、".join(native["roles"])
            guidance += ("\n用户明确允许在需要时使用 Codex 原生子代理，本轮角色为 " + roles
                         + "。用 agent_type 选择角色，fork_turns 设为 none，只在消息中传任务必要内容。"
                         + ("使用默认子代理连接。" if profile.get("worker_profile") else "沿用当前连接。")
                         + "由主 Agent 复核与最终交付。")
        worker = profile.get("worker_profile") if profile and native else None
        session = (codex_gateway.serve(profile, tools=tools, max_turns=max_turns, reviewer=reviewer, scoped=scoped,
                                      worker=worker, collaboration=bool(native))
                   if profile or tools else nullcontext())
        with session as gateway, codex_permissions.model_catalog(
            binary, profile["model"] if profile else None, reviewer_model,
        ) as catalog, worker_role_settings(gateway, native["roles"] if native else ()) as worker_settings:
            return _execute(binary, prompt, port, on_event, stop, gateway, guidance, permissions,
                            {**catalog, **delegation_settings, **worker_settings}, approvals, existing_thread, fork_thread,
                            scoped=scoped, ephemeral=ephemeral, subscription=subscription, guard_check=guard_check,
                            max_duration=max_duration, context_window=(profile or {}).get("capabilities", {}).get("context_window"))
    except codex_local.LocalCodexError as exc:
        raise AgentError(str(exc)) from None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        raise AgentError("无法启动 Codex 或加载审批模型目录") from None


def _turn_error(value, redact, *, subscription=False):
    if not isinstance(value, dict):
        return ""
    message = value.get("message")
    details = value.get("additionalDetails")
    description = " ".join(text for text in (message, details) if isinstance(text, str))
    if subscription and "api.openai.com" in description and "api.responses.write" in description:
        return "Codex 订阅请求发到了 OpenAI API，返回 401（缺少 api.responses.write）"
    info = value.get("codexErrorInfo")
    category = next(iter(info), "") if isinstance(info, dict) else info
    http_status = info.get(category) if isinstance(info, dict) else None
    status = http_status.get("httpStatusCode") if isinstance(http_status, dict) else None
    if category == "unauthorized" or status == 401 or re.search(r"\b401\s+Unauthorized\b", description, re.I):
        return "Codex 模型端点返回 401，当前登录无法通过鉴权"
    if category in ("rateLimitExceeded", "usageLimitExceeded") or status == 429:
        return "Codex 模型请求达到速率或用量限制"
    if category == "modelNotFound":
        return "Codex 无法调用当前模型，请刷新模型目录"
    if category == "contextWindowExceeded":
        return "Codex 请求超过模型上下文上限"
    if isinstance(message, str) and message.strip():
        # Keep signed URL parameters out of the visible failure message.
        return redact(re.sub(r"((?:https?|wss?)://[^\s?#]+)[?#][^\s]*", r"\1", message)).strip()[:320]
    return ""


def diagnostic_text(text, redact=_redact):
    text = re.sub(r"((?:https?|wss?)://)[^/\s?#]+@", r"\1", str(text))
    text = re.sub(r"((?:https?|wss?)://[^\s?#]+)[?#][^\s]*", r"\1", str(text))
    text = re.sub(r'(?i)\b(authorization|access[_-]token|api[_-]key|refresh[_-]token|password)'
                  r'("?\s*[:=]\s*)(?:"[^"]*"|[^\s,;]+)', r"\1\2[已隐藏凭证]", text)
    return redact(text).strip()[:2400]


def _protocol_error(error, method, redact, *, subscription, default_code, fallback=None):
    error = error if isinstance(error, dict) else {}
    info = error.get("codexErrorInfo")
    category = next(iter(info), "") if isinstance(info, dict) else info
    code = error.get("code")
    fields = info.get(category) if isinstance(info, dict) else None
    http = fields.get("httpStatusCode") if isinstance(fields, dict) else None
    diag = {"code": code if isinstance(code, (int, str)) else category or default_code, "method": method}
    if type(http) is int:
        diag["http_status"] = http
    detail = "\n".join(error[key] for key in ("message", "additionalDetails") if isinstance(error.get(key), str))
    if detail:
        diag["detail"] = diagnostic_text(detail, redact)
    reason = _turn_error(error, redact, subscription=subscription) or fallback
    return reason, diag


def _mcp_item_error(item):
    error = item.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"], False
    result = item.get("result")
    if not isinstance(result, dict):
        return "", False
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("error"), str):
        return structured["error"], structured.get("audited") is True
    for part in result.get("content") or ():
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        value = part.get("text")
        if not isinstance(value, str) or not value.startswith("{") or len(value) > 8192:
            continue
        with suppress(ValueError, RecursionError):
            parsed = json.loads(value)
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                return parsed["error"], parsed.get("audited") is True
    return "", False


def _context_needs_compaction(thread, context_window):
    status = (thread.get("status") or {}).get("type")
    if thread.get("ephemeral") is not False or status != "idle" or thread.get("canAcceptDirectInput") is False:
        return False
    # Estimate only history since the last compaction. Unknown model windows use a conservative threshold.
    window = context_window if type(context_window) is int and context_window > 0 else 32000
    byte_budget = window * 9 // 4
    size = 0
    for turn in reversed(thread.get("turns") or []):
        for item in reversed(turn.get("items") or []):
            if item.get("type") == "contextCompaction":
                return False
            size += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if size >= byte_budget:
                return True
    return False


def _app_server_events(process, prompt, sandbox, policy, reviewer, approvals, stop, *,
                       existing_thread=None, fork_thread=False, redact=_redact, ephemeral=False,
                       subscription=None, mcp_server="industplatform", scoped=False, guard_check=None,
                       max_duration=None, context_window=None):
    def send(message):
        codex_local.send_rpc(process, message)

    send({"id": 1, "method": "initialize", "params": codex_local.INITIALIZE})
    thread = None
    turn_id = None
    stop_deadline = None
    next_id = 20
    pending_commands = {}
    pending_items = {}
    output_chunks = {}
    reasoning_chunks = {}
    last_error = (None, {})
    children = {}
    announced_turn = False
    compacting = False
    compact_turn = None
    pending_turn = None
    startup_deadline = time.monotonic() + STARTUP_TIMEOUT
    model = subscription["model"]["model"] if subscription else None

    def origin(current, current_turn):
        return {"thread": current, "turn": current_turn,
                **({"subagent_id": current, "parent_turn_id": children[current][1] or turn_id}
                   if current in children else {})}

    def mark_child(child, parent, parent_turn):
        if (isinstance(child, str) and 0 < len(child) <= 100 and child != thread
                and (parent == thread or parent in children)):
            existing = children.get(child)
            if not existing or existing[1] is None and parent_turn:
                children[child] = (parent, parent_turn)
            return True
        return False

    def compaction_event(owner, current_turn, status="completed", preview=""):
        return {"type": "harness.item", "kind": "compaction", "label": "上下文压缩",
                "status": status, "preview": preview, "item": "compaction:" + str(current_turn),
                **origin(owner, current_turn)}

    def start_thread():
        params = {"cwd": str(ROOT), "approvalPolicy": policy, "sandbox": sandbox,
                  "approvalsReviewer": reviewer}
        if ephemeral:
            params["ephemeral"] = True
        if subscription:
            params.update(model=model, modelProvider="openai",
                          allowProviderModelFallback=False, runtimeWorkspaceRoots=[str(ROOT)])
        operation = "thread/start"
        if existing_thread:
            params["threadId"] = existing_thread
            operation = "thread/fork" if fork_thread else "thread/resume"
        send({"id": 2, "method": operation, "params": params})

    def pump():
        nonlocal stop_deadline, next_id
        if guard_check:
            guard_check()
        if not thread and time.monotonic() >= startup_deadline:
            error = AgentError("Codex App Server 初始化超时，请检查仓库运行目录和 CLI 版本",
                               code="CODEX_STARTUP_TIMEOUT", detail=f"App Server startup timeout: {STARTUP_TIMEOUT}s")
            error.diagnostic["method"] = "thread/start"
            raise error
        if stop and stop.is_set():
            target_turn = turn_id or compact_turn
            if not thread or not target_turn:
                raise AgentError("Codex 运行已停止")
            if stop_deadline is None:
                send({"id": 4, "method": "turn/interrupt", "params": {"threadId": thread, "turnId": target_turn}})
                stop_deadline = time.monotonic() + 2
            if time.monotonic() > stop_deadline:
                raise AgentError("已停止等待中断确认")
        elif isinstance(stop, RunSignal) and thread and turn_id:
            try:
                target, instruction = stop.steering.get_nowait()
            except queue.Empty:
                return
            if target == (thread, turn_id):
                send({"id": next_id, "method": "turn/steer", "params": {
                    "threadId": thread, "expectedTurnId": turn_id, "input": [{"type": "text", "text": instruction}],
                }})
                pending_commands[next_id] = instruction
                next_id += 1

    duration = SCOPED_STAGE_TIMEOUT if scoped and max_duration is None else max_duration
    for event in _read_events(process, tick=pump, max_duration=duration, waits_for_approval=approvals is not None):
        identity = event.get("id")
        method = event.get("method")
        params = event.get("params") or {}
        if method is None:
            if "error" in event and identity in (1, 2, 3, 5, 6):
                step, request = {1: ("初始化 App Server", "initialize"),
                                 2: ("建立线程", "thread/fork" if fork_thread else
                                     "thread/resume" if existing_thread else "thread/start"),
                                 3: ("提交执行轮", "turn/start"), 5: ("读取订阅账户", "account/read"),
                                 6: ("读取本机配置", "config/read")}[identity]
                reason, diagnostic = _protocol_error(event["error"], request, redact, subscription=bool(subscription),
                                                     default_code="CODEX_JSON_RPC_ERROR")
                error = AgentError("Codex " + step + "失败" + (" · " + reason if reason else ""))
                error.diagnostic.update(diagnostic)
                raise error
            if identity == 1:
                send({"method": "initialized"})
                if subscription:
                    send({"id": 5, "method": "account/read", "params": codex_local.ACCOUNT_READ})
                else:
                    start_thread()
            elif identity == 5:
                status = codex_local.account_status(event["result"])["status"]
                if status != "ready_subscription":
                    raise AgentError(codex_local.status_message(status))
                send({"id": 6, "method": "config/read", "params": {"cwd": str(ROOT)}})
            elif identity == 6:
                effective = event["result"]["config"]
                if (effective.get("model_provider") != "openai"
                        or codex_local.active_mcp_servers(effective) != {mcp_server}
                        or effective.get("chatgpt_base_url") != codex_local.CHATGPT_BASE
                        or effective.get("openai_base_url") != codex_local.SUBSCRIPTION_RESPONSES_BASE
                        or "openai" in (effective.get("model_providers") or {})):
                    raise AgentError("本机 Codex 的模型端点或工具配置与订阅隔离规则冲突，请重新检测")
                start_thread()
            elif identity == 2:
                result = event["result"]
                thread_info = result["thread"]
                thread = thread_info["id"]
                turn = {"threadId": thread, "input": [{"type": "text", "text": prompt}]}
                if subscription:
                    if (result["thread"].get("ephemeral") is not True or result.get("modelProvider") != "openai"
                            or result.get("model") != model
                            or (scoped and ((result.get("sandbox") or {}).get("type") != "readOnly"
                                           or (result.get("sandbox") or {}).get("networkAccess") is not False))):
                        raise AgentError("Codex 未确认临时会话、所选模型或阶段沙箱，无法启动执行轮")
                    if effort := subscription["model"].get("reasoning_effort"):
                        turn["effort"] = effort
                    turn["serviceTierForTurn"] = "default"
                    turn["summary"] = "auto"
                yield {"type": "harness.thread", "thread": thread, **({"ephemeral": True} if ephemeral else {}),
                       **({"native_tools": True} if scoped else {})}
                if existing_thread and not ephemeral and _context_needs_compaction(thread_info, context_window):
                    pending_turn, compacting = turn, True
                    send({"id": 7, "method": "thread/compact/start", "params": {"threadId": thread}})
                else:
                    send({"id": 3, "method": "turn/start", "params": turn})
            elif identity == 7 and compacting and "error" in event:
                error = event["error"]
                if error.get("code") != -32601:
                    yield compaction_event(thread, None, "failed",
                                           diagnostic_text(error.get("message", "无法启动上下文压缩"), redact))
                compacting = False
                send({"id": 3, "method": "turn/start", "params": pending_turn})
            elif identity == 3:
                turn_id = event["result"]["turn"]["id"]
                if scoped and not announced_turn:
                    announced_turn = True
                    yield {"type": "harness.turn", **origin(thread, turn_id)}
                    yield {"type": "harness.progress", "kind": "waiting"}
                if isinstance(stop, RunSignal):
                    stop.set_turn((thread, turn_id))
            elif identity in pending_commands:
                pending_commands.pop(identity)
                yield {"type": "steering", "accepted": "error" not in event}
            continue
        if identity is not None:
            result = _approval_result(method, params, pending_items, approvals, stop, redact, allow_elevation=not scoped)
            correction = result.pop("_correction", "") if result is not None else ""
            send({"id": identity, **({"result": result} if result is not None
                 else {"error": {"code": -32601, "message": "Unsupported client request"}})})
            if correction and isinstance(stop, RunSignal):
                try:
                    stop.steer(correction)
                except ValueError:
                    pass
            continue
        if compacting and params.get("threadId") == thread:
            if method == "turn/started":
                compact_turn = (params.get("turn") or {}).get("id")
                continue
            if method == "turn/completed":
                compact = params.get("turn") or {}
                if compact_turn and compact.get("id") not in (None, compact_turn):
                    continue
                if compact.get("status") != "completed":
                    detail = _turn_error(compact.get("error"), redact) or "上下文压缩未完成"
                    yield compaction_event(thread, compact_turn, "failed", detail)
                compacting = False
                compact_turn = None
                if stop and stop.is_set():
                    raise AgentError("Codex 运行已停止")
                send({"id": 3, "method": "turn/start", "params": pending_turn})
                continue
        if method == "thread/started" and scoped and thread:
            child = params.get("thread") or {}
            agent, parent = child.get("id"), child.get("parentThreadId")
            if mark_child(agent, parent, turn_id if parent == thread else None):
                yield {"type": "subagent", "id": agent, "parent": parent, "status": "running",
                       **origin(agent, None),
                       **{key: child[field] for key, field in (("model", "model"), ("effort", "reasoningEffort"),
                          ("path", "agentNickname")) if isinstance(child.get(field), str)}}
        elif method == "turn/started" and scoped and thread and params.get("threadId") == thread and not announced_turn:
            turn_id = (params.get("turn") or {}).get("id")
            if turn_id:
                announced_turn = True
                yield {"type": "harness.turn", **origin(thread, turn_id)}
                yield {"type": "harness.progress", "kind": "waiting"}
        elif method == "error" and not compacting and thread and params.get("threadId") == thread and params.get("turnId") in (None, turn_id):
            last_error = _protocol_error(params.get("error"), method, redact, subscription=bool(subscription),
                                         default_code="CODEX_TURN_ERROR", fallback="Codex 模型请求失败")
            if scoped and params.get("willRetry") is True:
                yield {"type": "harness.progress", "kind": "retrying", "detail": last_error[0]}
            elif params.get("willRetry") is False:
                yield {"type": "turn.failed", "detail": last_error[0], "diagnostic": last_error[1]}
                return
        elif (method == "item/reasoning/summaryTextDelta" and thread
              and (params.get("threadId") == thread or params.get("threadId") in children)):
            key = (params.get("threadId"), params.get("turnId"), params.get("itemId"))
            index, delta = params.get("summaryIndex"), params.get("delta")
            if (all(isinstance(part, str) for part in key) and type(index) is int
                    and 0 <= index < 24 and isinstance(delta, str)):
                if key not in reasoning_chunks and len(reasoning_chunks) >= 48:
                    reasoning_chunks.pop(next(iter(reasoning_chunks)))
                parts, sent = reasoning_chunks.get(key, ({}, 0))
                parts[index] = (parts.get(index, "") + delta)[:MAX_REASONING_CHARS]
                stamp = time.monotonic()
                reasoning_chunks[key] = (parts, stamp if stamp - sent >= .75 else sent)
                if stamp - sent >= .75:
                    preview = "\n".join(parts[index] for index in sorted(parts))[:MAX_REASONING_CHARS]
                    if preview.strip():
                        yield {"type": "harness.item", "kind": "reasoning", "label": "推理摘要", "preview": preview,
                               **origin(key[0], key[1]), "item": key[2]}
        elif method == "item/commandExecution/outputDelta":
            key = (params.get("threadId"), params.get("turnId"), params.get("itemId"))
            delta = params.get("delta")
            if not scoped and all(isinstance(part, str) for part in key) and isinstance(delta, str):
                previous, sent = output_chunks.get(key, ("", 0))
                if len(output_chunks) >= 48 and key not in output_chunks:
                    output_chunks.pop(next(iter(output_chunks)))
                output_chunks[key] = ((previous + delta)[-MAX_SUMMARY_CHARS:], sent)
                stamp = time.monotonic()
                if stamp - sent >= .25:
                    preview, _ = output_chunks[key]
                    output_chunks[key] = (preview, stamp)
                    yield {"type": "harness.item", "preview": preview, "label": "命令输出",
                           "thread": key[0], "turn": key[1], "item": key[2]}
        elif method == "item/started":
            item = params.get("item") or {}
            item_thread = params.get("threadId")
            if item.get("type") in ("commandExecution", "fileChange"):
                pending_items[item["id"]] = item
            elif scoped and item.get("type") == "reasoning" and item_thread == thread:
                yield {"type": "harness.progress", "kind": "reasoning"}
            elif (scoped and item.get("type") == "mcpToolCall" and item.get("server") == mcp_server
                  and (item_thread == thread or item_thread in children) and isinstance(item.get("tool"), str)):
                yield {"type": "harness.item", "kind": "tool", "label": item["tool"], "preview": "",
                       **origin(item_thread, params.get("turnId")), "item": item.get("id"), "status": "running"}
        elif method == "item/completed":
            item = params.get("item", {})
            pending_items.pop(item.get("id"), None)
            item_type = item.get("type")
            item_thread, item_turn = params.get("threadId"), params.get("turnId")
            last_delta, _ = output_chunks.pop((item_thread, item_turn, item.get("id")), ("", 0))
            related = bool(thread and (item_thread == thread or item_thread in children))
            if item_type == "contextCompaction" and related:
                yield compaction_event(item_thread, item_turn)
            elif item_type in ("collabAgentToolCall", "subAgentActivity") and (related or not scoped):
                if scoped:
                    parent = item.get("senderThreadId") or item_thread
                    agents = item.get("agentsStates") or {}
                    lifecycle = {"started": "running", "interrupted": "interrupted", "completed": "completed"}
                    if item_type == "subAgentActivity":
                        agents = {item.get("agentThreadId"): {"status": lifecycle.get(item.get("kind"))}}
                    else:
                        agents = {**{agent: {} for agent in item.get("receiverThreadIds", [])}, **agents}
                    for agent, value in agents.items():
                        if mark_child(agent, parent, item_turn):
                            state = value.get("status") if isinstance(value, dict) else None
                            yield {"type": "subagent", "id": agent, "parent": parent, "status": state or "running",
                                   **origin(agent, None), **{key: item[field] for key, field in (
                                       ("path", "agentPath"), ("model", "model"), ("effort", "reasoningEffort"))
                                       if isinstance(item.get(field), str)}}
                else:
                    yield {"type": "item.completed", "item": item}
            if item_type == "agentMessage" and item_thread == thread:
                yield {"type": "item.completed", "item": {**item, "type": "agent_message"}}
            elif item_type == "mcpToolCall":
                if scoped and related and item.get("server") == mcp_server and isinstance(item.get("tool"), str):
                    detail, audited = _mcp_item_error(item)
                    yield {"type": "harness.item", "kind": "tool", "label": item["tool"], "preview": detail,
                           **origin(item_thread, item_turn), "item": item.get("id"),
                           "arguments": item.get("arguments"), "audited": audited,
                           "status": "failed" if detail or item.get("status") == "failed" else "completed",
                           "duration_ms": item.get("durationMs")}
                    continue
                if item_thread == thread:
                    yield {"type": "item.completed", "item": {**item, "type": "mcp_tool_call"}}
                tool = item.get("tool")
                arguments = item.get("arguments")
                if item.get("server") == mcp_server and isinstance(tool, str) and tool in TOOL_LABELS:
                    preview = {key: value for key, value in arguments.items() if key in TOOL_PREVIEW_FIELDS
                               and isinstance(value, (str, int, float))} if isinstance(arguments, dict) else {}
                    yield {"type": "harness.item", "preview": json.dumps(preview, ensure_ascii=False)[:MAX_SUMMARY_CHARS],
                           "label": TOOL_LABELS[tool], "thread": item_thread, "turn": item_turn,
                           "item": item.get("id"), "duration_ms": item.get("durationMs")}
            elif item_type in ("commandExecution", "fileChange", "reasoning") and (related or not scoped):
                label = {"commandExecution": "命令", "fileChange": "文件差异", "reasoning": "推理摘要"}[item_type]
                if item_type == "reasoning":
                    parts, _ = reasoning_chunks.pop((item_thread, item_turn, item.get("id")), ({}, 0))
                    summary = item.get("summary")
                    preview = "\n".join(block for block in summary if isinstance(block, str)).strip() if isinstance(summary, list) else ""
                    preview = (preview or "\n".join(parts[index] for index in sorted(parts)))[:MAX_REASONING_CHARS]
                elif item_type == "fileChange":
                    preview = "\n".join(f"{change.get('path', '')}\n{change.get('diff', '')}" for change in item.get("changes", []))
                else:
                    preview = "\n".join(value for value in (item.get("command"), item.get("aggregatedOutput") or last_delta) if isinstance(value, str))
                if preview.strip():
                    yield {"type": "harness.item", "preview": preview[:MAX_SUMMARY_CHARS], "label": label,
                           "kind": item_type,
                           "item": item.get("id"), **origin(item_thread, item_turn),
                           "duration_ms": item.get("durationMs")}
        elif method in ("thread/compacted", "contextCompacted") and thread and (
                params.get("threadId") == thread or params.get("threadId") in children):
            yield compaction_event(params["threadId"], params.get("turnId"))
        elif method == "thread/tokenUsage/updated" and params.get("threadId") == thread:
            tokens = (params.get("tokenUsage", {}).get("total") or {}).get("totalTokens")
            if type(tokens) is int and tokens >= 0:
                yield {"type": "harness.tokens", "value": tokens}
        elif method == "turn/completed" and scoped and params.get("threadId") in children:
            child = params["threadId"]
            status = (params.get("turn") or {}).get("status")
            state = {"completed": "completed", "interrupted": "interrupted", "failed": "errored"}.get(status)
            if state:
                yield {"type": "subagent", "id": child, "parent": children[child][0], "status": state, **origin(child, None)}
        elif method == "turn/completed" and params.get("threadId") == thread:
            turn = params.get("turn") or {}
            if turn_id and (turn.get("id") or params.get("turnId")) not in (None, turn_id):
                continue
            if turn.get("status") == "completed":
                yield {"type": "turn.completed"}
            else:
                status = turn.get("status")
                detail, diagnostic = _protocol_error(turn.get("error"), method, redact, subscription=bool(subscription),
                    default_code="CODEX_TURN_INTERRUPTED" if status == "interrupted" else "CODEX_TURN_INCOMPLETE")
                yield {"type": "turn.failed", "detail": detail or last_error[0] or (
                    "Codex 执行轮已中断" if status == "interrupted" else "Codex 执行轮未正常完成"),
                    "diagnostic": {**(diagnostic if detail else last_error[1] or diagnostic),
                                   "turn_status": status or "unknown"}}
            return
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=.2)
    yield {"type": "turn.failed", "detail": "Codex App Server 在执行轮结束前退出" if thread else
           "Codex App Server 在建立线程前退出",
           "diagnostic": {**last_error[1], "code": "CODEX_APP_SERVER_EXIT", "method": "stdio",
                          "exit_code": process.poll()}}


def _approval_result(method, params, pending_items, approvals, stop, redact=_redact, *, allow_elevation=True):
    item = pending_items.get(params.get("itemId"), {})
    reason = params.get("reason") or ""
    additional = params.get("additionalPermissions")
    root = params.get("grantRoot")
    cwd = params.get("cwd") or item.get("cwd")
    network_scope = None
    if method == "item/commandExecution/requestApproval":
        network = params.get("networkApprovalContext")
        if (isinstance(network, dict) and network.get("protocol") == "https"
                and isinstance(network.get("host"), str)
                and re.fullmatch(r"[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?", network["host"])):
            network_scope = "https://" + network["host"].lower()
        label = "网络访问审批" if network else "命令执行审批"
        detail = json.dumps(network, ensure_ascii=False) if network else params.get("command") or item.get("command") or ""
    elif method == "item/fileChange/requestApproval":
        label = "文件变更审批"
        detail = "\n".join(str(change.get("path", "")) + "\n" + str(change.get("diff", ""))
                           for change in item.get("changes", []))
    elif method == "item/permissions/requestApproval":
        label = "额外权限审批"
        detail = json.dumps(params.get("permissions") or {}, ensure_ascii=False, indent=2)
    else:
        return None
    available = params.get("availableDecisions")
    can_approve = allow_elevation and bool(detail) and (available is None or "accept" in available)
    preview = "\n".join(part for part in (f"工作目录 {cwd}" if cwd else "", f"请求目录 {root}" if root else "",
                        str(detail), json.dumps(additional, ensure_ascii=False) if additional else "") if part)
    correction = ""
    if approvals is None:
        decision = "decline"
    elif (network_scope and can_approve and approvals.network_policy
          and approvals.network_policy(network_scope, "check")):
        decision = "accept"
    else:
        decision, correction = approvals.ask({"label": label, "reason": redact(str(reason)), "detail": redact(preview),
                                              "thread": params.get("threadId"), "risk": "网络访问" if network_scope else "权限提升",
                                              "network_scope": network_scope if approvals.network_policy else None,
                                              "can_approve": can_approve and len(preview) <= MAX_SUMMARY_CHARS}, stop)
    if method == "item/permissions/requestApproval":
        return {"scope": "turn", "permissions": params.get("permissions", {}) if decision == "accept" else {},
                "_correction": correction}
    if decision == "decline" and available is not None and "decline" not in available:
        decision = "cancel"
    return {"decision": decision, "_correction": correction}


def _execute(binary, prompt, port, on_event, stop, gateway, guidance, permissions, catalog, approvals=None,
             existing_thread=None, fork_thread=False, *, scoped=False, ephemeral=False, subscription=None,
             guard_check=None, max_duration=None, context_window=None):
    server_name = "industplatform_" + uuid4().hex[:12] if subscription else "industplatform"
    server = "mcp_servers." + server_name
    launch = {"state_home": subscription["state"], "shared_login": True, "mcp_server": server_name} if subscription else {}
    args = codex_args(binary, port, gateway=gateway, guidance=guidance, **launch)
    if subscription:
        inherited = subscription["mcp_servers"]
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in inherited):
            raise AgentError("本机 Codex 的 MCP 服务名无法安全隔离")
        # Keep disabled project entries valid when Codex loads user-only config for account RPCs.
        inactive_transport = {
            f"mcp_servers.{name}.{kind}": sys.executable if kind == "command" else "http://127.0.0.1:1/mcp"
            for name, kind in subscription["project_mcp_transports"].items()
        }
        args.extend(config_args({
            **codex_local.NATIVE_SETTINGS,
            **inactive_transport,
            **{f"mcp_servers.{name}.enabled": False for name in inherited},
            server + ".enabled": True,
        }))
    if scoped:
        if not subscription:
            args[4:4] = ["-c", "mcp_servers={}"]
        args.extend(config_args({
            "features.shell_tool": False, "features.apply_patch_freeform": False,
            "features.multi_agent": False, "features.multi_agent_v2": False,
            "features.apps": False, "features.hooks": False, "web_search": "disabled",
            server + ".tool_timeout_sec": 180,
            server + ".env.INDUST_SCOPE_SESSION": "1",
        }))
    sandbox, approval_policy, review_settings = codex_permissions.cli_permissions(permissions)
    args.extend(config_args({**review_settings, **catalog}))
    args.extend(config_args({
        server + ".env.INDUST_CODEX_GUI_MCP": "1", server + ".required": True,
        server + ".default_tools_approval_mode": "approve",
        "shell_environment_policy.inherit": "none" if subscription else "core",
    }))
    args.extend(["app-server", "--listen", "stdio://"])
    summary = ""
    completed = False
    failure = None
    retry_failure = ""
    tool_events = 0
    process = None
    stderr_reader = None
    stderr_tail = bytearray()
    events = None
    secrets = ()
    if gateway:
        secrets = tuple(filter(None, ((gateway.profile or {}).get("api_key"), gateway.model_token, gateway.mcp_token,
                                      (gateway.reviewer.profile or {}).get("api_key") if gateway.reviewer else None,
                                      gateway.worker.profile.get("api_key") if gateway.worker else None)))

    def redact_preview(text):
        for secret in secrets:
            text = text.replace(secret, "[已隐藏凭证]")
        return _redact(text)

    def read_stderr(stream):
        with suppress(OSError, ValueError):
            while chunk := stream.read1(2048):
                stderr_tail.extend(chunk)
                del stderr_tail[:-4096]

    try:
        # Codex App Server migrates its repository-local SQLite state on cold start.
        # Wait for the thread handshake before opening a second native process.
        with APP_SERVER_START_LOCK:
            if stop and stop.is_set():
                raise AgentError("Codex 运行已停止")
            try:
                if subscription:
                    env = codex_local.native_environment(subscription["state"])
                    env[codex_gateway.MCP_TOKEN] = gateway.mcp_token
                else:
                    env = gateway.environment() if gateway else codex_environment()
                process = subprocess.Popen(
                    args, cwd=ROOT, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env,
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                raise AgentError("无法启动本机 Codex CLI，请检查安装位置和运行目录",
                                 code=exc.errno or "CODEX_SPAWN_FAILURE",
                                 detail=diagnostic_text(exc, redact_preview)) from None
            stderr_reader = threading.Thread(target=read_stderr, args=(process.stderr,), daemon=True,
                                             name="indust-codex-stderr")
            stderr_reader.start()
            events = _app_server_events(
                process, prompt, sandbox, approval_policy, review_settings["approvals_reviewer"],
                None if scoped and not subscription else approvals, stop,
                existing_thread=existing_thread, fork_thread=fork_thread, redact=redact_preview, ephemeral=ephemeral,
                subscription=subscription, mcp_server=server_name, scoped=scoped, guard_check=guard_check,
                max_duration=max_duration, context_window=context_window,
            )
            first_event = next(events, None)
            if first_event is None:
                raise AgentError("Codex App Server 在建立线程前退出，请检查 CLI 版本及仓库运行目录")
        for event in chain((first_event,), events):
            event_type = event.get("type")
            if event_type == "turn.completed":
                completed = True
            elif event_type == "turn.failed":
                failure = AgentError(event.get("detail") or "Codex 执行轮未完成")
                failure.diagnostic.update(event.get("diagnostic", {}))
            elif event_type in ("harness.thread", "harness.item", "harness.tokens", "harness.progress"):
                if event_type == "harness.item":
                    preview = event["preview"]
                    event["preview"] = (diagnostic_text(preview, redact_preview) if event.get("kind") == "tool"
                                        else redact_preview(preview))
                elif event_type == "harness.progress":
                    if event.get("kind") == "retrying":
                        retry_failure = event.get("detail", "")
                    elif event.get("kind") not in ("waiting", "reasoning"):
                        retry_failure = ""
                on_event(event_type, event, None)
            elif event_type in ("harness.turn", "subagent"):
                on_event(event_type, event, None)
            elif event_type == "steering":
                on_event("status" if event["accepted"] else "warning",
                         "已发送本轮纠偏指令" if event["accepted"] else "执行轮已变化，纠偏未送达", None)
            if event_type != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                summary = item["text"].strip()
            elif item.get("type") in ("collabAgentToolCall", "collab_agent_tool_call", "collab_tool_call"):
                states = item.get("agentsStates") or item.get("agents_states")
                if isinstance(states, dict):
                    for child, state in states.items():
                        status = state.get("status") if isinstance(state, dict) else None
                        if (isinstance(child, str) and len(child) <= 100 and status in
                                ("pendingInit", "running", "interrupted", "completed", "errored", "shutdown", "notFound")):
                            details = {"id": child, "status": status, "parent": item.get("senderThreadId")}
                            if item.get("tool") == "spawnAgent":
                                details.update(model=item.get("model"), effort=item.get("reasoningEffort"))
                            on_event("subagent", {key: value for key, value in details.items() if value is not None}, None)
            elif item.get("type") == "subAgentActivity":
                child = item.get("agentThreadId")
                status = {"started": "running", "interrupted": "interrupted", "completed": "completed"}.get(item.get("kind"))
                if isinstance(child, str) and len(child) <= 100:
                    details = {"id": child, "status": status, "path": item.get("agentPath"),
                               "model": item.get("model"), "effort": item.get("reasoningEffort")}
                    on_event("subagent", {key: value for key, value in details.items() if value is not None}, None)
            elif not gateway and item.get("type") == "mcp_tool_call" and item.get("server") == "industplatform":
                tool = item.get("tool")
                label = TOOL_LABELS.get(tool) if isinstance(tool, str) else None
                if label and tool_events < MAX_TOOL_EVENTS:
                    success = item.get("status") == "completed"
                    on_event("tool" if success else "warning", label, _related_run(item) if success else None)
                    tool_events += 1
    except RunTimedOut as exc:
        failure = exc
        if retry_failure:
            failure.diagnostic["detail"] = failure.diagnostic.get("detail", "") + "\n" + retry_failure
    except AgentError as exc:
        failure = exc
    except (OSError, KeyError, TypeError, ValueError) as exc:
        failure = AgentError("Codex App Server 通信中断或返回了不兼容的数据", code="CODEX_PROTOCOL_FAILURE",
                             detail=diagnostic_text(exc, redact_preview))
    finally:
        if isinstance(stop, RunSignal):
            stop.set_turn(None)
        if events is not None:
            events.close()
        if process is not None:
            if process.poll() not in (None, 0):
                failure = failure or AgentError("Codex App Server 异常退出", code="CODEX_APP_SERVER_EXIT")
                failure.diagnostic["exit_code"] = process.returncode
            codex_local.stop_process(process)
            stderr_reader.join(timeout=.2)
            if failure and stderr_tail:
                detail = diagnostic_text(stderr_tail.decode("utf-8", errors="replace"), redact_preview)
                if detail:
                    failure.diagnostic["stderr"] = detail
            with suppress(OSError):
                process.stdin.close()
            process.stdout.close()
            process.stderr.close()
    if failure or not completed:
        if gateway and gateway.error == codex_gateway.TURN_LIMIT_ERROR and not (
            failure and failure.diagnostic.get("code") in (
                "network_idle_timeout", "stage_max_duration_exceeded", GUARD_ABORT_CODE, STAGE_TOOL_LIMIT_CODE)
        ):
            error = TurnBudgetExhausted(gateway.error, code="CODEX_TOOL_TURN_LIMIT")
            if failure:
                error.diagnostic.update(failure.diagnostic)
            raise error
        if gateway and gateway.error and failure:
            failure.diagnostic["gateway"] = diagnostic_text(gateway.error, redact_preview)
        raise failure or AgentError((gateway.error if gateway else None) or "Codex 执行轮未完成",
                                    code="CODEX_TURN_INCOMPLETE")
    if not summary:
        raise AgentError("Codex 已结束，本轮未取得最终回复", code="CODEX_EMPTY_REPLY")
    if gateway:
        review_key = gateway.reviewer.profile.get("api_key", "") if gateway.reviewer else ""
        if gateway.reviewer and gateway.reviewer.error:
            on_event("warning", "审批模型请求失败：" + gateway.reviewer.error, None)
        for secret in ((gateway.profile or {}).get("api_key", ""), review_key, gateway.model_token, gateway.mcp_token):
            if secret:
                summary = summary.replace(secret, "[已隐藏凭证]")
    return _redact(summary)

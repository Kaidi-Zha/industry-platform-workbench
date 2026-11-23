"""Per-session loopback provider for Codex, with private upstream credentials."""

import hmac
import http.client
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import codex_protocol as wire, provider_api as api
from .codex_permissions import REVIEW_ALIAS
from .codex_runtime import codex_environment

PROVIDER = "industplatform_api"
WORKER_ALIAS = "industplatform-worker-model"
MODEL_TOKEN = "INDUST_CODEX_MODEL_TOKEN"
MCP_TOKEN = "INDUST_CODEX_MCP_TOKEN"
REMOTE_CAP = 8 * 1024 * 1024
TURN_LIMIT_ERROR = "本轮已达到模型请求上限"
COLLABORATION_TOOLS = frozenset(("spawn_agent", "send_input", "wait", "wait_agent", "close_agent"))


class Gateway:
    def __init__(self, profile, tools, max_turns, reviewer=None, *, scoped=False, worker=None, collaboration=False):
        self.profile = profile
        self.tools = tools
        self.scoped = scoped
        self.collaboration = collaboration
        self.max_turns = max_turns
        self.turns = 0
        self.model_token = secrets.token_urlsafe(32)
        self.mcp_token = secrets.token_urlsafe(32)
        self.stopped = threading.Event()
        self.model_lock = threading.Lock()
        self.tool_lock = threading.Lock()
        self.port = None
        self.error = None
        self.history = {}
        self.translator = (wire.ChatTranslation() if profile and profile["protocol"] == api.CHAT
                           else wire.Translation(profile["protocol"]) if profile else None)
        if worker and (not profile or "astra" in worker["model"].lower()):
            raise ValueError("请选择允许委派的非 Astra 模型")
        self.worker = (Gateway({key: value for key, value in worker.items() if key != "multi_agent"},
                               tools if scoped else None, max_turns, scoped=scoped)
                       if worker else None)
        self.reviewer = Gateway({key: value for key, value in reviewer.items() if key != "multi_agent"}, None, max_turns) if reviewer else None

    def request_model(self, payload, headers):
        if payload.get("model") == WORKER_ALIAS:
            if not self.worker:
                raise api.ProviderError("本次会话未配置子代理模型")
            return self.worker, {**payload, "model": self.worker.profile["model"]}
        if payload.get("model") != REVIEW_ALIAS:
            return self, payload
        metadata = payload.get("client_metadata") or {}
        if self.reviewer is None or not (
            metadata.get("x-openai-subagent") == "guardian" or headers.get("x-codex-guardian")
        ):
            raise api.ProviderError("审批模型仅接受 Codex Guardian 的请求")
        return self.reviewer, {**payload, "model": self.reviewer.profile["model"]}

    def environment(self):
        env = codex_environment()
        if self.profile:
            env[MODEL_TOKEN] = self.model_token
        if self.tools:
            env[MCP_TOKEN] = self.mcp_token
        return env

    def _provider_settings(self, provider, path):
        fields = {
            "name": provider,
            "base_url": f"http://127.0.0.1:{self.port}{path}",
            "env_key": MODEL_TOKEN,
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
            "request_max_retries": 0,
            "stream_max_retries": 0,
        }
        return {f"model_providers.{provider}.{field}": value for field, value in fields.items()}

    def settings(self):
        settings = {"features.enable_request_compression": False,
                    "shell_environment_policy.exclude": [MODEL_TOKEN, MCP_TOKEN]}
        if not self.profile:
            return settings
        settings.update(self._provider_settings(PROVIDER, "/v1"))
        settings.update(model_provider=PROVIDER, model=self.profile["model"])
        context_window = self.profile.get("capabilities", {}).get("context_window")
        if context_window:
            settings["model_context_window"] = context_window
        effort = self.profile.get("reasoning_effort")
        if "astra" in self.profile["model"].lower() or self.profile.get("multi_agent"):
            settings.update({"features.multi_agent": False, "features.multi_agent_v2": False})
        if self.tools:
            settings["features.apps"] = False
        if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            settings["model_reasoning_effort"] = effort
        return settings

    def _restore_history(self, items):
        if not isinstance(items, list):
            return items
        restored, seen = [], set()
        for item in items:
            key = item.get("call_id") if item.get("type") in {"function_call", "custom_tool_call"} else item.get("id")
            turn = self.history.get(key)
            if turn is None:
                restored.append(item)
            elif turn[0] not in seen:
                restored.extend(turn[1])
                seen.add(turn[0])
        return restored

    def _remember(self, response):
        output = response["output"]
        turn = (response["id"], output)
        for item in output:
            for key in (item.get("id"), item.get("call_id") if item.get("type") in {"function_call", "custom_tool_call"} else None):
                if key:
                    self.history[key] = turn
        while len(self.history) > 512:
            self.history.pop(next(iter(self.history)))

    def _openai_request(self, handler, payload, path, transport, timeout):
        opener, base, headers = transport
        ultra = self.profile.get("multi_agent")
        deepseek = api._deepseek_responses(api.OPENAI, base)
        restore = bool(ultra or deepseek)
        aliases = {}
        outgoing = {**payload, "model": self.profile["model"]}
        if restore and "input" in outgoing:
            with self.model_lock:
                outgoing["input"] = self._restore_history(outgoing["input"])
        if deepseek and "input" in outgoing:
            outgoing["input"] = wire.deepseek_input(outgoing["input"])
        if deepseek and "tools" in outgoing:
            outgoing["tools"], aliases = wire.flatten_response_tools(outgoing)
        if path == "/responses":
            # The saved profile owns the effective effort and the Ultra switch.
            outgoing.pop("reasoning", None)
            self._effort(outgoing, api.OPENAI, base)
            outgoing.pop("multi_agent", None)
            if ultra:
                headers["OpenAI-Beta"] = "responses_multi_agent=v1"
                outgoing["multi_agent"] = ultra
                outgoing["include"] = list(dict.fromkeys([*(outgoing.get("include") or []),
                                                         "reasoning.encrypted_content"]))
            outgoing["store"] = False
            outgoing["stream"] = True
            headers["Accept"] = "text/event-stream"
        data = api._json_dump(outgoing).encode()
        if len(data) > REMOTE_CAP:
            raise api.ProviderError("模型请求超过大小限制")
        request = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
        with opener.open(request, timeout=timeout) as upstream:
            if path != "/responses" or upstream.headers.get_content_type() != "text/event-stream":
                reply = json.loads(_read(upstream, REMOTE_CAP))
                if path != "/responses":
                    handler.json(200, reply)
                    return
            elif ultra:
                reply = None
                for line in _lines(upstream, self.stopped):
                    event = _event(line)
                    if event and event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                        reply = event["response"]
                if reply is None:
                    raise api.ProviderError("Ultra 未返回完整响应")
            else:
                handler.start_sse()
                for line in _lines(upstream, self.stopped):
                    if restore:
                        event = _event(line)
                        if event:
                            if event.get("type") == "response.completed":
                                response = event["response"]
                                with self.model_lock:
                                    self._remember(response)
                                response["output"] = [wire.unflatten_item(item, aliases) for item in response["output"]]
                            elif isinstance(event.get("item"), dict):
                                event["item"] = wire.unflatten_item(event["item"], aliases)
                            line = b"data: " + api._json_dump(event).encode() + b"\n"
                    handler.chunk(line)
                return
        if restore:
            with self.model_lock:
                self._remember(reply)
        if aliases:
            reply = {**reply, "output": [wire.unflatten_item(item, aliases) for item in reply["output"]]}
        if ultra:
            visible = [item for item in reply["output"] if item.get("type") in {"function_call", "custom_tool_call"}
                       or (item.get("type") == "message" and item.get("phase") == "final_answer"
                           and item.get("agent", {}).get("agent_name") == "/root")]
            if reply.get("status") == "completed" and not visible:
                raise api.ProviderError("Ultra 未返回主 Agent 回复或待执行工具")
            reply = {**reply, "output": visible}
        handler.send_sse(reply)

    def _effort(self, payload, protocol, base):
        api._apply_reasoning(payload, protocol, self.profile["model"], base,
                             self.profile.get("reasoning_effort", ""),
                             override=self.profile.get("reasoning_level_override"),
                             discovered=self.profile.get("discovered_reasoning_levels"))

    def response(self, handler, payload, path):
        profile = self.profile
        protocol = profile["protocol"]
        if payload.get("model") != profile["model"]:
            raise api.ProviderError("本次 CLI 会话使用已选模型。切换模型请重新启动会话")
        with self.model_lock:
            if self.max_turns is not None and self.turns >= self.max_turns:
                raise api.ProviderError(TURN_LIMIT_ERROR)
            self.turns += 1
        if self.scoped and path == "/responses":
            allowed = {tool["name"] for tool in self.tools[0]}
            filtered = []
            for spec in payload.get("tools", []):
                name = spec.get("name", "")
                if spec.get("type") == "namespace" and name in ("mcp__industplatform", "mcp__industplatform__"):
                    filtered.append({**spec, "tools": [tool for tool in spec.get("tools", []) if tool.get("name") in allowed]})
                elif spec.get("type") == "function" and name.startswith("mcp__industplatform__") and name.removeprefix("mcp__industplatform__") in allowed:
                    filtered.append(spec)
                elif self.collaboration and spec.get("type") == "namespace" and name == "collaboration":
                    filtered.append({**spec, "tools": [tool for tool in spec.get("tools", []) if tool.get("name") in COLLABORATION_TOOLS]})
            payload = {**payload, "tools": filtered}
        parallel = profile.get("capabilities", {}).get("parallel_tool_calls")
        if type(parallel) is bool and payload.get("tools"):
            payload = {**payload, "parallel_tool_calls": parallel}
        elif protocol in (api.ANTHROPIC, api.CHAT) and "parallel_tool_calls" in payload:
            payload = {key: value for key, value in payload.items() if key != "parallel_tool_calls"}
        transport = api._transport(profile, protocol)
        timeout = api.GENERATION_TIMEOUT if profile.get("multi_agent") else 90
        if protocol == api.OPENAI:
            if path != "/responses" and profile.get("multi_agent"):
                raise api.ProviderError("Ultra 由模型服务管理上下文压缩")
            self._openai_request(handler, payload, path, transport, timeout)
            return
        if path != "/responses":
            raise api.ProviderError("当前模型接口没有远程上下文压缩端点")
        if payload.get("previous_response_id"):
            raise api.ProviderError("当前模型接口需要完整的 Codex 会话上下文")
        with self.model_lock:
            translated, identities = self.translator.request(payload, profile["model"])
        opener, base, headers = transport
        self._effort(translated, protocol, base)
        endpoint = {api.ANTHROPIC: "/messages", api.CHAT: "/chat/completions",
                    api.GEMINI: "/models/" + profile["model"] + ":generateContent"}[protocol]
        reply = api._post(opener, api.endpoint(protocol, base, endpoint), headers, translated,
                          time.monotonic() + timeout, self.stopped.is_set, request_timeout=timeout)
        with self.model_lock:
            response = self.translator.response(reply, identities, profile["model"])
        handler.send_sse(response)


def _read(stream, limit):
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise api.ProviderError("模型响应超出大小限制")
    return data


def _lines(stream, stopped):
    length = 0
    while not stopped.is_set():
        line = stream.readline(REMOTE_CAP + 1 - length)
        if not line:
            return
        length += len(line)
        if length > REMOTE_CAP:
            raise api.ProviderError("模型响应超出大小限制")
        yield line


def _event(line):
    if line.startswith(b"data:"):
        data = line[5:].strip()
        if data and data != b"[DONE]":
            return json.loads(data)
    return None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(95)
        self.streaming = False

    def json(self, status, payload):
        body = api._json_dump(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.chunk(body)

    def chunk(self, body):
        self.wfile.write(body)
        self.wfile.flush()

    def start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.streaming = True

    def send_sse(self, response):
        self.start_sse()
        for event in wire.sse(wire.events(response)):
            self.chunk(event)

    def _authorized(self, token):
        header = self.headers.get("Authorization", "")
        return (not self.headers.get("Origin") and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                and hmac.compare_digest(header.encode("utf-8"), ("Bearer " + token).encode()))

    def do_POST(self):
        gateway = self.server.gateway
        tool = self.path == "/mcp"
        token = gateway.mcp_token if tool else gateway.model_token
        if not self._authorized(token):
            self.json(401, {"error": {"message": "需要本机 Codex 会话凭据"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/json" or not 1 <= length <= REMOTE_CAP:
                raise ValueError("request size or type")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request")
            if tool and gateway.tools:
                definitions, call = gateway.tools
                with gateway.tool_lock:
                    if gateway.stopped.is_set():
                        raise api.ProviderError("本机 Codex 会话已经结束")
                    if payload.get("method") == "tools/list":
                        result = {"tools": definitions}
                    else:
                        result = call(payload["name"], payload["arguments"])
                self.json(200, {"result": result})
            elif gateway.profile and self.path in {"/v1/responses", "/v1/responses/compact"}:
                gateway, payload = gateway.request_model(payload, self.headers)
                gateway.response(self, payload, self.path.removeprefix("/v1"))
            else:
                self.json(404, {"error": {"message": "未找到本地供应商端点"}})
        except (BrokenPipeError, ConnectionResetError):
            return
        except urllib.error.HTTPError as exc:
            exc.close()
            self.error(gateway, f"模型服务返回 HTTP {exc.code}，请检查接口地址、模型配置和凭据", exc.code)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            self.error(gateway, "模型服务连接中断或超时", 502)
        except api.ProviderError as exc:
            self.error(gateway, str(exc), 400)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
            self.error(gateway, "模型适配器收到不兼容的请求或响应", 400)

    def error(self, gateway, message, code):
        gateway.error = message
        try:
            if self.streaming:
                for event in wire.sse([("error", {"code": "provider_error", "message": message})]):
                    self.chunk(event)
            else:
                self.json(code, {"error": {"message": message}})
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def serve(profile=None, *, tools=None, max_turns=None, reviewer=None, scoped=False, worker=None, collaboration=False):
    gateway = Gateway(profile, tools, max_turns, reviewer, scoped=scoped, worker=worker, collaboration=collaboration)
    with ThreadingHTTPServer(("127.0.0.1", 0), _Handler) as server:
        server.gateway = gateway
        gateway.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03}, daemon=True)
        thread.start()
        try:
            yield gateway
        finally:
            gateway.stopped.set()
            for child in (gateway.reviewer, gateway.worker):
                if child:
                    child.stopped.set()
            server.shutdown()
            thread.join()
            # Finish or reject queued tool callbacks before a paused run can be resumed.
            with gateway.tool_lock:
                pass

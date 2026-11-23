"""Native text and client-side function calling for model providers."""

import http.client
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


OPENAI = "openai-responses"
CHAT = "openai-chat-completions"
ANTHROPIC = "anthropic-messages"
GEMINI = "gemini-generate-content"
DEFAULT_BASE_URLS = {
    OPENAI: "https://api.openai.com/v1",
    CHAT: "https://api.openai.com/v1",
    ANTHROPIC: "https://api.anthropic.com/v1",
    GEMINI: "https://generativelanguage.googleapis.com/v1beta",
}
DEFAULT_MULTI_AGENT_CONCURRENCY = 3
OPENAI_MULTI_AGENT_MODELS = {
    "gpt-6.2-sol", "gpt-6.1-sol", "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}\Z")
GEMINI_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
KEY = re.compile(r"[!-~]{1,2048}\Z")
EFFORT_LEVELS = ("low", "medium", "high")
DEEPSEEK_EFFORTS = ("none", "low", "high", "max")
DEEPSEEK_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}
UNSUPPORTED_EFFORT = "该模型不支持所选推理强度，请改用供应商默认值或选择支持的强度"
PROTOCOL_REASONING_EFFORTS = {
    OPENAI: ("", "none", "minimal", *EFFORT_LEVELS, "xhigh", "max"),
    CHAT: ("", "none", "minimal", *EFFORT_LEVELS, "xhigh", "max"),
    ANTHROPIC: ("", *EFFORT_LEVELS, "xhigh", "max"),
    GEMINI: ("", "none", "minimal", *EFFORT_LEVELS, "xhigh"),
}
OPENAI_EFFORTS = {
    "o1": EFFORT_LEVELS, "o3": EFFORT_LEVELS, "o3-mini": EFFORT_LEVELS,
    "o4-mini": EFFORT_LEVELS, "o3-pro": ("high",),
    "gpt-5": ("minimal", *EFFORT_LEVELS),
    "gpt-5-mini": ("minimal", *EFFORT_LEVELS),
    "gpt-5-nano": ("minimal", *EFFORT_LEVELS),
    "gpt-5-pro": ("high",), "gpt-5.1": ("none", *EFFORT_LEVELS),
    "gpt-5.2": ("none", *EFFORT_LEVELS, "xhigh"),
    "gpt-5.2-pro": ("medium", "high", "xhigh"),
    "gpt-5.3-codex": (*EFFORT_LEVELS, "xhigh"),
    "gpt-5.4": ("none", *EFFORT_LEVELS, "xhigh"),
    "gpt-5.4-mini": ("none", *EFFORT_LEVELS, "xhigh"),
    "gpt-5.4-nano": ("none", *EFFORT_LEVELS, "xhigh"),
    "gpt-5.4-pro": ("medium", "high", "xhigh"),
    "gpt-5.5": ("none", *EFFORT_LEVELS, "xhigh"),
    "gpt-5.5-pro": ("medium", "high", "xhigh"),
    "gpt-5.6": ("none", *EFFORT_LEVELS, "xhigh", "max"),
    "gpt-5.6-sol": ("none", *EFFORT_LEVELS, "xhigh", "max"),
    "gpt-6-sol": (*EFFORT_LEVELS, "xhigh", "max"),
    "gpt-6-luna": (*EFFORT_LEVELS, "xhigh", "max"),
    "gpt-6.1-sol": (*EFFORT_LEVELS, "xhigh", "max"),
    "gpt-6.2-sol": (*EFFORT_LEVELS, "xhigh", "max"),
    "gpt-6.2-astra": (*EFFORT_LEVELS, "xhigh", "max"),
}
CLAUDE_EFFORTS = {
    "claude-opus-4": EFFORT_LEVELS,
    "claude-sonnet-4": EFFORT_LEVELS,
    "claude-opus-4-5": EFFORT_LEVELS,
    "claude-sonnet-4-5": EFFORT_LEVELS,
    "claude-haiku-4-5": EFFORT_LEVELS,
    "claude-opus-4-6": (*EFFORT_LEVELS, "max"),
    "claude-sonnet-4-6": (*EFFORT_LEVELS, "max"),
    "claude-opus-4-7": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-opus-4-8": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-opus-5": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-opus-5-5": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-sonnet-5": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-sonnet-5-5": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-haiku-5-5": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-fable-5": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-fable-5-1": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-fable-5-2": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-mythos-5": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-mythos-5-1": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-mythos-5-2": (*EFFORT_LEVELS, "xhigh", "max"),
    "claude-mythos-preview": (*EFFORT_LEVELS, "max"),
}
CLAUDE_THINKING_BUDGETS = {
    "claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5",
    "claude-opus-4", "claude-sonnet-4",
}
CLAUDE_BUDGET_LEVELS = {"low": 1024, "medium": 2048, "high": 4096}
GEMINI_THINKING_BUDGETS = {"none": 0, "low": 512, "medium": 1024, "high": 1536}
GEMINI_BUDGET_MODELS = {
    "gemini-2.5-pro": EFFORT_LEVELS,
    "gemini-2.5-flash": ("none", *EFFORT_LEVELS),
    "gemini-2.5-flash-lite": ("none", *EFFORT_LEVELS),
    "gemini-2.5-flash-preview": ("none", *EFFORT_LEVELS),
    "gemini-2.5-flash-lite-preview": ("none", *EFFORT_LEVELS),
}
GEMINI_LEVEL_MODELS = {
    "gemini-3-pro-preview": ("low", "high"),
    "gemini-3.1-pro-preview": EFFORT_LEVELS,
    "gemini-3-flash-preview": ("minimal", *EFFORT_LEVELS),
    "gemini-3.1-flash-lite": ("minimal", *EFFORT_LEVELS),
    "gemini-3.1-flash-lite-preview": ("minimal", *EFFORT_LEVELS),
    "gemini-3.5-flash": ("minimal", *EFFORT_LEVELS),
    "gemini-3.5-flash-lite": ("minimal", *EFFORT_LEVELS),
    "gemini-3.6-flash": ("minimal", *EFFORT_LEVELS),
    "gemini-3.8-flash": EFFORT_LEVELS,
    "gemini-4-carbon": EFFORT_LEVELS,
    "gemini-4-argon": EFFORT_LEVELS,
    "gemini-4-neon": (*EFFORT_LEVELS, "xhigh"),
    "gemini-4-hydrogen": (*EFFORT_LEVELS, "xhigh"),
}
MAX_DISCOVERY_PAGES = 5
MAX_DISCOVERED_MODELS = 200
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_REPLY_BYTES = 1024 * 1024
MAX_TURNS = 24
MAX_CALLS_PER_TURN = 16
REQUEST_TIMEOUT = 30
GENERATION_TIMEOUT = 180


class ProviderError(RuntimeError):
    """Provider setup, request, or response failure."""

    def __init__(self, message, *, code=None, detail=""):
        super().__init__(message)
        self.code = code
        self.detail = detail


class ToolTurnLimit(ProviderError):
    """The provider requested tools at the end of the execution budget."""


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_base_url(protocol, value):
    base = value.rstrip("/") if value else DEFAULT_BASE_URLS[protocol]
    try:
        if re.search(r"[^!-~]|[\\?#]", base):
            raise ValueError
        parsed = urllib.parse.urlsplit(base)
        host = parsed.hostname
        port = parsed.port
        if (not host or "%" in parsed.netloc or parsed.username is not None
                or parsed.password is not None or port == 0):
            raise ValueError
        authority = "[{}]".format(host) if ":" in host else host
        if not re.fullmatch(re.escape(authority) + r"(?::[0-9]+)?", parsed.netloc.lower()):
            raise ValueError
        if ":" not in host and not re.fullmatch(r"[a-z0-9._-]+", host):
            raise ValueError
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and host in LOOPBACK_HOSTS)):
            raise ValueError
    except ValueError:
        raise ValueError("请输入 HTTPS API 基址，只有本机接口支持 HTTP。基址不得含查询参数或凭证。") from None
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority += ":{}".format(port)
    return urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def normalize_model(protocol, value):
    if not isinstance(value, str) or not MODEL_ID.fullmatch(value):
        raise ValueError("请填写有效的模型 ID")
    if protocol == GEMINI:
        value = value.removeprefix("models/")
        if not GEMINI_MODEL_ID.fullmatch(value):
            raise ValueError("Gemini 连接需要 generateContent 的模型 ID，例如 gemini-...。")
    return value


def _model_family(model):
    model = model.removeprefix("models/")
    return re.sub(r"-(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{2}-\d{2}|latest)$", "", model)


def supports_multi_agent(protocol, model, base_url):
    """Check eligibility for native Responses Multi-agent mode."""
    if (protocol != OPENAI or not isinstance(base_url, str)
            or not isinstance(model, str) or "astra" in model.lower()
            or model.startswith("models/") or _model_family(model) not in OPENAI_MULTI_AGENT_MODELS):
        return False
    try:
        return normalize_base_url(protocol, base_url) == DEFAULT_BASE_URLS[OPENAI]
    except ValueError:
        return False


def normalize_multi_agent(value, protocol, model, base_url):
    """Validate the optional native Multi-agent configuration."""
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get("enabled")) is not bool
            or value.keys() - {"enabled", "max_concurrent_subagents"}):
        raise ValueError("Multi-agent 配置无效")
    concurrency = value.get("max_concurrent_subagents", DEFAULT_MULTI_AGENT_CONCURRENCY)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("并发子代理数必须为正整数")
    if not value["enabled"]:
        return None
    if not supports_multi_agent(protocol, model, base_url):
        raise ValueError("当前模型或 API 基址不支持 Multi-agent 模式")
    return {"enabled": True, "max_concurrent_subagents": concurrency}


def _deepseek(protocol, base_url):
    return protocol in (OPENAI, CHAT, ANTHROPIC) and urllib.parse.urlsplit(base_url).hostname == "api.deepseek.com"


def _deepseek_responses(protocol, base_url):
    return protocol == OPENAI and _deepseek(protocol, base_url)


def _google_compatible(protocol, base_url):
    return protocol == CHAT and urllib.parse.urlsplit(base_url).hostname == "generativelanguage.googleapis.com"


def endpoint(protocol, base, path):
    if (protocol == ANTHROPIC and not base.endswith("/v1")
            and urllib.parse.urlsplit(base).hostname in {"api.anthropic.com", "api.deepseek.com", "open.bigmodel.cn",
                                                       "api.z.ai", "openrouter.ai"}):
        base += "/v1"
    return base + path


def provider_family(protocol, base):
    location = urllib.parse.urlsplit(base)
    if location.hostname in LOOPBACK_HOSTS:
        return {11434: "ollama", 8000: "vllm"}.get(location.port, "custom")
    return {"api.openai.com": "openai", "api.anthropic.com": "anthropic",
            "api.deepseek.com": "deepseek", "generativelanguage.googleapis.com": "google",
            "api.x.ai": "xai", "us.api.x.ai": "xai", "openrouter.ai": "openrouter",
            "dashscope.aliyuncs.com": "dashscope", "open.bigmodel.cn": "glm", "api.z.ai": "glm",
            "api.siliconflow.cn": "siliconflow", "ark.cn-beijing.volces.com": "doubao",
            "api.minimaxi.com": "minimax", "api.minimax.io": "minimax", "api.minimax.chat": "minimax"}.get(location.hostname, "custom")


def supports_vision(protocol, model, base):
    family = _model_family(model)
    if family in GEMINI_LEVEL_MODELS or family in GEMINI_BUDGET_MODELS or family in CLAUDE_EFFORTS:
        return True
    if family == "deepseek-flash":
        return protocol == OPENAI
    return (family.startswith(("gpt-5", "gpt-6")) and family in OPENAI_EFFORTS
            or family == "grok-4.7" and provider_family(protocol, base) == "xai")


def canonical_reasoning_effort(protocol, base_url, effort):
    if _deepseek(protocol, base_url) and isinstance(effort, str):
        return DEEPSEEK_EFFORT_ALIASES.get(effort, effort)
    return effort


def normalize_effort_levels(protocol, base_url, values):
    """Validate and normalize a list of explicit effort IDs in display order."""
    if not isinstance(values, list):
        raise ValueError("推理强度必须为列表")
    allowed = (DEEPSEEK_EFFORTS if _deepseek(protocol, base_url)
               else PROTOCOL_REASONING_EFFORTS[protocol][1:])
    levels = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(UNSUPPORTED_EFFORT)
        level = canonical_reasoning_effort(protocol, base_url, value)
        if level not in allowed:
            raise ValueError(UNSUPPORTED_EFFORT)
        if level not in levels:
            levels.append(level)
    return levels


def _known_efforts(protocol, model, base_url):
    if _deepseek(protocol, base_url):
        return DEEPSEEK_EFFORTS
    family = _model_family(model)
    if _google_compatible(protocol, base_url) or (protocol == CHAT and family in GEMINI_LEVEL_MODELS):
        return GEMINI_LEVEL_MODELS.get(family) or GEMINI_BUDGET_MODELS.get(family)
    if protocol in (OPENAI, CHAT):
        if family == "grok-4.7" and provider_family(protocol, base_url) == "xai":
            return (*EFFORT_LEVELS, "xhigh")
        return OPENAI_EFFORTS.get(family)
    if protocol == ANTHROPIC:
        return CLAUDE_EFFORTS.get(family)
    return GEMINI_BUDGET_MODELS.get(family) or GEMINI_LEVEL_MODELS.get(family)


def reasoning_efforts(protocol, model, base_url="", *, override=None, discovered=None):
    """Resolve manual, declared, and fallback efforts in that order."""
    levels = override if override is not None else discovered
    if levels is not None:
        return ("", *normalize_effort_levels(protocol, base_url, levels))
    options = _known_efforts(protocol, model, base_url)
    if options is not None:
        return ("", *options)
    default_host = urllib.parse.urlsplit(DEFAULT_BASE_URLS[protocol]).hostname
    host = urllib.parse.urlsplit(base_url or DEFAULT_BASE_URLS[protocol]).hostname
    if host != default_host and not _google_compatible(protocol, base_url):
        return PROTOCOL_REASONING_EFFORTS[protocol]
    return ("",)


def _apply_reasoning(payload, protocol, model, base_url, effort, *, override, discovered):
    if not effort:
        return
    effort = canonical_reasoning_effort(protocol, base_url, effort)
    try:
        available = reasoning_efforts(protocol, model, base_url, override=override, discovered=discovered)
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    if effort not in available:
        raise ProviderError(UNSUPPORTED_EFFORT)
    family = _model_family(model)
    if _deepseek(protocol, base_url) and protocol != OPENAI:
        payload["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
        if effort != "none":
            if protocol == CHAT:
                payload["reasoning_effort"] = effort
            else:
                payload.setdefault("output_config", {})["effort"] = effort
    elif protocol == OPENAI:
        payload["reasoning"] = {"effort": effort}
    elif protocol == CHAT:
        payload["reasoning_effort"] = effort
    elif protocol == ANTHROPIC:
        if family in CLAUDE_THINKING_BUDGETS:
            if effort not in CLAUDE_BUDGET_LEVELS:
                raise ProviderError(UNSUPPORTED_EFFORT)
            budget = CLAUDE_BUDGET_LEVELS[effort]
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(payload["max_tokens"], budget + 512)
        elif family in CLAUDE_EFFORTS:
            payload["thinking"] = {"type": "adaptive"}
        if family not in CLAUDE_THINKING_BUDGETS or family == "claude-opus-4-5":
            payload.setdefault("output_config", {})["effort"] = effort
    else:
        if effort == "none" or (_model_family(model) in GEMINI_BUDGET_MODELS
                                and effort in GEMINI_THINKING_BUDGETS):
            thinking = {"thinkingBudget": GEMINI_THINKING_BUDGETS[effort]}
        else:
            thinking = {"thinkingLevel": effort.upper()}
        payload["generationConfig"]["thinkingConfig"] = thinking


def _transport(profile, protocol):
    base = profile.get("base_url", "")
    if not isinstance(base, str):
        raise ProviderError("模型接口基址无效")
    try:
        base = normalize_base_url(protocol, base)
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    local = urllib.parse.urlsplit(base).hostname in LOOPBACK_HOSTS
    key = profile.get("api_key", "")
    if not isinstance(key, str) or (key and not KEY.fullmatch(key)):
        raise ProviderError("模型接口密钥格式无效")
    if not key and not local:
        raise ProviderError("未设置模型接口密钥")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if protocol in (OPENAI, CHAT):
        if key:
            headers["Authorization"] = "Bearer " + key
    elif protocol == ANTHROPIC:
        headers["anthropic-version"] = "2023-06-01"
        if key:
            if urllib.parse.urlsplit(base).hostname == "openrouter.ai":
                headers["Authorization"] = "Bearer " + key
            else:
                headers["x-api-key"] = key
    elif key:
        headers["x-goog-api-key"] = key
    proxy = urllib.request.ProxyHandler({}) if local else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(proxy, _RejectRedirect())
    return opener, base, headers


def _check_active(deadline, cancelled):
    if cancelled is not None and cancelled():
        raise ProviderError("模型请求已取消")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("模型请求超时", code="PROVIDER_TIMEOUT")
    return remaining


def _json_dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _post(opener, url, headers, payload, deadline, cancelled, *, request_timeout=REQUEST_TIMEOUT):
    try:
        data = _json_dump(payload).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("模型请求数据格式无效") from None
    if len(data) > MAX_REQUEST_BYTES:
        raise ProviderError("模型请求超过大小限制")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return _request_json(opener, request, deadline, cancelled, request_timeout=request_timeout)


def _request_json(opener, request, deadline, cancelled=None, *, request_timeout=REQUEST_TIMEOUT):
    request_deadline = min(deadline, time.monotonic() + request_timeout)
    try:
        timeout = _check_active(request_deadline, cancelled)
        with opener.open(request, timeout=timeout) as response:
            raw = bytearray()
            while len(raw) <= MAX_REPLY_BYTES:
                _check_active(request_deadline, cancelled)
                part = response.read1(min(65536, MAX_REPLY_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise ProviderError("模型服务认证失败（HTTP 401）。请核对 API 密钥和服务商地址。", code=401) from None
        raise ProviderError("模型接口返回 HTTP {}".format(exc.code), code=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        raise ProviderError("模型接口连接中断或超时", code="PROVIDER_CONNECTION_ERROR",
                            detail=str(exc)) from None
    _check_active(request_deadline, cancelled)
    if len(raw) > MAX_REPLY_BYTES:
        raise ProviderError("模型响应超过读取范围")
    try:
        reply = json.loads(raw)
    except (ValueError, RecursionError):
        raise ProviderError("模型接口未返回有效 JSON") from None
    if not isinstance(reply, dict) or reply.get("error"):
        raise ProviderError("模型接口返回错误响应")
    return reply


def _declared_efforts(protocol, base_url, row):
    if _deepseek(protocol, base_url):
        effort = row.get("effort")
        if isinstance(effort, dict):
            levels = effort.get("supported_levels")
            if isinstance(levels, list):
                try:
                    # The directory lists thinking-on levels. Responses also accepts none.
                    return normalize_effort_levels(protocol, base_url, ["none", *levels])
                except ValueError:
                    return None
    if protocol == ANTHROPIC:
        capabilities = row.get("capabilities")
        effort = capabilities.get("effort") if isinstance(capabilities, dict) else None
        if isinstance(row.get("id"), str) and _model_family(row["id"]) in CLAUDE_THINKING_BUDGETS:
            return None
        if isinstance(effort, dict):
            if effort.get("supported") is False:
                return []
            statuses = {}
            for level in PROTOCOL_REASONING_EFFORTS[ANTHROPIC][1:]:
                detail = effort.get(level)
                if isinstance(detail, dict) and isinstance(detail.get("supported"), bool):
                    statuses[level] = detail["supported"]
            if statuses:
                return [level for level, supported in statuses.items() if supported]
    if protocol == GEMINI and row.get("thinking") is False:
        return []
    return None


def discover_models(profile):
    """List selectable model IDs without returning upstream bodies or credentials."""
    protocol = profile["protocol"]
    opener, base, headers = _transport(profile, protocol)
    deepseek_anthropic = protocol == ANTHROPIC and _deepseek(protocol, base)
    if deepseek_anthropic:
        url = "https://api.deepseek.com/models"
        headers = {"Authorization": "Bearer " + profile["api_key"]}
    else:
        url = endpoint(protocol, base, "/models")
    key = profile.get("api_key", "")
    deadline = time.monotonic() + REQUEST_TIMEOUT
    query = {"limit": 100} if protocol == ANTHROPIC and not deepseek_anthropic else {"pageSize": 100} if protocol == GEMINI else {}
    models = {}
    seen_cursors = set()
    for _ in range(MAX_DISCOVERY_PAGES):
        page_url = url + ("?" + urllib.parse.urlencode(query) if query else "")
        request = urllib.request.Request(page_url, headers=headers, method="GET")
        reply = _request_json(opener, request, deadline)
        rows = reply.get("models" if protocol == GEMINI else "data")
        if not isinstance(rows, list):
            raise ProviderError("模型接口未返回有效的模型列表")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if protocol == GEMINI:
                name = row.get("name")
                methods = row.get("supportedGenerationMethods")
                if (not isinstance(name, str) or not name.startswith("models/")
                        or not isinstance(methods, list) or "generateContent" not in methods):
                    continue
                label = row.get("displayName")
            else:
                name = row.get("id")
                label = row.get("display_name") if protocol == ANTHROPIC else None
            try:
                model = normalize_model(protocol, name)
            except ValueError:
                continue
            if model in models or (key and key in model):
                continue
            if len(models) == MAX_DISCOVERED_MODELS:
                return {"models": list(models.values()), "truncated": True}
            if (not isinstance(label, str) or not label.strip() or len(label) > 160
                    or any(unicodedata.category(char).startswith("C") for char in label)
                    or (key and key in label)):
                label = model
            entry = {"id": model, "name": label.strip()}
            declared = _declared_efforts(protocol, base, row)
            if declared is not None:
                entry["discovered_reasoning_levels"] = declared
            if (declared is not None or _known_efforts(protocol, model, base) is not None
                    or base == DEFAULT_BASE_URLS[protocol]):
                entry["reasoning_efforts"] = list(reasoning_efforts(protocol, model, base, discovered=declared))
            models[model] = entry
        if protocol == ANTHROPIC and reply.get("has_more") is True:
            cursor_field, cursor = "after_id", reply.get("last_id")
        elif protocol == GEMINI and reply.get("nextPageToken"):
            cursor_field, cursor = "pageToken", reply["nextPageToken"]
        else:
            return {"models": list(models.values()), "truncated": reply.get("has_more") is True}
        if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048
                or re.search(r"[^!-~]", cursor) or (key and key in cursor)
                or cursor in seen_cursors):
            break
        query[cursor_field] = cursor
        seen_cursors.add(cursor)
    return {"models": list(models.values()), "truncated": True}


def _request_payload(protocol, model, system, prompt, tools, max_tokens):
    declarations = [
        {"name": tool["name"], "description": tool["description"]} for tool in tools
    ]
    if protocol == OPENAI:
        history = [{"role": "user", "content": prompt}]
        payload = {
            "model": model, "input": history, "store": False,
            "max_output_tokens": max_tokens,
        }
        if system:
            payload["instructions"] = system
        if tools:
            payload["include"] = ["reasoning.encrypted_content"]
            payload["tools"] = [
                dict(declaration, type="function", parameters=tool["inputSchema"], strict=False)
                for declaration, tool in zip(declarations, tools)
            ]
    elif protocol == ANTHROPIC:
        history = [{"role": "user", "content": prompt}]
        payload = {"model": model, "messages": history, "max_tokens": max_tokens}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                dict(declaration, input_schema=tool["inputSchema"])
                for declaration, tool in zip(declarations, tools)
            ]
    elif protocol == CHAT:
        history = [{"role": "user", "content": prompt}]
        if system:
            history.insert(0, {"role": "system", "content": system})
        payload = {"model": model, "messages": history, "max_completion_tokens": max_tokens}
        if tools:
            payload["tools"] = [{"type": "function", "function": dict(declaration, parameters=tool["inputSchema"])}
                                for declaration, tool in zip(declarations, tools)]
    else:
        history = [{"role": "user", "parts": [{"text": prompt}]}]
        payload = {"contents": history, "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [{
                "functionDeclarations": [
                    dict(declaration, parametersJsonSchema=tool["inputSchema"])
                    for declaration, tool in zip(declarations, tools)
                ],
            }]
    return payload, history


def _blocks(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError
    return value


def _parse_turn(protocol, reply, *, multi_agent=False):
    if protocol == OPENAI:
        if reply.get("status", "completed") != "completed":
            detail = reply.get("incomplete_details")
            if isinstance(detail, dict) and detail.get("reason") == "max_output_tokens":
                raise ProviderError("模型达到输出 token 上限，未完成响应")
            raise ProviderError("模型接口未完成响应")
        assistant = _blocks(reply["output"])
        calls = [
            (item["name"], json.loads(item["arguments"]), item["call_id"])
            for item in assistant if item.get("type") == "function_call"
        ]
        messages = [item for item in assistant if item.get("type") == "message"]
        if multi_agent:
            messages = [
                item for item in messages
                if item.get("phase") == "final_answer" and isinstance(item.get("agent"), dict)
                and item["agent"].get("agent_name") == "/root"
            ]
        text = [
            block["text"] for item in messages
            for block in _blocks(item["content"]) if block.get("type") == "output_text"
        ]
    elif protocol == CHAT:
        choice = _blocks(reply["choices"])[0]
        if choice.get("finish_reason") not in (None, "stop", "tool_calls"):
            raise ProviderError("模型接口未完成响应")
        message = choice["message"]
        native_calls = _blocks(message.get("tool_calls") or [])
        calls = [(item["function"]["name"], json.loads(item["function"]["arguments"]), item["id"])
                 for item in native_calls if item.get("type") == "function"]
        text = [message["content"]] if isinstance(message.get("content"), str) else []
        assistant = {"role": "assistant", "content": message.get("content"),
                     **{key: message[key] for key in ("reasoning_content", "extra_content") if key in message}}
        if native_calls:
            assistant["tool_calls"] = native_calls
    elif protocol == ANTHROPIC:
        if reply.get("stop_reason") not in {None, "end_turn", "stop_sequence", "tool_use"}:
            raise ProviderError("模型接口未完成响应")
        blocks = _blocks(reply["content"])
        assistant = {"role": "assistant", "content": blocks}
        calls = [
            (block["name"], block["input"], block["id"])
            for block in blocks if block.get("type") == "tool_use"
        ]
        text = [block["text"] for block in blocks if block.get("type") == "text"]
    else:
        candidate = _blocks(reply["candidates"])[0]
        if candidate.get("finishReason") not in {None, "STOP"}:
            raise ProviderError("模型接口未完成响应")
        content = candidate["content"]
        parts = _blocks(content["parts"])
        assistant = {"role": "model", "parts": parts}
        calls = [
            (call["name"], call.get("args", {}), call.get("id"))
            for call in (part["functionCall"] for part in parts if "functionCall" in part)
        ]
        text = [part["text"] for part in parts if "text" in part and not part.get("thought")]
    return assistant, calls, "".join(text).strip()


def _tool_reply(protocol, call, result):
    name, _, call_id = call
    try:
        encoded = _json_dump(result)
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("模型工具结果无法转换为 JSON") from None
    if protocol == OPENAI:
        return {"type": "function_call_output", "call_id": call_id, "output": encoded}
    if protocol == CHAT:
        return {"role": "tool", "tool_call_id": call_id, "content": encoded}
    if protocol == ANTHROPIC:
        return {"type": "tool_result", "tool_use_id": call_id, "content": encoded}
    body = {"name": name, "response": result if isinstance(result, dict) else {"result": result}}
    if call_id is not None:
        body["id"] = call_id
    return {"functionResponse": body}


def _validate_calls(calls, protocol, names, *, guarded=False):
    if len(calls) > MAX_CALLS_PER_TURN:
        raise ProviderError("单轮模型工具调用次数超限")
    identifiers = set()
    for name, args, call_id in calls:
        if not isinstance(name, str) or (not guarded and name not in names) or not isinstance(args, dict):
            raise ProviderError("模型工具调用的名称或参数无效")
        if not (call_id is None and protocol == GEMINI):
            if not isinstance(call_id, str) or not call_id or call_id in identifiers:
                raise ProviderError("模型工具调用 ID 无效或重复")
            identifiers.add(call_id)


def generate(profile, system, prompt, *, tools=(), execute=None, max_turns=1,
             max_tokens=2048, cancelled=None, request_timeout=REQUEST_TIMEOUT, on_progress=None, on_unlisted_tool=None):
    """Return the final model text after at most max_turns provider requests."""
    protocol = profile.get("protocol")
    if not isinstance(protocol, str) or protocol not in DEFAULT_BASE_URLS:
        raise ProviderError("模型接口协议不受支持")
    if type(max_turns) is not int or not 1 <= max_turns <= MAX_TURNS:
        raise ProviderError("模型请求轮次超限")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32768:
        raise ProviderError("模型输出 token 数超限")
    tools = tuple(tools)
    try:
        model = normalize_model(protocol, profile.get("model"))
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    opener, base, headers = _transport(profile, protocol)
    try:
        multi_agent = normalize_multi_agent(profile.get("multi_agent"), protocol, model, base)
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    suffix = {OPENAI: "/responses", CHAT: "/chat/completions", ANTHROPIC: "/messages",
              GEMINI: "/models/" + model + ":generateContent"}[protocol]
    url = endpoint(protocol, base, suffix)
    payload, history = _request_payload(protocol, model, system, prompt, tools, max_tokens)
    if multi_agent:
        headers["OpenAI-Beta"] = "responses_multi_agent=v1"
        payload["multi_agent"] = multi_agent
    _apply_reasoning(payload, protocol, model, base, profile.get("reasoning_effort", ""),
                     override=profile.get("reasoning_level_override"),
                     discovered=profile.get("discovered_reasoning_levels"))
    names = {tool["name"] for tool in tools}
    deadline = time.monotonic() + GENERATION_TIMEOUT
    request_timeout = GENERATION_TIMEOUT if multi_agent else request_timeout
    for turn in range(max_turns):
        if on_progress:
            on_progress("waiting", "", {"turn": f"model-{turn + 1}"})
        reply = _post(opener, url, headers, payload, deadline, cancelled, request_timeout=request_timeout)
        try:
            assistant, calls, final_text = _parse_turn(protocol, reply, multi_agent=bool(multi_agent))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, RecursionError):
            raise ProviderError("模型接口响应格式无效") from None
        if on_progress and protocol == OPENAI:
            for index, item in enumerate(assistant):
                summary = item.get("summary")
                if item.get("type") == "reasoning" and isinstance(summary, list):
                    preview = "\n".join(part["text"] for part in summary if isinstance(part, dict)
                                        and part.get("type") == "summary_text" and isinstance(part.get("text"), str))
                    if preview.strip():
                        on_progress("summary", preview[:1600],
                                    {"turn": f"model-{turn + 1}", "item": f"summary-{index + 1}"})
        if not calls:
            if not final_text:
                raise ProviderError("模型接口未返回文本")
            return final_text
        if execute is None:
            raise ProviderError("模型工具调用无法继续")
        if turn + 1 == max_turns:
            raise ToolTurnLimit("本轮已达到模型请求上限")
        _validate_calls(calls, protocol, names, guarded=on_unlisted_tool is not None)
        callers = {}
        if multi_agent:
            callers = {
                item["call_id"]: item["caller"] for item in assistant
                if item.get("type") == "function_call" and item.get("caller") is not None
            }
        results = []
        for call in calls:
            _check_active(deadline, cancelled)
            name, args, call_id = call
            handler = execute if name in names else on_unlisted_tool
            result = _tool_reply(protocol, call, handler(name, args))
            if call_id in callers:
                result["caller"] = callers[call_id]
            results.append(result)
        if protocol == OPENAI:
            history.extend(assistant)
            history.extend(results)
        elif protocol == CHAT:
            history.extend([assistant, *results])
        elif protocol == ANTHROPIC:
            history.extend([assistant, {"role": "user", "content": results}])
        else:
            history.extend([assistant, {"role": "user", "parts": results}])
    raise ProviderError("模型请求轮次超限")

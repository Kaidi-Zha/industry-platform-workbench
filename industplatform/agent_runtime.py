"""Dispatch the same workbench tools through either execution mode."""

from . import codex_agent, codex_local, provider_api
from .settings import Settings


class CodexAppServerAdapter:
    def __init__(self, preferences, model=None):
        self.preferences = preferences
        self.model = model or {}

    def fetch_available_models(self, force_refresh: bool = False) -> codex_local.NativeProbe:
        return codex_local.DETECTOR.detect(self.preferences["codex_executable"], refresh=force_refresh)

    def execute(self, prompt, port, on_event, stop, **options):
        try:
            probe = self.fetch_available_models()
            model = codex_local.select_model(probe, self.model.get("id"), self.model.get("reasoning_effort"))
            if probe.mcp_servers is None:
                raise codex_local.LocalCodexError("无法确认本机 Codex 的 MCP 配置，请重新检测。")
            with codex_local.state_directory() as state:
                return codex_agent.execute(
                    prompt, port, on_event, stop, ephemeral=True,
                    subscription={"binary": probe.binary, "mcp_servers": probe.mcp_servers,
                                  "project_mcp_transports": probe.project_mcp_transports,
                                  "model": model, "state": state}, **options,
                )
        except codex_local.LocalCodexError as exc:
            raise codex_agent.AgentError(str(exc)) from None
        except OSError:
            raise codex_agent.AgentError("无法初始化仓库内的订阅运行目录") from None


class AgentRuntimeRouter:
    def __init__(self, mode=None, *, preferences=None, model=None):
        self.mode = mode
        self.subscription = CodexAppServerAdapter(preferences or {}, model) if mode == codex_local.SUBSCRIPTION else None

    @classmethod
    def for_run(cls, store, run):
        mode = run.get("execution_mode")
        return cls(mode, preferences=Settings(store).get() if mode == codex_local.SUBSCRIPTION else None,
                   model=run.get("subscription_model"))

    def execute(self, prompt, port, on_event, stop, *, profile=None, **options):
        if self.subscription:
            return self.subscription.execute(prompt, port, on_event, stop, **options)
        if self.mode == codex_local.API_GATEWAY:
            return self.api(prompt, stop, profile, on_event=on_event, **options)
        # Turns created before execution modes retain their original runtime.
        if profile is not None:
            options["profile"] = profile
        return codex_agent.execute(prompt, port, on_event, stop, **options)

    @staticmethod
    def api(prompt, stop, profile, *, guidance="", tools=None, max_turns=None, on_event=None, scoped=False,
            guard_check=None, **_):
        if not profile:
            raise codex_agent.AgentError("本次运行的 API 模型连接已失效，请重新选择模型")
        schemas, callback = tools or ((), None)
        key = profile.get("api_key") or ""

        def redact(text):
            return text.replace(key, "[已隐藏凭证]") if len(key) >= 8 else text

        def redact_value(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, list):
                return [redact_value(item) for item in value]
            if isinstance(value, dict):
                return {redact(name): redact_value(item) for name, item in value.items()}
            return value

        def invoke(name, arguments):
            if callback is None:
                raise provider_api.ProviderError("当前阶段没有可调用的工具")
            if guard_check:
                guard_check()
            value = callback(name, arguments)
            if guard_check:
                guard_check()
            return redact_value(value)

        def observe(kind, preview, location=None):
            if kind == "summary":
                on_event("harness.item", {"kind": "reasoning", "label": "推理摘要",
                                          **(location or {}),
                                          "preview": codex_agent._redact(redact(preview))}, None)
            else:
                on_event("harness.progress", {"kind": kind, **(location or {})}, None)

        try:
            answer = provider_api.generate(
                profile, redact(guidance), redact(prompt), tools=schemas, execute=invoke if callback else None,
                max_turns=max_turns or 1, max_tokens=8192, cancelled=stop.is_set if stop is not None else None,
                on_progress=observe if on_event and scoped else None,
                **({"on_unlisted_tool": invoke} if guard_check else {}),
            )
        except provider_api.ToolTurnLimit as exc:
            raise codex_agent.TurnBudgetExhausted(str(exc), code="PROVIDER_TOOL_TURN_LIMIT") from None
        except provider_api.ProviderError as exc:
            error = codex_agent.AgentError(codex_agent.diagnostic_text(exc, redact), code=exc.code or "PROVIDER_ERROR",
                                           detail=codex_agent.diagnostic_text(exc.detail, redact))
            if type(exc.code) is int:
                error.diagnostic["http_status"] = exc.code
            raise error from None
        return codex_agent._redact(redact(answer))

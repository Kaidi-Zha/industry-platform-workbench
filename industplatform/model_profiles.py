"""User-supplied model connections and their public metadata."""

import json
import threading
import time
import uuid
from contextlib import closing, contextmanager
from urllib.parse import urlsplit

from .credentials import CredentialError, local_vault
from .evaluation import TIERS
from .provider_api import (
    DEFAULT_BASE_URLS, DEFAULT_MULTI_AGENT_CONCURRENCY, KEY, LOOPBACK_HOSTS, PROTOCOL_REASONING_EFFORTS,
    UNSUPPORTED_EFFORT, provider_family, supports_vision,
    canonical_reasoning_effort, discover_models, generate, normalize_base_url, normalize_effort_levels,
    normalize_model, normalize_multi_agent, reasoning_efforts, supports_multi_agent,
)
from .storage import AGENT_ROUTES

PROTOCOL_LABELS = {
    "openai-responses": "OpenAI Responses",
    "openai-chat-completions": "OpenAI Chat Completions",
    "anthropic-messages": "Anthropic Messages",
    "gemini-generate-content": "Gemini generateContent",
}
DEEPSEEK_TIERS = {"deepseek-flash": "general", "deepseek-v4-pro": "frontier"}
PRESETS = (
    {"id": "openai", "name": "OpenAI", "label": "OpenAI", "group": "国际前沿", "protocol": "openai-responses",
     "base_url": "https://api.openai.com/v1", "models": ("gpt-6.2-sol", "gpt-6-luna")},
    {"id": "anthropic", "name": "Anthropic", "label": "Anthropic", "group": "国际前沿",
     "protocol": "anthropic-messages", "base_url": "https://api.anthropic.com",
     "models": ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5")},
    {"id": "google", "name": "Google", "label": "Google Gemini", "group": "国际前沿",
     "protocol": "openai-chat-completions", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
     "models": ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"),
     "alternate": {"protocol": "gemini-generate-content", "base_url": DEFAULT_BASE_URLS["gemini-generate-content"]}},
    {"id": "xai", "name": "xAI", "label": "xAI (Grok)", "group": "国际前沿", "protocol": "openai-responses",
     "base_url": "https://api.x.ai/v1", "models": ("grok-4.7",),
     "alternate": {"protocol": "openai-chat-completions", "base_url": "https://api.x.ai/v1"}},
    {"id": "openrouter", "name": "OpenRouter", "label": "OpenRouter", "group": "聚合网关",
     "protocol": "openai-chat-completions", "base_url": "https://openrouter.ai/api/v1", "models": (),
     "alternate": {"protocol": "anthropic-messages", "base_url": "https://openrouter.ai/api"}},
    {"id": "deepseek", "name": "DeepSeek", "label": "DeepSeek", "group": "国内主力",
     "protocol": "openai-responses", "base_url": "https://api.deepseek.com",
     "models": tuple(DEEPSEEK_TIERS), "tiers": DEEPSEEK_TIERS,
     "alternate": {"protocol": "anthropic-messages", "base_url": "https://api.deepseek.com/anthropic"}},
    {"id": "dashscope", "name": "阿里云百炼", "label": "阿里云百炼", "group": "国内主力",
     "protocol": "openai-chat-completions", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ("qwen-plus",)},
    {"id": "glm", "name": "智谱", "label": "智谱 (GLM)", "group": "国内主力", "protocol": "openai-chat-completions",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ("glm-5",),
     "alternate": {"protocol": "anthropic-messages", "base_url": "https://open.bigmodel.cn/api/anthropic"}},
    {"id": "siliconflow", "name": "硅基流动", "label": "硅基流动", "group": "国内主力",
     "protocol": "openai-chat-completions", "base_url": "https://api.siliconflow.cn/v1", "models": ()},
    {"id": "doubao", "name": "火山方舟", "label": "火山方舟 (豆包)", "group": "国内主力",
     "protocol": "openai-chat-completions", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "models": ()},
    {"id": "minimax", "name": "MiniMax", "label": "MiniMax", "group": "国内主力", "protocol": "openai-chat-completions",
     "base_url": "https://api.minimax.chat/v1", "models": ()},
    {"id": "ollama", "name": "Ollama", "label": "Ollama", "group": "本地私有", "protocol": "openai-chat-completions",
     "base_url": "http://localhost:11434/v1", "models": ()},
    {"id": "vllm", "name": "vLLM", "label": "vLLM", "group": "本地私有", "protocol": "openai-chat-completions",
     "base_url": "http://localhost:8000/v1", "models": ()},
    {"id": "custom", "name": "", "label": "自定义兼容接口", "group": "本地私有", "protocol": "openai-chat-completions",
     "base_url": "", "models": (),
     "alternate": {"protocol": "anthropic-messages", "base_url": ""}},
)
PRESET_IDS = {preset["id"] for preset in PRESETS}


def _preset_models(preset):
    address = {key: preset[key] for key in ("protocol", "base_url")}
    if preset.get("alternate"):
        addresses = [address, preset["alternate"]]
    else:
        addresses = [address]
    return {**preset, "models": list(preset["models"]), "vision": {
        item["protocol"]: [model for model in preset["models"]
                           if supports_vision(item["protocol"], model, item["base_url"])]
        for item in addresses
    }, "mappings": {
        item["protocol"]: {model: list(reasoning_efforts(item["protocol"], model, item["base_url"]))
                           for model in preset["models"]}
        for item in addresses
    }}

MAX_PROFILES = 32
REASONING_LEVEL_FIELDS = ("reasoning_level_override", "discovered_reasoning_levels")


def _correct_deepseek_tiers(db):
    migration = "model_profiles/deepseek_tier_defaults_v1"
    if not db.execute("INSERT OR IGNORE INTO global_settings VALUES (?, 'true')", (migration,)).rowcount:
        return
    # An empty tier is an explicit opt-out. Leave it and historical evaluations intact.
    for profile_id, protocol, base, preset, model, tier in db.execute("""SELECT id,protocol,base_url,preset,lower(model),tier
            FROM model_profiles WHERE tier<>'' AND lower(model) IN (?, ?)""", tuple(DEEPSEEK_TIERS)).fetchall():
        expected = DEEPSEEK_TIERS[model]
        if expected != tier and (preset == "deepseek" or provider_family(protocol, base) == "deepseek"):
            db.execute("UPDATE model_profiles SET tier=? WHERE id=?", (expected, profile_id))


def _field(payload, name, limit, default=""):
    value = payload.get(name, default)
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError("模型连接字段无效：" + name)
    return value.strip()


def _address(payload):
    protocol = _field(payload, "protocol", 40)
    if protocol not in PROTOCOL_LABELS:
        raise ValueError("请选择有效的 API 协议")
    return protocol, normalize_base_url(protocol, _field(payload, "base_url", 512))


def _check_key(key, base):
    local = urlsplit(base).hostname in LOOPBACK_HOSTS
    if (key and not KEY.fullmatch(key)) or (not key and not local):
        raise ValueError("请填写 API 密钥。更改协议或地址时需要重新填写密钥。")


def _available_efforts(protocol, model, base, levels):
    return reasoning_efforts(protocol, model, base, override=levels.get("reasoning_level_override"),
                             discovered=levels.get("discovered_reasoning_levels"))


def _validate_reasoning_effort(effort, available):
    if effort not in available:
        raise ValueError(UNSUPPORTED_EFFORT)
    return effort


def _reasoning_settings(payload, protocol, model, base, old=None, *, preserve_effort=False):
    levels = {}
    for field in REASONING_LEVEL_FIELDS:
        value = payload.get(field, old.get(field) if old else None)
        levels[field] = None if value is None else normalize_effort_levels(protocol, base, value)
    available = _available_efforts(protocol, model, base, levels)
    default = canonical_reasoning_effort(protocol, base, old["reasoning_effort"]) if preserve_effort and old else ""
    if default not in available:
        default = ""
    effort = _field(payload, "reasoning_effort", 20, default)
    effort = canonical_reasoning_effort(protocol, base, effort)
    return {**levels, "reasoning_effort": _validate_reasoning_effort(effort, available)}


def _reasoning_level_fields(profile):
    return {field: None if profile.get(field) is None else list(profile[field]) for field in REASONING_LEVEL_FIELDS}


def public_profile(profile):
    fields = ("id", "name", "protocol", "base_url", "model", "tier")
    effort = canonical_reasoning_effort(profile["protocol"], profile["base_url"], profile.get("reasoning_effort", ""))
    public = {**{key: profile[key] for key in fields}, "reasoning_effort": effort}
    if "multi_agent" in profile:
        public["multi_agent"] = dict(profile["multi_agent"])
    return public


class ModelProfiles:
    def __init__(self, store=None, *, vault=None):
        self.lock = threading.RLock()
        self.store = store
        self.vault = vault
        if store is not None and vault is None:
            self.vault = local_vault(store.path)
        self.profiles = {}
        self.providers = {}
        self.queued = {}
        if store is not None:
            with self._database(write=True) as connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS model_profiles (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL,
                        base_url TEXT NOT NULL, model TEXT NOT NULL, tier TEXT NOT NULL,
                        credential_ref TEXT, reasoning_effort TEXT NOT NULL DEFAULT '',
                        provider_id TEXT NOT NULL DEFAULT '', reasoning_level_override TEXT,
                        discovered_reasoning_levels TEXT, preset TEXT NOT NULL DEFAULT '',
                        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), capabilities TEXT NOT NULL DEFAULT '{}'
                    )
                """)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(model_profiles)")}
                if "reasoning_effort" not in columns:
                    connection.execute("ALTER TABLE model_profiles ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT ''")
                if "provider_id" not in columns:
                    connection.execute("ALTER TABLE model_profiles ADD COLUMN provider_id TEXT NOT NULL DEFAULT ''")
                if "preset" not in columns:
                    connection.execute("ALTER TABLE model_profiles ADD COLUMN preset TEXT NOT NULL DEFAULT ''")
                for field in REASONING_LEVEL_FIELDS:
                    if field not in columns:
                        connection.execute(f"ALTER TABLE model_profiles ADD COLUMN {field} TEXT")
                if "enabled" not in columns:
                    connection.execute("ALTER TABLE model_profiles ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))")
                if "capabilities" not in columns:
                    connection.execute("ALTER TABLE model_profiles ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'")
                connection.execute("UPDATE model_profiles SET provider_id = id WHERE provider_id = ''")
                connection.execute("""CREATE TABLE IF NOT EXISTS model_providers (
                    provider_id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL,
                    base_url TEXT NOT NULL, preset TEXT NOT NULL, credential_ref TEXT)""")
                connection.execute("""INSERT OR IGNORE INTO model_providers
                    SELECT provider_id,name,protocol,base_url,preset,credential_ref FROM model_profiles ORDER BY rowid""")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS retired_model_credentials (reference TEXT PRIMARY KEY)
                """)
                _correct_deepseek_tiers(connection)
            self._clear_retired_credentials()

    @contextmanager
    def _database(self, *, write=False):
        if self.store is None:
            yield None
            return
        with closing(self.store.connect()) as connection, connection:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _records(self, connection):
        if connection is None:
            return self.profiles
        fields = ("id", "name", "protocol", "base_url", "model", "tier", "credential_ref", "reasoning_effort",
                  "provider_id", *REASONING_LEVEL_FIELDS, "preset", "enabled", "capabilities")
        records = {}
        for row in connection.execute("""
            SELECT id, name, protocol, base_url, model, tier, credential_ref, reasoning_effort, provider_id,
                reasoning_level_override, discovered_reasoning_levels, preset, enabled, capabilities
            FROM model_profiles ORDER BY rowid
        """):
            record = dict(zip(fields, row))
            for field in REASONING_LEVEL_FIELDS:
                if record[field] is not None:
                    record[field] = json.loads(record[field])
            record["enabled"] = bool(record["enabled"])
            record["capabilities"] = json.loads(record["capabilities"])
            records[record["id"]] = record
        return records

    def _providers(self, connection):
        if connection is None:
            return self.providers
        fields = ("provider_id", "name", "protocol", "base_url", "preset", "credential_ref")
        return {row[0]: dict(zip(fields, row)) for row in connection.execute("""
            SELECT provider_id,name,protocol,base_url,preset,credential_ref FROM model_providers ORDER BY rowid
        """)}

    def _save_provider_record(self, connection, provider):
        if connection is None:
            self.providers[provider["provider_id"]] = provider
        else:
            connection.execute("""INSERT INTO model_providers VALUES
                (:provider_id,:name,:protocol,:base_url,:preset,:credential_ref)
                ON CONFLICT(provider_id) DO UPDATE SET name=excluded.name,protocol=excluded.protocol,
                base_url=excluded.base_url,preset=excluded.preset,credential_ref=excluded.credential_ref""", provider)

    def _clear_retired_credentials(self):
        with self._database(write=True) as connection:
            for (reference,) in connection.execute("SELECT reference FROM retired_model_credentials").fetchall():
                if connection.execute("""SELECT 1 FROM model_providers WHERE credential_ref=?
                        UNION ALL SELECT 1 FROM model_profiles WHERE credential_ref=? LIMIT 1""", (reference, reference)).fetchone():
                    connection.execute("DELETE FROM retired_model_credentials WHERE reference = ?", (reference,))
                    continue
                try:
                    self.vault.delete(reference)
                except CredentialError:
                    continue
                connection.execute("DELETE FROM retired_model_credentials WHERE reference = ?", (reference,))

    @staticmethod
    def _public_status(profile):
        levels = _reasoning_level_fields(profile)
        protocol, model, base = profile["protocol"], profile["model"], profile["base_url"]
        source = "preset"
        if levels["reasoning_level_override"] is not None:
            source = "manual"
        elif levels["discovered_reasoning_levels"] is not None:
            source = "api"
        return {
            **public_profile(profile),
            **levels,
            "has_key": bool(profile.get("credential_ref") or profile.get("api_key")),
            "reasoning_efforts": list(_available_efforts(protocol, model, base, levels)),
            "automatic_reasoning_efforts": list(reasoning_efforts(
                protocol, model, base, discovered=levels["discovered_reasoning_levels"],
            )),
            "reasoning_level_source": source,
            "multi_agent_supported": supports_multi_agent(protocol, model, base),
            "enabled": profile.get("enabled", True),
            "capabilities": dict(profile.get("capabilities", {})),
            "vision_supported": profile.get("capabilities", {}).get("vision", supports_vision(protocol, model, base)),
            "parallel_tools_supported": profile.get("capabilities", {}).get("parallel_tool_calls", False),
        }

    @staticmethod
    def _groups(profiles):
        groups = {}
        for profile in profiles.values():
            groups.setdefault(profile["provider_id"], []).append(profile)
        return groups

    def _public_provider(self, provider, profiles):
        fields = ("name", "protocol", "base_url")
        preset = provider.get("preset") or provider_family(provider["protocol"], provider["base_url"])
        return {"id": provider["provider_id"], "preset": preset, **{field: provider[field] for field in fields},
                "has_key": bool(provider.get("credential_ref") or provider.get("api_key")),
                "models": [self._public_status(profile) for profile in profiles]}

    def catalog(self):
        with self.lock, self._database() as connection:
            records = self._records(connection)
            profiles = [self._public_status(profile) for profile in records.values()]
            groups = self._groups(records)
            providers = [self._public_provider(provider, groups.get(identity, []))
                         for identity, provider in self._providers(connection).items()]
        protocols = [{"id": key, "label": label, "base_url": DEFAULT_BASE_URLS[key],
                      "reasoning_efforts": list(PROTOCOL_REASONING_EFFORTS[key])}
                     for key, label in PROTOCOL_LABELS.items()]
        return {"profiles": profiles, "providers": providers, "protocols": protocols,
                "presets": [_preset_models(preset) for preset in PRESETS],
                "multi_agent_default_concurrency": DEFAULT_MULTI_AGENT_CONCURRENCY,
                "credential_store": self.vault.identifier if self.vault is not None else "session"}

    @staticmethod
    def _record(profiles, profile_id):
        profile = profiles.get(profile_id) if isinstance(profile_id, str) else None
        if profile is None:
            raise ValueError("请选择已保存的模型连接")
        return profile

    def _api_key(self, record):
        if self.store is None:
            key = record["api_key"]
        else:
            reference = record["credential_ref"]
            key = self.vault.get(reference) if reference else ""
            if reference and (not key or not KEY.fullmatch(key)):
                raise CredentialError("本地保存的 API 密钥不可用，请重新填写并保存。")
        return key

    def _with_credential(self, profile):
        return {**public_profile(profile), **_reasoning_level_fields(profile),
                "capabilities": dict(profile.get("capabilities", {})), "api_key": self._api_key(profile)}

    def get(self, profile_id, *, run_options=None, allow_disabled=False):
        run_options = run_options or {}
        with self.lock, self._database() as connection:
            profile = self._record(self._records(connection), profile_id)
            if not allow_disabled and not profile["enabled"]:
                raise ValueError("该模型已停用，请启用或选择其他连接")
            if "reasoning_effort" in run_options:
                available = _available_efforts(profile["protocol"], profile["model"], profile["base_url"], profile)
                effort = _validate_reasoning_effort(run_options["reasoning_effort"], available)
                profile = {**profile, "reasoning_effort": effort}
            if "multi_agent" in run_options:
                value = run_options["multi_agent"]
                if value is None:
                    raise ValueError("Multi-agent 配置无效")
                multi_agent = normalize_multi_agent(
                    value, profile["protocol"], profile["model"], profile["base_url"],
                )
                if multi_agent is not None:
                    profile = {**profile, "multi_agent": multi_agent}
            return self._with_credential(profile)

    @staticmethod
    def _same_address(profile, protocol, base):
        return profile["protocol"] == protocol and normalize_base_url(protocol, profile["base_url"]) == base

    def _draft(self, payload, *, requires_model):
        protocol, base = _address(payload)
        key = _field(payload, "api_key", 2048)
        profile_id = _field(payload, "id", 64)
        provider_id = _field(payload, "provider_id", 64)
        draft = {"protocol": protocol, "base_url": base}
        if requires_model:
            draft["model"] = normalize_model(protocol, _field(payload, "model", 128))
        old_model = None
        if provider_id or profile_id:
            with self.lock, self._database() as connection:
                profiles = self._records(connection)
                if provider_id:
                    provider = self._provider(connection, provider_id)
                    group = self._groups(profiles).get(provider_id, [])
                else:
                    provider = self._record(profiles, profile_id)
                    group = [provider]
                if self._same_address(provider, protocol, base):
                    if not key:
                        key = self._api_key(provider)
                    if requires_model:
                        old_model = next((record for record in group if record["model"] == draft["model"]
                                          and (not profile_id or record["id"] == profile_id)), None)
        if requires_model:
            draft.update(_reasoning_settings(payload, protocol, draft["model"], base, old_model))
        _check_key(key, base)
        if (len(key) >= 8 and key in base) or (key and key in draft.get("model", "")):
            raise ValueError("请仅在密钥字段填写 API 密钥")
        draft["api_key"] = key
        return draft

    def _provider(self, connection, provider_id):
        provider = self._providers(connection).get(provider_id) if isinstance(provider_id, str) else None
        if provider is None:
            raise ValueError("请选择已保存的服务商")
        return provider

    @staticmethod
    def _model_record(fields, protocol, base, old, *, same_address):
        model = normalize_model(protocol, _field(fields, "model", 128))
        previous = old if same_address and old and old["model"] == model else None
        default_tier = DEEPSEEK_TIERS.get(model.lower(), "") if provider_family(protocol, base) == "deepseek" else ""
        tier = _field(fields, "tier", 20, previous["tier"] if previous else default_tier)
        if tier and tier not in TIERS:
            raise ValueError("请选择有效的评测梯度")
        enabled = fields.get("enabled", old["enabled"] if old else True)
        if type(enabled) is not bool:
            raise ValueError("模型启用状态无效")
        capabilities = fields.get("capabilities", previous["capabilities"] if previous else {})
        if (not isinstance(capabilities, dict) or set(capabilities) - {"vision", "parallel_tool_calls", "context_window"}
                or any(type(value) is not bool for key, value in capabilities.items() if key != "context_window")
                or "context_window" in capabilities and (type(capabilities["context_window"]) is not int
                    or not 1 <= capabilities["context_window"] <= 100_000_000)):
            raise ValueError("模型能力配置无效")
        return {"id": old["id"] if old else uuid.uuid4().hex, "model": model, "tier": tier,
                "enabled": enabled, "capabilities": dict(capabilities),
                **_reasoning_settings(fields, protocol, model, base, previous, preserve_effort=True)}

    @staticmethod
    def _retire(connection, references):
        connection.executemany("INSERT OR IGNORE INTO retired_model_credentials VALUES (?)",
                               ((reference,) for reference in references if reference))

    def _write_group(self, connection, old_group, group):
        kept_ids = {record["id"] for record in group}
        removed_ids = {record["id"] for record in old_group if record["id"] not in kept_ids}
        if connection is None:
            for record in group:
                self.profiles[record["id"]] = record
            for profile_id in removed_ids:
                del self.profiles[profile_id]
            return removed_ids

        rows = [{**record, "capabilities": json.dumps(record["capabilities"]), **{field: json.dumps(record[field]) for field in REASONING_LEVEL_FIELDS
                              if record[field] is not None}} for record in group]
        connection.executemany("""
            INSERT INTO model_profiles (
                id, name, protocol, base_url, model, tier, credential_ref, reasoning_effort, provider_id,
                reasoning_level_override, discovered_reasoning_levels, preset, enabled, capabilities
            )
            VALUES (:id, :name, :protocol, :base_url, :model, :tier, :credential_ref, :reasoning_effort, :provider_id,
                :reasoning_level_override, :discovered_reasoning_levels, :preset, :enabled, :capabilities)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, protocol=excluded.protocol,
                base_url=excluded.base_url, model=excluded.model, tier=excluded.tier,
                credential_ref=excluded.credential_ref, reasoning_effort=excluded.reasoning_effort,
                reasoning_level_override=excluded.reasoning_level_override,
                discovered_reasoning_levels=excluded.discovered_reasoning_levels, preset=excluded.preset,
                enabled=excluded.enabled, capabilities=excluded.capabilities
        """, rows)
        connection.executemany("DELETE FROM model_profiles WHERE id = ?", ((profile_id,) for profile_id in removed_ids))
        return removed_ids

    def save(self, payload):
        _, profiles, selected_id, _ = self._save_group(payload, legacy=True)
        return self._public_status(next(profile for profile in profiles if profile["id"] == selected_id))

    def save_provider(self, payload):
        provider, profiles, _, removed = self._save_group(payload, legacy=False)
        return self._public_provider(provider, profiles), removed

    def _save_group(self, payload, *, legacy):
        name = _field(payload, "name", 80)
        protocol, base = _address(payload)
        key = _field(payload, "api_key", 2048)
        if not name:
            raise ValueError("请填写连接名称")
        with self.lock:
            new_reference = None
            try:
                with self._database(write=True) as connection:
                    profiles = self._records(connection)
                    old = self._record(profiles, payload["id"]) if legacy and "id" in payload else None
                    provider_id = "" if legacy else _field(payload, "provider_id", 64)
                    if old:
                        provider_id = old["provider_id"]
                    prior = self._provider(connection, provider_id) if provider_id else None
                    old_group = self._groups(profiles).get(provider_id, [])
                    provider_id = provider_id or uuid.uuid4().hex
                    same_address = bool(prior) and self._same_address(prior, protocol, base)
                    old_models = {record["id"]: record for record in old_group}
                    selected_id = None
                    if legacy:
                        selected = self._model_record(payload, protocol, base, old, same_address=same_address)
                        selected_id = selected["id"]
                        models = [
                            selected if record["id"] == selected_id else self._model_record(
                                {"model": record["model"], "tier": record["tier"]}, protocol, base, record,
                                same_address=same_address,
                            )
                            for record in old_group
                        ] if old else [selected]
                    else:
                        submitted = payload.get("models")
                        if not isinstance(submitted, list) or len(submitted) > MAX_PROFILES:
                            raise ValueError("模型列表最多包含 {} 款模型".format(MAX_PROFILES))
                        models = []
                        for fields in submitted:
                            if not isinstance(fields, dict):
                                raise ValueError("模型配置必须为 JSON 对象")
                            model_id = _field(fields, "id", 64)
                            previous = self._record(old_models, model_id) if model_id else None
                            models.append(self._model_record(fields, protocol, base, previous, same_address=same_address))
                    if len({model["id"] for model in models}) != len(models):
                        raise ValueError("同一个已保存模型不能重复提交")
                    if len({model["model"] for model in models}) != len(models):
                        raise ValueError("同一服务商下的模型 ID 不能重复")
                    if len(profiles) - len(old_group) + len(models) > MAX_PROFILES:
                        raise ValueError("最多保存 {} 个模型连接".format(MAX_PROFILES))
                    reuse_key = not key and same_address
                    if reuse_key:
                        key = self._api_key(prior)
                    _check_key(key, base)
                    if len(key) >= 8 and any(key in value for value in (name, base, *(model["model"] for model in models))):
                        raise ValueError("请仅在密钥字段填写 API 密钥")
                    preset = _field(payload, "preset", 20, prior.get("preset", "") if prior else "")
                    if preset and preset not in PRESET_IDS:
                        raise ValueError("请选择有效的厂商模板")
                    provider = {"provider_id": provider_id, "name": name, "protocol": protocol, "base_url": base,
                                "preset": preset}
                    if connection is None:
                        provider["api_key"] = key
                    else:
                        old_reference = prior["credential_ref"] if prior else None
                        reference = old_reference if reuse_key else None
                        if key and not reference:
                            reference = uuid.uuid4().hex
                            self.vault.put(reference, key)
                            new_reference = reference
                        provider["credential_ref"] = reference
                        if old_reference != reference:
                            self._retire(connection, (old_reference,))
                    group = [{**model, **provider} for model in models]
                    self._save_provider_record(connection, provider)
                    removed_ids = self._write_group(connection, old_group, group)
            except Exception:
                if new_reference:
                    try:
                        self.vault.delete(new_reference)
                    except CredentialError:
                        with self._database(write=True) as connection:
                            self._retire(connection, (new_reference,))
                raise
            if self.store is not None:
                self._clear_retired_credentials()
            return provider, group, selected_id, removed_ids

    def remove(self, profile_id=None, *, provider_id=None, keep_provider=False):
        with self.lock:
            with self._database(write=True) as connection:
                profiles = self._records(connection)
                groups = self._groups(profiles)
                if provider_id is not None:
                    provider = self._provider(connection, provider_id)
                    group = groups.get(provider_id, [])
                    delete_provider = True
                else:
                    model = self._record(profiles, profile_id)
                    provider_id = model["provider_id"]
                    provider = self._provider(connection, provider_id)
                    group = [model]
                    delete_provider = not keep_provider and len(groups[provider_id]) == 1
                if delete_provider:
                    if connection is None:
                        del self.providers[provider_id]
                    else:
                        connection.execute("DELETE FROM model_providers WHERE provider_id=?", (provider_id,))
                        self._retire(connection, (provider["credential_ref"],))
                removed_ids = {profile["id"] for profile in group}
                if connection is None:
                    for model_id in removed_ids:
                        del self.profiles[model_id]
                    return removed_ids
                connection.executemany("DELETE FROM model_profiles WHERE id = ?",
                                       ((model_id,) for model_id in removed_ids))
            self._clear_retired_credentials()
            return removed_ids

    def set_enabled(self, profile_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("模型启用状态无效")
        with self.lock, self._database(write=True) as connection:
            row = self._record(self._records(connection), profile_id)
            if connection is not None:
                connection.execute("UPDATE model_profiles SET enabled=? WHERE id=?", (enabled, profile_id))
            row["enabled"] = enabled
            return self._public_status(row)

    def test_provider(self, provider_id):
        with self.lock, self._database() as connection:
            provider = self._provider(connection, provider_id)
            draft = {**provider, "api_key": self._api_key(provider)}
        start = time.monotonic()
        discover_models(draft)
        return {"ok": True, "latency_ms": round((time.monotonic() - start) * 1000)}

    def test(self, profile_id):
        return self._probe(self.get(profile_id, allow_disabled=True))

    def discover(self, fields):
        if fields.keys() == {"id"}:
            return discover_models(self.get(fields["id"], allow_disabled=True))
        return discover_models(self._draft(fields, requires_model=False))

    def probe(self, fields):
        if fields.keys() == {"id"}:
            return self.test(fields["id"])
        return self._probe(self._draft(fields, requires_model=True))

    @staticmethod
    def _probe(profile):
        key = profile.get("api_key", "")
        if key and key in profile["model"]:
            raise ValueError("请仅在密钥字段填写 API 密钥")
        start = time.monotonic()
        generate(profile, "This is a connectivity test.", "Reply only with OK.", max_tokens=4096)
        return {"ok": True, "latency_ms": round((time.monotonic() - start) * 1000), "model": profile["model"]}

    def _panel(self, connection):
        profiles = self._records(connection).values()
        panel = {tier: [profile for profile in profiles if profile["tier"] == tier and profile["enabled"]][:2]
                 for tier in TIERS}
        return panel if all(panel.values()) else None

    def panel_ready(self):
        with self.lock, self._database() as connection:
            panel = self._panel(connection)
            return panel is not None and (
                self.vault is None or self.vault.identifier != "unavailable"
                or not any(profile["credential_ref"] for profiles in panel.values() for profile in profiles)
            )

    def panel(self):
        with self.lock, self._database() as connection:
            panel = self._panel(connection)
            if panel is not None:
                return {tier: [self._with_credential(profile) for profile in profiles]
                        for tier, profiles in panel.items()}
        return None

    def enqueue(self, store, arguments, profile=None, panel=None, conversation_id=None, *, agent_options=None,
                plan=None, parent_run_id=None, public_research=None, benchmark=None, auto_benchmark=None,
                plan_version=None, plan_reason="", delegation=None, fork_native_thread=None, frontier_target=None,
                attachments=None, graph_mode=False, resolve_domain=False, domain_hint="", scope_request=None,
                execution_mode=None, subscription_model=None):
        # The worker waits for this lock after claiming a run. Only public metadata enters SQLite.
        if profile:
            history = store.plan_history(conversation_id) if plan is None and conversation_id else []
            effective_plan = history[-1]["plan"] if history else plan
            keys = (profile["api_key"], (profile.get("approval_profile") or {}).get("api_key", ""),
                    (profile.get("worker_profile") or {}).get("api_key", ""))
            if any(len(key) >= 8 and any(key in text for text in (arguments[2], *(effective_plan or {}).values()))
                   for key in keys):
                raise ValueError("任务正文或计划包含本次连接的密钥，请移除后提交")
        options = {"agent_options": agent_options} if agent_options is not None and arguments[0] in AGENT_ROUTES else {}
        reviewer = (profile or {}).get("approval_profile")
        worker = (profile or {}).get("worker_profile")
        with self.lock:
            run = store.create(*arguments, model_profile=public_profile(profile) if profile else None,
                               configured_panel=panel is not None, conversation_id=conversation_id,
                               plan=plan, parent_run_id=parent_run_id, public_research=public_research,
                               benchmark=benchmark, auto_benchmark=auto_benchmark, plan_version=plan_version,
                               plan_reason=plan_reason, delegation=delegation, fork_native_thread=fork_native_thread,
                               frontier_target=frontier_target,
                               attachments=attachments,
                               graph_mode=graph_mode, resolve_domain=resolve_domain, domain_hint=domain_hint, scope_request=scope_request,
                               execution_mode=execution_mode, subscription_model=subscription_model,
                               approval_profile=public_profile(reviewer) if reviewer else None,
                               worker_profile=public_profile(worker) if worker else None, **options)
            if profile is not None or panel is not None:
                self.queued[run["id"]] = profile if profile is not None else panel
        return run

    def take(self, run_id):
        with self.lock:
            return self.queued.pop(run_id, None)

    def restore(self, run):
        """Reopen the selected connection using its saved identity and run options."""
        def reopen(snapshot):
            options = {key: snapshot[key] for key in ("reasoning_effort", "multi_agent") if key in snapshot}
            profile = self.get(snapshot["id"], run_options=options, allow_disabled=True)
            if any(profile.get(key) != snapshot.get(key) for key in ("protocol", "base_url", "model")):
                raise ValueError("原模型连接的协议、地址或模型已修改")
            return profile

        connection = reopen(run["model_profile"])
        if run.get("approval_profile"):
            connection["approval_profile"] = reopen(run["approval_profile"])
        if run.get("worker_profile"):
            connection["worker_profile"] = reopen(run["worker_profile"])
        return connection

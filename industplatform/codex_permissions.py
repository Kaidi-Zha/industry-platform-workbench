"""Per-run Codex sandbox and Guardian model selection."""

import json
import subprocess
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .settings import DEFAULTS
from .codex_runtime import ROOT, codex_environment, prepare_codex_state


REVIEW_ALIAS = "industplatform-approval"


@lru_cache(maxsize=4)
def bundled_models(binary):
    result = subprocess.run([binary, "debug", "models", "--bundled"], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
                            cwd=ROOT, env=codex_environment())
    models = json.loads(result.stdout)["models"]
    if not isinstance(models, list) or not models:
        raise ValueError("Codex 模型目录为空")
    return tuple(models)


def native_review_models(binary):
    if not binary:
        return []
    try:
        return [{"id": model["slug"], "name": model["display_name"]}
                for model in bundled_models(binary)
                if model.get("visibility") == "list" and "astra" not in model["slug"].lower()]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        return []


def cli_permissions(options):
    options = options or {}
    sandbox = options.get("sandbox_mode", DEFAULTS["sandbox_mode"])
    mode = options.get("approval_mode")
    automatic = mode == "auto_review"
    settings = {
        "approvals_reviewer": "auto_review" if automatic else "user",
        "sandbox_workspace_write.network_access": sandbox == "workspace-write" and options.get("workspace_network_access") is True,
    }
    if automatic:
        settings["features.guardian_approval"] = True
    return sandbox, "on-request" if mode in ("ask", "auto_review") else "never", settings


def review_catalog(binary, primary_model, reviewer_model):
    entries = {model["slug"]: dict(model) for model in bundled_models(binary)}
    if primary_model is None:
        if reviewer_model not in {item["id"] for item in native_review_models(binary)}:
            raise ValueError("审批模型在本机 Codex 目录中不可用")
        for model in entries.values():
            model["auto_review_model_override"] = reviewer_model
    else:
        if primary_model not in entries:
            default = next(model for model in entries.values() if model.get("use_responses_lite") is False)
            entries[primary_model] = {
                **default,
                "slug": primary_model, "display_name": primary_model,
                "description": None, "default_reasoning_level": None, "supported_reasoning_levels": [],
                "shell_type": "unified_exec", "visibility": "list", "apply_patch_tool_type": None,
                "supported_in_api": True, "priority": 99,
                "truncation_policy": {"mode": "bytes", "limit": 10000},
                "context_window": 272000, "max_context_window": 272000,
                "supports_search_tool": False, "supports_reasoning_effort_updates": False,
            }
        entries[primary_model]["auto_review_model_override"] = REVIEW_ALIAS
        # Keep Codex's Guardian policy, while using normal Responses on the loopback provider.
        entries[REVIEW_ALIAS] = {
            **entries["codex-auto-review"], "slug": REVIEW_ALIAS, "display_name": reviewer_model,
            "use_responses_lite": False, "visibility": "hide",
        }
    return {"models": list(entries.values())}


@contextmanager
def model_catalog(binary, primary_model=None, reviewer_model=None):
    if not reviewer_model:
        yield {}
        return
    with tempfile.TemporaryDirectory(prefix="indust-guardian-", dir=prepare_codex_state()) as directory:
        path = Path(directory) / "models.json"
        path.write_text(json.dumps(review_catalog(binary, primary_model, reviewer_model), ensure_ascii=False),
                        encoding="utf-8")
        yield {"model_catalog_json": str(path)}

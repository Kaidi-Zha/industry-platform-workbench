"""Optional model-provider adapter for the synthetic benchmark fixtures."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .provider_api import ProviderError, generate

TIERS = ("general", "advanced", "frontier")
MAX_REPLY_BYTES = 262144
EVALUATION_INSTRUCTIONS = "完成离线工程任务。按题面要求只返回一个 JSON 对象。"


class EvaluationError(RuntimeError):
    """Provider transport, response, or setup failure."""


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def model_config():
    base = os.environ.get("INDUST_MODEL_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("INDUST_MODEL_API_KEY", "").strip()
    raw_panel = os.environ.get("INDUST_MODEL_PANEL", "")
    if not (base and key and raw_panel):
        return None
    parsed = urllib.parse.urlsplit(base)
    if (
        not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
        or not (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}))
    ):
        raise EvaluationError("模型接口须使用 HTTPS 或本机 HTTP 基址")
    try:
        parsed.port
        panel = json.loads(raw_panel)
    except ValueError as exc:
        raise EvaluationError("模型基址或分档 JSON 格式无效") from exc
    if not isinstance(panel, dict) or set(panel) != set(TIERS):
        raise EvaluationError("模型分档必须包含 general、advanced、frontier")
    model_ids = []
    for tier in TIERS:
        models = panel[tier]
        if not isinstance(models, list) or not 1 <= len(models) <= 2:
            raise EvaluationError("每个能力档配置一至两款模型")
        if any(not isinstance(model, str) or not model.strip() or len(model) > 128 for model in models):
            raise EvaluationError("模型 ID 无效")
        model_ids.extend(models)
    if len(set(model_ids)) != len(model_ids):
        raise EvaluationError("一款模型只能出现在一个档位中一次")
    return base, key, panel


def _task_prompt(task):
    return json.dumps({"task": task["description"], "inputs": task["inputs"]}, ensure_ascii=False)


def _answer(content):
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            pass
    return None


def solve_with_profile(task, profile):
    try:
        return _answer(generate(profile, EVALUATION_INSTRUCTIONS, _task_prompt(task), max_tokens=8192, request_timeout=90))
    except ProviderError as exc:
        raise EvaluationError(str(exc)) from None


def solve_with_provider(task, model, config):
    base, key, _ = config
    messages = [{"role": "system", "content": EVALUATION_INSTRUCTIONS},
                {"role": "user", "content": _task_prompt(task)}]
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_completion_tokens": 2048,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.build_opener(RejectRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise EvaluationError("模型接口返回 HTTP {}".format(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, OSError):
        raise EvaluationError("模型接口连接中断或超时") from None

    if len(raw) > MAX_REPLY_BYTES:
        raise EvaluationError("模型响应超过读取范围")
    try:
        reply = json.loads(raw)
        choice = reply["choices"][0]
        content = choice["message"].get("content")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise EvaluationError("模型接口响应不符合 Chat Completions 格式") from None
    return _answer(content)


def difficulty_from_panel(results, panel, repetitions=1):
    """Disagreements, provider errors, and exploratory panels remain unclassified."""
    if any(len(panel[tier]) < 2 for tier in TIERS):
        return "待复核"
    outcome = []
    for tier in TIERS:
        trials = [result for result in results if result["tier"] == tier]
        if len(trials) != len(panel[tier]) * repetitions or any(result["status"] != "scored" for result in trials):
            return "待复核"
        passes = {item["passed"] for item in trials}
        if len(passes) != 1:
            return "待复核"
        outcome.append(passes.pop())
    labels = {
        (True, True, True): "简单",
        (False, True, True): "较难",
        (False, False, True): "高难",
        (False, False, False): "复杂候选",
    }
    return labels.get(tuple(outcome), "待复核")

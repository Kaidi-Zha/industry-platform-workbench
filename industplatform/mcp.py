"""MCP stdio tools for local Agent clients, backed by the platform HTTP API."""

import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, agent_tasks, collection, industrial_bench
from .storage import AGENT_ROUTES

PROTOCOL_VERSION = "2025-11-25"
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RUN_ID_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
BENCHMARK_FOLDER = Path(__file__).resolve().parents[1] / ".local" / "benchmarks"
BENCHMARK_MAX_BYTES = 8 * 1024 * 1024


def tool(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool("get_catalog", "List registered collection routes and enabled evaluation cohorts."),
    tool("get_rules", "Read the engineering rules used by the CNC inspection demo."),
    tool("search_keywords", "Search the existing Forest industrial keyword index.", {
        "q": {"type": "string", "maxLength": 280},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }),
    tool("list_references", "Find evidence already in the Reference Library by industrial domain and keyword.", {
        "domain": {"type": "string", "maxLength": 100},
        "q": {"type": "string", "maxLength": 120},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
    }),
    tool("save_reference", "Record an original public source, its location, and the exact claim it supports.",
         {name: {"type": "string", "maxLength": size} for name, size in collection.REFERENCE_FIELDS.items()},
         [name for name in collection.REFERENCE_FIELDS if name in collection.REQUIRED_REFERENCE]),
    tool("start_run", "Queue collection or a CNC benchmark. Choose cohort=live to call configured model APIs.", {
        "route": {"type": "string", "maxLength": 80},
        "cohort": {"type": "string", "enum": ["offline", "live"]},
        "prompt": {"type": "string", "maxLength": 280},
        "seed": {"type": "integer", "minimum": 0, "maximum": 999999},
    }),
    tool("get_run", "Read status, candidate evidence, visible tasks, and evaluation results.", {
        "id": RUN_ID_SCHEMA,
    }, ("id",)),
    tool("wait_run", "Wait for a run to finish or return its current status at the timeout.", {
        "id": RUN_ID_SCHEMA,
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
    }, ("id",)),
    tool("export_benchmark", "Save the public ZIP for a completed run to the repository's benchmark folder.", {
        "id": RUN_ID_SCHEMA,
    }, ("id",)),
    tool("inspect_measurements", "Inspect two normalized metrology CSV texts offline. See get_rules for the rule scope.", {
        "requirements_csv": {"type": "string", "maxLength": 65536},
        "measurements_csv": {"type": "string", "maxLength": 65536},
    }, ("requirements_csv", "measurements_csv")),
    tool("benchmark_cases", "List cases linked to verified source evidence and available versioned benchmark formats."),
    tool("register_industrial_case", "Register a bounded source-linked case using the verified Reference IDs. Requires JSON with verifier, title, domain, scenario, part_id, revision, report_id, derivation, reference_ids and inputs.",
         {"case_json": {"type": "string", "maxLength": 12000}}, ("case_json",)),
    tool("build_industrial_benchmark", "Build and verify an industrial benchmark locally from a registered case. No model evaluation is started.", {
        "case_id": {"type": "string", "maxLength": 48},
        "template_id": {"type": "string", "maxLength": 48},
        "template_version": {"type": "integer", "minimum": 1, "maximum": 999999},
        "variant": {"type": "string", "enum": list(industrial_bench.VARIANTS)},
        "assignment": {"type": "string", "maxLength": 800},
    }, ("case_id", "template_id", "template_version")),
    tool(agent_tasks.TOOL_NAME,
         "Author a synthetic CNC first-article inspection task with two normalized CSVs. See get_rules for the rule scope.",
         {
             "title": {"type": "string", "maxLength": 100},
             "scenario": {"type": "string", "maxLength": 500},
             "requirements_csv": {"type": "string", "maxLength": agent_tasks.MAX_CSV_CHARS},
             "measurements_csv": {"type": "string", "maxLength": agent_tasks.MAX_CSV_CHARS},
         }, ("title", "scenario", "requirements_csv", "measurements_csv")),
]
TOOL_BY_NAME = {item["name"]: item for item in TOOLS}

def research_text(limit, choices=None):
    return {"type": "string", "maxLength": limit, **({"enum": choices} if choices else {})}


RESEARCH_TARGET = {
    "target_id": research_text(32), "stage": research_text(100), "deliverable": research_text(300),
    "level": research_text(12, ["document", "sample", "acceptance"]),
}
RESEARCH_RECORD = {
    "target_id": RESEARCH_TARGET["target_id"], "level": RESEARCH_TARGET["level"], "source_id": research_text(24),
    "locator": research_text(96), "quote": research_text(240), "title": research_text(220), "publisher": research_text(160),
    "kind": research_text(60), "claim": research_text(800), "version": research_text(120),
    "permission": research_text(20, ["stated", "unknown", "restricted"]),
    "license_quote": research_text(160), "record_id": research_text(100), "outcome": research_text(100),
    "relation_type": research_text(30, ["supplier_requirement", "actual_delivery", "collaboration",
                                       "demonstration", "compatibility"]),
    "from_party": research_text(160), "to_party": research_text(160), "artifact": research_text(240),
    "applicability": research_text(260),
}
RESEARCH_TOOLS = [
    tool("authority_sources", "Get authoritative publisher starting points, search backend and coverage targets."),
    tool("web_search", "Discover new candidates for a gap using the configured search engine. Check scope for a limited index.",
         {"query": research_text(260), "limit": {"type": "integer", "minimum": 1, "maximum": 8}}, ("query",)),
    tool("read_public_source", "Read the primary HTTPS source. Use focus words to locate passages. Returns source_id as id.",
         {"url": research_text(1200), "focus": research_text(240),
          "page": {"type": "integer", "minimum": 1, "maximum": 5000}}, ("url",)),
    tool("coverage_status", "Read verified evidence counts, gaps and past rounds before planning the next search path."),
    tool("set_coverage_target", "Add a deliverable to the checklist. Any expected count needs an exact excerpt from a verified reference.",
         {**RESEARCH_TARGET, "basis_ref": research_text(64), "count_basis": research_text(240),
          "expected_count": {"type": "integer", "minimum": 1, "maximum": 999}}, tuple(RESEARCH_TARGET)),
    tool("record_evidence", "Archive a claim at the exact locator from read_public_source. "
         "Omit quote for a document or sample without a relation to save a server-extracted excerpt. "
         "Acceptance and relations require a verbatim quote.",
         RESEARCH_RECORD, ("target_id", "level", "source_id", "locator", "title", "publisher", "kind", "claim", "permission")),
    tool("finish_research_round", "Close a search round after screening candidates. Name the search path and planned next action.",
         {"path": research_text(180), "next_step": research_text(400)}, ("path", "next_step")),
]

SELECT_BENCHMARK_SOURCE = tool(
    "select_benchmark_source",
    "Choose a verified Reference from this plan for offline benchmark construction. "
    "The prototype supports the official NIST/MTC Box Assembly archive for CNC/CMM inspection.",
    {"reference_id": research_text(64), "verifier": research_text(24, list(industrial_bench.INPUTS)),
     "scope_match": research_text(400)},
    ("reference_id", "verifier", "scope_match"),
)


def call_api(base, path, payload=None, binary=False):
    raw = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base + path, data=raw, headers={"Content-Type": "application/json"})
    try:
        with LOCAL_HTTP.open(request, timeout=3) as response:
            if binary:
                data = response.read(BENCHMARK_MAX_BYTES + 1)
                if len(data) > BENCHMARK_MAX_BYTES or response.headers.get_content_type() != "application/zip":
                    raise ValueError("Benchmark ZIP 数据无效或超出导出上限")
                return data
            return json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                message = json.load(exc).get("error")
            except (ValueError, AttributeError):
                message = None
        raise ValueError(message or "HTTP {}".format(exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError("本机 industplatform 服务不可用") from None


def run_tool(base, name, params):
    if name == "get_catalog":
        catalog = call_api(base, "/api/catalog")
        catalog["routes"] = [route for route in catalog["routes"] if route["id"] not in AGENT_ROUTES]
        return catalog
    if name == "get_rules":
        return call_api(base, "/api/rules")
    if name == "search_keywords":
        return call_api(base, "/api/keywords?" + urllib.parse.urlencode(params))
    if name == "list_references":
        return call_api(base, "/api/references?" + urllib.parse.urlencode(params))
    if name == "save_reference":
        return call_api(base, "/api/references", params)
    if name == "benchmark_cases":
        result = call_api(base, "/api/benchmarks")
        return {
            "cases": [{key: row[key] for key in ("id", "title", "domain", "verifier", "scenario")}
                      for row in result["cases"][:12]],
            "templates": [{key: row[key] for key in ("id", "version", "title", "domain", "verifier")}
                          for row in result["templates"][:24]],
            "variants": result["variants"],
        }
    if name == "register_industrial_case":
        try:
            value = json.loads(params.get("case_json", ""))
        except (TypeError, ValueError, RecursionError):
            raise ValueError("案例参数须为 JSON 对象") from None
        if not isinstance(value, dict):
            raise ValueError("案例参数须为 JSON 对象")
        return call_api(base, "/api/benchmarks/cases", {**value, "share_allowed": False})
    if name == "build_industrial_benchmark":
        allowed = set(TOOL_BY_NAME[name]["inputSchema"]["properties"]) | {"parent_run_id"}
        if params.keys() - allowed:
            raise ValueError("工业构题工具仅支持本地验收参数")
        return call_api(base, "/api/benchmarks", {**params, "cohort": "offline"})
    if name == "start_run":
        if params.get("route") in AGENT_ROUTES:
            raise ValueError("请在工作台发起 Agent 运行")
        if os.environ.get("INDUST_CODEX_GUI_MCP") == "1" and params.get("cohort") == "live":
            raise ValueError("请在工作台选择 CNC 首件检验和模型 API 盲测")
        return call_api(base, "/api/runs", params)
    if name in ("get_run", "wait_run", "export_benchmark"):
        run_id = params.get("id", "")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("运行 ID 无效")
        timeout = params.get("timeout_seconds", 20) if name == "wait_run" else 0
        if name == "wait_run" and (type(timeout) is not int or not 1 <= timeout <= 30):
            raise ValueError("等待时间须为 1 至 30 秒的整数")
        path = "/api/runs/" + run_id
        if name == "export_benchmark":
            payload = call_api(base, path + "/benchmark", binary=True)
            BENCHMARK_FOLDER.mkdir(parents=True, exist_ok=True)
            output = BENCHMARK_FOLDER / (run_id + ".zip")
            with tempfile.NamedTemporaryFile(dir=BENCHMARK_FOLDER, delete=False) as staged:
                temp_path = Path(staged.name)
                try:
                    staged.write(payload)
                    staged.close()
                    temp_path.replace(output)
                finally:
                    temp_path.unlink(missing_ok=True)
            return {"id": run_id, "benchmark_path": str(output), "bytes": len(payload)}
        deadline = time.monotonic() + timeout
        result = call_api(base, path)
        while result["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(min(0.4, max(0, deadline - time.monotonic())))
            result = call_api(base, path)
        if result.get("benchmark_ready") or (result["status"] == "completed" and result["route"] == "demo-cnc"):
            result["benchmark_url"] = base + path + "/benchmark"
        return result
    if name == "inspect_measurements":
        from .metrology import inspect_csv

        requirements = params.get("requirements_csv")
        measurements = params.get("measurements_csv")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 65536 for value in (requirements, measurements)):
            raise ValueError("请提供两份 1 至 65536 字符的 CSV 内容")
        return inspect_csv(requirements, measurements)
    if name == agent_tasks.TOOL_NAME:
        fields = TOOL_BY_NAME[name]["inputSchema"]["properties"]
        if params.keys() != fields.keys() or any(
            not isinstance(value, str) or not 1 <= len(value) <= fields[key]["maxLength"]
            for key, value in params.items()
        ):
            raise ValueError("请提供题目标题、场景及两份规范化 CSV")
        return agent_tasks.author(**params)
    raise ValueError("未知工具")


def dispatch(base, msg):
    method = msg.get("method")
    params = msg.get("params", {})
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "industplatform", "version": __version__}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        try:
            if not isinstance(name, str) or name not in TOOL_BY_NAME or not isinstance(args, dict):
                raise ValueError("工具名称或输入参数无效")
            result = run_tool(base, name, args)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except ValueError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    raise KeyError(method)


def _session_dispatch(base, msg, session_port):
    method = msg["method"]
    if method not in ("tools/list", "tools/call"):
        return dispatch(base, msg)
    params = msg.get("params", {})
    body = {"method": method, "name": params.get("name"), "arguments": params.get("arguments", {})}
    token = os.environ.get("INDUST_CODEX_MCP_TOKEN", "")
    request = urllib.request.Request(
        f"http://127.0.0.1:{session_port}/mcp", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with LOCAL_HTTP.open(request, timeout=170 if os.getenv("INDUST_SCOPE_SESSION") else 36) as response:
            result = json.load(response)["result"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        if method == "tools/list":
            raise KeyError(method) from None
        return {"content": [{"type": "text", "text": "本轮工业工具连接不可用"}], "isError": True}
    if method == "tools/list":
        return result
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def listen(port, session_port=None):
    base = "http://127.0.0.1:{}".format(port)
    for line in sys.stdin:
        request_id = None
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
                raise TypeError("Invalid JSON-RPC request")
            if "id" not in msg:
                continue
            request_id = msg["id"]
            if not isinstance(msg.get("params", {}), dict):
                raise TypeError("Invalid params")
            result = dispatch(base, msg) if session_port is None else _session_dispatch(base, msg, session_port)
            payload = {"result": result}
        except json.JSONDecodeError:
            payload = {"error": {"code": -32700, "message": "Parse error"}}
        except TypeError as exc:
            payload = {"error": {"code": -32600, "message": str(exc)}}
        except KeyError:
            payload = {"error": {"code": -32601, "message": "Method not found"}}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, **payload}, ensure_ascii=False) + "\n")
        sys.stdout.flush()

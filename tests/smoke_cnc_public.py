"""Run a native Codex/MCP pilot with replayed discovery and live public originals."""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import run_codex
from industplatform import codex_protocol as wire, codex_runtime, public_web
from industplatform.mcp import call_api
from industplatform.server import RequestHandler, workspace
from tests.smoke_parallel_codex import eventually


ROOT = Path(__file__).resolve().parents[1]
DMSC = "https://qifstandards.org/qif-demonstration-imts-chicago-2016/"
QINETIQ = "https://www.qinetiq.com/-/media/b6572f680bec4ee6a0439f716fc5d02d.ashx"
COMMIT = "6e861f04f7be40c985005fde683edd51af103790"
QIF = ("https://raw.githubusercontent.com/usnistgov/QIF/" + COMMIT
       + "/src/MSVCQIF/MSVCQIF/Win32/Debug/QIF_Results_Sample.xml")
DISCOVERY = {
    "supplier first article inspection FAIR final submittal quality clause": (QINETIQ, "QinetiQ M1 supplier guide"),
    "CMM QIF NIST open XML results sample": (QIF, "NIST QIF results example"),
}


def receipt(body):
    output = [item["output"] for item in body["input"] if item.get("type") == "function_call_output"][-1]
    if isinstance(output, list):
        output = "\n".join(item["text"] for item in output if "text" in item)
    output = output.rsplit("\nOutput:", 1)[-1].strip()
    try:
        return json.loads(output)
    except ValueError as exc:
        raise ValueError(f"MCP receipt was not JSON: {str(output)[:1200]}") from exc


def grounded(body, phrase, **metadata):
    source = receipt(body)
    block = next(row for row in source["passages"] if phrase in row["text"])
    return "record_evidence", {"source_id": source["id"], "locator": block["locator"], "quote": phrase, **metadata}


class Fixtures(BaseHTTPRequestHandler):
    """Loopback fixtures replay public search hits and drive real Codex tool calls."""

    def log_message(self, *_):
        pass

    def send(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)["q"][0]
        url, title = DISCOVERY[query]
        self.send(json.dumps({"results": [{"url": url, "title": title}]}).encode(), "application/json")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            self.respond(body)
        except (AssertionError, ValueError, KeyError, StopIteration) as exc:
            self.server.errors.append(str(exc) or type(exc).__name__)
            self.send_error(400)

    def respond(self, body):
        assert body["model"] == "gpt-5.4" and self.headers["Authorization"] == "Bearer fixture-p1-key"
        self.server.turn += 1
        turn = self.server.turn
        queries = list(DISCOVERY)
        action = None
        if turn == 1:
            action = ("authority_sources", {})
        elif turn == 2:
            assert any(row["id"] == "dmsc" for row in receipt(body)["entries"])
            action = ("web_search", {"query": queries[0]})
        elif turn == 3:
            hit = receipt(body)["results"][0]
            assert hit["url"] == QINETIQ and not hit["authority"]
            action = ("read_public_source", {"url": hit["url"], "focus": "shipment completed FAIR provided"})
        elif turn == 4:
            action = grounded(body, "With shipment of deliverable material, a completed FAIR shall be provided.",
                              target_id="handoff", level="document", permission="restricted",
                              title="Supplier Quality Clause M1 FAI Guide", publisher="QinetiQ",
                              version="QMS0013033 Rev A", kind="供应商交付规范", relation_type="supplier_requirement",
                              from_party="适用 M1 的供方", to_party="QinetiQ", artifact="随货提交完整 FAIR",
                              applicability="QinetiQ Quality Clause M1，第 3 节 Final Submittal",
                              claim="适用 M1 条款的供方应随交付物提交完整的 FAIR。")
        elif turn == 5:
            assert receipt(body)["outside_entries"]
            action = ("finish_research_round", {"path": "采购方公开条款", "next_step": "追踪公开 QIF 结果样例。"})
        elif turn == 7:
            assert "追踪公开 QIF 结果样例" in json.dumps(body["input"], ensure_ascii=False)
            action = ("web_search", {"query": queries[1]})
        elif turn == 8:
            assert receipt(body)["results"][0]["url"] == QIF
            action = ("read_public_source", {"url": QIF, "focus": "CharacteristicActual MeasuredValue actual XYZ"})
        elif turn == 9:
            action = grounded(body, "<Value>-0.020323885080472</Value>", target_id="measurement",
                              level="sample", permission="unknown", title="NIST QIF Results Sample",
                              publisher="NIST", version=COMMIT, kind="XML 工程样例",
                              claim="此 QIF XML 示例含数值化特性结果，可供格式与字段核验，生产来源未证实。")
        elif turn == 10:
            assert receipt(body)["level"] == "sample"
            action = ("read_public_source", {"url": DMSC, "focus": "workflow incorporate measurement plan"})
        elif turn == 11:
            action = grounded(body, "measurement plan creation for caliper and CMM measurements",
                              target_id="measurement", level="document", permission="unknown",
                              title="QIF Demonstration at IMTS Chicago 2016", publisher="DMSC",
                              kind="工序演示", version="2016-08-29",
                              claim="DMSC 的 IMTS 演示从 CAD/PMI 和计量计划推进到 CMM 程序与结果分析。")
        elif turn == 12:
            action = ("finish_research_round", {"path": "原作者仓库与标准组织演示",
                                              "next_step": "查可公开的签发或接收验收记录和加工程序。"})
        if action:
            name, args = action
            namespace = next(tool for tool in body["tools"] if tool["name"] == "mcp__industplatform")
            assert any(item["name"] == name for item in namespace["tools"])
            item = {"type": "function_call", "id": f"fc_{turn}", "call_id": f"call_{turn}",
                    "name": name, "namespace": namespace["name"], "arguments": json.dumps(args, ensure_ascii=False),
                    "status": "completed"}
        else:
            item = wire.message("本轮入口已核验，实际生产验收和工艺文件仍待查。")
        response = {"id": f"resp_p1_{turn}", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
        self.send(b"".join(wire.sse(wire.events(response))), "text/event-stream")


def main():
    output = Path(tempfile.mkdtemp(prefix="cnc-public-pilot-", dir=ROOT / ".local"))
    codex_runtime.CODEX_STATE_DIR = run_codex.CODEX_STATE_DIR = output / "codex"
    os.environ["INDUST_VAULT_KEY_FILE"] = str(output / "vault.key")
    os.environ.pop("INDUST_BRAVE_SEARCH_KEY", None)
    os.environ.pop("INDUST_SEARXNG_URL", None)
    open_index = public_web.search("Design Manufacturing and Inspection Data for a Three-Component Assembly Hedberg", 3)
    assert open_index["scope"] == "publications" and open_index["results"]
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixtures)
    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    with fixture, server:
        fixture.turn, fixture.errors = 0, []
        endpoint = f"http://127.0.0.1:{fixture.server_port}"
        os.environ["INDUST_SEARXNG_URL"] = endpoint
        server.anchor = output / "pilot.sqlite3"
        server.workspace_lock = threading.RLock()
        server.store, server.profiles, server.settings, server.workers = workspace(server, server.anchor)
        server.settings.update({"max_child_runs": 0, "max_authored_tasks": 0})
        for service in (fixture, server):
            threading.Thread(target=service.serve_forever, daemon=True).start()
        server.workers.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            profile = call_api(base, "/api/model-profiles", {
                "name": "Local pilot", "model": "gpt-5.4", "protocol": "openai-responses",
                "base_url": endpoint, "api_key": "fixture-p1-key",
            })["profile"]
            run = call_api(base, "/api/runs", {
                "route": "api-agent", "model_profile_id": profile["id"], "public_research": True,
                "prompt": "核对 CNC/CMM 首件检验公开证据。追踪 CMM 输出与供方的 FAIR 交付要求。",
                "plan": {"domain": "机械与离散制造", "scenario": "CNC／CMM 首件检验", "stages": "S1/S4/S5/S6",
                         "region": "不限", "time_window": "不限", "asset_types": "标准、工序和实样",
                         "deliverables": "可复核的 Reference 与待查缺口"},
            })

            def completed():
                latest = server.store.get(run["id"])
                return latest if latest["status"] not in {"queued", "running"} else None

            finished = eventually(completed, seconds=95)
            library = call_api(base, "/api/references?run=" + run["id"])
            coverage = finished["result"]["coverage"]
            assert not fixture.errors, fixture.errors
            assert coverage["round_count"] == 2 and library["total"] == 3, (finished, library)
            assert finished["status"] == "needs_review" and not coverage["complete"]
            assert any(record.get("relation", {}).get("type") == "supplier_requirement" for record in library["items"])
            assert sum(not record["verification"]["authority"] for record in library["items"]) == 2
            assert not (codex_runtime.CODEX_STATE_DIR / "auth.json").exists()
            report = output / "report.json"
            report.write_text(json.dumps({"run_id": run["id"], "run_status": finished["status"],
                                         "coverage": coverage, "references": library["items"],
                                         "live_publication_search": open_index}, ensure_ascii=False, indent=2))
            print("PASS", library["total"], "references;", coverage["round_count"], "rounds;", report)
        finally:
            server.shutdown()
            server.workers.close()
            fixture.shutdown()


if __name__ == "__main__":
    (ROOT / ".local").mkdir(exist_ok=True)
    main()

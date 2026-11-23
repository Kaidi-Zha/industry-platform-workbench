"""Run the full confirmed CNC/CMM plan with live public evidence and a local model fixture."""

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import run_codex
from industplatform import codex_protocol as wire, codex_runtime, industrial_bench, mcp, nist_benchmark
from industplatform.server import RequestHandler, workspace
from tests.smoke_cnc_public import receipt
from tests.smoke_parallel_codex import eventually


QUERY = "Design Manufacturing and Inspection Data for a Three-Component Assembly Hedberg"
PAPER = "https://nvlpubs.nist.gov/nistpubs/jres/124/jres.124.004.pdf"


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            item = self.respond(body)
        except (ValueError, TypeError, AssertionError, KeyError, StopIteration) as exc:
            self.server.error = repr(exc)
            self.send_error(400)
            return
        response = {"id": "resp_flow_" + str(self.server.calls), "status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
        raw = b"".join(wire.sse(wire.events(response)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def respond(self, body):
        self.server.calls += 1
        step = self.server.calls
        assert body["model"] == "gpt-5.4" and self.headers["Authorization"] == "Bearer fixture-flow-key"
        if step == 1:
            assert "用户已确认的采集计划" in json.dumps(body, ensure_ascii=False)
            name, args = "web_search", {"query": QUERY, "limit": 3}
        elif step == 2:
            hit = next(hit for hit in receipt(body)["results"] if hit["url"] == PAPER)
            name, args = "read_public_source", {"url": hit["url"], "focus": "Data DOI 1503154"}
        elif step == 3:
            source = receipt(body)
            phrase = "https://doi.org/10.18434/T4/1503154"
            passage = next(block for block in source["passages"] if phrase in block["text"])
            name, args = "record_evidence", {
                "source_id": source["id"], "locator": passage["locator"], "quote": phrase, "target_id": "measurement",
                "level": "document", "permission": "unknown", "title": "Design, Manufacturing, and Inspection Data",
                "kind": "数据说明论文", "publisher": "NIST", "version": "2019",
                "claim": "NIST 数据说明论文列出该制造检验数据包的 DOI。",
            }
        elif step == 4:
            name, args = "select_benchmark_source", {
                "reference_id": receipt(body)["id"], "verifier": "cnc-fai",
                "scope_match": "计划允许全球公开试验数据，NIST/MTC 数据包含 CMM 来料检验，属于 S5。",
            }
        elif step == 5:
            assert receipt(body)["verifier"] == "cnc-fai"
            name, args = "finish_research_round", {"path": "公开出版物追踪实验数据包",
                                                  "next_step": "定点读取已选的 QIF 样本并离线构题。"}
        else:
            assert step == 6
            return wire.message("已核验数据来源并选定实样入口。")
        namespace = next(tool for tool in body["tools"] if tool.get("name") == "mcp__industplatform")
        assert any(tool["name"] == name for tool in namespace["tools"])
        return {"type": "function_call", "id": f"fc_{step}", "call_id": f"call_{step}",
                "name": name, "namespace": namespace["name"], "arguments": json.dumps(args, ensure_ascii=False),
                "status": "completed"}


def main():
    root = Path(__file__).resolve().parents[1] / ".local"
    root.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="agent-benchmark-smoke-", dir=root))
    codex_runtime.CODEX_STATE_DIR = run_codex.CODEX_STATE_DIR = output / "codex"
    os.environ["INDUST_VAULT_KEY_FILE"] = str(output / "vault.key")
    os.environ.pop("INDUST_BRAVE_SEARCH_KEY", None)
    os.environ.pop("INDUST_SEARXNG_URL", None)
    with ThreadingHTTPServer(("127.0.0.1", 0), Model) as upstream, ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler) as srv:
        upstream.calls, upstream.error = 0, None
        srv.anchor, srv.workspace_lock = output / "workflow.sqlite3", threading.RLock()
        srv.store, srv.profiles, srv.settings, srv.workers = workspace(srv, srv.anchor)
        for service in (srv, upstream):
            threading.Thread(target=service.serve_forever, daemon=True).start()
        srv.workers.start()
        api = f"http://127.0.0.1:{srv.server_port}"
        try:
            profile = mcp.call_api(api, "/api/model-profiles", {
                "name": "CNC workflow fixture", "model": "gpt-5.4", "protocol": "openai-responses",
                "api_key": "fixture-flow-key", "base_url": f"http://127.0.0.1:{upstream.server_port}",
            })["profile"]
            assert not industrial_bench.cases(srv.store)
            run = mcp.call_api(api, "/api/runs", {
                "route": "api-agent", "model_profile_id": profile["id"], "auto_benchmark": True,
                "prompt": "查公开 CNC／CMM 首件检验实样，并生成离线题包。",
                "plan": {"domain": nist_benchmark.DOMAIN, "scenario": "CNC／CMM 首件检验", "stages": "S5",
                         "region": "不限", "time_window": "不限", "asset_types": "公开试验和 CMM 计量资料",
                         "deliverables": "实样尺寸复核题包"},
            })

            def done():
                result = srv.store.get(run["id"])
                return result if result["status"] not in {"running", "queued"} else None

            finished = eventually(done, seconds=90)
            assert finished["status"] == "completed", (finished.get("error"), upstream.error, finished["steps"])
            assert upstream.calls == 6 and not upstream.error
            result = finished["result"]
            assert result["sample"]["storage"] == "memory"
            assert result["sample"]["transfer_bytes"] < 2 * 1024 * 1024
            case_id = finished["benchmark"]["case_id"]
            with closing(srv.store.connect()) as db:
                case = json.loads(db.execute("SELECT body FROM industrial_cases WHERE id = ?", (case_id,)).fetchone()[0])
                assert case["input_run_id"] == run["id"] and "inputs" not in case
                assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
            assert industrial_bench.case(srv.store, case_id)["inputs"]
            turn = mcp.call_api(api, "/api/conversations/" + run["id"])["turns"][0]
            assert turn["benchmark_ready"] and turn["status"] == "completed"
            references = mcp.call_api(api, "/api/references?run=" + run["id"])["items"]
            assert {ref["verification"]["level"] for ref in references} == {"sample", "document"}
            assert not (codex_runtime.CODEX_STATE_DIR / "auth.json").exists()
            content = mcp.call_api(api, "/api/runs/" + run["id"] + "/benchmark", binary=True)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                manifest = json.loads(archive.read("benchmark.json"))
                scores = json.loads(archive.read("results.json"))
                assert scores["panel"] == {} and scores["evaluations"][0]["tier"] == "offline"
                assert scores["evaluations"][0]["passed"] is True
                assert set(archive.namelist()) == {"benchmark.json", "results.json"} | {
                    item["path"] for item in manifest["tasks"][0]["inputs"].values()
                }
                for item in manifest["tasks"][0]["inputs"].values():
                    assert hashlib.sha256(archive.read(item["path"])).hexdigest() == item["sha256"]
            for confirmations in ({"confirm_model_send": True}, {"confirm_case_permission": True}):
                try:
                    mcp.call_api(api, "/api/benchmarks", {**finished["benchmark"], "cohort": "live", **confirmations})
                except ValueError as exc:
                    assert "确认向模型发送" in str(exc)
                else:
                    raise AssertionError("Blind evaluation did not require both confirmations")
            assert not list(output.rglob("*.qif")) and not list(output.rglob("*.zip"))
            (output / "benchmark.zip").write_bytes(content)  # Explicitly save the tested client download only.
            (output / "report.json").write_text(json.dumps({
                "id": run["id"], "steps": finished["steps"], "coverage": result["coverage"],
                "sample": result["sample"], "offline_checks": scores["evaluations"],
                "archive_bytes": len(content), "references": references,
            }, ensure_ascii=False, indent=2))
            print(f"PASS: public search + verified QIF range + one offline benchmark run. {output}")
        finally:
            srv.shutdown()
            srv.workers.close()
            upstream.shutdown()


if __name__ == "__main__":
    main()

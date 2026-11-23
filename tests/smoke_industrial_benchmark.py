"""Exercise actual NIST inputs with native Codex authoring and loopback model judging."""

import argparse
import csv
import io
import json
import os
import tempfile
import threading
import zipfile
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import run_codex
from industplatform import codex_protocol as wire, codex_runtime, mcp
from industplatform.nist_benchmark import import_cases
from industplatform.server import RequestHandler, workspace
from tests.smoke_cnc_public import receipt
from tests.smoke_parallel_codex import eventually


def independent_answer(task):
    files = task["inputs"]
    measurements = {row["characteristic_id"]: row for row in csv.DictReader(io.StringIO(files["measurements.csv"]))}
    findings = []
    for row in csv.DictReader(io.StringIO(files["requirements.csv"])):
        identity = row["characteristic_id"]
        measured = measurements.get(identity)
        if not measured:
            details = ("hold", "missing_measurement", None, None)
        elif measured["revision"] != row["revision"]:
            details = ("hold", "revision_mismatch", None, None)
        else:
            value = Decimal(measured["value"])
            good = Decimal(row["lower_limit"]) <= value <= Decimal(row["upper_limit"])
            details = ("pass" if good else "fail", "within_limits" if good else "outside_limits", str(value), row["unit"])
        findings.append(dict(zip(("status", "code", "value_in_requirement_unit", "unit"), details),
                             characteristic_id=identity))
    statuses = {row["status"] for row in findings}
    status = next(name for name in ("hold", "fail", "pass") if name in statuses)
    handoff = {**json.loads(files["job.json"]), "action": {"hold": "hold", "fail": "segregate", "pass": "review_release"}[status]}
    return {"status": status, "findings": findings, "handoff": handoff}


class Providers(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            slot = self.path.strip("/").split("/")[0]
            assert body["model"] == "gpt-5.4"
            assert self.headers["Authorization"] == "Bearer fixture-benchmark-" + slot
            self.server.calls.append(slot)
            if slot == "author":
                data, mime = self.author(body), "text/event-stream"
            else:
                task = json.loads(body["input"][0]["content"])
                assert set(task) == {"task", "inputs"} and "references" not in task
                answer = independent_answer(task)
                if slot.startswith("general"):
                    answer["handoff"]["action"] = "release"
                data = json.dumps({"output": [wire.message(json.dumps(answer))]}).encode()
                mime = "application/json"
        except (AssertionError, ValueError, TypeError, KeyError, StopIteration) as exc:
            self.server.errors.append(repr(exc))
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def author(self, body):
        step = self.server.calls.count("author")
        if step == 1:
            name, args = "benchmark_cases", {}
        elif step == 2:
            case = next(row for row in receipt(body)["cases"] if row["verifier"] == "cnc-fai")
            name, args = "build_industrial_benchmark", {"case_id": case["id"], "template_id": "cnc-fai",
                                                        "template_version": 1, "variant": "wrong_revision"}
        elif step == 3:
            result = receipt(body)
            self.server.authored = result["id"]
            name, args = "wait_run", {"id": result["id"], "timeout_seconds": 10}
        else:
            assert step == 4 and receipt(body)["status"] == "completed"
            name, args = "", None
        if name:
            namespace = next(item for item in body["tools"] if item["name"] == "mcp__industplatform")
            assert any(tool["name"] == name for tool in namespace["tools"])
            item = {"type": "function_call", "id": "fc_" + str(step), "call_id": "call_" + str(step),
                    "name": name, "namespace": namespace["name"], "arguments": json.dumps(args), "status": "completed"}
        else:
            item = wire.message("本地工业题包已生成")
        response = {"id": "resp_" + str(step), "status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
        return b"".join(wire.sse(wire.events(response)))


def run(qif, output):
    codex_runtime.CODEX_STATE_DIR = run_codex.CODEX_STATE_DIR = output / "codex"
    os.environ["INDUST_VAULT_KEY_FILE"] = str(output / "vault.key")
    output_db = output / "benchmark.sqlite3"
    with ThreadingHTTPServer(("127.0.0.1", 0), Providers) as upstream:
        upstream.calls, upstream.errors, upstream.authored = [], [], None
        with ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler) as server:
            server.anchor, server.workspace_lock = output_db, threading.RLock()
            server.store, server.profiles, server.settings, server.workers = workspace(server, output_db)
            specimens = import_cases(server.store, qif, share_allowed=True)  # loopback endpoints only in this fixture
            result_by_verifier = {value["verifier"]: value for value in specimens}
            for service in (upstream, server):
                threading.Thread(target=service.serve_forever, daemon=True).start()
            server.workers.start()
            api = f"http://127.0.0.1:{server.server_port}"
            try:
                def profile(slot, tier=""):
                    return mcp.call_api(api, "/api/model-profiles", {
                        "name": slot, "tier": tier, "model": "gpt-5.4", "protocol": "openai-responses",
                        "base_url": f"http://127.0.0.1:{upstream.server_port}/{slot}",
                        "api_key": "fixture-benchmark-" + slot,
                    })["profile"]["id"]

                def wait_done(identifier):
                    def done():
                        run = server.store.get(identifier)
                        return run if run["status"] not in {"queued", "running"} else None
                    result = eventually(done, seconds=60)
                    assert result["status"] == "completed", (
                        result["id"], result["steps"], result.get("error"), upstream.errors,
                    )
                    return result

                author = profile("author")
                run = mcp.call_api(api, "/api/runs", {
                    "route": "api-agent", "model_profile_id": author, "prompt": "基于可用实样构造一条错版反例。",
                    "plan": {"domain": "机械与离散制造", "scenario": "工业构题", "stages": "S5",
                             "region": "不限", "time_window": "不限", "asset_types": "公开计量样本",
                             "deliverables": "当地离线题包"},
                })
                agent = wait_done(run["id"])
                assert upstream.authored in agent["result"]["agent_run_ids"]
                assert not (codex_runtime.CODEX_STATE_DIR / "auth.json").exists()
                authored = wait_done(upstream.authored)
                assert authored["result"]["tasks"][0]["variant"] == "wrong_revision"

                request = {"case_id": result_by_verifier["qif-intake"]["id"], "template_id": "qif-intake",
                           "template_version": 1, "variant": "duplicate"}
                qif_run = wait_done(mcp.call_api(api, "/api/benchmarks", request)["id"])
                assert qif_run["result"]["tasks"][0]["complexity"]["records"] == 4

                custom = {"title": "盖板班组模板", "domain": "机械与离散制造", "verifier": "cnc-fai",
                          "role": "复核员", "recipient": "质量组", "business_value": "检查处置依据",
                          "fields": {"urgency": "紧急度"}}
                format1 = mcp.call_api(api, "/api/benchmarks/templates", custom)
                format2 = mcp.call_api(api, "/api/benchmarks/templates", {
                    **custom, "recipient": "接收二组", "id": format1["id"], "version": format1["version"],
                })
                assert format2["version"] == 2
                selections = [profile(tier + str(number), tier) for tier in ("general", "advanced", "frontier")
                              for number in (1, 2)]
                request = {"case_id": result_by_verifier["cnc-fai"]["id"], "template_id": format1["id"],
                           "template_version": 1, "variant": "missing", "metadata": {"urgency": "按批"}, "cohort": "live",
                           "repetitions": 2, "profile_ids": selections}
                try:
                    mcp.call_api(api, "/api/benchmarks", request)
                except ValueError as exc:
                    assert "确认" in str(exc)
                else:
                    raise AssertionError("blind evaluation started without confirmation")
                blind = wait_done(mcp.call_api(api, "/api/benchmarks", {**request, "confirm_model_send": True})["id"])
                result = blind["result"]
                assert len(result["evaluations"]) == 12
                assert {row["profile_id"] for row in result["evaluations"]} == set(selections)
                assert all(upstream.calls.count(tier + str(number)) == 2 for tier in ("general", "advanced", "frontier")
                           for number in (1, 2))
                assert result["tasks"][0]["measured_difficulty"] == "较难"
                assert not upstream.errors, upstream.errors
                for record in (authored, qif_run, blind):
                    payload = mcp.call_api(api, f"/api/runs/{record['id']}/benchmark", binary=True)
                    (output / (record["id"] + ".zip")).write_bytes(payload)
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        manifest = json.loads(archive.read("benchmark.json"))
                        task = manifest["tasks"][0]
                        assert "reference" not in task and task["references"]
                        assert set(json.loads(archive.read("results.json"))).issuperset({"metrics", "tool", "evaluations"})
                        if record is blind:
                            assert task["template"]["version"] == 1 and task["fields"]["urgency"] == "按批"
                            assert task["workflow"]["recipient"] == "质量组"
                rows = mcp.call_api(api, "/api/library?kind=tasks")["items"]
                assert len(rows) == 3 and all(row["exportable"] for row in rows)
                (output / "report.json").write_text(json.dumps({
                    "author_run": agent["id"], "offline_cnc": authored["id"], "offline_qif": qif_run["id"],
                    "blind": blind["id"], "tier_rates": result["tier_rates"], "scope": "loopback model responses",
                }, ensure_ascii=False, indent=2))
                print(f"PASS: real NIST evidence, native authoring, two verifiers, 12 graded loopback trials and export. {output}")
            finally:
                server.shutdown()
                server.workers.close()
                upstream.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qif", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / ".local"
    root.mkdir(exist_ok=True)
    run(args.qif, Path(tempfile.mkdtemp(prefix="industrial-benchmark-smoke-", dir=root)))

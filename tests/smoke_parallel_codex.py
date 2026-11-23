"""Run with python -m tests.smoke_parallel_codex and Codex CLI on PATH."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from industplatform import codex_protocol as wire


ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "gpt-5.4": ("openai-responses", "CNC", "Metrology"),
    "claude-sonnet-4-6": ("anthropic-messages", "Metrology", "CNC"),
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def eventually(check, seconds=35):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out: " + check.__name__)


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            self.reply()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.server.errors.append(repr(exc))
            self.send_error(400)

    def reply(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = body["model"]
        protocol, own, other = MODELS[model]
        claude = protocol == "anthropic-messages"
        token = self.headers["x-api-key"] if claude else self.headers["Authorization"].removeprefix("Bearer ")
        assert token == "fixture-only-key-" + model
        assert self.path.endswith("/messages" if claude else "/responses")
        content = json.dumps(body.get("messages", body.get("input")), ensure_ascii=False)
        delegated = "ROLE_CHECK_FIXTURE" in content and '"type": "function_call_output"' not in content
        with self.server.lock:
            turn = 70 if delegated else self.server.turns.get(model, 0) + 1
            if not delegated:
                self.server.turns[model] = turn
        output = None
        namespace_name = "mcp__industplatform"
        if delegated:
            assert model == "gpt-5.4"
            assert "使用现有的工业 MCP" in content
            assert not any(tool.get("name") == "exec_command" for tool in body["tools"])
            self.server.child_done.set()
            output = "Delegated source work completed"
        elif turn == 1:
            assert "用户已确认的采集计划" in content and f"{own} test workflow" in content
            name = "save_reference"
            args = {
                "title": own + " fixture reference", "domain": own, "publisher": "Local fixture",
                "kind": "技术报告", "url": f"https://example.test/{own.lower()}", "version": "2026-01",
                "locator": "Section 2", "claim": "Fixture claim for " + own, "stage": "S5",
            }
        elif turn == 2:
            assert own + " fixture reference" in content and "Section 2" in content
            self.server.blocked.add(model)
            self.server.release.wait(50)
            output = "Reply before shutdown"
        elif turn == 3:
            assert "服务已重启" in content, (model, "missing restart checkpoint")
            assert own + " fixture reference" in content, (model, "missing saved reference")
            name, args = "list_references", {"domain": other}
        elif turn == 4:
            assert other + " fixture reference" in content and "Fixture claim for " + other in content
            output = model + " resumed with " + other + " evidence"
        elif turn == 5:
            if claude:
                self.server.cancel_ready.set()
                self.server.cancel_release.wait(25)
                output = "Reply before cancellation"
            else:
                assert "European region" in content
                name, namespace_name = "spawn_agent", "collaboration"
                args = {"task_name": "source_check", "agent_type": "industrial_sources", "fork_turns": "none",
                        "message": "ROLE_CHECK_FIXTURE. Reply when done."}
        else:
            assert not claude and turn == 6 and self.server.child_done.wait(8)
            output = "Executed confirmed plan v2"
        if claude:
            if output is None:
                alias = next(tool["name"] for tool in body["tools"] if tool["name"].endswith(name))
                blocks = [{"type": "tool_use", "id": f"toolu_{turn}", "name": alias, "input": args}]
            else:
                blocks = [{"type": "text", "text": output}]
            result = {"content": blocks, "stop_reason": "tool_use" if output is None else "end_turn"}
            data, content_type = json.dumps(result).encode(), "application/json"
        else:
            assert body["reasoning"]["effort"] == "high"
            if output is None:
                namespace = next(tool for tool in body["tools"] if tool.get("name") == namespace_name)
                tool = next(tool for tool in namespace["tools"] if tool["name"] == name)
                if name == "spawn_agent":
                    roles = tool["parameters"]["properties"]["agent_type"]["description"]
                    assert "industrial_sources" in roles and "industrial_verifier" not in roles
                item = {"type": "function_call", "id": f"fc_{turn}", "call_id": f"call_{turn}",
                        "name": name, "namespace": namespace["name"], "arguments": json.dumps(args),
                        "status": "completed"}
            else:
                item = wire.message(output)
            response = {"id": f"resp_{turn}", "status": "completed", "output": [item],
                        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                                  "input_tokens_details": {"cached_tokens": 0},
                                  "output_tokens_details": {"reasoning_tokens": 0}}}
            data, content_type = b"".join(wire.sse(wire.events(response))), "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run(upstream, directory):
    env = {key: value for key, value in os.environ.items() if key not in {
        "OPENAI_API_KEY", "CODEX_API_KEY", "CHATGPT_API_KEY", "CODEX_HOME", "CODEX_SQLITE_HOME",
    }}
    env.update(INDUST_VAULT_KEY_FILE=str(directory / "vault.key"), INDUST_GUI_CODEX="1")
    auth_dir = directory / "isolated-codex"
    bootstrap = (
        "from pathlib import Path; from industplatform import codex_runtime;"
        f"codex_runtime.CODEX_STATE_DIR = Path({str(auth_dir)!r});"
        "from industplatform.__main__ import main; main()"
    )
    process = None
    port = "0"
    origin = ""
    log = directory / "service-log"

    def start():
        nonlocal process, port, origin
        with log.open("w") as errors:
            process = subprocess.Popen(
                [sys.executable, "-c", bootstrap, "serve", "--db", str(directory / "runs.sqlite3"), "--port", port],
                env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=errors, text=True,
            )
            announcement = process.stdout.readline()
            assert "127.0.0.1:" in announcement, log.read_text()
        port = announcement.strip().rsplit(":", 1)[1]
        origin = "http://127.0.0.1:" + port

    def stop():
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process and process.stdout:
            process.stdout.close()

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        query = urllib.request.Request(origin + path, data, {"Content-Type": "application/json"})
        with OPENER.open(query, timeout=5) as result:
            return json.load(result)

    try:
        start()
        runs = {}
        profile_ids = {}
        for model, (protocol, domain, _) in MODELS.items():
            profile = request("/api/model-profiles", {
                "protocol": protocol, "model": model, "name": "Fixture " + model,
                "api_key": "fixture-only-key-" + model, "base_url": f"http://127.0.0.1:{upstream.server_port}",
            })["profile"]
            profile_ids[model] = profile["id"]
            scope = {"domain": domain, "scenario": domain + " test workflow", "stages": "S5",
                     "region": "不限", "time_window": "2024–2026", "asset_types": "公开技术报告",
                     "sources": "本机测试服务", "deliverables": "原文定位和对应判断"}
            options = {"reasoning_effort": "high"} if protocol == "openai-responses" else {}
            runs[model] = request("/api/runs", {
                "route": "api-agent", "model_profile_id": profile["id"], "prompt": "Execute the confirmed plan.",
                "plan": scope, **options,
            })["id"]
        def both_waiting_on_provider():
            for model, identity in runs.items():
                turn = request("/api/conversations/" + identity)["turns"][0]
                assert turn["status"] not in {"failed", "needs_review"}, (model, turn)
            return upstream.blocked == set(runs)

        eventually(both_waiting_on_provider)
        for model in (*runs, *reversed(runs)):
            turn = request("/api/conversations/" + runs[model])["turns"][0]
            assert turn["status"] == "running" and turn["model_profile"]["model"] == model
        assert {task["status"] for task in request("/api/conversations")["conversations"]} == {"running"}
        for model, (_, domain, _) in MODELS.items():
            result = request("/api/references?" + urlencode({"domain": domain, "run": runs[model]}))
            assert [item["title"] for item in result["items"]] == [domain + " fixture reference"]
        assert not (auth_dir / "auth.json").exists()
        first, second = (runs[model] for model in MODELS)
        original = request("/api/conversations/" + first)["turns"][0]["plan"]
        revised = request("/api/conversations/" + first + "/plan", {
            "plan": {**original, "region": "European region"}, "reason": "Add EU sources",
            "plan_version": 1, "workflow_mode": "chat",
        })
        assert revised["version"] == 2
        request("/api/runs/" + first + "/control", {"action": "pause"})
        eventually(lambda: request("/api/runs/" + first)["status"] == "paused")
        assert request("/api/runs/" + second)["status"] == "running"
        stop()
        upstream.release.set()
        start()
        history = request("/api/conversations/" + first)
        assert history["turns"][0]["status"] == "paused"
        assert history["turns"][0]["plan_version"] == 1 and history["plan_history"][-1] == revised
        request("/api/runs/" + first + "/control", {"action": "resume"})

        def completed():
            turns = {model: request("/api/conversations/" + run_id)["turns"][0] for model, run_id in runs.items()}
            for turn in turns.values():
                assert turn["status"] not in {"failed", "needs_review"}, turn
            return turns if all(turn["status"] == "completed" for turn in turns.values()) else None

        turns = eventually(completed)
        for model, turn in turns.items():
            assert turn["restarts"] == 1 and turn["id"] == runs[model]
            assert turn["result"]["agent_summary"] == model + " resumed with " + MODELS[model][2] + " evidence"
            assert "fixture-only-key" not in json.dumps(turn)
        assert request("/api/references")["total"] == 2
        follow = request("/api/runs", {
            "route": "api-agent", "model_profile_id": profile_ids["gpt-5.4"],
            "reasoning_effort": "high", "prompt": "Use the newly confirmed plan.",
            "conversation_id": first, "plan_version": 2,
            "delegation": {"limit": 2, "roles": ["industrial_sources"]},
        })
        assert follow["plan_version"] == 2 and follow["plan_reason"] == "Add EU sources"
        eventually(lambda: request("/api/runs/" + follow["id"])["status"] == "completed")
        cancel = request("/api/runs", {
            "route": "api-agent", "model_profile_id": profile_ids["claude-sonnet-4-6"],
            "prompt": "Cancel this workflow after it starts.", "conversation_id": second,
        })
        assert upstream.cancel_ready.wait(15)
        request("/api/runs/" + cancel["id"] + "/control", {"action": "cancel"})
        eventually(lambda: request("/api/runs/" + cancel["id"])["status"] == "cancelled")
        upstream.cancel_release.set()
        assert request("/api/references")["total"] == 2
        assert not upstream.errors, upstream.errors
        assert not (auth_dir / "auth.json").exists()
        print("PASS: two concurrent native Codex runs, task switching, pause/restart/resume, plan history, native role, cancellation, selected API profiles and shared references.")
    except Exception:
        print(log.read_text()[-4000:], file=sys.stderr)
        print("Provider fixture calls:", upstream.turns, "errors:", upstream.errors, file=sys.stderr)
        raise
    finally:
        upstream.release.set()
        upstream.cancel_release.set()
        stop()


if __name__ == "__main__":
    if not shutil.which("codex"):
        raise SystemExit("Codex CLI is required")
    (ROOT / ".local").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="parallel-smoke-", dir=ROOT / ".local") as path, \
         ThreadingHTTPServer(("127.0.0.1", 0), Provider) as fixture:
        fixture.lock = threading.Lock()
        fixture.release = threading.Event()
        fixture.child_done = threading.Event()
        fixture.cancel_ready = threading.Event()
        fixture.cancel_release = threading.Event()
        fixture.blocked, fixture.turns, fixture.errors = set(), {}, []
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        try:
            run(fixture, Path(path))
        finally:
            fixture.shutdown()

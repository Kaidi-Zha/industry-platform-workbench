"""Run the installed Codex CLI with this repository's industrial configuration."""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from industplatform.codex_runtime import CODEX_STATE_DIR, ROOT, codex_environment


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROFILE = "industplatform-codex"
DEFAULT_PORT = 18765


def read_health(port):
    try:
        with HTTP.open("http://127.0.0.1:{}/health".format(port), timeout=0.3) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {}


def request_config(port, path, fields=None):
    body = json.dumps(fields, ensure_ascii=False).encode() if fields is not None else None
    request = urllib.request.Request(
        "http://127.0.0.1:{}{}".format(port, path), data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with HTTP.open(request, timeout=200) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                message = json.load(exc).get("error")
            except (ValueError, AttributeError):
                message = None
        raise RuntimeError(message or "工作台请求失败（HTTP {}）".format(exc.code)) from None


def inspect_config(options):
    if options.mode == "settings":
        fields = None
        if options.setting:
            key, raw = options.setting.split("=", 1)
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            fields = {key: value}
        return request_config(options.port, "/api/settings", fields)
    if options.mode == "models":
        route = "/api/model-profiles"
        if options.action == "list":
            return request_config(options.port, route)
        return request_config(options.port, route + "/" + options.action, {"id": options.profile_id})
    if options.action == "move":
        return request_config(options.port, "/api/storage/move", {"path": options.path})["storage"]
    return request_config(options.port, "/api/settings")["storage"]


@contextmanager
def backend(port, attach):
    health = read_health(port)
    if attach:
        if health.get("profile") != PROFILE:
            raise RuntimeError("请先在此仓库启动端口 {} 的 industplatform 服务".format(port))
        yield
        return
    if health:
        raise RuntimeError("端口 {} 已有服务，可在确认是此仓库的服务后用 --attach".format(port))

    with subprocess.Popen(
        [sys.executable, "-B", "-m", "industplatform", "serve", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
    ) as child:
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("industplatform 服务启动失败")
                if read_health(port).get("profile") == PROFILE:
                    print("industplatform 工作台已启动 http://127.0.0.1:{}/".format(port), file=sys.stderr)
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("industplatform 服务启动超时")
            yield
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()


def config_args(settings):
    args = []
    for key, value in settings.items():
        args.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=False)])
    return args


def codex_args(executable, port, *, gateway=None, guidance="", state_home=CODEX_STATE_DIR,
               shared_login=False, mcp_server="industplatform"):
    instruction = (ROOT / "instructions" / "agent.md").read_text(encoding="utf-8").strip()
    server = "mcp_servers." + mcp_server
    settings = {
        "sqlite_home": str(state_home),
        "log_dir": str(state_home / "log"),
        "developer_instructions": "\n\n".join(part for part in (instruction, guidance) if part),
        server + ".command": sys.executable,
        server + ".args": ["-B", str(ROOT / "run_mcp.py"), "--port", str(port)],
        server + ".cwd": str(ROOT),
        server + ".tool_timeout_sec": 40,
    }
    if not shared_login:
        settings.update(cli_auth_credentials_store="file", mcp_oauth_credentials_store="file")
    if gateway is not None:
        if gateway.profile:
            settings.update(gateway.settings())
        if gateway.tools:
            settings[server + ".args"].extend(["--session-port", str(gateway.port)])
            settings[server + ".env_vars"] = ["INDUST_CODEX_MCP_TOKEN"]
            settings.setdefault("shell_environment_policy.exclude", ["INDUST_CODEX_MCP_TOKEN"])
    return [executable, "--no-daemon", "-C", str(ROOT), *config_args(settings)]


def run_configured_codex(executable, options, extra):
    from industplatform.codex_agent import DELEGATION_ROLES, worker_role_settings
    from industplatform.codex_gateway import serve
    from industplatform.credentials import CredentialError
    from industplatform.model_profiles import ModelProfiles
    from industplatform.storage import RunStore

    current = request_config(options.port, "/api/settings")
    preferences = current["preferences"]
    connection = options.model_profile or (preferences["default_model"] if options.mode != "native" else "")
    worker_id = preferences["default_worker_model"]
    if connection in ("", "@codex"):
        return subprocess.run(codex_args(executable, options.port) + extra, cwd=ROOT, env=codex_environment()).returncode

    store = ModelProfiles(RunStore(current["storage"]["database_path"]))
    overrides = {} if options.reasoning_effort is None else {"reasoning_effort": options.reasoning_effort}
    try:
        profile = store.get(connection, run_options=overrides)
        worker = store.get(worker_id) if worker_id else None
        with serve(profile, worker=worker) as gateway, worker_role_settings(gateway, ("worker", *DELEGATION_ROLES)) as roles:
            args = codex_args(executable, options.port, gateway=gateway) + config_args(roles)
            result = subprocess.run(args + extra, cwd=ROOT, env=gateway.environment())
            if result.returncode and gateway.error:
                print(gateway.error, file=sys.stderr)
            return result.returncode
    except (CredentialError, ValueError) as exc:
        raise RuntimeError(str(exc)) from None


def main():
    parser = argparse.ArgumentParser(description="Use Codex as the industplatform Agent")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--attach", action="store_true", help="connect to this repo's already running service")
    parser.add_argument("--model-profile", metavar="ID", help="use a saved API model inside Codex")
    parser.add_argument("--reasoning-effort", metavar="LEVEL", help="override the saved model's reasoning effort")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("check", help="verify Codex MCP registration without calling a model")
    chat = modes.add_parser("chat", help="open Codex's interactive CLI")
    chat.add_argument("prompt", nargs="?")
    execute = modes.add_parser("exec", help="run headlessly")
    execute.add_argument("prompt")
    execute.add_argument("--json", action="store_true", help="stream JSONL events")
    execute.add_argument("--output-schema", type=Path)
    native = modes.add_parser("native", help="pass native arguments to Codex (after --)")
    native.add_argument("codex_options", nargs=argparse.REMAINDER)
    settings = modes.add_parser("settings", help="inspect or update workbench preferences")
    settings.add_argument("--set", dest="setting", metavar="KEY=VALUE")
    models = modes.add_parser("models", help="list, discover, or test saved model connections")
    models.add_argument("action", nargs="?", choices=("list", "discover", "test"), default="list")
    models.add_argument("profile_id", nargs="?")
    storage = modes.add_parser("storage", help="inspect or relocate the workbench database")
    storage.add_argument("action", nargs="?", choices=("show", "move"), default="show")
    storage.add_argument("path", nargs="?", help="absolute destination database path")
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("端口须为 1 至 65535 的整数")
    if options.model_profile and options.mode not in ("chat", "exec", "native"):
        parser.error("--model-profile 用于 chat、exec 或 native")
    if options.reasoning_effort is not None and not options.model_profile:
        parser.error("--reasoning-effort 需要 --model-profile")
    if options.mode == "settings" and options.setting and "=" not in options.setting:
        parser.error("--set 需要 KEY=VALUE")
    if options.mode == "models" and options.action != "list" and not options.profile_id:
        parser.error("请传入已保存的连接 ID")
    if options.mode == "storage" and options.action == "move" and not options.path:
        parser.error("请传入新数据库文件的绝对路径")
    if options.mode in ("settings", "models", "storage"):
        try:
            with backend(options.port, options.attach):
                print(json.dumps(inspect_config(options), ensure_ascii=False, indent=2))
            return 0
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    executable = shutil.which("codex")
    if not executable:
        parser.error("PATH 中未找到 codex")
    args = codex_args(executable, options.port)
    if options.mode == "check":
        try:
            probe = subprocess.run(args + ["mcp", "list", "--json"], capture_output=True, text=True,
                                   cwd=ROOT, env=codex_environment())
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if probe.returncode:
            print(probe.stderr, file=sys.stderr)
            return probe.returncode
        servers = json.loads(probe.stdout)
        registered = any(
            server.get("name") == "industplatform"
            and server.get("enabled")
            and server.get("transport", {}).get("cwd") == str(ROOT)
            and str(ROOT / "run_mcp.py") in server.get("transport", {}).get("args", [])
            for server in servers
        )
        print(json.dumps({"industplatform_mcp_registered": registered}, ensure_ascii=False))
        return int(not registered)
    extra = []
    if options.mode == "native":
        extra = options.codex_options
        if extra[:1] == ["--"]:
            extra = extra[1:]
    elif options.mode == "exec":
        extra.append("exec")
        if options.json:
            extra.append("--json")
        if options.output_schema:
            extra.extend(["--output-schema", str(options.output_schema.resolve())])
    if getattr(options, "prompt", None):
        extra.extend(["--", options.prompt])
    try:
        with backend(options.port, options.attach):
            return run_configured_codex(executable, options, extra)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

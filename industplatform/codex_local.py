"""Reuse the local Codex login through App Server's account protocol."""

import json
import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .codex_runtime import ROOT, prepare_codex_state


logger = logging.getLogger(__name__)
SUBSCRIPTION = "codex_subscription"
API_GATEWAY = "api_gateway"
MODES = (SUBSCRIPTION, API_GATEWAY)
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}\Z")
EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z")
CHATGPT_BASE = "https://chatgpt.com/backend-api/"
SUBSCRIPTION_RESPONSES_BASE = CHATGPT_BASE + "codex"
ACCOUNT_READ = {"refreshToken": False}
CATALOG_SOURCE = "app-server model/list"
MIN_GPT_62_CLI_VERSION = "0.166.0"
VERSION_REPORT = re.compile(
    r"\bcodex(?:-cli)?[ \t]+v?([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)(?:\s|$)"
)
CLIENT_INFO = {"name": "industplatform", "title": "industplatform", "version": "0.1.0"}
INITIALIZE = {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}}
STATUS_MESSAGES = {
    "unchecked": "尚未检测本机 Codex。",
    "cli_missing": "未检测到 Codex CLI。安装后可直接检测桌面端已有登录。",
    "unauthenticated": "未检测到 ChatGPT 登录。请在 CLI 或桌面端登录后重新检测。",
    "conflict_apikey": "本机使用 API Key 登录。请在 CLI 或桌面端切换为 ChatGPT 登录，也可使用 API 模型网关。",
    "probe_failed": "本机 Codex 检测失败，请检查 CLI 版本和运行环境后重试。",
}
NATIVE_SETTINGS = {
    "model_provider": "openai",
    # The explicit provider URL takes precedence over the login-based default.
    "openai_base_url": SUBSCRIPTION_RESPONSES_BASE,
    "chatgpt_base_url": CHATGPT_BASE,
    "history.persistence": "none",
    "notify": [],
    "analytics.enabled": False,
    "features.apps": False,
    "features.plugins": False,
    "features.plugin_discovery_prefetch": False,
    "features.remote_plugin": False,
    "features.hooks": False,
    "features.computer_use": False,
    "features.browser_use": False,
    "features.goals": False,
    "features.shell_snapshot": False,
    "features.skill_mcp_dependency_install": False,
    "features.skip_host_skill_discovery": True,
    "web_search": "disabled",
}


class LocalCodexError(RuntimeError):
    """An operator-facing failure without any credential or RPC payload."""


class LocalCodexAuthRequired(LocalCodexError):
    """The native catalog or account request needs a ChatGPT login."""


@dataclass(frozen=True)
class CodexInstallation:
    path: str
    version: str


def _candidate_paths(home, name):
    paths = [Path(directory).expanduser() / name for directory in os.get_exec_path() if directory]
    paths.extend((home / ".local" / "bin" / name, home / ".npm-global" / "bin" / name))
    for prefix in (os.environ.get("HOMEBREW_PREFIX"), "/opt/homebrew", "/usr/local", "/home/linuxbrew/.linuxbrew"):
        if prefix and os.name != "nt":
            brew = Path(prefix)
            paths.append(brew / "bin" / name)
            paths.extend(sorted(brew.glob("Caskroom/codex/*/codex-*-apple-darwin")))
    if prefix := os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix"):
        paths.append(Path(prefix) / ("bin" if os.name != "nt" else "") / name)
    paths.extend(sorted(home.glob(".nvm/versions/node/*/bin/" + name), reverse=True))
    packages = native_home() / "packages"
    standalone = packages / "standalone"
    paths.extend((standalone / name, standalone / "bin" / name))
    for pattern in ("standalone/*/bin/", "standalone/*/", "app-server-daemon/releases/*/bin/"):
        paths.extend(sorted(packages.glob(pattern + name), reverse=True))
    if os.name == "nt":
        for directory in ("APPDATA", "ProgramFiles", "LOCALAPPDATA"):
            if location := os.environ.get(directory):
                root = Path(location)
                paths.extend(sorted(root.glob("npm/node_modules/@openai/codex/node_modules/@openai/"
                                              "codex-win32-*/vendor/*/codex/" + name)))
                paths.extend((root / "OpenAI" / "ChatGPT" / "resources" / name,
                              root / "Programs" / "ChatGPT" / "resources" / name,
                              root / "Programs" / "Codex" / "resources" / name))
                paths.extend(sorted(root.glob("OpenAI/Codex/bin/*/" + name), reverse=True))
    else:
        for app_directory in (Path("/Applications"), home / "Applications"):
            for app_name in ("ChatGPT.app", "Codex.app"):
                resources = app_directory / app_name / "Contents" / "Resources"
                paths.extend((resources / "codex-cli/CodexCLI.app/Contents/MacOS/codex",
                              resources / "codex-cli" / "bin" / "codex",
                              resources / "codex"))
    return paths


def _version_priority(version: str):
    release, _, prerelease = version.partition("+")[0].partition("-")
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split(".") if part)
    return tuple(map(int, release.split("."))), not prerelease, identifiers


def find_binary(configured: str = "", home: Optional[Path] = None) -> Optional[CodexInstallation]:
    """Resolve the pinned path or the highest installed CLI version."""
    name = "codex.exe" if os.name == "nt" else "codex"
    paths = [Path(configured).expanduser()] if configured else _candidate_paths(home or Path.home(), name)
    candidates: dict[Path, Path] = {}
    for path in paths:
        try:
            if path.is_absolute() and (os.name != "nt" or path.suffix.lower() == ".exe") and path.is_file() and os.access(path, os.X_OK):
                candidates.setdefault(path.resolve(), path)
        except (OSError, RuntimeError):
            continue
    if not candidates:
        if configured:
            raise LocalCodexError("指定的 Codex CLI 路径不可执行。")
        return None
    versions = []
    with state_directory() as state:
        env = native_environment(state)
        for path in candidates.values():
            try:
                report = subprocess.run([str(path), "--version"], env=env, cwd=ROOT, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, timeout=4, check=True)
            except (OSError, subprocess.SubprocessError):
                continue
            match = VERSION_REPORT.search(report.stdout.decode("utf-8", errors="replace"))
            if match:
                versions.append(CodexInstallation(str(path), match[1]))
    if not versions:
        raise LocalCodexError("未能读取可用的 Codex CLI 版本号。")
    return max(versions, key=lambda install: _version_priority(install.version))


def native_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def native_environment(state):
    reserved = {"OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "CODEX_HOME",
                "CODEX_SQLITE_HOME", "CODEX_AUTH", "BASH_ENV", "ENV", "ZDOTDIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() not in reserved
           and not key.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
           and (not key.upper().startswith("CODEX_") or key.upper() == "CODEX_CA_CERTIFICATE")}
    env.update(CODEX_HOME=str(native_home()), CODEX_SQLITE_HOME=str(state))
    return env


@contextmanager
def state_directory():
    with tempfile.TemporaryDirectory(dir=prepare_codex_state(), prefix="subscription-") as directory:
        yield Path(directory)


def account_status(response):
    account = response.get("account")
    account = account if isinstance(account, dict) else {}
    token_mode = response.get("authMode")
    if token_mode == "apikey" or account.get("type") in ("apiKey", "apikey"):
        return {"status": "conflict_apikey"}
    if token_mode == "chatgpt" or (token_mode is None and account.get("type") == "chatgpt"):
        return {"status": "ready_subscription",
                **{public: account[key][:160] for public, key in (("email", "email"), ("plan", "planType"))
                   if isinstance(account.get(key), str)}}
    return {"status": "unauthenticated"}


def status_message(status):
    message = STATUS_MESSAGES.get(status, "")
    if status == "unauthenticated" and ("WSL_INTEROP" in os.environ or "WSL_DISTRO_NAME" in os.environ):
        message += " WSL 与 Windows 桌面端通常使用不同的配置目录，可在 WSL 中执行 codex login。"
    return message


def _requires_login(error):
    if not isinstance(error, dict):
        return False
    data = error.get("data")
    fields = ("code", "type", "kind", "reason", "message", "error", "status")
    markers = [error.get(key) for key in fields]
    markers.extend((data.get(key) for key in fields) if isinstance(data, dict) else (data,))
    return any(value == 401 or (isinstance(value, str) and re.search(
        r"\b(?:auth(?:entication)?[\s_-]+required|login[\s_-]+required|unauthenticated|unauthorized|not logged in)\b",
        value, re.IGNORECASE,
    )) for value in markers)


def send_rpc(process, payload):
    process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    process.stdin.flush()


def stop_process(process):
    if process.poll() is None:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


class _AccountSession:
    def __init__(self, process):
        self.process = process
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=32)
        self.closed = threading.Event()
        self.identity = 0
        self.reader = threading.Thread(target=self.read, name="indust-codex-account", daemon=True)
        self.reader.start()

    def read(self):
        try:
            for line in iter(lambda: self.process.stdout.readline(1024 * 1024 + 1), b""):
                if len(line) > 1024 * 1024:
                    break
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                while not self.closed.is_set():
                    try:
                        self.messages.put(event, timeout=.1)
                        break
                    except queue.Full:
                        pass
                if self.closed.is_set():
                    return
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            self.closed.set()

    def receive(self, match, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.messages.get(timeout=.2)
            except queue.Empty:
                if self.closed.is_set():
                    break
                continue
            if match(event):
                return event
            if "id" in event and "method" in event:
                send_rpc(self.process, {"id": event["id"], "error": {"code": -32601, "message": "Unsupported request"}})
        raise LocalCodexError("本机 Codex 未返回状态，请重新检测。")

    def call(self, method, params):
        self.identity += 1
        identity = self.identity
        send_rpc(self.process, {"id": identity, "method": method, "params": params})
        event = self.receive(lambda row: row.get("id") == identity and "method" not in row, 8)
        if method == "model/list" and logger.isEnabledFor(logging.DEBUG):
            logger.debug("model/list %s", json.dumps(event, ensure_ascii=False))
        error = event.get("error")
        if method in ("account/read", "model/list") and _requires_login(error):
            raise LocalCodexAuthRequired(status_message("unauthenticated"))
        if isinstance(error, dict) and error.get("code") == -32601:
            raise LocalCodexError("当前 Codex CLI 未提供 " + method + "，请更新 CLI 后重试。")
        result = event.get("result")
        if not isinstance(result, dict):
            raise LocalCodexError("本机 Codex 的 " + method + " 请求失败。")
        return result


@contextmanager
def account_session(binary, *, client_name=CLIENT_INFO["name"]):
    from run_codex import config_args

    with state_directory() as state:
        settings = {**NATIVE_SETTINGS, "sqlite_home": str(state), "log_dir": str(state / "log")}
        args = [binary, "--no-daemon", "-C", str(ROOT), *config_args(settings), "app-server", "--listen", "stdio://"]
        with subprocess.Popen(args, cwd=ROOT, env=native_environment(state), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt") as process:
            session = _AccountSession(process)
            try:
                initial = {**INITIALIZE, "clientInfo": {**CLIENT_INFO, "name": client_name}}
                session.call("initialize", initial)
                send_rpc(process, {"method": "initialized"})
                yield session
            finally:
                session.closed.set()
                stop_process(process)
                session.reader.join(timeout=1)


def read_account(session):
    try:
        return account_status(session.call("account/read", ACCOUNT_READ))
    except LocalCodexAuthRequired:
        return {"status": "unauthenticated"}


def model_catalog_rows(session, *, include_hidden=False):
    rows: list[dict[str, Any]] = []
    cursor = None
    seen: set[str] = set()
    while True:
        result = session.call("model/list", {"cursor": cursor, "limit": 100, "includeHidden": include_hidden})
        page = result.get("data")
        if not isinstance(page, list):
            raise LocalCodexError("订阅模型目录响应格式无效。")
        rows.extend(row for row in page if isinstance(row, dict) and (include_hidden or row.get("hidden") is not True))
        cursor = result.get("nextCursor")
        if cursor is None or cursor == "":
            return rows
        if not isinstance(cursor, str) or cursor in seen:
            raise LocalCodexError("订阅模型目录分页未完成。")
        seen.add(cursor)


def _models(rows):
    models: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = row.get("id") or row.get("model")
        slug = row.get("model") or identity
        if not all(isinstance(value, str) and MODEL_ID.fullmatch(value) for value in (identity, slug)):
            continue
        options = row.get("supportedReasoningEfforts")
        levels = [option.get("reasoningEffort") if isinstance(option, dict) else option
                  for option in (options if isinstance(options, list) else [])]
        efforts = list(dict.fromkeys(level for level in levels if isinstance(level, str) and EFFORT.fullmatch(level)))
        name = row.get("displayName")
        model = {"id": identity, "model": slug, "name": name if isinstance(name, str) and name else slug,
                 "reasoning_efforts": efforts, "is_default": row.get("isDefault") is True}
        default = row.get("defaultReasoningEffort")
        if isinstance(default, str) and EFFORT.fullmatch(default):
            model["default_effort"] = default
        models.setdefault(identity, model)
    return list(models.values())


def _settings(session, *, project=True):
    config = session.call("config/read", {"cwd": str(ROOT)} if project else {}).get("config")
    if not isinstance(config, dict):
        raise LocalCodexError(STATUS_MESSAGES["probe_failed"])
    return config


def _cli_login_status(binary):
    from run_codex import config_args

    try:
        with state_directory() as state:
            settings = {**NATIVE_SETTINGS, "sqlite_home": str(state), "log_dir": str(state / "log")}
            result = subprocess.run([binary, "--no-daemon", *config_args(settings), "login", "status"],
                                    cwd=ROOT, env=native_environment(state), capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "probe_failed"
    diagnostic = (result.stdout + result.stderr).lower()
    if b"not logged in" in diagnostic:
        return "unauthenticated"
    if b"logged in using an api key" in diagnostic:
        return "conflict_apikey"
    return "probe_failed"


def active_mcp_servers(config):
    entries = config.get("mcp_servers") or {}
    if not isinstance(entries, dict) or any(not isinstance(value, dict) for value in entries.values()):
        raise LocalCodexError("无法确认本机 Codex 的 MCP 配置。")
    return {name for name, value in entries.items() if value.get("enabled") is not False}


@dataclass
class NativeProbe:
    status: str
    binary: str = ""
    version: str = ""
    account: dict = field(default_factory=dict)
    models: list = field(default_factory=list)
    mcp_servers: Optional[tuple[str, ...]] = None
    project_mcp_transports: dict = field(default_factory=dict)
    catalog_error: str = ""

    def public(self):
        needs_upgrade = self.version and _version_priority(self.version) < _version_priority(MIN_GPT_62_CLI_VERSION)
        result = {"status": self.status, "message": status_message(self.status), "version": self.version,
                  "binary_path": self.binary, "catalog_source": CATALOG_SOURCE if self.binary else "",
                  "version_notice": "GPT-6.2 需要 Codex CLI v" + MIN_GPT_62_CLI_VERSION + " 或更新版本。" if needs_upgrade else "",
                  "models": self.models or [], "catalog_error": self.catalog_error}
        if self.account:
            result.update((key, self.account[key]) for key in ("email", "plan") if key in self.account)
        return result


class CodexLocalDetector:
    def __init__(self):
        self.lock = threading.Lock()
        self.probe_lock = threading.Lock()
        self.cache = {}
        self.login = None

    @staticmethod
    def key(path):
        return (str(native_home()), path)

    def cached(self, path=""):
        key = self.key(path)
        with self.lock:
            probe = self.cache.get(key, (0, NativeProbe("unchecked")))[1]
            return {**probe.public(), "login_pending": bool(self.login and self.login[0] == key)}

    def detect(self, path="", *, refresh=False):
        key = self.key(path)
        with self.probe_lock:
            with self.lock:
                previous = self.cache.get(key)
            if not refresh and previous and time.monotonic() - previous[0] < 45:
                return previous[1]
            # The preceding probe has already closed its process before releasing this lock.
            probe = self._detect(path)
            with self.lock:
                self.cache = {key: (time.monotonic(), probe)}
            return probe

    @staticmethod
    def _detect(path):
        try:
            install = find_binary(path)
        except (LocalCodexError, OSError) as exc:
            message = str(exc) if isinstance(exc, LocalCodexError) else status_message("probe_failed")
            return NativeProbe("probe_failed", catalog_error=message)
        if install is None:
            return NativeProbe("cli_missing")
        binary, version = install.path, install.version
        try:
            with account_session(binary) as session:
                account = read_account(session)
                probe = NativeProbe(account["status"], binary, version, account)
                if probe.status != "ready_subscription":
                    return probe
                try:
                    probe.models = _models(model_catalog_rows(session))
                except LocalCodexAuthRequired:
                    return NativeProbe("unauthenticated", binary, version)
                except (LocalCodexError, OSError):
                    probe.catalog_error = "读取本机订阅模型目录失败，请重新检测。"
                else:
                    if not probe.models:
                        probe.catalog_error = "本机 Codex 未返回可选订阅模型。"
                    else:
                        CodexLocalDetector._read_mcp_settings(session, probe)
                return probe
        except (LocalCodexError, OSError, subprocess.SubprocessError, AttributeError, TypeError, ValueError):
            return NativeProbe(_cli_login_status(binary), binary, version)

    @staticmethod
    def _read_mcp_settings(session, probe):
        try:
            user = _settings(session, project=False)
            project = _settings(session)
            active = active_mcp_servers(user) | active_mcp_servers(project)
            # Account RPCs load user settings before the project layer.
            project_servers = project.get("mcp_servers") or {}
            probe.project_mcp_transports = {
                name: "url" if "url" in project_servers[name] else "command"
                for name in active - (user.get("mcp_servers") or {}).keys()
            }
            probe.mcp_servers = tuple(sorted(active))
        except (LocalCodexError, OSError, KeyError, TypeError, ValueError):
            # Catalog reads and execution configuration have separate failure conditions.
            pass

    def begin_login(self, path=""):
        probe = self.detect(path, refresh=True)
        if probe.status != "unauthenticated":
            raise LocalCodexError(status_message(probe.status) or "本机已经登录 ChatGPT。")
        key = self.key(path)
        with self.lock:
            if self.login:
                if self.login[0] == key:
                    return self.login[1]
                raise LocalCodexError("已有本机登录正在等待完成。")
            resources = account_session(probe.binary)
            try:
                session = resources.__enter__()
                try:
                    account = read_account(session)
                    if account["status"] == "ready_subscription":
                        try:
                            model_catalog_rows(session)
                        except LocalCodexAuthRequired:
                            pass
                        else:
                            raise LocalCodexError("本机登录已恢复。请重新检测账户。")
                    elif account["status"] != "unauthenticated":
                        raise LocalCodexError("本机登录已变化。请重新检测账户。")
                    result = session.call("account/login/start", {"type": "chatgpt"})
                    url = result.get("authUrl", "")
                    parsed = urlsplit(url)
                    login_id = result.get("loginId")
                    if (result.get("type") != "chatgpt" or parsed.scheme != "https"
                            or parsed.netloc not in ("auth.openai.com", "auth0.openai.com")
                            or not isinstance(login_id, str) or not login_id):
                        raise LocalCodexError("Codex 返回的登录地址不可用。请在 CLI 或桌面端登录。")
                except BaseException:
                    resources.__exit__(None, None, None)
                    raise
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                raise LocalCodexError("无法启动 Codex 登录。请在 CLI 或桌面端登录。") from None
            self.login = (key, url)
            threading.Thread(target=self._finish_login, args=(path, key, resources, session, login_id),
                             name="indust-codex-login", daemon=True).start()
            return url

    def _finish_login(self, path, key, resources, session, login_id):
        try:
            session.receive(lambda row: row.get("method") == "account/login/completed"
                            and (row.get("params") or {}).get("loginId") == login_id, 300)
        except (LocalCodexError, OSError):
            pass
        finally:
            resources.__exit__(None, None, None)
            with self.lock:
                self.cache.pop(key, None)
            self.detect(path, refresh=True)
            with self.lock:
                self.login = None


DETECTOR = CodexLocalDetector()


def select_model(probe, requested, effort, disabled=()):
    if probe.status != "ready_subscription" or probe.catalog_error:
        raise LocalCodexError(probe.catalog_error or status_message(probe.status))
    models = [model for model in probe.models or [] if model["id"] not in disabled]
    if requested:
        selected = next((model for model in models if model["id"] == requested), None) or next(
            (model for model in models if model["model"] == requested), None
        )
    else:
        selected = next((model for model in models if model["is_default"]), models[0] if models else None)
    if not selected:
        raise LocalCodexError("所选订阅模型不可用。请重新检测并选择已启用的模型。")
    if effort and effort not in selected["reasoning_efforts"]:
        raise LocalCodexError("订阅模型不支持所选思考等级。")
    default = selected.get("default_effort")
    return {"id": selected["id"], "model": selected["model"], "name": selected["name"],
            "reasoning_effort": effort or (default if default in selected["reasoning_efforts"] else None)}

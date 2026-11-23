"""Versioned domain verifier drafts and isolated Docker fixture execution."""

import hashlib
import importlib.util
import io
import json
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from contextlib import closing, nullcontext
from pathlib import Path
from uuid import uuid4


LAYERS = ("L1", "L2", "L3", "L4")
IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,220}\Z")
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
MAX_SAMPLE_BYTES = 16 * 1024 * 1024
MAX_SAMPLE_FILES = 128
PORTABLE_PATH = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]+\Z")
WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?\Z", re.I)
LEGACY_TASK = re.compile(r"[0-9a-f]{32}:[\w.-]{1,160}\Z")
DEFAULT = {"strategy": "acceptance_backwards", "image": "", "layers": {key: [] for key in LAYERS},
           "fixtures": {"standard": {}, "incomplete": {}}}


def tools():
    docker = shutil.which("docker")
    docker_ready = False
    if docker:
        try:
            check = subprocess.run([docker, "version", "--format", "{{.Server.Version}}"], timeout=3,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            docker_ready = check.returncode == 0 and bool(check.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return [
        {"label": "Codex App Server", "available": bool(shutil.which("codex")),
         "detail": "检查本机 Codex 可执行文件"},
        {"label": "工业 MCP", "available": True, "detail": "内置公域检索、证据核验和任务工具"},
        {"label": "Playwright", "available": bool(importlib.util.find_spec("playwright")),
         "detail": "检查本机 Python 浏览器工具"},
        {"label": "PDF／OCR", "available": bool(shutil.which("pdftotext") or shutil.which("docling") or shutil.which("mineru")),
         "detail": "检查 pdftotext、Docling 或 MinerU"},
        {"label": "Git", "available": bool(shutil.which("git")), "detail": "检查本机版本历史工具"},
        {"label": "Docker 沙箱", "available": docker_ready, "detail": "检查 Docker 引擎响应"},
    ]


def _domain(db, domain_id):
    if type(domain_id) is not int or not 1 <= domain_id <= 2**53 - 1:
        raise ValueError("请选择工业领域")
    row = db.execute("""SELECT d.name FROM domains d LEFT JOIN domain_library l ON l.domain_id=d.id
        WHERE d.id=? AND coalesce(l.archived,0)=0""", (domain_id,)).fetchone()
    if not row:
        raise ValueError("请选择工业领域")
    return row[0]


def _draft_key(db, store, domain_id, task):
    name = _domain(db, domain_id)
    if not task:
        return f"verifier-draft/{domain_id}"
    from . import library

    if not LEGACY_TASK.fullmatch(task) or not any(row["id"] == task for row in library.task_bindings(store, name)):
        raise ValueError("请选择当前领域内的已登记题目")
    return f"legacy-verifier/{domain_id}/{task}"


def _scoped_task(db, domain_id, task):
    return db.execute("""SELECT 1 FROM benchmark_tasks
        WHERE domain_id=? AND id=? AND state<>'archived'""", (domain_id, task)).fetchone()


def task_revision(db, domain_id, task):
    row = db.execute("""SELECT version,strategy,image,layers_json,fixtures_json,self_tests_json,is_stale
        FROM verifier_specs WHERE domain_id=? AND task_id=? ORDER BY version DESC LIMIT 1""",
        (domain_id, task)).fetchone()
    if not row:
        return None
    version, strategy, image, layers, fixtures, tests, stale = row
    return {"version": version, "spec": {"strategy": strategy, "image": image or "", "layers": json.loads(layers),
            "fixtures": json.loads(fixtures) or DEFAULT["fixtures"]},
            "tests": {entry["variant"]: entry["result"] for entry in json.loads(tests)
                      if isinstance(entry, dict) and "variant" in entry and "result" in entry},
            "is_stale": bool(stale)}


def _target(db, domain_id, task):
    _domain(db, domain_id)
    if not isinstance(task, str) or not _scoped_task(db, domain_id, task):
        raise ValueError("请选择当前领域内的已登记题目")


def read(store, domain_id, task=""):
    if not isinstance(task, str):
        raise ValueError("请选择验证器绑定")
    with closing(store.connect()) as db:
        if task and _scoped_task(db, domain_id, task):
            _target(db, domain_id, task)
            saved = task_revision(db, domain_id, task)
        else:
            key = _draft_key(db, store, domain_id, task)
            row = db.execute("SELECT value_json FROM global_settings WHERE key=?", (key,)).fetchone()
            saved = json.loads(row[0]) if row else None
    return saved or {"version": 0, "spec": DEFAULT, "tests": {}}


def sample_path(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 240:
        raise ValueError("样本路径无效")
    parts = name.split("/")
    if len(parts) > 12 or any(not PORTABLE_PATH.fullmatch(part) or part in (".", "..") or WINDOWS_DEVICE.fullmatch(part)
                               or part.endswith((" ", ".")) for part in parts):
        raise ValueError("样本须使用安全的相对文件路径")
    return name


def _sample_ref(ref):
    if not isinstance(ref, dict) or ref.keys() != {"sha256", "bytes", "archive"} or (
        not isinstance(ref["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"])
        or type(ref["bytes"]) is not int or not 0 <= ref["bytes"] <= MAX_SAMPLE_BYTES
        or type(ref["archive"]) is not bool
    ):
        raise ValueError("样本附件引用无效")
    return ref


def _archive_files(data):
    output = {}
    size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_SAMPLE_FILES:
                raise ValueError("ZIP 最多包含 128 个条目")
            for item in entries:
                name = sample_path(item.filename.rstrip("/") if item.is_dir() else item.filename)
                kind = stat.S_IFMT(item.external_attr >> 16)
                if kind not in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG):
                    raise ValueError("ZIP 样本只接受普通文件和目录")
                if item.is_dir():
                    continue
                if name in output or size + item.file_size > MAX_SAMPLE_BYTES:
                    raise ValueError("ZIP 含重复路径，或解包后超过 16 MiB")
                with archive.open(item) as content:
                    output[name] = content.read(item.file_size + 1)
                if len(output[name]) != item.file_size:
                    raise ValueError("ZIP 样本长度不一致")
                size += item.file_size
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError):
        raise ValueError("无法读取 ZIP 样本") from None
    if not output:
        raise ValueError("ZIP 中没有样本文件")
    _check_file_paths(output)
    return output


def _check_file_paths(files):
    normalized = set()
    directories = set()
    for name in files:
        parts = sample_path(name).casefold().split("/")
        key = "/".join(parts)
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if key in normalized or key in directories or parents & normalized:
            raise ValueError("样本文件路径冲突")
        normalized.add(key)
        directories |= parents


def upload_sample(store, domain_id, name, data):
    sample_path(name)
    if len(data) > MAX_SAMPLE_BYTES:
        raise ValueError("请选择 16 MiB 以内的样本文件")
    archive = name.casefold().endswith(".zip")
    if archive:
        _archive_files(data)
    digest = hashlib.sha256(data).hexdigest()
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _domain(db, domain_id)
        db.execute("INSERT OR IGNORE INTO verifier_samples VALUES (?,?,?)", (domain_id, digest, data))
    return {"sha256": digest, "bytes": len(data), "archive": archive}


def _mounted_files(db, domain_id, fixtures):
    files = {}
    total = 0
    for name, ref in fixtures.items():
        if isinstance(ref, str):
            additions = {name: ref.encode()}
        else:
            if db is None:
                raise ValueError("样本附件需要在本工作台加载")
            blob = db.execute("""SELECT content FROM verifier_samples
                WHERE domain_id=? AND sha256=? AND length(content)=?""",
                (domain_id, ref["sha256"], ref["bytes"])).fetchone()
            if not blob:
                raise ValueError("样本附件已不可用，请重新挂载")
            data = blob[0]
            if hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise ValueError("样本附件校验失败，请重新挂载")
            additions = _archive_files(data) if ref["archive"] else {name: data}
        total += sum(map(len, additions.values()))
        if files.keys() & additions.keys() or len(files) + len(additions) > MAX_SAMPLE_FILES or total > MAX_SAMPLE_BYTES:
            raise ValueError("每组样本最多 128 个文件和 16 MiB，且路径须唯一")
        files.update(additions)
    _check_file_paths(files)
    return files


def validated(value):
    if not isinstance(value, dict) or set(value) != {"image", "strategy", "layers", "fixtures"}:
        raise ValueError("验证配置须包含策略、镜像、四层规则和两组样本")
    image, layers, fixtures = value["image"], value["layers"], value["fixtures"]
    if value["strategy"] not in ("acceptance_backwards", "progressive"):
        raise ValueError("请选择验收策略")
    if not isinstance(image, str) or image and not IMAGE.fullmatch(image):
        raise ValueError("镜像名称无效")
    if not isinstance(layers, dict) or set(layers) != set(LAYERS):
        raise ValueError("规则应覆盖 L1–L4")
    for entries in layers.values():
        if not isinstance(entries, list) or len(entries) > 8:
            raise ValueError("每层最多配置 8 条验证规则")
        for row in entries:
            if (not isinstance(row, dict) or not isinstance(row.get("title"), str)
                    or not 1 <= len(row["title"].strip()) <= 100):
                raise ValueError("每条规则需要标题")
            fields = row.keys() - {"reference_citations"}
            if fields == {"title", "language", "script"}:
                if (row["language"] not in ("python", "shell") or not isinstance(row["script"], str)
                        or "\x00" in row["script"] or not 1 <= len(row["script"].strip()) <= 12000):
                    raise ValueError("验证脚本须为 12000 字符以内的 Python 或 Shell")
            elif (fields != {"title", "argv"} or not isinstance(row["argv"], list)
                  or not 1 <= len(row["argv"]) <= 16 or any(not isinstance(item, str)
                  or "\x00" in item or not 1 <= len(item) <= 600 for item in row["argv"])):
                raise ValueError("规则须声明非空 argv 数组或 Python／Shell 脚本")
            if "reference_citations" in row:
                from .engineering_quality import entries, object_fields
                for citation in entries(row["reference_citations"], "验证规则条款引用", 16):
                    object_fields(citation, ("reference_id", "clause_id", "applies_to"), label="验证规则条款引用")
    if not isinstance(fixtures, dict) or set(fixtures) != {"standard", "incomplete"}:
        raise ValueError("请配置标准样本与故意残缺样本")
    for files in fixtures.values():
        if not isinstance(files, dict) or len(files) > MAX_SAMPLE_FILES:
            raise ValueError("每组试跑样本最多 128 个文件")
        _check_file_paths(files)
        for entry in files.values():
            if not isinstance(entry, str):
                _sample_ref(entry)
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > 112 * 1024:
        raise ValueError("验证配置不得超过 112 KiB")
    return value


def save_task_revision(db, domain_id, task, version, value):
    _target(db, domain_id, task)
    if any("reference_citations" in rule for rows in value["layers"].values() for rule in rows):
        from .engineering_quality import eligible_references, resolve_citations
        node = db.execute("SELECT node_id FROM benchmark_tasks WHERE domain_id=? AND id=?", (domain_id, task)).fetchone()[0]
        refs = eligible_references(db, domain_id, node)
        for rows in value["layers"].values():
            for rule in rows:
                if "reference_citations" in rule:
                    resolve_citations(rule["reference_citations"], refs)
    current = task_revision(db, domain_id, task)
    if type(version) is not int or version != (current["version"] if current else 0):
        raise ValueError("验证器版本已改变，请重新加载")
    if current and current["spec"] == value and not current["is_stale"]:
        return current
    db.execute("""INSERT INTO verifier_specs(domain_id,task_id,version,strategy,image,layers_json,fixtures_json,self_tests_json)
                  VALUES(?,?,?,?,?,?,?,'[]')""",
        (domain_id, task, version + 1, value["strategy"], value["image"],
         json.dumps(value["layers"], ensure_ascii=False), json.dumps(value["fixtures"], ensure_ascii=False)))
    return {"version": version + 1, "spec": value, "tests": {}, "is_stale": False}


def save(store, domain_id, version, value, task=""):
    value = validated(value)
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _domain(db, domain_id)
        if not isinstance(task, str):
            raise ValueError("请选择验证器绑定")
        for fixtures in value["fixtures"].values():
            _mounted_files(db, domain_id, fixtures)
        if task and _scoped_task(db, domain_id, task):
            return save_task_revision(db, domain_id, task, version, value)
        if any("reference_citations" in rule for rows in value["layers"].values() for rule in rows):
            raise ValueError("请先将验证器绑定到当前领域采用结构化条款的题目")
        key = _draft_key(db, store, domain_id, task)
        existing = db.execute("SELECT value_json FROM global_settings WHERE key=?", (key,)).fetchone()
        actual = json.loads(existing[0])["version"] if existing else 0
        if type(version) is not int or version != actual:
            raise ValueError("配置已更新，请重新加载再保存")
        entry = {"version": actual + 1, "spec": value, "tests": {}}
        db.execute("""INSERT INTO global_settings VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json""",
                   (key, json.dumps(entry, ensure_ascii=False)))
        return entry


def _run_check(docker, image, directory, check):
    name = "indust-verifier-" + uuid4().hex
    argv = check.get("argv")
    if argv is None:
        argv = ["python3" if check["language"] == "python" else "/bin/sh", "-c", check["script"]]
    args = [docker, "run", "--name", name, "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--cpus", "1",
            "--memory", "512m", "--pids-limit", "32", "--user", "65534:65534",
            "--mount", f"type=bind,src={directory},dst=/work,readonly", "--workdir", "/work",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m", "--entrypoint", argv[0], image, *argv[1:]]
    started = time.monotonic()
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=12)
        if result.returncode in (125, 126, 127):
            raise ValueError("Docker 无法启动镜像或验证工具，请检查镜像与命令")
    except subprocess.TimeoutExpired:
        subprocess.run([docker, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=3, check=False)
        raise ValueError("验证规则执行超时") from None
    return {"title": check["title"], "passed": result.returncode == 0,
            "exit_code": result.returncode, "latency_ms": round((time.monotonic() - started) * 1000)}


def run_checks(spec, files, *, stop=None):
    validated(spec)
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("本机未安装 Docker CLI")
    if not spec["image"] or any(not spec["layers"][layer] for layer in LAYERS):
        raise ValueError("请保存本机已安装的镜像及 L1–L4 检查规则")
    if sum(len(rules) for rules in spec["layers"].values()) > 12:
        raise ValueError("单次试跑最多运行 12 条规则")
    _check_file_paths(files)
    contents = {name: value.encode("utf-8") if isinstance(value, str) else value for name, value in files.items()}
    if (len(contents) > MAX_SAMPLE_FILES or any(not isinstance(body, bytes) for body in contents.values())
            or sum(map(len, contents.values())) > MAX_SAMPLE_BYTES):
        raise ValueError("试跑文件格式或长度无效")
    rows = {}
    with tempfile.TemporaryDirectory(prefix="indust-verifier-") as folder:
        directory = Path(folder)
        directory.chmod(0o755)
        for name, body in contents.items():
            path = directory / name
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.write_bytes(body)
            path.chmod(0o644)
        for layer, checks in spec["layers"].items():
            rows[layer] = []
            for check in checks:
                if stop and stop.is_set():
                    raise ValueError("验证试跑已中断")
                rows[layer].append(_run_check(docker, spec["image"], folder, check))
    return {"checks": rows, "passed": all(row["passed"] for group in rows.values() for row in group)}


def run_fixture(spec, variant, *, stop=None, store=None, domain_id=None):
    if variant not in ("standard", "incomplete"):
        raise ValueError("请选择试跑样本")
    validated(spec)
    with (closing(store.connect()) if store else nullcontext()) as db:
        fixtures = {key: _mounted_files(db, domain_id, files) for key, files in spec["fixtures"].items()}
    if not fixtures["standard"] or fixtures["standard"] == fixtures["incomplete"]:
        raise ValueError("请提供一组标准样本和与其不同的残缺样本")
    outcome = run_checks(spec, fixtures[variant], stop=stop)
    return {**outcome, "expected": outcome["passed"] if variant == "standard" else not outcome["passed"]}


def record_task_trial(db, domain_id, task, saved, variant, observation):
    current = task_revision(db, domain_id, task)
    if not current or current["version"] != saved["version"] or current["is_stale"]:
        raise ValueError("试跑期间目标版本已变更")
    current["tests"][variant] = observation
    records = [{"variant": key, "result": result} for key, result in current["tests"].items()]
    db.execute("UPDATE verifier_specs SET self_tests_json=? WHERE domain_id=? AND task_id=? AND version=?",
               (json.dumps(records, ensure_ascii=False), domain_id, task, saved["version"]))


def trial(store, domain_id, variant, task="", version=None):
    saved = read(store, domain_id, task)
    if saved.get("is_stale") or (version is not None and saved["version"] != version):
        raise ValueError("请先加载并保存要试跑的验证器版本")
    observation = run_fixture(saved["spec"], variant, store=store, domain_id=domain_id)
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if task and _scoped_task(db, domain_id, task):
            _target(db, domain_id, task)
            record_task_trial(db, domain_id, task, saved, variant, observation)
            return observation
        key = _draft_key(db, store, domain_id, task)
        row = db.execute("SELECT value_json FROM global_settings WHERE key=?", (key,)).fetchone()
        current = json.loads(row[0]) if row else {}
        if current.get("version") != saved["version"]:
            raise ValueError("试跑期间配置已有更新")
        current["tests"][variant] = observation
        db.execute("UPDATE global_settings SET value_json=? WHERE key=?", (json.dumps(current, ensure_ascii=False), key))
    return observation


def inspect_image(image):
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("请填写有效的 Docker 镜像")
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("本机未安装 Docker CLI")
    try:
        result = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", image],
                                capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("无法连接本机 Docker 引擎") from None
    if result.returncode or not result.stdout.strip():
        raise ValueError("镜像尚未安装，或本机 Docker 引擎未启动")
    return {"id": result.stdout.strip()}

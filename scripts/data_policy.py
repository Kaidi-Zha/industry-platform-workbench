"""Shared hygiene, branch isolation and monotonic data checks."""

import re
from pathlib import PurePosixPath

from industplatform import shared_metadata as metadata, taxonomy

FROZEN_SCHEMAS = {taxonomy.REGISTRY_PATH, taxonomy.LIFECYCLE_PATH}
BRANCH = re.compile(r"(data|feat|fix|chore)/[A-Za-z0-9][A-Za-z0-9._/-]*")
RAW = frozenset((".pdf", ".step", ".stp", ".iges", ".igs", ".stl", ".dwg", ".dxf", ".ger", ".gbr",
                 ".gerber", ".drl", ".xln", ".gbl", ".gtl", ".gko", ".gts", ".gbs", ".gm1", ".g2l",
                 ".kicad_pcb", ".kicad_sch", ".zip", ".tar", ".gz", ".bz2", ".xz", ".zst", ".zstd", ".7z", ".rar",
                 ".har", ".mhtml", ".wacz", ".warc", ".parquet"))
LOCAL_SEGMENTS = frozenset((".staging", ".local", "__pycache__", "node_modules", ".venv", ".pytest_cache",
                            ".mypy_cache", ".ruff_cache", "logs", "snapshots", "raw_captures",
                            ".ssh", ".aws", ".secrets"))
CREDENTIAL_FILES = frozenset(("auth.json", "credentials.json", "secrets.json", ".netrc",
                              "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"))
KEY_SUFFIXES = frozenset((".key", ".pem", ".p12", ".pfx", ".ppk"))
SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    rb"|(?<![A-Za-z0-9_-])(?:sk-(?:proj-|ant-api\d+-|svcacct-)?[A-Za-z0-9_-]{28,}"
    rb"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AIza[0-9A-Za-z_-]{35}"
    rb"|(?:AKIA|ASIA)[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,})(?![A-Za-z0-9_-])"
    rb"|(?:api[_-]?key|client[_-]?secret|access[_-]?token|secret[_-]?access[_-]?key)"
    rb"""["']?\s*[:=]\s*["'][A-Za-z0-9_+/@.=:-]{30,}["']""", re.I)
HTML = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*(?:<!doctype\s+html\b|<html[\s>])", re.I)
MAINTENANCE = re.compile(r"(?:\.(?:github|githooks|agents|codex)/.*|"
                         r"(?:scripts|schemas|docs|tests)/.*|"
                         r"\.gitignore|AGENTS\.md|README\.md|TODO\.md|"
                         r"pyproject\.toml|(?:requirements(?:-[a-z-]+)?\.txt))")


def branch_kind(branch):
    match = BRANCH.fullmatch(branch)
    if not match:
        raise ValueError("工作分支须使用 data/、feat/、fix/ 或 chore/")
    return match[1]


def forbidden_path(path):
    lower = path.casefold()
    parts = lower.split("/")
    name = parts[-1]
    suffix = PurePosixPath(name).suffix
    if "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        return "路径含有不安全的字符"
    if (LOCAL_SEGMENTS.intersection(parts) or any(part.endswith(".credentials") for part in parts)
            or any(part.startswith((".venv-", ".data-move-", ".data-location-", ".write-")) for part in parts)
            or lower.startswith((".codex/log/", ".codex/sessions/", ".codex/archived_sessions/", "data/local/", "data/raw/"))
            or lower == ".codex/history.jsonl" or re.fullmatch(r"rollout-.*\.jsonl", name)
            or name.startswith(".env") or re.search(r"\.env(?:\.|$)", name)
            or name in CREDENTIAL_FILES or name in (".ds_store", "thumbs.db", "desktop.ini") or suffix in KEY_SUFFIXES
            or re.search(r"(?:\.sqlite[^/]*|\.db(?:-.+)?|-(?:wal|shm)|\.location\.json|\.vault|\.log|\.py[co])$", name)):
        return "本地运行产物或凭据"
    if suffix in RAW:
        return "原始附件、工程文件或压缩包"
    if name.endswith((".html", ".htm")) and not path.startswith("industplatform/static/"):
        return "原始 HTML 页面"
    return None


def inspect_blob(repo, path, entry):
    cause = forbidden_path(path)
    if cause:
        raise ValueError(f"{path} · {cause}")
    if entry.mode not in ("100644", "100755"):
        raise ValueError(f"{path} · 不接受符号链接或 Git 子模块")
    if entry.size >= metadata.MAX_BYTES:
        raise ValueError(f"{path} · 单文件达到 500 KB")
    data = repo.blob(entry)
    check_content(path, data)
    return data


def check_content(path, data):
    if SECRET.search(data):
        raise ValueError(f"{path} · 疑似 API 密钥、Token 或私钥")
    if not path.startswith("industplatform/static/") and HTML.match(data):
        raise ValueError(f"{path} · 疑似整页 HTML")
    if data.startswith((b"%PDF-", b"PK\x03\x04", b"SQLite format 3\0", b"ISO-10303-21;",
                        b"\x1f\x8b", b"\x28\xb5\x2f\xfd", b"7z\xbc\xaf\x27\x1c")):
        raise ValueError(f"{path} · 检出原件、压缩包或 SQLite 文件签名")


def check_ignore(previous, current):
    before, after = previous.decode("utf-8").splitlines(), current.decode("utf-8").splitlines()
    if after[:len(before)] != before or any(line.lstrip().startswith("!") for line in after[len(before):]):
        raise ValueError(".gitignore 仅可在末尾追加忽略规则，禁止移除规则或添加反选")


def check_path(kind, status, path):
    if path.casefold().startswith(metadata.INDEX):
        raise ValueError(f"{path} · 自动索引仅由主分支索引流程更新")
    if kind == "data":
        if path.casefold().startswith("schemas/"):
            raise ValueError(f"{path} · data/ 分支不能修改分类码表或 Schema")
        metadata.path_parts(path)
        if status not in ("A", "M"):
            raise ValueError(f"{path} · 共享记录不能物理删除、重命名或改文件类型")
    elif path.casefold().startswith("data/") or path.casefold() == "data":
        raise ValueError(f"{path} · 数据变更须使用 data/ 批次")
    if kind == "chore" and not MAINTENANCE.fullmatch(path):
        raise ValueError(f"{path} · chore/ 仅用于仓库配置、脚本、测试和文档")


def record_blob(repo, path, entry):
    if entry.mode != "100644":
        raise ValueError(f"{path} · 共享条目须为普通 JSON 文本文件")
    data = inspect_blob(repo, path, entry)
    try:
        return metadata.validate_record(path, data)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{path} · {exc}") from None


def validate_changes(repo, base, head, branch, *, correction=False, history=True):
    """Validate the batch against its starting tree and return its data records."""
    kind = branch_kind(branch)
    paths = repo.changes(base, head)
    before, after = repo.tree(base), repo.tree(head)
    checked = set()
    records = {}
    modified = []
    for status, path in paths:
        check_path(kind, status, path)
        if PurePosixPath(path).name == ".gitignore":
            if status == "D":
                raise ValueError("不能删除根 .gitignore")
            old = repo.blob(before[path]) if path in before else b""
            check_ignore(old, repo.blob(after[path]))
        if status == "D":
            if path in FROZEN_SCHEMAS:
                raise ValueError(f"{path} · 不能删除已固化的分类码表")
            continue
        entry = after[path]
        if kind == "data":
            record = record_blob(repo, path, entry)
            records[path] = record
            if status == "M":
                prior = record_blob(repo, path, before[path])
                if (not correction or record["version"] <= prior["version"]
                        or record["updated_at"] < prior["updated_at"] or not record.get("correction_reason")):
                    raise ValueError(f"{path} · 纠错须声明原因、递增版本并填写 correction_reason")
                modified.append(path)
            elif record["version"] != 1 or record["status"] != "active":
                raise ValueError(f"{path} · 新条目须以 active v1 建立")
        else:
            data = inspect_blob(repo, path, entry)
            if path in FROZEN_SCHEMAS:
                try:
                    taxonomy.check_registry_change(path, data, repo.blob(before[path]) if path in before else None)
                except ValueError as exc:
                    raise ValueError(f"{path} · {exc}") from None
        checked.add((path, entry.oid))
    # Inspect intermediate blobs even when their paths were removed before the PR tip.
    for path, mode, oid in (repo.introduced(base, head) if history else ()):
        if path in before and (before[path].mode, before[path].oid) == (mode, oid):
            continue
        check_path(kind, "M", path)
        if (path, oid) in checked:
            continue
        if mode not in ("100644", "100755"):
                raise ValueError(f"{path} · 批次历史包含非普通文件")
        size = int(repo.git("cat-file", "-s", oid))
        if forbidden_path(path) or size >= metadata.MAX_BYTES:
                raise ValueError(f"{path} · 批次历史包含受限文件")
        check_content(path, repo.git("cat-file", "blob", oid))
        checked.add((path, oid))
    if len({record["task_id"] for record in records.values()}) > 1:
        raise ValueError("每个 data/ 批次必须只包含一次完整采集或纠错任务")
    for path, record in records.items():
        replacement = record.get("superseded_by")
        if replacement:
            expected = path.rsplit("/", 1)[0] + "/" + replacement + ".json"
            if expected not in after or record_blob(repo, expected, after[expected])["status"] != "active":
                raise ValueError(f"{path} · 替代条目必须在同领域同类型存在并处于 active")
    return records, modified

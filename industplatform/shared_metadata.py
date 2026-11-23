"""Versioned metadata shared through Git and projected into the local workbench."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import reference_catalog as catalog, taxonomy
from .frontier import normalized_url

KINDS = ("sources", "literature", "entities", "processes", "tasks")
URL_KINDS = ("sources", "literature")
PREFIX = "data/domains/"
INDEX = "data/_index/"
MAX_BYTES = 500_000
IDENTITY = re.compile(r"[0-9a-f]{24}")
BATCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
PUBLISHED_REF = "refs/remotes/origin/main"
SYNC_DEFAULTS = {"auto_import": True, "audit_ignores": True}
FIELDS = frozenset(("canonical_id", "canonical_key", "domain_id", "kind", "title", "task_id",
                    "version", "status", "updated_at", "payload", *taxonomy.FIELDS))
PRIVATE_PARAMETER = re.compile(
    r"^(x-(?:amz|obs|oss|goog)-.*|.*(?:token|signature|password|secret|credential).*|"
    r"(?:api|access)[_-]?key|auth(?:orization)?|ak|sk|sig|ossaccesskeyid|accesskeyid)$", re.I)
PRIVATE_FIELDS = frozenset(("raw_html", "raw_content", "snapshot_bytes", "api_key", "api_token",
                            "base64", "private_key", "password", "expected_answer"))


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def json_object(data: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("JSON 包含重复字段")
        return result

    def invalid_constant(_):
        raise ValueError("JSON 数值必须有限")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("请使用 UTF-8 JSON，并检查嵌套深度") from exc


def required_text(value: Any, limit: int, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or "\0" in value:
        raise ValueError(f"{label} 须为 1–{limit} 字符")
    value.encode("utf-8")
    return value


def canonical_key(kind: str, key: Any) -> str:
    key = required_text(key, 1600, "实体键").strip()
    if kind in URL_KINDS:
        key = normalized_url(key)
        if any(PRIVATE_PARAMETER.fullmatch(name) for name, _ in parse_qsl(urlsplit(key).query, keep_blank_values=True)):
            raise ValueError("信源须使用不含签名和凭据的公开链接")
        return key
    return " ".join(unicodedata.normalize("NFKC", key).casefold().split())


def canonical_id(domain: str, kind: str, key: str, object_cluster: str) -> str:
    fields = (domain, kind, key) if kind in URL_KINDS else (domain, kind, object_cluster, key)
    return hashlib.sha256("\n".join(fields).encode("utf-8")).hexdigest()[:24]


def record_path(record: dict[str, Any]) -> str:
    return f"{PREFIX}{record['l2_code']}/{record['kind']}/{record['canonical_id']}.json"


def path_parts(path: str) -> tuple[str, str, str]:
    parts = path.split("/")
    if (len(parts) != 5 or parts[:2] != ["data", "domains"]
            or parts[3] not in KINDS or not parts[4].endswith(".json") or not IDENTITY.fullmatch(parts[4][:-5])):
        raise ValueError(f"无效的共享条目路径 {path}")
    return taxonomy.require_l2(parts[2]), parts[3], parts[4][:-5]


def check_payload(payload: Any, depth: int = 0) -> None:
    if depth > 20:
        raise ValueError("payload 嵌套过深")
    if isinstance(payload, dict):
        if PRIVATE_FIELDS.intersection(key.casefold() for key in payload):
            raise ValueError("payload 含有原件、认证信息或私有答案字段")
        for key, value in payload.items():
            key.encode("utf-8")
            if key in ("url", "source_url", "evidence_url") and value is not None:
                canonical_key("sources", value)
            check_payload(value, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            check_payload(value, depth + 1)
    elif isinstance(payload, str):
        payload.encode("utf-8")
        if re.search(r"<(?:!doctype\s+html\b|html[\s>])", payload, re.I):
            raise ValueError("payload 须保存元数据而非整页 HTML")
    elif isinstance(payload, float) and not math.isfinite(payload):
        raise ValueError("payload 数值必须有限")


def validate_record(path: str, data: bytes) -> dict[str, Any]:
    domain, kind, identity = path_parts(path)
    if len(data) >= MAX_BYTES:
        raise ValueError("条目大小达到 500 KB")
    record = json_object(data)
    if not isinstance(record, dict) or not FIELDS <= record.keys():
        raise ValueError("条目缺少必填字段")
    if record.keys() - FIELDS - {"correction_reason", "superseded_by"}:
        raise ValueError("条目含有未知字段，请使用 payload")
    label = taxonomy.classification({key: record[key] for key in taxonomy.FIELDS})
    if (record["domain_id"], record["l2_code"], record["kind"], record["canonical_id"]) != (domain, domain, kind, identity):
        raise ValueError("领域、类型和 ID 须与文件路径一致")
    key = canonical_key(kind, record["canonical_key"])
    if key != record["canonical_key"] or canonical_id(domain, kind, key, label["object_cluster"]) != identity:
        raise ValueError("canonical_key 或 canonical_id 与规范化身份不一致")
    required_text(record["title"], 220, "标题")
    if not isinstance(record["task_id"], str) or not BATCH.fullmatch(record["task_id"]):
        raise ValueError("task_id 须指向一次完整的采集任务")
    version, status, stamp = record["version"], record["status"], record["updated_at"]
    if type(version) is not int or not 1 <= version <= 2**31 - 1 or status not in ("active", "deprecated"):
        raise ValueError("version 须为正整数，status 须为 active 或 deprecated")
    if not isinstance(stamp, str) or not UTC.fullmatch(stamp):
        raise ValueError("updated_at 须为 UTC 秒精度时间")
    datetime.fromisoformat(stamp[:-1] + "+00:00")
    if version > 1 or status == "deprecated" or "correction_reason" in record:
        required_text(record.get("correction_reason"), 2000, "correction_reason")
    if "superseded_by" in record:
        target = record["superseded_by"]
        if status != "deprecated" or not isinstance(target, str) or not IDENTITY.fullmatch(target) or target == identity:
            raise ValueError("superseded_by 须指向同领域同类型的另一个条目")
    payload = record["payload"]
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload 须为非空对象")
    check_payload(payload)
    if kind in URL_KINDS and canonical_key(kind, payload.get("url")) != key:
        raise ValueError("payload.url 须与规范化信源身份一致")
    if kind == "literature":
        catalog.validate_pair(payload.get("authority_tier"), payload.get("source_channel"))
        catalog.process_ids(payload.get("process_node_ids"))
        expected = catalog.known_channel(payload["url"], record["title"])
        if expected and payload["source_channel"] != expected:
            raise ValueError("文献来源类型应归入 " + expected)
    return record


def new_record(classification: dict[str, Any], kind: str, key: str, title: str, task_id: str,
               payload: dict[str, Any]) -> dict[str, Any]:
    label = taxonomy.classification(classification)
    domain = label["l2_code"]
    key = canonical_key(kind, key)
    identity = canonical_id(domain, kind, key, label["object_cluster"])
    record = {"canonical_id": identity, "canonical_key": key, "domain_id": domain,
              "kind": kind, "title": title, "task_id": task_id, "version": 1, "status": "active",
              "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "payload": payload, **label}
    path = record_path(record)
    try:
        return validate_record(path, json_bytes(record))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{path} · {exc}") from None


def initialize(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS shared_metadata (
        path TEXT PRIMARY KEY,
        domain_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        canonical_id TEXT NOT NULL,
        title TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        body TEXT NOT NULL,
        UNIQUE(domain_id,kind,canonical_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS shared_metadata_lookup ON shared_metadata(domain_id,status,kind)")
    db.execute("""CREATE TABLE IF NOT EXISTS shared_metadata_sync (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        checkout TEXT NOT NULL,
        last_synced_commit TEXT NOT NULL,
        source_ref TEXT NOT NULL DEFAULT 'HEAD',
        repository TEXT,
        synced_at TEXT
    )""")
    columns = {column[1] for column in db.execute("PRAGMA table_info(shared_metadata_sync)")}
    if "source_ref" not in columns:
        db.execute("ALTER TABLE shared_metadata_sync ADD COLUMN source_ref TEXT NOT NULL DEFAULT 'HEAD'")
    if "synced_at" not in columns:
        db.execute("ALTER TABLE shared_metadata_sync ADD COLUMN synced_at TEXT")
    if "repository" not in columns:
        db.execute("ALTER TABLE shared_metadata_sync ADD COLUMN repository TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS collaboration_repository (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), repository TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS shared_metadata_preferences (
        key TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
    )""")


def configured_repository(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT repository FROM collaboration_repository WHERE singleton=1").fetchone()
    return row[0] if row else ""


def sync_preferences(db: sqlite3.Connection) -> dict[str, bool]:
    return {**SYNC_DEFAULTS, **{key: bool(value) for key, value in db.execute(
        "SELECT key,enabled FROM shared_metadata_preferences") if key in SYNC_DEFAULTS}}


def context_records(
    db: sqlite3.Connection, name: str, limit: int = 10, *, label: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return bounded Git-backed leads for the active domain."""
    if not name and not label:
        return []
    if label:
        return [json.loads(row[0]) for row in db.execute("""SELECT body FROM shared_metadata
            WHERE domain_id=? AND status='active' AND json_extract(body,'$.l2_code')=domain_id
                AND json_extract(body,'$.object_cluster')=? AND length(body)<=12000
            ORDER BY CASE kind WHEN 'literature' THEN 0 WHEN 'processes' THEN 1 ELSE 2 END, path LIMIT ?""",
            (label["l2_code"], label["object_cluster"], limit))]
    return [json.loads(row[0]) for row in db.execute("""SELECT body FROM shared_metadata
        WHERE status='active' AND json_extract(body,'$.payload.domain_name')=?
            AND length(body)<=12000
        ORDER BY CASE kind WHEN 'literature' THEN 0 WHEN 'processes' THEN 1 ELSE 2 END, path LIMIT ?""",
        (name, limit))]


def existing_object_clusters(
    db: sqlite3.Connection, l2_code: str, root: Path = taxonomy.ROOT,
) -> list[dict[str, str]]:
    """Find authored object identifiers in the named industry's local checkout and task drafts."""
    code = taxonomy.require_l2(l2_code)
    examples: dict[str, str] = {}
    domain = root / PREFIX / code
    if domain.is_dir() and not domain.is_symlink():
        for kind in KINDS:
            folder = domain / kind
            if folder.is_symlink():
                continue
            for file in sorted(folder.glob("*.json")):
                try:
                    if file.is_symlink() or not file.is_file() or file.stat().st_size >= MAX_BYTES:
                        continue
                    record = validate_record(file.relative_to(root).as_posix(), file.read_bytes())
                except (OSError, ValueError):
                    continue
                examples.setdefault(record["object_cluster"], record["title"][:140])
    for cluster, title in db.execute("""SELECT json_extract(body,'$.object_cluster'), min(title)
        FROM shared_metadata WHERE domain_id=? AND status='active' GROUP BY json_extract(body,'$.object_cluster')""",
                                     (code,)):
        if isinstance(cluster, str) and taxonomy.valid_cluster(cluster):
            examples.setdefault(cluster, title[:140])
    for cluster, name in db.execute("""SELECT json_extract(body,'$.task_taxonomy.object_cluster'),
        coalesce(json_extract(body,'$.scope_domain_name'), json_extract(body,'$.plan.domain'),'')
        FROM runs WHERE json_extract(body,'$.task_taxonomy.l2_code')=? AND status<>'cancelled'
        ORDER BY sequence DESC""", (code,)):
        if isinstance(cluster, str) and taxonomy.valid_cluster(cluster):
            examples.setdefault(cluster, name[:140] if isinstance(name, str) else "")
    return [{"object_cluster": key, "example": title} for key, title in sorted(examples.items())]


def library_industry_hint(db: sqlite3.Connection, domain: int) -> dict[str, str] | None:
    """Use an unambiguous official name or alias to seed the stage's L2 lookup."""
    from .domain_libraries import _normalized

    row = db.execute("""SELECT d.name,coalesce(l.aliases,'[]') FROM domains d
        LEFT JOIN domain_library l ON l.domain_id=d.id WHERE d.id=? AND coalesce(l.archived,0)=0""",
                     (domain,)).fetchone()
    if not row:
        return None
    name = _normalized(row[0])
    aliases = json.loads(row[1])
    alias_keys = {_normalized(alias) for alias in aliases if isinstance(alias, str)}
    exact, by_alias = [], []
    for code, industry in taxonomy.registry()[1].items():
        official = {_normalized(industry[key]) for key in ("name_zh", "english_id")}
        hint = {"l2_code": code, "name_zh": industry["name_zh"]}
        if name in official:
            exact.append(hint)
        elif official & alias_keys:
            by_alias.append(hint)
    matches = exact or by_alias
    return matches[0] if len(matches) == 1 else None


def _object_terms(value: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9]{2,}", value))
    for phrase in re.findall(r"[\u3400-\u9fff]{2,}", value):
        terms.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return terms - {"http", "https", "html", "www", "pdf"}


def rank_object_clusters(rows: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    """Rank exact identifiers, complete phrases and token overlap in that order."""
    focus = " ".join(re.findall(r"[a-z0-9\u3400-\u9fff]+", unicodedata.normalize("NFKC", query).casefold()))
    terms = _object_terms(focus)
    matches = []
    for row in rows:
        slug = row["object_cluster"]
        label = slug.replace("-", " ")
        example = " ".join(re.findall(r"[a-z0-9\u3400-\u9fff]+",
                                      unicodedata.normalize("NFKC", row["example"]).casefold()))
        overlap = terms & _object_terms(label + " " + example)
        exact = int(focus == label)
        phrase = int(bool(focus) and (focus in label or focus in example or
                                      " " + label + " " in " " + focus + " "))
        if exact or phrase or overlap:
            matches.append(((-exact, -phrase, -sum(len(term) for term in overlap), slug), row))
    return [row for _, row in sorted(matches, key=lambda item: item[0])]

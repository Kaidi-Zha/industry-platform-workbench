"""Confirmed collection scopes and source-level reference records."""

import hashlib
import json
from contextlib import closing
from urllib.parse import urlsplit, urlunsplit

from . import workbench


PLAN_FIELDS = {
    "domain": 100, "scenario": 240, "stages": 500, "region": 160, "time_window": 160,
    "asset_types": 500, "sources": 1000, "deliverables": 700, "open_questions": 600,
}
PLAN_MODES = ("chat", "research", "benchmark")
REQUIRED_PLAN = {"domain", "scenario", "stages", "region", "time_window", "asset_types", "deliverables"}
REFERENCE_FIELDS = {
    "title": 220, "domain": 100, "scenario": 240, "stage": 100, "kind": 60, "publisher": 160,
    "url": 1200, "version": 120, "locator": 240, "claim": 800,
}
REQUIRED_REFERENCE = {"title", "domain", "kind", "publisher", "url", "locator", "claim"}


def fields(value, limits, required, label):
    if not isinstance(value, dict) or value.keys() - limits.keys():
        raise ValueError(label + "格式无效")
    clean = {}
    for name, limit in limits.items():
        text = value.get(name, "")
        if not isinstance(text, str) or len(text) > limit or "\x00" in text:
            raise ValueError(f"{label}的 {name} 须为不超过 {limit} 字符的文本")
        clean[name] = text.strip()
    if any(not clean[name] for name in required):
        raise ValueError(label + "缺少必填内容")
    return clean


def plan(value, *, resolve_domain=False):
    return fields(value, PLAN_FIELDS, REQUIRED_PLAN - {"domain"} if resolve_domain else REQUIRED_PLAN, "采集计划")


def revision_details(mode, reason):
    if mode not in PLAN_MODES:
        raise ValueError("请选择采集计划的运行方式")
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValueError("修订理由最多 500 字符")
    return reason.strip()


def mode_for_run(run):
    return "benchmark" if run.get("auto_benchmark") else "research" if run.get("research") else "chat"


def plan_history(connection, conversation_id):
    rows = connection.execute(
        "SELECT body FROM collection_plan_revisions WHERE conversation_id = ? ORDER BY version",
        (conversation_id,),
    ).fetchall()
    if rows:
        return [json.loads(row[0]) for row in rows]
    history = []
    for (body,) in connection.execute(
        "SELECT body FROM runs WHERE conversation_id = ? ORDER BY sequence", (conversation_id,)
    ):
        run = json.loads(body)
        scope, mode = run.get("plan"), mode_for_run(run)
        if scope and (not history or (scope, mode) != (history[-1]["plan"], history[-1]["mode"])):
            history.append({"version": len(history) + 1, "plan": scope, "mode": mode,
                            "reason": "", "confirmed_at": run["created_at"]})
    return history


def confirm_revision(connection, conversation_id, scope, mode, reason, *, at, expected=None, register_domain=True):
    reason = revision_details(mode, reason)
    history = plan_history(connection, conversation_id)
    latest = history[-1] if history else None
    version = latest["version"] if latest else 0
    if expected is not None and expected != version:
        raise ValueError("已有新版采集计划，请重新打开后再确认")
    if latest and (scope, mode) == (latest["plan"], latest["mode"]):
        return latest
    if latest and not reason:
        raise ValueError("请填写本次采集计划的修订理由")
    revision = {"version": version + 1, "plan": scope, "mode": mode,
                "reason": reason, "confirmed_at": at}
    # Materialize earlier run snapshots when first revising an existing conversation.
    connection.executemany(
        "INSERT OR IGNORE INTO collection_plan_revisions VALUES (?, ?, ?)",
        [(conversation_id, row["version"], json.dumps(row, ensure_ascii=False)) for row in (*history, revision)],
    )
    if register_domain and scope["domain"]:
        workbench.register_domain(connection, scope["domain"])
    return revision


def initialize(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS collection_plan_revisions (
            conversation_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            body TEXT NOT NULL,
            PRIMARY KEY(conversation_id, version)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS evidence_references (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL COLLATE NOCASE,
            body TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS reference_runs (
            reference_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            PRIMARY KEY(reference_id, run_id)
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS references_by_domain ON evidence_references(domain)")
    connection.execute("CREATE INDEX IF NOT EXISTS references_by_run ON reference_runs(run_id, reference_id)")


def save_reference(store, value, run_id=None, *, verification=None, relation=None, checkpoint=None):
    item = fields(value, REFERENCE_FIELDS, REQUIRED_REFERENCE, "Reference")
    try:
        url = urlsplit(item["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None or any(
            char.isspace() or ord(char) < 32 for char in item["url"]
        ):
            raise ValueError
        item["url"] = urlunsplit((url.scheme, url.netloc.lower(), url.path or "/", url.query, url.fragment))
    except ValueError:
        raise ValueError("Reference 须填写有效的 HTTP 或 HTTPS 原文链接") from None
    identity = [item[key] for key in ("domain", "url", "version", "locator")]
    if verification:
        item["verification"] = verification
        identity.extend((verification["target"], verification["quote"], verification["level"]))
        if relation:
            item["relation"] = relation
            identity.append(relation)
    else:
        identity.append(item["claim"])
    item["id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    with closing(store.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if run_id:
            row = connection.execute("SELECT body FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("未找到 Reference 所属任务")
            run = json.loads(row[0])
            scope = run.get("plan")
            if not scope or run["status"] != "running" or item["domain"] != scope["domain"]:
                raise ValueError("Reference 必须归入正在执行的采集计划所确认的领域")
            if run.get("research") and not verification:
                raise ValueError("持续补查任务须先读取原文，再使用 record_evidence 入库")
        connection.execute(
            "INSERT OR IGNORE INTO evidence_references(id, domain, body) VALUES (?, ?, ?)",
            (item["id"], item["domain"], json.dumps(item, ensure_ascii=False)),
        )
        workbench.register_domain(connection, item["domain"])
        if run_id:
            connection.execute("INSERT OR IGNORE INTO reference_runs VALUES (?, ?)", (item["id"], run_id))
        saved = json.loads(connection.execute(
            "SELECT body FROM evidence_references WHERE id = ?", (item["id"],)
        ).fetchone()[0])
        if checkpoint:
            checkpoint(connection, saved)
        return saved


def reference(store, reference_id):
    with closing(store.connect()) as connection:
        row = connection.execute("SELECT body FROM evidence_references WHERE id = ?", (reference_id,)).fetchone()
    return json.loads(row[0]) if row else None


def reference_domains(store):
    with closing(store.connect()) as connection:
        rows = connection.execute("""
            SELECT d.name, (SELECT count(*) FROM evidence_references WHERE domain = d.name)
            FROM domains d LEFT JOIN domain_library m ON m.domain_id=d.id
            WHERE coalesce(m.archived,0)=0 ORDER BY d.id
        """).fetchall()
    return [{"domain": name, "total": count} for name, count in rows]


def references(store, *, domain="", query="", offset=0, limit=20, run_id=None):
    predicates, parameters = [], []
    if domain:
        predicates.append("r.domain = ?")
        parameters.append(domain)
    if query:
        predicates.append("""EXISTS (
            SELECT 1 FROM json_each(r.body) AS field
            WHERE field.key <> 'id' AND instr(lower(field.value), lower(?)) > 0
        )""")
        parameters.append(query)
    if run_id:
        predicates.append("EXISTS (SELECT 1 FROM reference_runs WHERE reference_id = r.id AND run_id = ?)")
        parameters.append(run_id)
    where = " AND ".join(predicates) or "1"
    with closing(store.connect()) as connection:
        count = connection.execute(
            f"SELECT COUNT(*) FROM evidence_references r WHERE {where}", parameters
        ).fetchone()[0]
        rows = connection.execute(f"""
            SELECT r.body, (
                SELECT json_group_array(run_id) FROM reference_runs WHERE reference_id = r.id
            ) FROM evidence_references r WHERE {where}
            ORDER BY r.rowid DESC LIMIT ? OFFSET ?
        """, (*parameters, limit, max(0, offset))).fetchall()
        domains = [row[0] for row in connection.execute(
            "SELECT DISTINCT domain FROM evidence_references ORDER BY domain"
        )]
    return {"items": [{**json.loads(body), "run_ids": json.loads(runs)} for body, runs in rows],
            "domains": domains, "total": count}

"""Domain workspace schema and read models shared by the workbench shell."""

import json
import re
import sqlite3
import uuid
from datetime import datetime
from contextlib import closing
from pathlib import Path

from . import domain_keywords, domain_libraries, engineering_quality as quality, forest, frontier as crawl, import_library
from . import process_topology as topology
from . import reference_catalog as catalog
from .scope_guard import ENTITY_ID, STAGES

SCHEMA = Path(__file__).with_name("workbench.sql")
ACTIVE = frozenset(("queued", "running", "pausing", "cancelling"))
DEFAULT_EXPORT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "工业题包",
    "type": "object",
    "required": ["platform_version", "run_id", "tasks"],
    "properties": {
        "platform_version": {"type": "string"},
        "run_id": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "description", "inputs"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "inputs": {"type": "object"},
                    "engineering": {"type": ["object", "null"]},
                },
            },
        },
    },
}


def register_domain(connection, name):
    if connection.execute("""
        SELECT 1 FROM domains d JOIN domain_library l ON l.domain_id=d.id WHERE name=? AND l.archived=1
    """, (name,)).fetchone():
        raise ValueError("此领域已从资料库移除，请选择其他领域")
    connection.execute("INSERT OR IGNORE INTO domains(name) VALUES (?)", (name,))


def initialize(connection):
    # Execute DDL inside the store's migration transaction. executescript would commit it first.
    statement = ""
    for line in SCHEMA.read_text(encoding="utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(process_nodes)")}
    if "flow_group" not in columns:
        connection.execute("ALTER TABLE process_nodes ADD COLUMN flow_group TEXT NOT NULL DEFAULT ''")
    if "lifecycle_stages_json" not in columns:
        connection.execute("""ALTER TABLE process_nodes ADD COLUMN lifecycle_stages_json TEXT NOT NULL DEFAULT '[]'
            CHECK(json_valid(lifecycle_stages_json) AND json_type(lifecycle_stages_json) = 'array')""")
    crawl.initialize(connection)
    connection.execute("""
        INSERT OR IGNORE INTO domains(name)
        SELECT coalesce(json_extract(body, '$.plan.domain'), '') FROM runs
        UNION ALL SELECT coalesce(json_extract(body, '$.plan.domain'), '') FROM collection_plan_revisions
        UNION ALL SELECT domain FROM evidence_references
    """)
    domain_libraries.initialize(connection, forest.domains())
    connection.execute("""CREATE TABLE IF NOT EXISTS domain_activity (
        domain_id INTEGER PRIMARY KEY REFERENCES domains(id) ON DELETE CASCADE,
        updated_at TEXT NOT NULL
    )""")
    for table in ("domain_library", "process_nodes", "process_edges", "process_node_links", "raw_sources",
                  "verified_references", "benchmark_tasks", "verifier_specs", "eval_runs"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            row = "OLD" if operation == "DELETE" else "NEW"
            connection.execute(f"""CREATE TRIGGER IF NOT EXISTS activity_{table}_{operation}
                AFTER {operation} ON {table}
                WHEN EXISTS (SELECT 1 FROM domains WHERE id={row}.domain_id)
                BEGIN
                    INSERT INTO domain_activity VALUES ({row}.domain_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    ON CONFLICT(domain_id) DO UPDATE SET updated_at=excluded.updated_at;
                END""")


add_domain = domain_libraries.add


def _upstream_ids(value):
    if "upstream_ids" in value:
        upstream = value["upstream_ids"]
    else:
        predecessor = value.get("upstream_id")
        upstream = [predecessor] if predecessor is not None and predecessor != "" else []
    if (not isinstance(upstream, list)
            or any(not isinstance(item, str) or not ENTITY_ID.fullmatch(item) for item in upstream)
            or len(upstream) != len(set(upstream))):
        raise ValueError("请选择不重复的当前领域上游工序")
    return set(upstream)


def _set_upstream(db, domain, node_id, upstream):
    before = {item[0] for item in db.execute(
        "SELECT upstream_id FROM process_edges WHERE domain_id=? AND downstream_id=?", (domain, node_id))}
    for identity in sorted(upstream - before):
        if not db.execute("SELECT 1 FROM process_nodes WHERE domain_id=? AND id=?", (domain, identity)).fetchone():
            raise ValueError("上游工序必须属于当前领域")
        if db.execute("""
            WITH RECURSIVE dependent(id) AS (
                SELECT ? UNION SELECT e.downstream_id FROM process_edges e JOIN dependent d
                  ON e.upstream_id=d.id WHERE e.domain_id=?
            ) SELECT 1 FROM dependent WHERE id=?
        """, (node_id, domain, identity)).fetchone():
            raise ValueError("该连线会形成循环，请选择其他上游工序")
    db.executemany("DELETE FROM process_edges WHERE domain_id=? AND upstream_id=? AND downstream_id=?",
                   ((domain, identity, node_id) for identity in sorted(before - upstream)))
    db.executemany("INSERT INTO process_edges VALUES (?,?,?)",
                   ((domain, identity, node_id) for identity in sorted(upstream - before)))
    topology.assert_linked_dag(db, domain)


def add_process_node(store, value):
    domain_id, title = value.get("domain_id"), value.get("title")
    if type(domain_id) is not int or not 1 <= domain_id <= 2**53 - 1:
        raise ValueError("请选择工业领域")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or any(ord(char) < 32 for char in title):
        raise ValueError("工序名称须为 1–100 字符")
    upstream = _upstream_ids(value)
    occupations = _occupations(value.get("occupations", ""))
    group = topology.group_name(value.get("flow_group", ""))
    lifecycle = json.dumps(topology.lifecycle_stages(value.get("lifecycle_stages", [])))
    with closing(store.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM domains WHERE id = ?", (domain_id,)).fetchone():
            raise ValueError("未找到该工业领域")
        matched = topology.matching_node(connection, domain_id, title)
        if matched:
            raise ValueError(f"已有相同工序「{matched[1]}」，可在节点详情整理关联")
        node_id = uuid.uuid4().hex
        connection.execute("""
            INSERT INTO process_nodes(domain_id, id, position, title, occupations_json, flow_group, lifecycle_stages_json)
            SELECT ?, ?, coalesce(max(position), 0) + 1, ?, ?, ?, ? FROM process_nodes WHERE domain_id = ?
        """, (domain_id, node_id, title.strip(), json.dumps(occupations, ensure_ascii=False), group, lifecycle, domain_id))
        _set_upstream(connection, domain_id, node_id, upstream)
        return {"id": node_id, "title": title.strip()}


def _occupations(value):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("工种总长度最多 240 字符")
    return list(dict.fromkeys(role.strip() for role in value.replace("，", "、").split("、") if role.strip()))


def change_process_node(store, value):
    domain, identity, action = value.get("domain_id"), value.get("id"), value.get("action")
    if type(domain) is not int or not 1 <= domain <= 2**53 - 1 or not isinstance(identity, str) or not ENTITY_ID.fullmatch(identity):
        raise ValueError("请选择所属领域的工序")
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("""SELECT title,flow_group,lifecycle_stages_json FROM process_nodes
            WHERE domain_id=? AND id=?""", (domain, identity)).fetchone()
        if not previous:
            raise ValueError("未找到该工序")
        if action in ("link", "unlink"):
            return topology.change_link(db, domain, identity, value)
        if action == "remove":
            db.execute("DELETE FROM process_edges WHERE domain_id=? AND (upstream_id=? OR downstream_id=?)",
                       (domain, identity, identity))
            try:
                db.execute("DELETE FROM process_nodes WHERE domain_id=? AND id=?", (domain, identity))
            except sqlite3.IntegrityError:
                raise ValueError("已有工序、采集、文献或题目关联，工序无法删除") from None
            return {"removed": identity}
        title = value.get("title")
        if (action != "update" or not isinstance(title, str) or not 1 <= len(title.strip()) <= 100
                or any(ord(char) < 32 for char in title)):
            raise ValueError("请输入有效的工序名称")
        occupations = _occupations(value.get("occupations", ""))
        if topology.title_key(title) != topology.title_key(previous[0]):
            matched = topology.matching_node(db, domain, title, exclude=identity)
            if matched:
                raise ValueError(f"已有相同工序「{matched[1]}」，可在节点详情整理关联")
        group = topology.group_name(value.get("flow_group", previous[1]))
        lifecycle = topology.lifecycle_stages(value.get("lifecycle_stages", json.loads(previous[2])))
        db.execute("""UPDATE process_nodes SET title=?,occupations_json=?,flow_group=?,lifecycle_stages_json=?
            WHERE domain_id=? AND id=?""", (title.strip(), json.dumps(occupations, ensure_ascii=False),
                                            group, json.dumps(lifecycle), domain, identity))
        if "upstream_ids" in value or "upstream_id" in value:
            _set_upstream(db, domain, identity, _upstream_ids(value))
        return {"id": identity, "title": title.strip()}


def export_schema(store, domain_id):
    with closing(store.connect()) as connection:
        row = connection.execute("SELECT export_override_json FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if domain_id is not None and row is None:
            raise ValueError("未找到该工业领域")
        base = connection.execute("SELECT value_json FROM global_settings WHERE key = 'export_schema'").fetchone()
        return {"global": json.loads(base[0]) if base else DEFAULT_EXPORT_SCHEMA, "override": json.loads(row[0]) if row else {}}


def save_export_schema(store, value):
    domain_id, scope, schema = value.get("domain_id"), value.get("scope"), value.get("schema")
    if (scope == "domain" or domain_id is not None) and (type(domain_id) is not int or not 1 <= domain_id <= 2**53 - 1):
        raise ValueError("请选择工业领域")
    if scope not in ("global", "domain") or not isinstance(schema, dict):
        raise ValueError("输出格式须为 JSON 对象")
    try:
        raw = json.dumps(schema, ensure_ascii=False, allow_nan=False)
    except (ValueError, RecursionError):
        raise ValueError("输出格式包含无效的 JSON 值") from None
    if len(raw.encode("utf-8")) > 24 * 1024:
        raise ValueError("输出格式最多 24 KiB")
    if (scope == "global" or "type" in schema) and schema.get("type") != "object":
        raise ValueError("题包格式须声明 object 类型")
    with closing(store.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if domain_id is not None and not connection.execute("SELECT 1 FROM domains WHERE id = ?", (domain_id,)).fetchone():
            raise ValueError("未找到该工业领域")
        if scope == "global":
            connection.execute("""
                INSERT INTO global_settings(key, value_json) VALUES ('export_schema', ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """, (raw,))
        else:
            connection.execute("UPDATE domains SET export_override_json = ? WHERE id = ?", (raw, domain_id))
    return {"scope": scope, "schema": schema}


def task_value(store, domain, key, changes=None):
    from . import library

    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}:[\w.-]{1,160}", key):
        raise ValueError("请选择领域内已登记的题目")
    with closing(store.connect()) as db:
        name = db.execute("SELECT name FROM domains WHERE id=?", (domain,)).fetchone()
    if not name:
        raise ValueError("未找到该工业领域")
    identity = key.split(":", 1)[1]
    rows = library.read(store, "tasks", query=identity, limit=100, domain=name[0])["items"]
    item = next((entry for entry in rows if entry["id"] == key), None)
    if item is None:
        raise ValueError("未找到当前领域内的题目")
    record_key = f"task-value/{domain}/{key}"
    with closing(store.connect()) as db, db:
        if changes is None:
            row = db.execute("SELECT value_json FROM global_settings WHERE key=?", (record_key,)).fetchone()
            return json.loads(row[0]) if row else {}
        need, minutes = changes.get("necessity"), changes.get("expert_minutes")
        if type(need) is not int or not 1 <= need <= 5 or type(minutes) is not int or not 1 <= minutes <= 100000:
            raise ValueError("岗位刚需度须为 1–5，专家耗时须为正整数分钟")
        output = {"necessity": need, "expert_minutes": minutes}
        for field in ("frequency", "loss", "handoff"):
            value = changes.get(field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
                raise ValueError("请登记岗位频率、错误损失和下游衔接依据")
            output[field] = value.strip()
        refs = changes.get("references")
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 16 or any(
            not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", ref) for ref in refs
        )):
            raise ValueError("请至少绑定一条已核验文献 ID")
        for ref in refs:
            known = db.execute("""
                SELECT 1 FROM evidence_references
                    WHERE domain=? AND id=? AND json_type(body,'$.verification')='object'
                UNION SELECT 1 FROM verified_references WHERE domain_id=? AND id=?
            """, (name[0], ref, domain, ref)).fetchone()
            if not known:
                raise ValueError("所有文献须已核验并属于当前领域")
        output["references"] = list(dict.fromkeys(refs))
        db.execute("""INSERT INTO global_settings VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json""",
                   (record_key, json.dumps(output, ensure_ascii=False)))
        return output


def network_approval(store, domain, destination, action):
    with closing(store.connect()) as db, db:
        domain_id = db.execute("SELECT id FROM domains WHERE name = ?", (domain,)).fetchone()
        if not domain_id:
            return False
        if action == "remember":
            db.execute("INSERT OR IGNORE INTO domain_network_approvals VALUES (?,?)", (domain_id[0], destination))
        return db.execute("SELECT 1 FROM domain_network_approvals WHERE domain_id=? AND destination=?",
                          (domain_id[0], destination)).fetchone() is not None


def domain_approvals(store, domain_id, revoke=None):
    with closing(store.connect()) as db, db:
        if revoke is not None:
            db.execute("DELETE FROM domain_network_approvals WHERE domain_id=? AND destination=?", (domain_id, revoke))
        rows = db.execute("SELECT destination FROM domain_network_approvals WHERE domain_id=? ORDER BY destination",
                          (domain_id,)).fetchall()
    return [row[0] for row in rows]


def _reference_page(db, domain, query, offset, *, item, node, tier, channel):
    if tier and tier not in catalog.TIERS or channel and channel not in catalog.CHANNELS:
        raise ValueError("文献分类筛选无效")
    processes = [dict(row) for row in db.execute(
        "SELECT id,title,position FROM process_nodes WHERE domain_id=? ORDER BY position", (domain,))]
    if node and node != catalog.UNASSIGNED:
        node, = catalog.resolve_nodes(db, domain, [node])
    node_title = next((process["title"] for process in processes if process["id"] == node), "")
    if node == catalog.UNASSIGNED:
        node_title = "未关联工序"
    scope = """FROM verified_references v JOIN raw_sources s ON (v.domain_id,v.source_id)=(s.domain_id,s.id)"""
    filters = ["v.domain_id=?", quality.READY_REF]
    parameters = [domain]
    for value, column in ((item, "v.id"), (tier, "v.authority_tier"), (channel, "v.source_channel")):
        if value:
            filters.append(column + "=?")
            parameters.append(value)
    if node:
        if node == catalog.UNASSIGNED:
            filters.append("""NOT EXISTS(SELECT 1 FROM verified_reference_nodes l
                             WHERE (l.domain_id,l.reference_id)=(v.domain_id,v.id))""")
        else:
            filters.append(catalog.LINK.format(node="?"))
            parameters.append(node)
    if query:
        filters.append(f"""instr(lower(v.id || ' ' || v.title || ' ' || v.publisher || ' ' || s.canonical_url || ' '
            || v.summary_zh || ' ' || coalesce(json_extract(v.quality_json,'$.version_or_date.version'),'') || ' '
            || coalesce(json_extract(v.quality_json,'$.version_or_date.published_on'),'') || ' '
            || coalesce(v.object_cluster,'')),lower(?))>0 OR EXISTS (
                SELECT 1 FROM process_nodes p WHERE p.domain_id=v.domain_id AND instr(lower(p.title),lower(?))>0
                    AND {catalog.LINK.format(node="p.id")})""")
        parameters.extend((query, query))
    where = " AND ".join(f"({condition})" for condition in filters)
    counts = dict(db.execute(f"""SELECT coalesce(l.node_id,?),count(*) {scope}
        LEFT JOIN verified_reference_nodes l ON (l.domain_id,l.reference_id)=(v.domain_id,v.id)
        WHERE {where} GROUP BY l.node_id""", (catalog.UNASSIGNED, *parameters)))
    rows = db.execute(f"""
        SELECT v.id,v.node_id,v.title,v.publisher,s.canonical_url AS url,v.authority_tier,v.source_channel,
               v.object_cluster,v.lifecycle_stages_json,
               coalesce(json_extract(v.quality_json,'$.version_or_date.version'),
                        json_extract(v.quality_json,'$.version_or_date.published_on')) AS version,
               v.summary_zh,s.sha256,
               (SELECT json_group_array(node_id) FROM (
                    SELECT l.node_id FROM verified_reference_nodes l JOIN process_nodes p ON (p.domain_id,p.id)=(l.domain_id,l.node_id)
                    WHERE (l.domain_id,l.reference_id)=(v.domain_id,v.id) ORDER BY p.position)) AS node_ids,
               (SELECT count(*) FROM task_references t JOIN benchmark_tasks b
                  ON (b.domain_id,b.id)=(t.domain_id,t.task_id)
                  WHERE (t.domain_id,t.reference_id)=(v.domain_id,v.id) AND b.state<>'archived') AS citations
        {scope} WHERE {where} ORDER BY (
           SELECT min(p.position) FROM verified_reference_nodes l JOIN process_nodes p ON (p.domain_id,p.id)=(l.domain_id,l.node_id)
           WHERE (l.domain_id,l.reference_id)=(v.domain_id,v.id)
        ) NULLS LAST,v.title COLLATE NOCASE,v.id LIMIT 31 OFFSET ?
    """, (*parameters, offset)).fetchall()
    titles = {process["id"]: process["title"] for process in processes}
    available = catalog.coverage(db, domain)
    distribution = available.get(None, {})
    for row in processes:
        progress = available.get(row["id"], {})
        row.update(count=counts.get(row["id"], 0), authority_counts=progress.get("tiers", {}),
                   missing_t1_standard=catalog.missing_standard(progress.get("tiers", {})))
    processes.append({"id": catalog.UNASSIGNED, "title": "未关联工序", "count": counts.get(catalog.UNASSIGNED, 0),
                      "authority_counts": available.get(catalog.UNASSIGNED, {}).get("tiers", {}), "missing_t1_standard": False})
    items = []
    for reference in rows[:30]:
        value = dict(reference)
        nodes = json.loads(value.pop("node_ids"))
        value["process_node_ids"] = nodes or [catalog.UNASSIGNED]
        value["process_names"] = [titles[identity] for identity in nodes]
        lifecycle = value.pop("lifecycle_stages_json")
        value["lifecycle_stages"] = json.loads(lifecycle) if lifecycle else []
        items.append(value)
    return {
        "items": items, "more": len(rows) > 30, "node": node, "node_title": node_title, "processes": processes,
        "total": distribution.get("total", 0),
        "authority_tiers": [{"id": key, "title": title, "short_title": short,
                             "verified": distribution.get("tiers", {}).get(key, 0)}
                            for key, (title, short) in catalog.TIERS.items()],
        "channels": [{"id": key, "title": title, "verified": distribution.get("channels", {}).get(key, 0)}
                     for key, title in catalog.CHANNELS.items()],
    }


def source_page(store, domain_id, kind, query="", offset=0, *, item="", stage="", node="", tier="", channel=""):
    if kind not in ("references", "frontier", "imports") or not 0 <= offset <= 2**31 - 1:
        raise ValueError("信源查询参数无效")
    if node and (kind != "references" or not ENTITY_ID.fullmatch(node)) or kind != "references" and (tier or channel):
        raise ValueError("请选择有效的文献工序")
    with closing(store.connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        domain = connection.execute("SELECT name FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if domain is None:
            raise ValueError("未找到该工业领域")
        if kind == "imports":
            return import_library.page(connection, domain_id, query, offset, item=item, stage=stage)
        if kind == "references":
            return _reference_page(connection, domain_id, query, offset, item=item, node=node, tier=tier, channel=channel)
        if kind == "frontier":
            sql = f"""
                SELECT f.id, f.target,
                    CASE WHEN w.quality_note<>'' THEN 'filtered' ELSE f.state END AS state,
                    f.node_id,f.round_number,f.kind,f.attempts,
                    coalesce(nullif(w.quality_note,''),f.last_error) AS last_error,
                    f.source_id,p.title AS stage,c.title AS channel,s.retrieved_at,s.canonical_url,
                    result.assessment,s.sha256,e.first_round,NULL AS reference_id,NULL AS import_summary
                FROM crawl_frontier f JOIN process_nodes p ON (p.domain_id,p.id)=(f.domain_id,f.node_id)
                JOIN crawl_channels c ON c.id=f.channel_id
                LEFT JOIN raw_sources s ON (s.domain_id,s.id)=(f.domain_id,f.source_id)
                LEFT JOIN workflow_source_passages w ON (w.domain_id,w.node_id,w.source_id)=(f.domain_id,f.node_id,f.source_id)
                LEFT JOIN frontier_candidates result ON
                    (result.domain_id,result.node_id,result.round_number,result.source_id)
                    =(f.domain_id,f.node_id,f.round_number,f.source_id)
                LEFT JOIN entity_fingerprints e ON (e.domain_id,e.node_id,e.fingerprint)=(result.domain_id,result.node_id,result.fingerprint)
                WHERE f.domain_id=?
                UNION ALL
                SELECT s.id,coalesce(json_extract(s.import_metadata_json,'$.title'),s.canonical_url),CASE WHEN w.quality_note<>'' THEN 'filtered'
                    WHEN s.status='discovered' AND json_type(s.import_metadata_json,'$.batch_id')='text' THEN 'needs_review'
                    WHEN s.status IN ('discovered','blocked','rejected') THEN s.status ELSE 'done' END,
                    w.node_id,NULL,'url',NULL,nullif(w.quality_note,''),s.id,p.title,
                    CASE WHEN json_type(s.import_metadata_json,'$.batch_id')='text' THEN '本地导入' ELSE 'Agent 调研' END,
                    s.retrieved_at,s.canonical_url,NULL,s.sha256,NULL,NULL,json_extract(s.import_metadata_json,'$.summary')
                FROM raw_sources s LEFT JOIN workflow_source_passages w ON (w.domain_id,w.source_id)=(s.domain_id,s.id)
                LEFT JOIN process_nodes p ON (p.domain_id,p.id)=(w.domain_id,w.node_id)
                WHERE s.domain_id=?
                    AND NOT EXISTS (SELECT 1 FROM crawl_frontier f WHERE (f.domain_id,f.source_id)=(s.domain_id,s.id)
                        AND (w.node_id IS NULL OR w.node_id=f.node_id))
                    AND (?=s.id OR s.status<>'discovered' OR NOT EXISTS (
                        SELECT 1 FROM raw_sources fetched WHERE fetched.domain_id=s.domain_id
                          AND fetched.canonical_url=s.canonical_url AND fetched.version<>''
                    ))
                UNION ALL
                SELECT v.id,v.title,CASE WHEN w.quality_note<>'' THEN 'filtered' ELSE 'legacy_review' END,
                    v.node_id,NULL,'url',NULL,nullif(w.quality_note,''),v.source_id,p.title,'历史核验',
                    s.retrieved_at,s.canonical_url,NULL,s.sha256,NULL,v.id,NULL
                FROM verified_references v JOIN raw_sources s ON (s.domain_id,s.id)=(v.domain_id,v.source_id)
                LEFT JOIN process_nodes p ON (p.domain_id,p.id)=(v.domain_id,v.node_id)
                LEFT JOIN workflow_source_passages w ON (w.domain_id,w.node_id,w.source_id)=(v.domain_id,v.node_id,v.source_id)
                WHERE v.domain_id=? AND NOT ({quality.READY_REF} IS TRUE)
                    AND NOT EXISTS (SELECT 1 FROM verified_references other
                        WHERE (other.domain_id,other.node_id,other.source_id)=(v.domain_id,v.node_id,v.source_id)
                            AND json_array_length(other.quality_json,'$.extracted_clauses') > 0)
                UNION ALL
                SELECT e.id,coalesce(json_extract(e.body,'$.title'),json_extract(e.body,'$.url')),
                    CASE WHEN json_type(e.body,'$.verification')='object' THEN 'legacy_review' ELSE 'discovered' END,
                    NULL,NULL,'url',NULL,NULL,NULL,json_extract(e.body,'$.stage'),'历史信源',NULL,
                    json_extract(e.body,'$.url'),NULL,json_extract(e.body,'$.verification.sha256'),NULL,e.id,NULL
                FROM evidence_references e WHERE e.domain=?
            """
            args = (domain_id, domain_id, item, domain_id, domain["name"])
            columns = "id || ' ' || target || ' ' || coalesce(stage,'') || ' ' || channel || ' ' || coalesce(canonical_url,'') || ' ' || coalesce(import_summary,'')"
        rows = connection.execute(f"""
            SELECT * FROM ({sql}) WHERE (instr(lower({columns}), lower(?)) > 0 OR ? = '')
              AND (? = '' OR id = ?) ORDER BY id LIMIT 31 OFFSET ?
        """, (*args, query, query, item, item, offset)).fetchall()
        channels = [dict(row) for row in connection.execute("""
            SELECT c.id, c.title, count(f.id) AS total,
               count(CASE WHEN f.state='done' THEN 1 END) AS completed,
               count(CASE WHEN f.state='blocked' THEN 1 END) AS blocked
            FROM reference_channel_tiers taxonomy JOIN crawl_channels c ON c.id=taxonomy.channel
            LEFT JOIN crawl_frontier f ON f.channel_id=c.id AND f.domain_id=?
            GROUP BY c.id ORDER BY c.rowid
        """, (domain_id,))]
        by_channel = catalog.coverage(connection, domain_id).get(None, {}).get("channels", {})
        for channel in channels:
            channel["verified"] = by_channel.get(channel["id"], 0)
    return {"items": [dict(row) for row in rows[:30]], "more": len(rows) > 30, "channels": channels}


def reference_detail(store, domain, identity):
    with closing(store.connect()) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        row = db.execute(f"""SELECT v.id,v.node_id,v.authority_tier,v.source_channel,v.object_cluster,v.lifecycle_stages_json,
            v.title,v.summary_zh,v.publisher,v.finding,v.authority,v.locator,
            v.excerpt,v.permission,v.verified_at,v.attachment_uri,s.snapshot_uri,s.sha256,s.retrieved_at,
            s.canonical_url AS url,p.title AS stage,v.quality_json,({quality.READY_REF}) AS quality_approved,
            w.quality_note
            FROM verified_references v JOIN raw_sources s ON (s.domain_id,s.id)=(v.domain_id,v.source_id)
            LEFT JOIN process_nodes p ON (p.domain_id,p.id)=(v.domain_id,v.node_id)
            LEFT JOIN workflow_source_passages w ON (w.domain_id,w.node_id,w.source_id)=(v.domain_id,v.node_id,v.source_id)
            WHERE v.domain_id=? AND v.id=?""", (domain, identity)).fetchone()
        if row:
            item = dict(row)
            item.update(json.loads(item.pop("quality_json")))
            nodes = db.execute("""SELECT p.id,p.title,p.position FROM verified_reference_nodes l JOIN process_nodes p
                ON (p.domain_id,p.id)=(l.domain_id,l.node_id) WHERE l.domain_id=? AND l.reference_id=?
                ORDER BY p.position""", (domain, identity)).fetchall()
            item["processes"] = [dict(node) for node in nodes]
            item["process_node_ids"] = [node["id"] for node in nodes] or [catalog.UNASSIGNED]
            item["authority_tier_title"] = catalog.TIERS.get(item["authority_tier"], ("", ""))[0]
            item["source_channel_title"] = catalog.CHANNELS.get(item["source_channel"], "")
            lifecycle = item.pop("lifecycle_stages_json")
            item["lifecycle_stages"] = json.loads(lifecycle) if lifecycle else []
            item["quality_approved"] = bool(item["quality_approved"])
            for key in ("attachment_uri", "snapshot_uri"):
                # These database fields may hold local paths. Only registered public links go to the browser.
                if not (item[key] or "").startswith("https://"):
                    item[key] = None
        else:
            row = db.execute("""SELECT e.body FROM evidence_references e JOIN domains d ON d.name=e.domain
                WHERE d.id=? AND e.id=?""", (domain, identity)).fetchone()
            if not row:
                raise ValueError("当前领域没有此文献")
            old = json.loads(row[0])
            item = {key: old[key] for key in ("title", "summary_zh", "publisher", "url", "locator", "stage") if key in old}
            verification = old.get("verification") or {}
            item.update(id=identity, finding=old.get("claim"), excerpt=verification.get("quote"),
                        retrieved_at=verification.get("retrieved_at"), verified_at=verification.get("at"), quality_approved=False)

        item["task_usage"] = []
        for task in db.execute("""SELECT t.id,t.title,t.definition_json,t.is_stale FROM task_references b
            JOIN benchmark_tasks t ON (t.domain_id,t.id)=(b.domain_id,b.task_id)
            WHERE b.domain_id=? AND b.reference_id=? AND t.state<>'archived' ORDER BY t.title""", (domain, identity)):
            item["task_usage"].append({"id": task["id"], "title": task["title"], "is_stale": bool(task["is_stale"]),
                "citations": [citation for citation in json.loads(task["definition_json"]).get("reference_citations", [])
                              if citation["reference_id"] == identity]})
        for task in db.execute("""SELECT r.id AS run_id,json_extract(t.value,'$.id') AS id,
            json_extract(t.value,'$.title') AS title FROM runs r,json_each(r.body,'$.result.tasks') t
            WHERE json_type(t.value,'$.id')='text' AND json_extract(r.body,'$.plan.domain')=(SELECT name FROM domains WHERE id=?)
              AND EXISTS(SELECT 1 FROM json_each(t.value,'$.references') b WHERE json_extract(b.value,'$.id')=?)
            ORDER BY r.sequence DESC LIMIT 50""", (domain, identity)):
            item["task_usage"].append({"id": task["run_id"] + ":" + task["id"], "title": task["title"], "citations": []})
        item["verifier_usage"] = [dict(check) for check in db.execute("""
            SELECT t.id AS task_id,t.title AS task_title,v.version,v.is_stale,l.key AS layer,
                json_extract(rule.value,'$.title') AS title,json_extract(c.value,'$.clause_id') AS clause_id,
                json_extract(c.value,'$.applies_to') AS applies_to
            FROM verifier_specs v JOIN benchmark_tasks t ON (t.domain_id,t.id)=(v.domain_id,v.task_id),
                json_each(v.layers_json) l,json_each(l.value) rule,json_each(rule.value,'$.reference_citations') c
            WHERE v.domain_id=? AND t.state<>'archived' AND json_extract(c.value,'$.reference_id')=?
                AND v.version=(SELECT max(version) FROM verifier_specs WHERE (domain_id,task_id)=(v.domain_id,v.task_id))
            ORDER BY t.title,l.key,rule.key
        """, (domain, identity))]
        return item


def run_domain(store, run):
    name = run.get("scope_domain_name") or (run.get("plan") or {}).get("domain") or (run.get("benchmark") or {}).get("domain")
    if not name:
        for task in run.get("result", {}).get("tasks", []):
            if name := (task.get("template") or {}).get("domain"):
                break
    if not name and run.get("parent_run_id"):
        parent = store.get(run["parent_run_id"]) or {}
        name = parent.get("scope_domain_name") or (parent.get("plan") or {}).get("domain")
    if not name:
        return None
    with closing(store.connect()) as db:
        row = db.execute("""SELECT d.id FROM domains d LEFT JOIN domain_library l ON l.domain_id=d.id
            WHERE d.name=? AND coalesce(l.archived,0)=0""", (name,)).fetchone()
    return row[0] if row else None


def _card_keywords(connection, domain, counts, occupations):
    identity = domain["id"]
    keywords = domain["keywords"]
    # Derive additional display keywords from stage outputs without granting agent tools domain writes.
    documents = connection.execute(f"""
        SELECT title,summary_zh FROM (
            SELECT title,summary_zh FROM verified_references WHERE domain_id=? AND {quality.READY_REF}
            ORDER BY verified_at DESC,id LIMIT 12
        ) UNION ALL SELECT title,'' FROM (
            SELECT title FROM process_nodes WHERE domain_id=? AND is_stale=0 ORDER BY position LIMIT 12
        )
    """, (identity, identity)).fetchall() if counts["nodes"] or counts["references"] else []
    suggestions = domain_keywords.extract(documents) if documents else []
    retained = domain_keywords.LIMIT - 1
    tags = domain_keywords.merge(keywords[:retained], suggestions, keywords[retained:], occupations)
    if len(tags) >= 3:
        return tags
    classification = connection.execute("""SELECT json_extract(import_metadata_json,'$.l2_code'),
        coalesce(json_extract(import_metadata_json,'$.object_cluster'),'') FROM raw_sources
        WHERE domain_id=? AND import_metadata_json<>'{}' ORDER BY id LIMIT 1
    """, (identity,)).fetchone() if counts["imported"] else None
    return domain_keywords.merge(tags, domain_keywords.fallback(domain["name"], *(classification or ())))[:3]


def _domain_cards(connection, domains, threads):
    latest = {}
    for thread in threads:
        name = (thread["domain"] or "").casefold()
        latest.setdefault(name, thread)
    legacy_counts = {name.casefold(): (tasks, evaluated) for name, tasks, evaluated in connection.execute("""
        WITH archived AS (
            SELECT coalesce(nullif(json_extract(r.body,'$.plan.domain'),''),
                            nullif(json_extract(r.body,'$.benchmark.domain'),''),
                            json_extract(t.value,'$.template.domain'), '') AS domain,
                EXISTS(SELECT 1 FROM json_each(r.body,'$.result.evaluations') e
                    WHERE json_extract(e.value,'$.task_id')=json_extract(t.value,'$.id')
                    AND json_extract(e.value,'$.status')='scored') AS evaluated
            FROM runs r, json_each(r.body,'$.result.tasks') t
        ) SELECT domain, count(*), sum(evaluated) FROM archived GROUP BY domain COLLATE NOCASE
    """)}
    for domain in domains:
        identity = domain["id"]
        row = connection.execute(f"""
            SELECT count(*) AS nodes,
                (SELECT created_at FROM domains WHERE id=?) AS created,
                (SELECT count(*) FROM verified_references WHERE domain_id=? AND {quality.READY_REF}) AS workspace_references,
                (SELECT count(*) FROM benchmark_tasks WHERE domain_id=? AND state<>'archived') AS tasks,
                (SELECT count(DISTINCT e.task_id) FROM eval_runs e
                    JOIN benchmark_tasks t ON (e.domain_id,e.task_id)=(t.domain_id,t.id)
                    WHERE e.domain_id=? AND t.state<>'archived' AND e.status='completed' AND e.is_stale=0) AS evaluated,
                (SELECT count(*) FROM verifier_specs v JOIN benchmark_tasks t ON (v.domain_id,v.task_id)=(t.domain_id,t.id)
                    WHERE v.domain_id=? AND t.state<>'archived') AS verifiers,
                EXISTS(SELECT 1 FROM crawl_frontier WHERE domain_id=?) AS collecting
            FROM process_nodes WHERE domain_id=?
        """, (identity,) * 7).fetchone()
        legacy = legacy_counts.get(domain["name"].casefold(), (0, 0))
        occupations = [item[0] for item in connection.execute("""
            SELECT DISTINCT j.value FROM process_nodes p, json_each(p.occupations_json) j WHERE p.domain_id=?
            ORDER BY j.value
        """, (identity,))]
        current = latest.get(domain["name"].casefold(), {})
        imported, pending_imports = import_library.source_counts(connection, identity)
        counts = dict(nodes=row["nodes"], references=row["workspace_references"], imported=imported,
                      tasks=row["tasks"] + legacy[0], evaluated=row["evaluated"] + legacy[1])
        stage = current.get("stage") if current.get("status") in (*ACTIVE, "awaiting_scope", "awaiting_stage", "paused", "needs_review") else None
        fallback = (counts["evaluated"], row["verifiers"], counts["tasks"],
                    counts["references"] or row["collecting"] or counts["imported"], counts["nodes"], True)
        updated = connection.execute("SELECT updated_at FROM domain_activity WHERE domain_id=?", (identity,)).fetchone()
        timestamps = [value for value in (row["created"], updated[0] if updated else None, current.get("updated_at")) if value]
        domain.update(workspace_references=row["workspace_references"],
                      counts=counts, local_import_count=pending_imports,
                      occupations=occupations, keywords=_card_keywords(connection, domain, counts, occupations),
                      stage=stage or next(key for key, present in zip(reversed(STAGES), fallback) if present),
                      updated_at=max(timestamps, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))))


def overview(store, domain_id=None, *, thread=None, level=None, include_import_coverage=True):
    threads = store.conversations()
    requested = next((row for row in threads if row["id"] == thread), None)
    with closing(store.connect()) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        domains = domain_libraries.catalog(connection)
        _domain_cards(connection, domains, threads)
        if requested:
            name = requested["domain"]
            thread_domain = next((row for row in domains if (name or "").casefold() == row["name"].casefold()), None)
        else:
            thread_domain = None
        domain = next((row for row in domains if row["id"] == domain_id), None)
        if domain is None and domain_id is not None:
            target = connection.execute("SELECT merged_into FROM domain_library WHERE domain_id=?", (domain_id,)).fetchone()
            domain = next((row for row in domains if target and row["id"] == target[0]), None)
        thread_nodes = []
        if thread_domain and level == "global":
            thread_nodes = [dict(row) for row in connection.execute("""SELECT id,title FROM process_nodes
                WHERE domain_id=? ORDER BY position""", (thread_domain["id"],))]
        if level == "global":
            domain = None
        elif level is None and domain_id is None:
            domain = thread_domain if requested else next(iter(domains), None)
        nodes, edges, relations, participants, metrics, references, frontier, stage_states = [], [], [], [], {}, [], [], {}
        flow_groups, node_links, node_suggestions = [], [], {}
        import_coverage = []
        if domain:
            identity = (domain["id"],)
            metrics["imported_sources"] = domain["counts"]["imported"]
            if include_import_coverage and metrics["imported_sources"]:
                import_coverage = import_library.coverage(connection, domain["id"])
            evidence_coverage = catalog.coverage(connection, domain["id"])
            nodes = [dict(row) for row in connection.execute(f"""
                SELECT p.id, p.title, p.position, t.expected_sources,
                    (SELECT count(*) FROM (
                        SELECT source_id FROM crawl_frontier WHERE (domain_id,node_id)=(p.domain_id,p.id)
                            AND kind='url' AND state='done' AND source_id IS NOT NULL
                        UNION
                        SELECT source_id FROM workflow_node_sources WHERE (domain_id,node_id)=(p.domain_id,p.id)
                        UNION
                        SELECT v.source_id FROM verified_reference_nodes l JOIN verified_references v
                            ON (l.domain_id,l.reference_id)=(v.domain_id,v.id)
                        WHERE (l.domain_id,l.node_id)=(p.domain_id,p.id) AND {quality.READY_REF}
                    )) AS sources,
                    (SELECT count(*) FROM process_node_evidence WHERE (domain_id,node_id)=(p.domain_id,p.id)) AS legacy_evidence_count,
                    (SELECT count(DISTINCT v.source_id) FROM verified_reference_nodes l JOIN verified_references v
                         ON (l.domain_id,l.reference_id)=(v.domain_id,v.id)
                     WHERE (l.domain_id,l.node_id)=(p.domain_id,p.id) AND {quality.READY_REF})
                    + (SELECT count(*) FROM process_node_evidence WHERE (domain_id,node_id)=(p.domain_id,p.id)) AS verified_sources,
                    (SELECT status FROM frontier_campaigns WHERE (domain_id,node_id)=(p.domain_id,p.id)) AS crawl_status,
                    EXISTS(SELECT 1 FROM workflow_frontier_nodes w WHERE (w.domain_id,w.node_id)=(p.domain_id,p.id))
                        AS scoped_frontier,
                    p.occupations_json,p.is_stale,p.stale_reason,p.flow_group,p.lifecycle_stages_json
                FROM process_nodes p
                LEFT JOIN process_coverage_targets t ON (t.domain_id, t.node_id) = (p.domain_id, p.id)
                WHERE p.domain_id = ? ORDER BY p.position
            """, identity)]
            for node in nodes:
                node["occupations"] = json.loads(node.pop("occupations_json"))
                node["lifecycle_stages"] = json.loads(node.pop("lifecycle_stages_json"))
                node["channels"] = []
                classification = evidence_coverage.get(node["id"], {})
                node["reference_count"] = classification.get("total", 0)
                node["authority_counts"] = classification.get("tiers", {})
                node["missing_t1_standard"] = catalog.missing_standard(node["authority_counts"])
                node["verified"] = node["reference_count"] + node.pop("legacy_evidence_count")
            by_node = {node["id"]: node for node in nodes}
            for row in connection.execute("""
                SELECT p.id AS node_id,c.id,c.title,count(f.id) AS total,
                    count(CASE WHEN f.state='done' THEN 1 END) AS completed,
                    count(CASE WHEN f.state='blocked' THEN 1 END) AS blocked,
                    count(CASE WHEN f.state='leased' THEN 1 END) AS leased,
                    count(CASE WHEN f.state IN ('queued','leased','retry') THEN 1 END) AS pending
                FROM process_nodes p CROSS JOIN reference_channel_tiers taxonomy JOIN crawl_channels c ON c.id=taxonomy.channel
                LEFT JOIN crawl_frontier f ON (f.domain_id,f.node_id,f.channel_id)=(p.domain_id,p.id,c.id)
                WHERE p.domain_id=? GROUP BY p.id,c.id ORDER BY p.position,c.rowid
            """, identity):
                channel = dict(row)
                node_id = channel.pop("node_id")
                channel["verified"] = evidence_coverage.get(node_id, {}).get("channels", {}).get(channel["id"], 0)
                by_node[node_id]["channels"].append(channel)
            active = [json.loads(row[0]) for row in connection.execute("""
                SELECT c.body FROM workflow_scope_contracts c JOIN runs r ON c.run_id=r.id
                WHERE c.state='executing' AND r.status='running'
                  AND CAST(json_extract(c.body,'$.domain_id') AS INTEGER)=?
                  AND json_extract(c.body,'$.target_stage')=?
                  AND json_extract(c.body,'$.operation_mode') IS NOT 'READ_ONLY'
            """, (*identity, STAGES[2]))]
            for node in nodes:
                channels = node["channels"]
                done = sum(channel["completed"] / channel["total"] for channel in channels if channel["total"])
                expected = node.pop("expected_sources") or 1
                evidence = min(1, node.pop("verified_sources") / expected)
                working = any(not scope["allowed_process_node_ids"] or node["id"] in scope["allowed_process_node_ids"]
                              for scope in active)
                managed = node.pop("scoped_frontier")
                pending = any(channel["pending"] or channel["blocked"] for channel in channels)
                exhausted = (node["crawl_status"] == "exhausted" and not pending
                             and crawl._decide(connection, (domain["id"], node["id"]))[0])
                running = working or any(channel["leased"] for channel in channels) or (
                    not managed and node["crawl_status"] == "running")
                node["crawl_status"] = ("running" if running else "exhausted" if exhausted else
                                        "blocked" if node["crawl_status"] == "blocked" else "idle")
                progress = round(100 * (done + evidence) / (len(channels) + 1))
                node["coverage"] = 100 if node["crawl_status"] == "exhausted" else (
                    max(1, min(progress, 99)) if done or evidence else 0)
            participants = [dict(row) for row in connection.execute("""
                SELECT a.name,a.role,a.node_id,p.title AS stage,v.id AS reference_id,v.title AS reference_title,
                    v.locator,s.canonical_url AS url
                FROM process_participants a JOIN process_nodes p ON (a.domain_id,a.node_id)=(p.domain_id,p.id)
                JOIN verified_references v ON (a.domain_id,a.reference_id)=(v.domain_id,v.id)
                JOIN raw_sources s ON (v.domain_id,v.source_id)=(s.domain_id,s.id)
                WHERE a.domain_id=? ORDER BY p.position,a.name
            """, identity)]
            for stage, state, status in connection.execute("""
                SELECT json_extract(c.body,'$.target_stage'),c.state,r.status
                FROM workflow_scope_contracts c JOIN runs r ON r.id=c.run_id
                WHERE CAST(coalesce(json_extract(c.body,'$.domain_id'),json_extract(r.body,'$.scope_domain_id')) AS INTEGER)=?
                ORDER BY r.sequence DESC,c.rowid DESC
            """, identity):
                if stage and state != "replaced":
                    value = ("completed" if state == "completed" else "running" if status in ACTIVE
                             else "draft" if status not in ("failed", "cancelled") else "")
                    if value and (stage not in stage_states or value == "running"):
                        stage_states[stage] = value
            edges = [dict(row) for row in connection.execute("""
                SELECT upstream_id, downstream_id FROM process_edges
                WHERE domain_id = ? ORDER BY upstream_id, downstream_id
            """, identity)]
            flow_groups = topology.flow_groups(nodes, edges)
            node_links = topology.links_for(connection, domain["id"])
            node_suggestions = topology.similar_nodes(nodes, node_links)
            relations = [dict(row) for row in connection.execute("""
                SELECT json_extract(e.body, '$.relation.from_party') AS sender,
                    json_extract(e.body, '$.relation.to_party') AS recipient,
                    json_extract(e.body, '$.relation.artifact') AS deliverable,
                    json_extract(e.body, '$.relation.type') AS kind,
                    json_extract(e.body, '$.locator') AS locator, json_extract(e.body, '$.url') AS url
                FROM evidence_references e WHERE e.domain=? AND json_type(e.body,'$.relation')='object'
                  AND json_type(e.body,'$.verification')='object'
                UNION ALL
                SELECT sender,recipient,deliverable,kind,locator,url
                FROM grounded_process_relations WHERE domain_id=? LIMIT 40
            """, (domain["name"], domain["id"]))]
            for table in ("crawl_frontier", "raw_sources", "benchmark_tasks", "verifier_specs", "eval_runs"):
                metrics[table] = connection.execute(f"SELECT count(*) FROM {table} WHERE domain_id = ?", identity).fetchone()[0]
            task_states = connection.execute("""SELECT count(*) AS total,
                count(CASE WHEN state='ready' AND is_stale=0 AND quality_approved=1 THEN 1 END) AS finished
                FROM benchmark_tasks WHERE domain_id=? AND state<>'archived'""", identity).fetchone()
            verifier_states = connection.execute("""SELECT count(*) AS total,
                count(CASE WHEN v.is_stale=0 AND (SELECT count(DISTINCT json_extract(v.self_tests_json,'$['||j.key||'].variant'))
                    FROM json_each(v.self_tests_json) j
                    WHERE json_extract(v.self_tests_json,'$['||j.key||'].variant') IN ('standard','incomplete')
                        AND json_extract(v.self_tests_json,'$['||j.key||'].result.expected')=1)=2 THEN 1 END) AS finished
                FROM verifier_specs v WHERE v.domain_id=? AND v.version=(SELECT max(latest.version)
                    FROM verifier_specs latest WHERE (latest.domain_id,latest.task_id)=(v.domain_id,v.task_id))
            """, identity).fetchone()
            evaluation_states = connection.execute("""SELECT count(*) AS total,
                count(CASE WHEN e.is_stale=0 AND e.status='completed' THEN 1 END) AS finished
                FROM eval_runs e WHERE e.domain_id=? AND e.rowid=(SELECT max(latest.rowid)
                    FROM eval_runs latest WHERE (latest.domain_id,latest.task_id)=(e.domain_id,e.task_id))
            """, identity).fetchone()
            for stage, (total, finished) in zip(("STAGE_4_TASK_DESIGN", "STAGE_5_VERIFIER", "STAGE_6_EVAL_EXPORT"),
                                                 (task_states, verifier_states, evaluation_states)):
                if total and stage_states.get(stage) != "running":
                    stage_states[stage] = "completed" if total == finished else "draft"
            references = [dict(row) for row in connection.execute(f"""
                SELECT v.id,v.title,p.title AS stage,v.publisher,v.locator,s.canonical_url AS url,v.authority_tier,v.source_channel
                FROM verified_references v JOIN raw_sources s ON (s.domain_id,s.id)=(v.domain_id,v.source_id)
                LEFT JOIN process_nodes p ON (p.domain_id,p.id)=(v.domain_id,v.node_id)
                WHERE v.domain_id=? AND {quality.READY_REF} LIMIT 25
            """, identity)]
            frontier = [dict(row) for row in connection.execute("""
                SELECT f.id, f.target, f.state, f.attempts, f.next_attempt_at, p.title AS stage, c.title AS channel
                FROM crawl_frontier f JOIN process_nodes p ON (p.domain_id, p.id) = (f.domain_id, f.node_id)
                JOIN crawl_channels c ON c.id = f.channel_id
                WHERE f.domain_id = ? ORDER BY f.round_number DESC, f.id LIMIT 25
            """, identity)]
            metrics["verified_references"] = domain["workspace_references"]
            if stage_states.get("STAGE_3_ACQUISITION") != "running":
                if nodes and all(node["crawl_status"] == "exhausted" for node in nodes):
                    stage_states["STAGE_3_ACQUISITION"] = "completed"
                elif metrics["verified_references"] or metrics["imported_sources"] or frontier or "STAGE_3_ACQUISITION" in stage_states:
                    stage_states["STAGE_3_ACQUISITION"] = "draft"
            metrics["reference_summaries"] = connection.execute(f"""SELECT count(*) FROM verified_references
                WHERE domain_id=? AND {quality.READY_REF} AND summary_zh<>''""", identity).fetchone()[0]
    if level == "global":
        tasks = threads
    else:
        name = domain["name"].casefold() if domain else ""
        tasks = [row for row in threads if name and (name == (row["domain"] or "").casefold() or
                 name in {library.casefold() for library in row.get("library_domains", [])}) or
                 level is None and (row["id"] == thread or row["domain"] == "")]
    return {"domains": domains, "domain": domain, "thread_domain": thread_domain, "thread_exists": bool(requested),
            "nodes": nodes, "thread_nodes": thread_nodes, "edges": edges, "flow_groups": flow_groups,
            "node_links": node_links, "node_suggestions": node_suggestions,
            "relations": relations, "participants": participants, "stage_states": stage_states,
            "references": references, "frontier": frontier, "metrics": metrics, "import_coverage": import_coverage,
            "threads": tasks, "active_count": sum(row["status"] in ACTIVE for row in tasks)}



def record_process_graph(store, run_id, name, graph_json):
    """Extend a confirmed graph with within-domain evidence and acyclic edges."""
    try:
        graph = json.loads(graph_json)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("工艺图谱须为有效 JSON") from None
    if not isinstance(graph, list) or not 1 <= len(graph) <= 16:
        raise ValueError("一次可登记 1–16 个工序")
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        domain = db.execute("SELECT id FROM domains WHERE name=?", (name,)).fetchone()
        run = db.execute("SELECT status,body FROM runs WHERE id=?", (run_id,)).fetchone()
        record = json.loads(run[1]) if run else {}
        if (not domain or not run or run[0] != "running" or not record.get("research")
                or record.get("plan", {}).get("domain") != name):
            raise ValueError("工艺图谱只能由已确认的研究任务登记")
        domain_id = domain[0]
        identity = dict(db.execute("SELECT title,id FROM process_nodes WHERE domain_id=?", (domain_id,)))
        position = db.execute("SELECT coalesce(max(position),0) FROM process_nodes WHERE domain_id=?",
                              (domain_id,)).fetchone()[0]
        saved = {}
        incoming = {}
        for row in graph:
            required = {"title", "occupations", "references", "upstream"}
            if not isinstance(row, dict) or not required <= row.keys() <= required | {"flow_group", "lifecycle_stages"}:
                raise ValueError("每个节点须包含工序名、工种、证据和上游列表")
            title, refs, upstream = row["title"], row["references"], row["upstream"]
            if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or title in saved
                    or not isinstance(refs, list) or not 1 <= len(refs) <= 8
                    or not isinstance(upstream, list) or len(upstream) > 8
                    or any(not isinstance(entry, str) or not 1 <= len(entry) <= 100 for entry in upstream)):
                raise ValueError("工序名称、证据或上游列表无效")
            title = title.strip()
            if title in saved:
                raise ValueError("同一工序只能出现一次")
            occupations = _occupations(row["occupations"])
            matched = topology.matching_node(db, domain_id, title)
            node = matched[0] if matched else None
            if node and node in saved.values():
                raise ValueError("同一工序只能出现一次")
            if not node:
                position += 1
                node = uuid.uuid4().hex
                group = topology.group_name(row.get("flow_group", ""))
                lifecycle = topology.lifecycle_stages(row.get("lifecycle_stages", []))
                db.execute("""INSERT INTO process_nodes(domain_id,id,position,title,occupations_json,flow_group,lifecycle_stages_json)
                    VALUES (?,?,?,?,?,?,?)""", (domain_id, node, position, title,
                                             json.dumps(occupations, ensure_ascii=False), group, json.dumps(lifecycle)))
            identity[title] = node
            for ref in refs:
                if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{64}", ref) or not db.execute("""
                    SELECT 1 FROM evidence_references WHERE domain=? AND id=?
                    AND json_type(body,'$.verification')='object'
                """, (name, ref)).fetchone():
                    raise ValueError("每个工序须关联当前领域已核验的原文")
                db.execute("INSERT OR IGNORE INTO process_node_evidence VALUES (?,?,?)", (domain_id, node, ref))
            saved[title] = node
            incoming[title] = upstream
        for title, names in incoming.items():
            child = saved[title]
            for upstream in names:
                source = identity.get(upstream.strip())
                if not source:
                    raise ValueError("上游工序须存在于图谱中")
                if db.execute("""
                    WITH RECURSIVE dependent(id) AS (
                        SELECT ? UNION SELECT e.downstream_id FROM process_edges e JOIN dependent d
                          ON e.upstream_id=d.id WHERE e.domain_id=?
                    ) SELECT 1 FROM dependent WHERE id=?
                """, (child, domain_id, source)).fetchone():
                    raise ValueError("提出的上下游连线会形成循环")
                db.execute("INSERT OR IGNORE INTO process_edges VALUES (?,?,?)", (domain_id, source, child))
        topology.assert_linked_dag(db, domain_id)
        return [{"id": node, "title": title} for title, node in saved.items()]

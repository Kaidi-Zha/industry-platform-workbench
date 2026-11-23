"""Domain identity, catalog matching, and transactional library management."""

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing

PAGE_SIZE = 12
DEPENDENCIES = (
    "process_nodes", "process_edges", "process_node_links", "process_node_link_audit",
    "frontier_rounds", "crawl_frontier", "entity_fingerprints",
    "frontier_candidates", "verified_references", "verified_reference_nodes",
    "process_relations", "process_participants", "process_coverage_targets",
    "benchmark_tasks", "task_references", "verifier_specs", "eval_runs", "harness_threads",
    "frontier_campaigns", "frontier_reviews", "process_node_evidence",
)


def initialize(db, legacy_names):
    db.execute("""
        CREATE TABLE IF NOT EXISTS domain_library (
            domain_id INTEGER PRIMARY KEY REFERENCES domains(id) ON DELETE CASCADE,
            description TEXT NOT NULL DEFAULT '',
            aliases TEXT NOT NULL DEFAULT '[]',
            keywords TEXT NOT NULL DEFAULT '[]',
            upstream TEXT NOT NULL DEFAULT '',
            downstream TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            merged_into INTEGER REFERENCES domains(id)
        )
    """)
    if db.execute("SELECT 1 FROM global_settings WHERE key='domain-seeds-removed'").fetchone():
        return
    # The old taxonomy inserted an empty row for each Forest category on every boot.
    tables = (*DEPENDENCIES, "raw_sources", "domain_network_approvals", "domain_attachments")
    for name in legacy_names:
        row = db.execute("SELECT id, export_override_json FROM domains WHERE name=?", (name,)).fetchone()
        if not row or row[1] != "{}":
            continue
        identity = row[0]
        if (any(db.execute(f"SELECT 1 FROM {table} WHERE domain_id=? LIMIT 1", (identity,)).fetchone()
                for table in tables)
                or _named_use(db, name)):
            continue
        db.execute("DELETE FROM domains WHERE id=?", (identity,))
    db.execute("INSERT INTO global_settings VALUES ('domain-seeds-removed', 'true')")


def _named_use(db, name):
    return db.execute("""
        SELECT 1 FROM runs WHERE json_extract(body,'$.plan.domain')=? COLLATE NOCASE
        UNION ALL SELECT 1 FROM collection_plan_revisions WHERE json_extract(body,'$.plan.domain')=? COLLATE NOCASE
        UNION ALL SELECT 1 FROM evidence_references WHERE domain=?
        UNION ALL SELECT 1 FROM industrial_cases WHERE json_extract(body,'$.domain')=? COLLATE NOCASE
        UNION ALL SELECT 1 FROM benchmark_templates WHERE json_extract(body,'$.domain')=? COLLATE NOCASE
        LIMIT 1
    """, (name,) * 5).fetchone()


def catalog(db):
    rows = db.execute("""
        SELECT d.id, d.name, coalesce(m.description,''), coalesce(m.aliases,'[]'),
            coalesce(m.keywords,'[]'), coalesce(m.upstream,''), coalesce(m.downstream,'')
        FROM domains d LEFT JOIN domain_library m ON d.id=m.domain_id
        WHERE coalesce(m.archived,0)=0 ORDER BY d.id
    """)
    return [dict(id=i, name=name, description=desc, aliases=json.loads(aliases), keywords=json.loads(keys),
                 upstream=up, downstream=down) for i, name, desc, aliases, keys, up, down in rows]


def _text(value, limit, *, required=False):
    if (not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip())
            or any(ord(c) < 32 for c in value)):
        raise ValueError("请检查领域名称、简介、别名、关键词及上下游范围")
    return value.strip()


def _terms(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("别名和关键词各可填写最多 12 条")
    return list(dict.fromkeys(_text(term, 60, required=True) for term in value))


def _normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _exact(rows, names):
    terms = {_normalized(name) for name in names} - {""}
    return [row for row in rows if terms.intersection(map(_normalized, [row["name"], *row["aliases"]]))]


def _rank(row, names, keywords):
    if _exact([row], names):
        return 1, 0, 0
    labels = list(map(_normalized, [row["name"], *row["aliases"]]))
    terms = list(map(_normalized, [*names, *keywords]))
    contained = sum(any(min(len(label), len(term)) >= 2 and (label in term or term in label)
                        for label in labels) for term in terms)
    hints = _normalized(" ".join([*row["keywords"], row["description"], row["upstream"], row["downstream"]]))
    return 0, contained, sum(len(term) >= 2 and term in hints for term in terms)


def _catalog_version(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def register(db, name, *, description="", aliases=(), keywords=(), upstream="", downstream=""):
    cursor = db.execute("INSERT INTO domains(name) VALUES (?)", (name,))
    identity = cursor.lastrowid
    db.execute("""INSERT INTO domain_library(domain_id,description,aliases,keywords,upstream,downstream)
        VALUES (?,?,?,?,?,?)""", (identity, description, json.dumps(aliases, ensure_ascii=False),
                                   json.dumps(keywords, ensure_ascii=False), upstream, downstream))
    return identity


def add(store, value):
    name = _text(value.get("name"), 100, required=True)
    if value.keys() != {"name"}:
        raise ValueError("请输入领域名称")
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if _exact(catalog(db), [name]):
            raise ValueError("该工业领域已存在")
        try:
            return {"id": register(db, name), "name": name}
        except sqlite3.IntegrityError:
            raise ValueError("该名称仍关联已删除或合并的领域，请使用其他名称") from None


class Resolution:
    """A search snapshot is required before the owner can mount or create a library."""

    def __init__(self, store, run):
        self.store = store
        self.run = run
        self.rows = None
        self.version = None
        self.seen = set()
        self.names = []
        self.keywords = []
        self.exact = []

    def search(self, name, aliases, keywords, offset=0):
        names = [_text(name, 100, required=True), *_terms(aliases)]
        keywords = _terms(keywords)
        with closing(self.store.connect()) as db:
            rows = catalog(db)
        version = _catalog_version(rows)
        if offset == 0:
            self.version, self.names, self.keywords = version, names, keywords
            self.rows = sorted(rows, key=lambda row: _rank(row, names, keywords), reverse=True)
            self.exact = _exact(rows, names)
            self.seen.clear()
        if self.version != version or names != self.names or keywords != self.keywords or offset > len(rows):
            raise ValueError("领域目录已变化或检索词已修改，请从首屏重新检索")
        page = self.rows[offset:offset + PAGE_SIZE]
        self.seen.update(row["id"] for row in page)
        return {"candidates": page, "exact_matches": [row["id"] for row in self.exact],
                "total": len(rows), "next_offset": offset + PAGE_SIZE if offset + PAGE_SIZE < len(rows) else None}

    def finish(self, *, domain_id=None, profile=None, reason):
        reason = _text(reason, 360, required=True)
        run = self.run
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.store.ensure_worker_can_write(db, run["id"])
            if mounted := run["result"].get("domain_library"):
                return mounted
            rows = catalog(db)
            if self.version != _catalog_version(rows):
                raise ValueError("请先检索现有领域，目录变化时请重新检索")
            created = False
            if domain_id is not None:
                selected = next((row for row in rows if row["id"] == domain_id and domain_id in self.seen), None)
                if not selected:
                    raise ValueError("请选择已检索的领域库")
                if self.exact and domain_id not in {row["id"] for row in self.exact}:
                    raise ValueError("请优先使用名称或别名精确命中的领域")
            else:
                name = _text(profile["name"], 100, required=True)
                description = _text(profile["description"], 320, required=True)
                aliases, keywords = _terms(profile["aliases"]), _terms(profile["keywords"])
                if not keywords:
                    raise ValueError("新建领域须提供核心关键词")
                upstream = _text(profile["upstream"], 200, required=True)
                downstream = _text(profile["downstream"], 200, required=True)
                # Newly proposed canonical name and aliases may reveal an exact match.
                matches = {row["id"]: row for row in [*self.exact, *_exact(rows, [name, *aliases])]}
                if matches:
                    raise ValueError("已有名称或别名匹配，请挂载现有库：" + "、".join(row["name"] for row in matches.values()))
                if len(self.seen) != len(rows):
                    raise ValueError("请翻阅剩余候选并完成语义判重，再创建领域")
                try:
                    identity = register(db, name, description=description, aliases=aliases, keywords=keywords,
                                        upstream=upstream, downstream=downstream)
                except sqlite3.IntegrityError:
                    raise ValueError("该名称关联已删除领域，请使用不同名称") from None
                selected = {"id": identity, "name": name}
                created = True

            # Persist the confirmed scope and the match in one transaction.
            mounted = {"id": selected["id"], "name": selected["name"], "created": created, "reason": reason}
            stored = json.loads(db.execute("SELECT body FROM runs WHERE id=?", (run["id"],)).fetchone()[0])
            stored["plan"]["domain"] = selected["name"]
            stored["result"]["domain_library"] = mounted
            text = ("已建领域资料库" if created else "已匹配并挂载至现有领域库") + "「" + selected["name"] + "」"
            stored["result"].setdefault("agent_events", []).append({"kind": "status", "text": text})
            db.execute("""UPDATE collection_plan_revisions SET body=json_set(body,'$.plan.domain',?)
                WHERE conversation_id=? AND version=?""", (selected["name"], run["conversation_id"], run["plan_version"]))
            self.store.save(db, stored)
        run["plan"]["domain"] = selected["name"]
        run["result"]["domain_library"] = mounted
        run["result"].setdefault("agent_events", []).append({"kind": "status", "text": text})
        return mounted


def _move_named_records(db, previous, name, source_id, destination_id):
    from .research import checkpoint_id

    for turn, payload in db.execute("""
        SELECT conversation_id, json_extract(body,'$.plan') FROM runs
        WHERE conversation_id IS NOT NULL AND json_extract(body,'$.plan.domain')=? COLLATE NOCASE
          AND json_extract(body,'$.research')=1
    """, (previous,)):
        scope = json.loads(payload)
        prior = checkpoint_id(turn, scope)
        scope["domain"] = name
        next_key = checkpoint_id(turn, scope)
        db.execute("INSERT OR IGNORE INTO research_checkpoints SELECT ?, body FROM research_checkpoints WHERE id=?",
                   (next_key, prior))
    for table in ("runs", "collection_plan_revisions"):
        db.execute(f"""
            UPDATE {table} SET body=json_set(body, '$.plan.domain', ?)
            WHERE json_extract(body, '$.plan.domain')=? COLLATE NOCASE
        """, (name, previous))
    db.execute("""
        UPDATE runs SET body=json_set(body, '$.result.domain_library.name', ?, '$.result.domain_library.id', ?)
        WHERE json_extract(body, '$.result.domain_library.id')=?
    """, (name, destination_id, source_id))
    # Move the conversation's workspace binding while retaining the original execution records.
    db.execute("""
        UPDATE runs SET body=json_set(body,'$.scope_domain_id',?,'$.scope_domain_name',?)
        WHERE CAST(json_extract(body,'$.scope_domain_id') AS INTEGER)=?
    """, (str(destination_id), name, source_id))
    if source_id != destination_id:
        db.execute("""
            UPDATE runs SET body=json_set(body,'$.scope_request.domain_id',?)
            WHERE CAST(json_extract(body,'$.scope_request.domain_id') AS INTEGER)=?
        """, (str(destination_id), source_id))
    db.execute("""
        UPDATE runs SET body=json_set(body,'$.benchmark.domain',?) WHERE json_extract(body,'$.benchmark.domain')=?
    """, (name, previous))
    db.execute("UPDATE evidence_references SET domain=?, body=json_set(body,'$.domain',?) WHERE domain=?",
               (name, name, previous))
    for table in ("industrial_cases", "benchmark_templates"):
        db.execute(f"UPDATE {table} SET body=json_set(body,'$.domain',?) WHERE json_extract(body,'$.domain')=?",
                   (name, previous))


def _merge(db, source, target):
    from .frontier import reconcile_domains

    identity, destination = source["id"], target["id"]
    db.execute("PRAGMA defer_foreign_keys=ON")
    reconcile_domains(db, identity, destination)
    for table in ("workflow_frontier_nodes", "workflow_node_sources", "workflow_source_passages", "workflow_export_snapshots"):
        db.execute(f"UPDATE {table} SET domain_id=? WHERE domain_id=?", (destination, identity))
    shift = db.execute("SELECT coalesce(max(position),0) FROM process_nodes WHERE domain_id=?",
                       (destination,)).fetchone()[0]
    db.execute("UPDATE process_nodes SET domain_id=?, position=position+? WHERE domain_id=?",
               (destination, shift, identity))
    for table in DEPENDENCIES[1:]:
        db.execute(f"UPDATE {table} SET domain_id=? WHERE domain_id=?", (destination, identity))
    db.execute("""INSERT OR IGNORE INTO verifier_samples SELECT ?,sha256,content FROM verifier_samples WHERE domain_id=?""",
               (destination, identity))
    db.execute("DELETE FROM verifier_samples WHERE domain_id=?", (identity,))
    db.execute("""
        INSERT INTO raw_sources(domain_id,id,canonical_url,version,sha256,status,snapshot_uri,parsed_text_uri,
                                retrieved_at,import_metadata_json)
        SELECT ?,id,canonical_url,version,sha256,status,snapshot_uri,parsed_text_uri,retrieved_at,import_metadata_json
        FROM raw_sources WHERE domain_id=?
        ON CONFLICT(domain_id,id) DO UPDATE SET status=excluded.status, sha256=excluded.sha256,
            retrieved_at=coalesce(raw_sources.retrieved_at,excluded.retrieved_at)
        WHERE excluded.status='verified' OR (excluded.status='parsed' AND raw_sources.status='discovered')
    """, (destination, identity))
    db.execute("""
        UPDATE raw_sources AS target SET import_metadata_json=source.import_metadata_json
        FROM raw_sources AS source WHERE target.domain_id=? AND source.domain_id=? AND target.id=source.id
          AND target.import_metadata_json='{}' AND source.import_metadata_json<>'{}'
    """, (destination, identity))
    db.execute("DELETE FROM raw_sources WHERE domain_id=?", (identity,))
    overrides = dict(db.execute("SELECT id, export_override_json FROM domains WHERE id IN (?,?)",
                                (identity, destination)))
    if overrides[identity] != "{}" and overrides[destination] == "{}":
        db.execute("UPDATE domains SET export_override_json=? WHERE id=?", (overrides[identity], destination))
    old_draft = f"verifier-draft/{identity}"
    new_draft = f"verifier-draft/{destination}"
    if db.execute("SELECT count(*) FROM global_settings WHERE key IN (?,?)", (old_draft, new_draft)).fetchone()[0] == 2:
        raise ValueError("两个领域均有验证器草稿，请先处理冲突")
    db.execute("UPDATE global_settings SET key=? WHERE key=?", (new_draft, old_draft))
    for namespace in ("task-value", "legacy-verifier"):
        prefix = f"{namespace}/{identity}/"
        db.execute("UPDATE global_settings SET key=? || substr(key,?) WHERE substr(key,1,?)=?",
                   (f"{namespace}/{destination}/", len(prefix) + 1, len(prefix), prefix))
    if (overrides[identity] != "{}" and overrides[destination] != "{}"
            and json.loads(overrides[identity]) != json.loads(overrides[destination])):
        raise ValueError("两个领域的导出覆写不同，请先统一再合并")
    _move_named_records(db, source["name"], target["name"], identity, destination)
    aliases = list(dict.fromkeys([*target["aliases"], source["name"], *source["aliases"]]))
    keywords = list(dict.fromkeys([*target["keywords"], *source["keywords"]]))
    db.execute("INSERT OR IGNORE INTO domain_library(domain_id) VALUES (?)", (destination,))
    db.execute("""
        UPDATE domain_library SET aliases=?, keywords=?, description=?, upstream=?, downstream=? WHERE domain_id=?
    """, (json.dumps(aliases, ensure_ascii=False), json.dumps(keywords, ensure_ascii=False),
          target["description"] or source["description"], target["upstream"] or source["upstream"],
          target["downstream"] or source["downstream"], destination))
    db.execute("UPDATE domain_library SET merged_into=? WHERE merged_into=?", (destination, identity))
    db.execute("UPDATE domain_library SET merged_into=?, archived=1 WHERE domain_id=?", (destination, identity))


def manage(store, value):
    action, identity = value.get("action"), value.get("id")
    if action not in ("rename", "merge", "delete") or type(identity) is not int or not 0 < identity < 2**53:
        raise ValueError("请选择领域资料库及操作")
    try:
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = catalog(db)
            source = next((row for row in rows if row["id"] == identity), None)
            if not source:
                raise ValueError("该领域已移除或合并")
            target = None
            if action == "merge":
                target = next((row for row in rows if row["id"] == value.get("target_id") and row["id"] != identity), None)
                if not target:
                    raise ValueError("请选择要保留的领域资料库")
            for library in [source, *([target] if target else [])]:
                if db.execute("""
                    SELECT 1 FROM runs WHERE (json_extract(body,'$.plan.domain')=? COLLATE NOCASE
                        OR CAST(COALESCE(json_extract(body,'$.scope_domain_id'),
                                         json_extract(body,'$.execution_scope.domain_id'),
                                         json_extract(body,'$.scope_request.domain_id')) AS INTEGER)=?)
                    AND status IN ('queued','running','paused','pausing','cancelling','awaiting_scope') LIMIT 1
                """, (library["name"], library["id"])).fetchone() or db.execute("""
                    SELECT 1 FROM crawl_frontier WHERE domain_id=? AND state='leased'
                    AND lease_expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now') LIMIT 1
                """, (library["id"],)).fetchone():
                    raise ValueError("该领域还有执行中、暂停或排队的任务，请先结束任务")
            db.execute("INSERT OR IGNORE INTO domain_library(domain_id) VALUES (?)", (identity,))
            if action == "rename":
                name = _text(value.get("name"), 100, required=True)
                if _exact([row for row in rows if row["id"] != identity], [name]):
                    raise ValueError("该名称已被其他领域使用，可合并至该领域")
                if name != source["name"]:
                    db.execute("UPDATE domains SET name=? WHERE id=?", (name, identity))
                    aliases = list(dict.fromkeys([source["name"], *source["aliases"]]))
                    db.execute("UPDATE domain_library SET aliases=? WHERE domain_id=?",
                               (json.dumps(aliases, ensure_ascii=False), identity))
                    _move_named_records(db, source["name"], name, identity, identity)
                return {"id": identity, "name": name}
            if target:
                _merge(db, source, target)
                return {"id": target["id"], "name": target["name"]}
            db.execute("UPDATE domain_library SET archived=1 WHERE domain_id=?", (identity,))
            return {"id": identity, "archived": True}
    except sqlite3.IntegrityError:
        raise ValueError("领域名称或记录有冲突，未保存本次变更") from None

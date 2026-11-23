"""Apply classified import records to the working library without publishing them."""

import hashlib
import json
from collections import Counter, defaultdict

from . import domain_keywords, domain_libraries, engineering_quality as quality, shared_metadata, taxonomy


def initialize(db):
    if "import_metadata_json" not in {row[1] for row in db.execute("PRAGMA table_info(raw_sources)")}:
        db.execute("ALTER TABLE raw_sources ADD COLUMN import_metadata_json TEXT NOT NULL DEFAULT '{}'")
    db.execute("""CREATE INDEX IF NOT EXISTS raw_sources_local_import ON raw_sources(domain_id,id)
        WHERE import_metadata_json<>'{}'""")
    db.execute("""CREATE TABLE IF NOT EXISTS local_import_domains (
        l2_code TEXT PRIMARY KEY, domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE)""")


def coverage(db, domain_id):
    counts = dict(db.execute("""
        SELECT stage.value,count(*) FROM raw_sources s, json_each(s.import_metadata_json,'$.lifecycle_stages') stage
        WHERE s.domain_id=? AND s.import_metadata_json<>'{}' GROUP BY stage.value
    """, (domain_id,)))
    return [{"code": code, "title": item["name_zh"], "count": counts[code]}
            for code, item in taxonomy.lifecycle().items() if code in counts]


def source_counts(db, domain_id):
    return db.execute(f"""
        WITH verified_urls AS (
            SELECT DISTINCT s.canonical_url FROM verified_references v
            JOIN raw_sources s ON (s.domain_id,s.id)=(v.domain_id,v.source_id)
            WHERE v.domain_id=? AND {quality.READY_REF}
        ) SELECT count(*), count(*) FILTER (
            WHERE lead.status NOT IN ('verified','rejected') AND verified_urls.canonical_url IS NULL
        ) FROM raw_sources lead LEFT JOIN verified_urls USING (canonical_url)
        WHERE lead.domain_id=? AND lead.import_metadata_json<>'{{}}'
    """, (domain_id, domain_id)).fetchone()


def refresh_keywords(db, domain_id):
    rows = db.execute("""SELECT json_extract(import_metadata_json,'$.title'),
        json_extract(import_metadata_json,'$.summary'),json_extract(import_metadata_json,'$.l2_code')
        FROM raw_sources WHERE domain_id=? AND import_metadata_json<>'{}' AND status<>'rejected' ORDER BY id
    """, (domain_id,)).fetchall()
    codes = Counter(code for _, _, code in rows if code)
    code = codes.most_common(1)[0][0] if codes else None
    extracted = domain_keywords.extract(((title, summary) for title, summary, _ in rows), code)
    if not extracted:
        return None
    row = db.execute("SELECT keywords FROM domain_library WHERE domain_id=?", (domain_id,)).fetchone()
    existing = json.loads(row[0]) if row else []
    keywords = domain_keywords.merge(extracted, existing)
    if keywords == existing:
        return None
    db.execute("""INSERT INTO domain_library(domain_id,keywords) VALUES (?,?)
        ON CONFLICT(domain_id) DO UPDATE SET keywords=excluded.keywords
    """, (domain_id, json.dumps(keywords, ensure_ascii=False)))
    return keywords


def backfill_keywords(db):
    domains = db.execute("""SELECT DISTINCT d.id,d.name FROM raw_sources s JOIN domains d ON d.id=s.domain_id
        LEFT JOIN domain_library l ON l.domain_id=d.id
        WHERE s.import_metadata_json<>'{}' AND coalesce(l.archived,0)=0 ORDER BY d.id
    """).fetchall()
    return [{"id": identity, "name": name, "keywords": keywords} for identity, name in domains
            if (keywords := refresh_keywords(db, identity)) is not None]


def page(db, domain_id, query, offset, *, item, stage):
    stages = taxonomy.lifecycle()
    if stage and stage not in stages:
        raise ValueError("请选择有效的生命周期阶段")
    rows = db.execute(f"""
        WITH page AS (
        SELECT s.id,s.canonical_url AS url,
            json_extract(s.import_metadata_json,'$.title') AS title,
            json_extract(s.import_metadata_json,'$.summary') AS summary,
            json_extract(s.import_metadata_json,'$.object_cluster') AS object_cluster,
            json_extract(s.import_metadata_json,'$.lifecycle_stages') AS lifecycle,
            CASE WHEN s.status='discovered' THEN 'needs_review' ELSE s.status END AS state
        FROM raw_sources s WHERE s.domain_id=? AND s.import_metadata_json<>'{{}}'
          AND (?='' OR s.id=?) AND (?='' OR EXISTS (
              SELECT 1 FROM json_each(s.import_metadata_json,'$.lifecycle_stages') WHERE value=?
          )) AND (?='' OR instr(lower(s.canonical_url || ' ' || title || ' ' || summary || ' ' || object_cluster),lower(?))>0)
        ORDER BY s.id LIMIT 31 OFFSET ?
        ) SELECT page.*, (SELECT v.id FROM raw_sources pinned
            JOIN verified_references v ON (v.domain_id,v.source_id)=(pinned.domain_id,pinned.id)
            WHERE pinned.domain_id=? AND pinned.canonical_url=page.url AND {quality.READY_REF}
            ORDER BY v.verified_at DESC,v.id LIMIT 1) AS reference_id
        FROM page ORDER BY id
    """, (domain_id, item, item, stage, stage, query, query, offset, domain_id)).fetchall()
    items = []
    for row in rows[:30]:
        record = dict(row)
        if record["reference_id"]:
            record["state"] = "verified"
        record["lifecycle"] = [{"code": code, "title": stages[code]["name_zh"]}
                               for code in json.loads(record["lifecycle"]) if code in stages]
        items.append(record)
    return {"items": items, "more": len(rows) > 30, "channels": [], "stage_title": stages[stage]["name_zh"] if stage else ""}


def domain_scopes(db, code, catalog):
    active = {item["id"]: item for item in catalog}
    binding = db.execute("SELECT domain_id FROM local_import_domains WHERE l2_code=?", (code,)).fetchone()
    bound = binding[0] if binding else None
    while bound and bound not in active:
        merged = db.execute("SELECT merged_into FROM domain_library WHERE domain_id=?", (bound,)).fetchone()
        bound = merged[0] if merged else None
    industry = taxonomy.registry()[1][code]
    matches = domain_libraries._exact(catalog, [industry["name_zh"], industry["english_id"]])
    primary = active.get(bound) or (matches[0] if len(matches) == 1 else None)
    known = {row[0] for row in db.execute("""SELECT DISTINCT CAST(coalesce(json_extract(body,'$.scope_domain_id'),
        json_extract(body,'$.result.domain_library.id')) AS INTEGER) FROM runs
        WHERE json_extract(body,'$.task_taxonomy.l2_code')=?""", (code,)) if row[0] in active}
    known.update(item["id"] for item in matches)
    if primary:
        known.add(primary["id"])
    return primary, known


def materialize(db, batch_id, records, *, related=None):
    """Within an industry, preserve the first entry and its established provenance."""
    catalog = domain_libraries.catalog(db)
    by_code = defaultdict(list)
    for item in records:
        by_code[taxonomy.require_l2(item["l2_code"])].append(item)
    receipts = {}
    enriched_domains = set()
    for code, group in sorted(by_code.items()):
        target, known = domain_scopes(db, code, catalog)
        allowed = {item["id"] for item in catalog}
        if related:
            known.update(related.get(code, set()) & allowed)
        active = {item["id"]: item for item in catalog}
        existing = {}
        for identity in sorted(known, key=lambda item: (target is None or item != target["id"], item)):
            for (url,) in db.execute("SELECT DISTINCT canonical_url FROM raw_sources WHERE domain_id=?", (identity,)):
                try:
                    key = shared_metadata.canonical_key("sources", url)
                except ValueError:
                    continue
                existing.setdefault(key, identity)
        for item in group:
            url = item["canonical_key"]
            if url in existing:
                destination = active[existing[url]]
                added = 0
            else:
                if target is None:
                    name = taxonomy.registry()[1][code]["name_zh"]
                    identity = domain_libraries.register(db, name, description="本地资料导入登记的来源与工业分类。")
                    target = {"id": identity, "name": name}
                    active[identity] = target
                    catalog.append({**target, "aliases": []})
                destination = target
                identity = destination["id"]
                attributes = {"batch_id": batch_id, "title": item["title"], "summary": item["payload"]["summary_zh"],
                              "l2_code": code, "object_cluster": item["object_cluster"],
                              "lifecycle_stages": item["lifecycle_stages"]}
                source_id = hashlib.sha256((url + "\n").encode()).hexdigest()
                source = db.execute("""INSERT OR IGNORE INTO raw_sources(domain_id,id,canonical_url,status,import_metadata_json)
                    VALUES (?,?,?,'discovered',?)""", (identity, source_id, url,
                                                        json.dumps(attributes, ensure_ascii=False)))
                added = source.rowcount
                existing[url] = identity
                if added:
                    enriched_domains.add(identity)
            receipt = receipts.setdefault((code, destination["id"]), {"id": destination["id"], "name": destination["name"],
                                                                      "l2_code": code, "added": 0, "duplicates": 0})
            receipt["added"] += added
            receipt["duplicates"] += 1 - added
        if target:
            db.execute("""INSERT INTO local_import_domains(l2_code,domain_id) VALUES (?,?) ON CONFLICT(l2_code)
                DO UPDATE SET domain_id=excluded.domain_id""", (code, target["id"]))
    for identity in sorted(enriched_domains):
        refresh_keywords(db, identity)
    return list(receipts.values())

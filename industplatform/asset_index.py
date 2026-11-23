"""Cross-domain metadata index. Asset bodies stay in their owning workspace."""

from contextlib import closing
import json
import re
import sqlite3

from . import engineering_quality as quality, library

KINDS = ("sources", "tasks", "verifiers", "evaluations")
FILTERS = ("stage", "authority", "difficulty")
PAGE_SIZE = 30

# Existing archives predate the normalized workflow tables. Keep their run and
# task identifiers so index entries open the original record.
LEGACY_TASKS = """
    SELECT r.id run_id, t.value body, json_extract(t.value,'$.id') task_id,
        json_extract(t.value,'$.title') title, r.status,
        coalesce(nullif(json_extract(r.body,'$.plan.domain'),''),
                 nullif(json_extract(r.body,'$.benchmark.domain'),''),
                 json_extract(t.value,'$.template.domain'), '') domain_name,
        coalesce(json_extract(t.value,'$.stage'), json_extract(t.value,'$.workflow.stage'), '') stage,
        coalesce(json_extract(t.value,'$.measured_difficulty'), json_extract(t.value,'$.difficulty'), '') difficulty,
        coalesce(json_extract(r.body,'$.updated_at'),json_extract(r.body,'$.created_at')) updated_at, r.body run_body
    FROM runs r, json_each(r.body,'$.result.tasks') t WHERE json_type(t.value,'$.id')='text'
"""

RECORDS = {
    "sources": f"""
        SELECT v.id, v.domain_id, v.title, p.title stage,
            v.authority_tier authority, '' difficulty,
            CASE WHEN w.quality_note<>'' THEN 'filtered' WHEN {quality.READY_REF} THEN 'verified' ELSE 'legacy_review' END status,
            v.verified_at updated_at, CASE WHEN {quality.READY_REF} THEN 'reference' ELSE 'frontier' END origin,
            s.canonical_url subtitle, NULL run_id
        FROM verified_references v JOIN raw_sources s ON (s.domain_id,s.id)=(v.domain_id,v.source_id)
        LEFT JOIN process_nodes p ON (p.domain_id,p.id)=(v.domain_id,v.node_id)
        LEFT JOIN workflow_source_passages w ON (w.domain_id,w.node_id,w.source_id)=(v.domain_id,v.node_id,v.source_id)
        UNION ALL
        SELECT e.id,d.id,json_extract(e.body,'$.title'),json_extract(e.body,'$.stage'),NULL,'',
            CASE WHEN json_type(e.body,'$.verification')='object' THEN 'legacy_review' ELSE 'discovered' END,
            json_extract(e.body,'$.updated_at'),'frontier',
            json_extract(e.body,'$.url'),NULL
        FROM evidence_references e JOIN domains d ON e.domain=d.name
        UNION ALL
        SELECT s.id,s.domain_id,coalesce(json_extract(s.import_metadata_json,'$.title'),s.canonical_url),
            (SELECT p.title FROM process_nodes p JOIN workflow_node_sources w
             ON (p.domain_id,p.id)=(w.domain_id,w.node_id)
             WHERE (w.domain_id,w.source_id)=(s.domain_id,s.id) ORDER BY p.position LIMIT 1),
            '','',CASE WHEN s.status='discovered' AND json_type(s.import_metadata_json,'$.batch_id')='text'
                       THEN 'needs_review' ELSE s.status END,s.retrieved_at,
            CASE WHEN s.import_metadata_json<>'{{}}' THEN 'import' ELSE 'frontier' END,
            CASE WHEN json_type(s.import_metadata_json,'$.batch_id')='text' THEN s.canonical_url ELSE s.version END,NULL
        FROM raw_sources s WHERE NOT EXISTS (
            SELECT 1 FROM crawl_frontier f WHERE (f.domain_id,f.source_id)=(s.domain_id,s.id))
        UNION ALL
        SELECT f.id,f.domain_id,f.target,p.title,'','',f.state,s.retrieved_at,'frontier',c.title,NULL
        FROM crawl_frontier f JOIN process_nodes p ON (p.domain_id,p.id)=(f.domain_id,f.node_id)
        JOIN crawl_channels c ON c.id=f.channel_id
        LEFT JOIN raw_sources s ON (s.domain_id,s.id)=(f.domain_id,f.source_id)
        UNION ALL
        SELECT r.id || ':' || json_extract(a.value,'$.id'), d.id, json_extract(a.value,'$.title'),
            json_extract(a.value,'$.target.stage'), '', '', r.status,
            json_extract(r.body,'$.created_at'), 'archive', json_extract(a.value,'$.source'), r.id
        FROM runs r JOIN json_each(r.body,'$.result.assets') a
        LEFT JOIN domains d ON d.name=json_extract(r.body,'$.plan.domain')
        WHERE json_extract(a.value,'$.data_origin') IS NOT 'synthetic'
            AND json_extract(a.value,'$.source') LIKE 'https://%'
    """,
    "tasks": """
        SELECT t.id,t.domain_id,t.title,p.title stage,'' authority,
            coalesce((SELECT e.difficulty FROM eval_runs e WHERE (e.domain_id,e.task_id)=(t.domain_id,t.id)
                    AND e.is_stale=0 AND e.status='completed' ORDER BY e.rowid DESC LIMIT 1),t.complexity) difficulty,
            CASE WHEN t.is_stale=1 THEN 'stale' WHEN t.quality_approved=0 THEN 'legacy_review' ELSE t.state END status,NULL updated_at,
            'task' origin,json_extract(t.definition_json,'$.description') subtitle,NULL run_id
        FROM benchmark_tasks t JOIN process_nodes p ON (p.domain_id,p.id)=(t.domain_id,t.node_id)
        WHERE t.state<>'archived'
        UNION ALL
        SELECT l.run_id || ':' || l.task_id, d.id, l.title, l.stage, '', l.difficulty, l.status, l.updated_at,
            'archive', json_extract(l.body,'$.description'), l.run_id
        FROM legacy l LEFT JOIN domains d ON l.domain_name=d.name
    """,
    "verifiers": """
        SELECT t.id,t.domain_id,t.title,p.title stage,'' authority,t.complexity difficulty,
            CASE WHEN v.is_stale=1 THEN 'stale' ELSE 'registered' END status,NULL updated_at,
            'verifier' origin,'v' || v.version subtitle,NULL run_id
        FROM verifier_specs v JOIN benchmark_tasks t ON (t.domain_id,t.id)=(v.domain_id,v.task_id)
        JOIN process_nodes p ON (p.domain_id,p.id)=(t.domain_id,t.node_id)
        WHERE t.state<>'archived' AND v.version=(SELECT max(version) FROM verifier_specs
            WHERE (domain_id,task_id)=(v.domain_id,v.task_id))
        UNION ALL
        SELECT l.run_id || ':' || l.task_id,d.id,l.title,l.stage,'',l.difficulty,'registered',l.updated_at,
            CASE WHEN g.key IS NULL THEN 'archive' ELSE 'verifier' END,
            coalesce('v' || json_extract(g.value_json,'$.version'), json_extract(l.body,'$.verifier.id')),l.run_id
        FROM legacy l LEFT JOIN domains d ON l.domain_name=d.name
        LEFT JOIN global_settings g ON g.key='legacy-verifier/' || d.id || '/' || l.run_id || ':' || l.task_id
        WHERE g.key IS NOT NULL OR json_type(l.body,'$.verifier') IN ('object','text')
    """,
    "evaluations": """
        SELECT e.id,e.domain_id,t.title,p.title stage,'' authority,e.difficulty,
            CASE WHEN e.is_stale=1 THEN 'stale' ELSE e.status END status,e.confirmed_at updated_at,
            'evaluation' origin, 'Pass@' || e.pass_at_k subtitle,NULL run_id
        FROM eval_runs e JOIN benchmark_tasks t ON (t.domain_id,t.id)=(e.domain_id,e.task_id)
        JOIN process_nodes p ON (p.domain_id,p.id)=(t.domain_id,t.node_id)
        WHERE t.state<>'archived'
        UNION ALL
        SELECT l.run_id || ':' || l.task_id,d.id,l.title,l.stage,'',l.difficulty,l.status,l.updated_at,
            'archive','',l.run_id
        FROM legacy l LEFT JOIN domains d ON l.domain_name=d.name
        WHERE EXISTS(SELECT 1 FROM json_each(l.run_body,'$.result.evaluations') e
            WHERE json_extract(e.value,'$.task_id')=l.task_id)
    """,
}


def read(store, kind, *, domain=None, query="", offset=0, filters=None):
    if kind not in KINDS or not 0 <= offset <= 2**31 - 1:
        raise ValueError("资源查询参数无效")
    filters = filters or {}
    sql = f"""
        WITH legacy AS ({LEGACY_TASKS}), assets AS ({RECORDS[kind]}), visible AS (
            SELECT a.*, d.name domain_name FROM assets a LEFT JOIN domains d ON d.id=a.domain_id
            LEFT JOIN domain_library m ON m.domain_id=d.id
            WHERE coalesce(m.archived,0)=0 AND (? IS NULL OR a.domain_id=?)
        )
    """
    where = ["instr(lower(concat_text), lower(?))>0" for _ in query.split()]
    args = [domain, domain, *query.split()]
    for key in FILTERS:
        if filters.get(key):
            where.append(f"{key}=?")
            args.append(filters[key])
    with closing(store.connect()) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(sql + """
            SELECT * FROM (SELECT *, coalesce(title,'') || ' ' || coalesce(domain_name,'') || ' '
                || coalesce(stage,'') || ' ' || coalesce(subtitle,'') || ' ' || id AS concat_text FROM visible)
        """ + (" WHERE " + " AND ".join(where) if where else "") + """
            ORDER BY updated_at DESC NULLS LAST, domain_id, id, origin LIMIT ? OFFSET ?
        """, (*args, PAGE_SIZE + 1, offset)).fetchall()
        options = {key: [row[0] for row in db.execute(sql + f"""
            SELECT DISTINCT {key} FROM visible WHERE {key} IS NOT NULL AND {key}<>'' ORDER BY {key}
        """, (domain, domain))] for key in FILTERS}
    return {"items": [{key: row[key] for key in row.keys() if key != "concat_text"} for row in rows[:PAGE_SIZE]],
            "more": len(rows) > PAGE_SIZE, "options": options}


def evaluation(store, domain, identity):
    if not re.fullmatch(r"[\w.-]{1,160}", identity):
        raise ValueError("评测编号无效")
    with closing(store.connect()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT e.id,e.status,e.task_id,e.difficulty,e.verifier_version,e.confirmed_at,
            e.pass_at_k,e.is_stale,e.stale_reason,e.results_json,t.title
            FROM eval_runs e JOIN benchmark_tasks t ON (t.domain_id,t.id)=(e.domain_id,e.task_id)
            WHERE e.domain_id=? AND e.id=? AND t.state<>'archived'""", (domain, identity)).fetchone()
    if not row:
        raise ValueError("当前领域没有此评测记录")
    output = dict(row)
    trials = json.loads(output.pop("results_json")).get("trials", [])
    output["trials"] = [{key: value for key, value in trial.items() if key in library.EVALUATION_FIELDS} for trial in trials]
    return output

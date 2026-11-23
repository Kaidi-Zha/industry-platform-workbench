"""Review and export metadata from one completed scoped agent run."""

import argparse
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

try:
    from .repo_support import Repository, fail, safe_path
    from .data_policy import check_content, metadata, taxonomy
    from .sync_meta_to_local import database_path
except ImportError:
    from repo_support import Repository, fail, safe_path
    from data_policy import check_content, metadata, taxonomy
    from sync_meta_to_local import database_path

from industplatform import reference_catalog as catalog


def closed_outputs(db, run_id):
    row = db.execute("SELECT status,body FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise ValueError("找不到任务")
    status, body = row
    run = json.loads(body)
    scope = run.get("execution_scope", {})
    queue = scope.get("stage_queue") or []
    if (status != "completed" or not run.get("scoped_workflow") or scope.get("state") != "completed"
            or run.get("result", {}).get("scope_continue") or (queue and scope.get("stage_index", 0) + 1 != len(queue))):
        raise ValueError("任务尚未跑完所选阶段，继续在本地保留草稿")
    domain = run.get("scope_domain_id") or scope.get("domain_id")
    domain_row = db.execute("SELECT name FROM domains WHERE id=?", (domain,)).fetchone()
    if not domain_row:
        raise ValueError("已完成任务没有绑定领域")
    output_column = ("a.output_id" if "output_id" in {
        column[1] for column in db.execute("PRAGMA table_info(workflow_scope_audit)")
    } else "NULL")
    outputs = defaultdict(list)
    for tool, entity, detail, output_id in db.execute(f"""SELECT a.tool,a.entity_id,a.detail,{output_column}
        FROM workflow_scope_audit a JOIN workflow_scope_contracts c ON c.id=a.contract_id
        WHERE c.run_id=? AND c.state='completed' AND a.kind='applied' ORDER BY a.sequence""", (run_id,)):
        outputs[tool].append((entity, detail, output_id))
    return domain, domain_row[0], outputs, run.get("task_taxonomy")


def task_records(db, run_id, label=None, *, retired=None):
    domain, domain_name, outputs, saved_label = closed_outputs(db, run_id)
    if label is not None:
        label = taxonomy.classification(label)
    if saved_label is not None:
        saved_label = taxonomy.classification(saved_label)
        if label is not None and label != saved_label:
            raise ValueError("导出参数与任务已确认的分类不一致")
        label = saved_label
    if label is None:
        raise ValueError("任务尚未记录分类，请提供 --l2-code、--object-cluster 与 --lifecycle-stages")
    records: dict[str, dict] = {}

    def add(kind, key, title, payload, record_label=label):
        value = metadata.new_record(record_label, kind, key, title, run_id, {"domain_name": domain_name, **payload})
        path = metadata.record_path(value)
        check_content(path, metadata.json_bytes(value))
        records.setdefault(path, value)

    nodes = dict(db.execute("SELECT id,title FROM process_nodes WHERE domain_id=?", (domain,)))
    if retired is not None:
        for identity, detail, _ in outputs["delete_process_node"]:
            if identity not in nodes and detail.startswith("删除无引用工序「") and detail.endswith("」"):
                key = metadata.canonical_key("processes", detail.removeprefix("删除无引用工序「")[:-1])
                retired.add(metadata.record_path({**label, "kind": "processes", "canonical_id":
                    metadata.canonical_id(label["l2_code"], "processes", key, label["object_cluster"])}))
    for identity in sorted({item[0] for tool in ("upsert_process_node", "upsert_process_edge") for item in outputs[tool]}):
        if identity not in nodes:
            continue
        (roles,) = db.execute("SELECT occupations_json FROM process_nodes WHERE domain_id=? AND id=?",
                              (domain, identity)).fetchone()
        edges = db.execute("""SELECT upstream_id,downstream_id FROM process_edges
            WHERE domain_id=? AND ? IN (upstream_id,downstream_id) ORDER BY upstream_id,downstream_id""",
                           (domain, identity)).fetchall()
        add("processes", nodes[identity], nodes[identity],
            {"occupations": json.loads(roles), "upstream": [nodes[first] for first, last in edges if last == identity],
             "downstream": [nodes[last] for first, last in edges if first == identity]})

    source_ids = {output for _, _, output in outputs["save_raw_source"] if output}
    source_urls = {detail[len("登记原文 "):] for _, detail, output in outputs["save_raw_source"]
                   if not output and detail.startswith("登记原文 ")}
    for identity, url, version, sha in db.execute("""SELECT id,canonical_url,version,sha256 FROM raw_sources
        WHERE domain_id=? AND status IN ('parsed','verified') ORDER BY retrieved_at DESC,id""", (domain,)):
        if identity not in source_ids and url not in source_urls:
            continue
        assets = [json.loads(row[0]) for row in db.execute("""SELECT artifact_json FROM workflow_source_passages
            WHERE domain_id=? AND source_id=? AND artifact_json<>'{}' ORDER BY node_id""", (domain, identity))]
        add("sources", url, url[:220], {"url": url, "source_version": version, "sha256": sha,
                                       "artifact": assets[0] if assets else {}})

    ref_ids = {output for _, _, output in outputs["promote_verified_ref"] if output}
    old_refs = {(node, detail) for node, detail, output in outputs["promote_verified_ref"] if not output}
    documents = {}
    for identity, node, title, body, url, sha, permission, tier, channel, cluster, stages in db.execute("""
        SELECT v.id,v.node_id,v.title,v.quality_json,s.canonical_url,s.sha256,v.permission,
               v.authority_tier,v.source_channel,v.object_cluster,v.lifecycle_stages_json
        FROM verified_references v JOIN raw_sources s
        ON (v.domain_id,v.source_id)=(s.domain_id,s.id) WHERE v.domain_id=? AND v.permission<>'restricted'
        AND json_array_length(v.quality_json,'$.extracted_clauses')>0 ORDER BY v.verified_at DESC,v.id""", (domain,)):
        if identity in ref_ids or (node, f"核验技术文献「{title}」") in old_refs:
            key = metadata.canonical_key("literature", url)
            document = json.loads(body)
            # Preserve locator and engineering checks; keep the original text at its publisher.
            for clause in document["extracted_clauses"]:
                clause.pop("quote", None)
            linked = catalog.linked_ids(db, domain, identity)
            reference_label = {**label, "object_cluster": cluster or label["object_cluster"],
                               "lifecycle_stages": json.loads(stages) if stages else label["lifecycle_stages"]}
            if key not in documents:
                documents[key] = (title, reference_label, {**document, "url": url, "sha256": sha,
                    "permission": permission, "authority_tier": tier, "source_channel": channel,
                    "process_node_ids": linked, "process": nodes.get(node)})
            else:
                _, saved_label, saved = documents[key]
                if (saved["sha256"], saved["authority_tier"], saved["source_channel"]) != (sha, tier, channel):
                    continue
                saved["process_node_ids"] = list(dict.fromkeys([*saved["process_node_ids"], *linked]))
                known = {clause["id"] for clause in saved["extracted_clauses"]}
                saved["extracted_clauses"].extend(clause for clause in document["extracted_clauses"] if clause["id"] not in known)
                saved_label["lifecycle_stages"] = sorted(set(saved_label["lifecycle_stages"]) | set(reference_label["lifecycle_stages"]))
    for key, (title, reference_label, document) in documents.items():
        document["process_node_ids"] = document["process_node_ids"] or [catalog.UNASSIGNED]
        add("literature", key, title, document, record_label=reference_label)

    changed_relations = {(node, output or detail.removesuffix(" · 待文献核验"))
                         for node, detail, output in outputs["upsert_process_relation"]}
    for node, identity, sender, recipient, deliverable, kind, clause, locator, url in db.execute(
            """SELECT node_id,id,sender,recipient,deliverable,kind,evidence_clause_id,locator,url
            FROM grounded_process_relations WHERE domain_id=? ORDER BY id""", (domain,)):
        if (node, identity) in changed_relations or (node, sender + " → " + recipient) in changed_relations:
            key = json.dumps([nodes[node], sender, recipient, deliverable, kind], ensure_ascii=False)
            add("entities", key, f"{sender} → {recipient}"[:220], {"process": nodes[node], "sender": sender,
                "recipient": recipient, "deliverable": deliverable, "relationship": kind,
                "evidence_url": url, "locator": locator, "clause_id": clause})

    participants = {(node, detail) for node, detail, _ in outputs["upsert_process_node"]}
    for node, name, role, url, locator in db.execute("""SELECT p.node_id,p.name,p.role,s.canonical_url,v.locator
        FROM process_participants p JOIN verified_references v
        ON (p.domain_id,p.reference_id)=(v.domain_id,v.id) JOIN raw_sources s
        ON (v.domain_id,v.source_id)=(s.domain_id,s.id) WHERE p.domain_id=? ORDER BY p.node_id,p.name,p.role""", (domain,)):
        if (node, f"登记企业「{name}」") in participants:
            add("entities", json.dumps([nodes[node], name, role], ensure_ascii=False), name,
                {"process": nodes[node], "name": name, "role": role, "evidence_url": url, "locator": locator})

    task_ids = {entity for entity, _, _ in outputs["upsert_benchmark_task"]}
    for identity, node, title, body, complexity in db.execute("""SELECT id,node_id,title,definition_json,complexity
        FROM benchmark_tasks WHERE domain_id=? AND quality_approved=1 AND is_stale=0 AND state='ready'
        ORDER BY id""", (domain,)):
        if identity not in task_ids:
            continue
        definition = json.loads(body)
        # Only engineering contracts and artifact locators are public task metadata.
        keys = ("description", "constraints", "expected_output", "industrial_context", "boundary_conditions",
                "edge_cases_and_rejection", "downstream_handoff", "deliverables", "value_assessment", "input_artifacts")
        facts = {key: definition[key] for key in keys if key in definition}
        citations = [dict(zip(("title", "url", "locator", "permission"), row)) for row in db.execute("""
            SELECT v.title,s.canonical_url,v.locator,v.permission FROM task_references t
            JOIN verified_references v ON (t.domain_id,t.reference_id)=(v.domain_id,v.id) JOIN raw_sources s
            ON (v.domain_id,v.source_id)=(s.domain_id,s.id) WHERE t.domain_id=? AND t.task_id=? ORDER BY v.id""",
            (domain, identity))]
        add("tasks", json.dumps([nodes[node], title], ensure_ascii=False), title,
            {"process": nodes[node], "complexity": complexity, **facts, "citations": citations})
    return records


def export(repo, database, run_id, label=None, *, apply=False):
    if not metadata.BATCH.fullmatch(run_id):
        raise ValueError("请指定有效的任务 ID")
    if not database.is_file():
        raise ValueError("工作台数据库不存在")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        records = task_records(db, run_id, label)
    if not records:
        raise ValueError("本次完成的任务没有可共享的条目")
    if apply and not repo.branch().startswith("data/"):
        raise ValueError("请从最新 main 创建 data/ 分支后导出")
    existing = repo.tree(repo.revision("HEAD"), metadata.PREFIX)
    known_main = repo.revision("origin/main", optional=True)
    if known_main:
        if apply and repo.merge_base(known_main, repo.revision("HEAD")) != known_main:
            raise ValueError("当前分支落后于 origin/main，请先更新")
        existing.update(repo.tree(known_main, metadata.PREFIX))
    root = repo.root if apply else repo.root / ".staging" / "exports" / run_id
    planned = {}
    for path, record in records.items():
        target = safe_path(root, path)
        if path not in existing:
            planned[target] = metadata.json_bytes(record)
    created = []
    try:
        for target, data in planned.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                created.append(target)
                stream.write(data)
    except OSError:
        for target in created:
            target.unlink()
        raise
    return len(created), len(records) - len(created), root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--l2-code", "--domain-id", dest="l2_code", help="official L2 code, e.g. D01.2")
    parser.add_argument("--object-cluster")
    parser.add_argument("--lifecycle-stages", nargs="+")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--apply", action="store_true", help="write reviewed records to the data branch")
    args = parser.parse_args()
    try:
        repo = Repository()
        fields = {key: getattr(args, key) for key in taxonomy.FIELDS}
        label = fields if any(value is not None for value in fields.values()) else None
        count, duplicates, root = export(repo, database_path(repo, args.db), args.run_id, label, apply=args.apply)
        print(f"导出 {count} 条，跳过 {duplicates} 条同键记录 · {root}")
    except (OSError, ValueError, sqlite3.Error, KeyError, RuntimeError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

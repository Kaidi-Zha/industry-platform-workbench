"""Read public evidence and task records from the run archive."""

import json
from contextlib import closing
from urllib.parse import urlsplit

from .storage import AGENT_ROUTES
from .workflow import benchmark_ready


KINDS = ("sources", "datasets", "tasks")
ASSET_FIELDS = (
    "id", "title", "source", "family", "version", "version_source",
    "license", "license_source", "license_basis", "license_sha256",
    "route", "status", "fields", "summary", "declared_files", "data_origin", "format",
)
EVIDENCE_FIELDS = ("file", "url", "format", "fields", "field_count", "rows", "bytes", "sha256", "extent")
TASK_FIELDS = (
    "id", "title", "difficulty", "measured_difficulty", "description",
    "checks", "inputs", "rule_ids", "data_origin", "verification", "author", "template",
    "complexity", "references", "workflow", "derivation", "fields", "variant", "verifier",
)
EVALUATION_FIELDS = ("task_id", "tier", "model", "provider", "profile", "profile_id",
                     "reasoning_effort", "trial", "status", "passed", "reason", "checks", "latency_ms")


def _select(record, fields):
    return {key: record[key] for key in fields if key in record}


def _url(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        location = urlsplit(value)
        return location.scheme == "https" and bool(location.hostname) and location.username is None
    except ValueError:
        return False


def _provenance(runs):
    parents = {}
    for run in runs:
        if run.get("route") in AGENT_ROUTES:
            for child_id in run.get("result", {}).get("agent_run_ids", []):
                parents.setdefault(child_id, run)
    return parents


def _row(run, parent, item_id, title, status, summary, meta, links, details):
    details.update(route=run.get("route", ""), created_at=run.get("created_at", ""))
    row = {
        "id": item_id,
        "title": title,
        "run_id": run["id"],
        "status": status,
        "summary": summary,
        "meta": [value for value in meta if isinstance(value, str) and value],
        "links": links,
        "details": details,
        "agent_triggered": parent is not None or run.get("route") in AGENT_ROUTES,
    }
    if parent is not None:
        details["agent_run_id"] = parent["id"]
        row["conversation_id"] = parent.get("conversation_id") or parent["id"]
    elif run.get("route") in AGENT_ROUTES:
        row["conversation_id"] = run.get("conversation_id") or run["id"]
    return row


def _sample(asset):
    evidence = asset.get("evidence")
    if not isinstance(evidence, dict):
        return None
    if not (evidence.get("file") and evidence.get("fields") and evidence.get("sha256")):
        return None
    size = evidence.get("bytes")
    if type(size) is not int or size <= 0:
        return None
    return _select(evidence, EVIDENCE_FIELDS)


def _asset_rows(runs, parents, kind):
    records = {}
    for run in runs:
        for asset in run.get("result", {}).get("assets", []):
            asset_id = asset.get("id")
            if not asset_id:
                continue
            sample = _sample(asset)
            synthetic = asset.get("data_origin") == "synthetic"
            public = _url(asset.get("source"))
            if kind == "sources" and (synthetic or not public):
                continue
            if kind == "datasets" and not (synthetic or (public and sample)):
                continue
            identity = (asset_id, asset.get("version"))
            previous = records.get(identity)
            if previous is not None:
                previous["details"]["observations"] += 1
                if previous["status"] != "candidate" or sample is None:
                    continue

            status = "synthetic" if synthetic else "sampled" if sample else "candidate"
            details = _select(asset, ASSET_FIELDS)
            details["observations"] = previous["details"]["observations"] if previous else 1
            if sample is not None:
                details["evidence"] = sample
            if "run_evidence" in asset:
                details["run_evidence"] = _select(asset["run_evidence"], ("downloaded_bytes", "metadata_failures"))
            if asset.get("target"):
                details["target"] = _select(asset["target"], ("peu", "stage", "reason"))
            locations = [
                ("来源", asset.get("source")), ("版本依据", asset.get("version_source")),
                ("许可依据", asset.get("license_source")),
            ]
            if sample:
                locations.append(("样本文件", sample.get("url")))
            links = [{"label": label, "url": url} for label, url in locations if _url(url)]
            meta = [
                "合成数据" if synthetic else asset.get("route"),
                asset.get("version"), asset.get("license"),
            ]
            if sample:
                meta.append("{} B · {} 个字段".format(sample["bytes"], sample.get("field_count") or len(sample["fields"])))
            records[identity] = _row(
                run, parents.get(run["id"]), asset_id, asset.get("title") or asset_id,
                status, asset.get("summary") or "", meta, links, details,
            )
    return records.values()


def _task_rows(runs, parents):
    for run in runs:
        result = run.get("result", {})
        evaluations = {}
        for evaluation in result.get("evaluations", []):
            evaluations.setdefault(evaluation.get("task_id"), []).append(_select(evaluation, EVALUATION_FIELDS))
        exportable = benchmark_ready(run)
        links = [{"label": "下载 Benchmark", "url": "/api/runs/{}/benchmark".format(run["id"])}] if exportable else []
        for task in result.get("tasks", []):
            task_id = task.get("id")
            if not task_id:
                continue
            details = _select(task, TASK_FIELDS)
            details["evaluations"] = evaluations.get(task_id, [])
            details["cohort"] = run.get("cohort", "")
            if run.get("benchmark"):
                details["benchmark_case"] = run["benchmark"]
            description = task.get("description") or ""
            row = _row(
                run, parents.get(run["id"]), "{}:{}".format(run["id"], task_id), task.get("title") or task_id,
                task.get("verification") or run["status"],
                description.split("\n\n", 1)[0],
                [task.get("difficulty"), task.get("measured_difficulty"),
                 "合成数据" if task.get("data_origin") == "synthetic" else None],
                links, details,
            )
            row["exportable"] = exportable
            yield row


def _matches(row, terms):
    details = {name: value for name, value in row["details"].items() if name != "inputs"}
    searchable = json.dumps({**row, "details": details}, ensure_ascii=False).casefold()
    return all(term in searchable for term in terms)


def _archive(store, domain):
    with closing(store.connect()) as connection:
        runs = [json.loads(body) for (body,) in connection.execute("SELECT body FROM runs ORDER BY sequence DESC")]
    parents = _provenance(runs)
    if domain:
        def in_domain(run):
            parent = parents.get(run["id"], {})
            scope = run.get("plan") or parent.get("plan") or {}
            name = scope.get("domain") or run.get("benchmark", {}).get("domain")
            if not name:
                tasks = run.get("result", {}).get("tasks", [])
                name = next((task["template"]["domain"] for task in tasks if task.get("template", {}).get("domain")), "")
            return name.casefold() == domain.casefold()

        runs = [run for run in runs if in_domain(run)]
    return runs, parents


def task_bindings(store, domain):
    runs, _ = _archive(store, domain)
    return [dict(id=f"{run['id']}:{task['id']}", title=task.get("title") or str(task["id"]))
            for run in runs for task in run.get("result", {}).get("tasks", []) if task.get("id")]


def read(store, kind, query="", offset=0, limit=30, *, domain=""):
    """Return a filtered, paginated view using the full archive in newest-first order."""
    if kind not in KINDS:
        raise ValueError("未知资源类型")
    if not isinstance(query, str) or len(query) > 280:
        raise ValueError("检索词上限为 280 字符")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset 必须为非负整数")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1 至 100 的整数")

    runs, parents = _archive(store, domain)
    rows = _task_rows(runs, parents) if kind == "tasks" else _asset_rows(runs, parents, kind)
    terms = query.casefold().split()
    items, total = [], 0
    for row in rows:
        if terms and not _matches(row, terms):
            continue
        if offset <= total < offset + limit:
            items.append(row)
        total += 1
    if kind == "tasks" and domain and items:
        with closing(store.connect()) as db:
            owner = db.execute("SELECT id FROM domains WHERE name=? COLLATE NOCASE", (domain,)).fetchone()
            ids = [f"{namespace}/{owner[0]}/{row['id']}" for row in items
                   for namespace in ("task-value", "legacy-verifier")] if owner else []
            metadata = dict(db.execute("SELECT key,value_json FROM global_settings WHERE key IN (" +
                                     ",".join("?" for _ in ids) + ")", ids)) if ids else {}
        for item, value_key, verifier_key in zip(items, ids[::2], ids[1::2]):
            if value_key in metadata:
                value = json.loads(metadata[value_key])
                item["value"] = {**value, "minutes": value.get("expert_minutes")}
            if verifier_key in metadata:
                verifier = json.loads(metadata[verifier_key])
                ready = all(verifier["tests"].get(key, {}).get("expected") for key in ("standard", "incomplete"))
                item["verifier"] = {"version": verifier["version"], "ready": ready}
    return {"items": items, "total": total, "offset": offset, "limit": limit}

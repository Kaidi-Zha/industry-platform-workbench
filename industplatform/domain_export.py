"""Export domain benchmark records without materializing external datasets."""

import io
import json
import zipfile
from contextlib import closing

from . import engineering_quality as quality, library, workbench

FIELDS = ("id", "title", "description", "inputs", "references", "engineering", "verifier", "evaluations")


def _schema_fields(store, domain_id):
    schema = workbench.export_schema(store, domain_id)
    defaults = schema["global"].get("x-export-fields", {})
    override = schema["override"].get("x-export-fields", {})
    if not isinstance(defaults, dict) or not isinstance(override, dict):
        raise ValueError("导出字段映射须为 JSON 对象")
    mapping = {**defaults, **override}
    if not isinstance(mapping, dict) or set(mapping) - set(FIELDS):
        raise ValueError("导出字段映射包含未知字段")
    if any(value is not False and (not isinstance(value, str) or not 1 <= len(value.strip()) <= 100)
           for value in mapping.values()):
        raise ValueError("导出字段须为字段名或 false")
    result = {field: mapping.get(field, field) for field in FIELDS if mapping.get(field) is not False}
    if not result or len(set(result.values())) != len(result):
        raise ValueError("请保留至少一个字段且避免列名重复")
    return result


def scoped_record(db, domain_id, identity):
    from .scope_tools import evaluation_ready, task_snapshot
    from .sandbox_verifier import task_revision

    task = task_snapshot(db, domain_id, identity)
    saved = task_revision(db, domain_id, identity)
    if task["is_stale"] or not evaluation_ready(saved):
        return None
    if "input_artifacts" in task:
        task["engineering"] = {key: task[key] for key in quality.TASK_FIELDS}
    task["verifier"] = {"version": saved["version"], "strategy": saved["spec"]["strategy"]}
    task["references"] = [r[0] for r in db.execute(
        "SELECT reference_id FROM task_references WHERE domain_id=? AND task_id=? ORDER BY reference_id",
        (domain_id, identity))]
    task["evaluations"] = [json.loads(r[0]) for r in db.execute("""SELECT results_json FROM eval_runs
        WHERE domain_id=? AND task_id=? AND is_stale=0 AND status='completed' ORDER BY rowid""", (domain_id, identity))]
    return task


def records(store, domain_id):
    columns = _schema_fields(store, domain_id)
    result = []
    with closing(store.connect()) as db:
        row = db.execute("""SELECT d.name FROM domains d LEFT JOIN domain_library l ON l.domain_id=d.id
            WHERE d.id=? AND coalesce(l.archived,0)=0""", (domain_id,)).fetchone()
        if not row:
            raise ValueError("未找到该工业领域")
        domain = row[0]
        for (identity,) in db.execute("""SELECT id FROM benchmark_tasks WHERE domain_id=?
                                        AND state='ready' ORDER BY id""", (domain_id,)):
            task = scoped_record(db, domain_id, identity)
            if task is not None:
                result.append({target: task.get(field) for field, target in columns.items()})
    offset = 0
    while True:
        page = library.read(store, "tasks", domain=domain, offset=offset, limit=100)
        for item in page["items"]:
            if not item["exportable"]:
                continue
            value = {"id": item["details"].get("id", item["id"]), "title": item["title"], **item["details"]}
            result.append({target: value.get(field) for field, target in columns.items()})
        offset += len(page["items"])
        if offset >= page["total"] or not page["items"]:
            break
    return result, columns


def generate(store, domain_id, kind):
    rows, columns = records(store, domain_id)
    if not rows:
        raise ValueError("当前领域暂无已验收可导出的题包")
    return encode(rows, columns, kind)


def batch(store, domains):
    if not domains or len(domains) > 100:
        raise ValueError("请选择 1 至 100 个领域")
    output = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for domain_id in domains:
            rows, columns = records(store, domain_id)
            filename = f"domain-{domain_id}.jsonl"
            if rows:
                archive.writestr(filename, encode(rows, columns, "jsonl")[0])
            manifest.append({"domain_id": domain_id, "tasks": len(rows), **({"file": filename} if rows else {})})
        if not any(entry["tasks"] for entry in manifest):
            raise ValueError("所选领域暂无已验收可导出的题包")
        archive.writestr("manifest.json", json.dumps({"domains": manifest}, ensure_ascii=False, indent=2))
    return output.getvalue()


def encode(values, columns, kind):
    if kind == "jsonl":
        return ("\n".join(json.dumps(row, ensure_ascii=False) for row in values) + "\n").encode(), "application/x-ndjson"
    try:
        import pyarrow as arrow
        import pyarrow.parquet as parquet
    except ImportError:
        raise ValueError("导出 Parquet 需要在服务的 Python 环境安装 PyArrow") from None
    buffer = io.BytesIO()
    encoded = {target: [None if row[target] is None else row[target] if isinstance(row[target], str)
                         else json.dumps(row[target], ensure_ascii=False) for row in values]
               for target in columns.values()}
    schema = arrow.schema([(name, arrow.string()) for name in encoded])
    parquet.write_table(arrow.Table.from_pydict(encoded, schema=schema), buffer, compression="zstd")
    return buffer.getvalue(), "application/vnd.apache.parquet"

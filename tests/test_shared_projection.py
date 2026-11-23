import json
import subprocess
from contextlib import closing

import pytest

from industplatform import (
    asset_index, collaboration, engineering_quality, guarded_workflow, shared_metadata, taxonomy, workbench,
)
from industplatform.storage import RunStore
from scripts.repo_support import Repository
from scripts.sync_meta_to_local import synchronize
from tests.test_engineering_quality import PASSAGES, document


DATA_REPOSITORY = "owner/workspace-data"
PCB_LABEL = {"l2_code": "D01.2", "object_cluster": "pcb-intake", "lifecycle_stages": ["S3", "S5"]}
DESIGN_TITLE = "PCB 设计文件导出"
CAM_TITLE = "板厂 CAM 入厂核对"
STANDARD_URL = "https://example.org/pcb/manufacturing-intake"


def record(kind, key, title, payload, label=None):
    return shared_metadata.new_record(label or PCB_LABEL, kind, key, title, "shared-fixture", payload)


def source(title, slug, state="unverified", label=None):
    url = "https://example.org/" + slug
    return record("sources", url, title, {
        "url": url, "provenance": "local_import", "verification_status": state, "summary_zh": title + "的工艺资料线索。",
    }, label)


def process(node_id, title, position, *, upstream=(), downstream=(), upstream_ids=None, downstream_ids=None):
    payload = {
        "process_node_id": node_id, "position": position, "flow_group": "制板输入",
        "occupations": ["CAM 工程师"], "upstream": list(upstream), "downstream": list(downstream),
    }
    if upstream_ids is not None:
        payload["upstream_ids"] = list(upstream_ids)
    if downstream_ids is not None:
        payload["downstream_ids"] = list(downstream_ids)
    return record("processes", title, title, payload)


def literature():
    profile = engineering_quality.reference_document(document(), PASSAGES)
    for clause in profile["extracted_clauses"]:
        clause.pop("quote")
    return record("literature", STANDARD_URL, profile["title"], {
        **profile, "url": STANDARD_URL, "sha256": "a" * 64, "permission": "unknown",
        "authority_tier": "T2_MANUFACTURER", "source_channel": "C2_OEM_FAB_SPEC",
        "process_node_ids": ["cam"], "process": CAM_TITLE,
    })


def publish(repo, *rows):
    for row in rows:
        path = repo.root / shared_metadata.record_path(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(shared_metadata.json_bytes(row))
    repo.git("add", "--all")
    repo.git("commit", "--quiet", "-m", "Publish shared fixture")
    head = repo.revision("HEAD")
    repo.git("update-ref", shared_metadata.PUBLISHED_REF, head)
    return head


def fixture(tmp_path):
    path = tmp_path / "data"
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(path)], check=True)
    repo = Repository(path)
    repo.git("config", "user.name", "Local Test")
    repo.git("config", "user.email", "local@example.org")
    store = RunStore(tmp_path / "workbench.sqlite3")
    worker = collaboration.Collaboration(store, data_root=tmp_path / "checkouts")
    worker.configure(DATA_REPOSITORY)
    return repo, store, worker


def pull(repo, store, *, rebuild=False):
    return synchronize(repo, store.path, ref=shared_metadata.PUBLISHED_REF,
                       repository=DATA_REPOSITORY, rebuild=rebuild)


def test_fresh_sync_projects_data_into_library_graph_and_source_pages(tmp_path):
    repo, store, _ = fixture(tmp_path)
    other = {**PCB_LABEL, "l2_code": "D02.1", "object_cluster": "mechanical-fixtures"}
    ref = literature()
    clause = next(item for item in ref["payload"]["extracted_clauses"] if item["kind"] == "acceptance")
    head = publish(
        repo,
        source("待核验制板资料", "pcb/raw-data"),
        source("已过滤制板资料", "pcb/old-promotion", "rejected"),
        source("机械夹具工程数据", "fixtures/asset", label=other),
        process("pcb-design", DESIGN_TITLE, 1, downstream=[CAM_TITLE], downstream_ids=["cam"]),
        process("cam", CAM_TITLE, 2, upstream=[DESIGN_TITLE], upstream_ids=["pcb-design"]),
        ref,
        record("entities", json.dumps([CAM_TITLE, "示例板厂", "manufacturer"]), "示例板厂", {
            "process": CAM_TITLE, "name": "示例板厂", "role": "manufacturer",
            "evidence_url": STANDARD_URL, "locator": clause["locator"],
        }),
        record("entities", json.dumps([CAM_TITLE, "制板设计岗", "示例板厂", "声明单位的钻孔文件", "supplier_requirement"]),
               "制板设计岗 → 示例板厂", {
                   "process": CAM_TITLE, "sender": "制板设计岗", "recipient": "示例板厂", "deliverable": "声明单位的钻孔文件",
                   "relationship": "supplier_requirement", "evidence_url": STANDARD_URL, "locator": clause["locator"],
                   "clause_id": clause["id"],
               }),
        record("tasks", json.dumps([CAM_TITLE, "板框收件复核"]), "板框收件复核", {
            "process": CAM_TITLE, "complexity": "L2", "description": "解析 Gerber 板框，检查工件交付的坐标单位。",
            "citations": [{"title": ref["title"], "url": STANDARD_URL, "locator": clause["locator"]}],
        }),
    )

    assert pull(repo, store) == (head, 9, 0)
    global_view = workbench.overview(store, level="global")
    assert len(global_view["domains"]) == 2
    assert global_view["threads"] == []
    name = taxonomy.registry()[1]["D01.2"]["name_zh"]
    pcb = next(domain for domain in global_view["domains"] if domain["name"] == name)
    assert (pcb["counts"]["nodes"], pcb["counts"]["references"]) == (2, 1)
    domain = pcb["id"]
    view = workbench.overview(store, domain, level="domain")
    assert [node["id"] for node in view["nodes"]] == ["pcb-design", "cam"]
    assert view["edges"] == [{"upstream_id": "pcb-design", "downstream_id": "cam"}]
    assert [member["name"] for member in view["participants"]] == ["示例板厂"]
    assert [relation["deliverable"] for relation in view["relations"]] == ["声明单位的钻孔文件"]
    assert view["nodes"][1]["verified"] == 1

    refs = workbench.source_page(store, domain, "references", node="cam")
    assert [(item["title"], item["process_node_ids"]) for item in refs["items"]] == [(ref["title"], ["cam"])]
    imports = workbench.source_page(store, domain, "imports")
    assert {item["title"]: item["state"] for item in imports["items"]} == {
        "待核验制板资料": "needs_review", "已过滤制板资料": "rejected",
    }
    assert {item["title"] for item in asset_index.read(store, "sources", query="制板资料")["items"]} == {
        "待核验制板资料", "已过滤制板资料",
    }
    task = guarded_workflow.domain_tasks(store, domain)["items"][0]
    assert (task["title"], task["node_id"], task["status"], task["quality_approved"]) == (
        "板框收件复核", "cam", "draft", False,
    )
    assert [ref["url"] for ref in task["details"]["references"]] == [STANDARD_URL]
    assert pull(repo, store) == (head, 0, 0)
    assert pull(repo, store, rebuild=True) == (head, 9, 0)
    rebuilt = workbench.overview(store, domain, level="domain")
    assert {key: rebuilt[key] for key in ("nodes", "edges", "participants", "relations", "metrics")} == {
        key: view[key] for key in ("nodes", "edges", "participants", "relations", "metrics")
    }


def test_old_cache_at_same_commit_is_backfilled_without_waiting_for_new_git_commit(tmp_path):
    repo, store, _ = fixture(tmp_path)
    row = source("已拉取但未展示的资料", "pcb/existing")
    head = publish(repo, row)
    with closing(store.connect()) as db, db:
        db.execute("""INSERT INTO shared_metadata(path,domain_id,kind,canonical_id,title,version,status,body)
            VALUES (?,?,?,?,?,?,?,?)""", (
            shared_metadata.record_path(row), row["l2_code"], row["kind"], row["canonical_id"], row["title"],
            row["version"], row["status"], shared_metadata.json_bytes(row).decode("utf-8"),
        ))
        db.execute("""INSERT INTO shared_metadata_sync(singleton,checkout,last_synced_commit,source_ref,repository,synced_at)
            VALUES (1,?,?,?,?,?)""", (str(repo.root), head, shared_metadata.PUBLISHED_REF, DATA_REPOSITORY, row["updated_at"]))
    assert workbench.overview(store, level="global")["domains"] == []

    restarted = RunStore(store.path)
    domains = workbench.overview(restarted, level="global")["domains"]
    assert len(domains) == 1
    assert pull(repo, restarted) == (head, 0, 0)
    assert [item["title"] for item in workbench.source_page(restarted, domains[0]["id"], "imports")["items"]] == [
        "已拉取但未展示的资料",
    ]


def test_corrections_and_disconnect_remove_only_still_owned_projected_rows(tmp_path):
    repo, store, worker = fixture(tmp_path)
    name = taxonomy.registry()[1]["D01.2"]["name_zh"]
    domain = workbench.add_domain(store, {"name": name})["id"]
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO process_nodes(domain_id,id,title,position) VALUES (?, 'own', '本机编写的工序', 1)",
                   (domain,))

    other = {**PCB_LABEL, "l2_code": "D02.1", "object_cluster": "mechanical-fixtures"}
    original = source("团队资料第一版", "pcb/revision")
    retired = source("后续被废止的团队资料", "pcb/withdrawn")
    changed_locally = source("团队资料随后本机编辑", "pcb/amended-locally")
    publish(repo, original, retired, changed_locally, source("仅来自远端的夹具行业", "fixtures/shared", label=other))
    pull(repo, store)
    assert len(workbench.overview(store, level="global")["domains"]) == 2
    with closing(store.connect()) as db, db:
        db.execute("""UPDATE raw_sources SET import_metadata_json=json_set(import_metadata_json,'$.title',?)
            WHERE (domain_id,canonical_url)=(?,?)""",
                   ("本机修订标题", domain, changed_locally["canonical_key"]))

    publish(repo, {**original, "version": 2, "title": "团队资料第二版", "correction_reason": "更新工艺版本"},
            {**retired, "version": 2, "status": "deprecated", "correction_reason": "原入口已废止"},
            {**changed_locally, "version": 2, "title": "远端修订标题", "correction_reason": "更新原文标题"})
    pull(repo, store)
    assert {item["title"] for item in workbench.source_page(store, domain, "imports")["items"]} == {
        "团队资料第二版", "本机修订标题",
    }
    worker.configure("")
    assert [item["title"] for item in workbench.source_page(store, domain, "imports")["items"]] == ["本机修订标题"]
    view = workbench.overview(store, domain, level="domain")
    assert [node["id"] for node in view["nodes"]] == ["own"]
    assert len(view["domains"]) == 1
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM shared_metadata").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_invalid_remote_commit_preserves_last_successful_workbench_view(tmp_path):
    repo, store, _ = fixture(tmp_path)
    row = source("仍可访问的旧版资料", "pcb/last-good")
    good = publish(repo, row)
    pull(repo, store)
    domain = workbench.overview(store, level="global")["domains"][0]["id"]

    invalid = source("缺少有效来源的记录", "pcb/bad-link")
    invalid["payload"]["url"] = "https://example.org/different-canonical-key"
    publish(repo, {**row, "title": "本轮不应局部写入的标题"}, invalid)
    with pytest.raises(ValueError, match="payload.url"):
        pull(repo, store)

    assert [item["title"] for item in workbench.source_page(store, domain, "imports")["items"]] == [row["title"]]
    with closing(store.connect()) as db:
        assert db.execute("SELECT last_synced_commit FROM shared_metadata_sync").fetchone() == (good,)
        assert db.execute("SELECT title FROM shared_metadata").fetchall() == [(row["title"],)]

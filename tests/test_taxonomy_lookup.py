import json
from contextlib import closing

from industplatform import codex_agent, domain_libraries, guarded_workflow, scope_guard, scope_tools, shared_metadata, taxonomy
from industplatform.storage import RunStore


LABEL = {"l2_code": "D01.2", "object_cluster": "pcb-gerber-cam-intake", "lifecycle_stages": ["S5"]}
OBJECT_TITLE = "PCB Gerber 板厂 CAM 入厂核对"


def test_large_catalog_find_existing_object_and_save_classification(tmp_path):
    store = RunStore(tmp_path / "large-scope.sqlite3")
    industry = taxonomy.registry()[1][LABEL["l2_code"]]
    with closing(store.connect()) as db, db:
        domain = domain_libraries.register(db, industry["name_zh"])
        for index in range(377):
            label = LABEL if index == 376 else {**LABEL, "object_cluster": f"pcb-component-{index:03d}"}
            url = f"https://example.org/source-{index}"
            title = OBJECT_TITLE if index == 376 else f"PCB 元器件文档 {index}"
            row = shared_metadata.new_record(label, "sources", url, title, "shared-fixture", {"url": url})
            db.execute("INSERT INTO shared_metadata VALUES (?,?,?,?,?,?,?,?)", (
                shared_metadata.record_path(row), row["domain_id"], row["kind"], row["canonical_id"],
                row["title"], row["version"], row["status"], shared_metadata.json_bytes(row).decode(),
            ))

    store.create(codex_agent.ROUTE, "offline", "核对 PCB 制造文件交付中的 Gerber 和板厂 CAM 入厂标准。")
    run = store.claim()
    contract = scope_guard.ExecutionScopeContract.parse(
        {"target_stage": scope_guard.STAGES[0], "domain_id": domain, "router_confidence": 1})
    with closing(store.connect()) as db, db:
        guarded_workflow._store_contract(db, run, contract, "executing")
        store.save(db, run)
        assert guarded_workflow._local_context(db, contract)["domain_taxonomy_hint"] == {
            "name_zh": industry["name_zh"], "l2_code": LABEL["l2_code"],
        }

    tools = scope_tools.StageTools(store, run, contract)
    result = tools.call("lookup_industry_taxonomy", {"l2_code": LABEL["l2_code"]})
    assert result["existing_object_clusters_total"] == 377
    assert len(result["existing_object_clusters"]) == 8
    assert result["existing_object_clusters"][0] == {"object_cluster": LABEL["object_cluster"], "example": OBJECT_TITLE}
    assert result["next_cluster_offset"] is None
    assert tools.call("classify_industry_task", {"classification_json": json.dumps(LABEL)}) == LABEL
    assert store.get(run["id"])["task_taxonomy"] == LABEL
    assert tools.calls == 2

    # Even an extremely broad explicit search can expose only three pages.
    offset = 0
    page_ids = []
    while offset is not None:
        result = tools.call("lookup_industry_taxonomy", {
            "l2_code": LABEL["l2_code"], "object_query": "PCB", "cluster_offset": offset,
        })
        assert result["existing_object_clusters_matched"] == 377
        page_ids.extend(item["object_cluster"] for item in result["existing_object_clusters"])
        offset = result["next_cluster_offset"]
    assert len(set(page_ids)) == len(page_ids) == 24

    focused = tools.call("lookup_industry_taxonomy", {"l2_code": LABEL["l2_code"], "object_query": "入厂核对"})
    assert focused["existing_object_clusters"] == [{"object_cluster": LABEL["object_cluster"], "example": OBJECT_TITLE}]
    assert focused["next_cluster_offset"] is None


def test_official_hint_with_unique_alias_does_not_guess_ambiguous_library(tmp_path):
    store = RunStore(tmp_path / "domain-scope.sqlite3")
    pcb = taxonomy.registry()[1][LABEL["l2_code"]]
    other = taxonomy.registry()[1]["D01.3"]
    with closing(store.connect()) as db, db:
        named = domain_libraries.register(db, pcb["name_zh"], aliases=[other["name_zh"]])
        alias = domain_libraries.register(db, "PCB 试制库", aliases=[pcb["english_id"]])
        ambiguous = domain_libraries.register(db, "硬件试制", aliases=[pcb["name_zh"], other["name_zh"]])
        assert shared_metadata.library_industry_hint(db, named)["l2_code"] == LABEL["l2_code"]
        assert shared_metadata.library_industry_hint(db, alias)["l2_code"] == LABEL["l2_code"]
        assert shared_metadata.library_industry_hint(db, ambiguous) is None

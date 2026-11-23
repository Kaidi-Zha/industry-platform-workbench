import io
import json
import tempfile
import zipfile
import unittest
from unittest.mock import patch
from contextlib import closing
from pathlib import Path

from industplatform import asset_index, attachments, collection, domain_export, engineering_quality, import_library, reference_catalog, workbench
from industplatform.storage import API_AGENT_ROUTE, RunStore
from tests.test_engineering_quality import PASSAGES, document, seed_qualified_reference


class WorkbenchMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RunStore(Path(directory.name) / "workbench.sqlite3")

    def test_import_links_to_verified_version_and_references_filter_by_node(self):
        domain = workbench.add_domain(self.store, {"name": "PCB 工序核验"})["id"]
        cam = workbench.add_process_node(self.store, {"domain_id": domain, "title": "CAM 核对"})["id"]
        plating = workbench.add_process_node(self.store, {"domain_id": domain, "title": "孔金属化"})["id"]
        url = "https://example.org/pcb-spec"
        import_meta = json.dumps({"title": "PCB 规范", "summary": "CAM 技术要求", "object_cluster": "pcb-fab",
                                  "lifecycle_stages": ["S3"], "batch_id": "local-document"})
        with closing(self.store.connect()) as db, db:
            db.execute("""INSERT INTO raw_sources(domain_id,id,canonical_url,status,import_metadata_json)
                       VALUES (?,'lead',?,'discovered',?)""", (domain, url, import_meta))
            db.execute("""INSERT INTO raw_sources(domain_id,id,canonical_url,version,status)
                       VALUES (?,'pinned',?,'r2','parsed')""", (domain, url))
        self.assertEqual(workbench.overview(self.store, domain)["domain"]["local_import_count"], 1)
        with closing(self.store.connect()) as db, db:
            seed_qualified_reference(db, domain, cam, "cam-ref", "pinned")
        imported, = workbench.source_page(self.store, domain, "imports")["items"]
        self.assertEqual((imported["state"], imported["reference_id"]), ("verified", "cam-ref"))
        summary = workbench.overview(self.store, domain)["domain"]
        self.assertEqual((summary["counts"]["imported"], summary["local_import_count"]), (1, 0))
        selected = workbench.source_page(self.store, domain, "references", node=cam)
        self.assertEqual(selected["node_title"], "CAM 核对")
        self.assertEqual([row["id"] for row in selected["items"]], ["cam-ref"])
        self.assertEqual(workbench.source_page(self.store, domain, "references", node=plating)["items"], [])
        with self.assertRaises(ValueError):
            workbench.source_page(self.store, domain, "references", node="other-domain-node")

    def test_backfill_keywords_from_existing_import_and_project_new_process_terms(self):
        name = "燃料加工与炼制"
        identity = workbench.add_domain(self.store, {"name": name})["id"]
        data = json.dumps({"title": "硫回收技改报告", "summary": "环评公示；license=GPL-3.0",
                           "l2_code": "D04.1", "object_cluster": "sulfur-recovery-unit"}, ensure_ascii=False)
        parameters = (identity, data)
        with closing(self.store.connect()) as db, db:
            db.execute("""INSERT INTO raw_sources(domain_id,id,canonical_url,status,import_metadata_json)
                VALUES (?,'lead','https://example.org/recovery','discovered',?)""", parameters)
            self.assertEqual(import_library.backfill_keywords(db), [
                {"id": identity, "name": name, "keywords": ["硫回收", "技改", "环评"]}
            ])
            self.assertEqual(import_library.backfill_keywords(db), [])
            self.assertEqual(db.execute("SELECT domain_id,import_metadata_json FROM raw_sources").fetchall(), [parameters])
        workbench.add_process_node(self.store, {"domain_id": identity, "title": "硫回收装置 PLC 控制"})
        self.assertIn("PLC", workbench.overview(self.store, identity)["domain"]["keywords"])
        with closing(self.store.connect()) as db:
            keywords, = db.execute("SELECT keywords FROM domain_library WHERE domain_id=?", (identity,)).fetchone()
            self.assertEqual(json.loads(keywords), ["硫回收", "技改", "环评"])

    def test_reclassifies_legacy_official_manual_and_resolves_process_by_title_once(self):
        path = self.store.path.parent / "legacy.sqlite3"
        with patch.object(reference_catalog, "migrate"):
            old = RunStore(path)
        domain = workbench.add_domain(old, {"name": "PCB 旧文献"})["id"]
        process = workbench.add_process_node(old, {"domain_id": domain, "title": "制造文件导出"})
        url = "https://docs.kicad.org/9.0/en/cli/cli.html"
        profile = engineering_quality.reference_document(document(), PASSAGES)
        profile["stage"] = "01 制造文件导出"
        profile["publisher_info"]["authority"] = "open_record"
        with closing(old.connect()) as db, db:
            db.execute("""INSERT INTO raw_sources(domain_id,id,canonical_url,status,import_metadata_json)
                          VALUES (?,'manual',?,'parsed',?)""", (domain, url, json.dumps({
                              "object_cluster": "pcb-export", "lifecycle_stages": ["S3"],
                          })))
            db.execute("""INSERT INTO verified_references(domain_id,id,source_id,publisher,title,authority,locator,
                finding,permission,verified_at,quality_json)
                VALUES (?,'ref','manual','KiCad','KiCad CLI','research','L1.1','导出制板数据','unknown','2026-01-16',?)""",
                (domain, json.dumps(profile)))
        migrated = RunStore(path)
        view = workbench.source_page(migrated, domain, "references", node="01")
        ref, = view["items"]
        self.assertEqual((ref["id"], ref["authority_tier"], ref["source_channel"]), ("ref", "T3_TOOLCHAIN", "C3_TOOL_FORMAT_DOC"))
        self.assertEqual((ref["process_node_ids"], ref["object_cluster"], ref["lifecycle_stages"]), ([process["id"]], "pcb-export", ["S3"]))
        self.assertEqual(workbench.source_page(RunStore(path), domain, "references", node="01"), view)

    def test_existing_evidence_and_threads_are_scoped(self):
        domain = workbench.add_domain(self.store, {"name": "CNC／CMM"})
        other = workbench.add_domain(self.store, {"name": "PCB"})
        scope = collection.plan({"domain": domain["name"], "scenario": "首件检验", "region": "不限",
                                 "time_window": "不限", "stages": "S5", "asset_types": "检测记录",
                                 "deliverables": "测量和放行记录"})
        run = self.store.create(API_AGENT_ROUTE, "offline", "研究首件检验", plan=scope)
        item = {"domain": domain["name"], "title": "尺寸要求", "kind": "标准", "publisher": "测试机构",
                "url": "https://example.test/spec", "locator": "第 2 页", "claim": "尺寸验收要求"}
        collection.save_reference(self.store, item)
        verified = collection.save_reference(
            self.store, {**item, "locator": "第 3 页"},
            verification={"target": "measurement", "quote": "量具需定期校准", "level": "document"},
        )
        store = RunStore(self.store.path)
        cn = workbench.overview(store, domain["id"])
        self.assertEqual([row["id"] for row in cn["threads"]], [run["id"]])
        self.assertEqual(cn["active_count"], 1)
        self.assertEqual(cn["metrics"]["verified_references"], 0)
        self.assertFalse(cn["references"])
        archived = workbench.source_page(store, domain["id"], "frontier", item=verified["id"])["items"][0]
        self.assertEqual(archived["state"], "legacy_review")
        self.assertFalse(workbench.reference_detail(store, domain["id"], verified["id"])["quality_approved"])
        self.assertFalse(workbench.overview(store, other["id"])["threads"])
        self.assertFalse(workbench.overview(store, other["id"])["references"])
        with self.assertRaisesRegex(ValueError, "已存在"):
            workbench.add_domain(store, {"name": "pcb"})

    def test_global_workspace_does_not_implicitly_select_or_leak_a_domain(self):
        first = workbench.add_domain(self.store, {"name": "首件检验"})
        second = workbench.add_domain(self.store, {"name": "焊接组装"})
        for domain in (first, second):
            scope = {"domain": domain["name"], "scenario": "调查", "stages": "校准",
                     "region": "不限", "time_window": "不限", "asset_types": "公开资料", "deliverables": "核验说明"}
            self.store.create(API_AGENT_ROUTE, "offline", domain["name"], plan=scope)
        unassigned = self.store.create(API_AGENT_ROUTE, "offline", "未定领域",
                                       scope_request={"domain_id": None, "target": {}})
        first_thread = self.store.conversations(first["name"])[0]["id"]
        total = workbench.overview(self.store, thread=first_thread, level="global")
        self.assertIsNone(total["domain"])
        self.assertEqual(total["thread_domain"]["id"], first["id"])
        self.assertEqual(len(total["threads"]), 3)
        scoped = workbench.overview(self.store, first["id"], level="domain")
        self.assertEqual([row["id"] for row in scoped["threads"]], [first_thread])
        missing = workbench.overview(self.store, 999, thread=unassigned["id"], level="domain")
        self.assertIsNone(missing["domain"])
        self.assertFalse(missing["threads"])

    def test_assets_use_global_filters_and_batches_keep_each_domains_schema(self):
        defaults = {"type": "object", "x-export-fields": {"title": "name"}}
        workbench.save_export_schema(self.store, {"scope": "global", "schema": defaults})
        first = workbench.add_domain(self.store, {"name": "PCB"})["id"]
        second = workbench.add_domain(self.store, {"name": "CMM"})["id"]
        workbench.save_export_schema(self.store, {"domain_id": second, "scope": "domain", "schema": {
            "x-export-fields": {"title": "inspection_name"},
        }})
        self.assertEqual(workbench.export_schema(self.store, None), {"global": defaults, "override": {}})
        with closing(self.store.connect()) as db, db:
            for domain, name in ((first, "板卡检查"), (second, "首件检查")):
                db.execute("""INSERT INTO process_nodes(domain_id,id,title,position)
                    VALUES(?,'node',?,1)""", (domain, name))
                db.execute("""INSERT INTO benchmark_tasks(domain_id,id,node_id,title,definition_json,state)
                    VALUES(?,'task','node',?,'{}','ready')""", (domain, name))
                tests = [{"variant": variant, "result": {"expected": True}} for variant in ("standard", "incomplete")]
                db.execute("""INSERT INTO verifier_specs(domain_id,task_id,version,strategy,layers_json,self_tests_json)
                    VALUES(?,'task',1,'progressive','{"L1":[],"L2":[],"L3":[],"L4":[]}',?)""", (domain, json.dumps(tests)))
                db.execute("""INSERT INTO eval_runs(domain_id,id,task_id,verifier_version,status,model_matrix_json,
                    pass_at_k,difficulty,confirmed_at) VALUES(?,'eval','task',1,'completed','[]',1,'hard','2026-01-01')""",
                    (domain,))
        self.assertEqual(len(asset_index.read(self.store, "verifiers")["items"]), 2)
        self.assertEqual([row["domain_id"] for row in asset_index.read(self.store, "tasks", query="首件 PCB")["items"]], [])
        result = asset_index.read(self.store, "evaluations", filters={"stage": "首件检查", "difficulty": "hard"})
        self.assertEqual([(row["domain_id"], row["id"]) for row in result["items"]], [(second, "eval")])
        self.assertEqual(asset_index.evaluation(self.store, second, "eval")["title"], "首件检查")
        for domain in workbench.overview(self.store, level="global")["domains"]:
            self.assertEqual(domain["counts"], {"nodes": 1, "references": 0, "imported": 0, "tasks": 1, "evaluated": 1})
        with zipfile.ZipFile(io.BytesIO(domain_export.batch(self.store, [first, second]))) as archive:
            self.assertIn("name", json.loads(archive.read(f"domain-{first}.jsonl")))
            self.assertIn("inspection_name", json.loads(archive.read(f"domain-{second}.jsonl")))
            self.assertEqual(len(json.loads(archive.read("manifest.json"))["domains"]), 2)

    def test_migration_materializes_preexisting_domain(self):
        old_domain = "原有数据库的工艺"
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO evidence_references VALUES (?, ?, ?)", ("existing", old_domain, "{}"))
        reopened = RunStore(self.store.path)
        names = [row["domain"] for row in collection.reference_domains(reopened)]
        self.assertIn(old_domain, names)

    def test_workbench_nodes_and_schema_persist_in_their_domain(self):
        first = workbench.add_domain(self.store, {"name": "研制工艺"})["id"]
        second = workbench.add_domain(self.store, {"name": "验收工艺"})["id"]
        upstream = workbench.add_process_node(self.store, {"domain_id": first, "title": "设计"})
        downstream = workbench.add_process_node(self.store, {
            "domain_id": first, "title": "交付", "upstream_id": upstream["id"],
        })
        with self.assertRaisesRegex(ValueError, "当前领域"):
            workbench.add_process_node(self.store, {
                "domain_id": second, "title": "跨域误连", "upstream_id": upstream["id"],
            })
        override = {"properties": {"material": {"type": "string"}}}
        workbench.save_export_schema(self.store, {"domain_id": first, "scope": "domain", "schema": override})
        base = {"type": "object", "title": "新版工艺题包"}
        workbench.save_export_schema(self.store, {"domain_id": first, "scope": "global", "schema": base})
        with self.assertRaisesRegex(ValueError, "object"):
            workbench.save_export_schema(self.store, {"domain_id": first, "scope": "domain", "schema": {"type": "array"}})
        store = RunStore(self.store.path)
        self.assertEqual(workbench.overview(store, first)["edges"], [{
            "upstream_id": upstream["id"], "downstream_id": downstream["id"],
        }])
        self.assertFalse(workbench.overview(store, second)["nodes"])
        self.assertEqual(workbench.export_schema(store, first), {"global": base, "override": override})
        self.assertEqual(workbench.export_schema(store, second), {"global": base, "override": {}})

    def test_verified_source_search_checks_all_pages_and_domain(self):
        first = workbench.add_domain(self.store, {"name": "首件检查"})["id"]
        other = workbench.add_domain(self.store, {"name": "其他领域"})["id"]
        item = {"domain": "首件检查", "title": "测量规程", "kind": "手册", "publisher": "检验室",
                "url": "https://example.test/origin", "locator": "第 3 页", "claim": "标定时机"}
        proof = {"target": "check", "quote": "每班开始前标定", "level": "document"}
        with closing(self.store.connect()) as db, db:
            for index in range(32):
                key = "qualified-" + str(index)
                db.execute("INSERT INTO raw_sources(domain_id,id,canonical_url,status) VALUES(?,?,?,'parsed')",
                           (first, key, "https://example.test/" + key))
                seed_qualified_reference(db, first, None, key, key)
        legacy = collection.save_reference(self.store, {**item, "title": "未核验记录", "locator": "第 100 页"})
        keyword = "板框文件"
        self.assertTrue(workbench.source_page(self.store, first, "references", keyword)["more"])
        self.assertEqual(len(workbench.source_page(self.store, first, "references", keyword, offset=30)["items"]), 2)
        self.assertFalse(workbench.source_page(self.store, first, "references", item=legacy["id"])["items"])
        self.assertFalse(workbench.source_page(self.store, other, "references", keyword)["items"])

    def test_process_groups_and_multiple_upstream_links_persist(self):
        domain = workbench.add_domain(self.store, {"name": "多路工序"})["id"]

        def node(title, **fields):
            return workbench.add_process_node(self.store, {"domain_id": domain, "title": title, **fields})["id"]

        layout = node("板厂 CAM 入件核查", lifecycle_stages=["S3"])
        with self.assertRaisesRegex(ValueError, "相同工序"):
            node("板厂CAM入厂核对")
        kitting = node("装配齐套", lifecycle_stages=["S4"])
        assemble = node("装配", flow_group="装配与齐套", upstream_ids=[layout, kitting])
        workbench.change_process_node(self.store, {"domain_id": domain, "id": assemble, "action": "update",
            "title": "装配", "flow_group": "自定义生产", "upstream_ids": [layout, kitting]})
        result = workbench.overview(RunStore(self.store.path), domain)
        groups = {group["title"]: group["node_ids"] for group in result["flow_groups"]}
        self.assertEqual(groups, {"设计与工艺准备": [layout], "装配与齐套": [kitting], "自定义生产": [assemble]})
        self.assertEqual({(edge["upstream_id"], edge["downstream_id"]) for edge in result["edges"]},
                         {(layout, assemble), (kitting, assemble)})

    def test_process_aliases_keep_raw_edges_and_reject_cycles(self):
        domain = workbench.add_domain(self.store, {"name": "工序关联审计"})["id"]

        def node(title, **fields):
            return workbench.add_process_node(self.store, {"domain_id": domain, "title": title, **fields})["id"]

        source = node("制造准备")
        middle = node("钻孔", upstream_id=source)
        downstream = node("电测", upstream_id=middle)
        alias = node("制造文件导出")
        link = {"domain_id": domain, "action": "link", "id": alias,
                "canonical_id": source, "reason": "按产品工单合并显示"}
        self.assertTrue(workbench.change_process_node(self.store, link)["changed"])
        self.assertFalse(workbench.change_process_node(self.store, link)["changed"])
        with self.assertRaisesRegex(ValueError, "循环"):
            workbench.change_process_node(self.store, {**link, "id": downstream})
        before = workbench.overview(self.store, domain)
        self.assertEqual(len(before["nodes"]), 4)
        self.assertEqual(len(before["node_links"]), 1)
        with self.assertRaisesRegex(ValueError, "循环"):
            workbench.change_process_node(self.store, {"domain_id": domain, "id": alias, "action": "update",
                "title": "制造文件导出", "upstream_ids": [downstream]})
        workbench.change_process_node(self.store, {**link, "action": "unlink"})
        after = workbench.overview(self.store, domain)
        self.assertEqual(before["edges"], after["edges"])
        self.assertEqual(after["node_links"], [])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT action,reason FROM process_node_link_audit ORDER BY sequence").fetchall(),
                             [("link", "按产品工单合并显示"), ("unlink", "恢复为独立工序")])

    def test_recorded_graph_is_grounded_and_cycle_rolls_back(self):
        import json

        domain = workbench.add_domain(self.store, {"name": "任务图谱"})["id"]
        plan = collection.plan({"domain": "任务图谱", "scenario": "CMM", "region": "不限", "time_window": "不限",
                                "stages": "S5", "asset_types": "规范", "deliverables": "工艺图谱"})
        owner = self.store.create(API_AGENT_ROUTE, "offline", "画图", plan=plan, public_research=True, graph_mode=True)
        self.store.claim()
        ref = collection.save_reference(self.store, {
            "domain": plan["domain"], "title": "测量指导", "kind": "手册", "publisher": "测试机构",
            "url": "https://example.test/inspection", "locator": "第 1 页", "claim": "测量工序",
        }, verification={"target": "test", "quote": "计量工序", "level": "document"})
        nodes = [{"title": "设计", "occupations": "设计员", "references": [ref["id"]], "upstream": []},
                 {"title": "检测", "occupations": "量测员", "references": [ref["id"]], "upstream": ["设计"]}]
        recorded = workbench.record_process_graph(self.store, owner["id"], plan["domain"], json.dumps(nodes))
        summary = workbench.overview(self.store, domain)
        self.assertEqual(len(summary["edges"]), 1)
        self.assertEqual([node["verified"] for node in summary["nodes"]], [1, 1])
        with self.assertRaisesRegex(ValueError, "循环"):
            workbench.record_process_graph(self.store, owner["id"], plan["domain"],
                                           json.dumps([{**nodes[0], "upstream": ["检测"]}]))
        self.assertEqual(workbench.overview(self.store, domain)["edges"][0]["upstream_id"], recorded[0]["id"])

    def test_attachments_are_bound_to_the_selected_domain(self):
        owner = workbench.add_domain(self.store, {"name": "附件一"})
        other = workbench.add_domain(self.store, {"name": "附件二"})
        with patch.object(attachments, "ROOT", self.store.path.parent.resolve()):
            item = attachments.save(self.store, owner["id"], "fixture.step", b"ISO-10303-21;")
            record, = attachments.resolve(self.store, owner["name"], [item["id"]])
            self.assertTrue(record["path"].startswith(".local"))
            with self.assertRaises(ValueError):
                attachments.resolve(self.store, other["name"], [item["id"]])
            with self.assertRaises(ValueError):
                attachments.save(self.store, owner["id"], "../bad.pdf", b"%PDF-")

    def test_export_field_mapping_merges_and_rejects_duplicates(self):
        domain = workbench.add_domain(self.store, {"name": "导出领域"})["id"]
        workbench.save_export_schema(self.store, {"domain_id": domain, "scope": "global", "schema": {
            "type": "object", "x-export-fields": {"description": "prompt", "verifier": False},
        }})
        workbench.save_export_schema(self.store, {"domain_id": domain, "scope": "domain", "schema": {
            "x-export-fields": {"title": "task_title"},
        }})
        result = domain_export._schema_fields(self.store, domain)
        self.assertEqual((result["description"], result["title"]), ("prompt", "task_title"))
        self.assertNotIn("verifier", result)
        workbench.save_export_schema(self.store, {"domain_id": domain, "scope": "domain", "schema": {
            "x-export-fields": {"title": "prompt"},
        }})
        with self.assertRaisesRegex(ValueError, "重复"):
            domain_export._schema_fields(self.store, domain)

    def test_frontier_statistics_deduplicate_and_ignore_pending(self):
        import sqlite3

        domain_id = workbench.add_domain(self.store, {"name": "金属加工"})["id"]
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO process_nodes(domain_id, id, position, title) VALUES (?, 'fai', 1, '检验')", (domain_id,))
            db.execute("INSERT INTO frontier_rounds(domain_id, node_id, number) VALUES (?, 'fai', 1)", (domain_id,))
            db.executemany("""
                INSERT INTO raw_sources(domain_id, id, canonical_url, status) VALUES (?, ?, ?, 'parsed')
            """, [(domain_id, key, "https://example.test/" + key) for key in ("first", "mirror", "pending")])
            signature = "a" * 64
            db.execute("INSERT INTO entity_fingerprints VALUES (?, 'fai', ?, 'first', 1)", (domain_id, signature))
            db.executemany("INSERT INTO frontier_candidates VALUES (?, 'fai', 1, ?, ?, ?)", [
                (domain_id, "first", signature, "eligible"), (domain_id, "mirror", signature, "eligible"),
                (domain_id, "pending", None, "pending"),
            ])
            self.assertEqual(db.execute("SELECT assessed, novel, pending FROM frontier_round_metrics").fetchone(), (2, 1, 1))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO process_edges VALUES (?, 'fai', 'another-domain')", (domain_id,))


class DynamicDomainTests(unittest.TestCase):
    setUp = WorkbenchMigrationTests.setUp
    # Each test opens its own local DB. The configured model is replaced at the
    # orchestration boundary so the same MCP callbacks drive the full workflow.
    def test_empty_catalog_and_legacy_empty_seeds(self):
        from industplatform import forest

        self.assertEqual(workbench.overview(self.store)["domains"], [])
        names = forest.domains()
        self.assertGreaterEqual(len(names), 2)
        kept = workbench.add_domain(self.store, {"name": names[0]})
        workbench.add_process_node(self.store, {"domain_id": kept["id"], "title": "用户实际登记工序"})
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO domains(name) VALUES (?)", (names[1],))
            db.execute("DELETE FROM global_settings WHERE key='domain-seeds-removed'")
        reopened = RunStore(self.store.path)
        self.assertEqual([row["name"] for row in workbench.overview(reopened)["domains"]], [names[0]])

    def test_agent_matches_broader_domain_before_using_research_tools(self):
        import json
        from industplatform import codex_agent, workflow

        pcb = workbench.add_domain(self.store, {"name": "PCB 制造与装配"})
        plan = collection.plan({"domain": "", "scenario": "阻焊与回流焊", "region": "不限",
                                "time_window": "不限", "stages": "工艺分析", "asset_types": "规范",
                                "deliverables": "工艺图谱"}, resolve_domain=True)
        owner = self.store.create(API_AGENT_ROUTE, "offline", "查阻焊与回流焊", plan=plan, public_research=True,
                                  resolve_domain=True)
        self.assertEqual(self.store.plan_history(owner["id"])[0]["plan"]["domain"], "")
        stages = []

        def execute(prompt, port, on_event, stop, *, profile, tools, **kwargs):
            schemas, call = tools
            names = {tool["name"] for tool in schemas}
            if "search_domain_libraries" in names:
                self.assertNotIn("web_search", names)
                match = call("search_domain_libraries", {"name": "阻焊及回流焊",
                             "aliases": json.dumps(["焊接装配"]), "keywords": json.dumps(["阻焊", "回流焊"])})
                self.assertEqual(match["candidates"][0]["id"], pcb["id"])
                mounted = call("mount_domain_library", {"domain_id": pcb["id"], "reason": "属于 PCB 制造及装配工艺"})
                self.assertEqual(mounted["name"], pcb["name"])
                stages.append("mount")
            else:
                self.assertIn("record_process_graph", names)
                self.assertIn(pcb["name"], prompt)
                self.assertEqual(self.store.get(owner["id"])["plan"]["domain"], pcb["name"])
                stages.append("research")
            return "研究任务"

        with patch.object(codex_agent, "execute", side_effect=execute):
            workflow.process_run(self.store, self.store.claim(), port=1234, connections={"model": "fixture"})
        self.assertEqual(stages[0:2], ["mount", "research"])
        result = self.store.get(owner["id"])["result"]
        self.assertTrue(any(row["text"] == "已匹配并挂载至现有领域库「PCB 制造与装配」" for row in result["agent_events"]))
        self.assertEqual(len(workbench.overview(self.store)["domains"]), 1)

    def test_create_rechecks_catalog_and_merge_keeps_evidence_and_scope(self):
        from industplatform import domain_libraries, local_import

        plan = collection.plan({"scenario": "插件装配", "region": "不限", "time_window": "不限",
                                "stages": "焊接", "asset_types": "规范", "deliverables": "要点"},
                               resolve_domain=True)
        profile = {"name": "PCB 制造与装配", "description": "涵盖制板、贴装及焊接", "aliases": ["PCB装配"],
                   "keywords": ["回流焊", "阻焊"], "upstream": "板材", "downstream": "成品检验"}

        def pending(prompt):
            item = self.store.create(API_AGENT_ROUTE, "offline", prompt, plan=plan.copy(),
                                     public_research=True, resolve_domain=True)
            return domain_libraries.Resolution(self.store, self.store.claim(agent_only=True)), item

        decision, first = pending("研究新领域")
        concurrent, second = pending("研究相同领域")
        for resolver in (decision, concurrent):
            self.assertEqual(resolver.search("PCB 制造", [], ["阻焊"])["total"], 0)
        mounted = decision.finish(profile=profile, reason="空库")
        with self.assertRaisesRegex(ValueError, "目录变化"):
            concurrent.finish(profile=profile, reason="使用过期检索结果")
        results = concurrent.search("PCB裝配", ["PCB装配"], ["阻焊"])
        self.assertEqual(results["exact_matches"], [mounted["id"]])
        with self.assertRaisesRegex(ValueError, "已有名称或别名"):
            concurrent.finish(profile=profile, reason="重复新建")

        first_run = self.store.get(first["id"])
        self.assertEqual(first_run["plan"]["domain"], profile["name"])
        first_run["status"] = "completed"
        self.store.update(first_run)
        secondary = workbench.add_domain(self.store, {"name": "印制电路板"})
        node = workbench.add_process_node(self.store, {"domain_id": secondary["id"], "title": "电气验收"})
        scoped = self.store.create(API_AGENT_ROUTE, "offline", "核对电气验收",
                                   scope_request={"domain_id": str(secondary["id"])})
        scoped.update(status="completed", scope_domain_id=str(secondary["id"]), scope_domain_name=secondary["name"],
                      execution_scope={"domain_id": str(secondary["id"])})
        self.store.update(scoped)
        import_receipt = json.dumps([
            {**library, "l2_code": "D01.2", "added": added, "duplicates": duplicates}
            for library, added, duplicates in ((secondary, 0, 2), ({"id": mounted["id"], "name": mounted["name"]}, 4, 0))
        ])
        with closing(self.store.connect()) as db, db:
            db.execute("""INSERT INTO local_import_batches(id,status,expected,updated_at,library_receipt)
                VALUES ('local-import','ready',1,1700000000,?)""", (import_receipt,))
        evidence = collection.save_reference(self.store, {"domain": secondary["name"], "title": "验收规范",
            "kind": "规范", "publisher": "测试机构", "url": "https://example.org/inspection",
            "locator": "第 2 章", "claim": "说明验收流程"},
            verification={"level": "document", "quote": "包含验收工序", "target": "inspection"})
        domain_libraries.manage(self.store, {"action": "merge", "id": secondary["id"], "target_id": mounted["id"]})
        summary = workbench.overview(RunStore(self.store.path), secondary["id"])
        self.assertEqual(summary["domain"]["id"], mounted["id"])
        self.assertIn("印制电路板", summary["domain"]["aliases"])
        self.assertEqual(summary["nodes"][0]["id"], node["id"])
        self.assertFalse(summary["references"])
        review = workbench.source_page(self.store, mounted["id"], "frontier", item=evidence["id"])["items"][0]
        self.assertEqual(review["state"], "legacy_review")
        self.assertEqual(collection.reference(self.store, evidence["id"])["domain"], profile["name"])
        renamed = self.store.get(scoped["id"])
        self.assertEqual(renamed["scope_domain_id"], str(mounted["id"]))
        self.assertEqual(renamed["scope_request"]["domain_id"], str(mounted["id"]))
        self.assertEqual(renamed["execution_scope"], scoped["execution_scope"])
        self.assertEqual(workbench.overview(self.store, secondary["id"], thread=scoped["id"])["thread_domain"]["id"],
                         mounted["id"])
        self.assertFalse(self.store.conversations(secondary["name"]))
        with closing(self.store.connect()) as db:
            imported = local_import.task_turn(db, "local-import")["result"]["local_import"]["domains"]
            self.assertEqual(imported, [{"id": mounted["id"], "name": profile["name"], "l2_code": "D01.2",
                                         "added": 4, "duplicates": 2}])
            self.assertEqual(db.execute("SELECT library_receipt FROM local_import_batches").fetchone()[0], import_receipt)
        domain_libraries.manage(self.store, {"action": "delete", "id": mounted["id"]})
        self.assertEqual(workbench.overview(RunStore(self.store.path))["domains"], [])
        self.assertIsNotNone(collection.reference(self.store, evidence["id"]))


class WorkspaceRouteTests(unittest.TestCase):
    def test_static_revalidation_reuses_module_without_body(self):
        from industplatform.server import RequestHandler

        handler = object.__new__(RequestHandler)
        handler.path = "/static/workbench.js"
        handler.headers = {}
        with patch.object(handler, "send_bytes") as response:
            handler.read_route()
            self.assertEqual(response.call_args.args[0], 200)
            self.assertTrue(response.call_args.args[1])
            tag = response.call_args.kwargs["etag"]
            handler.headers["If-None-Match"] = tag
            handler.read_route()
            self.assertEqual(response.call_args.args[:2], (304, b""))
            self.assertEqual(response.call_args.kwargs["etag"], tag)

    def test_legacy_entry_redirects_with_valid_domain_and_benchmark_intent(self):
        from industplatform.server import RequestHandler

        handler = object.__new__(RequestHandler)
        handler.path = "/operations?domain=legacy-name&workspace=17&bench=eval"
        with patch.object(handler, "redirect") as redirect:
            handler.read_route()
        redirect.assert_called_once_with("/?domain=17&bench=eval")

    def test_direct_workspace_paths_serve_the_same_shell(self):
        from industplatform.server import RequestHandler

        handler = object.__new__(RequestHandler)
        with patch.object(handler, "send_bytes") as response:
            for path in ("/global/assets/evaluations?difficulty=medium", "/global/settings/schema",
                         "/domains/17/sources?item=ref"):
                handler.path = path
                handler.read_route()
                self.assertEqual(response.call_args.args[0], 200)
                self.assertIn(b'id="domain-back"', response.call_args.args[1])

    def test_unknown_page_recovers_and_unknown_api_or_asset_is_404(self):
        from industplatform.server import RequestHandler

        handler = object.__new__(RequestHandler)
        handler.headers = {"Accept": "text/html"}
        with patch.object(handler, "redirect") as redirect, patch.object(handler, "send_json") as response:
            handler.path = "/retired/isolated-view"
            handler.read_route()
            redirect.assert_called_once_with("/#scope")
            for path in ("/api/no-such-endpoint", "/static/app.js", "/static/app.css"):
                handler.path = path
                handler.read_route()
                self.assertEqual(response.call_args.args[0], 404)
            redirect.assert_called_once()

import json
import tempfile
import unittest
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from industplatform import codex_agent, engineering_quality as quality, guarded_workflow, sandbox_verifier, scope_guard, scope_tools, workbench
from industplatform.storage import RunStore


GERBER = """G04 Workbench fixture*
%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.100*%
D10*
X0000000000Y0000000000D02*
X0010000000Y0000000000D01*
M02*
"""
DRILL = """;TYPE=PLATED
;Exported by PCB CAD
M48
METRIC,TZ
T01C0.8
%
T01
X1.000Y1.000
M30
"""
PASSAGES = [
    {"locator": "L1.1", "text": "Gerber coordinates must use six digits after the decimal point."},
    {"locator": "L2.1", "text": "The board outline must be present in a separate Gerber file."},
    {"locator": "L3.1", "text": "The fabrication team shall reject a drill file with unspecified units before accepting the job."},
]


def document(clauses=None):
    return {
        "title": "PCB fabrication intake specification",
        "summary_zh": "规定板框文件、坐标声明与钻孔的收件条件。",
        "publisher_info": {"name": "Fixture Engineering Office", "authority": "manufacturer", "qualification": "板厂制造文件收件规范发布部门"},
        "version_or_date": {"version": "Rev 3", "published_on": "2026-01-16"},
        "applicable_scope": {"applies_to": "刚性裸板的制板文件审核", "preconditions": ["设计方已导出当前版本的 Gerber 和 Excellon"]},
        "limitations": ["制造文件准入条件不代表贴装工序的验收要求。"],
        "extracted_clauses": clauses if clauses is not None else [
            {"kind": "threshold", "locator": PASSAGES[0]["locator"], "quote": PASSAGES[0]["text"],
             "requirement": "Gerber 小数坐标必须保留 6 位。", "check": "解析 Gerber FS 模板并比较小数位数。"},
            {"kind": "format", "locator": PASSAGES[1]["locator"], "quote": PASSAGES[1]["text"],
             "requirement": "Gerber 板框必须单独交付。", "check": "验证板框 Gerber 可解析且对应独立的板框路径。"},
            {"kind": "acceptance", "locator": PASSAGES[2]["locator"], "quote": PASSAGES[2]["text"],
             "requirement": "未声明钻孔文件单位时退回制板工单。", "check": "读取 Excellon 文件头并在无单位时输出拒收。"},
        ],
    }


def task_definition(reference, clause):
    citation = {"reference_id": reference, "clause_id": clause}
    return {
        "description": "读取实样 Gerber 和 Excellon，核对板框与孔位的坐标单位，给出 CAM 交付和拒收清单。",
        "industrial_context": {"upstream_input": "PCB 设计输出和钻孔资料", "responsibility": "板厂 CAM 工程师收件",
                               "pain_point": "孔位单位误判会使单板废弃"},
        "applicable_standards": [{"name": "PCB fabrication intake specification", "version": "Rev 3",
                                  "applies_to": "同批刚性裸板", **citation}],
        "boundary_conditions": [{"parameter": "板框和孔中心的安全间距", "limit": "≥ 0.5", "unit": "mm", "precision": "0.01 mm",
                                 "exceptions": "工单允许的边缘半孔另行提交工程评审", "basis": "case"}],
        "edge_cases_and_rejection": [{"condition": "钻孔单位缺失", "detection": "解析 Excellon M48 至 % 的单位声明",
                                      "disposition": "拒收该批制造文件，标出文件和定位并退回导出岗"}],
        "downstream_handoff": {"recipient": "板厂生产资料岗", "process": "生产准备", "interface": "收件报告 answer.json",
                               "acceptance": "输出孔位原单位、毫米坐标和刨除例外后的越界清单，并标出是否可进入 CAM 拼板"},
        "inputs": {"fab/outline.gbr": GERBER, "fab/holes.drl": DRILL},
        "input_artifacts": [
            {"path": "fab/outline.gbr", "role": "唯一机械板框", "origin": "sandbox_fixture", "provenance": "工位复现实样，坐标单位为 mm"},
            {"path": "fab/holes.drl", "role": "通孔坐标原件", "origin": "sandbox_fixture", "provenance": "工位复现实样，保留 M48 真实语法"},
        ],
        "deliverables": [{"path": "answer.json", "format": "JSON", "interface": "holes 数组使用毫米坐标",
                          "acceptance": "比较板框与钻孔中心位置，保留单位、检查判定和失败工件路径"}],
        "value_assessment": {
            "frequency": {"min": 8, "max": 15, "period": "week", "basis": "每周 8–15 个板号发布示例工况", "source_status": "estimate"},
            "expert_minutes": {"min": 20, "max": 35, "basis": "3 项格式核对各 5–8 分钟再整理交接单", "source_status": "estimate"},
            "error_loss": {"min": 500, "max": 1250, "currency": "CNY", "scenario": "孔偏导致 5 张工程样板返工",
                           "basis": "每张重制报价假设 100–250 元", "source_status": "estimate"},
            "automation_value": {"benefit": "自动定位可疑工件并缩短复核等待时间", "basis": "按每周 8 个板号估算节省重复开档时间"},
        },
        "reference_citations": [{**citation, "applies_to": "识别缺失钻孔单位的拒收条件"}],
    }


def admission_document(clauses=None, *, nodes=None, **classifications):
    return {**document(clauses), "authority_tier": "T2_MANUFACTURER", "source_channel": "C2_OEM_FAB_SPEC",
            "process_node_ids": ["cam"] if nodes is None else nodes, "object_cluster": "pcb-intake",
            "lifecycle_stages": ["S3", "S5"], **classifications}


def seed_qualified_reference(db, domain, node, identity="ref", source="ref-source", profile=None):
    profile = profile or quality.reference_document(document(), PASSAGES)
    first = profile["extracted_clauses"][0]
    db.execute("""INSERT INTO verified_references(domain_id,id,source_id,node_id,publisher,title,authority,locator,finding,
        permission,verified_at,excerpt,summary_zh,quality_json,authority_tier,source_channel,object_cluster,lifecycle_stages_json)
        VALUES(?,?,?,?,?,?,?,?,?,'unknown','2026-01-16',?,?,?,'T2_MANUFACTURER','C2_OEM_FAB_SPEC','pcb-intake','["S3","S5"]')""",
        (domain, identity, source, node, profile["publisher_info"]["name"], profile["title"], "manufacturer",
         first["locator"], first["requirement"], first["quote"], profile["summary_zh"], json.dumps(profile)))
    if node:
        db.execute("INSERT INTO verified_reference_nodes VALUES (?,?,?)", (domain, identity, node))
    return profile


class EngineeringAdmissionTests(unittest.TestCase):
    def test_published_process_checks_qualify_without_an_artificial_numeric_limit(self):
        for quote in (
            "After reflow, assembled PCBs undergo automated optical inspection.",
            "SPI measures paste height, area, and volume to verify the solder deposits.",
            "The machine checks hole position and diameter against the drill file.",
            "Reflow soldering attaches SMT components to the PCB using a controlled thermal profile.",
        ):
            with self.subTest(quote=quote):
                clause = {"kind": "process", "locator": "L1.1", "quote": quote,
                          "requirement": "按相应 PCB 工序执行检查。", "check": "检查工序中的 PCB 交接记录。"}
                record = quality.reference_document(document([clause]), [{"locator": "L1.1", "text": quote}])
                self.assertEqual(len(record["extracted_clauses"]), 1)
        self.assertFalse(quality.technical_excerpt("Our industry-leading system provides excellent PCB inspection."))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = RunStore(Path(folder.name) / "admission.sqlite3")
        self.domain = workbench.add_domain(self.store, {"name": "PCB 验收测试"})["id"]
        with closing(self.store.connect()) as db, db:
            db.executemany("INSERT INTO process_nodes(domain_id,id,title,position) VALUES (?,?,?,?)",
                           [(self.domain, "cam", "板厂 CAM 入厂核对", 1), (self.domain, "release", "生产准备", 2)])
            db.execute("INSERT INTO process_edges VALUES(?, 'cam', 'release')", (self.domain,))

    def stage(self, number, *, limited=True):
        run = self.store.create(codex_agent.ROUTE, "offline", "测试准入")
        run["status"] = "running"
        self.store.update(run)
        stage = scope_guard.STAGES[number - 1]
        narrow = {"granularity": "PROCESS_NODE_ONLY", "allowed_process_node_ids": ["cam"]} if limited else {}
        contract = scope_guard.ExecutionScopeContract.parse({"target_stage": stage, "domain_id": self.domain,
            "router_confidence": 1, **narrow})
        with closing(self.store.connect()) as db, db:
            guarded_workflow._store_contract(db, run, contract, "executing")
            self.store.save(db, run)
        return scope_tools.StageTools(self.store, run, contract)

    @property
    def scope(self):
        return {"domain_id": str(self.domain), "process_node_id": "cam"}

    def source(self, source, passages, url=None):
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO raw_sources(domain_id,id,canonical_url,version,sha256,status) VALUES(?,?,?,?,?,'parsed')",
                       (self.domain, source, url or "https://example.test/" + source, "fixed-version", "a" * 64))
            db.execute("INSERT INTO workflow_node_sources VALUES (?, 'cam', ?)", (self.domain, source))
            db.execute("""INSERT INTO workflow_source_passages(domain_id,node_id,source_id,body)
                          VALUES (?, 'cam', ?, ?)""", (self.domain, source, json.dumps(passages)))

    def test_reference_gate_archives_promo_and_keeps_clause_addresses(self):
        stage = self.stage(3)
        quote = "AutoRebuild automatically reconstructs Gerber projects and improves productivity."
        self.source("product", [{"locator": "L1.1", "text": quote}])
        promo = [{**document()["extracted_clauses"][0], "kind": "format", "quote": quote}]
        result = stage.call("promote_verified_ref", {**self.scope, "source_id": "product", "document_json": json.dumps(admission_document(promo))})
        self.assertTrue(result["filtered"], result)
        self.assertFalse(workbench.source_page(self.store, self.domain, "references")["items"])
        self.assertEqual(workbench.source_page(self.store, self.domain, "frontier")["items"][0]["state"], "filtered")

        self.source("technical", PASSAGES)
        data = admission_document()
        invalid = {**self.scope, "source_id": "technical"}
        self.assertIn("version_or_date", stage.call("promote_verified_ref", {**invalid,
            "document_json": json.dumps({key: item for key, item in data.items() if key != "version_or_date"})})["error"])
        data["extracted_clauses"][0]["quote"] = "A fabricated 5.5 mm tolerance."
        self.assertIn("精确定位", stage.call("promote_verified_ref", {**invalid, "document_json": json.dumps(data)})["error"])
        command = "Usage: kicad-cli pcb export drill [--output OUTPUT_DIR]"
        self.source("cli", [{"locator": "L1.1", "text": command}])
        misuse = {**document()["extracted_clauses"][0], "kind": "command", "quote": command,
                  "requirement": "必须指定 --output 参数"}
        rejected = stage.call("promote_verified_ref", {**self.scope, "source_id": "cli",
            "document_json": json.dumps(admission_document([misuse]))})
        self.assertIn("可选参数", rejected["error"])
        admitted = stage.call("promote_verified_ref", {**invalid, "document_json": json.dumps(admission_document())})
        self.assertEqual(len(admitted["clauses"]), 3)
        ref = workbench.reference_detail(self.store, self.domain, admitted["id"])
        self.assertEqual(ref["version_or_date"]["version"], "Rev 3")
        self.assertEqual([clause["id"] for clause in ref["extracted_clauses"]], [clause["id"] for clause in admitted["clauses"]])
        self.assertEqual(len(workbench.source_page(self.store, self.domain, "references")["items"]), 1)

    def test_verified_document_links_multiple_nodes_with_scope_and_taxonomy_checks(self):
        self.source("cli-manual", PASSAGES, "https://docs.kicad.org/10.0/en/cli/cli.html")
        broad = self.stage(3, limited=False)
        narrow = self.stage(3)
        payload = admission_document(nodes=["01", "02"], authority_tier="T3_TOOLCHAIN", source_channel="C3_TOOL_FORMAT_DOC")

        def admit(stage, data, source="cli-manual"):
            return stage.call("promote_verified_ref", {**self.scope, "source_id": source, "document_json": json.dumps(data)})

        self.assertIn("锁定范围", admit(narrow, payload)["error"])
        self.assertIn("C3_TOOL_FORMAT_DOC", admit(broad, admission_document())["error"])
        self.assertIn("process_node_ids", admit(broad, {**payload, "process_node_ids": ["cam", "unassigned"]})["error"])
        accepted = admit(broad, payload)
        self.assertEqual(accepted["process_node_ids"], ["cam", "release"])
        ref = accepted["id"]
        narrowed_patch = {**payload, "process_node_ids": ["cam"], "summary_zh": "重写共享文献摘要。"}
        self.assertIn("锁定范围", admit(narrow, narrowed_patch)["error"])
        self.source("unassigned", PASSAGES)
        self.assertIn("领域范围", admit(narrow, admission_document(nodes=["unassigned"]), "unassigned")["error"])
        unassigned = admit(broad, admission_document(nodes=["unassigned"]), "unassigned")
        self.assertEqual(unassigned["process_node_ids"], ["unassigned"])
        selected = workbench.source_page(self.store, self.domain, "references", node="02",
                                         tier="T3_TOOLCHAIN", channel="C3_TOOL_FORMAT_DOC")
        self.assertEqual([item["id"] for item in selected["items"]], [ref])
        self.assertEqual(selected["node_title"], "生产准备")
        self.assertEqual(sum(item["verified"] for item in selected["channels"]), 2)
        self.assertEqual(len(selected["channels"]), 7)
        self.assertTrue(next(process for process in selected["processes"] if process["id"] == "release")["missing_t1_standard"])
        self.assertEqual([item["id"] for item in workbench.source_page(
            self.store, self.domain, "references", node="unassigned")["items"]], [unassigned["id"]])
        with closing(self.store.connect()) as db:
            self.assertIn(ref, quality.eligible_references(db, self.domain, "release"))
            self.assertNotIn(unassigned["id"], quality.eligible_references(db, self.domain, "cam"))
        self.assertEqual([item["id"] for item in workbench.reference_detail(
            RunStore(self.store.path), self.domain, ref)["processes"]], ["cam", "release"])

    def test_task_gate_rejects_json_and_saves_clause_lineage_with_real_files(self):
        self.source("technical", PASSAGES)
        stage3 = self.stage(3)
        data = stage3.call("promote_verified_ref", {**self.scope, "source_id": "technical", "document_json": json.dumps(admission_document())})
        ref = data["id"]
        clause = data["clauses"][-1]["id"]
        with closing(self.store.connect()) as db:
            self.assertIn("企业交付关系", quality.task_admission(db, self.domain, ["cam"])[0]["issues"][0]["message"])
        stage4 = self.stage(4)
        inputs = {**self.scope, "task_id": "cam-fixture"}
        definition = task_definition(ref, clause)
        valid = {"title": "Gerber 与钻孔文件工艺验收", "definition": definition, "state": "ready",
                 "complexity": "L3", "downstream_id": "release"}
        write = lambda item: stage4.call("upsert_benchmark_task", {**inputs, "patch_json": json.dumps(item)})
        self.assertIn("企业交付关系", write(valid)["error"])
        stage2 = self.stage(2)
        relation = {"sender": "设计交付岗", "recipient": "板厂 CAM", "deliverable": "Gerber 和钻孔文件",
                    "kind": "supplier_requirement", "reference_id": ref, "clause_id": clause}
        self.assertTrue(stage2.call("upsert_process_relation", {**self.scope, "relation_id": "delivery",
            "relationship_json": json.dumps(relation)})["changed"])
        self.assertIn("clause_id", write({**valid, "definition": {**definition,
            "reference_citations": [{**definition["reference_citations"][0], "clause_id": "missing"}]}})["error"])
        self.assertIn("boundary_conditions", write({**valid, "definition": {
            key: value for key, value in definition.items() if key != "boundary_conditions"}})["error"])
        self.assertIn("工程格式", write({**valid, "definition": {**definition,
            "inputs": {"fab/outline.gbr": '{"files": []}', "fab/holes.drl": DRILL}}})["error"])
        self.assertIn("真实工程格式", write({**valid, "definition": {**definition,
            "inputs": {"manifest.json": '{"files": []}'}, "input_artifacts": [{"path": "manifest.json",
                "role": "入厂台账", "origin": "supporting", "provenance": "故意残缺测试样本"}]}})["error"])
        self.assertIn("具体内容", write({**valid, "title": "probe"})["error"])
        self.assertIn("具体内容", write({**valid, "definition": {**definition,
            "value_assessment": {**definition["value_assessment"], "automation_value": {
                "benefit": "本题不据此推算", "basis": "待定"}}}})["error"])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM benchmark_tasks").fetchone()[0], 0)
        self.assertEqual(write(valid), {"id": "cam-fixture"})
        summary = guarded_workflow.domain_tasks(self.store, self.domain, summary=True)
        self.assertNotIn("inputs", summary["items"][0]["details"])
        self.assertTrue(summary["items"][0]["quality_approved"])
        self.assertTrue(summary["admission"][0]["ready"])
        task = guarded_workflow.domain_tasks(self.store, self.domain, item="cam-fixture")["items"][0]
        self.assertEqual({file["format"] for file in task["details"]["input_artifacts"]}, {"Gerber", "Excellon"})

        spec = {**sandbox_verifier.DEFAULT, "layers": {**sandbox_verifier.DEFAULT["layers"], "L2": [
            {"title": "钻孔必须声明单位", "argv": ["check-units"], "reference_citations": definition["reference_citations"]},
        ]}}
        sandbox_verifier.save(self.store, self.domain, 0, spec, "cam-fixture")
        detail = workbench.reference_detail(self.store, self.domain, ref)
        self.assertEqual(detail["task_usage"][0]["citations"][0]["clause_id"], clause)
        self.assertEqual(detail["verifier_usage"][0]["layer"], "L2")
        self.assertEqual(detail["verifier_usage"][0]["clause_id"], clause)

    def test_public_artifact_registration_pins_a_file_without_copying_its_body(self):
        stage = self.stage(3)
        mime = Message()
        mime["Content-Type"] = "application/octet-stream"
        url = "https://example.com/outline.gbr"
        with patch.object(scope_tools.public_web, "fetch", return_value=(url, mime, GERBER.encode())):
            file = stage.call("save_raw_source", {**self.scope, "url": url})
        self.assertEqual(file["artifact"]["format"], "Gerber")
        with patch.object(scope_tools.public_web, "fetch", side_effect=AssertionError("unexpected fetch")):
            cached = stage.call("save_raw_source", {**self.scope, "source_id": file["id"]})
        self.assertEqual(cached["artifact"], file["artifact"])
        self.assertEqual(len(file["sha256"]), 64)
        with closing(self.store.connect()) as db:
            artifact = json.loads(db.execute("""SELECT artifact_json FROM workflow_source_passages
                WHERE source_id=?""", (file["id"],)).fetchone()[0])
        self.assertEqual(artifact, {"format": "Gerber", "bytes": len(GERBER.encode())})


if __name__ == "__main__":
    unittest.main()

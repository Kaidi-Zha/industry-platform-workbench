import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from industplatform import codex_agent, guarded_workflow, domain_libraries, sandbox_verifier, scope_guard, scope_tools, workbench, workflow
from industplatform.server import RequestHandler, chat_turn, retry_stage
from industplatform.storage import RunStore
from tests.test_engineering_quality import PASSAGES, admission_document, seed_qualified_reference, task_definition

PCB_CLASSIFICATION = {"l2_code": "D01.2", "object_cluster": "pcb-assembly", "lifecycle_stages": ["S5"]}


class WorkflowScopeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = RunStore(Path(self.folder.name) / "scope.sqlite3")
        with closing(self.store.connect()) as db, db:
            self.domain = domain_libraries.register(db, "PCB 制造")
            self.other = domain_libraries.register(db, "首件检验")
            for domain in (self.domain, self.other):
                db.executemany("""INSERT INTO process_nodes(domain_id,id,title,position)
                    VALUES(?,?,?,?)""", [(domain, "solder", "回流焊", 1), (domain, "inspect", "光学检查", 2),
                                          (domain, "release", "放行", 3)])
                db.execute("""INSERT INTO raw_sources(domain_id,id,canonical_url,version,sha256,status)
                    VALUES(?,'ref-source','https://example.org/process','1',?,'parsed')""", (domain, 'a' * 64))
                profile = seed_qualified_reference(db, domain, "solder")
                clause = profile["extracted_clauses"][-1]["id"]
                db.execute("""UPDATE verified_references SET finding='Assembly limits'
                              WHERE domain_id=? AND id='ref'""", (domain,))
                db.execute("""INSERT INTO process_relations(domain_id,node_id,id,sender,recipient,deliverable,kind,
                    evidence_id,evidence_clause_id,state) VALUES(?,'solder','handoff','前道工位','回流焊岗','工单',
                    'supplier_requirement','ref',?,'verified')""", (domain, clause))
                for number, node in enumerate(("solder", "inspect", "solder"), 1):
                    task = f"task-{number}"
                    db.execute("""INSERT INTO benchmark_tasks(domain_id,id,node_id,title,definition_json,state)
                        VALUES(?,?,?,?,?,'ready')""", (domain, task, node, f"实验样题 {number}",
                                                        json.dumps({**task_definition("ref", clause), "description": "确认样本状态"}
                                                                   if node == "solder" else {"description": "确认样本状态"})))
                    if node == "solder":
                        db.execute("INSERT INTO task_references VALUES (?,?,'ref')", (domain, task))
                    db.execute("""INSERT INTO verifier_specs(domain_id,task_id,version,strategy,layers_json,self_tests_json)
                        VALUES(?,?,1,'progressive',?,?)""", (domain, task, json.dumps({f"L{i}": [] for i in range(1, 5)}),
                            json.dumps([{"variant": kind, "result": {"expected": True}}
                                        for kind in ("standard", "incomplete")])))
                    db.execute("""INSERT INTO eval_runs(domain_id,id,task_id,verifier_version,status,model_matrix_json,pass_at_k,
                        difficulty,confirmed_at) VALUES(?,?,?,1,'completed','[]',1,'medium','2026-01-01')""",
                        (domain, f"eval-{number}", task))

    def queued(self, prompt, domain=True, hint=None, **options):
        return self.store.create(codex_agent.ROUTE, "offline", prompt,
            scope_request={"domain_id": str(self.domain) if domain else None, "target": hint or {}, **options})

    def raw(self, stage, **values):
        return json.dumps({"target_stage": scope_guard.STAGES[stage - 1], "domain_id": str(self.domain),
            "router_confidence": .97, "rationale": "识别到目标工作", **values}, ensure_ascii=False)

    def process(self, fixture):
        with patch.object(codex_agent, "execute", side_effect=fixture):
            workflow.process_run(self.store, self.store.claim(), 18765, threading.Event())

    def classify(self, call, label=PCB_CLASSIFICATION):
        code = label["l2_code"]
        result = call("lookup_industry_taxonomy", {"l2_code": code, "hint_offset": 0, "cluster_offset": 0})
        self.assertEqual(result["industry"]["l2_code"], code)
        self.assertIn("existing_object_clusters", result)
        self.assertEqual(call("classify_industry_task", {"classification_json": json.dumps(label)}), label)

    def states(self, table, name):
        with closing(self.store.connect()) as db:
            return db.execute(f"SELECT domain_id,{name},is_stale FROM {table} ORDER BY domain_id,{name}").fetchall()

    def test_unexhausted_acquisition_resumes_with_original_scope_after_reopen(self):
        stage = scope_guard.STAGES[2]
        turn = self.queued("穷尽采集回流焊工艺资料", hint={"stage": stage, "process_node_id": "solder"})

        def first_pass(_, port, event, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                self.assertTrue(call("list_domains", {"query": "PCB"})["domains"])
                return self.raw(3, granularity="PROCESS_NODE_ONLY", allowed_process_node_ids=["solder"])
            self.assertTrue(call("propose_upstream_suggestion", {"target_stage": scope_guard.STAGES[1],
                                                              "reason": "待查节点连接的出处"})["recorded"])
            return "保留待查范围"

        self.process(first_pass)
        before = self.store.get(turn["id"])
        self.assertEqual(before["status"], "needs_review")
        self.assertTrue(RunStore.can_resume(before))
        self.assertEqual([event["label"] for stage in before["result"]["stage_timeline"] for event in stage["events"]],
                         ["list_domains", "propose_upstream_suggestion"])
        self.store = RunStore(self.store.path)
        queued, stopping, discarded = self.store.control(turn["id"], "resume")
        self.assertEqual((queued["status"], stopping, discarded), ("queued", [], []))
        self.assertEqual(queued["execution_scope"]["contract_id"], before["execution_scope"]["contract_id"])
        self.assertEqual(queued["execution_scope"]["allowed_process_node_ids"], ["solder"])

        def resume(_, port, event, stop, **options):
            self.assertNotIn("list_domains", {tool["name"] for tool in options["tools"][0]})
            return "本轮继续核验"

        self.process(resume)
        self.assertEqual(self.store.get(turn["id"])["restarts"], 1)
        self.assertEqual(len(self.store.conversation(turn["conversation_id"])), 1)

    def test_explicit_domain_stage_and_readonly_review_use_persisted_contracts(self):
        run = self.queued("只执行阶段 1 领域定位；不要采集资料。")

        def locate(_, port, event, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                event("harness.thread", {"thread": "01a1fcfa-bbe7-7cf2-8e94-e2dcd8a32bbf"}, None)
                return self.raw(3)
            event("harness.thread", {"thread": "01a1fcfa-dea0-7683-be47-6e9ba685a901"}, None)
            self.assertIn("search_domain_libraries", {tool["name"] for tool in schemas})
            self.assertNotIn("enqueue_frontier", {tool["name"] for tool in schemas})
            self.classify(call)
            return "现有领域为 PCB 制造"

        self.process(locate)
        self.assertEqual(self.store.conversations()[0]["stage"], scope_guard.STAGES[0])
        saved = self.store.get(run["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual([(item["stage_key"], item["thread_id"]) for item in saved["result"]["stage_timeline"]],
                         [("routing", "01a1fcfa-bbe7-7cf2-8e94-e2dcd8a32bbf"), ("stage_1", "01a1fcfa-dea0-7683-be47-6e9ba685a901")])
        review = self.store.create(codex_agent.ROUTE, "offline", "只读取回流焊已有文献，不联网。",
            conversation_id=run["conversation_id"], scope_request={"domain_id": str(self.domain), "target": {}})

        def review_local(prompt, port, event, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                return self.raw(3)
            self.assertEqual({tool["name"] for tool in schemas}, {"get_topology_summary", "list_reference_summaries"})
            self.assertIn("Assembly limits", prompt)
            self.assertIn("error", call("enqueue_frontier", {"domain_id": str(self.domain),
                "process_node_id": "solder", "query": "回流焊"}))
            return "根据已有文献复核"

        with patch.object(scope_tools.public_web, "search") as search:
            self.process(review_local)
            search.assert_not_called()
        current = self.store.get(review["id"])
        self.assertEqual(current["execution_scope"]["operation_mode"], scope_guard.READ_ONLY)
        self.assertFalse(current["result"]["scope_continue"])
        self.assertEqual(next(step["summary"] for step in current["steps"] if step["id"] == "agent"), "只读复核完成")
        self.assertEqual(self.store.conversations()[0]["stage"], scope_guard.STAGES[2])
        self.assertEqual(self.store.conversations()[0]["initial_stage"], scope_guard.STAGES[0])
        self.assertEqual(current["result"]["scope_receipts"][0]["changes"], [])
        self.assertFalse(guarded_workflow._is_readonly("只执行阶段 3 穷尽采集；验证器与评测阶段为只读。"))

    def test_graph_progress_updates_from_evidence_and_channel_work(self):
        nodes = lambda: {node["id"]: node for node in workbench.overview(self.store, self.domain)["nodes"]}
        baseline = nodes()
        self.assertGreater(baseline["solder"]["coverage"], 0)
        self.assertEqual(baseline["inspect"]["coverage"], 0)
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO frontier_rounds(domain_id,node_id,number) VALUES (?,'solder',1)", (self.domain,))
            db.execute("""INSERT INTO crawl_frontier(domain_id,node_id,id,round_number,channel_id,kind,target,
                 query_expansion,state) VALUES (?,'solder','search',1,'C2_OEM_FAB_SPEC','query','回流焊规范','synonym','done')""", (self.domain,))
        self.assertGreater(nodes()["solder"]["coverage"], baseline["solder"]["coverage"])
        turn = self.queued("只执行阶段 3 补查回流焊")

        def collect(_, port, event, stop, **options):
            if any(tool["name"] == "list_domains" for tool in options["tools"][0]):
                return self.raw(3)
            in_progress = nodes()
            self.assertEqual(in_progress["solder"]["crawl_status"], "running")
            self.assertEqual(in_progress["inspect"]["crawl_status"], "idle")
            return "已查看队列"

        self.process(collect)
        self.assertEqual(self.store.get(turn["id"])["status"], "completed")
        self.assertEqual(nodes()["solder"]["crawl_status"], "idle")

    def test_domain_routing_can_start_without_a_selected_library(self):
        stage = scope_guard.STAGES[0]
        run = self.queued("请定位 PCB 裸板交付领域", domain=False, hint={"stage": stage})
        def fixture(_, port, event, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                return self.raw(1, domain_id=None)
            name = "PCB 裸板交付"
            self.assertIn("candidates", call("search_domain_libraries", {"name": name}))
            profile = {"profile_json": json.dumps({
                "name": name, "description": "裸板制造文件", "keywords": ["Gerber"],
                "upstream": "PCB 文件导出", "downstream": "板厂 CAM 入厂核对",
            })}
            self.assertIn("确认官方 L2", call("create_domain_library", profile)["error"])
            label = {**PCB_CLASSIFICATION, "object_cluster": "pcb-fabrication-cam"}
            self.assertIn("请先查询", call("classify_industry_task", {"classification_json": json.dumps(label)})["error"])
            self.classify(call, label)
            created = call("create_domain_library", profile)
            self.assertEqual(created["name"], name)
            return "新领域已登记"
        self.process(fixture)
        saved = self.store.get(run["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["scope_domain_name"], "PCB 裸板交付")
        self.assertEqual(saved["task_taxonomy"]["object_cluster"], "pcb-fabrication-cam")

    def test_acquisition_reuses_only_references_in_the_locked_process(self):
        payload = {"target_stage": scope_guard.STAGES[2], "domain_id": self.domain,
                   "granularity": "PROCESS_NODE_ONLY", "allowed_process_node_ids": ["solder"]}
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO workflow_node_sources VALUES (?, 'solder', 'ref-source')", (self.domain,))
            scoped = guarded_workflow._local_context(db, scope_guard.ExecutionScopeContract.parse(payload))
            other = guarded_workflow._local_context(db, scope_guard.ExecutionScopeContract.parse({
                **payload, "allowed_process_node_ids": ["inspect"],
            }))
        self.assertEqual([row["id"] for row in scoped["references"]], ["ref"])
        self.assertEqual(other["references"], [])
        nodes = workbench.overview(self.store, self.domain)["nodes"]
        self.assertEqual(next(row["sources"] for row in nodes if row["id"] == "solder"), 1)

    def test_stage_two_catalogs_only_participants_grounded_in_the_target_process(self):
        stage = scope_guard.STAGES[1]
        run = self.queued("为回流焊梳理核心制造主体", hint={"stage": stage, "process_node_id": "solder"})

        def fixture(_, port, event, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                return self.raw(2, granularity="PROCESS_NODE_ONLY", allowed_process_node_ids=["solder"])
            payload = {"name": "示例制造主体", "role": "manufacturer", "reference_id": "ref"}
            base = {"domain_id": str(self.domain), "process_node_id": "solder"}
            self.assertIn("error", call("upsert_process_node", {
                **base, "patch_json": json.dumps({"participant": {**payload, "reference_id": "unverified"}})}))
            self.assertEqual(call("upsert_process_node", {
                **base, "patch_json": json.dumps({"participant": payload})})["id"], "solder")
            return "工序主体已登记"

        self.process(fixture)
        self.assertEqual(self.store.get(run["id"])["status"], "completed")
        graph = workbench.overview(self.store, self.domain)
        company, = graph["participants"]
        self.assertEqual((company["role"], company["stage"], company["locator"]), ("manufacturer", "回流焊", "L1.1"))
        self.assertEqual(len(graph["nodes"][0]["channels"]), 7)
        self.assertEqual(graph["stage_states"][stage], "completed")
        self.assertEqual(workbench.overview(self.store, self.other)["participants"], [])

    def test_stage_one_leaves_call_budget_to_save_the_classification(self):
        run = self.queued("确认 PCB 制造所属行业", stage_queue=[scope_guard.STAGES[0]])

        def simulate(_, port, observe, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                return self.raw(1)
            lookup = {"l2_code": PCB_CLASSIFICATION["l2_code"]}
            for _ in range(scope_tools.STAGE_TOOL_LIMIT - scope_tools.DOMAIN_WRITE_RESERVE):
                self.assertIn("industry", call("lookup_industry_taxonomy", lookup))
            notice = call("lookup_industry_taxonomy", lookup)
            self.assertEqual(notice["category"], "tool_budget")
            self.assertNotIn("code", notice)
            options["guard_check"]()
            self.assertEqual(call("classify_industry_task", {"classification_json": json.dumps(PCB_CLASSIFICATION)}),
                             PCB_CLASSIFICATION)
            return "已保存分类"

        self.process(simulate)
        saved = self.store.get(run["id"])
        attempt = saved["result"]["stage_timeline"][-1]
        self.assertEqual((saved["status"], attempt["status"]), ("completed", "completed"))
        self.assertEqual(saved["task_taxonomy"], PCB_CLASSIFICATION)
        self.assertEqual(attempt["receipt"]["rejections"][0]["category"], "tool_budget")

    def test_exhausted_stage_budget_stops_run_and_allows_retry_at_same_scope(self):
        run = self.queued("确认 PCB 制造所属行业并补工艺图谱",
                          stage_queue=list(scope_guard.STAGES[:2]), execution_pace=scope_guard.AUTO_CONTINUE_QUEUE)
        code = scope_guard.STAGE_TOOL_LIMIT_CODE

        def simulate(_, port, observe, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                return self.raw(1, requested_stages=list(scope_guard.STAGES[:2]))
            self.assertIn("industry", call("lookup_industry_taxonomy", {"l2_code": PCB_CLASSIFICATION["l2_code"]}))
            for _ in range(scope_tools.STAGE_TOOL_LIMIT - 1):
                self.assertNotIn("error", call("update_domain_meta", {
                    "domain_id": str(self.domain), "patch_json": '{"description":"审查领域"}',
                }))
            result = call("classify_industry_task", {"classification_json": json.dumps(PCB_CLASSIFICATION)})
            self.assertEqual((result["category"], result["code"]), ("tool_budget", code))
            self.assertEqual(call("update_domain_meta", {})["code"], code)
            with self.assertRaises(codex_agent.AgentError) as context:
                options["guard_check"]()
            self.assertEqual(context.exception.diagnostic["code"], code)
            return "即使适配器返回成功，也应结束本阶段"

        self.process(simulate)
        saved = self.store.get(run["id"])
        self.assertEqual(saved["status"], "needs_review")
        self.assertNotIn("task_taxonomy", saved)
        timeline = saved["result"]["stage_timeline"]
        self.assertEqual([item["stage_key"] for item in timeline], ["routing", "stage_1"])
        attempt = timeline[-1]
        self.assertEqual((attempt["status"], attempt["error"]["code"]), ("failed", code))
        self.assertTrue(attempt["error"]["retryable"])
        self.assertEqual([entry["category"] for entry in attempt["receipt"]["rejections"]], ["tool_budget"])
        self.store.control(run["id"], "resume", stage_attempt=attempt["id"])
        self.assertEqual(self.store.get(run["id"])["execution_scope"]["target_stage"], scope_guard.STAGES[0])

    def test_stage_two_guard_aborts_on_third_boundary_rejection_and_keeps_retry_in_scope(self):
        current, later = scope_guard.STAGES[1:3]
        run = self.queued("为回流焊复核工艺图谱", hint={"stage": current, "process_node_id": "solder"})
        base = {"domain_id": str(self.domain), "process_node_id": "solder"}

        def simulate(prompt, port, observe, stop, **options):
            schemas, call = options["tools"]
            names = {tool["name"] for tool in schemas}
            if "list_domains" in names:
                return self.raw(2, granularity="PROCESS_NODE_ONLY", allowed_process_node_ids=["solder"])
            self.assertFalse(names & {"classify_industry_task", "save_raw_source", "promote_verified_ref", "delete_process_node"})
            allowed_line = next(line for line in options["guidance"].splitlines() if "本轮唯一允许的写入工具" in line)
            self.assertIn("upsert_process_relation", allowed_line)
            self.assertNotIn("save_raw_source", allowed_line)
            guard = options["guard_check"]
            self.assertEqual(call("search_topology_evidence", {**base, "query": "回流焊"})["category"], "source_rate_limit")
            self.assertEqual(call("read_topology_evidence", {
                **base, "url": "https://example.org/intake?signature=fixture",
            })["category"], "source_policy")
            guard()
            self.assertEqual(call("save_raw_source", base)["category"], "scope_guard")
            guard()
            self.assertEqual(call("upsert_process_node", {
                **base, "process_node_id": "inspect", "patch_json": '{"title":"不可修改"}',
            })["category"], "scope_guard")
            guard()
            # Some App Server versions reject hallucinated MCP calls before dispatching the callback.
            native = {"kind": "tool", "label": "promote_verified_ref", "thread": "attempt-two",
                      "turn": "tool-turn", "item": "forbidden", "status": "failed",
                      "arguments": {**base, "url": "https://fixture:password@example.org/intake#secret"}}
            observe("harness.item", native, None)
            observe("harness.item", native, None)
            with self.assertRaisesRegex(codex_agent.AgentError, "累计 3 次"):
                guard()
            # The outer runner must still abort even if a model adapter incorrectly claims success.
            return "不应写入"

        with patch.object(scope_tools.public_web, "search", side_effect=scope_tools.public_web.SearchError("Crossref", 429)), \
                patch.object(scope_tools.public_web, "fetch", side_effect=scope_tools.public_web.SourcePolicyError("原文地址解析到非公网网络")):
            self.process(simulate)
        handler = object.__new__(RequestHandler)
        handler.path = "/api/conversations/" + run["conversation_id"]
        handler.server = SimpleNamespace(store=self.store, workers=SimpleNamespace(pending_approval=lambda _: None))
        with patch.object(handler, "send_json") as response:
            handler.read_route()
        status, history = response.call_args.args
        self.assertEqual(status, 200)
        attempt = history["turns"][0]["result"]["stage_timeline"][-1]
        self.assertEqual((attempt["stage_key"], attempt["status"], attempt["error"]["code"]),
                         ("stage_2", "failed", scope_guard.GUARD_ABORT_CODE))
        self.assertEqual(attempt["error"]["suggested_stages"], [later])
        receipt = attempt["receipt"]
        self.assertEqual(receipt["changes"], [])
        self.assertEqual([(entry["tool"], entry["category"]) for entry in receipt["rejections"]], [
            ("search_topology_evidence", "source_rate_limit"), ("read_topology_evidence", "source_policy"),
            ("save_raw_source", "scope_guard"), ("upsert_process_node", "scope_guard"), ("promote_verified_ref", "scope_guard"),
        ])
        self.assertEqual(receipt["rejections"][1]["parameters"]["url"], "https://example.org/intake")
        self.assertEqual(receipt["rejections"][3]["parameters"]["process_node_id"], "inspect")
        self.assertEqual(receipt["rejections"][4]["parameters"]["url"], "https://example.org/intake")
        self.assertEqual(workbench.overview(self.store, self.domain)["nodes"][1]["title"], "光学检查")

        def control(*args, **kwargs):
            return self.store.control(*args, **kwargs)[0]

        server = SimpleNamespace(store=self.store, workers=SimpleNamespace(control=control))
        payload = {"action": "retry_stage", "attempt_id": attempt["id"]}
        with self.assertRaisesRegex(ValueError, "只可为可写任务确认"):
            retry_stage(server, run["id"], {**payload, "later_stages": [scope_guard.STAGES[0]]})
        resumed = retry_stage(server, run["id"], {**payload, "later_stages": [later]})
        self.assertEqual(resumed["execution_scope"]["stage_queue"], (current, later))
        self.assertEqual(resumed["execution_scope"]["target_stage"], current)

        def retry(prompt, port, observe, stop, **options):
            options["guard_check"]()
            names = {tool["name"] for tool in options["tools"][0]}
            self.assertIn("upsert_process_node", names)
            self.assertFalse(names & {"list_domains", "enqueue_frontier", "promote_verified_ref"})
            return "已复核回流焊工序"

        self.process(retry)
        saved = self.store.get(run["id"])
        first, last = saved["result"]["stage_timeline"][-2:]
        self.assertEqual((first["id"], last["stage_key"], saved["status"]), (attempt["id"], "stage_2", "awaiting_stage"))
        self.assertEqual(last["receipt"]["rejections"], [])
        self.assertEqual(saved["execution_scope"]["next_stage"], later)

    def test_topology_summary_paginates_nodes_and_edges_independently(self):
        with closing(self.store.connect()) as db, db:
            rows = [(self.domain, f"step-{i}", f"工序 {i}", i + 4) for i in range(8)]
            db.executemany("INSERT INTO process_nodes(domain_id,id,title,position) VALUES (?,?,?,?)", rows)
            db.executemany("INSERT INTO process_edges VALUES (?,?,?)",
                           [(self.domain, f"step-{i}", f"step-{i+1}") for i in range(7)])
            first = scope_tools.topology_summary(db, self.domain)
            second = scope_tools.topology_summary(db, self.domain, first["next_offset"], first["next_edge_offset"])
            self.assertEqual(len(first["nodes"]), scope_tools.PAGE_SIZE)
            self.assertEqual(len(first["edges"]), scope_tools.PAGE_SIZE)
            self.assertEqual((len(second["nodes"]), len(second["edges"])), (5, 1))
            self.assertIsNone(second["next_offset"])
            self.assertIsNone(second["next_edge_offset"])
            self.assertFalse({node["id"] for node in first["nodes"]} & {node["id"] for node in second["nodes"]})
            targeted = scope_tools.topology_summary(db, self.domain, nodes=("step-2",))
            self.assertEqual([node["id"] for node in targeted["nodes"]], ["step-2"])
            self.assertEqual(targeted["edges"], [("step-1", "step-2"), ("step-2", "step-3")])

    def test_verifier_bindings_use_current_workshop_ids_and_legacy_archive(self):
        run = self.queued("既有构题运行")
        run.update(plan={"domain": "PCB 制造"}, status="completed")
        run["result"]["tasks"] = [{"id": "archive-case", "title": "既有案例"}]
        with closing(self.store.connect()) as db, db:
            self.store.save(db, run)
            db.execute("UPDATE benchmark_tasks SET state='archived' WHERE domain_id=? AND id='task-1'", (self.domain,))
        bindings = guarded_workflow.domain_task_bindings(self.store, self.domain)["items"]
        workshop = guarded_workflow.domain_tasks(self.store, self.domain, summary=True)["items"]
        self.assertEqual(bindings[:-1], [{key: row[key] for key in ("id", "title", "number")} for row in workshop])
        self.assertEqual(bindings[-1], {"id": run["id"] + ":archive-case", "title": "既有案例"})
        self.assertEqual(sandbox_verifier.read(self.store, self.domain, bindings[-1]["id"])["version"], 0)
        with self.assertRaisesRegex(ValueError, "当前领域"):
            sandbox_verifier.read(self.store, self.other, bindings[-1]["id"])

    def test_stage_two_rejects_process_alias_and_retains_group(self):
        stage = scope_guard.STAGES[1]
        turn = self.queued("梳理板厂接单与制造交付", hint={"stage": stage})

        def fixture(_, port, event, stop, **options):
            schemas, call = options["tools"]
            if any(item["name"] == "list_domains" for item in schemas):
                return self.raw(2)
            payload = {"title": "板厂 CAM 入件核查", "flow_group": "板厂接单", "lifecycle_stages": ["S3"]}
            args = {"domain_id": str(self.domain), "process_node_id": "cam-existing", "patch_json": json.dumps(payload)}
            self.assertEqual(call("upsert_process_node", args)["id"], "cam-existing")
            repeated = call("upsert_process_node", {**args, "process_node_id": "cam-duplicate",
                "patch_json": json.dumps({**payload, "title": "板厂CAM入厂核对"})})
            self.assertIn("cam-existing", repeated["error"])
            return "流程已登记"

        self.process(fixture)
        self.assertEqual(self.store.get(turn["id"])["status"], "completed")
        nodes = workbench.overview(self.store, self.domain)["nodes"]
        related = [node for node in nodes if node["id"].startswith("cam-")]
        self.assertEqual([(node["id"], node["flow_group"], node["lifecycle_stages"]) for node in related],
                         [("cam-existing", "板厂接单", ["S3"])])

    def test_task_verifier_studio_revises_only_the_bound_task(self):
        import io
        import zipfile

        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("gerber/top.gbr", b"\x00\xffPCB")
            archive.writestr("assembly/model.step", b"ISO-10303-21;")
        standard = sandbox_verifier.upload_sample(self.store, self.domain, "fixture.zip", package.getvalue())
        incomplete = sandbox_verifier.upload_sample(self.store, self.domain, "fault/empty.gbr", b"")
        check = {"title": "检查", "language": "python", "script": "print('fixture')"}
        spec = {"strategy": "progressive", "image": "fixture:v1",
                "layers": {key: [check] for key in sandbox_verifier.LAYERS},
                "fixtures": {"standard": {"fixture.zip": standard}, "incomplete": {"fault/empty.gbr": incomplete}}}
        with self.assertRaisesRegex(ValueError, "重新挂载"):
            sandbox_verifier.save(self.store, self.other, 1, spec, "task-1")
        saved = sandbox_verifier.save(self.store, self.domain, 1, spec, "task-1")
        self.assertEqual(saved["version"], 2)
        self.assertFalse(guarded_workflow.domain_tasks(self.store, self.domain, item="task-1")["items"][0]["exportable"])
        self.assertEqual(sandbox_verifier.read(self.store, self.domain, "task-2")["version"], 1)
        self.assertEqual(sandbox_verifier.read(self.store, self.domain)["version"], 0)

        def run_check(_docker, _image, folder, rule):
            self.assertEqual(rule, check)
            files = Path(folder)
            passed = (files / "gerber/top.gbr").exists()
            if passed:
                self.assertEqual((files / "gerber/top.gbr").read_bytes(), b"\x00\xffPCB")
                self.assertTrue((files / "assembly/model.step").is_file())
            else:
                self.assertEqual((files / "fault/empty.gbr").read_bytes(), b"")
            return {"title": rule["title"], "passed": passed, "latency_ms": 1}

        with patch.object(sandbox_verifier.shutil, "which", return_value="fixture-docker"), \
                patch.object(sandbox_verifier, "_run_check", side_effect=run_check):
            self.assertTrue(sandbox_verifier.trial(self.store, self.domain, "standard", "task-1", 2)["expected"])
            self.assertTrue(sandbox_verifier.trial(self.store, self.domain, "incomplete", "task-1", 2)["expected"])
            with self.assertRaisesRegex(ValueError, "版本"):
                sandbox_verifier.trial(self.store, self.domain, "incomplete", "task-1", 1)
        case = guarded_workflow.domain_tasks(self.store, self.domain)["items"][0]
        self.assertTrue(case["verifier"]["ready"])
        self.assertTrue(case["exportable"])
        self.assertTrue(case["evaluation"]["is_stale"])

        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../outside.step", b"invalid")
        with self.assertRaisesRegex(ValueError, "相对文件路径"):
            sandbox_verifier.upload_sample(self.store, self.domain, "fixture.zip", package.getvalue())

    def test_local_task_blocks_every_other_write_and_only_marks_dependents(self):
        run = self.queued("请更新第3题的题目约束")
        calls = []
        domain = str(self.domain)
        def fixture(_, port, event, stop, **options):
            self.assertTrue(options["scoped"])
            tools, call = options["tools"]
            names = {tool["name"] for tool in tools}
            calls.append(names)
            if "list_domains" in names:
                self.assertEqual(names, {"list_domains", "get_topology_summary", "list_tasks_brief", "get_stage_progress"})
                self.assertIn("error", call("upsert_benchmark_task", {}))
                self.assertEqual(call("list_tasks_brief", {"domain_id": domain})["tasks"][2]["id"], "task-3")
                return self.raw(4)
            self.assertEqual(names, {"upsert_benchmark_task", "propose_upstream_suggestion"})
            edit = {"domain_id": domain, "task_id": "task-3", "patch_json": '{"definition":{"constraints":"核对厚度"}}'}
            for name, args in [
                ("upsert_benchmark_task", {**edit, "task_id": "task-1"}),
                ("upsert_benchmark_task", {**edit, "domain_id": str(self.other)}),
                ("upsert_benchmark_task", {**edit, "patch_json": '[{"id":"task-1","title":"绕过"}]'}),
            ]:
                self.assertIn("error", call(name, args))
            self.assertEqual(call("upsert_benchmark_task", edit), {"id": "task-3"})
            return "题目约束已更新"
        self.process(fixture)
        saved = self.store.get(run["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(saved["execution_scope"]["allowed_task_ids"], ["task-3"])
        self.assertFalse(saved["result"]["scope_continue"])
        receipt = saved["result"]["scope_receipts"][0]
        self.assertEqual(receipt["rejected"], 3)
        self.assertEqual(len(receipt["changes"]), 1)
        self.assertEqual(len(receipt["stale"]), 2)
        self.assertEqual({(domain, task) for domain, task, stale in self.states("verifier_specs", "task_id") if stale},
                         {(self.domain, "task-3")})
        self.assertEqual({(domain, key) for domain, key, stale in self.states("eval_runs", "id") if stale},
                         {(self.domain, "eval-3")})

    def test_ambiguous_requests_need_confirmation_once_and_survive_restart(self):
        run = self.queued("补全回流焊的开源资料，同时修改题目")
        self.process(lambda *_args, **_opts: self.raw(3, requested_stages=list(scope_guard.STAGES[2:4])))
        saved = self.store.get(run["id"])
        self.assertEqual(saved["status"], "awaiting_scope")
        self.assertLess(saved["execution_scope"]["router_confidence"], .85)
        identity = saved["execution_scope"]["contract_id"]
        confirm = {"contract_id": identity, "domain_id": str(self.domain),
                   "target_stage": scope_guard.STAGES[1], "process_node_id": "solder"}
        with self.assertRaises(scope_guard.ScopeViolationError):
            guarded_workflow.confirm(self.store, None, run["id"], {**confirm, "process_node_id": "other"})
        guarded_workflow.confirm(self.store, None, run["id"], confirm)
        with self.assertRaises(scope_guard.ScopeViolationError):
            guarded_workflow.confirm(self.store, None, run["id"], confirm)
        self.store.claim()  # Restart between claiming and executing.
        self.store = RunStore(self.store.path)
        self.store.recover()
        callbacks = []
        def fixture(_, port, event, stop, **options):
            schemas, call = options["tools"]
            callbacks.append(call)
            self.assertIn("upsert_process_node", {tool["name"] for tool in schemas})
            payload = {"domain_id": str(self.domain), "process_node_id": "solder", "patch_json": '{"title":"回流焊核验"}'}
            self.assertIn("error", call("upsert_process_node", {**payload, "process_node_id": "inspect"}))
            self.assertEqual(call("upsert_process_node", payload)["id"], "solder")
            return "工序已写入"
        self.process(fixture)
        self.assertEqual(self.store.get(run["id"])["status"], "completed")
        self.assertEqual(len(callbacks), 1)
        self.assertIn("error", callbacks[0]("upsert_process_node", {
            "domain_id": str(self.domain), "process_node_id": "solder", "patch_json": '{"title":"结束后写入"}'}))

    def test_missing_prerequisite_prevents_execution(self):
        with closing(self.store.connect()) as db, db:
            empty = domain_libraries.register(db, "未建图谱的领域")
        run = self.queued("在未建图谱的领域构造题目")
        self.process(lambda *_args, **_opts: self.raw(4, domain_id=str(empty)))
        saved = self.store.get(run["id"])
        self.assertEqual(saved["status"], "awaiting_scope")
        self.assertEqual(saved["execution_scope"]["suggested_stage"], scope_guard.STAGES[1])
        self.assertNotIn("scope_receipts", saved["result"])

    def test_verifier_revision_requires_same_task_and_keeps_definition(self):
        run = self.queued("给第3题的验证器增加规则")
        def fixture(_, port, event, stop, **options):
            schemas, call = options["tools"]
            if {tool["name"] for tool in schemas} == {tool["name"] for tool in guarded_workflow.ROUTER_TOOLS}:
                return self.raw(5)
            payload = {"domain_id": str(self.domain), "task_id": "task-3", "version": 1,
                       "patch_json": '{"strategy":"acceptance_backwards"}'}
            self.assertIn("error", call("upsert_verifier_spec", {**payload, "task_id": "task-2"}))
            self.assertIn("error", call("upsert_verifier_spec", {**payload, "patch_json": '{"description":"改题干"}'}))
            self.assertEqual(call("upsert_verifier_spec", payload), {"version": 2})
            for variant in ("standard", "incomplete"):
                self.assertTrue(call("run_sandbox_verify", {"domain_id": str(self.domain), "task_id": "task-3",
                                                            "variant": variant})["expected"])
            return "验证器修订已保存"
        with patch.object(scope_tools.sandbox_verifier, "run_fixture", return_value={"expected": True}):
            self.process(fixture)
        self.assertEqual(self.store.get(run["id"])["status"], "completed")
        with closing(self.store.connect()) as db:
            self.assertTrue(scope_tools.evaluation_ready(scope_tools.verifier_row(db, self.domain, "task-3")))
            self.assertEqual(scope_tools.task_snapshot(db, self.domain, "task-3")["description"], "确认样本状态")

    def test_explicit_full_flow_stops_at_missing_coverage(self):
        run = self.queued("请连续执行全流程")
        stage = iter((1, 2, 3))
        manifests = []

        def fixture(_, port, event, stop, **options):
            schemas, call = options["tools"]
            names = {tool["name"] for tool in schemas}
            if "list_domains" in names:
                return self.raw(next(stage))
            manifests.append(names)
            if "search_domain_libraries" in names:
                self.classify(call)
            if "upsert_process_node" in names:
                call("upsert_process_node", {"domain_id": str(self.domain), "process_node_id": "solder",
                                             "patch_json": '{"title":"回流焊复核"}'})
            return "阶段结果"

        self.process(fixture)
        current = self.store.get(run["id"])
        self.assertEqual(current["status"], "needs_review")
        self.assertEqual(current["execution_scope"]["execution_pace"], scope_guard.AUTO_CONTINUE_QUEUE)
        self.assertEqual([row["stage"] for row in current["result"]["scope_receipts"]], list(scope_guard.STAGES[:3]))
        self.assertIn("覆盖缺口", current["error"])
        self.assertEqual(len(manifests), 3)

    def test_selected_queue_pauses_and_resumes_in_place_with_a_gap(self):
        scope = {"stage": scope_guard.STAGES[1], "process_node_id": "solder"}
        queue = [scope_guard.STAGES[1], scope_guard.STAGES[3]]
        prompt = "整理回流焊工艺，然后构建出3道题。"
        turn = self.queued(prompt, hint=scope, stage_queue=queue)
        domain = str(self.domain)
        node = {"domain_id": domain, "process_node_id": "solder"}
        old_tool = None
        authored = 0

        def fixture(input_prompt, _port, _event, _stop, **options):
            nonlocal old_tool, authored
            schemas, call = options["tools"]
            names = {tool["name"] for tool in schemas}
            self.assertIn(prompt, input_prompt)
            if "list_domains" in names:
                return self.raw(3, stage_queue=[scope_guard.STAGES[2], scope_guard.STAGES[4]])
            if "upsert_process_node" in names:
                self.assertNotIn("upsert_benchmark_task", names)
                self.assertEqual(call("upsert_process_node", {**node, "patch_json": '{"title":"回流焊核验"}'})["id"], "solder")
                old_tool = call
                return "已更新工序"
            self.assertNotIn("enqueue_frontier", names)
            self.assertIn("error", old_tool("upsert_process_node", {**node, "patch_json": '{"title":"越界修改"}'}))
            with closing(self.store.connect()) as db:
                profile = json.loads(db.execute("SELECT quality_json FROM verified_references WHERE domain_id=? AND id='ref'",
                                                (self.domain,)).fetchone()[0])
            definition = task_definition("ref", profile["extracted_clauses"][-1]["id"])
            for _ in range(1 if authored == 0 else 2):
                authored += 1
                record = {"title": f"回流焊文件收检任务 {authored}", "state": "ready", "definition": definition}
                payload = {**node, "task_id": f"new-{authored}", "patch_json": json.dumps(record)}
                self.assertEqual(call("upsert_benchmark_task", payload).get("id"), payload["task_id"])
            if authored == 3:
                self.assertIn("error", call("upsert_benchmark_task", {**payload, "task_id": "new-4"}))
            return "构题结果已登记"

        self.process(fixture)
        paused = self.store.get(turn["id"])
        self.assertEqual(paused["status"], "awaiting_stage")
        contract = paused["execution_scope"]
        self.assertEqual((contract["target_stage"], contract["next_stage"], contract["stage_queue"]), (*queue, queue))
        self.store = RunStore(self.store.path)
        self.store.recover()
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.control(turn["id"], "resume")
        with self.assertRaises(ValueError):
            self.store.create(codex_agent.ROUTE, "offline", "跳过", conversation_id=turn["conversation_id"])
        approve = {"action": "continue_stage", "contract_id": contract["contract_id"]}
        guarded_workflow.confirm(self.store, None, turn["id"], approve)
        with self.assertRaises(scope_guard.ScopeViolationError):
            guarded_workflow.confirm(self.store, None, turn["id"], approve)
        self.store.claim()
        self.store.recover()
        self.process(fixture)
        partial = self.store.get(turn["id"])
        self.assertEqual(partial["status"], "needs_review")
        self.assertIn("1/3", partial["error"])
        self.store.control(turn["id"], "resume")
        self.process(fixture)
        saved = self.store.get(turn["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["execution_scope"]["allowed_process_node_ids"], ["solder"])
        self.assertIsNone(saved["execution_scope"]["next_stage"])
        self.assertEqual([report["stage"] for report in saved["result"]["scope_receipts"]], queue)
        self.assertEqual(len(self.store.conversation(turn["conversation_id"])), 1)
        self.assertEqual(self.store.conversations()[0]["initial_stage"], queue[0])

    def test_failure_and_retry_preserve_phase_order_and_tool_ownership(self):
        queue = list(scope_guard.STAGES[:3])
        run = self.queued("梳理 PCB 工艺并整理规范", stage_queue=queue, execution_pace=scope_guard.AUTO_CONTINUE_QUEUE)
        first_calls = []

        def simulate(prompt, _, observe, stop, **options):
            schemas, call = options["tools"]
            names = {tool["name"] for tool in schemas}
            if "list_domains" in names:
                call("list_domains", {})
                first_calls.append("routing")
                return self.raw(1, stage_queue=queue)
            if "create_domain_library" in names:
                self.classify(call)
                first_calls.append("stage_1")
                return "领域定位成果"
            self.assertIn("upsert_process_node", names)
            self.assertNotIn("enqueue_frontier", names)
            first_calls.append("stage_2")
            observe("harness.thread", {"thread": "stage-2-root"}, None)
            observe("harness.turn", {"thread": "stage-2-root", "turn": "topology-turn"}, None)
            observe("subagent", {"id": "child-1", "parent": "stage-2-root", "status": "running"}, None)
            summary = {"kind": "reasoning", "label": "推理摘要", "thread": "child-1", "turn": "child-turn",
                       "subagent_id": "child-1", "item": "reasoning-1"}
            for text in (" ", "核对", "核对原文"):
                observe("harness.item", {**summary, "preview": text}, None)
            observe("harness.item", {"kind": "tool", "label": "read_topology_evidence", "thread": "child-1",
                                    "turn": "child-turn", "subagent_id": "child-1", "item": "tool-1",
                                    "preview": "原文访问受阻", "status": "failed", "duration_ms": 9}, None)
            raise codex_agent.RunTimedOut("阶段超时", code="network_idle_timeout")

        self.process(simulate)
        saved = self.store.get(run["id"])
        timeline = chat_turn(saved)["result"]["stage_timeline"]
        self.assertEqual([stage["stage_key"] for stage in timeline], ["routing", "stage_1", "routing", "stage_2"])
        self.assertEqual([stage["status"] for stage in timeline], ["completed"] * 3 + ["failed"])
        self.assertEqual(timeline[1]["summary"], "领域定位成果")
        failure = timeline[-1]
        self.assertEqual((failure["error"]["kind"], failure["error"]["code"]), ("stage_error", "network_idle_timeout"))
        summaries = [event for event in failure["events"] if event["kind"] == "reasoning"]
        self.assertEqual([(event["preview"], event["agent_kind"], event["parent_turn_id"]) for event in summaries],
                         [("核对原文", "subagent", "topology-turn")])
        self.assertTrue(all(event["stage_key"] == stage["stage_key"] for stage in timeline for event in stage["events"]))
        self.assertEqual(first_calls, ["routing", "stage_1", "routing", "stage_2"])
        with self.assertRaisesRegex(ValueError, "阶段状态已变化"):
            self.store.control(run["id"], "resume", stage_attempt=timeline[1]["id"])
        resumed, _, _ = self.store.control(run["id"], "resume", stage_attempt=failure["id"])
        self.assertEqual(resumed["execution_scope"]["contract_id"], saved["execution_scope"]["contract_id"])
        self.assertEqual(resumed["status"], "queued")

        def retry(_, _port, _observe, _stop, **options):
            self.assertIn("upsert_process_node", {tool["name"] for tool in options["tools"][0]})
            return "阶段 2 重试成果"

        self.process(retry)
        updated = chat_turn(self.store.get(run["id"]))
        last = updated["result"]["stage_timeline"]
        self.assertEqual(updated["status"], "awaiting_stage")
        self.assertEqual([stage["status"] for stage in last], ["completed"] * 3 + ["failed", "completed"])
        self.assertEqual((last[-1]["stage_key"], last[-1]["summary"]), ("stage_2", "阶段 2 重试成果"))
        self.assertEqual(last[-2]["id"], failure["id"])

    def test_intelligent_queue_can_finish_a_new_domain_without_backtracking(self):
        queue = list(scope_guard.STAGES[:4])
        prompt = "调研新的裸板 CAM 工艺，找规范，再构建出3道题。"
        turn = self.queued(prompt, domain=False, execution_pace=scope_guard.AUTO_CONTINUE_QUEUE, collection_stop="task")
        url = "https://example.com/cam-intake"
        domain = None
        phases = []
        label = {**PCB_CLASSIFICATION, "object_cluster": "pcb-fabrication-cam"}

        def fixture(input_prompt, _port, _event, _stop, **options):
            nonlocal domain
            names, call = {tool["name"] for tool in options["tools"][0]}, options["tools"][1]
            if "list_domains" in names:
                return self.raw(len(phases) + 1, stage_queue=queue, domain_id=str(self.other) if domain else None)
            phases.append(queue[len(phases)])
            self.assertIn(prompt, input_prompt)
            if "create_domain_library" in names:
                name = "裸板 CAM 交付"
                call("search_domain_libraries", {"name": name})
                self.classify(call, label)
                domain = call("create_domain_library", {"profile_json": json.dumps({"name": name,
                    "description": "板厂接收制造文件并复核", "keywords": ["Gerber"], "upstream": "设计导出",
                    "downstream": "板厂接收"})})["id"]
            elif "upsert_process_node" in names:
                self.assertNotIn("promote_verified_ref", names)
                self.assertEqual(json.loads(input_prompt.rsplit("\n", 1)[1])["task_taxonomy"], label)
                node = {"domain_id": domain, "process_node_id": "cam"}
                self.assertEqual(call("upsert_process_node", {**node, "patch_json": '{"title":"CAM 文件收件"}'})["id"], "cam")
                call("read_topology_evidence", {**node, "url": url})
                proof = {"sender": "设计方", "recipient": "板厂制造部门", "deliverable": "Excellon 钻孔文件",
                         "kind": "supplier_requirement", "source_url": url,
                         "locator": PASSAGES[2]["locator"], "quote": PASSAGES[2]["text"]}
                result = call("upsert_process_relation", {**node, "relation_id": "handoff", "relationship_json": json.dumps(proof)})
                self.assertTrue(result["changed"], result)
                with closing(self.store.connect()) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM raw_sources WHERE domain_id=?", (domain,)).fetchone()[0], 0)
            elif "promote_verified_ref" in names:
                self.assertIn(PASSAGES[2]["text"], input_prompt)
                self.assertNotIn("upsert_process_relation", names)
                self.assertNotIn("classify_industry_task", names)
                node = {"domain_id": domain, "process_node_id": "cam"}
                result = call("save_raw_source", {**node, "url": url})
                admission = call("promote_verified_ref", {**node, "source_id": result["id"],
                    "document_json": json.dumps(admission_document())})
                self.assertEqual(len(admission["clauses"]), 3)
            elif "upsert_benchmark_task" in names:
                self.assertNotIn("upsert_verifier_spec", names)
                with closing(self.store.connect()) as db:
                    pending, = db.execute("SELECT state FROM process_relations WHERE domain_id=?", (domain,)).fetchone()
                    self.assertEqual(pending, "pending")
                    relation = db.execute("SELECT evidence_id,evidence_clause_id FROM grounded_process_relations WHERE domain_id=?",
                                          (domain,)).fetchone()
                    self.assertIsNotNone(relation)
                for idx in range(1, 4):
                    task_id = f"cam-{idx}"
                    patch_json = json.dumps({"title": f"板厂样件预检 {idx}", "state": "ready", "definition": task_definition(*relation)})
                    self.assertEqual(call("upsert_benchmark_task", {"domain_id": domain, "process_node_id": "cam",
                        "task_id": task_id, "patch_json": patch_json}).get("id"), task_id)
            return "阶段已处理"

        with patch.object(scope_tools.public_web, "fetch", return_value=(url, {}, b"spec")), patch.object(
                scope_tools.public_web, "extract", return_value={"sha256": "b" * 64, "passages": PASSAGES, "bytes": 4}):
            self.process(fixture)
        result = self.store.get(turn["id"])
        self.assertEqual((result["status"], phases, result["scope_domain_id"]), ("completed", queue, domain))
        self.assertEqual(result["task_taxonomy"], label)
        self.assertEqual(result["execution_scope"]["stage_queue"], queue)
        self.assertFalse(result["result"]["scope_continue"])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM benchmark_tasks WHERE domain_id=? AND quality_approved=1", (domain,)).fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM verifier_specs WHERE domain_id=?", (domain,)).fetchone()[0], 0)

    def test_auto_queue_still_waits_for_model_evaluation_consent(self):
        queue = list(scope_guard.STAGES[4:])
        turn = self.queued("核对第3题的验证器，再用模型盲测", stage_queue=queue, execution_pace=scope_guard.AUTO_CONTINUE_QUEUE)
        def fixture(_, _port, _event, _stop, **options):
            names = {tool["name"] for tool in options["tools"][0]}
            if "list_domains" in names:
                return self.raw(5, stage_queue=queue)
            self.assertIn("run_sandbox_verify", names)
            self.assertNotIn("run_tiered_eval", names)
            return "目标题已有正反验收"
        self.process(fixture)
        waiting = self.store.get(turn["id"])
        self.assertEqual(waiting["status"], "awaiting_scope")
        self.assertEqual(waiting["execution_scope"]["target_stage"], queue[-1])
        with self.assertRaises(scope_guard.ScopeViolationError):
            guarded_workflow.confirm(self.store, None, turn["id"], {"contract_id": waiting["execution_scope"]["contract_id"],
                "target_stage": queue[-1], "domain_id": str(self.domain), "task_id": "task-3"})

    def test_export_snapshots_only_the_selected_task_and_locks_schema(self):
        run = self.queued("仅导出第3题的 JSONL")
        def fixture(_, port, event, stop, **options):
            schemas, call = options["tools"]
            if any(tool["name"] == "list_domains" for tool in schemas):
                return self.raw(6)
            payload = {"domain_id": str(self.domain), "task_id": "task-3"}
            self.assertIn("error", call("run_tiered_eval", {**payload, "pass_at_k": 1}))
            self.assertIn("error", call("export_dataset", {**payload, "task_id": "task-1", "format": "jsonl"}))
            self.assertIn("error", call("save_export_schema", {"domain_id": str(self.domain), "patch_json": '{}'}))
            self.assertEqual(call("export_dataset", {**payload, "format": "jsonl"})["count"], 1)
            return "仅导出选定题目"
        self.process(fixture)
        self.assertEqual(self.store.get(run["id"])["status"], "completed")
        with closing(self.store.connect()) as db:
            body, = db.execute("SELECT body FROM workflow_export_snapshots").fetchone()
            self.assertIn("实验样题 3", body)
            self.assertNotIn("实验样题 1", body)

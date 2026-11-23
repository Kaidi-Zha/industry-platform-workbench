"""Durable run queue and results for a local platform instance."""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import collection, industrial_bench, local_import, reference_catalog, research, scope_guard, shared_metadata
from . import stage_timeline, workbench
from .codex_agent import ROUTE as AGENT_ROUTE
from .codex_local import API_GATEWAY, SUBSCRIPTION

API_AGENT_ROUTE = "api-agent"
AGENT_ROUTES = (AGENT_ROUTE, API_AGENT_ROUTE)
STOPPING_RUNS = {"pausing": "paused", "cancelling": "cancelled"}
ACTIVE_RUNS = ("queued", "running", *STOPPING_RUNS)


class RunInterrupted(Exception):
    """A worker reached a checkpoint after a stop request."""


STEPS = (
    ("discover", "数据采集"),
    ("qualify", "资产核验"),
    ("author", "任务构建"),
    ("verify", "独立评估"),
    ("publish", "Benchmark"),
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    sequence INTEGER NOT NULL UNIQUE,
                    body TEXT NOT NULL,
                    route TEXT NOT NULL DEFAULT ''
                )
            """)
            if "route" not in {column[1] for column in connection.execute("PRAGMA table_info(runs)")}:
                connection.execute("ALTER TABLE runs ADD COLUMN route TEXT NOT NULL DEFAULT ''")
            if "conversation_id" not in {column[1] for column in connection.execute("PRAGMA table_info(runs)")}:
                connection.execute("ALTER TABLE runs ADD COLUMN conversation_id TEXT")
                connection.execute("UPDATE runs SET conversation_id = id WHERE route IN (?, ?)", AGENT_ROUTES)
            connection.execute("CREATE INDEX IF NOT EXISTS conversation_turns ON runs(conversation_id, sequence)")
            connection.execute("""CREATE INDEX IF NOT EXISTS run_parent
                ON runs(json_extract(body, '$.parent_run_id'))""")
            collection.initialize(connection)
            research.initialize(connection)
            industrial_bench.initialize(connection)
            workbench.initialize(connection)
            scope_guard.initialize(connection)
            shared_metadata.initialize(connection)
            local_import.initialize(connection)
            reference_catalog.migrate(connection)

    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function("workflow_contract_id", 0, lambda: None)
        return connection

    @staticmethod
    def save(connection, run):
        run["updated_at"] = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE runs SET status = ?, body = ? WHERE id = ?",
            (run["status"], json.dumps(run, ensure_ascii=False), run["id"]),
        )

    def create(self, route, cohort, prompt, seed=None, *, model_profile=None, configured_panel=False,
               conversation_id=None, agent_options=None, plan=None, approval_profile=None, parent_run_id=None,
               public_research=None, benchmark=None, auto_benchmark=None, plan_version=None, plan_reason="", delegation=None,
               fork_native_thread=None, frontier_target=None, attachments=None, graph_mode=False, resolve_domain=False, domain_hint="",
               scope_request=None, worker_profile=None, execution_mode=None, subscription_model=None):
        agent_label = "Codex · API 模型" if route == API_AGENT_ROUTE else "Codex Agent"
        if execution_mode:
            agent_label = "API 模型网关" if execution_mode == API_GATEWAY else "Codex 本地订阅"
        steps = (("agent", agent_label), ("review", "交付结果")) if route in AGENT_ROUTES else STEPS
        run = {
            "id": uuid.uuid4().hex,
            "status": "queued",
            "route": route,
            "cohort": cohort,
            "prompt": prompt,
            "seed": seed,
            "created_at": utc_now(),
            "steps": [
                {"id": step_id, "label": label, "status": "pending", "summary": ""}
                for step_id, label in steps
            ],
            "result": {"assets": [], "rules": [], "tasks": [], "evaluations": []},
        }
        if scope_request is not None:
            run["scoped_workflow"] = True
            run["scope_request"] = scope_request
            run["steps"].insert(0, {"id": "scope", "label": "识别执行范围", "status": "pending", "summary": ""})
        if model_profile is not None:
            run["model_profile"] = model_profile
        if execution_mode:
            run["execution_mode"] = execution_mode
        if execution_mode == SUBSCRIPTION:
            run["subscription_model"] = subscription_model
        if approval_profile is not None:
            run["approval_profile"] = approval_profile
        if worker_profile is not None:
            run["worker_profile"] = worker_profile
        if route in AGENT_ROUTES and agent_options is not None:
            run["agent_options"] = agent_options
        if delegation is not None:
            run["delegation"] = delegation
        if fork_native_thread:
            run["fork_native_thread"] = fork_native_thread
        if frontier_target:
            run["frontier_target"] = frontier_target
        if attachments:
            run["attachments"] = attachments
        if resolve_domain:
            run["resolve_domain"] = True
            if domain_hint:
                run["domain_hint"] = domain_hint
        if graph_mode:
            run["graph_mode"] = True
        if configured_panel:
            run["configured_panel"] = True
        if benchmark is not None:
            run["benchmark"] = benchmark
        if route in AGENT_ROUTES:
            run["conversation_id"] = conversation_id or run["id"]
        elif conversation_id is not None:
            raise ValueError("请选择 Agent 对话")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if scope_request is not None:
                repository = shared_metadata.configured_repository(connection)
                saved_base = connection.execute("""SELECT last_synced_commit FROM shared_metadata_sync
                    WHERE source_ref=? AND coalesce(repository,'')=?""",
                                                (shared_metadata.PUBLISHED_REF, repository)).fetchone()
                if repository:
                    run["shared_metadata_repository"] = repository
                if saved_base:
                    run["shared_metadata_base"] = saved_base[0]
            if conversation_id is not None:
                last = connection.execute("""
                    SELECT status, body FROM runs WHERE conversation_id = ? ORDER BY sequence DESC LIMIT 1
                """, (conversation_id,)).fetchone()
                if last is None:
                    raise ValueError("未找到这条对话")
                if last[0] in (*ACTIVE_RUNS, "paused", "awaiting_scope", "awaiting_stage"):
                    raise ValueError("请先结束本轮 Agent，或在暂停点继续")
                previous = json.loads(last[1])
                if scope_request is not None and not scope_request.get("domain_id") and previous.get("scope_domain_id"):
                    scope_request["domain_id"] = previous["scope_domain_id"]
                history = collection.plan_history(connection, conversation_id)
                latest = history[-1] if history else None
                if plan_version is not None and (not latest or plan_version != latest["version"]):
                    raise ValueError("已有新版采集计划，请刷新后重新提交")
                if plan is None and latest:
                    plan = latest["plan"]
                mode = latest["mode"] if latest else collection.mode_for_run(previous)
                if public_research is None:
                    public_research = mode != "chat"
                if auto_benchmark is None:
                    auto_benchmark = mode == "benchmark"
            if plan is not None:
                if route not in AGENT_ROUTES:
                    raise ValueError("采集计划须由 Agent 执行")
                mode = "benchmark" if auto_benchmark else "research" if public_research else "chat"
                revision = collection.confirm_revision(connection, run["conversation_id"], plan, mode,
                                                       plan_reason, at=run["created_at"], expected=plan_version,
                                                       register_domain=scope_request is None)
                run.update(plan=plan, plan_version=revision["version"], plan_reason=revision["reason"])
            if auto_benchmark and scope_request is None:
                if route not in AGENT_ROUTES or cohort != "offline" or plan is None:
                    raise ValueError("自动构题须先确认采集计划，并只执行离线验收")
                run["auto_benchmark"] = True
                public_research = True
                run["steps"] = [{"id": key, "label": label, "status": "pending", "summary": ""}
                                for key, label in (
                                    ("agent", "搜索与证据入库"), ("sample", "实样登记"), *STEPS,
                                )]
            if public_research and scope_request is None:
                if route not in AGENT_ROUTES or plan is None:
                    raise ValueError("持续补查需要先确认采集计划")
                run["research"] = True
            if parent_run_id:
                parent = connection.execute("SELECT body FROM runs WHERE id = ?", (parent_run_id,)).fetchone()
                if route in AGENT_ROUTES or cohort != "offline" or parent is None:
                    raise ValueError("仅采集计划可关联离线子工作流")
                owner = json.loads(parent[0])
                if not owner.get("plan") or owner["status"] != "running":
                    raise ValueError("对应的采集计划未在运行")
                for child in self._children(connection, parent_run_id):
                    if (child["route"], child["prompt"], child["seed"], child.get("benchmark")) == (
                        route, prompt, seed, benchmark,
                    ):
                        return child
                run["parent_run_id"] = parent_run_id
            connection.execute("""
                INSERT INTO runs(id, status, sequence, body, route, conversation_id)
                VALUES (?, ?, (SELECT COALESCE(MAX(sequence), 0) + 1 FROM runs), ?, ?, ?)
            """, (run["id"], run["status"], json.dumps(run, ensure_ascii=False), route, run.get("conversation_id")))
        return run

    def get(self, run_id):
        with closing(self.connect()) as connection:
            item = connection.execute("SELECT body FROM runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(item[0]) if item else None

    def has_active_runs(self):
        with closing(self.connect()) as connection:
            states = ", ".join("?" for _ in ACTIVE_RUNS)
            return connection.execute(f"""SELECT 1 FROM runs WHERE status IN ({states})
                UNION ALL SELECT 1 FROM local_import_batches WHERE status IN ('queued','running') LIMIT 1""",
                                      ACTIVE_RUNS).fetchone() is not None

    def recent(self):
        with closing(self.connect()) as connection:
            items = connection.execute(
                "SELECT body FROM runs ORDER BY sequence DESC LIMIT 25"
            ).fetchall()
        return [json.loads(item[0]) for item in items]

    def conversations(self, domain=None):
        with closing(self.connect()) as connection:
            rows = connection.execute("""
                WITH grouped AS (
                    SELECT conversation_id, MAX(sequence) AS last_sequence
                    FROM runs WHERE conversation_id IS NOT NULL GROUP BY conversation_id
                ), scoped AS (
                    SELECT grouped.conversation_id, COALESCE(json_extract(last.body, '$.plan.scenario'),
                           json_extract(first.body, '$.prompt')) AS title, last.status,
                           COALESCE(json_extract(last.body,'$.updated_at'),json_extract(last.body,'$.created_at')) AS updated,
                           coalesce(json_extract(contract.body,'$.target_stage'),
                                    json_extract(last.body,'$.execution_scope.target_stage')) AS stage,
                           coalesce(json_extract(initial.body,'$.stage_queue[0]'), json_extract(initial.body,'$.target_stage'),
                                    json_extract(first.body,'$.execution_scope.target_stage')) AS initial_stage,
                           CASE WHEN last.status IN ('paused','needs_review','failed') THEN last.body END AS resumable_body,
                           COALESCE(json_extract(last.body, '$.scope_domain_name'), (SELECT json_extract(body, '$.plan.domain')
                             FROM collection_plan_revisions WHERE conversation_id = grouped.conversation_id
                             ORDER BY version DESC LIMIT 1), json_extract(last.body, '$.plan.domain'),
                             CASE WHEN json_extract(last.body,'$.scoped_workflow') THEN '' END) AS domain,
                           last.sequence
                    FROM grouped JOIN runs AS first ON first.id = grouped.conversation_id
                    JOIN runs AS last ON last.sequence = grouped.last_sequence
                    LEFT JOIN workflow_scope_contracts contract ON contract.run_id=last.id AND
                        contract.id=json_extract(last.body,'$.execution_scope.contract_id')
                    LEFT JOIN workflow_scope_contracts initial ON initial.run_id=first.id AND
                        initial.id=json_extract(first.body,'$.execution_scope.contract_id')
                )
                SELECT conversation_id, title, status, updated, domain, stage, initial_stage, resumable_body FROM scoped
                WHERE (? IS NULL OR domain = ? COLLATE NOCASE)
                ORDER BY updated DESC, sequence DESC
            """, (domain, domain)).fetchall()
            imports = [row for row in local_import.task_rows(connection) if not domain or
                       domain.casefold() in {name.casefold() for name in row["library_domains"]}]
        tasks = [dict(id=run_id, title=title, status=status, updated_at=updated, domain=domain,
                     stage=stage if stage in scope_guard.STAGES else None,
                     initial_stage=initial if initial in scope_guard.STAGES else None,
                     can_resume=bool(body and self.can_resume(json.loads(body))))
                for run_id, title, status, updated, domain, stage, initial, body in rows]
        return sorted(tasks + imports, key=lambda row: row["updated_at"] or "", reverse=True) if imports else tasks

    def latest_turn(self, conversation_id):
        with closing(self.connect()) as connection:
            row = connection.execute("""
                SELECT body FROM runs WHERE conversation_id = ? ORDER BY sequence DESC LIMIT 1
            """, (conversation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def conversation(self, conversation_id):
        with closing(self.connect()) as connection:
            rows = connection.execute("""
                SELECT body FROM runs WHERE conversation_id = ? ORDER BY sequence
            """, (conversation_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def plan_history(self, conversation_id):
        with closing(self.connect()) as connection:
            return collection.plan_history(connection, conversation_id)

    def confirm_plan(self, conversation_id, plan, mode, reason, version):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""SELECT body FROM runs WHERE conversation_id=?
                                        ORDER BY sequence DESC LIMIT 1""", (conversation_id,)).fetchone()
            if not row:
                raise ValueError("未找到这条对话")
            return collection.confirm_revision(connection, conversation_id, plan, mode, reason,
                                               at=utc_now(), expected=version,
                                               register_domain=not json.loads(row[0]).get("scoped_workflow"))

    def previous_turns(self, run, limit=5):
        with closing(self.connect()) as connection:
            rows = connection.execute("""
                SELECT body FROM runs WHERE conversation_id = ? AND sequence <
                  (SELECT sequence FROM runs WHERE id = ?)
                ORDER BY sequence DESC LIMIT ?
            """, (run.get("conversation_id"), run["id"], limit)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    @staticmethod
    def _children(connection, parent_run_id):
        rows = connection.execute("""
            SELECT body FROM runs WHERE json_extract(body, '$.parent_run_id') = ? ORDER BY sequence
        """, (parent_run_id,))
        return [json.loads(row[0]) for row in rows]

    def children(self, parent_run_id):
        with closing(self.connect()) as connection:
            return self._children(connection, parent_run_id)

    def claim(self, agent_only=None):
        condition = "status = 'queued'"
        parameters = ()
        if agent_only is not None:
            condition += " AND route IN (?, ?)" if agent_only else " AND route NOT IN (?, ?)"
            parameters = AGENT_ROUTES
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                f"SELECT body FROM runs WHERE {condition} ORDER BY sequence LIMIT 1", parameters
            ).fetchone()
            if not item:
                return None
            run = json.loads(item[0])
            run["status"] = "running"
            self.save(connection, run)
            return run

    @staticmethod
    def ensure_worker_can_write(connection, run_id):
        (status,) = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if status in (*STOPPING_RUNS, "paused", "cancelled"):
            raise RunInterrupted()

    def update(self, run):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self.ensure_worker_can_write(connection, run["id"])
            self.save(connection, run)

    @staticmethod
    def _settle(run, status):
        if run.get("scoped_workflow") and status in ("paused", "cancelled"):
            stage_timeline.materialize(run)
            stage_timeline.interrupt(run, status, "本轮已暂停" if status == "paused" else "本轮已中断")
        run["status"] = status
        run.pop("error", None)
        if status == "queued":
            run["restarts"] = run.get("restarts", 0) + 1
        for step in run["steps"]:
            if status == "queued" and step["status"] == "paused":
                step.update(status="pending", summary="")
            elif status in ("paused", "cancelled") and step["status"] in ("running", "paused"):
                step.update(status=status, summary="")

    @staticmethod
    def can_resume(run):
        if run.get("route") not in AGENT_ROUTES or not (run.get("plan") or run.get("scoped_workflow")):
            return False
        if run["status"] == "paused":
            return True
        scope = run.get("execution_scope") or {}
        result = run.get("result") or {}
        timeline = stage_timeline.public(run)
        if (run["status"] in ("needs_review", "failed") and timeline and timeline[-1]["status"] == "failed"
                and timeline[-1]["error"].get("retryable")):
            return True
        return run["status"] == "needs_review" and (
            bool(result.get("frontier") and not result["frontier"].get("exhausted")) or
            not scope and run.get("scoped_workflow") and any(
                step["id"] == "scope" and step["status"] == "needs_review" for step in run.get("steps", ())) or
            scope.get("state") == "blocked" and result.get("scope_retryable") is True or
            scope.get("target_stage") == scope_guard.STAGES[2] and scope.get("state") == "blocked"
            and (result.get("coverage_pending") or run.get("error") == "采集仍有覆盖缺口，已保留本轮证据和待查队列。")
        )

    def control(self, identity, action, *, stage_attempt=None, model_selection=None, later_stages=None):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT body FROM runs WHERE id = ?", (identity,)).fetchone()
            if not row:
                raise ValueError("未找到这次运行")
            run = json.loads(row[0])
            if run["route"] not in AGENT_ROUTES or not (run.get("plan") or run.get("scoped_workflow")):
                raise ValueError("仅已确认计划的 Agent 任务支持运行控制")
            resumable = action == "resume" and self.can_resume(run)
            allowed = {"pause": ("queued", "running"),
                       "resume": ("paused", "needs_review", "failed") if resumable else (),
                       "cancel": ("queued", "running", "paused", "awaiting_scope", "awaiting_stage")}
            if action not in allowed or run["status"] not in allowed[action]:
                raise ValueError("当前任务状态不支持此操作，请刷新后重试")
            if run.get("scoped_workflow"):
                stage_timeline.materialize(run)
            attempt = stage_timeline.latest(run)
            if stage_attempt is not None:
                if (not attempt or stage_attempt != attempt["id"] or attempt["status"] != "failed"
                        or not attempt.get("error", {}).get("retryable") or action != "resume"):
                    raise ValueError("阶段状态已变化，请刷新后重试")
                run["retry_stage_only"] = True
                if model_selection:
                    if run.get("delegation"):
                        raise ValueError("本轮使用原生委派，请使用原连接重试阶段")
                    for key in ("model_profile", "subscription_model", "approval_profile", "worker_profile"):
                        run.pop(key, None)
                    run.update(model_selection)
            if later_stages is not None:
                error = (attempt or {}).get("error") or {}
                if (stage_attempt is None or error.get("code") != scope_guard.GUARD_ABORT_CODE
                        or not error.get("suggested_stages")):
                    raise ValueError("仅越阶段操作触发熔断时可以在这里确认后续环节")
                scope_guard.extend_stage_queue(connection, run, later_stages)
            stopping = []
            discarded = []
            for item in [run, *self._children(connection, identity)]:
                if item["status"] not in allowed[action]:
                    continue
                if item is not run and action == "resume" and item["status"] != "paused":
                    continue
                if action == "resume" and item is run and run["status"] in ("needs_review", "failed"):
                    latest = connection.execute("SELECT id FROM runs WHERE conversation_id=? ORDER BY sequence DESC LIMIT 1",
                                                (run["conversation_id"],)).fetchone()
                    if latest and latest[0] != identity:
                        raise ValueError("该会话已有更新的执行轮，请打开最新一轮继续")
                    scope = run.get("execution_scope")
                    if scope and not (attempt and attempt["stage_key"] == "routing"):
                        cursor = connection.execute("""UPDATE workflow_scope_contracts SET state='locked'
                            WHERE id=? AND run_id=? AND state='blocked'""", (scope["contract_id"], identity))
                        if cursor.rowcount != 1:
                            raise ValueError("采集范围已变化，请重新确认")
                        scope["state"] = "locked"
                        run["result"].pop("coverage_pending", None)
                status = {"pause": "paused", "resume": "queued", "cancel": "cancelled"}[action]
                if item["status"] == "running":
                    status = "pausing" if action == "pause" else "cancelling"
                    stopping.append(item["id"])
                self._settle(item, status)
                if action == "cancel":
                    discarded.append(item["id"])
                self.save(connection, item)
            if stopping and identity not in stopping:
                run["control_ack"] = True
                run["status"] = "pausing" if action == "pause" else "cancelling"
                self.save(connection, run)
            return run, stopping, discarded

    def _settle_stopping(self, connection, run_id, *, acknowledge=False, recovering=False):
        row = connection.execute("SELECT body FROM runs WHERE id = ?", (run_id,)).fetchone()
        run = json.loads(row[0])
        status = STOPPING_RUNS.get(run["status"])
        if not status:
            return
        if acknowledge:
            run["control_ack"] = True
        if recovering or (run.get("control_ack") and not any(
            child["status"] in STOPPING_RUNS for child in self._children(connection, run_id)
        )):
            run.pop("control_ack", None)
            self._settle(run, status)
        self.save(connection, run)

    def settle_control(self, run):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._settle_stopping(connection, run["id"], acknowledge=True)
            if run.get("parent_run_id"):
                self._settle_stopping(connection, run["parent_run_id"])

    def recover(self):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for (identity,) in connection.execute("SELECT id FROM runs WHERE status IN ('pausing','cancelling')").fetchall():
                self._settle_stopping(connection, identity, recovering=True)
            items = connection.execute("SELECT body FROM runs WHERE status = 'running'").fetchall()
            for item in items:
                run = json.loads(item[0])
                if run.get("scoped_workflow") or run.get("plan") or run.get("parent_run_id") or (
                    run["route"] == industrial_bench.ROUTE and run["cohort"] == "offline"
                ):
                    if run.get("scoped_workflow"):
                        stage_timeline.materialize(run)
                        stage_timeline.interrupt(run, "interrupted", "工作台服务已重启")
                    run["status"] = "queued"
                    run["restarts"] = run.get("restarts", 0) + 1
                    run.pop("error", None)
                    for step in run["steps"]:
                        step.update(status="pending", summary="")
                    self.save(connection, run)
                    continue
                run["status"] = "needs_review"
                run["error"] = "服务运行中断，可用相同设置新建一次运行"
                for step in run["steps"]:
                    if step["status"] == "running":
                        step.update(status="needs_review", summary=run["error"])
                self.save(connection, run)

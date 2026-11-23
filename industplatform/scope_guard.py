"""Persisted workflow contracts, entity fences and change receipts."""

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Optional
from uuid import uuid4


class WorkflowStage(str, Enum):
    DOMAIN = "STAGE_1_DOMAIN"
    TOPOLOGY = "STAGE_2_TOPOLOGY"
    ACQUISITION = "STAGE_3_ACQUISITION"
    TASK_DESIGN = "STAGE_4_TASK_DESIGN"
    VERIFIER = "STAGE_5_VERIFIER"
    EVAL_EXPORT = "STAGE_6_EVAL_EXPORT"


STAGES = tuple(stage.value for stage in WorkflowStage)
STAGE_NAMES = dict(zip(STAGES, ("领域定位", "工艺图谱", "穷尽采集", "构题", "验证器", "评测与导出")))
ENTITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")
GRANULARITIES = ("STAGE_WIDE", "PROCESS_NODE_ONLY", "TASK_ONLY", "VERIFIER_ONLY")
MIN_CONFIDENCE = .85
APPEND = "APPEND_OR_PATCH"
REBUILD = "REBUILD_TARGET"
READ_ONLY = "READ_ONLY"
PAUSE_EACH_STAGE = "PAUSE_EACH_STAGE"
AUTO_CONTINUE_QUEUE = "AUTO_CONTINUE_QUEUE"
EXECUTION_PACES = (PAUSE_EACH_STAGE, AUTO_CONTINUE_QUEUE)
COLLECTION_STOPS = ("task", "exhaustive")
FULL_WORKFLOW = re.compile(r"(?:连续|自动|依次)(?:执行|推进|完成|跑)(?:整个|全部|这套|所有|六个)?(?:全流程|六阶段|6阶段)|从.{0,16}(?:到|至)导出.{0,8}(?:全流程|自动)")
EVAL_REQUEST = re.compile(r"盲测|调用.{0,10}模型.{0,8}评测|模型.{0,9}(?:梯度)?评测")
_TASK_QUANTITY = re.compile(r"(?:构建|构造|生成|设计|编写|出题|出)(?:出)?\s*([1-9][0-9]?|[一二三四五六七八九十]{1,3})\s*"
                            r"(?:道|个)[^\n，。；、]{0,18}?题")


class ScopeViolationError(ValueError):
    """The requested action falls outside the committed execution scope."""


class ScopeBoundaryError(ScopeViolationError):
    """A tool or mutation exceeds the current authorization, not an evidence or input error."""


GUARD_REJECTION_LIMIT = 3
GUARD_ABORT_CODE = "guard_loop_abort"

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def validate_stage_queue(queue):
    if (not isinstance(queue, (list, tuple)) or not 1 <= len(queue) <= len(STAGES)
            or any(stage not in STAGES for stage in queue)
            or tuple(queue) != tuple(stage for stage in STAGES if stage in queue)):
        raise ScopeViolationError("请按工作流顺序选择互不重复的执行环节")
    return tuple(queue)


def requested_task_count(prompt):
    quantities = set()
    digits = dict(zip("一二三四五六七八九", range(1, 10)))
    for match in _TASK_QUANTITY.finditer(prompt):
        if re.search(r"(?:不|不要|不必|无需|禁止|别)\s*$", prompt[:match.start()]):
            continue
        value = match[1]
        if value.isascii():
            quantities.add(int(value))
        elif "十" in value:
            left, _, right = value.partition("十")
            quantities.add(10 * digits.get(left, 1) + digits.get(right, 0))
        elif value in digits:
            quantities.add(digits[value])
    return next(iter(quantities)) if len(quantities) == 1 else None


def scoped_new_tasks(db, contract, *, ready=False):
    readiness = "AND t.state='ready' AND t.quality_approved=1 AND t.is_stale=0" if ready else ""
    return tuple(row[0] for row in db.execute(f"""SELECT DISTINCT t.id FROM workflow_scope_audit a
        JOIN benchmark_tasks t ON t.domain_id=? AND t.id=a.entity_id
        WHERE a.contract_id=? AND a.kind='applied' AND a.tool='upsert_benchmark_task'
          AND a.detail LIKE '新建题目%' {readiness} ORDER BY a.sequence""", (contract.domain, contract.contract_id)))


@dataclass(frozen=True)
class ExecutionScopeContract:
    contract_id: str
    stage_queue: tuple[str, ...]
    execution_pace: str
    stage_index: int
    granularity: str
    domain_id: Optional[str]
    allowed_process_node_ids: tuple
    allowed_task_ids: tuple
    operation_mode: str
    router_confidence: float
    rationale: str
    ambiguity_prompt: str = ""

    @property
    def domain(self):
        return int(self.domain_id) if self.domain_id is not None else None

    @property
    def target_stage(self):
        return self.stage_queue[self.stage_index]

    @property
    def next_stage(self):
        return next(iter(self.stage_queue[self.stage_index + 1:]), None)

    @classmethod
    def parse(cls, payload, *, confirmed=False):
        if not isinstance(payload, dict):
            raise ScopeViolationError("执行作用域格式无效")
        queue = validate_stage_queue(payload.get("stage_queue", (payload.get("target_stage"),)))
        index = payload.get("stage_index", 0)
        pace = payload.get("execution_pace", PAUSE_EACH_STAGE)
        if type(index) is not int or not 0 <= index < len(queue) or pace not in EXECUTION_PACES:
            raise ScopeViolationError("执行环节位置或推进节奏无效")
        stage, kind = queue[index], payload.get("granularity", "STAGE_WIDE")
        mode, confidence = payload.get("operation_mode", APPEND), payload.get("router_confidence", 0)
        domain = payload.get("domain_id")
        if type(domain) is int:
            domain = str(domain)
        if domain in ("", None):
            domain = None
        elif not isinstance(domain, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", domain):
            raise ScopeViolationError("领域 ID 无效")
        if (kind not in GRANULARITIES or mode not in (APPEND, REBUILD, READ_ONLY)
                or type(confidence) not in (int, float) or not 0 <= confidence <= 1):
            raise ScopeViolationError("阶段、粒度、操作方式或置信度无效")

        def ids(key):
            value = payload.get(key, [])
            if not isinstance(value, (list, tuple)) or len(value) > 32 or any(
                    not isinstance(item, str) or not ENTITY_ID.fullmatch(item) for item in value):
                raise ScopeViolationError("目标 ID 列表无效")
            return tuple(dict.fromkeys(value))

        nodes, tasks = ids("allowed_process_node_ids"), ids("allowed_task_ids")
        if nodes and kind == "STAGE_WIDE" or tasks and kind in ("STAGE_WIDE", "PROCESS_NODE_ONLY"):
            raise ScopeViolationError("粒度必须与目标 ID 一致")
        if ((kind == "PROCESS_NODE_ONLY" and not nodes) or (kind in ("TASK_ONLY", "VERIFIER_ONLY") and not tasks)):
            raise ScopeViolationError("局部范围必须指定目标 ID")
        if stage in STAGES[:3] and tasks or stage == STAGES[0] and nodes:
            raise ScopeViolationError("当前阶段与实体类型不一致")
        if kind == "VERIFIER_ONLY" and stage != STAGES[4]:
            raise ScopeViolationError("仅验证器阶段可以锁定到验证器")
        if mode == REBUILD and not confirmed:
            confidence = min(confidence, .84)
        reason, question = payload.get("rationale", ""), payload.get("ambiguity_prompt", "") or ""
        if not isinstance(reason, str) or not isinstance(question, str):
            raise ScopeViolationError("范围说明须为文本")
        return cls(uuid4().hex, queue, pace, index, kind, domain, nodes, tasks, mode, float(confidence),
                   reason.strip()[:360], question.strip()[:360])


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_scope_contracts(
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), state TEXT NOT NULL
            CHECK(state IN ('clarification','locked','executing','completed','blocked','replaced')),
        body TEXT NOT NULL CHECK(json_valid(body)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("CREATE INDEX IF NOT EXISTS workflow_scope_by_run ON workflow_scope_contracts(run_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_scope_audit(
        sequence INTEGER PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES workflow_scope_contracts(id),
        kind TEXT NOT NULL, tool TEXT NOT NULL, entity_id TEXT, detail TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_node_sources(
        domain_id INTEGER NOT NULL, node_id TEXT NOT NULL, source_id TEXT NOT NULL,
        PRIMARY KEY(domain_id,node_id,source_id),
        FOREIGN KEY(domain_id,node_id) REFERENCES process_nodes(domain_id,id),
        FOREIGN KEY(domain_id,source_id) REFERENCES raw_sources(domain_id,id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_source_passages(
        domain_id INTEGER NOT NULL, node_id TEXT NOT NULL, source_id TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(domain_id,node_id,source_id),
        FOREIGN KEY(domain_id,node_id,source_id) REFERENCES workflow_node_sources(domain_id,node_id,source_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_export_snapshots(
        id TEXT PRIMARY KEY, domain_id INTEGER NOT NULL REFERENCES domains(id), contract_id TEXT NOT NULL
            REFERENCES workflow_scope_contracts(id), kind TEXT NOT NULL, columns_json TEXT NOT NULL, body TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS workflow_frontier_nodes(
        domain_id INTEGER NOT NULL, node_id TEXT NOT NULL, PRIMARY KEY(domain_id,node_id),
        FOREIGN KEY(domain_id,node_id) REFERENCES process_nodes(domain_id,id))""")
    migrate_references = "quality_json" not in {row[1] for row in db.execute("PRAGMA table_info(verified_references)")}
    for table, additions in {
        "workflow_scope_audit": {"output_id": "TEXT", "attempt_id": "TEXT", "rejection_type": "TEXT",
                                 "params_json": "TEXT", "requested_stage": "TEXT"},
        "workflow_source_passages": {
            "artifact_json": "TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(artifact_json))",
            "quality_note": "TEXT NOT NULL DEFAULT ''",
        },
        "verified_references": {
            "excerpt": "TEXT NOT NULL DEFAULT ''",
            "summary_zh": "TEXT NOT NULL DEFAULT ''",
            "quality_json": "TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(quality_json) AND json_type(quality_json)='object')",
        },
        "process_relations": {"evidence_clause_id": "TEXT", "evidence_url": "TEXT",
                              "evidence_locator": "TEXT", "evidence_quote": "TEXT"},
        "benchmark_tasks": {"quality_approved": "INTEGER NOT NULL DEFAULT 0 CHECK(quality_approved IN (0,1))"},
    }.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in additions.items():
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    grounded = db.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='grounded_process_relations'").fetchone()
    if grounded and "verified_reference_nodes" not in grounded[0]:
        db.execute("DROP VIEW grounded_process_relations")
    db.execute("""CREATE VIEW IF NOT EXISTS grounded_process_relations AS
        SELECT domain_id,node_id,id,sender,recipient,deliverable,kind,evidence_id,evidence_clause_id,locator,url
        FROM (
            SELECT r.domain_id,r.node_id,r.id,r.sender,r.recipient,r.deliverable,r.kind,v.id AS evidence_id,
                json_extract(c.value,'$.id') AS evidence_clause_id,
                json_extract(c.value,'$.locator') AS locator,s.canonical_url AS url,
                row_number() OVER (PARTITION BY r.domain_id,r.node_id,r.id
                    ORDER BY v.verified_at DESC,v.id,json_extract(c.value,'$.id')) AS rank
            FROM process_relations r JOIN verified_reference_nodes links ON (r.domain_id,r.node_id)=(links.domain_id,links.node_id)
            JOIN verified_references v ON (links.domain_id,links.reference_id)=(v.domain_id,v.id)
            JOIN raw_sources s ON (v.domain_id,v.source_id)=(s.domain_id,s.id)
            JOIN json_each(v.quality_json,'$.extracted_clauses') c
            WHERE (r.state='verified' AND r.evidence_id=v.id AND r.evidence_clause_id=json_extract(c.value,'$.id'))
                OR (r.state='pending' AND r.evidence_url=s.canonical_url AND r.evidence_quote<>''
                    AND json_extract(c.value,'$.kind') IN ('process','acceptance','failure_case')
                    AND instr(lower(json_extract(c.value,'$.quote')),lower(r.evidence_quote))>0)
        ) WHERE rank=1""")
    if migrate_references:
        from .engineering_quality import FILTERED_SOURCE, marketing_excerpt

        for domain, node, source, excerpt in db.execute("""SELECT v.domain_id,v.node_id,v.source_id,v.excerpt
            FROM verified_references v WHERE v.node_id IS NOT NULL AND v.excerpt<>''""").fetchall():
            if marketing_excerpt(excerpt):
                db.execute("""UPDATE workflow_source_passages SET quality_note=?
                    WHERE (domain_id,node_id,source_id)=(?,?,?) AND quality_note=''""",
                    (FILTERED_SOURCE, domain, node, source))
    # A verifier revision owns its fixture snapshot. Historical domain drafts remain usable.
    for table in ("process_nodes", "benchmark_tasks", "verifier_specs", "eval_runs"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "is_stale" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0 CHECK(is_stale IN (0,1))")
            db.execute(f"ALTER TABLE {table} ADD COLUMN stale_reason TEXT NOT NULL DEFAULT ''")
        if table == "verifier_specs" and "fixtures_json" not in columns:
            db.execute("ALTER TABLE verifier_specs ADD COLUMN fixtures_json TEXT NOT NULL DEFAULT '{}'")
    _stale_triggers(db)


def _stale_triggers(db):
    def downstream(table, row, reason):
        tasks = (f"SELECT id FROM benchmark_tasks WHERE domain_id={row}.domain_id AND id={row}.task_id"
                 if table == "verifier_specs" else f"SELECT {row}.id")
        if table == "process_nodes":
            tasks = f"""SELECT id FROM benchmark_tasks WHERE domain_id={row}.domain_id AND
              (node_id={row}.id OR downstream_id={row}.id OR node_id IN (
                WITH RECURSIVE nodes(id) AS (
                    SELECT downstream_id FROM process_edges WHERE domain_id={row}.domain_id AND upstream_id={row}.id
                    UNION SELECT e.downstream_id FROM process_edges e JOIN nodes n ON n.id=e.upstream_id
                        WHERE e.domain_id={row}.domain_id
                ) SELECT id FROM nodes
              ))"""
        output = ""
        if table == "process_nodes":
            output += f"""UPDATE process_nodes SET is_stale=1,stale_reason='{reason}'
                WHERE domain_id={row}.domain_id AND id IN (
                    WITH RECURSIVE nodes(id) AS (
                      SELECT downstream_id FROM process_edges WHERE domain_id={row}.domain_id AND upstream_id={row}.id
                      UNION SELECT e.downstream_id FROM process_edges e JOIN nodes n ON n.id=e.upstream_id
                        WHERE e.domain_id={row}.domain_id
                    ) SELECT id FROM nodes);
                UPDATE benchmark_tasks SET is_stale=1,stale_reason='{reason}'
                          WHERE domain_id={row}.domain_id AND id IN ({tasks});"""
        if table != "verifier_specs":
            output += f"""UPDATE verifier_specs SET is_stale=1,stale_reason='{reason}'
                          WHERE domain_id={row}.domain_id AND task_id IN ({tasks});"""
        return output + f"""UPDATE eval_runs SET is_stale=1,stale_reason='{reason}'
                            WHERE domain_id={row}.domain_id AND task_id IN ({tasks});"""

    for table, fields, reason in (
        ("process_nodes", ("title", "occupations_json"), "上游工序已更新"),
        ("benchmark_tasks", ("title", "definition_json", "node_id", "downstream_id", "complexity"), "上游题目定义已更新"),
        ("verifier_specs", ("strategy", "image", "layers_json", "fixtures_json"), "上游验证器已更新"),
    ):
        differs = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in fields)
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS scoped_stale_{table}
            AFTER UPDATE OF {','.join(fields)} ON {table} WHEN {differs}
            BEGIN {downstream(table, 'NEW', reason)} END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS scoped_stale_new_verifier AFTER INSERT ON verifier_specs
        BEGIN UPDATE eval_runs SET is_stale=1,stale_reason='上游验证器已更新'
        WHERE domain_id=NEW.domain_id AND task_id=NEW.task_id AND verifier_version<NEW.version; END""")
    for action, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS scoped_stale_binding_{action.lower()}
            AFTER {action} ON task_references BEGIN
            UPDATE verifier_specs SET is_stale=1,stale_reason='上游题目证据已更新'
                WHERE domain_id={row}.domain_id AND task_id={row}.task_id;
            UPDATE eval_runs SET is_stale=1,stale_reason='上游题目证据已更新'
                WHERE domain_id={row}.domain_id AND task_id={row}.task_id; END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS scoped_stale_edge_{action.lower()}
            AFTER {action} ON process_edges BEGIN
            UPDATE process_nodes SET is_stale=1,stale_reason='上游工艺连接已更新'
                WHERE domain_id={row}.domain_id AND id={row}.downstream_id;
            UPDATE benchmark_tasks SET is_stale=1,stale_reason='上游工艺连接已更新'
                WHERE domain_id={row}.domain_id AND node_id={row}.downstream_id;
            UPDATE verifier_specs SET is_stale=1,stale_reason='上游工艺连接已更新'
                WHERE domain_id={row}.domain_id AND task_id IN (SELECT id FROM benchmark_tasks
                    WHERE domain_id={row}.domain_id AND node_id={row}.downstream_id);
            UPDATE eval_runs SET is_stale=1,stale_reason='上游工艺连接已更新'
                WHERE domain_id={row}.domain_id AND task_id IN (SELECT id FROM benchmark_tasks
                    WHERE domain_id={row}.domain_id AND node_id={row}.downstream_id);
            END""")
    for table, key in (("process_nodes", "id"), ("benchmark_tasks", "id"), ("verifier_specs", "task_id"), ("eval_runs", "id")):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS scope_stale_audit_{table}
            AFTER UPDATE OF is_stale,stale_reason ON {table}
            WHEN NEW.is_stale=1 AND (NEW.is_stale IS NOT OLD.is_stale OR NEW.stale_reason IS NOT OLD.stale_reason)
                AND workflow_contract_id() IS NOT NULL
            BEGIN INSERT INTO workflow_scope_audit(contract_id,kind,tool,entity_id,detail)
                VALUES (workflow_contract_id(),'stale','{table}',NEW.{key},NEW.stale_reason); END""")


def active_domain(db, domain):
    return db.execute("""SELECT d.name FROM domains d LEFT JOIN domain_library l ON l.domain_id=d.id
                        WHERE d.id=? AND coalesce(l.archived,0)=0""", (domain,)).fetchone()


def validate_targets(db, contract):
    if contract.domain is None and contract.target_stage == STAGES[0]:
        return
    if not active_domain(db, contract.domain):
        raise ScopeViolationError("请先选择有效领域，或确认领域定位阶段")
    for node in contract.allowed_process_node_ids:
        if not db.execute("SELECT 1 FROM process_nodes WHERE domain_id=? AND id=?", (contract.domain, node)).fetchone():
            raise ScopeViolationError("目标工序不属于已锁定领域")
    for task in contract.allowed_task_ids:
        row = db.execute("SELECT node_id FROM benchmark_tasks WHERE domain_id=? AND id=? AND state<>'archived'",
                         (contract.domain, task)).fetchone()
        if not row or contract.allowed_process_node_ids and row[0] not in contract.allowed_process_node_ids:
            raise ScopeViolationError("题目不属于已锁定范围")


class ScopeGuardMiddleware:
    def __init__(self, store, run, contract, allowed, attempt_id=None):
        self.store, self.run, self.contract = store, run, contract
        self.allowed = frozenset(allowed)
        self.attempt_id = attempt_id or uuid4().hex
        self.aborted = None

    def check(self):
        if self.aborted is not None:
            raise self.aborted

    def reject(self, tool, reason, *, params=None, category="validation", requested_stage=None):
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.aborted is not None:
                return
            db.execute("""INSERT INTO workflow_scope_audit
                (contract_id,kind,tool,detail,attempt_id,rejection_type,params_json,requested_stage)
                VALUES (?,'rejected',?,?,?,?,?,?)""",
                (self.contract.contract_id, tool, reason, self.attempt_id, category, dumps(params or {}), requested_stage))
            if category != "scope_guard":
                return
            rows = db.execute("""SELECT tool,requested_stage FROM workflow_scope_audit
                WHERE contract_id=? AND attempt_id=? AND kind='rejected' AND rejection_type='scope_guard'
                ORDER BY sequence""", (self.contract.contract_id, self.attempt_id)).fetchall()
            if len(rows) >= GUARD_REJECTION_LIMIT:
                from .codex_agent import AgentError

                later = {stage for _, stage in rows}
                later_stages = [stage for stage in STAGES[STAGES.index(self.contract.target_stage) + 1:] if stage in later]
                message = "模型累计 3 次尝试执行超出当前阶段授权的操作，已提前中止。"
                if later_stages and self.contract.operation_mode != READ_ONLY:
                    message += "如需执行这些操作，请确认后续阶段后重试。"
                failure = AgentError(message, code=GUARD_ABORT_CODE, detail="、".join(dict.fromkeys(tool for tool, _ in rows)))
                failure.diagnostic["suggested_stages"] = later_stages
                self.aborted = failure

    def node(self, db, node, *, exists=True):
        contract = self.contract
        if not isinstance(node, str) or not ENTITY_ID.fullmatch(node):
            raise ScopeViolationError("须指定唯一工序 ID")
        if contract.allowed_process_node_ids and node not in contract.allowed_process_node_ids:
            raise ScopeBoundaryError("目标工序超出锁定范围")
        if exists and not db.execute("SELECT 1 FROM process_nodes WHERE domain_id=? AND id=?", (contract.domain, node)).fetchone():
            raise ScopeViolationError("未找到本领域的目标工序")

    def task(self, db, task, *, exists=True):
        contract = self.contract
        if not isinstance(task, str) or not ENTITY_ID.fullmatch(task):
            raise ScopeViolationError("须指定唯一题目 ID")
        if contract.allowed_task_ids and task not in contract.allowed_task_ids:
            raise ScopeBoundaryError("目标题目超出锁定范围")
        row = db.execute("""SELECT node_id FROM benchmark_tasks WHERE domain_id=? AND id=? AND state<>'archived'""",
                         (contract.domain, task)).fetchone()
        if row:
            self.node(db, row[0])
        elif exists or contract.allowed_task_ids:
            raise ScopeViolationError("未找到本领域的目标题目")

    @contextmanager
    def write(self, tool, args):
        try:
            self.check()
            if tool not in self.allowed:
                raise ScopeBoundaryError("本阶段没有此工具的写入权限")
            with closing(self.store.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self.check()
                state = db.execute("SELECT state FROM workflow_scope_contracts WHERE id=? AND run_id=?",
                                   (self.contract.contract_id, self.run["id"])).fetchone()
                status = db.execute("""SELECT status,json_extract(body,'$.execution_scope.contract_id')
                    FROM runs WHERE id=?""", (self.run["id"],)).fetchone()
                if state != ("executing",) or status != ("running", self.contract.contract_id):
                    raise ScopeViolationError("契约未执行或已结束")
                validate_targets(db, self.contract)
                if "domain_id" in args and args["domain_id"] != self.contract.domain_id:
                    raise ScopeBoundaryError("目标领域超出锁定范围")
                if "process_node_id" in args:
                    self.node(db, args["process_node_id"], exists=tool != "upsert_process_node")
                if "task_id" in args:
                    self.task(db, args["task_id"], exists=tool != "upsert_benchmark_task")
                db.create_function("workflow_contract_id", 0, lambda: self.contract.contract_id)
                yield db
        except sqlite3.IntegrityError:
            raise ScopeViolationError("目标存在引用或数据冲突，未写入") from None

    def append(self, db, kind, tool, entity=None, detail="", *, output_id=None):
        db.execute("INSERT INTO workflow_scope_audit(contract_id,kind,tool,entity_id,detail,output_id) VALUES (?,?,?,?,?,?)",
                   (self.contract.contract_id, kind, tool, entity, detail, output_id))

def restore_contract(body):
    value = json.loads(body)
    stage = value.pop("target_stage", None)
    auto = value.pop("auto_advance_next_stage", False)
    if "stage_queue" not in value:
        value.update(stage_queue=STAGES[STAGES.index(stage):] if auto else (stage,),
                     stage_index=0, execution_pace=AUTO_CONTINUE_QUEUE if auto else PAUSE_EACH_STAGE)
    value["stage_queue"] = tuple(value["stage_queue"])
    value["allowed_process_node_ids"] = tuple(value["allowed_process_node_ids"])
    value["allowed_task_ids"] = tuple(value["allowed_task_ids"])
    return ExecutionScopeContract(**value)


def public_contract(contract):
    return {**asdict(contract), "target_stage": contract.target_stage, "next_stage": contract.next_stage,
            "stage_name": STAGE_NAMES[contract.target_stage]}


def extend_stage_queue(db, run, stages):
    selected = validate_stage_queue(stages)
    scope = run.get("execution_scope") or {}
    contract_id = scope.get("contract_id")
    row = db.execute("SELECT body FROM workflow_scope_contracts WHERE id=? AND run_id=? AND state='blocked'",
                     (contract_id, run["id"])).fetchone()
    if not row:
        raise ScopeViolationError("当前阶段范围已变化，请重新确认")
    contract = restore_contract(row[0])
    later = STAGES[STAGES.index(contract.target_stage) + 1:]
    if contract.operation_mode == READ_ONLY or any(stage not in later for stage in selected):
        raise ScopeViolationError("只可为可写任务确认当前阶段之后的环节")
    queue = tuple(stage for stage in STAGES if stage in contract.stage_queue or stage in selected)
    updated = replace(contract, stage_queue=queue, execution_pace=PAUSE_EACH_STAGE)
    db.execute("UPDATE workflow_scope_contracts SET body=? WHERE id=?",
               (dumps({**asdict(updated), "target_stage": updated.target_stage}), contract_id))
    scope.update(public_contract(updated))
    db.execute("""INSERT INTO workflow_scope_audit(contract_id,kind,tool,detail)
        VALUES (?,'scope_extended','retry_stage',?)""", (contract_id, "用户确认后续环节 " + "、".join(selected)))


def rejection_details(db, contract_id, attempt_id=None):
    rows = db.execute("""SELECT sequence,tool,entity_id,detail,created_at,rejection_type,params_json,requested_stage
        FROM workflow_scope_audit WHERE contract_id=? AND kind='rejected'
        AND (? IS NULL OR attempt_id=?) ORDER BY sequence""", (contract_id, attempt_id, attempt_id))
    details = []
    for seq, tool, entity, reason, at, category, params, stage in rows:
        if category is None:
            if reason == "原文地址解析到非公网网络":
                category = "source_policy"
            elif reason and "检索失败（HTTP 429）" in reason:
                category = "source_rate_limit"
            elif reason == "当前任务已锁定分类，请新建任务重新定位":
                category = "scope_guard"
        details.append(dict(sequence=seq, tool=tool, entity_id=entity, reason=reason, at=at,
                            category=category or "legacy", parameters=json.loads(params) if params else {},
                            requested_stage=stage))
    return details


def receipt(db, contract, attempt_id=None):
    rows = db.execute("""SELECT kind,tool,entity_id,detail FROM workflow_scope_audit
                         WHERE contract_id=? ORDER BY sequence""", (contract.contract_id,)).fetchall()
    changes = [dict(tool=tool, entity_id=entity, detail=detail) for kind, tool, entity, detail in rows if kind == "applied"]
    stale = [dict(table=tool, entity_id=entity, reason=detail) for kind, tool, entity, detail in rows if kind == "stale"]
    rejections = rejection_details(db, contract.contract_id, attempt_id)
    return {"contract_id": contract.contract_id, "stage": contract.target_stage, "changes": changes, "stale": stale,
            "rejected": len(rejections), "rejections": rejections,
            "suggestions": [detail for kind, _, _, detail in rows if kind == "suggestion"],
            "read_only_stages": [item for item in STAGES if item != contract.target_stage]}

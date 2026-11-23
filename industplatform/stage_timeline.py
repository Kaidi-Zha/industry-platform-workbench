"""Durable trace buckets owned by individual workflow stage attempts."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from .scope_guard import STAGES, STAGE_NAMES


KEYS = {stage: f"stage_{index}" for index, stage in enumerate(STAGES, 1)}
LABELS = {"routing": "范围路由", **{KEYS[s]: f"阶段 {i} · {STAGE_NAMES[s]}" for i, s in enumerate(STAGES, 1)}}
LEGACY_KEYS = {**KEYS, **{name: KEYS[s] for s, name in STAGE_NAMES.items()},
               "识别执行范围": "routing", "范围路由": "routing", "scope": "routing"}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _bucket(key, identity, start=None, **fields):
    return {"id": identity, "stage_key": key, "stage_label": LABELS[key], "status": "running",
            "started_at": start, "events": [], **fields}


def begin(run, key, **fields):
    timeline = materialize(run)
    if timeline and timeline[-1]["status"] == "running":
        finish(timeline[-1], "interrupted", "上一次执行已中断")
    stage = _bucket(key, uuid4().hex, timestamp(), **fields)
    timeline.append(stage)
    return stage


def latest(run):
    timeline = run.get("result", {}).get("stage_timeline")
    return timeline[-1] if timeline else None


def record(stage, kind, fields, *, stamp=None):
    fields = dict(fields)
    child = fields.pop("subagent_id", None)
    event = {**fields, "kind": kind, "stage_key": stage["stage_key"], "stage_label": stage["stage_label"],
             "agent_kind": "subagent" if child else "main", "subagent_id": child,
             "parent_turn_id": fields.get("parent_turn_id") or (stage.get("turn_id") if child else None)}
    if kind == "progress":
        current = stage.get("activity", {})
        if event != {key: value for key, value in current.items() if key != "since"}:
            stage["activity"] = {**event, "since": stamp or timestamp()}
        return event
    if kind == "reasoning" and not event.get("preview", "").strip():
        return None
    events = stage["events"]
    if event.get("item"):
        identity = ("kind", "thread", "turn", "subagent_id", "item")
        for previous in reversed(events):
            if all(previous.get(key) == event.get(key) for key in identity):
                previous.update(event)
                return previous
    event["at"] = stamp or timestamp()
    events.append(event)
    return event


def finish(stage, status, outcome="", *, summary="", error=None, at=None):
    stage.update(status=status, finished_at=at or timestamp(), outcome=outcome)
    if summary:
        stage["summary"] = summary
    if error:
        stage["error"] = record(stage, "stage_error", error, stamp=stage["finished_at"])
    for event in stage["events"]:
        if event.get("status") in ("running", "pendingInit"):
            event["status"] = "interrupted"


def interrupt(run, status, outcome):
    stage = latest(run)
    if stage and stage["status"] in ("running", "awaiting_scope"):
        finish(stage, status, outcome)


def _thread_start(thread):
    try:
        value = UUID(thread)
        if value.version == 7:
            return datetime.fromtimestamp((value.int >> 80) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return None


def _legacy_trace(run):
    """Project old attributed records without inventing ownership for unlabelled logs."""
    result = run.get("result", {})
    scope = run.get("execution_scope") or {}
    steps = {row["id"]: row for row in run.get("steps", [])}
    timeline, roots = [], {}

    def add(key, start=None):
        stage = _bucket(key, f"archive-{len(timeline)}", start)
        timeline.append(stage)
        return stage

    for row in result.get("rollout_threads", []):
        key, thread = LEGACY_KEYS.get(row.get("phase")), row.get("thread_id")
        if key and isinstance(thread, str) and thread not in roots:
            stage = roots[thread] = add(key, _thread_start(thread))
            stage["thread_id"] = thread
    cursor = 0
    agents = result.get("subagents", {})
    for event in result.get("harness_items", []):
        thread = event.get("thread")
        child = agents.get(thread)
        parent = child.get("parent") if isinstance(child, dict) else None
        stage = roots.get(thread) or roots.get(parent)
        key = LEGACY_KEYS.get(event.get("phase"))
        if stage:
            cursor = max(cursor, timeline.index(stage))
        elif key:
            for index in range(cursor, len(timeline)):
                if timeline[index]["stage_key"] == key:
                    cursor, stage = index, timeline[index]
                    break
            if stage is None:
                stage = add(key)
                cursor = len(timeline) - 1
        if stage is None:
            continue
        fields = dict(event)
        kind = fields.pop("kind", "tool")
        fields.pop("phase", None)
        preview = fields.get("preview", "")
        if kind == "tool" and preview in (event.get("phase", "") + " · 已返回", event.get("phase", "") + " · 调用被拒绝"):
            fields.update(preview="", status="completed" if preview.endswith("已返回") else "failed")
        if parent and parent in roots and thread not in roots:
            fields["subagent_id"] = thread
        elif thread and thread not in roots:
            # A historical foreign thread without explicit ancestry cannot be attributed.
            continue
        record(stage, kind, fields, stamp=event.get("at") or stage["started_at"] or run.get("created_at", ""))
    for report in result.get("scope_receipts", []):
        key = KEYS.get(report.get("stage"))
        if not key:
            continue
        stage = next((row for row in reversed(timeline) if row["stage_key"] == key), None) or add(key)
        stage.update(contract_id=report.get("contract_id"), outcome=report.get("outcome", ""),
                     summary=report.get("summary", ""), status="needs_review" if report.get("status") == "blocked" else "completed")
        if key == "stage_1" and result.get("domain_library"):
            stage["domain_library"] = result["domain_library"]
    scope_step = steps.get("scope", {})
    agent_step = steps.get("agent", {})
    running = run.get("status") == "running"
    error = run.get("error")
    has_scope_failure = error and scope_step.get("status") in ("needs_review", "failed")
    has_agent_failure = error and agent_step.get("status") in ("needs_review", "failed")
    key = "routing" if has_scope_failure or scope_step.get("status") == "running" else KEYS.get(scope.get("target_stage"))
    needs_tail = has_scope_failure or has_agent_failure or running or run.get("status") in ("awaiting_scope", "paused", "cancelled")
    if key and needs_tail and (timeline or has_scope_failure or has_agent_failure) and (
        not timeline or timeline[-1]["stage_key"] != key
    ):
        add(key)
    for index, stage in enumerate(timeline):
        last = index == len(timeline) - 1
        status = stage["status"]
        if last and (has_scope_failure or has_agent_failure and not result.get("scope_retryable") and not result.get("coverage_pending")):
            diagnostic = {"message": error, "retryable": True}
            if error == "Codex 运行超时，可调整任务范围后重试":
                diagnostic.update(code="WORKBENCH_TIMEOUT_LEGACY", detail=error)
            finish(stage, "failed", error=diagnostic, at=run.get("updated_at"))
        elif last and run.get("status") in ("paused", "cancelled", "awaiting_scope"):
            if status != "completed":
                finish(stage, run["status"], at=run.get("updated_at"))
        elif last and running and status == "running":
            activity = result.get("harness_activity")
            if activity and LEGACY_KEYS.get(activity.get("phase")) == stage["stage_key"]:
                record(stage, "progress", {"state": activity.get("kind"),
                       **{k: v for k, v in activity.items() if k not in ("kind", "since", "phase")}},
                       stamp=activity.get("since"))
        else:
            if status == "running":
                stage["status"] = "completed"
            end = run.get("updated_at") if last else timeline[index + 1]["started_at"]
            if stage["started_at"] and end:
                stage["finished_at"] = end
                stage["timing_estimated"] = True
        if stage["stage_key"] == "routing" and stage["status"] == "completed":
            stage["outcome"] = "已锁定执行范围"
    return timeline


def public(run):
    result = run.get("result", {})
    if "stage_timeline" in result:
        return result["stage_timeline"]
    if run.get("scoped_workflow"):
        return _legacy_trace(run)
    return []


def materialize(run):
    result = run["result"]
    if "stage_timeline" not in result:
        result["stage_timeline"] = public(run)
    return result["stage_timeline"]

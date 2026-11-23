"""Run collection, task generation, scoring, and tool verification."""

import hashlib
import io
import json
import logging
import threading
import time
import zipfile
from contextlib import closing

from . import __version__, api_agent, codex_agent, codex_local, collection, industrial_bench, metrology, nist_benchmark, public_web, research, sources
from .agent_runtime import AgentRuntimeRouter
from .credentials import CredentialError
from . import frontier, workbench
from .evaluation import EvaluationError, difficulty_from_panel, model_config, solve_with_profile, solve_with_provider
from .settings import AGENT_PREFERENCES, DEFAULTS, PERMISSION_PREFERENCES
from .storage import AGENT_ROUTES, RunInterrupted

logger = logging.getLogger(__name__)


def catalog(profiles=None):
    result = sources.catalog()
    result["version"] = __version__
    result["research_backend"] = public_web.backend()
    result["delegation_roles"] = codex_agent.DELEGATION_ROLES
    result["cohorts"] = [{"id": "offline", "label": "离线工具基线"}]
    result["api_agent_available"] = True
    result["routes"].append(dict(api_agent.ROUTE_DETAILS))
    result["agent_available"] = bool(codex_agent.executable())
    if result["agent_available"]:
        result["routes"].append(dict(codex_agent.ROUTE_DETAILS))
    try:
        if (profiles is not None and profiles.panel_ready()) or model_config():
            result["cohorts"].append({"id": "live", "label": "模型 API 盲测"})
    except EvaluationError as exc:
        result["configuration_note"] = str(exc)
    return result


def accept_request(payload, native_panel=False):
    if not isinstance(payload, dict):
        raise ValueError("任务参数必须为 JSON 对象")
    route = payload.get("route", "demo-cnc")
    mode = payload.get("execution_mode")
    cohort = payload.get("cohort", "offline")
    prompt = payload.get("prompt", "")
    seed = payload.get("seed")
    route_ids = {route_info["id"] for route_info in sources.catalog()["routes"]}
    if route == codex_agent.ROUTE:
        if mode != codex_local.SUBSCRIPTION and not codex_agent.executable():
            raise ValueError("服务尚未启用 Codex Agent")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请输入要交给 Codex 的工业任务")
    elif route == api_agent.ROUTE:
        if mode != codex_local.API_GATEWAY and not codex_agent.executable(configured_model=True):
            raise ValueError("旧版 API Agent 需要本机 Codex CLI。可改用 API 模型网关。")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请输入要交给 Codex 的工业任务")
        if not isinstance(payload.get("model_profile_id"), str):
            raise ValueError("请选择模型连接")
    elif not isinstance(route, str) or route not in route_ids:
        raise ValueError("请选择已登记的采集入口")
    if cohort not in ("offline", "live"):
        raise ValueError("请选择已登记的评估方式")
    max_prompt = 4000 if route in AGENT_ROUTES else 280
    if not isinstance(prompt, str) or len(prompt) > max_prompt:
        raise ValueError("任务文本上限为 {} 字符".format(max_prompt))
    if seed is not None and (route != "demo-cnc" or type(seed) is not int or not 0 <= seed <= 999999):
        raise ValueError("CNC 题目种子须为 0 至 999999 的整数")
    if cohort == "live":
        if route != "demo-cnc":
            raise ValueError("当前模型盲测仅接受 CNC 合成题包")
        if not native_panel and not model_config():
            raise ValueError("尚未配置可调用的模型面板")
    return route, cohort, prompt.strip(), seed


def update_step(store, run, step_id, status, summary=""):
    step = next(step for step in run["steps"] if step["id"] == step_id)
    step.update(status=status, summary=summary)
    store.update(run)


def run_discovery(store, run):
    assets = sources.discover(run["route"], run["prompt"])
    run["result"]["assets"] = assets
    update_step(store, run, "discover", "completed", "发现 {} 条可查看记录".format(len(assets)))
    run["status"] = "needs_review"
    sampled = sum(asset["status"] == "sampled" for asset in assets)
    run["result"]["metrics"] = {"asset_count": len(assets), "sampled_count": sampled}
    update_step(
        store, run, "qualify", "needs_review",
        "已核对 {} 项样本或结构证据，真实资产需确认任务所需字段与用途许可后再入题".format(sampled)
        if assets else "本轮未返回匹配的候选记录，可调整检索词或采集入口",
    )


def public_task(task):
    return {key: task[key] for key in ("id", "title", "difficulty", "description", "checks", "inputs", "rule_ids", "data_origin")}


def run_benchmark(store, run, connections=None):
    result = run["result"]
    result["assets"] = [metrology.demo_asset()]
    update_step(store, run, "discover", "completed", "载入 CNC 检测合成数据")
    update_step(store, run, "qualify", "running")
    result["rules"] = metrology.rules()
    update_step(store, run, "qualify", "completed", "已登记示例资产范围及验收规则")

    update_step(store, run, "author", "running")
    seed = run.get("seed")
    tasks = metrology.make_tasks(seed) if seed is not None else metrology.make_tasks()
    for task in tasks:
        if not metrology.verify_answer(task, task["reference"])["passed"]:
            raise RuntimeError("参考答案未通过验证器：" + task["id"])
    result["tasks"] = [public_task(task) for task in tasks]
    update_step(store, run, "author", "completed", "生成 {} 道题并验证独立参考答案".format(len(tasks)))

    update_step(store, run, "verify", "running")
    result["tool"] = metrology.audit_tool()
    live = run["cohort"] == "live"
    config = model_config() if live and connections is None and not run.get("configured_panel") else None
    if live and config is None and connections is None:
        raise EvaluationError("模型面板配置已变更")
    native = {profile["id"]: profile for group in connections.values() for profile in group} if connections else {}
    if connections:
        panel = {tier: [profile["id"] for profile in group] for tier, group in connections.items()}
    else:
        panel = config[2] if config else {"baseline": ["offline-inspection-cli"]}
    result["panel"] = panel

    for task, visible in zip(tasks, result["tasks"]):
        trials = []
        for tier, models in panel.items():
            for model in models:
                item = {"task_id": task["id"], "tier": tier, "model": model}
                try:
                    if model in native:
                        connection = native[model]
                        item.update(model=connection["model"], profile_id=model, provider=connection["protocol"],
                                    profile=connection["name"], reasoning_effort=connection.get("reasoning_effort", ""))
                        answer = solve_with_profile(visible, connection)
                    elif config:
                        answer = solve_with_provider(visible, model, config)
                    else:
                        files = visible["inputs"]
                        answer = metrology.inspect_csv(files["requirements.csv"], files["measurements.csv"])
                    judgement = metrology.verify_answer(task, answer)
                    item.update(passed=judgement["passed"], reason=judgement["reason"], status="scored")
                    # Answers and per-check values stay in the evaluation process.
                except EvaluationError as exc:
                    item.update(passed=None, reason=str(exc), status="provider_error")
                trials.append(item)
                result["evaluations"].append(item)
                store.update(run)
        visible["measured_difficulty"] = difficulty_from_panel(trials, panel) if live else "未做模型盲测"

    scored = [trial for trial in result["evaluations"] if trial["status"] == "scored"]
    provider_errors = len(result["evaluations"]) - len(scored)
    result["metrics"] = {
        "asset_count": len(result["assets"]),
        "task_count": len(tasks),
        "attempted": len(result["evaluations"]),
        "scored": len(scored),
        "passed": sum(item["passed"] for item in scored),
        "provider_errors": provider_errors,
    }
    audit_failed = not all(check["passed"] for check in result["tool"]["checks"])
    if provider_errors or audit_failed:
        run["status"] = "needs_review"
        reason = "有 {} 条模型接口错误".format(provider_errors) if provider_errors else "开源工具验收存在未通过项"
        update_step(store, run, "verify", "needs_review", reason)
        return
    update_step(store, run, "verify", "completed", "完成 {} 次判分和开源 CLI 验收".format(len(scored)))
    run["status"] = "completed"
    update_step(store, run, "publish", "completed", "可下载公开题包、数据归属和分数摘要")


def run_industrial_benchmark(store, run, connections=None, *, registered_source=None):
    specification = run["benchmark"]
    source = registered_source or industrial_bench.case(store, specification["case_id"])
    layout = industrial_bench.template(store, specification["template_id"], specification["template_version"])
    if not industrial_bench.matches_template(source, layout):
        raise EvaluationError("案例与模板的领域或验证器不匹配")
    value = industrial_bench.variant(source, specification["variant"])
    layout = {**layout, "domain": source["domain"], "assignment": specification["assignment"]}
    for key in ("role", "recipient", "business_value"):
        if specification["metadata"].get(key):
            layout[key] = specification["metadata"][key]
    live = run["cohort"] == "live"
    if live and (not connections or not (value["share_allowed"] or specification.get("case_send_confirmed"))):
        raise EvaluationError("模型面板未就绪或案例没有模型评测授权")
    result = run["result"]
    result["assets"] = [{"id": item["id"], "title": item["title"], "source": item["url"],
                         "version": item["version"], "status": "sampled" if item["level"] != "document" else "available"}
                        for item in value["references"]]
    update_step(store, run, "discover", "completed", "载入案例输入及 Reference")
    expected = industrial_bench.oracle(value, layout["recipient"])
    audit = industrial_bench.audit(value, expected)
    result["tool"] = audit
    result["rules"] = [{**row, "title": row["name"], "description": row["basis"]}
                       for row in industrial_bench.verifier_layers(value)]
    if audit["status"] != "passed":
        raise EvaluationError("分层验证器回归失败")
    update_step(store, run, "qualify", "completed", "格式、工程判断及下游交付检查已通过回归")
    task = industrial_bench.make_task(value, layout, specification["metadata"])
    task.update(verification="offline_verified", measured_difficulty="未做模型盲测")
    result["tasks"] = [task]
    update_step(store, run, "author", "completed", "已冻结题包输入、来源和版本")
    update_step(store, run, "verify", "running")
    if live:
        panel = {tier: [profile["id"] for profile in group] for tier, group in connections.items()}
        result["panel"] = panel
        repetitions = specification["repetitions"]
        for tier, group in connections.items():
            for profile in group:
                for trial in range(1, repetitions + 1):
                    item = {"task_id": task["id"], "tier": tier, "model": profile["model"],
                            "provider": profile["protocol"], "profile": profile["name"], "profile_id": profile["id"],
                            "reasoning_effort": profile.get("reasoning_effort", ""), "trial": trial}
                    started = time.monotonic()
                    try:
                        answer = solve_with_profile(task, profile)
                        judgement = industrial_bench.score(value, expected, answer)
                        item.update(judgement, status="scored", answer_format="json" if isinstance(answer, dict) else "invalid")
                    except EvaluationError as exc:
                        item.update(passed=None, reason=str(exc), status="provider_error")
                    item["latency_ms"] = round((time.monotonic() - started) * 1000)
                    result["evaluations"].append(item)
                    store.update(run)
        if specification.get("panel_mode") == "probe":
            task["measured_difficulty"] = "探索性盲测，未定档"
        else:
            task["measured_difficulty"] = difficulty_from_panel(result["evaluations"], panel, repetitions)
    else:
        result["panel"] = {}
        if registered_source:
            result["evaluations"] = [{"task_id": task["id"], "tier": "offline", "model": "本地验证器",
                                      **industrial_bench.score(value, expected, expected), "status": "scored"}]
    trials = result["evaluations"]
    scored = [item for item in trials if item["status"] == "scored"]
    result["metrics"] = {"asset_count": len(result["assets"]), "task_count": 1, "attempted": len(trials),
                         "scored": len(scored), "passed": sum(item["passed"] for item in scored),
                         "provider_errors": len(trials) - len(scored)}
    if live:
        result["tier_rates"] = {}
        for tier in result["panel"]:
            attempted = sum(item["tier"] == tier for item in trials)
            eligible = [item for item in scored if item["tier"] == tier]
            passed = sum(item["passed"] for item in eligible)
            result["tier_rates"][tier] = {"attempted": attempted, "scored": len(eligible), "passed": passed,
                                          "pass_rate": passed / len(eligible) if eligible else None}
    errors = result["metrics"]["provider_errors"]
    update_step(store, run, "verify", "needs_review" if errors else "completed",
                f"{errors} 次模型接口异常" if errors else "已记录分层校验与模型判分")
    run["status"] = "needs_review" if errors else "completed"
    if registered_source:
        # Publish the case pointer and the finished package in one transaction.
        next(step for step in run["steps"] if step["id"] == "publish").update(
            status="completed", summary="题包已就绪，下载时生成 ZIP"
        )
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            store.ensure_worker_can_write(db, run["id"])
            industrial_bench.save_case(db, registered_source, input_run_id=run["id"])
            store.save(db, run)
    else:
        update_step(store, run, "publish", "completed", "可下载带校验值和验收记录的本地题包")


def _agent_prompt(store, run):
    options = run.get("agent_options", {}) if run["route"] == api_agent.ROUTE else {}
    limit = options.get("history_turns", DEFAULTS["history_turns"])
    if not limit:
        return run["prompt"], []
    previous = store.previous_turns(run, limit=limit)
    if not previous:
        return run["prompt"], []
    context = []
    related = []
    for turn in previous:
        result = turn.get("result", {})
        run_ids = result.get("agent_run_ids", [])
        related.extend(run_ids)
        context.append({"用户": turn["prompt"][:350], "Agent": result.get("agent_summary", "")[:550],
                        "关联运行": run_ids, "任务": [item["id"] for item in result.get("tasks", [])]})
    return ("当前用户消息\n" + run["prompt"] + "\n\n同一对话的最近记录（供理解当前消息）\n"
            + json.dumps(context, ensure_ascii=False)), list(dict.fromkeys(related))


def finish_automated_benchmark(store, run, study, stop):
    result = run["result"]
    selection = study.state.get("benchmark_source")
    update_step(store, run, "sample", "running", "核查所选来源并在内存抽取 QIF 样本")
    if not selection:
        raise EvaluationError("采集结果已保留，尚未选定与计划匹配的可核验取样入口")
    if stop and stop.is_set():
        raise EvaluationError("服务关闭，已保存采集检查点")
    if result.get("sample") and result["tasks"]:
        saved = result["sample"]["case"]
        inputs = result["tasks"][0]["inputs"]
        case = {**saved, "inputs": {name: inputs[name] for name in industrial_bench.INPUTS[saved["verifier"]]}}
    else:
        try:
            case, reference, transferred = nist_benchmark.sample_case(store, run, selection, stop)
        except ValueError as exc:
            raise EvaluationError(str(exc)) from None
        study.track_sample(reference)
        result["sample"] = {"case": industrial_bench.case_summary(case), "transfer_bytes": transferred,
                            "member_bytes": reference["verification"]["bytes"], "storage": "memory",
                            "reference_id": reference["id"]}
    case["input_run_id"] = run["id"]
    result["coverage"] = study.status()
    run["benchmark"] = {"case_id": case["id"], "template_id": case["verifier"], "template_version": 1,
                        "variant": "as_recorded", "metadata": {}, "assignment": "", "repetitions": 1}
    update_step(store, run, "sample", "completed", "已核对并登记实样，归档及原文件仅在内存处理")
    run_industrial_benchmark(store, run, registered_source=case)


def run_agent(store, run, port, stop, connection=None, approvals=None, runtime=None):
    runtime = runtime or AgentRuntimeRouter.for_run(store, run)
    result = run["result"]
    configured_model = run["route"] == api_agent.ROUTE
    scope = run.get("plan")
    study = None
    automatic = run.get("auto_benchmark")
    coverage_scope = run.get("frontier_target")
    if coverage_scope:
        result["frontier"] = frontier.start(store, *coverage_scope)
    if automatic and result.get("research_finished"):
        update_step(store, run, "agent", "completed", "已从采集检查点恢复")
        finish_automated_benchmark(store, run, research.Research(store, run), stop)
        return
    children = store.children(run["id"]) if scope else []
    native_checkpoint = (result.get("native_thread_id")
                         if run.get("restarts") and not runtime.mode else None)
    starting = "正在连接 API 模型" if runtime.mode == codex_local.API_GATEWAY else "正在启动 Codex CLI"
    result.setdefault("agent_events", []).append({
        "kind": "status", "text": "从已保存的检查点续跑" if run.get("restarts") else starting,
    })
    result["agent_run_ids"] = list(dict.fromkeys([
        *result.get("agent_run_ids", []), *(child["id"] for child in children)
    ]))
    update_step(store, run, "agent", "running", "执行项目 Skills 与工业 MCP 工具")

    def record_event(kind, text, related_id):
        if kind == "harness.thread":
            if text.get("ephemeral"):
                result.setdefault("rollout_threads", []).append({"phase": "agent", "thread_id": text["thread"]})
            else:
                result["native_thread_id"] = text["thread"]
            store.update(run)
            return
        if kind == "harness.tokens":
            result["tokens"] = text["value"]
            store.update(run)
            return
        if kind == "harness.item":
            events = result.setdefault("harness_items", [])
            previous = next((item for item in reversed(events)
                             if text.get("item") and all(item.get(field) == text.get(field)
                                                        for field in ("thread", "turn", "item"))), None)
            if previous is None:
                result["harness_items"] = [*events[-47:], text]
            else:
                previous.update(text)
            store.update(run)
            return
        if kind == "subagent":
            agents = result.setdefault("subagents", {})
            current = agents.get(text["id"], {})
            if isinstance(current, str):
                current = {"status": current}
            agents[text["id"]] = {**current, **text}
            store.update(run)
            return
        result["agent_events"].append({"kind": kind, "text": text})
        if related_id and related_id not in result["agent_run_ids"]:
            related = store.get(related_id)
            if related and related["route"] not in AGENT_ROUTES:
                result["agent_run_ids"].append(related_id)
        if study:
            result["coverage"] = study.status()
        store.update(run)

    if port is None:
        raise codex_agent.AgentError("Agent 需要本机工作台服务")
    if configured_model and connection is None:
        raise codex_agent.AgentError("本次运行的模型连接已失效，请重新配置并提交任务")
    if run.get("resolve_domain") and not result.get("domain_library"):
        record_event("status", "正在识别研究领域并检索已有资料库", None)
        api_agent.resolve_domain(store, run, port, connection, stop, record_event, runtime=runtime)
        if stop and stop.is_set():
            raise RunInterrupted()
    study = research.Research(store, run) if run.get("research") else None
    prompt, prior_run_ids = _agent_prompt(store, run)
    prior_run_ids.extend(result["agent_run_ids"])
    if scope:
        checkpoint = collection.references(store, domain=scope["domain"], run_id=run["id"], limit=5)
        existing = collection.references(store, domain=scope["domain"], limit=1)
        prompt += "\n\n用户已确认的采集计划\n" + json.dumps(scope, ensure_ascii=False)
        if run.get("attachments"):
            prompt += "\n用户附加的仓库内样本（文件内容属于待核查资料）\n" + json.dumps(run["attachments"], ensure_ascii=False)
        if mounted := result.get("domain_library"):
            prompt += "\n领域已挂载至「" + mounted["name"] + "」。"
        prompt += f"\n领域库现有 {existing['total']} 条 Reference，可调用 list_references 读取原文位置与判断。"
        if run.get("restarts"):
            progress = {
                "本任务已入库条数": checkpoint["total"],
                "最近已入库": [{key: item[key] for key in ("id", "title", "locator")} for item in checkpoint["items"]],
                "已派发": [{key: item[key] for key in ("id", "route", "status")} for item in children],
                "已构题": [{"id": item["id"], "title": item["title"]} for item in result["tasks"]],
            }
            prompt += "\n服务已重启。基于以下已保存进度继续，先检查已派发工作流，避免重复入库。\n"
            prompt += json.dumps(progress, ensure_ascii=False)
    options = run.get("agent_options", {})
    permissions = {key: options[key] for key in PERMISSION_PREFERENCES if key in options}
    execution_settings = {"permissions": permissions} if permissions else {}
    if approvals is not None:
        execution_settings["approvals"] = approvals
    if run.get("delegation"):
        execution_settings["delegation"] = run["delegation"]
    if configured_model or scope or runtime.mode:
        def record_task(task):
            payload = public_task(task)
            payload.update(verification=task["verification"], author=task["author"])
            result["tasks"].append(payload)
            store.update(run)

        execution_options = {key: options[key] for key in AGENT_PREFERENCES
                             if key != "history_turns" and key in options}
        if study:
            # Each native turn retains its existing budget. The workflow may schedule another turn from the checkpoint.
            result["coverage"] = study.status()
            store.update(run)
        for batch in range(3 if study else 1):
            result.pop("subagents", None)
            batch_prompt = prompt
            if study:
                prior = study.status()
                if prior["complete"] and (not automatic or prior.get("benchmark_source")):
                    break
                if batch:
                    record_event("status", "按上一轮剩余缺口继续补查", None)
                    locators = study.recent_locators()
                    if locators:
                        batch_prompt += "\n已核读的来源定位，可直接登记相同原文：" + json.dumps(locators, ensure_ascii=False)
                batch_prompt += "\n\n持久化的覆盖记录\n" + json.dumps(prior, ensure_ascii=False)
                batch_prompt += "\n优先查上一轮 next_step 和尚无证据的交付物。"
            prepared, guidance, tools, call_tool, max_turns = api_agent.tool_session(
                connection or {}, batch_prompt, port, record_event, stop,
                on_task=None if study else record_task, prior_run_ids=prior_run_ids, plan=scope, run_id=run["id"],
                prior_task_ids=tuple(item["id"] for item in result["tasks"]), research=study, **execution_options,
            )
            if stop and stop.is_set():
                raise RunInterrupted()
            try:
                native_args = {}
                if not runtime.mode and batch == 0 and (native_checkpoint or run.get("fork_native_thread")):
                    native_args = {"existing_thread": native_checkpoint or run["fork_native_thread"],
                                   "fork_thread": not native_checkpoint}
                result["agent_summary"] = runtime.execute(
                    prepared, port, record_event, stop, profile=connection, guidance=guidance,
                    tools=(tools, call_tool), max_turns=max_turns, **execution_settings, **native_args,
                )
            except codex_agent.TurnBudgetExhausted:
                if not study:
                    raise
                record_event("status", "本轮模型调用已达上限，已保存查证进度", None)
            if study:
                latest = study.status()
                result["coverage"] = latest
                store.update(run)
                if ((automatic and latest.get("benchmark_source")) or (latest["complete"] and not automatic)
                        or (batch and latest == prior) or study.attempts["search"] >= research.MAX_SEARCHES
                        or study.attempts["read"] >= research.MAX_READS):
                    break
    else:
        result["agent_summary"] = runtime.execute(prompt, port, record_event, stop, **execution_settings)
    result["agent_events"].append({"kind": "status", "text": (
        "已提交最终回复" if result.get("agent_summary") else "采集批次结束，已保存查证进度"
    )})
    if automatic and result["coverage"].get("benchmark_source"):
        result["research_finished"] = True
    update_step(store, run, "agent", "completed", "本轮 Agent 执行完成")
    if automatic:
        finish_automated_benchmark(store, run, study, stop)
        return
    if coverage_scope:
        result["frontier"] = frontier.status(store, *coverage_scope)
    if study and (not result["coverage"]["complete"] or coverage_scope and not result["frontier"]["exhausted"]):
        run["status"] = "needs_review"
        run["error"] = ("当前只查到文献索引，开放网页路径和其余覆盖缺口已保留"
                        if public_web.backend() == "crossref" else "覆盖判定尚未结束，已保留查证记录与待查缺口")
        update_step(store, run, "review", "needs_review", run["error"])
        return
    run["status"] = "completed"
    update_step(store, run, "review", "completed", "可查看回复和关联工作流")


def process_run(store, run, port=None, stop=None, connections=None, profiles=None, approvals=None):
    try:
        if run["route"] in AGENT_ROUTES:
            offline_stage = run.get("auto_benchmark") and run["result"].get("research_finished")
            if not offline_stage and run.get("model_profile") and connections is None and profiles:
                try:
                    connections = profiles.restore(run)
                except (ValueError, CredentialError) as exc:
                    raise codex_agent.AgentError("无法续跑 · " + str(exc)) from None
            if run.get("scoped_workflow"):
                from .guarded_workflow import run_workflow

                run_workflow(store, run, port, stop, connections, profiles, approvals=approvals)
            else:
                run_agent(store, run, port, stop, connections, approvals)
            return
        update_step(store, run, "discover", "running")
        if run["route"] == "demo-cnc":
            run_benchmark(store, run, connections)
        elif run["route"] == industrial_bench.ROUTE:
            run_industrial_benchmark(store, run, connections)
        else:
            run_discovery(store, run)
    except RunInterrupted:
        return
    except (sources.SourceError, EvaluationError, codex_agent.AgentError) as exc:
        if stop and stop.is_set() and (run.get("scoped_workflow") or run.get("plan") or run.get("parent_run_id")):
            return
        if run.get("scoped_workflow"):
            from .guarded_workflow import fail_run

            fail_run(store, run, exc)
            return
        run["status"] = "needs_review"
        run["error"] = str(exc)
        if run["route"] in AGENT_ROUTES:
            run["result"].setdefault("agent_events", []).append({"kind": "warning", "text": str(exc)})
        for step in run["steps"]:
            if step["status"] == "running":
                step.update(status="needs_review", summary=str(exc))
        store.update(run)
    except Exception as exc:
        logger.exception("Run %s failed", run["id"])
        if run.get("scoped_workflow"):
            from .guarded_workflow import fail_run

            if not stop or not stop.is_set():
                fail_run(store, run, exc, internal=True)
            return
        run["status"] = "failed"
        run["error"] = "内部执行失败，请查看服务日志"
        for step in run["steps"]:
            if step["status"] == "running":
                step.update(status="failed", summary=run["error"])
        store.update(run)


class Workers:
    def __init__(self, store, count=2, port=None, profiles=None, *, on_agent_finish=None):
        self.store = store
        self.port = port
        self.profiles = profiles
        self.on_agent_finish = on_agent_finish
        self.stop = threading.Event()
        self.control_lock = threading.Lock()
        self.running = {}
        self.approvals = {}
        self.threads = [
            threading.Thread(target=self.work, args=(False,), name="indust-worker-{}".format(index), daemon=True)
            for index in range(count)
        ]
        self.threads.extend(
            threading.Thread(target=self.work, args=(True,), name=f"indust-agent-worker-{index}", daemon=True)
            for index in range(2)
        )
        self.threads.append(threading.Thread(target=self.collect_frontier, name="indust-frontier", daemon=True))

    def collect_frontier(self):
        while not self.stop.is_set():
            try:
                if not frontier.run_one(self.store):
                    self.stop.wait(1)
            except Exception:
                logger.exception("公域采集队列执行异常")
                self.stop.wait(3)

    def start(self):
        self.store.recover()
        for thread in self.threads:
            thread.start()

    def control(self, run_id, action, **options):
        with self.control_lock:
            run, stopping, discarded = self.store.control(run_id, action, **options)
            for identity in stopping:
                self.running[identity].set()
            for identity in discarded:
                if self.profiles:
                    self.profiles.take(identity)
            return run

    def pending_approval(self, run_id):
        with self.control_lock:
            inbox = self.approvals.get(run_id)
            return inbox.snapshot() if inbox else None

    def decide_approval(self, run_id, identity, decision, correction=""):
        with self.control_lock:
            inbox = self.approvals.get(run_id)
            if not inbox or self.running[run_id].is_set():
                raise ValueError("这条审批请求已结束，请查看任务的最新状态")
            inbox.resolve(identity, decision, correction)

    def steer(self, run_id, text):
        with self.control_lock:
            signal = self.running.get(run_id)
            inbox = self.approvals.get(run_id)
            if not signal or (inbox and inbox.snapshot()):
                raise ValueError("请等待执行轮就绪或先处理待审批操作")
            signal.steer(text)

    def work(self, agent_only):
        while not self.stop.is_set():
            with self.control_lock:
                run = None if self.stop.is_set() else self.store.claim(agent_only)
                if run:
                    stop = self.running[run["id"]] = codex_agent.RunSignal()
                    approvals = None
                    if run.get("agent_options", {}).get("approval_mode") == "ask":
                        def policy(destination, action, owner=run):
                            domain = (owner.get("plan") or {}).get("domain")
                            return bool(domain) and workbench.network_approval(self.store, domain, destination, action)

                        approvals = self.approvals[run["id"]] = codex_agent.ApprovalInbox(
                            policy if run.get("plan") else None)

            if not run:
                self.stop.wait(0.2)
                continue
            try:
                connections = self.profiles.take(run["id"]) if self.profiles else None
                process_run(self.store, run, self.port, stop, connections, self.profiles, approvals=approvals)
            except RunInterrupted:
                pass
            finally:
                if agent_only and self.on_agent_finish:
                    self.on_agent_finish()
                with self.control_lock:
                    self.store.settle_control(run)
                    self.running.pop(run["id"], None)
                    self.approvals.pop(run["id"], None)

    def close(self):
        with self.control_lock:
            self.stop.set()
            for stop in self.running.values():
                stop.set()
        for thread in self.threads:
            thread.join(timeout=3)


def benchmark_zip(run):
    result = run["result"]
    industrial = run["route"] == industrial_bench.ROUTE or run.get("auto_benchmark")
    manifest = {
        "platform_version": __version__,
        "run_id": run["id"],
        "route": run["route"],
        "cohort": run["cohort"],
        "seed": run.get("seed"),
        "data_origin": result["tasks"][0]["data_origin"] if industrial else "synthetic",
        "release": "local_evaluation" if industrial else "public_development",
        "assets": result["assets"],
        "rules": result["rules"],
        "tasks": [],
    }
    if run.get("auto_benchmark"):
        manifest.update(plan=run["plan"], sample=result["sample"], coverage=result["coverage"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for task in result["tasks"]:
            entry = {name: value for name, value in task.items() if name != "inputs"}
            entry["inputs"] = {}
            for name, content in task["inputs"].items():
                path = "tasks/{}/{}".format(task["id"], name)
                payload = content.encode("utf-8")
                archive.writestr(path, payload)
                entry["inputs"][name] = {"path": path, "sha256": hashlib.sha256(payload).hexdigest()}
            manifest["tasks"].append(entry)
        archive.writestr("benchmark.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("results.json", json.dumps({
            "panel": result["panel"],
            "metrics": result["metrics"],
            "evaluations": result["evaluations"],
            "tool": result["tool"],
            **({"tier_rates": result["tier_rates"]} if "tier_rates" in result else {}),
        }, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def benchmark_ready(run):
    if run["route"] == "demo-cnc":
        return run["status"] == "completed"
    return (run["route"] == industrial_bench.ROUTE or bool(run.get("auto_benchmark"))) and any(
        step["id"] == "publish" and step["status"] == "completed" for step in run["steps"]
    )

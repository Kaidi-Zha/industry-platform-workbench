import { $, actions, button, column, field, href, icon, popover, popoverSelect, request } from "./workbench-ui.js";
import { tierNames as tierName } from "./workbench-models.js";

export const stageNames = {
  STAGE_1_DOMAIN: "领域定位", STAGE_2_TOPOLOGY: "工艺图谱", STAGE_3_ACQUISITION: "穷尽采集",
  STAGE_4_TASK_DESIGN: "构题工坊", STAGE_5_VERIFIER: "验证器工程", STAGE_6_EVAL_EXPORT: "评测与导出",
};
const stages = Object.keys(stageNames);
const stageNumber = (stage) => stages.indexOf(stage) + 1;
const stageLabel = (stage) => "阶段 " + stageNumber(stage) + " · " + stageNames[stage];
const labelWrap = (label, control) => column($("span", "field-label", label), control.element);
export const pausePace = "PAUSE_EACH_STAGE";
const autoPace = "AUTO_CONTINUE_QUEUE";

function choiceGroup(label, options, choose) {
  const group = $("div", "composer-choices");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", label);
  const choices = options.map(([key, text, symbol]) => {
    const control = button(text, () => choose(key), "ghost size-sm", symbol);
    group.append(control);
    return [key, control];
  });
  return { group, update: (selected, disabled = false) => choices.forEach(([key, row]) => {
    row.setAttribute("aria-pressed", String(selected.includes(key)));
    row.disabled = disabled;
  }) };
}

function composerPopover(label, symbol, className) {
  const panel = $("div", "composer-panel " + className);
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", label);
  const menu = popover(label, panel, { appearance: "ghost size-sm", symbol, align: "end", preferAbove: true });
  for (const key of ["click", "keydown"]) menu.trigger.addEventListener(key, (event) => {
    if (key === "keydown") {
      if (event.key !== "ArrowDown") return;
      event.preventDefault();
      menu.open();
    }
    if (panel.matches(":popover-open")) panel.querySelector("button:not(:disabled)")?.focus();
  });
  return { ...menu, panel };
}

export function executionRangeMenu(value, domain, editPlan) {
  const menu = composerPopover("执行范围", "graph", "execution-range-panel");
  menu.element.classList.add("execution-range");
  let inventory;
  function setQueue(queue) {
    value.stageQueue = stages.filter((stage) => queue.includes(stage));
    if (value.scopeTarget && !value.stageQueue.includes(value.scopeTarget.stage)) delete value.scopeTarget;
    value.userOptions = true;
    update();
  }
  const smart = button("由 Agent 智能识别", () => setQueue([]), "ghost", "spark");
  const presets = choiceGroup("快捷范围", [["1", "调研至采集 1→3"], ["4", "构题到验证 4→5"], ["6", "全流程 1→6"]], (id) =>
    setQueue(presetQueues[id]));
  const presetQueues = { "1": stages.slice(0, 3), "4": stages.slice(3, 5), "6": stages };
  const stageChoices = choiceGroup("执行环节", stages.map((stage, index) => [stage, index + 1 + " " + stageNames[stage]]), (stage) => {
    setQueue(value.stageQueue.includes(stage) ? value.stageQueue.filter((item) => item !== stage) : [...value.stageQueue, stage]);
  });
  stageChoices.group.classList.add("execution-stage-grid");
  const choices = choiceGroup("推进节奏", [[pausePace, "每完成一个环节暂停确认"], [autoPace, "所选环节连续自动推进"]], (pace) => {
    value.executionPace = pace;
    value.userOptions = true;
    update();
  });
  const pace = $("div", "execution-pace");
  pace.append($("span", "field-label", "推进节奏"), choices.group);
  const warning = $("p", "execution-warning");
  warning.setAttribute("role", "status");
  const plan = button("编辑采集计划", () => {
    menu.close(true);
    editPlan();
  }, "ghost size-sm", "task");
  menu.panel.append(smart, presets.group, stageChoices.group, pace, warning, plan);
  function update() {
    const queue = value.stageQueue;
    const numbers = queue.map(stageNumber);
    const contiguous = numbers.length >= 3 && numbers.at(-1) - numbers[0] + 1 === numbers.length;
    const span = numbers.length ? "阶段 " + (contiguous ? numbers[0] + "→" + numbers.at(-1) : numbers.join(", ")) : "智能识别";
    const mode = value.executionPace === autoPace ? " · 连续推进" : queue.length > 1 ? " · 分步确认" : "";
    const caption = "执行范围: " + span + (queue.length === 1 ? "" : mode);
    menu.caption.replaceChildren($("span", "range-full", caption), $("span", "range-compact", span));
    menu.trigger.setAttribute("aria-label", caption);
    menu.trigger.title = caption;
    smart.setAttribute("aria-pressed", String(!queue.length));
    presets.update(Object.keys(presetQueues).filter((key) => queue.join() === presetQueues[key].join()));
    stageChoices.update(queue);
    choices.update([value.executionPace]);
    pace.hidden = queue.length === 1;
    const missingTask = queue.includes(stages[4]) && !queue.includes(stages[3]) && inventory && !inventory.tasks?.length;
    const missingGraph = queue.some((stage) => stageNumber(stage) >= 3) && !queue.includes(stages[1]) && inventory && !inventory.nodes?.length;
    const unmet = queue.map((stage) => inventory?.prerequisites?.[stage]).find((issue) => issue && !queue.includes(issue.stage));
    warning.textContent = missingTask ? "当前领域尚无相关题目，建议先勾选阶段 4 或绑定已有题目。" :
      missingGraph ? "当前领域尚无工艺图谱，建议先勾选阶段 2。" : unmet?.message || "";
    warning.hidden = !warning.textContent;
    plan.hidden = !value.prompt.trim() || !queue.includes(stages[2]);
    menu.reposition();
  }
  menu.panel.addEventListener("toggle", async ({ newState }) => {
    if (newState !== "open") return;
    update();
    try {
      inventory = await request("/api/workbench/scope-options" + (domain ? "?domain=" + encodeURIComponent(domain) : ""));
      if (menu.panel.isConnected) update();
    } catch { /* A dependency hint is optional. Execution validates prerequisites. */ }
  });
  update();
  return menu;
}

export function sandboxMenu(value) {
  const menu = composerPopover("沙箱与采集", "shield", "sandbox-options-panel");
  menu.element.classList.add("sandbox-options");
  const rows = [];
  const field = (label, options, choose) => {
    const controls = choiceGroup(label, options, (id) => {
      choose(id);
      value.userOptions = true;
      update();
    });
    const line = $("div", "sandbox-options-row");
    line.append($("span", "field-label", label), controls.group);
    menu.panel.append(line);
    rows.push({ ...controls, line });
  };
  field("权限模式", [["read-only", "只读"], ["workspace-write", "工作区读写"]], (mode) => { value.sandbox = mode; });
  field("危险命令审批", [["ask", "问我"], ["never", "拒绝"]], (mode) => { value.approval = mode; });
  field("采集停止策略", [["task", "达到本轮目标"], ["exhaustive", "穷尽模式"]], (mode) => { value.collectionStop = mode; });
  function update() {
    rows[0].update([value.sandbox]);
    rows[1].update([value.approval === "ask" ? "ask" : "never"]);
    rows[1].line.hidden = value.mode !== "codex_subscription";
    rows[2].update([value.collectionStop]);
    menu.reposition();
  }
  update();
  return { ...menu, update };
}

export function scopePill(scope) {
  const titles = scope.targets || [];
  const target = titles.length ? "仅限「" + titles.join("、") + "」" :
    scope.domain_name ? "「" + scope.domain_name + "」" : "当前领域";
  const card = $("div", "scope-pill");
  const queue = scope.stage_queue || [];
  const progress = queue.length > 1 ? " · " + (scope.stage_index + 1) + "/" + queue.length : "";
  card.append(icon("lock"), $("span", "", "已锁定范围 · " + stageLabel(scope.target_stage) + " / " + target +
    progress + (scope.next_stage ? (scope.execution_pace === autoPace ? " · 连续推进" : " · 分步确认") : " · 完成后停止")));
  return card;
}

export function clarifyScope(turn, profiles, apply, cancel) {
  const scope = turn.execution_scope;
  const panel = $("form", "scope-clarification");
  const controls = $("div", "scope-fields");
  const modelList = $("div", "scope-models");
  const message = $("p", "inline-error");
  message.setAttribute("role", "alert");
  const stage = popoverSelect("执行阶段", stages.map((id) => [id, stageLabel(id)]), {
    value: scope.stage_locked ? scope.target_stage : scope.suggested_stage || scope.target_stage,
  });
  const domain = popoverSelect("领域", []);
  const entity = popoverSelect("目标范围", []);
  const reloadTargets = (inventory) => {
    const index = stageNumber(stage.value);
    const options = [];
    if (index > 1) options.push(...(inventory.nodes || []).map(({ id, title }) => ["node:" + id, title]));
    if (index >= 4) options.push(...(inventory.tasks || []).map(({ id, title, number }) => ["task:" + id, "第 " + number + " 题 · " + title]));
    const initial = scope.allowed_task_ids?.[0] ? "task:" + scope.allowed_task_ids[0] :
      scope.allowed_process_node_ids?.[0] ? "node:" + scope.allowed_process_node_ids[0] : "";
    entity.setOptions([["", index === 1 ? "领域资料库" : "当前阶段全部对象"], ...options], initial);
    controls.lastElementChild.hidden = index === 1;
    modelList.hidden = index !== 6 || scope.operation_mode === "READ_ONLY";
  };
  const confirm = button("确认范围并执行", null, "primary");
  confirm.type = "submit";
  confirm.disabled = true;
  const dismiss = button("取消本轮", async () => {
    dismiss.disabled = true;
    try { await cancel(); }
    catch (error) { message.textContent = error.message; dismiss.disabled = false; }
  }, "ghost");
  const rebuild = field("confirm_rebuild", "确认重建或删除所选目标", { type: "checkbox" });
  rebuild.hidden = scope.operation_mode !== "REBUILD_TARGET";
  let inventory = {};
  let selectedDomain = scope.domain_id || "";
  let serial = 0;
  async function load() {
    const epoch = ++serial;
    confirm.disabled = true;
    try {
      inventory = await request("/api/workbench/scope-options" + (domain.value ? "?domain=" + encodeURIComponent(domain.value) : ""));
      if (epoch !== serial) return;
      domain.setOptions([["", "自动识别新领域"], ...inventory.domains.map(({ id, name }) => [id, name])],
        selectedDomain);
      if (domain.value && !inventory.nodes) {
        inventory = await request("/api/workbench/scope-options?domain=" + encodeURIComponent(domain.value));
      }
      if (epoch !== serial) return;
      reloadTargets(inventory);
      confirm.disabled = false;
    } catch (error) { if (epoch === serial) message.textContent = error.message; }
  }
  const external = field("confirm_model_send", "本次允许将题面发给所选模型", { type: "checkbox" });
  const choices = profiles.filter(({ tier }) => tierName[tier]).map((profile) => {
    const input = $("input");
    Object.assign(input, { type: "checkbox", name: "profile_id", value: profile.id });
    const label = $("label", "scope-checkbox");
    label.append(input, $("span", "", tierName[profile.tier] + " · " + profile.model));
    return label;
  });
  modelList.append(external, ...(choices.length ? choices : [$("p", "meta", "可在模型设置中配置三个评测梯度。")]));
  stage.disabled = Boolean(scope.stage_locked);
  domain.disabled = Boolean(scope.domain_locked);
  controls.append(labelWrap("执行阶段", stage), labelWrap("领域", domain), labelWrap("目标范围", entity));
  panel.append($("h2", "", "确认执行范围"), $("p", "muted", scope.ambiguity_prompt), controls, rebuild, modelList,
    actions(confirm, dismiss), message);
  stage.element.addEventListener("change", () => reloadTargets(inventory));
  domain.element.addEventListener("change", () => {
    // Keep the user-selected blank option even when the suggestion was an existing domain.
    selectedDomain = domain.value;
    load();
  });
  panel.addEventListener("submit", async (event) => {
    event.preventDefault();
    confirm.disabled = true;
    message.textContent = "";
    const form = new FormData(panel);
    const [kind, ...parts] = entity.value.split(":");
    const target = parts.join(":");
    try {
      await apply({
        contract_id: scope.contract_id, target_stage: stage.value, domain_id: domain.value || null,
        task_id: kind === "task" ? target : null, process_node_id: kind === "node" ? target : null,
        confirm_rebuild: !rebuild.hidden && form.has("confirm_rebuild"),
        confirm_model_send: !modelList.hidden && form.has("confirm_model_send"), profile_ids: form.getAll("profile_id"),
      });
    } catch (error) { message.textContent = error.message; confirm.disabled = false; }
  });
  load();
  return panel;
}

export function scopeReceipts(result, openDetails, agentSummary) {
  const details = $("details", "fold scope-receipt");
  details.id = "receipt-" + result.contract_id;
  details.open = openDetails.has(details.id);
  const summary = [stageLabel(result.stage), result.changes.length ? result.changes.length + " 项改动" : "无数据改动"];
  if (result.stale.length) summary.push(result.stale.length + " 项待同步");
  const rejections = result.rejections || [];
  const rejectionCategory = ({ category, reason }) =>
    reason === "已达到本阶段的工具调用上限" ? "tool_budget" : category;
  const categories = rejections.map(rejectionCategory);
  const boundaries = categories.filter((category) => category === "scope_guard").length;
  const sources = categories.filter((category) => category?.startsWith("source_")).length;
  const budget = categories.filter((category) => category === "tool_budget").length;
  if (boundaries) summary.push(boundaries + " 次权限拦截");
  if (sources) summary.push(sources + " 次信源受阻");
  if (budget) summary.push("工具调用额度已用完");
  const others = result.rejected - boundaries - sources - budget;
  if (others > 0) summary.push(others + " 次请求未通过");
  details.append($("summary", "", summary.join(" · ")));
  const list = $("ul");
  for (const { tool, entity_id, detail } of result.changes) {
    const row = $("li", "", detail);
    if (entity_id) row.append(" ", $("code", "meta", entity_id));
    if (tool === "export_dataset") row.append(" ", href("下载", "/api/workbench/scope-exports/" + entity_id, "button ghost", "download"));
    list.append(row);
  }
  for (const { entity_id, reason } of result.stale) list.append($("li", "", "待同步 · " + entity_id + " · " + reason));
  for (const note of result.suggestions) list.append($("li", "", "建议 · " + note));
  if (list.children.length) details.append(list);
  if (rejections.length) {
    const reasons = $("ul", "scope-rejections");
    const categoryLabels = { scope_guard: "阶段权限", source_policy: "原文访问策略",
      source_rate_limit: "信源限流", source_error: "信源错误", tool_budget: "工具额度", validation: "校验未通过" };
    for (const entry of rejections) {
      const row = $("li");
      const name = $("div", "scope-rejection-name");
      name.append($("code", "mono", entry.tool));
      const category = categoryLabels[rejectionCategory(entry)];
      if (category) name.append($("span", "meta", category));
      row.append(name, $("p", "", entry.reason || "请求未通过"));
      const parameters = Object.entries(entry.parameters || {});
      if (parameters.length) row.append($("p", "mono meta", parameters.map(([key, value]) => key + " = " + value).join(" · ")));
      reasons.append(row);
    }
    details.append(reasons);
    if (rejections.every(({ parameters }) => !Object.keys(parameters || {}).length)) {
      details.append($("p", "meta", "这批记录未保存调用参数。"));
    }
  }
  if (agentSummary) details.append(agentSummary);
  details.append($("p", "meta", "其余阶段保持只读 · " + result.read_only_stages.map(stageNumber).join("、")));
  return details;
}

export function staleTag(ctx, row, stage, target) {
  if (!row.is_stale) return null;
  const trigger = button("上游已更新 · 待同步", () => {
    const prompt = stage === "STAGE_6_EVAL_EXPORT" ? "请对题目「" + row.title + "」重新做模型盲测" :
      "请同步「" + row.title + "」的" + stageNames[stage];
    ctx.startFrom(prompt + "。依据：" + (row.stale_reason || "上游记录已变化"), { stage, ...target });
  }, "scope-stale");
  trigger.title = row.stale_reason || "同步这条记录";
  return trigger;
}

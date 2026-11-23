import { $, actions, button, stateBadge } from "./workbench-ui.js";

const labels = {
  running: "进行中", completed: "已完成", failed: "已中断", needs_review: "待复核",
  awaiting_scope: "待确认", paused: "已暂停", cancelled: "已中断", interrupted: "已中断",
};
const toolKinds = new Set(["tool", "commandExecution", "fileChange"]);
const diagnosticFields = [
  ["code", "错误码"], ["method", "协议调用"], ["exit_code", "退出码"], ["http_status", "HTTP 状态"],
  ["turn_status", "执行轮状态"], ["detail", "原始信息"], ["stderr", "stderr"], ["gateway", "网关"],
];

export function updateStageClock(node) {
  const start = Date.parse(node.dateTime);
  const end = node.dataset.end ? Date.parse(node.dataset.end) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return;
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  node.textContent = (node.dataset.estimated ? "约 " : "") +
    (seconds < 60 ? seconds + " 秒" : Math.floor(seconds / 60) + " 分 " + seconds % 60 + " 秒");
}

function renderEvent(event, id, openDetails) {
  const label = event.display_label || event.label || (event.kind === "reasoning" ? "推理摘要" : "工具调用");
  const duration = Number.isFinite(event.duration_ms) ? event.duration_ms + " 毫秒" : "";
  const state = event.kind === "compaction"
    ? { completed: "已压缩", failed: "压缩失败" }[event.status]
    : { completed: "已返回", failed: "调用失败" }[event.status] || labels[event.status];
  const description = [state, duration].filter(Boolean).join(" · ");
  const header = $("span", "stage-event-head");
  header.append($("span", "", label));
  if (description) header.append($("span", "stage-event-status", description));
  if (!event.preview?.trim()) {
    if (event.kind === "reasoning") return null;
    return header;
  }
  const entry = $("details", "stage-event");
  entry.id = id;
  entry.open = openDetails.has(id);
  const trigger = $("summary");
  trigger.append(header);
  entry.append(trigger, $("pre", "", event.preview));
  return entry;
}

function rolloutRows(events, key, openDetails) {
  const agents = new Map(events.filter((entry) => entry.kind === "subagent").map((entry) => [entry.subagent_id, entry]));
  const groups = new Map();
  const rows = [];
  function group(id) {
    if (groups.has(id)) return groups.get(id);
    const agent = agents.get(id);
    const container = $("div", "stage-child");
    groups.set(id, container);
    const title = $("div", "stage-child-head");
    title.append($("span", "", "子代理 " + (agent?.path || id.slice(0, 8))));
    if (agent?.status) title.append(stateBadge(agent.status));
    container.append(title);
    const parent = agent?.parent;
    if (parent && parent !== id && agents.has(parent) && !groups.has(parent)) {
      group(parent).append(container);
    } else if (parent && groups.has(parent) && !container.contains(groups.get(parent))) {
      groups.get(parent).append(container);
    } else rows.push(container);
    return container;
  }
  events.forEach((event, index) => {
    const actor = event.subagent_id;
    if (event.kind === "subagent") {
      if (actor) group(actor);
    } else if (toolKinds.has(event.kind) || ["reasoning", "compaction"].includes(event.kind)) {
      const row = renderEvent(event, key + "-event-" + index, openDetails);
      if (row && actor) group(actor).append(row);
      else if (row) rows.push(row);
    }
  });
  return rows;
}

function renderFailure(stage, options, key) {
  const error = stage.error || {};
  const box = $("div", "stage-error");
  const heading = $("p", "stage-error-message", error.message || stage.outcome || "本阶段执行已中断");
  heading.setAttribute("role", "alert");
  box.append(heading);
  const fields = diagnosticFields.flatMap(([field, caption]) =>
    error[field] !== null && error[field] !== undefined && error[field] !== "" ? [caption + " " + error[field]] : []);
  if (fields.length) {
    const log = $("details", "stage-diagnostic");
    log.id = key + "-diagnostic";
    log.open = options.openDetails.has(log.id);
    log.append($("summary", "", "查看原始诊断日志"), $("pre", "", fields.join("\n")));
    box.append(log);
  }
  if (options.retry && error.retryable) {
    const feedback = $("p", "inline-error");
    feedback.hidden = true;
    feedback.setAttribute("role", "alert");
    const retry = button("重试本阶段", async () => {
      retry.disabled = swap.disabled = true;
      feedback.hidden = true;
      try {
        await options.retry();
      } catch (failure) {
        feedback.textContent = failure.message;
        feedback.hidden = false;
        retry.disabled = swap.disabled = false;
      }
    }, "size-sm");
    const swap = options.unlock
      ? button("解锁后续阶段并重试", options.unlock, "ghost size-sm")
      : button("切换模型重试", options.chooseModel, "ghost size-sm");
    box.append(actions(retry, swap), feedback);
  }
  return box;
}

export function stageCard(stage, options) {
  const { prefix, expanded, openDetails, agentFilter, live } = options;
  const key = prefix + "-" + stage.id;
  const events = (stage.events || []).filter((entry) => !agentFilter || entry.subagent_id === agentFilter);
  if (agentFilter && !events.length) return null;
  const card = $("section", "stage-item");
  card.dataset.stage = stage.stage_key;
  card.dataset.status = stage.status;
  const header = $("div", "stage-header");
  const dot = $("span", "stage-dot");
  dot.setAttribute("aria-hidden", "true");
  const name = $("h3", "stage-name", stage.stage_label);
  const status = $("div", "stage-meta");
  status.append($("span", "", labels[stage.status] || ""));
  if (stage.started_at && (live || stage.finished_at)) {
    const time = $("time", "stage-clock");
    time.dateTime = stage.started_at;
    if (stage.finished_at) time.dataset.end = stage.finished_at;
    if (stage.timing_estimated) time.dataset.estimated = "true";
    updateStageClock(time);
    status.append(time);
  }
  header.append(dot, name, status);
  card.append(header);
  const count = events.filter((event) => toolKinds.has(event.kind)).length;
  const rows = rolloutRows(events, key, openDetails);
  if (rows.length) {
    const panel = $("div", "stage-rollout");
    panel.id = key + "-activity";
    panel.hidden = !expanded.has(key);
    panel.tabIndex = 0;
    panel.setAttribute("aria-label", stage.stage_label + "过程细节");
    panel.append(...rows);
    const toggle = button(count ? "过程细节 (" + count + ")" : "过程细节", () => {
      panel.hidden = !panel.hidden;
      if (panel.hidden) expanded.delete(key);
      else expanded.add(key);
      const summary = card.querySelector(".stage-summary-preview");
      if (summary) summary.hidden = !panel.hidden;
      toggle.setAttribute("aria-expanded", String(!panel.hidden));
    }, "ghost size-sm stage-toggle", "chevron");
    toggle.setAttribute("aria-controls", panel.id);
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
    header.append(toggle);
    card.append(panel);
  }
  if (!agentFilter) {
    const outcome = live ? options.approval ? "等待权限审批" : stage.activity?.label : stage.outcome;
    const conclusion = $("div", "stage-conclusion");
    if (outcome && stage.status !== "failed") conclusion.append($("p", "stage-outcome", outcome));
    if (live && stage.activity?.detail) conclusion.append($("p", "stage-outcome", stage.activity.detail));
    if (live && stage.activity?.state === "summary") {
      const summary = events.findLast(({ kind, agent_kind }) => kind === "reasoning" && agent_kind === "main")?.preview;
      if (summary) {
        const preview = $("p", "stage-outcome stage-summary-preview",
          summary.length <= 180 ? summary : summary.slice(0, 180) + "…");
        preview.hidden = expanded.has(key);
        conclusion.append(preview);
      }
    }
    if (options.content) conclusion.append(...options.content);
    if (conclusion.childElementCount) card.insertBefore(conclusion, card.querySelector(".stage-rollout"));
    if (stage.status === "failed") card.append(renderFailure(stage, options, key));
  }
  return card;
}

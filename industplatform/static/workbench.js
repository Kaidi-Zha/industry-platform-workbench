import {
  $, actions, button, column, empty, field, get, heading, href, icon, iconButton,
  popoverSelect, post, request, stateBadge, stateTone,
} from "./workbench-ui.js";
import {
  apiGateway, codexSubscription, defaultNativeModel, editProvider, effortNames, enabledNativeModels, modelPicker, profileUsable,
} from "./workbench-models.js";
import { clarifyScope, executionRangeMenu, pausePace, sandboxMenu, scopePill, scopeReceipts, stageNames } from "./workbench-scope.js";
import { stageCard, updateStageClock } from "./workbench-timeline.js";
import { workspacePath, workspaceRoute } from "./workbench-router.js";
import { settingsSectionView } from "./workbench-settings.js";
import { initializeCollaboration, openSync } from "./workbench-collaboration.js";
import { benchmarkFormView, runDetailView } from "./workbench-benchmarks.js";
import { exportView, graphView, schemaView, settingsView, sourcesView, tasksView, verifierView } from "./workbench-views.js";

import { assetsView, domainsView, timeLabel } from "./workbench-overview.js";

const steps = [
  ["scope", "领域任务与对话", "terminal"], ["graph", "工艺图谱", "graph"], ["sources", "信源与文献", "book"],
  ["tasks", "构题工坊", "task"], ["verifiers", "验证器工程", "shield"], ["export", "评测与导出", "chart"],
];
const globalSteps = [
  ["scope", "研究指挥台", "terminal"], ["domains", "领域资料库", "book"], ["assets", "全局数据汇总", "archive"],
];
const views = { graph: graphView, sources: sourcesView, domains: domainsView, assets: assetsView, tasks: tasksView,
  verifiers: verifierView, export: exportView, settings: settingsView };
const terminalStatus = new Set(["completed", "needs_review", "failed", "cancelled"]);
const activeStatus = new Set(["queued", "running", "pausing", "cancelling"]);
const media = matchMedia("(max-width: 760px)");
const darkMode = matchMedia("(prefers-color-scheme: dark)");
const drafts = new Map();
const viewStates = new Map();
const seenApprovals = new Set();
const busyApprovals = new Set();
const expandedStages = new Set();
let markdown;
let markdownLoading;

function loadMarkdown() {
  return markdownLoading ||= import("./vendor/markdown-it-15.0.3.mjs").then(({ default: MarkdownIt }) => {
    const parser = new MarkdownIt({ html: false, breaks: true, linkify: true });
    const validateLink = parser.validateLink;
    parser.validateLink = (target) => {
      if (!validateLink(target)) return false;
      try {
        const url = new URL(target, document.baseURI);
        return ["https:", "http:", "mailto:"].includes(url.protocol) && !url.username && !url.password;
      } catch { return false; }
    };
    parser.renderer.rules.table_open = (tokens, index, options, _env, renderer) =>
      '<div class="markdown-table">' + renderer.renderToken(tokens, index, options);
    parser.renderer.rules.table_close = (tokens, index, options, _env, renderer) =>
      renderer.renderToken(tokens, index, options) + '</div>\n';
    return markdown = parser;
  });
}
const shortcuts = [
  ["梳理企业与工艺", "梳理当前领域代表性企业、岗位与生产工序。对每条上下游关系标明交付物及可核验出处。"],
  ["从种子文献补查", "以这些种子文献为入口，追查原始文件、关联规范、可公开获取的实际数据及使用许可。\n种子文献："],
  ["从工程规范构题", "从已有规范和真实样本提炼一道工程题，明确岗位、交付物、工程判据和下游验收。\n规范或资料："],
];

let data;
let locationState;
let config = { profiles: [], routes: {}, settings: {}, native: {} };
let subscriptionProbe;
let configReady = false;
let currentChat = null;
let threadView = null;
let editAction = null;
let drawerTab = "agents";
let eventAgent = null;
let workspaceEpoch = 0;
let workspaceLoading = 0;
let lastWorkspaceRead = 0;
let chatEpoch = 0;
let inspectorKey = "";
let taskHistory = null;
let refreshView = null;
const historyExpanded = { desktop: true, mobile: false };
const historyMode = () => media.matches ? "mobile" : "desktop";
let sidebarPinned = !media.matches;
let submitting = false;

function locationFor(fragment, domain = locationState?.domain, params) {
  return workspacePath(fragment, domain, params);
}

function workingDomain(thread = locationState?.thread) {
  return data?.domain || (thread ? data?.thread_domain : null);
}

function readLocation() {
  return workspaceRoute(new URL(window.location.href));
}

function followLocation() {
  const route = readLocation();
  const original = location.pathname + location.search + location.hash;
  if (original !== route.path) history.replaceState(null, "", route.path);
  return route;
}

function chooseSidebar(open) {
  if (media.matches && open && get("shell").dataset.drawer === "open") setDrawer(false);
  get("shell").dataset.sidebar = open ? "open" : "closed";
  get("sidebar-toggle").setAttribute("aria-expanded", String(open));
  get("sidebar-backdrop").hidden = !(media.matches && open);
}

function setDrawer(open, focus = false) {
  if (open && media.matches) chooseSidebar(false);
  const inspector = get("inspector");
  if (!open && inspector.contains(document.activeElement)) get("inspector-toggle").focus();
  get("shell").dataset.drawer = open ? "open" : "closed";
  inspector.inert = !open;
  inspector.setAttribute("aria-hidden", String(!open));
  get("inspector-toggle").setAttribute("aria-expanded", String(open));
  get("drawer-backdrop").hidden = !open;
  if (open) {
    renderInspector();
    if (focus) get("inspector-close").focus();
  }
}

function startFrom(text, target = null, { exhaustive = false } = {}) {
  const value = draftFor(null);
  value.prompt = text;
  value.scopeTarget = target;
  value.stageQueue = target ? [target.stage] : [];
  value.executionPace = pausePace;
  value.collectionStop = exhaustive ? "exhaustive" : "task";
  navigate(locationFor("scope"));
  requestAnimationFrame(() => {
    const prompt = get("composer-prompt");
    prompt?.focus();
    prompt?.setSelectionRange(prompt.value.length, prompt.value.length);
  });
}

function draftFor(thread = locationState?.thread) {
  const key = thread || [data?.domain?.id, "new"].join(":");
  if (!drafts.has(key)) {
    const settings = config.settings;
    drafts.set(key, { prompt: "", mode: settings.default_execution_mode || apiGateway,
      profile: settings.default_model === "@codex" ? "" : settings.default_model || "",
      codexModel: settings.default_codex_model || "", effort: null,
      sandbox: "workspace-write", approval: settings.approval_mode || "never",
      collectionStop: "task", stageQueue: [], executionPace: pausePace, userOptions: false });
  }
  return drafts.get(key);
}

function domainViewState() {
  const id = data.domain?.id;
  if (!viewStates.has(id)) viewStates.set(id, {});
  return viewStates.get(id);
}

function statusDot(tone, label) {
  const dot = $("span", "dot");
  dot.dataset.tone = tone;
  dot.setAttribute("role", "img");
  dot.setAttribute("aria-label", label);
  dot.title = label;
  return dot;
}

function renderSidebar() {
  const domain = data.domain;
  get("workspace-brand").hidden = Boolean(domain);
  get("domain-back").hidden = !domain;
  const name = get("sidebar-domain-name");
  name.hidden = !domain;
  name.textContent = domain?.name || "";
  name.title = domain?.name || "";
  const progress = [Boolean(data.domain), Boolean(data.nodes.length),
    data.metrics.verified_references > 0 || data.frontier.length > 0, data.metrics.benchmark_tasks > 0,
    data.metrics.verifier_specs > 0, data.metrics.eval_runs > 0];
  const contractStages = Object.keys(stageNames);
  const approvals = pendingApprovals();
  const stages = steps.map(([id], index) => {
    const stage = contractStages[index];
    if (approvals.some(({ turn }) => (turn.execution_scope?.target_stage || contractStages[0]) === stage)) return "needs_review";
    if (id === "scope" && data.monitor?.some(({ turn }) => !turn.execution_scope && activeStatus.has(turn.status)) ||
        id === "sources" && data.nodes.some(({ crawl_status, channels }) => crawl_status === "running" ||
          channels?.some((channel) => channel.leased))) {
      return "running";
    }
    const state = data.stage_states?.[stage];
    if (state) return state;
    if (id === "scope" && progress[index]) return "completed";
    return progress[index] ? "draft" : "";
  });
  const nav = get("workflow-nav");
  const entries = domain ? steps : globalSteps;
  const navKey = JSON.stringify([domain?.id, locationState.page, locationState.run, stages]);
  if (nav.dataset.key !== navKey) {
    nav.dataset.key = navKey;
    nav.replaceChildren(...entries.map(([id, title, symbol], index) => {
      const entry = href(title, locationFor(id), "", symbol);
      if (id === (locationState.level === "global" && locationState.run ? "assets" : locationState.page)) {
        entry.setAttribute("aria-current", "page");
      }
      const status = domain && stages[index];
      if (status) entry.append(statusDot(status === "running" ? "workflow-active" : stateTone(status), stateBadge(status).textContent));
      return entry;
    }));
  }
  const settings = get("settings-link");
  if (locationState.page === "settings") settings.setAttribute("aria-current", "page");
  else settings.removeAttribute("aria-current");
  const page = locationState.run && !domain ? "assets" : locationState.page;
  const title = entries.find(([id]) => id === page)?.[1] || "网关与设置";
  const crumbs = get("breadcrumb");
  const trail = [];
  if (domain) {
    trail.push(href("领域资料库", locationFor("domains", null), "breadcrumb-library"),
      href(domain.name, locationFor("scope", domain.id), "truncate breadcrumb-domain"));
    trail[1].title = domain.name;
  }
  trail.push(href(title, locationFor(page, domain?.id || null), "breadcrumb-page"));
  trail.at(-1).setAttribute("aria-current", "location");
  const crumbKey = JSON.stringify([title, domain?.id, domain?.name]);
  if (crumbs.dataset.key !== crumbKey) {
    crumbs.dataset.key = crumbKey;
    crumbs.replaceChildren(...trail.flatMap((entry, index) => index ? [$("span", "breadcrumb-divider", "/"), entry] : [entry]));
  }
  get("inspector-scope").textContent = workingDomain()?.name || "全平台";
  renderTaskHistory();
}

function renderTaskHistory() {
  if (!taskHistory?.isConnected) return;
  const pending = new Set(pendingApprovals().map(({ thread }) => thread.id));
  const records = data.threads;
  const domainExists = new Set(data.domains.map(({ name }) => name));
  const key = JSON.stringify([locationState.thread, records, [...pending],
    records.map(({ domain }) => domainExists.has(domain))]);
  if (taskHistory.dataset.key === key) return;
  taskHistory.dataset.key = key;
  const list = taskHistory.querySelector(".task-history-list");
  const retainedFocus = document.activeElement?.dataset.threadId;
  list.replaceChildren(...records.map((record) => {
    const link = href("", locationFor("scope/" + record.id), "task-history-item");
    link.dataset.threadId = record.id;
    link.title = record.title;
    if (record.id === locationState.thread) link.setAttribute("aria-current", "page");
    const status = pending.has(record.id) ? $("span", "badge warning", "待审批")
      : record.status === "paused" ? $("span", "badge warning", "已中断可续跑")
      : record.can_resume ? $("span", "badge warning", "待续跑") : stateBadge(record.status);
    const stages = Object.keys(stageNames);
    const current = stages.indexOf(record.stage) + 1;
    const initial = stages.indexOf(record.initial_stage) + 1;
    const meta = $("div", "task-history-meta");
    meta.append(status);
    if (initial && current && initial !== current) {
      const progression = $("span", "meta", "阶段 " + initial + " → " + current);
      progression.title = "起始阶段 " + initial + "，最新阶段 " + current;
      meta.append(progression);
    } else if (current || initial) meta.append($("span", "meta", "阶段 " + (current || initial)));
    if (record.updated_at) meta.append(timeLabel(record.updated_at));
    const domain = record.domain ? record.domain + (domainExists.has(record.domain) ? "" : "（历史）")
      : record.route === "local-import" ? (record.library_domains.join("、") || "本地资料导入") : "领域待确认";
    link.append($("span", "task-history-title", record.title), $("span", "truncate meta", domain), meta);
    return link;
  }));
  if (!records.length) list.append($("p", "meta", "新任务和执行记录将在这里显示。"));
  if (retainedFocus) [...list.children].find((row) => row.dataset.threadId === retainedFocus)?.focus({ preventScroll: true });
}

function wrapResearch(content) {
  const wrapper = $("div", "research-layout");
  taskHistory = $("aside", "task-history");
  taskHistory.setAttribute("aria-label", "历史任务");
  const newTask = href("新建研究任务", locationFor("scope"), "button ghost", "plus");
  newTask.classList.add("new-research");
  newTask.setAttribute("aria-label", "新建研究任务");
  newTask.title = "新建研究任务";
  const list = $("div", "task-history-list");
  list.id = "task-history-list";
  const toggle = button("", () => {
    const mode = historyMode();
    historyExpanded[mode] = !historyExpanded[mode];
    syncHistory();
  }, "ghost history-toggle", "history");
  toggle.append($("span", "", "历史任务"));
  toggle.setAttribute("aria-label", "历史任务");
  toggle.setAttribute("aria-controls", list.id);
  const header = $("header", "task-history-header");
  header.append(toggle, newTask);
  taskHistory.append(header, list);
  wrapper.append(taskHistory, content);
  syncHistory(wrapper);
  return wrapper;
}

function syncHistory(wrapper = taskHistory?.parentElement) {
  if (!wrapper) return;
  const expanded = historyExpanded[historyMode()];
  wrapper.dataset.history = expanded ? "open" : "closed";
  const toggle = taskHistory.querySelector(".history-toggle");
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.title = expanded ? "收起历史任务" : "展开历史任务";
  taskHistory.querySelector(".task-history-list").hidden = !expanded;
}
function liveAgents() {
  return (data?.monitor || []).flatMap(({ thread, turn }) => {
    if (turn.status !== "running") return [];
    const stage = turn.result?.stage_timeline?.at(-1);
    if (stage?.status === "running") {
      return stage.events.filter(({ kind }) => kind === "subagent")
        .map((agent) => ({ ...agent, id: agent.subagent_id, thread, stage: stage.stage_label }));
    }
    return Object.entries(turn.result?.subagents || {}).map(([id, value]) => ({
      ...(typeof value === "string" ? { status: value } : value), id, thread,
    }));
  });
}

function pendingApprovals() {
  return (data?.monitor || []).filter(({ turn }) => turn.pending_approval);
}

function updateActivityTime(node) {
  const seconds = Math.floor((Date.now() - Date.parse(node.dateTime)) / 1000);
  node.textContent = !Number.isFinite(seconds) ? "" : seconds < 1 ? "刚刚" :
    seconds < 60 ? seconds + " 秒" : Math.floor(seconds / 60) + " 分 " + (seconds % 60) + " 秒";
}

function runActivity(turn) {
  if (!activeStatus.has(turn.status)) return null;
  const result = turn.result || {};
  const activity = turn.status === "running" && result.harness_activity;
  const phase = activity?.phase || turn.steps?.find(({ status }) => status === "running")?.label;
  const label = turn.pending_approval ? "等待权限审批" :
    ({ queued: "正在排队", pausing: "正在暂停本轮", cancelling: "正在中断本轮" }[turn.status]
      || activity?.label || "正在准备执行");
  const row = $("div", "run-activity");
  row.setAttribute("role", "status");
  row.dataset.tone = turn.pending_approval || activity?.kind === "retrying" ? "warning" : "active";
  const headline = $("div", "run-activity-line");
  const dot = $("span", "run-activity-dot");
  dot.setAttribute("aria-hidden", "true");
  headline.append(dot, $("span", "run-activity-label", [phase, label].filter(Boolean).join(" · ")));
  const elapsed = $("time", "run-activity-time");
  elapsed.dateTime = activity?.since || turn.updated_at || turn.created_at || "";
  elapsed.setAttribute("aria-hidden", "true");
  elapsed.title = "当前状态持续时间";
  updateActivityTime(elapsed);
  headline.append(elapsed);
  row.append(headline);
  if (activity?.detail && !turn.pending_approval) row.append($("p", "run-activity-detail", activity.detail));
  return row;
}

function turnBadge(turn) {
  if (turn.pending_approval) return stateBadge("awaiting_approval");
  const failed = !activeStatus.has(turn.status) && turn.result?.stage_timeline?.at(-1)?.status === "failed";
  return failed ? $("span", "badge danger", "已中断") : stateBadge(turn.status);
}

async function retryStage(turn, stage, model = {}) {
  await post("/api/runs/" + turn.id + "/scope", { action: "retry_stage", attempt_id: stage.id, ...model });
  await reload();
}

function unlockLaterStages(turn, stage) {
  const queue = turn.execution_scope.stage_queue;
  const current = turn.execution_scope.target_stage;
  const suggested = new Set(stage.error.suggested_stages);
  const stages = Object.keys(stageNames);
  const later = stages.slice(stages.indexOf(current) + 1);
  const choices = later.map((key) => {
    const label = $("label", "stage-unlock-choice");
    const input = $("input");
    input.type = "checkbox";
    input.name = key;
    input.checked = queue.includes(key) || suggested.has(key);
    input.disabled = queue.includes(key);
    label.append(input, $("span", "", "阶段 " + (stages.indexOf(key) + 1) + " · " + stageNames[key]));
    return label;
  });
  editor("选择后续阶段", [
    $("p", "muted", "先重试" + stageNames[current] + "。完成后由你确认何时进入下一阶段。"),
    ...choices,
  ], "确认并重试", async (values) => {
    const selected = later.filter((key) => Object.hasOwn(values, key) || queue.includes(key) && suggested.has(key));
    if (!selected.length) throw new Error("请选择要继续的阶段。");
    await retryStage(turn, stage, { later_stages: selected });
  });
}

function renderStatus() {
  const approvals = pendingApprovals();
  const agents = liveAgents().filter(({ status }) => ["running", "pendingInit"].includes(status));
  const notice = get("approval-count");
  notice.hidden = !approvals.length;
  notice.textContent = approvals.length || "";
  get("health-dot").dataset.tone = agents.length || data.active_count ? "active" : "success";
  const running = (data.monitor || []).filter(({ turn }) => activeStatus.has(turn.status));
  const apiRunning = running.some(({ turn }) => turn.execution_mode === apiGateway);
  const connected = running.some(({ turn }) => turn.result?.stage_timeline?.at(-1)?.thread_id ||
    turn.result?.native_thread_id || turn.result?.rollout_threads?.length);
  const importing = data.threads.some(({ route, status }) => route === "local-import" && activeStatus.has(status));
  get("health-label").textContent = connected && apiRunning ? "研究任务运行中" : connected ? "App Server 已连接"
    : apiRunning ? "API 模型网关运行中" : importing ? "正在整理资料"
    : data.active_count ? "正在准备执行" : "工作台就绪";
  const tokens = (data.monitor || []).reduce((total, { turn }) => total + (turn.result?.tokens || 0), 0);
  const counters = [];
  if (agents.length) counters.push($("span", "", "并发子代理 " + agents.length));
  if (tokens) counters.push($("span", "", tokens.toLocaleString("zh-CN") + " Token"));
  get("header-metrics").replaceChildren(...counters);
  const fresh = approvals.filter(({ turn }) => !seenApprovals.has(turn.pending_approval.id));
  if (fresh.length && !get("editor-dialog").open) {
    for (const { turn } of fresh) seenApprovals.add(turn.pending_approval.id);
    drawerTab = "approvals";
    setDrawer(true);
  } else if (get("shell").dataset.drawer === "open") renderInspector();
}

function renderInspector() {
  for (const tab of get("inspector-tabs").querySelectorAll("button")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.tab === drawerTab));
  }
  const content = get("inspector-content");
  const records = data?.monitor || [];
  const history = locationState?.thread === currentChat?.id ? currentChat?.turns?.at(-1) : null;
  const key = JSON.stringify([drawerTab, eventAgent, data?.domain?.id, records, history]);
  if (key === inspectorKey) return;
  inspectorKey = key;
  if (drawerTab === "agents") {
    const agents = liveAgents();
    if (!agents.length) {
      content.replaceChildren(empty("graph", "当前没有活动子代理", "子代理开始执行后会出现在这里。"));
      return;
    }
    const groups = new Map();
    for (const { id, status, thread, model, effort, path, parent, stage } of agents) {
      if (!groups.has(thread.id)) {
        const group = $("section", "agent-tree");
        group.append(href(thread.title, locationFor("scope/" + thread.id, locationState.level === "domain" ? locationState.domain : null), "button"));
        groups.set(thread.id, group);
      }
      const card = $("article", "agent-card");
      card.append($("h3", "mono", path || id.slice(0, 12)));
      if (stage) card.append($("p", "meta", stage));
      if (model || effort) card.append($("p", "", [model, effortNames[effort] || effort].filter(Boolean).join(" · ")));
      if (parent && agents.some((agent) => agent.id === parent)) {
        card.append($("p", "meta mono", "上级代理 " + parent.slice(0, 12)));
      }
      card.append(actions(stateBadge(status), button("查看日志", () => {
        eventAgent = id;
        drawerTab = "events";
        renderInspector();
      })));
      groups.get(thread.id).append(card);
    }
    content.replaceChildren(...groups.values());
    return;
  }
  if (drawerTab === "events") {
    const open = new Set(Array.from(content.querySelectorAll("details[open]")).map((row) => row.id));
    const clearFilter = eventAgent && button("查看全部事件", () => { eventAgent = null; renderInspector(); });
    const monitored = records.length ? records : history ? [{ thread: { title: history.prompt }, turn: history }] : [];
    const sections = [];
    for (const { thread, turn } of monitored) {
      const result = turn.result || {};
      const group = $("section", "inspector-activity");
      const title = $("h3", "inspector-task-title");
      titleThread(title, thread.title);
      group.append(title);
      const timeline = result.stage_timeline || [];
      for (const stage of timeline) {
        const entry = stageCard(stage, { prefix: "monitor-" + turn.id, expanded: expandedStages, openDetails: open,
          agentFilter: eventAgent, live: stage.status === "running" && turn.status === "running", approval: turn.pending_approval });
        if (entry) group.append(entry);
      }
      if (!timeline.length && !eventAgent) {
        const activity = runActivity(turn);
        if (activity) group.append(activity);
        if (turn.error) group.append($("p", "inline-error", turn.error));
        for (const event of result.harness_items || []) {
          if (!event.preview?.trim()) continue;
          const detail = $("details", "stage-event");
          detail.append($("summary", "", event.display_label || event.label), $("pre", "", event.preview));
          group.append(detail);
        }
      }
      if (group.childElementCount > 1) sections.push(group);
    }
    content.replaceChildren(...(clearFilter ? [clearFilter] : []), ...(sections.length ? sections :
      [$("p", "muted", eventAgent ? "该子代理暂无运行事件。" : "等待执行事件。")]));
    return;
  }
  const approvals = pendingApprovals();
  if (!approvals.length) {
    content.replaceChildren(empty("shield", "暂无待审批操作", "需要人工确认的请求会在这里出现。"));
    return;
  }
  content.replaceChildren(...approvals.map(({ thread, turn }) => {
    const pending = turn.pending_approval;
    const card = $("article", "approval-card");
    const feedback = $("p", "inline-error");
    feedback.setAttribute("role", "alert");
    const controls = actions();
    const choices = [["单次允许", "accept"], ...(pending.network_scope ? [["本领域始终允许", "domain"]] : [])];
    for (const [label, decision] of choices) {
      const control = button(label, async () => {
        busyApprovals.add(pending.id);
        for (const child of controls.children) child.disabled = true;
        try {
          await post("/api/runs/" + turn.id + "/approval", { id: pending.id, decision });
          await reload();
        } catch (error) {
          feedback.textContent = error.message;
        } finally {
          busyApprovals.delete(pending.id);
          for (const child of controls.children) child.disabled = child.dataset.disabled === "true";
        }
      }, decision === "accept" ? "primary" : "");
      control.dataset.disabled = String(!pending.can_approve);
      control.disabled = busyApprovals.has(pending.id) || control.dataset.disabled === "true";
      controls.append(control);
    }
    controls.append(button("拒绝并附带纠正意见", () => editor("拒绝权限请求", [
      field("correction", "纠正意见（可选）", { as: "textarea", limit: 2000 }),
    ], "拒绝", async ({ correction }) => {
      await post("/api/runs/" + turn.id + "/approval", { id: pending.id, decision: "decline", correction });
      await reload();
    })));
    card.append($("h3", "", pending.label), $("p", "meta", thread.title),
      actions($("span", "badge warning", pending.risk || "权限审批"),
        ...(pending.thread ? [$("code", "meta mono", pending.thread)] : [])));
    if (pending.reason) card.append($("p", "", pending.reason));
    if (pending.detail) card.append($("pre", "", pending.detail));
    card.append(controls, feedback);
    return card;
  }));
}

function editor(title, children, confirm, run, variant = "") {
  if (get("editor-dialog").open) return;
  editAction = run;
  get("dialog-title").textContent = title;
  get("dialog-fields").className = "dialog-fields " + variant;
  get("dialog-fields").replaceChildren(...children);
  get("dialog-submit").textContent = confirm;
  get("dialog-error").hidden = true;
  get("editor-dialog").showModal();
}

function manageDomain(action, domain) {
  const payload = { action, id: domain.id };
  const create = (title, pieces, confirm) => editor(title, pieces, confirm, async (values) => {
    await post("/api/workbench/domains", { ...payload, ...values,
      ...(values.target_id ? { target_id: Number(values.target_id) } : {}) });
    navigate(locationFor("domains", null));
    await reload(true);
  });
  if (action === "rename") {
    create("重命名领域", [field("name", "领域名称", { value: domain.name, required: true, limit: 100 })], "保存");
  } else if (action === "merge") {
    const options = data.domains.filter(({ id }) => id !== domain.id).map(({ id, name }) => [id, name]);
    create("合并重复领域库", [$("p", "muted", "将「" + domain.name + "」的研究记录移入所选领域。"),
      field("target_id", "保留的领域", { options, as: "select", required: true })], "确认合并");
  } else {
    create("删除领域资料库", [$("p", "muted", "从资料库移除「" + domain.name + "」。历史记录将保留。")], "移除资料库");
  }
}

function addDomain() {
  editor("新建工业领域", [field("name", "领域名称", { required: true, limit: 100 })], "创建领域", async ({ name }) => {
    const result = await post("/api/workbench/domains", { name });
    navigate(locationFor("scope", result.id));
  });
}

function editNode(node = null) {
  const id = data.domain.id;
  const choices = data.nodes.filter((candidate) => candidate.id !== node?.id);
  const upstream = new Set(data.edges.filter(({ downstream_id }) => downstream_id === node?.id).map((edge) => edge.upstream_id));
  const predecessors = $("fieldset", "graph-predecessors");
  predecessors.append($("legend", "", "上游工序（可多选）"));
  const list = $("div", "graph-predecessor-list");
  for (const candidate of choices) {
    const item = $("label");
    const input = $("input");
    input.type = "checkbox";
    input.value = candidate.id;
    input.checked = upstream.has(candidate.id);
    item.append(input, $("span", "", candidate.title));
    list.append(item);
  }
  predecessors.append(list);
  editor(node ? "编辑工艺节点" : "添加工艺节点", [
    field("title", "工序名称", { value: node?.title, required: true, limit: 100 }),
    field("flow_group", "流程分区（留空自动）", { value: node?.flow_group, limit: 60 }),
    field("occupations", "涉及工种（用顿号分隔）", { value: node?.occupations?.join("、"), limit: 240 }),
    ...(choices.length ? [predecessors] : []),
  ], "保存节点", async ({ title, flow_group, occupations }) => {
    const changes = node ? { action: "update", id: node.id } : {};
    const upstream_ids = [...list.querySelectorAll("input:checked")].map((input) => input.value);
    await post("/api/workbench/nodes", { domain_id: id, title, flow_group, occupations, upstream_ids, ...changes });
    await reload(true);
  });
}

function organizeNode(node, action) {
  const domain_id = data.domain.id;
  const endpoint = "/api/workbench/nodes";
  if (action === "unlink") {
    editor("解除工序关联", [$("p", "muted", "将「" + node.title + "」恢复为画布上的独立工序。")],
      "解除关联", async () => {
        await post(endpoint, { domain_id, id: node.id, action });
        await reload(true);
      });
    return;
  }
  const linked = new Set(data.node_links.map((link) => link.node_id));
  const suggested = new Set(data.node_suggestions?.[node.id] || []);
  const choices = data.nodes.filter(({ id }) => id !== node.id && !linked.has(id))
    .sort((a, b) => Number(suggested.has(b.id)) - Number(suggested.has(a.id)));
  if (!choices.length) return;
  editor("整理关联工序", [
    $("p", "muted", "将「" + node.title + "」收拢到指定主工序。原工序的文献与任务保留在关联详情。"),
    field("canonical_id", "画布主工序", { as: "select", required: true,
      options: [["", "请选择"], ...choices.map(({ id, title }) => [id, title])] }),
    field("reason", "关联依据", { as: "textarea", required: true, limit: 500 }),
  ], "保存关联", async ({ canonical_id, reason }) => {
    await post(endpoint, { domain_id, id: node.id, action, canonical_id, reason });
    navigate(locationFor("graph"));
    await reload(true);
  });
}

function removeNode(node) {
  const domain_id = data.domain.id;
  editor("删除工序", [$("p", "", "删除「" + node.title + "」及其连线。有关联证据的工序须先解除关联。")], "删除工序", async () => {
    await post("/api/workbench/nodes", { domain_id, id: node.id, action: "remove" });
    navigate(locationFor("graph"));
    await reload(true);
  });
}

function assessFrontier(row, refresh) {
  const domain_id = data.domain.id;
  editor("复核来源", [
    $("p", "muted", "保留原文链接和版本。填写同版本已核验文献 ID 以纳入统计，留空则按所填理由排除。"),
    field("reference_id", "已核验文献 ID（可选）", { limit: 64 }),
    field("note", "核验或排除依据", { as: "textarea", required: true, limit: 500 }),
  ], "保存复核", async ({ note, reference_id }) => {
    await post("/api/workbench/frontier", { domain_id, node_id: row.node_id, id: row.source_id,
      round: row.round_number, action: "assess", note, reference_id });
    await refresh();
  });
}

function controlsBusy(turn) {
  return turn && !terminalStatus.has(turn.status);
}

function composer(thread = null) {
  const value = draftFor(thread);
  if (!thread && configReady && !value.userOptions) {
    value.mode = config.settings.default_execution_mode;
    value.profile = config.settings.default_model === "@codex" ? "" : config.settings.default_model;
    value.codexModel = config.settings.default_codex_model;
  }
  const form = $("form", "composer");
  const input = $("textarea");
  input.id = "composer-prompt";
  input.placeholder = "输入研究目标，粘贴标准或网址…";
  input.setAttribute("aria-label", "工业领域研究目标");
  input.value = value.prompt;
  input.maxLength = 4000;
  const bar = $("div", "composer-controls");
  const pills = $("div", "composer-pills");
  const models = config.profiles.filter(profileUsable);
  if (configReady && !models.some(({ id }) => id === value.profile)) value.profile = models[0]?.id || "";
  const initialNative = defaultNativeModel(config.native, config.settings, value.codexModel);
  if (initialNative) value.codexModel = initialNative.id;
  const effortSelect = popoverSelect("思考等级", [], {
    prefix: "思考", symbol: "bolt", appearance: "ghost size-sm effort-trigger", preferAbove: true,
  });
  const selectedModel = () => value.mode === codexSubscription ?
    enabledNativeModels(config.native, config.settings).find(({ id }) => id === value.codexModel) :
    models.find(({ id }) => id === value.profile);
  const readyToSubmit = () => Boolean(selectedModel());
  const rangeMenu = executionRangeMenu(value, workingDomain(thread)?.id, () => planEditor(value, thread, error));
  const policyMenu = sandboxMenu(value);
  const modelSelect = modelPicker(config, value, (mode, id, chosen = true) => {
    const key = mode === codexSubscription ? "codexModel" : "profile";
    if (value.mode !== mode || (mode === apiGateway && value.profile !== id)) value.effort = null;
    value.mode = mode;
    value[key] = id;
    if (chosen) value.userOptions = true;
    renderEffort();
  }, () => navigate(locationFor("settings")), detectSubscription);
  function renderEffort() {
    const model = selectedModel();
    const levels = model?.reasoning_efforts?.filter(Boolean) || [];
    const nativeDefault = value.mode === codexSubscription && levels.includes(model?.default_effort) ? model.default_effort : null;
    if (model && !levels.includes(value.effort)) value.effort = nativeDefault;
    const options = levels.map((level) => [level, effortNames[level] || level]);
    if (!nativeDefault) options.unshift(["", "默认"]);
    effortSelect.setOptions(options, value.effort ?? "");
    effortSelect.disabled = !levels.length;
    policyMenu.update();
  }
  effortSelect.element.addEventListener("change", () => {
    value.effort = effortSelect.value || null;
    value.userOptions = true;
  });
  renderEffort();
  const error = $("p", "inline-error composer-error");
  error.setAttribute("role", "alert");
  error.hidden = true;
  form.selectRetryModel = (turn, stage) => modelSelect.openForRetry(async (mode, model) => {
    error.hidden = true;
    try {
      await retryStage(turn, stage, {
        execution_mode: mode,
        [mode === codexSubscription ? "subscription_model_id" : "model_profile_id"]: model.id,
        ...(value.effort ? { reasoning_effort: value.effort } : {}),
      });
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    }
  });
  const send = iconButton("提交指令", null, "up", "primary send");
  send.type = "submit";
  const interrupt = iconButton("中断本轮", async () => {
    const latest = currentChat?.id === thread ? currentChat.turns.at(-1) : null;
    if (latest?.status !== "running") return;
    interrupt.disabled = true;
    error.hidden = true;
    try {
      await post("/api/runs/" + latest.id + "/control", { action: "cancel" });
      await reload();
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      interrupt.disabled = false;
    }
  }, "stop", "ghost interrupt");
  interrupt.hidden = currentChat?.id !== thread || currentChat?.turns.at(-1)?.status !== "running";
  const suggestions = $("div", "mentions");
  const uploaded = $("div", "actions composer-files");
  const files = $("input");
  files.type = "file";
  files.multiple = true;
  files.accept = ".pdf,.step,.stp,.dxf,.stl,.iges,.igs";
  files.hidden = true;
  let uploading = false;
  const drawFiles = () => {
    uploaded.replaceChildren(...(value.attachments || []).map((file) => {
      const detach = button(file.name + " · 移除", () => {
        value.attachments = value.attachments.filter(({ id }) => id !== file.id);
        drawFiles();
      }, "ghost", "close");
      detach.title = file.name;
      return detach;
    }));
    uploaded.hidden = !uploaded.children.length;
  };
  const upload = async (selection) => {
    if (uploading || !selection.length) return;
    if (!workingDomain(thread)) {
      error.textContent = "可在领域建立后继续上传附件，或先手动建立领域。";
      error.hidden = false;
      return;
    }
    uploading = true;
    const domainID = workingDomain(thread).id;
    error.hidden = true;
    try {
      value.attachments ||= [];
      for (const file of selection) {
        if (value.attachments.length >= 4) throw new Error("每个任务可上传最多 4 个附件");
        const q = new URLSearchParams({ domain: domainID, name: file.name });
        const result = await request("/api/workbench/attachments?" + q, {
          method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
        });
        if (!value.attachments.some(({ id }) => id === result.id)) value.attachments.push(result);
        drawFiles();
      }
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
    } finally { uploading = false; files.value = ""; }
  };
  files.addEventListener("change", () => upload([...files.files]));
  form.addEventListener("dragover", (event) => {
    if (event.dataTransfer.types.includes("Files")) event.preventDefault();
  });
  form.addEventListener("drop", (event) => {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    upload([...event.dataTransfer.files]);
  });
  const addFile = button("", () => files.click(), "ghost size-sm attach", "plus");
  addFile.setAttribute("aria-label", "添加附件");
  addFile.title = "添加附件";
  addFile.append($("span", "", "附件"));
  drawFiles();
  suggestions.hidden = true;
  let suggestionIndex = 0;
  let available = [];
  function sizePrompt() {
    input.style.height = "auto";
    input.style.height = Math.min(240, input.scrollHeight) + "px";
  }
  const mention = () => /(?:^|\s)@([^\s@]*)$/.exec(input.value.slice(0, input.selectionStart));
  function updateMentions() {
    const match = mention();
    const choices = [...steps,
      ...(data.domain ? data.nodes : data.thread_nodes || []).map((node) => ["node:" + node.id, node.title, "graph"]),
      ...data.references.map((source) => ["reference:" + source.id, source.title, "book"]),
    ];
    available = match ? choices.filter(([id, title]) => title.includes(match[1]) || (match[1] && id.includes(match[1]))).slice(0, 12) : [];
    suggestionIndex = Math.min(suggestionIndex, Math.max(0, available.length - 1));
    suggestions.hidden = available.length === 0;
    suggestions.replaceChildren(...available.map(([id, title, symbol], index) => {
      const option = button(title, () => insertMention(id, title), "ghost", symbol);
      option.setAttribute("aria-pressed", String(index === suggestionIndex));
      option.addEventListener("mousedown", (event) => event.preventDefault());
      return option;
    }));
  }
  function insertMention(id, title) {
    const end = input.selectionStart;
    const match = mention();
    if (!match) return;
    const [kind, identity] = id.split(":", 2);
    const anchor = identity ? " [" + (kind === "node" ? "工艺节点" : "文献 ID") + " " + identity + "]" : "";
    input.setRangeText("@" + title + anchor + " ", end - match[1].length - 1, end, "end");
    value.prompt = input.value;
    send.disabled = false;
    suggestions.hidden = true;
    sizePrompt();
    input.focus();
  }
  input.addEventListener("input", () => {
    value.prompt = input.value;
    sizePrompt();
    send.disabled = !value.prompt.trim();
    error.hidden = true;
    suggestionIndex = 0;
    updateMentions();
  });
  input.addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    if (!suggestions.hidden && ["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(event.key)) {
      event.preventDefault();
      if (event.key === "Escape") suggestions.hidden = true;
      else if (event.key === "Enter") insertMention(...available[suggestionIndex]);
      else {
        suggestionIndex = (suggestionIndex + (event.key === "ArrowDown" ? 1 : -1) + available.length) % available.length;
        updateMentions();
      }
    } else if (event.key === "Enter" && !event.shiftKey &&
        (config.settings.send_shortcut === "enter" || event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      form.requestSubmit(send);
    }
  });
  input.addEventListener("blur", () => { suggestions.hidden = true; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    if (uploading) {
      error.textContent = "请等待附件上传完成。";
      error.hidden = false;
      return;
    }
    const latest = currentChat?.id === thread ? currentChat.turns.at(-1) : null;
    if (latest?.status === "running" && !latest.scoped_workflow && value.prompt.trim()) {
      if (value.attachments?.length) {
        error.textContent = "附件可随本轮结束后的新指令一起提交。";
        error.hidden = false;
        return;
      }
      send.disabled = true;
      try {
        await post("/api/runs/" + latest.id + "/steer", { text: value.prompt.trim() });
        value.prompt = input.value = "";
        sizePrompt();
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      } finally {
        send.disabled = !value.prompt.trim();
      }
    } else if (controlsBusy(latest)) {
      error.textContent = latest.status === "awaiting_scope" || latest.status === "awaiting_stage" ? "请先处理上方的待确认事项。"
        : "请等待本轮结束，或先取消当前指令。";
      error.hidden = false;
    } else if (!readyToSubmit()) {
      error.replaceChildren("请先选择已配置的模型。", href("打开模型设置", locationFor("settings"), "button ghost"));
      error.hidden = false;
    } else if (value.prompt.trim() && !submitting) {
      submitting = true;
      send.disabled = true;
      try {
        await submitScope(value, thread);
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      } finally {
        submitting = false;
        send.disabled = !value.prompt.trim();
      }
    }
  });
  send.disabled = !value.prompt.trim();
  pills.append(addFile, modelSelect.element, effortSelect.element, rangeMenu.element, policyMenu.element);
  const submitControls = actions(interrupt, send);
  submitControls.classList.add("composer-submit");
  bar.append(pills, submitControls);
  if (value.forkRun) {
    const branch = actions($("span", "badge active", "从已有执行轮分支"), button("取消分支", () => {
      delete value.forkRun;
      branch.remove();
    }));
    branch.className = "actions branch-notice";
    form.append(branch);
  }
  form.append(input, suggestions, uploaded, bar, files, error);
  requestAnimationFrame(() => { if (input.isConnected) sizePrompt(); });
  return form;
}

async function submitScope(value, thread, extra = {}) {
  const route = locationState;
  const domain = workingDomain(thread)?.id;
  const settings = {
    prompt: value.prompt.trim(), execution_mode: value.mode, cohort: "offline",
    sandbox_mode: value.sandbox,
    ...extra,
    scope_request: { domain_id: domain || null, target: value.scopeTarget || {},
      ...(value.stageQueue.length ? { stage_queue: value.stageQueue } : {}),
      execution_pace: value.executionPace, collection_stop: value.collectionStop },
  };
  if (value.mode === apiGateway) {
    settings.model_profile_id = value.profile;
  } else {
    settings.subscription_model_id = value.codexModel;
    settings.approval_mode = value.approval;
  }
  if (value.effort !== null) settings.reasoning_effort = value.effort;
  if (value.attachments?.length) settings.attachments = value.attachments.map(({ id }) => id);
  if (thread) settings.conversation_id = thread;
  const result = await post("/api/runs", settings);
  const conversation = result.conversation_id;
  delete value.attachments;
  delete value.scopeTarget;
  delete value.forkRun;
  Object.assign(value, { prompt: "", stageQueue: [], executionPace: pausePace });
  drafts.set(conversation, { ...value });
  if (locationState.path === route.path) navigate(locationFor("scope/" + conversation, route.domain));
}

function planEditor(value, thread, error) {
  const available = value.mode === codexSubscription ?
    enabledNativeModels(config.native, config.settings).some(({ id }) => id === value.codexModel) : value.profile;
  if (!available) {
    error.replaceChildren("请先选择已配置的模型。", href("打开模型设置", locationFor("settings"), "button ghost"));
    error.hidden = false;
    return;
  }
  const current = thread && currentChat?.id === thread ? currentChat : null;
  const rev = current?.plan_history?.at(-1);
  const previous = rev?.plan || current?.turns?.at(-1)?.plan || {};
  const name = workingDomain(thread)?.name || "";
  const nodes = data.domain ? data.nodes : data.thread_nodes || [];
  const prompt = value.prompt.trim();
  const urls = [...new Set(prompt.match(/https?:\/\/[^\s，、；）》]+/g) || [])].join("\n").slice(0, 1000);
  const resolve = !name && !thread && !value.forkRun && !value.attachments?.length;
  const planFields = [
    ["scenario", "工程场景", previous.scenario || prompt.split("\n")[0].slice(0, 240), 240, true],
    ["stages", "工序范围", previous.stages || (!resolve && nodes.map(({ title }) => title).join("、").slice(0, 500)) || "按研究目标识别关键工序", 500, true],
    ["region", "地区", previous.region || "不限", 160, true],
    ["time_window", "时间范围", previous.time_window || "不限", 160, true],
    ["asset_types", "资产类型", previous.asset_types || "公开规范、厂商文档、实际工程样本", 500, true],
    ["deliverables", "预期交付物", previous.deliverables || "按工序排列的证据、原文位置与覆盖缺口", 700, true],
    ["sources", "种子入口", urls || previous.sources || "", 1000, false],
    ["open_questions", "待确认事项", previous.open_questions || "", 600, false],
  ];
  editor("确认采集计划", [
    ...(resolve ? [$("p", "meta wide", "领域由 Agent 查重并匹配。")] : []),
    ...planFields.map(([key, label, content, limit, required]) => {
      const wide = !["region", "time_window"].includes(key);
      return field(key, label, { value: content, limit, required, wide, as: wide ? "textarea" : "input" });
    }),
    ...(rev ? [field("plan_reason", "修订理由（范围或运行方式改变时填写）", { limit: 500, wide: true })] : []),
  ], "确认并运行", async (fields) => {
    const { plan_reason, ...scope } = fields;
    const extra = { plan: { domain: name, ...scope }, public_research: true };
    if (resolve) extra.resolve_domain = true;
    if (rev) {
      extra.plan_version = rev.version;
      extra.plan_reason = plan_reason || "";
    }
    await submitScope(value, thread, extra);
  }, "plan");
}

function hero() {
  const section = $("section", "command-home");
  const quick = $("div", "quick-actions");
  quick.setAttribute("role", "group");
  quick.setAttribute("aria-label", "研究模板");
  for (const [title, prompt] of shortcuts) {
    quick.append(button(title, () => startFrom(prompt), "shortcut"));
  }
  section.append(heading(data.domain ? "领域任务与对话" : "研究指挥台",
    data.domain ? data.domain.name : "今天想搜索哪个领域？"), quick, composer(null));
  return section;
}

function titleThread(h1, title) {
  const full = title?.trim() || "执行线程";
  const firstLine = full.split(/[\n\r。！？]/, 1)[0].trim() || full;
  const chars = Array.from(firstLine);
  h1.textContent = chars.length > 24 ? chars.slice(0, 23).join("") + "…" : firstLine;
  h1.title = full;
}

function agentMessage(text) {
  const message = $("div", "agent-message");
  if (markdown) message.innerHTML = markdown.render(text);
  else message.textContent = text;
  for (const link of message.querySelectorAll("a[href]")) {
    if (link.origin !== location.origin && ["http:", "https:"].includes(link.protocol)) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  }
  return message;
}

function renderImportTask(turn, timeline) {
  const progress = turn.result.local_import;
  const card = $("article", "turn");
  const count = (value) => value.toLocaleString("zh-CN");
  const summary = turn.status === "completed"
    ? `已整理 ${count(progress.processed)} 条来源，保留 ${count(progress.accepted)} 条` +
      (progress.processed > progress.accepted ? `，筛除 ${count(progress.processed - progress.accepted)} 条。` : "。")
    : `已整理 ${count(progress.processed)} / ${count(progress.candidates)} 条来源。`;
  card.append(heading("本地资料导入"), actions(stateBadge(turn.status), ...(progress.model ? [$("span", "mono meta", progress.model)] : [])),
    $("p", "muted", summary));
  if (activeStatus.has(turn.status) && progress.candidates) {
    const meter = $("progress", "import-task-progress");
    meter.max = progress.candidates;
    meter.value = progress.processed;
    meter.setAttribute("aria-label", "本地资料整理进度");
    card.append(meter);
  }
  for (const domain of progress.domains) {
    const row = $("div", "phase-row");
    const text = [domain.added ? `入库 ${count(domain.added)} 条` : "", domain.duplicates ? `已有 ${count(domain.duplicates)} 条` : ""]
      .filter(Boolean).join(" · ");
    row.append($("span", "mono meta", domain.l2_code),
      href(domain.name, locationFor(domain.added ? "sources/imports" : "sources/frontier", domain.id),
        "domain-mount-link", "book"), $("span", "meta", text));
    card.append(row);
  }
  if (progress.error) card.append($("p", "inline-error", progress.error));
  if (turn.status === "failed" || turn.status === "needs_review") {
    const retry = button(progress.ready ? "导入本机资料库" : "继续整理", async () => {
      retry.disabled = true;
      try {
        await post("/api/workbench/collaboration/imports", { action: progress.ready ? "import_local" : "prepare", batch: turn.id });
        await reload();
      } catch (error) {
        card.append($("p", "inline-error", error.message));
        retry.disabled = false;
      }
    }, "primary");
    card.append(retry);
  }
  timeline.replaceChildren(card);
}

function renderThread(turns) {
  if (!threadView) return;
  const { timeline, scroller, subtitle, resume, footer, title } = threadView;
  if (turns.length) titleThread(title, turns[0].prompt);
  const last = turns.at(-1);
  footer.hidden = last?.route === "local-import";
  if (footer.hidden) {
    subtitle.replaceChildren(stateBadge(last.status));
    resume.replaceChildren();
    renderImportTask(last, timeline);
    return;
  }
  const interrupt = footer.querySelector(".interrupt");
  if (interrupt) interrupt.hidden = last?.status !== "running";
  const scrollTop = scroller.scrollTop;
  const stayAtEnd = !timeline.querySelector(".turn") || scroller.scrollHeight - scrollTop - scroller.clientHeight < 120;
  const openDetails = new Set(Array.from(timeline.querySelectorAll("details[open]")).map((row) => row.id));
  const panelScroll = new Map(Array.from(timeline.querySelectorAll(".stage-rollout:not([hidden])"))
    .map((panel) => [panel.id, panel.scrollTop]));
  subtitle.replaceChildren(...(last ? [turnBadge(last)] : []));
  resume.replaceChildren();
  if (last?.can_resume && last.result?.stage_timeline?.at(-1)?.status !== "failed") {
    const feedback = $("span", "meta");
    feedback.setAttribute("role", "status");
    const continueRun = button("继续当前任务", async () => {
      continueRun.disabled = true;
      try {
        await post("/api/runs/" + last.id + "/control", { action: "resume" });
        await reload();
      } catch (error) {
        feedback.textContent = error.message;
        continueRun.disabled = false;
      }
    }, "primary", "retry");
    resume.append(continueRun, feedback);
  }
  if (!turns.length) {
    timeline.replaceChildren($("p", "muted", "正在读取线程…"));
    return;
  }
  timeline.replaceChildren(...turns.map((turn) => {
    const item = $("article", "turn");
    const result = turn.result || {};
    item.append($("div", "user-message", turn.prompt));
    const meta = $("div", "turn-meta");
    meta.append(turnBadge(turn));
    if (turn.subscription_model) meta.append($("span", "meta", "Codex 订阅 · " + turn.subscription_model.name));
    else if (turn.model_profile?.model) meta.append($("span", "meta", turn.model_profile.model));
    item.append(meta);
    const stages = result.stage_timeline || [];
    const reports = result.scope_receipts || [];
    const scope = turn.execution_scope;
    if (stages.length) {
      const stagesView = $("div", "stage-timeline");
      stagesView.setAttribute("aria-label", "阶段时间线");
      for (const stage of stages) {
        const content = [];
        if (stage.domain_library) {
          const library = stage.domain_library;
          const message = (library.created ? "已建领域资料库" : "已匹配并挂载至现有领域库") + "「" + library.name + "」";
          content.push(href(message, locationFor("sources", library.id), "domain-mount-link", "book"));
        }
        if (stage.summary) content.push(agentMessage(stage.summary));
        const report = stage.receipt || reports.find(({ contract_id }) => contract_id === stage.contract_id);
        if (report) {
          const receipt = scopeReceipts(report, openDetails, null);
          receipt.id += "-" + stage.id;
          receipt.open = openDetails.has(receipt.id);
          content.push(receipt);
        }
        const primary = stage === stages.at(-1);
        const live = primary && stage.status === "running" && activeStatus.has(turn.status);
        if (live && scope?.state === "executing" && stage.contract_id === scope.contract_id) {
          content.unshift(scopePill(scope));
        }
        const canRetry = primary && turn === last && turn.can_resume && stage.status === "failed";
        const canUnlock = canRetry && stage.error?.code === "guard_loop_abort" && stage.error.suggested_stages?.length
          && scope?.operation_mode !== "READ_ONLY";
        stagesView.append(stageCard(stage, {
          prefix: "chat-" + turn.id, expanded: expandedStages, openDetails, live, content, approval: turn.pending_approval,
          ...(canRetry ? {
            retry: () => retryStage(turn, stage),
            chooseModel: () => footer.querySelector(".composer")?.selectRetryModel(turn, stage),
            ...(canUnlock ? { unlock: () => unlockLaterStages(turn, stage) } : {}),
          } : {}),
        }));
      }
      item.append(stagesView);
    } else {
      const activity = runActivity(turn);
      if (activity) item.append(activity);
      if (result.agent_summary) item.append(agentMessage(result.agent_summary));
      if (turn.error) item.append($("p", "inline-error", turn.error));
    }
    if (turn.status === "awaiting_scope" && turn.execution_scope) {
      item.append(clarifyScope(turn, config.profiles.filter(profileUsable), async (payload) => {
        await post("/api/runs/" + turn.id + "/scope", payload);
        await reload();
      }, async () => {
        await post("/api/runs/" + turn.id + "/control", { action: "cancel" });
        await reload();
      }));
    }
    const options = actions();
    if (turn.status === "awaiting_stage" && scope?.next_stage) {
      const checkpoint = $("section", "scope-clarification stage-checkpoint");
      const message = $("p", "inline-error");
      message.setAttribute("role", "alert");
      const decide = async (advance) => {
        confirm.disabled = cancel.disabled = true;
        message.textContent = "";
        try {
          await post("/api/runs/" + turn.id + (advance ? "/scope" : "/control"),
            advance ? { action: "continue_stage", contract_id: scope.contract_id } : { action: "cancel" });
          await reload();
        } catch (error) {
          message.textContent = error.message;
          confirm.disabled = cancel.disabled = false;
        }
      };
      const confirm = button("确认并推进至" + stageNames[scope.next_stage], () => decide(true), "primary", "arrow");
      const cancel = button("取消余下环节", () => decide(false), "ghost");
      checkpoint.append($("h2", "", stageNames[scope.target_stage] + "已完成"),
        $("p", "muted", reports.at(-1)?.outcome), actions(confirm, cancel), message);
      item.append(checkpoint);
    }
    if (turn.benchmark_ready) options.append(href("下载题包", "/api/runs/" + turn.id + "/benchmark", "button primary", "download"));
    if (turn.can_publish) options.append(button("校验并推送到仓库 →", () => openSync(turn.id), "sync-secondary publish-shortcut"));
    if (result.tasks?.length && workingDomain()) options.append(href("查看构题成果", locationFor("tasks", workingDomain().id), "button"));
    if (turn.pending_approval) options.append(button("查看待审批操作", () => { drawerTab = "approvals"; setDrawer(true, true); }, "primary", "shield"));
    if (terminalStatus.has(turn.status) && !turn.scoped_workflow && turn.result?.native_thread_id && !["failed", "cancelled"].includes(turn.status)) {
      options.append(button("从此分支", () => {
        const target = draftFor(null);
        target.forkRun = turn.id;
        target.mode = turn.route === "codex-agent" ? codexSubscription : apiGateway;
        target.profile = turn.model_profile?.id || "";
        startFrom("沿用此会话的证据与范围，补充以下任务：");
      }, "", "graph"));
    }
    if (["running", "queued", "paused"].includes(turn.status)) {
      const controls = [[turn.status === "running" ? "中断执行轮" : "取消本轮", "cancel"]];
      if (turn.status !== "paused") controls.unshift(["暂停本轮", "pause"]);
      for (const [label, action] of controls) {
        const trigger = button(label, async () => {
          trigger.disabled = true;
          try {
            await post("/api/runs/" + turn.id + "/control", { action });
            await reload();
          } catch (error) {
            options.append($("span", "inline-error", error.message));
          } finally {
            trigger.disabled = false;
          }
        });
        options.append(trigger);
      }
    }
    if (options.children.length) item.append(options);
    return item;
  }));
  for (const [id, top] of panelScroll) {
    const panel = document.getElementById(id);
    if (panel) panel.scrollTop = top;
  }
  scroller.scrollTop = stayAtEnd ? scroller.scrollHeight : scrollTop;
}

async function loadChat(thread) {
  if (!thread || locationState?.page !== "scope") return;
  if (!data.threads.some(({ id }) => id === thread)) {
    threadView?.timeline.replaceChildren(empty("thread", "未找到该历史任务", "可从历史任务中选择其他会话。",
      actions(href("新建研究任务", locationFor("scope"), "button primary"))));
    return;
  }
  const epoch = ++chatEpoch;
  try {
    const snapshot = await request("/api/conversations/" + thread);
    if (epoch !== chatEpoch || locationState.thread !== thread) return;
    const first = currentChat?.id !== thread;
    if (first || JSON.stringify(snapshot) !== JSON.stringify(currentChat)) {
      if (!markdown && snapshot.turns.some(({ result }) => result?.agent_summary || result?.stage_timeline?.some(({ summary }) => summary))) {
        await loadMarkdown().catch(() => { markdownLoading = null; });
        if (epoch !== chatEpoch || locationState.thread !== thread) return;
      }
      currentChat = snapshot;
      if (first && !draftFor(thread).userOptions) {
        const recent = snapshot.turns.at(-1);
        if (recent) {
          const previous = draftFor(thread);
          if (recent.route === "codex-agent" || recent.route === "api-agent") {
            previous.mode = recent.execution_mode || (recent.route === "codex-agent" ? codexSubscription : apiGateway);
            previous.profile = recent.model_profile?.id || previous.profile;
            previous.codexModel = recent.subscription_model?.id || previous.codexModel;
          }
          previous.sandbox = recent.sandbox_mode || previous.sandbox;
          previous.approval = recent.approval_mode || previous.approval;
          previous.effort = recent.subscription_model?.reasoning_effort || recent.model_profile?.reasoning_effort || null;
          previous.collectionStop = recent.collection_stop || "task";
          threadView?.footer.lastElementChild.replaceWith(composer(thread));
        }
      }
      renderThread(snapshot.turns);
      if (get("shell").dataset.drawer === "open") renderInspector();
    }
  } catch (error) {
    if (epoch === chatEpoch && threadView) threadView.timeline.replaceChildren($("p", "inline-error", error.message));
  }
}

function renderMain() {
  const { page, thread, run, section, query, item, filters } = locationState;
  const view = get("view");
  view.dataset.page = page;
  view.setAttribute("aria-busy", "false");
  threadView = null;
  taskHistory = null;
  refreshView = null;
  if (page !== "scope") {
    const ctx = { data, route: locationFor, navigate, editor, startFrom, addDomain, manageDomain, run, section, query, item, filters,
      openProvider: (catalog, choices, saved) => editProvider(catalog, choices, (provider) => {
        loadConfig();
        saved(provider);
      }), addNode: () => editNode(), editNode, removeNode, organizeNode, assessFrontier,
      beginNodeResearch(node) {
        startFrom("沿「" + node.title + "」收集工序规范、开源材料和公开交接证据。记录发布方、原文锚点和覆盖缺口。",
          { stage: "STAGE_3_ACQUISITION", process_node_id: node.id }, { exhaustive: true });
      },
      openImport() {
        openSync();
        if (get("sync-import-picker").hidden) get("sync-import-toggle").click();
      }, page, node: locationState.node, viewState: domainViewState(), config, reloadConfig: loadConfig,
      onRefresh: (callback) => { refreshView = callback; } };
    if (run) {
      view.replaceChildren(runDetailView(ctx));
    } else if (page === "settings" && section === "schema") {
      view.replaceChildren(schemaView(ctx));
    } else if (page === "settings" && section) {
      view.replaceChildren(settingsSectionView(ctx));
    } else if (data.domain && section && ["tasks", "export"].includes(page)) {
      view.replaceChildren(benchmarkFormView(ctx));
    } else view.replaceChildren(views[page](ctx));
  } else if (!thread) {
    view.replaceChildren(wrapResearch(hero()));
  } else {
    const wrapper = $("div", "thread-view");
    const subject = data.threads.find(({ id }) => id === thread);
    const subtitle = $("div", "actions");
    const timeline = $("div", "timeline");
    const scroller = $("div", "thread-scroll");
    scroller.setAttribute("aria-label", "对话消息");
    scroller.tabIndex = 0;
    scroller.append(timeline);
    const footer = $("div", "thread-footer");
    const resume = $("div", "resume-current actions");
    footer.append(resume, composer(thread));
    const header = heading("", workingDomain()?.name || "", subtitle);
    const title = header.querySelector("h1");
    title.classList.add("truncate");
    titleThread(title, subject?.title);
    threadView = { timeline, scroller, subtitle, footer, resume, title };
    wrapper.append(header, scroller, footer);
    view.replaceChildren(wrapResearch(wrapper));
    renderThread(currentChat?.id === thread ? currentChat.turns : []);
  }
  renderTaskHistory();
}

function applyLocation(next) {
  if (locationState?.thread !== next.thread) {
    currentChat = null;
    ++chatEpoch;
  }
  locationState = next;
}

async function reload(main = false, route = followLocation()) {
  const current = ++workspaceEpoch;
  workspaceLoading = current;
  lastWorkspaceRead = performance.now();
  try {
    const query = new URLSearchParams({ level: route.level, view: route.page });
    if (route.domain) query.set("domain", route.domain);
    if (route.thread) query.set("thread", route.thread);
    const fresh = await request("/api/workbench?" + query);
    if (current !== workspaceEpoch || JSON.stringify(route) !== JSON.stringify(readLocation())) return;
    const differentDomain = fresh.domain?.id !== data?.domain?.id;
    const librariesChanged = JSON.stringify(data?.domains) !== JSON.stringify(fresh.domains);
    const sourcesChanged = JSON.stringify(data?.metrics) !== JSON.stringify(fresh.metrics);
    const graphChanged = JSON.stringify([data?.nodes, data?.edges, data?.participants, data?.relations, data?.import_coverage]) !==
      JSON.stringify([fresh.nodes, fresh.edges, fresh.participants, fresh.relations, fresh.import_coverage]);
    if (route.level === "domain" && (!fresh.domain || route.thread && fresh.thread_exists &&
        fresh.thread_domain?.id !== fresh.domain.id)) {
      const target = route.thread ? locationFor("scope/" + route.thread, fresh.thread_domain?.id || null)
        : locationFor("domains", null);
      history.replaceState(null, "", target);
      return reload(true, readLocation());
    }
    if (route.domain && String(fresh.domain.id) !== route.domain) {
      const parts = new URL(route.path, location.href);
      parts.pathname = parts.pathname.replace("/domains/" + route.domain, "/domains/" + fresh.domain.id);
      history.replaceState(null, "", parts.pathname + parts.search);
      route = readLocation();
    }
    data = fresh;
    const routeChanged = locationState?.path !== route.path;
    if (differentDomain || routeChanged) applyLocation(route);
    renderSidebar();
    renderStatus();
    if (differentDomain || routeChanged || main || get("view").getAttribute("aria-busy") === "true"
        || locationState.page === "domains" && librariesChanged
        || locationState.page === "graph" && graphChanged
        || locationState.page === "sources" && locationState.section !== "datasets" && (sourcesChanged || graphChanged)) renderMain();
    else refreshView?.();
    get("load-error").hidden = true;
    if (locationState.thread) loadChat(locationState.thread);
  } catch (error) {
    if (current !== workspaceEpoch) return;
    const banner = get("load-error");
    banner.textContent = error.message;
    banner.hidden = false;
    get("health-label").textContent = "连接异常";
    get("health-dot").dataset.tone = "danger";
  } finally {
    if (workspaceLoading === current) workspaceLoading = 0;
  }
}

function navigate(url, { replace = false } = {}) {
  const target = new URL(url, window.location.href);
  if (target.origin !== location.origin) return;
  const original = location.pathname + location.search + location.hash;
  const route = workspaceRoute(target);
  if (route.path === locationState?.path && route.path === original) return;
  for (const panel of document.querySelectorAll(":popover-open")) panel.hidePopover();
  for (const dialog of document.querySelectorAll(".detail-drawer[open]")) dialog.close();
  if (route.path !== original) history[replace ? "replaceState" : "pushState"](null, "", route.path);
  const needsCoverage = route.page === "graph" && data?.metrics.imported_sources && !data.import_coverage.length;
  const sameScope = data && locationState?.domain === route.domain && !route.thread && !locationState.thread && !needsCoverage;
  if (media.matches) chooseSidebar(false);
  get("stage-body").scrollTop = 0;
  if (sameScope) {
    ++workspaceEpoch;
    applyLocation(route);
    renderSidebar();
    renderMain();
  } else {
    ++chatEpoch;
    get("view").replaceChildren($("p", "loading", "正在读取工作台…"));
    get("view").setAttribute("aria-busy", "true");
    reload(true, route);
  }
}

function applyTheme() {
  const theme = config.settings.theme;
  document.documentElement.dataset.theme = theme === "system" ? (darkMode.matches ? "dark" : "light") : theme;
}

darkMode.addEventListener("change", applyTheme);

async function detectSubscription(refresh = false) {
  if (!subscriptionProbe || refresh) {
    const pending = post("/api/codex-local", { action: "detect", refresh })
      .finally(() => { if (subscriptionProbe === pending) subscriptionProbe = null; });
    subscriptionProbe = pending;
  }
  return subscriptionProbe;
}

async function loadConfig() {
  try {
    const [models, routes, settings, native] = await Promise.all([
      request("/api/model-profiles"), request("/api/catalog"), request("/api/settings"), request("/api/codex-local"),
    ]);
    config = { profiles: models.profiles, models, routes, settings: settings.preferences, native, ready: true };
    applyTheme();
    document.documentElement.dataset.density = config.settings.density;
    configReady = true;
    get("gateway-dot").hidden = false;
    get("gateway-dot").dataset.tone = "success";
    if (locationState?.page === "scope") {
      const draft = draftFor();
      if (!draft.userOptions && (!locationState.thread || currentChat?.id !== locationState.thread)) {
        draft.mode = config.settings.default_execution_mode;
        draft.profile = config.settings.default_model === "@codex" ? "" : config.settings.default_model;
        draft.codexModel = config.settings.default_codex_model;
        draft.approval = config.settings.approval_mode || draft.approval;
      }
      renderMain();
    }
  } catch {
    configReady = true;
    get("gateway-dot").hidden = true;
    if (locationState?.page === "scope") renderMain();
  }
}

document.addEventListener("click", (event) => {
  const a = event.target.closest('a[href^="/global/"], a[href^="/domains/"]');
  if (!a || a.origin !== location.origin || event.button !== 0 || event.altKey || event.shiftKey || event.metaKey || event.ctrlKey) return;
  event.preventDefault();
  if (media.matches) {
    chooseSidebar(false);
    setDrawer(false);
  }
  navigate(a.href);
});
get("sidebar-toggle").addEventListener("click", () => {
  sidebarPinned = get("shell").dataset.sidebar === "closed";
  chooseSidebar(sidebarPinned);
});
get("sidebar-backdrop").addEventListener("click", () => chooseSidebar(false));
get("inspector-toggle").addEventListener("click", () => setDrawer(get("shell").dataset.drawer !== "open", true));
get("health-pill").addEventListener("click", () => setDrawer(get("shell").dataset.drawer !== "open", true));
get("inspector-close").addEventListener("click", () => setDrawer(false));
get("drawer-backdrop").addEventListener("click", () => setDrawer(false));
get("inspector-tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-tab]");
  if (tab) { drawerTab = tab.dataset.tab; eventAgent = null; renderInspector(); }
});
get("dialog-close").addEventListener("click", () => { if (!submitting) get("editor-dialog").close(); });
get("dialog-cancel").addEventListener("click", () => { if (!submitting) get("editor-dialog").close(); });
get("editor-dialog").addEventListener("cancel", (event) => { if (submitting) event.preventDefault(); });
get("editor-dialog").addEventListener("close", () => { if (data) renderStatus(); });
get("editor-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting || !editAction) return;
  const error = get("dialog-error");
  error.hidden = true;
  const unset = [...event.target.querySelectorAll("[data-required]")].find(
    (select) => !select.querySelector('input[type="hidden"]').value,
  );
  if (unset) {
    error.textContent = "请选择" + unset.dataset.required;
    error.hidden = false;
    unset.querySelector("button").focus();
    return;
  }
  submitting = true;
  get("dialog-submit").disabled = true;
  try {
    const fields = Object.fromEntries([...new FormData(event.target)].map(([key, value]) => [key, value.trim()]));
    await editAction(fields);
    get("editor-dialog").close();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    get("dialog-submit").disabled = false;
    submitting = false;
  }
});
function restoreNavigation() {
  const route = followLocation();
  if (route.path !== locationState?.path) navigate(route.path);
}
window.addEventListener("popstate", restoreNavigation);
window.addEventListener("hashchange", restoreNavigation);
window.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.shiftKey && (event.key === "," || event.code === "Comma")
      && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    navigate(locationFor("settings"));
  }
  if (event.key === "Escape" && !event.defaultPrevented && !document.querySelector(":popover-open, dialog[open]")) {
    if (get("shell").dataset.drawer === "open") setDrawer(false);
    else if (media.matches) chooseSidebar(false);
  }
});
media.addEventListener("change", ({ matches }) => {
  chooseSidebar(!matches && sidebarPinned);
  syncHistory();
});

chooseSidebar(sidebarPinned);
const resumeOnEntry = location.pathname === "/" && !location.hash && !location.search;
const initialLoad = reload(true);
initializeCollaboration(() => {
  const route = followLocation();
  const libraryView = ["domains", "assets", "graph"].includes(route.page) ||
    (route.page === "sources" && route.section !== "datasets");
  const taskList = ["tasks", "export"].includes(route.page) && !route.section && !route.run;
  return reload(libraryView || taskList, route);
}, initialLoad);
initialLoad.then(() => loadConfig()).then(() => {
  if (!resumeOnEntry || !config.settings.resume_last_chat || readLocation().path !== locationFor("scope", null) || draftFor(null).prompt) return;
  if (data?.threads[0]) navigate(locationFor("scope/" + data.threads[0].id));
});
document.addEventListener("visibilitychange", () => { if (!document.hidden && !workspaceLoading) reload(); });
window.setInterval(() => {
  if (document.hidden || workspaceLoading) return;
  const live = !data || data.active_count || get("shell").dataset.drawer === "open";
  if (live || performance.now() - lastWorkspaceRead >= 12000) reload();
}, 2600);
window.setInterval(() => {
  if (!document.hidden) {
    document.querySelectorAll(".run-activity-time").forEach(updateActivityTime);
    document.querySelectorAll(".stage-clock:not([data-end])").forEach(updateStageClock);
  }
}, 1000);

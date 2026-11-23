import { $, actions, button, external, get, icon, post, request } from "./workbench-ui.js";
import { localImport } from "./workbench-import.js";

const path = "/api/workbench/collaboration";
const kindLabels = { literature: "文献", sources: "信源", entities: "企业关系", processes: "工序", tasks: "题目" };
const steps = [
  ["validate", "本地卫生、L2 码表与只增不减校验"],
  ["rebase", "拉取 main，自动去重变基"],
  ["push", "单 Commit 推送并同步本地库"],
];
const pending = (job) => ["queued", "running", "waiting_pull"].includes(job?.status);
const mono = (value) => $("span", "sync-mono", value);
const sha = (value) => mono(value.slice(0, 8));
const clock = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" });

let state;
let target = "";
let selectTarget = false;
let selection = new Set();
let pendingRequest = "";
let readError = "";
let publishError = "";
let gen = 0;
let loading = false;
let lastRead = 0;
let detailKey = "";
let preview = new Map();
let recent = [];
let settingsPaint;
let drawerRepositoryPaint;
let importPaint;
let onLibraryChanged;
let focusFrom;
let earliestReceipt = 0;
let taskKey = "";
let historyKey = "";
let feedbackKey = "";

function relativeTime(value) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return minutes + "分钟前";
  if (minutes < 1440) return Math.floor(minutes / 60) + "小时前";
  return Math.floor(minutes / 1440) + "天前";
}

function ready() {
  return !!state?.repository && !state.error && pendingRequest !== "repository";
}

function canPublish() {
  return ready() && state.writable !== false;
}

function isPulling() {
  return ["pull", "rebuild"].includes(pendingRequest) || pending(state?.pull) || pending(state?.rebuild);
}

function lastSyncIssue() {
  return [state?.pull, state?.refresh, state?.rebuild].filter(Boolean)
    .sort((a, b) => b.updated_at - a.updated_at)[0]?.error;
}

function failedPublication() {
  return state?.runs.filter(({ job }) => job?.status === "failed")
    .sort((a, b) => b.activity_at - a.activity_at)[0];
}

function anchor() {
  const pull = get("sync-anchor-pull");
  const meta = get("sync-anchor-meta");
  const count = get("sync-anchor-counts");
  const error = readError || state?.error || state?.remote_error || lastSyncIssue()
    || failedPublication();
  const busy = isPulling() || pending(state?.refresh) || state?.runs.some(({ job }) => pending(job) && job.status !== "waiting_pull");
  const stale = state?.remote_commit && state.remote_commit !== state.commit;
  get("sync-anchor-dot").dataset.tone = busy ? "warning" : error ? "danger"
    : !state?.repository ? "" : stale || state.pending_count || !state.commit ? "warning" : "success";
  pull.disabled = !ready() || !!pendingRequest || isPulling();
  pull.toggleAttribute("data-spinning", isPulling());
  pull.title = isPulling() ? "正在拉取 main 并同步本地元信息…" : "立即拉取同步";
  count.replaceChildren();
  if (state?.remote_ahead > 0) {
    const badge = mono("↓" + state.remote_ahead);
    badge.title = "远端待拉取 " + state.remote_ahead + " 个提交";
    count.append(badge);
  }
  if (state?.pending_count > 0) {
    const badge = mono("↑" + state.pending_count);
    badge.classList.add("sync-up-count");
    badge.title = "待发布 " + state.pending_count + " 项任务";
    count.append(badge);
  }
  meta.classList.toggle("sync-danger", !busy && !!error);
  if (busy) meta.textContent = "正在同步远端与本地库…";
  else if (error) meta.textContent = "同步失败 · 点击查看详情";
  else if (state && !state.repository) meta.textContent = "未配置资料仓库";
  else if (state?.commit) {
    meta.replaceChildren(mono("main"), " · ", sha(state.commit),
      state.synced_at ? " · " + relativeTime(state.synced_at) : "");
  } else meta.replaceChildren(mono("main"), state ? " · 等待首次同步" : " · 正在读取状态…");
}

function availableRuns() {
  return state?.runs.filter((item) => item.selectable) || [];
}

function selectionState() {
  const available = availableRuns();
  for (const id of selection) if (!available.some((item) => item.id === id)) selection.delete(id);
  const correction = [...selection].some((id) => preview.get(id)?.correction);
  const reason = get("sync-correction-reason");
  const missing = correction && !reason.value.trim();
  get("sync-correction").hidden = !correction;
  const note = !state?.repository ? ["配置私有资料仓库后可发布"]
    : state.writable === false ? ["当前 GitHub 账号仅有读取权限"]
    : missing ? ["请先补充纠错说明"] : ["校验与去重后提交至 ", mono(state.repository + ":main")];
  get("sync-footer-note").replaceChildren(...note);
  get("sync-footer-note").classList.toggle("sync-warning", missing);
  const publish = get("sync-publish");
  publish.replaceChildren(...(pendingRequest === "publish" ? ["正在提交…"]
    : selection.size ? ["校验并推送 (", mono(selection.size), ")"] : ["校验并推送至 ", mono("main")]));
  publish.disabled = !canPublish() || !selection.size || missing || !!pendingRequest
    || [...selection].some((id) => !preview.has(id) || preview.get(id).error);
  const selectAll = get("sync-select-all");
  selectAll.textContent = available.every((item) => selection.has(item.id)) ? "取消全选" : "全选";
  selectAll.hidden = available.length <= 1;
  selectAll.disabled = !canPublish();
  for (const input of get("sync-tasks").querySelectorAll("input")) input.checked = selection.has(input.value);
}

function renderTasks() {
  const available = availableRuns();
  const rows = state?.runs.filter((item) => item.selectable || pending(item.job)) || [];
  const key = JSON.stringify([!!state, canPublish(), rows.map(({ id, title, l2_code, selectable, job }) =>
    [id, title, l2_code, selectable, job?.status, preview.get(id)])]);
  if (taskKey !== key) {
    taskKey = key;
    const container = get("sync-tasks");
    const focused = container.contains(document.activeElement) ? document.activeElement.value : "";
    if (!rows.length) {
      container.replaceChildren($("p", "sync-empty", !state && !readError ? "正在读取任务…" :
        "完成采集任务或整理本地资料后，可以在这里发布。"));
    } else {
      const list = $("div", "sync-task-list");
      for (const item of rows) {
        const row = $(item.selectable ? "label" : "div", "sync-task");
        const check = $("input", "sync-check");
        Object.assign(check, { type: "checkbox", value: item.id, disabled: !canPublish() || !item.selectable });
        check.setAttribute("aria-label", "选择任务 " + item.title);
        check.addEventListener("change", () => {
          if (check.checked) selection.add(item.id);
          else selection.delete(item.id);
          selectionState();
        });
        const content = $("div", "sync-task-content");
        const header = $("div", "sync-task-head");
        const title = $("p", "truncate", item.title);
        title.title = item.title;
        header.append(title);
        if (item.l2_code) header.append($("span", "sync-code-badge sync-mono", item.l2_code));
        const meta = $("div", "sync-meta sync-task-meta");
        const detail = preview.get(item.id);
        const counts = Object.entries(detail?.counts || {});
        if (counts.length) meta.append("新增 ");
        for (const [kind, count] of counts) meta.append(mono(count), " " + kindLabels[kind] + " · ");
        const status = item.job?.status;
        const label = status === "waiting_pull" ? "拉取成功后继续" : status === "queued" ? "已排队"
          : status === "running" ? "正在发布" : detail?.error ? "本地门禁未通过"
          : !state?.repository ? "待配置资料仓库" : !detail ? "正在读取校验结果…"
          : detail.correction || counts.length ? "本地预检通过" : detail.total ? "共享库已有同键资料" : "没有可发布的记录";
        meta.append(label);
        if (detail?.error) meta.title = detail.error;
        if (detail?.correction) meta.append(" ", $("span", "sync-warning", "含历史纠错"));
        content.append(header, meta);
        row.append(check, content);
        list.append(row);
      }
      container.replaceChildren(list);
      if (focused) [...list.querySelectorAll("input")].find((input) => input.value === focused)?.focus();
    }
  }
  const counter = get("sync-task-count");
  counter.textContent = state?.pending_count ? "(" + state.pending_count + ")" : "";
  counter.hidden = !state?.pending_count;
  selectionState();
}

function renderFeedback() {
  const active = state?.runs.filter(({ job }) => pending(job) && job.status !== "waiting_pull") || [];
  const failed = failedPublication();
  const touched = state?.runs.filter(({ activity_at }) => activity_at >= earliestReceipt && activity_at > 0) || [];
  const finished = earliestReceipt && touched.filter(({ job }) => job?.outcome).length;
  const entry = active.find(({ job }) => job.status === "running") || active[0] || failed;
  const projection = pending(state?.refresh) ? "syncing" : state?.refresh?.error || "";
  const problem = publishError || (!active.length ? failed?.job.error || state?.refresh?.error : "");
  const phase = entry?.job.phase || "validate";
  const show = entry || problem || finished || pendingRequest === "publish";
  const key = JSON.stringify([entry?.id, phase, !!show, problem, finished, pendingRequest, projection, entry?.job.status]);
  if (key === feedbackKey) return;
  feedbackKey = key;
  const panel = get("sync-feedback");
  panel.hidden = !show;
  if (!show) return;
  const list = $("ol", "sync-pipeline");
  const at = !entry && projection ? steps.length - 1
    : finished && !entry ? steps.length : steps.findIndex(([step]) => step === phase);
  for (const [index, [, label]] of steps.entries()) {
    const step = $("li", "", label);
    step.prepend(mono(index + 1));
    if (index === at) step.dataset.status = problem ? "error" : "running";
    else if (index < at) step.dataset.status = "complete";
    list.append(step);
  }
  panel.replaceChildren(list);
  if (problem) panel.append($("p", "sync-gate-error", problem));
  else if (finished && !entry && !projection) {
    const outcome = touched.map(({ job }) => job?.outcome).filter(Boolean);
    const summary = [
      [outcome.filter((item) => item.status === "pushed").length, "已推送 ", " 项"],
      [outcome.reduce((total, item) => total + (item.corrected || 0), 0), "纠错 ", " 条"],
      [outcome.reduce((total, item) => total + (item.skipped || 0), 0), "跳过重复 ", " 条"],
    ].filter(([count]) => count > 0).map(([count, prefix, unit]) => prefix + count + unit);
    if (summary.length) panel.append($("p", "sync-meta", summary.join(" · ")));
  }
}

function renderHistory() {
  const key = JSON.stringify([state?.repository, recent, !!state]);
  if (key === historyKey) return;
  historyKey = key;
  const panel = get("sync-recent");
  if (!recent.length) {
    panel.replaceChildren($("p", "sync-meta", "暂无推送记录"));
    return;
  }
  panel.replaceChildren(...recent.map((item) => {
    const row = $("div", "sync-history-row");
    const title = $("span", "truncate", item.title);
    title.title = item.title;
    const label = $("div", "sync-history-label");
    label.append(sha(item.commit), mono(item.l2_code), "·", title);
    const time = $("time", "sync-meta", relativeTime(item.time));
    time.dateTime = item.time;
    time.title = new Date(item.time).toLocaleString("zh-CN");
    const link = external("↗", "https://github.com/" + state.repository + "/commit/" + item.commit);
    link.classList.add("sync-sha-link");
    link.title = "在 GitHub 查看 " + item.commit;
    link.setAttribute("aria-label", "在 GitHub 查看提交 " + item.commit.slice(0, 8));
    row.append(label, time, link);
    return row;
  }));
}

function paint() {
  anchor();
  settingsPaint?.();
  if (!get("sync-drawer").open) return;
  const repo = get("sync-repository");
  repo.textContent = state?.repository || "";
  repo.hidden = !repo.textContent;
  drawerRepositoryPaint?.();
  const baseline = get("sync-baseline");
  baseline.closest(".sync-remote").hidden = !!state && !state.repository;
  const head = state?.commit || state?.remote_commit;
  baseline.textContent = "main" + (head ? " · " + head.slice(0, 8) : "");
  baseline.title = head || "";
  const local = get("sync-local-store");
  local.className = "";
  if (isPulling() || pending(state?.refresh)) local.textContent = "正在同步…";
  else if (state?.remote_ahead > 0) {
    local.className = "sync-warning";
    local.replaceChildren("滞后远端 ", mono(state.remote_ahead), " 个提交");
  } else if (!state?.commit || state.remote_commit && state.remote_commit !== state.commit) {
    local.className = "sync-warning";
    local.textContent = state ? (state.commit ? "本地库需同步" : "待首次同步") : "正在读取…";
  } else {
    local.className = "sync-success";
    local.replaceChildren("已增量同步", ...(state.synced_at ? ["（", mono(clock.format(new Date(state.synced_at))), "）"] : []));
  }
  get("sync-drawer-pull").disabled = !ready() || !!pendingRequest || isPulling();
  const error = get("sync-remote-error");
  error.textContent = readError || state?.error || lastSyncIssue() || state?.remote_error || "";
  error.hidden = !error.textContent;
  importPaint?.(state);
  renderTasks();
  renderFeedback();
  get("sync-recent").closest(".sync-section").hidden = !!state && !state.repository;
  renderHistory();
}

function accept(next) {
  const prior = state;
  if (prior && prior.repository !== next.repository) {
    preview = new Map();
    recent = [];
    selection.clear();
    target = "";
    selectTarget = false;
    detailKey = "";
    earliestReceipt = 0;
    publishError = "";
    get("sync-correction-reason").value = "";
  }
  state = next;
  const projectionChanged = prior
    ? prior.repository !== next.repository || prior.commit !== next.commit ||
      ["pull", "refresh", "rebuild"].some((operation) => {
        const receipt = next[operation];
        return receipt?.status === "completed" && receipt.updated_at > (prior[operation]?.updated_at ?? 0);
      })
    : Boolean(next.commit);
  if (projectionChanged) onLibraryChanged?.();
}

async function refresh(force = false) {
  if (pendingRequest === "repository" || loading && !force) return;
  const token = ++gen;
  loading = true;
  lastRead = performance.now();
  try {
    const suffix = target ? "&run=" + encodeURIComponent(target) : "";
    const next = await request(path + "?view=summary" + suffix);
    if (token !== gen) return;
    accept(next);
    readError = "";
    if (get("sync-drawer").open) {
      const key = JSON.stringify([state.repository, state.remote_commit, target,
        availableRuns().map(({ id, activity_at }) => [id, activity_at])]);
      if (key !== detailKey) {
        const full = await request(path + "?detail=1" + suffix);
        if (token !== gen) return;
        accept(full);
        preview = new Map(full.runs.filter((item) => item.preview).map(({ id, preview }) => [id, preview]));
        recent = full.recent;
        detailKey = key;
        if (selectTarget && availableRuns().some(({ id }) => id === target)) selection.add(target);
        selectTarget = false;
      }
    }
    paint();
  } catch (error) {
    if (token !== gen) return;
    readError = error.message;
    paint();
  } finally {
    if (token === gen) loading = false;
  }
}

async function submit(payload) {
  if (pendingRequest) throw new Error("正在提交同步操作，请稍后再试");
  if (payload.action === "repository") {
    gen++;
    loading = false;
  } else if (state?.repository) {
    payload = { ...payload, repository: state.repository };
  }
  pendingRequest = payload.action;
  paint();
  try {
    const next = await post(path, payload);
    accept(next);
    readError = "";
    return next;
  } finally {
    pendingRequest = "";
    paint();
    refresh(true);
  }
}

function pull() {
  submit({ action: "pull" }).catch((error) => { readError = error.message; paint(); });
}

export function openSync(id = "") {
  const drawer = get("sync-drawer");
  if (!drawer.open) {
    focusFrom = document.activeElement;
    drawer.showModal();
    get("sync-anchor-open").setAttribute("aria-expanded", "true");
  }
  if (id) {
    target = id;
    selectTarget = true;
    detailKey = "";
    selection = new Set([id]);
    get("sync-correction-reason").value = "";
  }
  refresh(true);
  if (!id) paint();
}

export function initializeCollaboration(refreshLibrary, firstView) {
  onLibraryChanged = refreshLibrary;
  importPaint = localImport(refresh, (id) => selection.add(id));
  const drawer = get("sync-drawer");
  const trigger = get("sync-anchor-open");
  const repository = repositoryForm();
  get("sync-baseline").closest(".sync-remote").before(repository.form);
  drawerRepositoryPaint = repository.update;
  trigger.addEventListener("click", () => drawer.open ? drawer.close() : openSync());
  get("sync-anchor-pull").addEventListener("click", (event) => { event.stopPropagation(); pull(); });
  get("sync-drawer-pull").addEventListener("click", pull);
  get("sync-dismiss").addEventListener("click", () => drawer.close());
  let pressedBackdrop = false;
  drawer.addEventListener("pointerdown", (event) => { pressedBackdrop = event.target === drawer; });
  drawer.addEventListener("click", (event) => { if (event.target === drawer && pressedBackdrop) drawer.close(); });
  drawer.addEventListener("close", () => {
    trigger.setAttribute("aria-expanded", "false");
    target = "";
    selectTarget = false;
    if (focusFrom?.isConnected) focusFrom.focus();
  });
  get("sync-select-all").addEventListener("click", () => {
    const ids = availableRuns().map(({ id }) => id);
    selection = ids.every((id) => selection.has(id)) ? new Set() : new Set(ids);
    selectionState();
  });
  const reason = get("sync-correction-reason");
  reason.addEventListener("input", selectionState);
  get("sync-publish").addEventListener("click", async () => {
    publishError = "";
    earliestReceipt = Date.now() / 1000;
    const correction = !get("sync-correction").hidden;
    try {
      await submit({ action: "publish", run_ids: [...selection], correction_reason: correction ? reason.value.trim() : "" });
      reason.value = "";
      target = "";
    } catch (error) { publishError = error.message; paint(); }
  });
  firstView.then(() => refresh());
  window.setInterval(() => {
    const live = drawer.open || isPulling() || pending(state?.refresh) || state?.runs.some(({ job }) => pending(job));
    if (!document.hidden && (live || performance.now() - lastRead >= 12000)) refresh();
  }, 2800);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
}

function repositoryForm() {
  const form = $("form", "sync-repository-form");
  const field = $("label", "field");
  const input = $("input", "sync-mono");
  Object.assign(input, { type: "text", placeholder: "owner/repository 或 GitHub 地址", maxLength: 512,
    spellcheck: false, autocapitalize: "none", autocomplete: "off" });
  field.append($("span", "", "私有资料仓库"), input);
  const save = button("保存", null, "sync-secondary");
  save.type = "submit";
  const test = button("测试连接", () => change({ action: "test" }), "sync-secondary");
  const disconnect = button("断开连接", () => change({ action: "repository", repository: "" }), "ghost sync-text-button");
  const connection = actions(test, disconnect);
  const feedback = $("p", "sync-meta");
  feedback.setAttribute("role", "status");
  const controls = $("div", "sync-repository-controls");
  controls.append(field, save);
  form.append(controls, connection, feedback);
  let loadedRepository;
  let error = "";
  let confirmation = "";

  async function change(payload) {
    error = "";
    confirmation = "";
    try {
      await submit(payload);
      if (payload.action === "repository") {
        input.value = state.repository || "";
        confirmation = state.repository ? "已保存。可测试连接或拉取共享资料。" : "已断开资料仓库";
      }
    } catch (failure) {
      error = failure.message;
    }
    update();
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!save.disabled) change({ action: "repository", repository: input.value.trim() });
  });
  input.addEventListener("input", () => {
    error = "";
    confirmation = "";
    update();
  });

  function update() {
    const selected = state?.repository || "";
    if (selected !== loadedRepository) {
      loadedRepository = selected;
      input.value = selected;
      error = "";
      confirmation = "";
    }
    const changed = !!input.value.trim() && input.value.trim() !== selected;
    input.disabled = !state || pendingRequest === "repository";
    save.disabled = input.disabled || !changed || !!pendingRequest;
    save.textContent = pendingRequest === "repository" ? "正在保存…" : "保存";
    connection.hidden = !selected;
    test.disabled = !ready() || changed || !!pendingRequest || pending(state?.probe);
    disconnect.disabled = !!pendingRequest;
    const issue = error || state?.error || state?.probe?.error || (!selected && readError) || "";
    const probe = pending(state?.probe) ? "正在测试连接…" : state?.probe?.outcome
      ? (state.probe.outcome.writable ? "连接成功，可读写" : "连接成功，当前账号只读") : "";
    feedback.textContent = issue || (changed && selected ? "切换后可从新仓库拉取共享资料。草稿保留在本机。"
      : probe || confirmation);
    feedback.hidden = !feedback.textContent;
    feedback.classList.toggle("sync-danger", !!issue);
  }

  update();
  return { form, update };
}

export function collaborationSettings(ctx) {
  const content = $("div", "sync-settings");
  const repository = repositoryForm();
  const connection = $("section", "sync-setting-section");
  connection.append($("h3", "", "仓库连接配置"), repository.form);

  const preferences = $("section", "sync-setting-section sync-preferences");
  preferences.append($("h3", "", "本地门禁与自动同步偏好"));
  const fields = [
    ["auto_import", "自动发布后更新本地库", "资料自动发布后，刷新本机的共享资料视图。"],
    ["automatic", "新任务完成后自动校验并提交至 main", "任务完结后先校验，再去重并逐项提交。修改既有记录时需补充纠错说明。"],
    ["audit_ignores", "Agent 运行收尾时自动审计忽略规则", "为本地运行产物补充忽略规则。"],
  ].map(([key, label, description]) => {
    const row = $("label", "sync-preference");
    const input = $("input", "sync-check");
    input.type = "checkbox";
    input.addEventListener("change", () => change(key === "automatic"
      ? { action: "auto_sync", enabled: input.checked } : { action: "preference", name: key, enabled: input.checked }));
    const text = $("span");
    text.append($("span", "sync-preference-title", label), $("span", "sync-meta", description));
    row.append(input, text);
    preferences.append(row);
    return [key, input];
  });
  const maintenance = $("details", "sync-maintenance");
  const heading = $("summary", "", "高级维护与恢复");
  heading.append(icon("chevron"));
  const feedback = $("p", "sync-meta");
  feedback.setAttribute("role", "status");
  const commands = $("pre", "sync-command sync-mono");
  commands.hidden = true;
  const copy = button("复制命令", async () => {
    try { await navigator.clipboard.writeText(commands.textContent); feedback.textContent = "已复制命令"; }
    catch { feedback.textContent = "请选中命令后手动复制"; }
  }, "sync-secondary");
  copy.hidden = true;
  const rebuild = button("全量重建本地 SQLite 索引", () => ctx.editor("确认重建本地索引",
    [$("p", "", "从共享 main 重新建立本机资料索引。研究任务与草稿会保留。")], "确认重建", () =>
      submit({ action: "rebuild", confirmed: true })), "sync-secondary sync-danger");
  const rollback = button("生成最近提交的回滚命令", () => ctx.editor("确认生成回滚命令",
    [$("p", "", "生成最近资料提交的版本化撤回命令。请核对命令和回滚原因后在终端执行。")],
    "生成命令", async () => {
      const result = await submit({ action: "rollback_command", confirmed: true });
      commands.textContent = result.rollback_command;
      commands.hidden = copy.hidden = false;
    }), "sync-secondary sync-danger");
  maintenance.append(heading, actions(rebuild, rollback), commands, copy, feedback);
  const error = $("p", "sync-error");
  error.setAttribute("role", "alert");
  content.append(connection, preferences, maintenance, error);

  async function change(payload) {
    error.textContent = "";
    try { await submit(payload); }
    catch (failure) { error.textContent = failure.message; }
  }

  let lastRepository;
  function update() {
    repository.update();
    for (const [key, input] of fields) {
      input.checked = key === "automatic" ? !!state?.automatic : !!state?.preferences?.[key];
      input.disabled = !state || !!pendingRequest || key === "automatic" && !canPublish();
    }
    if (lastRepository !== state?.repository) {
      lastRepository = state?.repository;
      commands.textContent = feedback.textContent = "";
      commands.hidden = copy.hidden = true;
    }
    rebuild.disabled = !ready() || !!pendingRequest || pending(state?.rebuild);
    rollback.disabled = !canPublish() || !!pendingRequest || !state?.remote_commit;
    if (state?.rebuild) feedback.textContent = state.rebuild.error || (pending(state.rebuild) ? "正在重建本地索引…"
      : state.rebuild.outcome ? "本地索引已重建" : "");
  }

  settingsPaint = () => { if (content.isConnected) update(); };
  update();
  return content;
}

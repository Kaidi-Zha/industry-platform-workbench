import { $, actions, button, column, external, href, icon, popoverSelect, request, saveLocal } from "./workbench-ui.js";

const stages = { 2: "工艺图谱", 3: "信源与文献" };
const periods = { shift: "班", day: "日", week: "周", month: "月", year: "年", batch: "批" };
const clauseKinds = { format: "格式", threshold: "限值", command: "命令", process: "工艺", acceptance: "验收", failure_case: "故障判例" };
const origins = { public_source: "公开工程原件", sandbox_fixture: "可运行工程样本", supporting: "辅助文件" };

function label(text, className = "record-truncate") {
  const node = $("span", className, text);
  node.title = text;
  return node;
}

function shortRange({ min, max } = {}) {
  return min === undefined ? "" : min === max ? String(min) : min + "–" + max;
}

export function valueSummary(task) {
  const value = task.value?.assessment;
  if (!value) return $("span", "meta", "价值待复核");
  const summary = [
    shortRange(value.frequency) + " 次／" + periods[value.frequency.period],
    "专家 " + shortRange(value.expert_minutes) + " 分钟",
    value.error_loss.currency + " " + shortRange(value.error_loss),
  ].join(" · ");
  return label(summary, "badge value-summary record-truncate");
}

function recordDrawer(title, badgeText) {
  const drawer = $("dialog", "detail-drawer record-drawer");
  drawer.setAttribute("aria-label", title);
  const header = $("header", "record-drawer-header");
  const caption = $("div", "record-caption");
  const status = $("span", "badge", badgeText);
  const heading = $("h2", "", title);
  const close = button("收起面板", () => drawer.close(), "ghost record-dismiss", "close");
  const controls = actions();
  const content = $("div", "record-body");
  close.autofocus = true;
  caption.append(status, heading);
  header.append(column(caption, controls), close);
  drawer.append(header, content);
  drawer.addEventListener("click", (event) => {
    if (event.target === drawer && event.clientX < drawer.getBoundingClientRect().left) drawer.close();
  });
  drawer.addEventListener("close", () => drawer.remove(), { once: true });
  document.body.append(drawer);
  drawer.showModal();
  return { drawer, heading, status, controls, content };
}

function accordion(title, children, open = false, subtitle) {
  const group = $("details", "record-section");
  const head = $("summary", "");
  const caption = $("div", "");
  caption.append($("h3", "", title));
  if (subtitle) caption.append($("span", "meta", subtitle));
  head.append(caption, icon("chevron"));
  const body = $("div", "record-section-content");
  body.append(...children);
  group.open = open;
  group.append(head, body);
  return group;
}

function definitionList(entries) {
  const fields = $("dl", "record-fields");
  for (const [name, value] of entries) {
    const field = $("div");
    const detail = $("dd", "");
    if (value instanceof Node) detail.append(value);
    else detail.textContent = value || "待补录";
    field.append($("dt", "meta", name), detail);
    fields.append(field);
  }
  return fields;
}

function bulletList(values) {
  const list = $("ul", "record-points");
  for (const value of values || []) list.append($("li", "", value));
  return list.childElementCount ? list : $("span", "muted", "待补录");
}

function filesTree(artifacts, inline) {
  if (!artifacts.length) return $("p", "muted", "尚无已登记工程工件。");
  const root = $("ul", "artifact-tree");
  const folders = new Map([["", root]]);
  for (const file of artifacts) {
    const parts = file.path.split("/");
    const name = parts.pop();
    let directory = "";
    for (const part of parts) {
      const parent = folders.get(directory);
      directory += part + "/";
      if (folders.has(directory)) continue;
      const item = $("li");
      const group = $("ul");
      item.append($("code", "meta artifact-folder", part + "/"), group);
      parent.append(item);
      folders.set(directory, group);
    }
    const row = $("li");
    const details = $("details", "artifact-file");
    const header = $("summary");
    header.append(label(name, "mono record-truncate"), $("span", "meta", file.format || "历史输入"));
    const body = $("div", "artifact-detail");
    body.append(definitionList([["用途", file.role], ["来源", origins[file.origin]], ["溯源", file.provenance],
      ["SHA-256", file.sha256 ? $("code", "mono", file.sha256) : ""], ["大小", file.bytes != null ? file.bytes + " 字节" : ""]]));
    const source = external("打开工程原件", file.url);
    if (source) body.append(source);
    if (typeof inline[file.path] === "string") {
      body.append($("pre", "artifact-preview mono", inline[file.path]), button("下载文件", () => {
        saveLocal(name, inline[file.path], "application/octet-stream");
      }, "ghost", "download"));
    }
    details.append(header, body);
    row.append(details);
    folders.get(directory).append(row);
  }
  return root;
}

function valueBlock(task) {
  const value = task.value?.assessment;
  if (!value) return $("p", "muted", "历史记录缺少可复核的量化价值。");
  const grid = $("div", "record-metrics");
  const { frequency, expert_minutes: minutes, error_loss: loss, automation_value: automation } = value;
  for (const [label, main, basis, evidence] of [
    ["岗位工作频率", shortRange(frequency) + " 次／" + periods[frequency.period], frequency.basis, frequency.source_status],
    ["专家平均耗时", shortRange(minutes) + " 分钟", minutes.basis, minutes.source_status],
    ["典型错误损失", loss.currency + " " + shortRange(loss), loss.scenario + "。 " + loss.basis, loss.source_status],
    ["付费／自动化替代价值", automation.benefit, automation.basis, ""],
  ]) {
    const cell = $("div", "record-metric");
    cell.append($("span", "meta", label), $("strong", "", main), $("p", "muted", basis));
    if (evidence) cell.append($("span", "badge", evidence === "estimate" ? "估算" : "实录"));
    grid.append(cell);
  }
  return grid;
}

export function taskAuthorButton(ctx, readiness, preferredNode, secondary = []) {
  const name = preferredNode ? "用于构题" : "从已核验文献自动构题";
  const warning = $("div", "record-admission");
  warning.hidden = true;
  warning.setAttribute("role", "status");
  const gate = button(name, () => {
    const selected = nodeSelect?.value || preferredNode;
    const node = readiness.find(({ id }) => id === selected);
    if (!node?.ready) {
      const notices = node?.issues || readiness.flatMap((item) => item.issues);
      warning.replaceChildren($("strong", "", "构题前置不足"),
        ...(notices.length ? [...new Set(notices.map(({ message }) => message))].map((message) => $("p", "muted", message)) :
          [$("p", "muted", "请先建立工艺图谱并核验具体技术条款。")]),
        actions(...[...new Set(notices.map(({ stage }) => stage).concat(notices.length ? [] : [2, 3]))].map((stage) =>
          href("打开" + stages[stage], ctx.route(stage === 2 ? "graph" : "sources"), "button ghost"))));
      warning.hidden = false;
      return;
    }
    ctx.prepareTask?.();
    ctx.startFrom("执行阶段 4。为「" + node.title + "」构造一道工业难题。沿已确认的上下游交付关系，使用真实工程工件、适用规范与可验收边界。",
      { stage: "STAGE_4_TASK_DESIGN", process_node_id: node.id });
  }, "primary", "spark");
  const nodeSelect = !preferredNode && readiness.length > 1
    ? popoverSelect("选择工序", [["", "选择工序"], ...readiness.map((node) => [node.id, node.title])],
      { value: readiness.find(({ ready }) => ready)?.id || readiness[0].id }) : null;
  nodeSelect?.element.addEventListener("change", () => { warning.hidden = true; });
  // A single process does not need a selector.
  preferredNode ||= readiness.length === 1 ? readiness[0].id : null;
  return column(actions(...(nodeSelect ? [nodeSelect.element] : []), gate, ...secondary), warning);
}

export function taskDialog(ctx, summary, readiness, businessEditor) {
  const panel = recordDrawer(summary.title, summary.quality_approved ? "已通过构题门禁" : "历史题目 · 待复核");
  panel.content.append($("p", "muted", "正在读取题目…"));
  const domain = ctx.data.domain.id;
  const load = summary.scoped
    ? request("/api/workbench/scope-tasks?" + new URLSearchParams({ domain, item: summary.id }))
      .then(({ items }) => items[0])
    : Promise.resolve(summary);
  load.then((task) => {
    if (!panel.drawer.open) return;
    if (!task) throw new Error("当前领域没有此题目");
    const d = task.details || {};
    const ready = readiness.find(({ id }) => task.node_id === id);
    const verifier = ctx.route("verifiers", domain, { item: task.id });
    const edit = button("编辑", () => {
      if (!task.scoped) {
        valueSection.open = true;
        valueSection.querySelector("input")?.focus();
        return;
      }
      if (!ready?.ready) {
        missing.hidden = false;
        return;
      }
      panel.drawer.close();
      ctx.startFrom("执行阶段 4。请补齐或修改题目「" + task.title + "」的工程定义：",
        { stage: "STAGE_4_TASK_DESIGN", task_id: task.id });
    }, "ghost");
    panel.status.textContent = task.is_stale ? "上游已更新 · 待同步" : task.quality_approved ? "已通过构题门禁" : "历史题目 · 待复核";
    const next = href("进入验证器工程", verifier, "button");
    panel.controls.append(edit, next);
    next.addEventListener("click", () => panel.drawer.close());
    const missing = $("div", "record-admission");
    missing.setAttribute("role", "status");
    missing.hidden = true;
    if (ready && !ready.ready) missing.append($("strong", "", "构题前置不足"),
      ...ready.issues.map(({ message }) => $("p", "muted", message)));
    const context = d.industrial_context || {};
    const handoff = d.downstream_handoff || {};
    const files = d.input_artifacts || Object.keys(d.inputs || {}).map((path) => ({ path }));
    const references = new Map((d.references || []).filter((ref) => ref.id).map((ref) => [ref.id, ref]));
    const citations = d.reference_citations || [...references.keys()].map((reference_id) => ({ reference_id }));
    const evidence = $("div", "record-citations");
    for (const citation of citations) {
      const ref = references.get(citation.reference_id) || {};
      const id = "#" + citation.reference_id.slice(0, 8);
      const link = button(id + " · " + (ref.title || "查看文献"), () =>
        referenceDialog(ctx, { ...ref, id: citation.reference_id }, citation.clause_id), "ghost", "book");
      const clause = ref.clauses?.find(({ id }) => id === citation.clause_id);
      evidence.append(column(link,
        ...(clause ? [$("span", "", clause.locator + " · " + clause.requirement)] : []),
        $("span", "meta", citation.applies_to || ref.locator || "")));
    }
    const openVerifier = href("为本题配置 Verifier", verifier, "button", "arrow");
    openVerifier.addEventListener("click", () => panel.drawer.close());
    const standards = (d.applicable_standards || []).map((rule) => rule.name + " · " + rule.version + " — " + rule.applies_to);
    const boundaries = (d.boundary_conditions || []).map((rule) =>
      column($("strong", "", rule.parameter + " · " + rule.limit + " " + rule.unit),
        $("p", "muted", "精度 " + rule.precision + "。例外 " + rule.exceptions),
        $("span", "meta", rule.basis === "normative" ? "来自引用条款" : "本题工况")));
    const exceptions = (d.edge_cases_and_rejection || []).map((rule) =>
      column($("strong", "", rule.condition), $("p", "", rule.detection), $("p", "muted", rule.disposition)));
    const contract = (d.deliverables || []).map((rule) =>
      column($("code", "mono", rule.path), $("p", "", rule.format + " · " + rule.interface), $("p", "muted", rule.acceptance)));
    const valueSection = accordion("商业与岗位价值评估", [
      valueBlock(task), ...(!task.scoped ? [businessEditor(ctx, task)] : []),
    ]);
    panel.content.replaceChildren(missing,
      accordion("工业背景与工艺定位", [
        definitionList([["上游输入", context.upstream_input], ["岗位职责", context.responsibility],
          ["核心痛点", context.pain_point], ["下游去向", [handoff.process, handoff.recipient].filter(Boolean).join(" · ") || task.value?.handoff]]),
        ...(d.description || task.summary ? [$("h4", "", "任务说明"), $("p", "record-prose", d.description || task.summary)] : []),
      ], true, d.stage || d.workflow?.stage),
      accordion("前置条件、标准与边界", [
        $("h4", "", "适用标准"), bulletList(standards), $("h4", "", "容差与例外"),
        ...(boundaries.length ? boundaries : [$("p", "muted", "缺少可验收的显式边界。")]),
        ...(d.constraints ? [$("p", "record-prose", d.constraints)] : []), $("h4", "", "异常与拒收"),
        ...(exceptions.length ? exceptions : [$("p", "muted", "待补录异常处置规则。")]),
      ]),
      accordion("工程输入与交付物契约", [
        $("h4", "", "工程文件树"), filesTree(files, d.inputs || {}), $("h4", "", "交付物"),
        ...(contract.length ? contract : [$("p", "muted", d.expected_output || "待补录下游接口。")]),
        ...(handoff.interface ? [definitionList([["接收接口", handoff.interface], ["放行条件", handoff.acceptance]])] : []),
      ], false, files.length ? files.length + " 个输入文件" : ""),
      valueSection,
      accordion("证据链与验证器", [
        citations.length ? evidence : $("p", "muted", "待绑定具体规范条款。"),
        actions(openVerifier, ...(!task.scoped && task.run_id ?
          [href("历史题包与验收", ctx.route("tasks/run/" + task.run_id), "button")] : [])),
      ]));
  }).catch((error) => {
    if (panel.drawer.open) panel.content.replaceChildren($("p", "inline-error", error.message));
  });
}

export function referenceDialog(ctx, reference, focusClause) {
  const title = reference.title || "文献详情";
  const panel = recordDrawer(title, "文献档案");
  panel.content.append($("p", "muted", "正在读取文献…"));
  const domain = ctx.data.domain.id;
  request("/api/workbench/reference?" + new URLSearchParams({ domain, item: reference.id })).then((row) => {
    if (!panel.drawer.open) return;
    panel.heading.textContent = row.title || title;
    panel.status.textContent = row.quality_approved ? "已核验技术文献" : row.quality_note ? "低规范价值 · 已过滤" : "历史信源 · 待复核";
    const processes = row.processes || [];
    const preferred = processes.find(({ id }) => id === ctx.filters?.node) || processes.find(({ id }) => id === reference.groupNode)
      || processes.find(({ id }) => id === row.node_id) || processes[0];
    panel.controls.append(button("编辑", () => {
      panel.drawer.close();
      const anchor = processes.length === 1 && row.node_id === processes[0].id ? row.node_id : "";
      ctx.startFrom("执行阶段 3。复核「" + row.title + "」的发布方、版本、适用工序、权威层次、信源渠道和逐条技术规则。原文 " + row.url,
        { stage: "STAGE_3_ACQUISITION", ...(anchor ? { process_node_id: anchor } : {}) });
    }, "ghost"));
    if (preferred && row.quality_approved) {
      const promote = button("用于构题", async () => {
        promote.disabled = true;
        try {
          const params = new URLSearchParams({ domain, view: "summary" });
          const { admission } = await request("/api/workbench/scope-tasks?" + params);
          if (!panel.drawer.open) return;
          promotion.replaceChildren(taskAuthorButton({ ...ctx, prepareTask: () => panel.drawer.close() }, admission, preferred.id));
          promotion.querySelector("button")?.click();
        } catch (error) { promotion.replaceChildren($("p", "inline-error", error.message)); }
        finally { promote.disabled = false; }
      }, "outline");
      panel.controls.append(promote);
    }
    const publisher = row.publisher_info || {};
    const release = row.version_or_date || {};
    const scope = row.applicable_scope || {};
    const links = $("div", "record-source-links");
    const original = external(row.url, row.url);
    if (original) {
      original.classList.add("reference-url", "mono");
      links.append(original);
    }
    for (const [title, url] of [["打开快照", row.snapshot_uri], ["下载附件", row.attachment_uri]]) {
      const link = external(title, url);
      if (link) links.append(link);
    }
    const clauses = row.extracted_clauses || [];
    if (clauses.length || row.excerpt) {
      links.append(button("下载已保存摘录", () => saveLocal("ref-" + row.id.slice(0, 8) + ".txt",
        [row.title, row.url, ...clauses.map(({ locator, quote }) => locator + "\n" + quote), ...(!clauses.length ? [row.excerpt] : [])]
          .join("\n\n"), "text/plain;charset=utf-8"), "ghost", "download"));
    }
    const extracts = $("div", "record-clauses");
    for (const item of clauses) {
      const block = $("article", "record-clause");
      block.id = "clause-" + item.id;
      block.append(actions($("span", "badge", clauseKinds[item.kind]), label(item.locator, "meta mono")),
        $("h4", "", item.requirement), $("blockquote", "", item.quote), $("p", "muted", "检查方法 " + item.check));
      if (item.id === focusClause) block.dataset.focus = "true";
      extracts.append(block);
    }
    if (!clauses.length && row.excerpt) extracts.append($("p", "muted", "历史摘录 · " + (row.locator || "定位待复核")),
      $("blockquote", "", row.excerpt));
    const usage = $("div", "record-usage");
    for (const [heading, items] of [["引用题目", row.task_usage || []], ["验证器检查项", row.verifier_usage || []]]) {
      usage.append($("h4", "", heading));
      if (!items.length) usage.append($("p", "muted", "暂无引用"));
      for (const item of items) {
        const route = item.task_id ? "verifiers" : "tasks";
        const link = href(item.task_id ? item.layer + " · " + item.title + " · " + item.task_title : item.title,
          ctx.route(route, domain, { item: item.task_id || item.id }), "button ghost");
        link.addEventListener("click", () => {
          document.querySelectorAll(".record-drawer[open]").forEach((drawer) => drawer.close());
        });
        usage.append(column(link, $("span", "meta", item.applies_to || (item.citations || []).map(({ applies_to }) => applies_to).join(" · "))));
      }
    }
    const promotion = $("div", "record-promotion");
    panel.content.replaceChildren(promotion,
      ...(row.quality_note ? [$("p", "muted", row.quality_note)] : []),
      accordion("发布方与权威档案", [
        definitionList([["发布机构", publisher.name || row.publisher], ["机构资质", publisher.qualification],
          ["权威层次", row.authority_tier_title], ["信源渠道", row.source_channel_title], ["标准号／版本", release.version],
          ["发布或修订日期", release.published_on], ["抓取时间", row.retrieved_at],
          ["原文与附件", links]]),
      ], !focusClause, row.summary_zh),
      accordion("适用范围与前置条件", [
        definitionList([["工序", processes.map(({ title }) => title).join("、") || (row.quality_approved ? "未关联工序" : row.stage)],
          ["适用对象", scope.applies_to], ["工程对象", row.object_cluster],
          ["生命周期", row.lifecycle_stages?.join("、")],
          ["工程前提", bulletList(scope.preconditions)], ["不适用边界", bulletList(row.limitations)]]),
      ]),
      accordion("工程硬条款", [extracts.childElementCount ? extracts : $("p", "muted", "待抽取可核验的技术条款。")],
        Boolean(focusClause), clauses.length ? clauses.length + " 条" : ""),
      accordion("引用血缘", [usage]));
    if (focusClause) extracts.querySelector('[data-focus="true"]')?.scrollIntoView({ block: "start" });
  }).catch((error) => {
    if (panel.drawer.open) panel.content.replaceChildren($("p", "inline-error", error.message));
  });
}

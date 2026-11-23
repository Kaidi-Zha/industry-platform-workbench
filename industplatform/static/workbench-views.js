import {
  capabilityTags, codexSubscription, defaultNativeModel, evaluationTiers, executionModeTabs,
  nativeEffortSummary, nativeStatus, nativeVersion, profileUsable, tierNames,
} from "./workbench-models.js";
import { staleTag } from "./workbench-scope.js";
import {
  $, actions, button, column, empty, external, gridTable, heading, href, icon, iconButton,
  jsonCode, popover, popoverSelect, post, request, segments, stateBadge,
} from "./workbench-ui.js";

import { processCanvas } from "./workbench-graph.js";
import { settingsTabs } from "./workbench-settings.js";
import { referenceDialog, taskAuthorButton, taskDialog, valueSummary } from "./workbench-records.js";

const graphPrompt = "调研当前领域的典型生产流程。按工序顺序给出可复核的工艺 DAG 草案，注明上游、关键岗位与下游交付物。依据已核验技术条款登记上游与下游企业，并写明发起方、接收方和交付物的有向关系。不要把公开的接单要求写成实际成交。";

function detailDrawer(title, content) {
  const drawer = $("dialog", "detail-drawer");
  drawer.setAttribute("aria-label", title);
  const header = $("header");
  header.append($("h2", "", title), iconButton("关闭", () => drawer.close(), "close"));
  drawer.append(header, content);
  drawer.addEventListener("click", (event) => {
    if (event.target === drawer) drawer.close();
  });
  drawer.addEventListener("close", () => drawer.remove(), { once: true });
  document.body.append(drawer);
  drawer.showModal();
  return drawer;
}

function channelCoverage(channels) {
  const panel = $("div", "channel-grid");
  for (const channel of channels) {
    const details = [];
    const meter = $("progress");
    meter.max = channel.total || 1;
    meter.value = channel.completed;
    meter.setAttribute("aria-label", channel.title + " 采集队列完成度");
    if (channel.total) details.push(meter);
    if (channel.verified) details.push($("span", "meta", "已核验 " + channel.verified + " 篇"));
    if (channel.total) details.push($("span", "meta", "已处理 " + channel.completed + "/" + channel.total +
      (channel.blocked ? " · " + channel.blocked + " 项受阻" : "")));
    if (!details.length) details.push($("span", "meta", "待查"));
    panel.append(column($("h3", "", channel.title), ...details));
  }
  return panel;
}

const enterpriseRoles = [
  ["upstream", "上游关键企业"], ["manufacturer", "核心制造主体"], ["downstream", "下游交付对接方"],
];

function enterpriseGroups(participants, showProcess, action) {
  const section = $("section", "graph-enterprises");
  const header = $("header", "enterprise-heading");
  header.append($("h2", "", "企业上下游关系"));
  if (action) header.append(action);
  section.append(header);
  const groups = $("div", "enterprise-groups");
  for (const [role, title] of enterpriseRoles) {
    const companies = participants.filter((entry) => entry.role === role);
    const list = $("details");
    const summary = $("summary");
    summary.append($("span", "", title));
    const count = new Set(companies.map((entry) => entry.name.toLocaleLowerCase())).size;
    if (count) summary.append($("span", "meta mono", count));
    summary.append(icon("chevron"));
    list.append(summary);
    for (const company of companies) {
      const row = $("div", "enterprise-company");
      row.append($("span", "", company.name));
      if (showProcess) row.append(button(company.stage, () => showProcess(company.node_id), "ghost"));
      const original = external(company.locator || "查看原文依据", company.url);
      if (original) row.append(original);
      list.append(row);
    }
    if (!count) list.append($("p", "meta", "此角色待补充可核验的企业证据。"));
    groups.append(list);
  }
  section.append(groups);
  return section;
}

export function graphView(ctx) {
  const { nodes, node_links: links = [], node_suggestions: suggestions = {} } = ctx.data;
  const page = column(heading("工艺图谱", "梳理工序依赖与上下游交付。"));
  const summary = $("div", "kpi-strip");
  for (const [label, count] of [["工艺节点", nodes.length], ["已核验文献", ctx.data.metrics.verified_references]]) {
    if (!count) continue;
    const metric = $("div", "kpi");
    metric.append($("span", "meta", label), $("strong", "", count.toLocaleString("zh-CN")));
    summary.append(metric);
  }
  const relationships = ctx.data.relations || [];
  const roles = [...new Set(nodes.flatMap((node) => node.occupations || []))];
  if (summary.children.length) page.append(summary);
  const importCoverage = ctx.data.import_coverage || [];
  const coverage = importCoverage.length ? $("section", "graph-imports") : null;
  if (coverage) {
    const count = ctx.data.metrics.imported_sources.toLocaleString("zh-CN");
    const title = actions($("h2", "", "导入资料覆盖"),
      href(count + " 条信源", ctx.route("sources/imports"), "button ghost"));
    const stages = $("div", "graph-import-stages");
    for (const { code, title, count } of importCoverage) {
      const entry = href("", ctx.route("sources/imports", ctx.data.domain.id, { stage: code }), "graph-import-stage");
      entry.append($("span", "meta mono", code), $("span", "", title), $("span", "mono", count.toLocaleString("zh-CN")));
      entry.setAttribute("aria-label", `${title}，${count} 条导入信源`);
      stages.append(entry);
    }
    coverage.append(title, stages);
    if (!nodes.length) page.append(coverage);
  }
  const researchRoles = nodes.length && button("补查企业角色", () => ctx.startFrom(graphPrompt,
    { stage: "STAGE_2_TOPOLOGY" }), "ghost size-sm", "search");
  const companies = enterpriseGroups(ctx.data.participants || [], openNode, researchRoles);
  if (!nodes.length) page.append(companies);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const aliases = new Map(links.map((link) => [link.node_id, link]));
  const nodeReferences = (node, extra) => ctx.route("sources/references", ctx.data.domain.id, { node: node.id, ...extra });
  const standardGap = (node) => href("缺 T1 标准", nodeReferences(node, { tier: "T1_STANDARD" }), "meta standard-gap");
  function openNode(id) {
    const node = byId.get(id);
    if (!node) return;
    const afterClose = (action) => () => { drawer.close(); action(); };
    const content = $("section", "node-reading");
    const drawer = detailDrawer(node.title, content);
    content.append(nodeStatus(node),
      ...(node.is_stale ? [staleTag({ ...ctx, startFrom: (...args) => { drawer.close(); ctx.startFrom(...args); } },
        node, "STAGE_2_TOPOLOGY", { process_node_id: node.id })] : []),
      $("p", "meta", "采集进度 " + node.coverage + "% · 已核验 " + node.verified + " 条文献"),
      href("查看工序文献", nodeReferences(node), "button ghost", "book"),
      ...(node.missing_t1_standard ? [standardGap(node)] : []),
      $("h3", "", "目标工种"),
      node.occupations?.length ? actions(...node.occupations.map((role) => $("span", "badge", role))) :
        $("p", "meta", "待登记工种"),
      $("h3", "", "七类信源覆盖"),
      channelCoverage(node.channels || []),
      actions(button("追加此工序采集", afterClose(() => ctx.beginNodeResearch(node)), "primary", "search"),
        button("编辑工序与连线", afterClose(() => ctx.editNode(node)), "", "task"),
        button("在画布中定位", afterClose(() => canvas.focusNode(aliases.get(id)?.canonical_id || id)), "ghost", "fit")));
    const alias = aliases.get(id);
    const linked = links.filter((link) => link.canonical_id === id);
    const similar = (suggestions[id] || []).map((identity) => byId.get(identity)).filter(Boolean);
    if (alias || linked.length || similar.length) {
      content.append($("h3", "", alias || linked.length ? "关联工序" : "相似工序"));
      if (alias) {
        content.append(button(byId.get(alias.canonical_id).title, afterClose(() => openNode(alias.canonical_id)), "ghost"),
          $("p", "meta", alias.reason));
      }
      for (const link of linked) {
        const related = byId.get(link.node_id);
        content.append(actions(button(related.title, afterClose(() => openNode(related.id)), "ghost"),
          $("span", "meta", link.reason)));
      }
      if (!alias && !linked.length) content.append($("p", "meta", "核对输入、输出和岗位后再整理重叠的工序。"),
        ...similar.map((related) => button(related.title, afterClose(() => openNode(related.id)), "ghost")));
    }
    content.append(actions(
      ...(alias ? [button("解除工序关联", afterClose(() => ctx.organizeNode(node, "unlink")))] :
        !linked.length && nodes.length > 1 ? [button("整理关联工序", afterClose(() => ctx.organizeNode(node, "link")))] : []),
      button("删除工序", afterClose(() => ctx.removeNode(node)), "ghost")));
    if (ctx.data.participants?.some((entry) => entry.node_id === id)) {
      content.append(enterpriseGroups(ctx.data.participants.filter((entry) => entry.node_id === id)));
    }
  }
  function nodeStatus(node) {
    const saturated = node.crawl_status === "exhausted";
    const active = node.crawl_status === "running" || node.channels?.some((channel) => channel.leased > 0);
    const blocked = node.crawl_status === "blocked";
    return $("span", "badge " + (saturated ? "success" : blocked ? "warning" : active ? "active" : ""),
      saturated ? "已饱和" : active && !blocked ? "采集中" : "待深挖");
  }
  const canvas = processCanvas(ctx, {
    openNode, references: nodeReferences,
    emptyState: () => empty("graph", "尚未构建该领域工艺 DAG 图谱", "从有证据的调研草案开始，或先添加已知工序。",
      actions(button("用 Agent 自动调研并生成图谱", () => ctx.startFrom(graphPrompt,
        { stage: "STAGE_2_TOPOLOGY" }), "primary", "spark"),
        button("手动添加首个工艺节点", ctx.addNode))),
  });
  page.append(canvas.element);
  if (nodes.length) {
    if (roles.length) {
      const occupations = $("details", "graph-roles");
      occupations.append($("summary", "", "工种分布 · " + roles.length + " 类"),
        actions(...roles.map((role) => $("span", "badge", role))));
      page.append(occupations);
    }
    page.append(companies);
    if (ctx.node && ctx.viewState.openedGraphNode !== ctx.node) queueMicrotask(() => {
      if (page.isConnected && !document.querySelector(".detail-drawer[open]")) {
        ctx.viewState.openedGraphNode = ctx.node;
        canvas.focusNode(aliases.get(ctx.node)?.canonical_id || ctx.node);
        openNode(ctx.node);
      }
    });
    if (!ctx.node) delete ctx.viewState.openedGraphNode;
  }
  if (relationships.length) {
    const names = { actual_delivery: "实际交付", supplier_requirement: "供方交付要求", collaboration: "联合协作",
      compatibility: "工具兼容", demonstration: "研究示范" };
    const evidence = $("section", "graph-evidence");
    evidence.append($("h2", "", "企业与机构关系"), gridTable(["发起方", "承接方", "交付物", "关系", "依据"],
      relationships.map((row) => [row.sender, row.recipient, row.deliverable, $("span", "badge", names[row.kind] || "有原文依据"),
        external(row.locator || "查看原文", row.url)])));
    page.append(evidence);
  }
  if (nodes.length && coverage) page.append(coverage);
  return page;
}


function datasetsView(ctx) {
  const search = $("input");
  search.type = "search";
  search.placeholder = "搜索数据集";
  search.setAttribute("aria-label", "搜索数据集");
  search.maxLength = 120;
  search.value = ctx.query;
  const content = $("div");
  let epoch = 0;
  let timer;
  let rows = [];
  async function load(append = false) {
    const current = ++epoch;
    const query = new URLSearchParams({ kind: "datasets", q: search.value, offset: append ? rows.length : 0 });
    if (ctx.data.domain) query.set("domain", ctx.data.domain.name);
    try {
      const page = await request("/api/library?" + query);
      if (current !== epoch) return;
      rows = append ? [...rows, ...page.items] : page.items;
      if (!rows.length) {
        content.replaceChildren(empty("cube", "暂无已登记的数据集", "完成样本核验后，可在这里查看来源、版本与许可。",
          actions(href("查看信源与文献", ctx.route("sources"), "button primary"))));
      } else {
        content.replaceChildren(gridTable(["数据集", "状态", "版本与许可", "出处"], rows.map((row) => [
          column($("strong", "", row.title), $("p", "meta", row.summary)), stateBadge(row.status), row.meta.join(" · "),
          actions(...row.links.map(({ label, url }) => external(label, url)).filter(Boolean),
            href("运行记录", ctx.route("tasks/run/" + row.run_id), "button ghost")),
        ])));
        if (rows.length < page.total) content.append(button("加载更多", () => load(true), "ghost"));
      }
    } catch (error) { if (current === epoch) content.replaceChildren($("p", "inline-error", error.message)); }
  }
  search.addEventListener("input", () => {
    ++epoch;
    clearTimeout(timer);
    timer = setTimeout(() => load(), 180);
  });
  load();
  return column(heading("数据集", "", actions(search, href("信源与文献", ctx.route("sources"), "button ghost"))), content);
}

export function sourcesView(ctx) {
  if (ctx.section === "datasets") return datasetsView(ctx);
  const importCount = ctx.data.metrics.imported_sources;
  const tabs = [["imports", "本地导入"], ["frontier", "原始采集队列"], ["references", "已核验文献库"]];
  const kind = ctx.section || (importCount && !ctx.data.metrics.verified_references && !ctx.item
    && ![ctx.filters.node, ctx.filters.tier, ctx.filters.channel, ctx.filters.view].some(Boolean)
    ? "imports" : "references");
  const classified = kind === "references";
  const { tier = "", channel = "", node: process = "", view = "" } = ctx.filters;
  const lifecycle = ctx.filters?.stage || "";
  const scope = ctx.data.domain.id;
  const tools = $("div", "sources-controls");
  const search = $("label", "source-search");
  const field = $("input");
  field.type = "search";
  field.placeholder = "搜索信源";
  field.setAttribute("aria-label", "搜索信源");
  field.maxLength = 120;
  field.value = ctx.query;
  search.append(icon("search"), field);
  const content = $("div");
  const page = column(heading("信源与文献", "", tools));
  const filters = $("div", "reference-filters");
  if (classified) {
    page.classList.add("reference-library");
    page.append(filters);
  }
  page.append(content);
  let offset = 0;
  let timer;
  let epoch = 0;
  let rows = [];
  let focusItem = ctx.item;
  const groupStates = new Map();
  const crawlColumns = ["检索式／网址", "工序", "渠道", "抓取状态", "新增贡献", "抓取时间", "操作"];
  const importColumns = ["名称与来源", "工程对象", "生命周期", "状态", "原文"];
  const allLabel = "查看全部" + (kind === "references" ? "已核验文献" : kind === "frontier" ? "采集队列" : "导入信源");
  const allItems = () => href(allLabel, ctx.route("sources/" + kind), "button ghost");
  tools.append(segments("信源类型", tabs, kind, (next) => {
    if (kind === next && !focusItem) return;
    const filter = field.value.trim();
    ctx.navigate(ctx.route("sources/" + next, scope, filter ? { q: filter } : undefined));
  }), search);
  function selectFacets(overrides) {
    const params = { tier, channel, node: process, view, q: field.value.trim(), ...overrides };
    ctx.navigate(ctx.route("sources/references", scope, Object.fromEntries(Object.entries(params).filter(([, value]) => value))));
  }
  async function load(append = false) {
    clearTimeout(timer);
    const current = ++epoch;
    if (!append) offset = 0;
    const q = field.value.trim();
    content.setAttribute("aria-busy", "true");
    if (!append) content.replaceChildren($("p", "muted", "正在读取信源…"));
    try {
      const query = new URLSearchParams({ domain: scope, kind, q, offset });
      if (focusItem) query.set("item", focusItem);
      if (lifecycle) query.set("stage", lifecycle);
      if (process) query.set("node", process);
      if (tier) query.set("tier", tier);
      if (channel) query.set("channel", channel);
      const result = await request("/api/workbench/sources?" + query);
      if (!page.isConnected || current !== epoch) return;
      rows = append ? [...rows, ...result.items] : result.items;
      offset += result.items.length;
      if (classified && !filters.children.length) filters.append(...referenceFilters(result, { tier, channel, view }, selectFacets));
      const filterLabel = lifecycle ? `${lifecycle} ${result.stage_title}` : "";
      const filterBar = filterLabel ? actions($("span", "badge", filterLabel),
        href("清除筛选", ctx.route("sources/" + kind, scope, q ? { q } : undefined), "button ghost")) : null;
      if (focusItem && !rows.length) {
        content.replaceChildren(empty("book", "当前领域没有此信源", "", actions(allItems())));
        return;
      }
      if (!rows.length) {
        const text = kind === "references"
          ? ["还没有通过准入的技术文献", "Agent 核验发布版本、适用边界与工程条款后，文献会出现在这里。"]
          : ["采集队列暂为空", "发起一次研究，将公开入口和待查范围交给 Agent。"];
        const filtered = classified && (tier || channel || process);
        const standardGap = classified && tier === "T1_STANDARD" && result.node && result.node !== "unassigned";
        const emptyPanel = q ? empty("search", "未找到匹配信源", "",
          actions(button("清除搜索", () => { field.value = ""; load(); }, "primary")))
          : filtered ? empty("book", standardGap ? "该工序尚缺 T1 标准" : "当前分类暂无已核验文献", "",
            actions(standardGap ? button("补查此工序的标准依据", () => ctx.startFrom(
              "沿标准组织与法规库补查「" + result.node_title + "」适用的正式行业标准，核对版本、适用范围和可验收条款。",
              { stage: "STAGE_3_ACQUISITION", process_node_id: result.node }), "primary")
              : button("清除筛选", () => selectFacets({ node: "", tier: "", channel: "" }), "primary")))
          : kind === "imports" ? empty("book", lifecycle ? "该阶段暂无导入信源" : "当前领域暂无本地导入信源", "",
            lifecycle ? actions(allItems()) : actions(button("导入本地资料", ctx.openImport, "primary")))
            : empty("book", ...text, actions(button("发起采集研究", () => ctx.startFrom(
            "执行阶段 3。检索当前领域的技术标准、工艺约束和公开工程实样，逐条核对规范与不适用边界。",
            { stage: "STAGE_3_ACQUISITION" }), "primary"),
          ...(kind === "references" ? [href("查看待复核信源", ctx.route(importCount ? "sources/imports" : "sources/frontier"), "button ghost")] : [])));
        content.replaceChildren(...(kind === "frontier" ? [channelCoverage(result.channels)] : []), ...(filterBar ? [filterBar] : []),
          emptyPanel);
        return;
      }
      const records = classified
        ? renderReferences(ctx, result, rows, view, Boolean(q || tier || channel || focusItem), groupStates)
        : kind === "imports" ? gridTable(importColumns, rows.map((row) => [
          column(recordText(row.title), recordText(row.summary || row.url, "meta row-sub")),
          recordText(row.object_cluster, "mono"),
          actions(...row.lifecycle.map(({ code, title }) => {
            const tag = $("span", "badge mono", code);
            tag.title = title;
            return tag;
          })), stateBadge(row.state), row.reference_id ? button("核验文献", () => referenceDialog(ctx,
            { id: row.reference_id, title: row.title }), "ghost record-action") : external("查看原文", row.url),
        ]), "record-table imports-table")
        : gridTable(crawlColumns, rows.map((row) => {
          const buttons = actions();
          const original = external("查看原文", row.canonical_url);
          if (row.reference_id) buttons.append(button("展开详情", () => referenceDialog(ctx,
            { id: row.reference_id, title: row.target }), "ghost record-action"));
          else if (original) buttons.append(original);
          if (row.node_id && ["retry", "blocked"].includes(row.state)) buttons.append(button("重试", async () => {
            try {
              await post("/api/workbench/frontier", { action: "retry", id: row.id,
                domain_id: ctx.data.domain.id, node_id: row.node_id });
              load();
            } catch (error) { buttons.append($("span", "inline-error", error.message)); }
          }));
          if (row.assessment === "pending") buttons.append(button("复核", () => ctx.assessFrontier(row, load)));
          return [row.import_summary ? column(recordText(row.target), recordText(row.import_summary, "meta row-sub"))
            : column(recordText(row.target, "mono"), recordText(row.last_error, "meta row-sub")),
            recordText(row.stage || ""), recordText(row.channel), stateBadge(row.state),
            row.first_round != null && row.first_round === row.round_number ? $("span", "badge success", "新增实体") : "",
            row.retrieved_at ? $("time", "mono", row.retrieved_at) : "", buttons];
        }), "record-table frontier-table");
      content.replaceChildren(...(kind === "frontier" ? [channelCoverage(result.channels)] : []), ...(filterBar ? [filterBar] : []), records);
      if (classified && result.node && !append) content.querySelector("[data-process-focused]")?.scrollIntoView({ block: "nearest" });
      if (focusItem) {
        content.prepend(actions(allItems()));
        if (kind === "references" && !document.querySelector(".detail-drawer[open]")) referenceDialog(ctx, rows[0]);
      }
      if (result.more) {
        const more = button("加载更多", () => { more.disabled = true; load(true); }, "table-footer");
        content.append(more);
      }
    } catch (error) {
      if (current === epoch) content.replaceChildren($("p", "inline-error", error.message), button("重试", () => load()));
    } finally {
      if (current === epoch) content.setAttribute("aria-busy", "false");
    }
  }
  field.addEventListener("input", () => {
    ++epoch;
    focusItem = "";
    clearTimeout(timer);
    timer = setTimeout(() => load(), 180);
  });
  load();
  return page;
}

function referenceFilters(result, { tier, channel, view }, select) {
  const authority = segments("权威层次", [
    ["", result.total ? "全部 (" + result.total + ")" : "全部"],
    ...result.authority_tiers.map((entry) => [
      entry.id, entry.title + (entry.verified ? " (" + entry.verified + ")" : entry.id === "T1_STANDARD" ? " · 缺" : ""),
    ]),
  ], tier, (value) => select({ tier: value }));
  authority.classList.add("reference-tiers");
  for (const [index, entry] of result.authority_tiers.entries()) {
    if (!entry.verified) authority.children[index + 1].dataset.gap = "true";
  }
  const covered = result.channels.filter((entry) => entry.verified).length;
  const allChannels = "7 类渠道" + (covered ? " (" + covered + "/7)" : "");
  const channels = popoverSelect("工业信源渠道", [
    ["", allChannels],
    ...result.channels.map(({ id, title, verified }) => [id, title + (verified ? " · " + verified + " 篇" : " · 待核验")]),
  ], {
    value: channel, align: "end",
    renderCaption: (caption, value) => {
      const match = result.channels.find(({ id }) => id === value);
      caption.textContent = match ? "渠道 " + match.id.slice(0, 2) : allChannels;
    },
    decorateOption: (option, value) => {
      if (result.channels.some(({ id, verified }) => id === value && !verified)) {
        const dot = $("span", "dot");
        dot.dataset.tone = "warning";
        option.prepend(dot);
      }
    },
  });
  const processes = popoverSelect("工序", [
    ["", "全部工序"], ...result.processes.map(({ id, title, position }) =>
      [id, (position ? String(position).padStart(2, "0") + " " : "") + title]),
  ], {
    value: result.node, align: "end",
    renderCaption: (caption, value) => {
      const process = result.processes.find(({ id }) => id === value);
      if (process?.position) caption.textContent = "工序 " + String(process.position).padStart(2, "0");
    },
  });
  channels.element.addEventListener("change", () => select({ channel: channels.value }));
  processes.element.addEventListener("change", () => select({ node: processes.value }));
  const layout = segments("文献布局", [["", "按工序分组"], ["flat", "平铺列表"]], view, (value) => select({ view: value }));
  return [authority, actions(channels.element, processes.element, layout)];
}

function renderReferences(ctx, result, rows, view, filtered, groupStates) {
  const tierById = new Map(result.authority_tiers.map((entry) => [entry.id, entry]));
  const open = (row) => referenceDialog(ctx, row);
  function table(records, flat = false, groupNode) {
    const detail = (row) => open({ ...row, groupNode });
    const headers = ["编号", "正式名称与技术定位", ...(flat ? ["所属工序"] : []), "权威层次与发布方", "适用版本／标准", "详情"];
    const list = gridTable(headers, records.map((row) => [
      referenceID(row),
      column($("span", "reference-title", row.title), $("span", "meta row-sub reference-summary", row.summary_zh)),
      ...(flat ? [$("span", "reference-process-names", row.process_names.join("、") || "未关联工序")] : []),
      column($("span", "authority-tier " + (row.authority_tier || ""), tierById.get(row.authority_tier)?.short_title || "待归类"),
        $("span", "meta row-sub", row.publisher)),
      column($("span", "mono", row.version), ...(row.citations ? [$("span", "meta row-sub", "被引用 " + row.citations + " 次")] : [])),
      button("展开详情", () => detail(row), "ghost record-action"),
    ]), "references-table record-table" + (flat ? " references-flat" : " references-grouped"));
    bindRecordRows(list, records, detail);
    return list;
  }
  if (view === "flat") return table(rows, true);
  const groups = $("div", "reference-groups");
  const byProcess = new Map();
  for (const row of rows) {
    for (const id of row.process_node_ids) {
      if (!byProcess.has(id)) byProcess.set(id, []);
      byProcess.get(id).push(row);
    }
  }
  for (const { id, title, position, count, authority_counts, missing_t1_standard } of result.processes) {
    if (result.node ? result.node !== id : !count && (filtered || id === "unassigned")) continue;
    const records = byProcess.get(id) || [];
    const group = $("details", "reference-group");
    if (result.node === id) group.dataset.processFocused = "";
    group.open = groupStates.get(id) ?? Boolean(records.length);
    group.addEventListener("toggle", () => { if (group.isConnected) groupStates.set(id, group.open); });
    const titleRow = $("summary");
    const name = $("span", "reference-group-name");
    name.append(icon("chevron"));
    if (position) name.append($("span", "mono", String(position).padStart(2, "0")));
    name.append($("span", "", title));
    const status = $("span", "reference-group-status");
    if (count) status.append($("span", "meta", count + " 篇"));
    for (const tier of result.authority_tiers) {
      if (authority_counts[tier.id]) status.append($("span", "reference-tier-mark mono", tier.id.slice(0, 2)));
    }
    if (missing_t1_standard) status.append($("span", "meta standard-gap", "缺 T1 标准"));
    titleRow.append(name, status);
    group.append(titleRow, records.length ? table(records, false, id) : $("p", "meta reference-group-note", count ? "加载更多后显示" : "待补文献"));
    groups.append(group);
  }
  return groups;
}

function recordText(value, className = "") {
  const text = $("span", "record-truncate " + className, value);
  if (value) text.title = value;
  return text;
}

function taskNumber(row) {
  return row.number ? "T-" + String(row.number).padStart(3, "0") : row.details?.id || row.id.split(":").at(-1);
}

function bindRecordRows(table, records, open) {
  table.querySelectorAll("tbody tr").forEach((row, index) => {
    row.classList.add("record-selectable");
    row.addEventListener("click", (event) => {
      if (!event.target.closest("button, a, input, label")) {
        row.querySelector("button.record-action")?.focus({ preventScroll: true });
        open(records[index]);
      }
    });
  });
}

async function taskData(ctx, summary = false) {
  const q = new URLSearchParams({ kind: "tasks", domain: ctx.data.domain.name });
  const scope = new URLSearchParams({ domain: ctx.data.domain.id });
  if (summary) scope.set("view", "summary");
  if (ctx.item) { scope.set("item", ctx.item); q.set("q", ctx.item); }
  const [records, scoped] = await Promise.all([request("/api/library?" + q),
    request("/api/workbench/scope-tasks?" + scope)]);
  const items = [...scoped.items, ...records.items].filter((row) => !ctx.item || row.id === ctx.item);
  return summary ? { items, admission: scoped.admission } : items;
}

function taskControls(ctx, admission) {
  const manual = href("登记 CNC／QIF 样例", ctx.route("tasks/case"), "button", "plus");
  return taskAuthorButton(ctx, admission, null, [
    manual, href("既有案例验收", ctx.route("tasks/build"), "button outline"),
  ]);
}

function referenceID(record) {
  const digest = record.sha256;
  const short = "#" + String(record.id).slice(0, 8);
  if (!digest) return $("code", "", short);
  const panel = $("div", "reference-fingerprint");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "完整 SHA-256");
  const announcement = $("span", "fingerprint-notice");
  announcement.setAttribute("role", "status");
  const copy = button("复制", async () => {
    try {
      await navigator.clipboard.writeText(digest);
      trigger.close(true);
      announcement.textContent = "已复制";
      clearTimeout(noticeTimer);
      noticeTimer = setTimeout(() => { announcement.textContent = ""; }, 1600);
    } catch {
      window.getSelection()?.selectAllChildren(panel.querySelector("code"));
    }
  }, "ghost");
  panel.append($("code", "", digest), copy);
  const trigger = popover(short, panel, { appearance: "ghost mono reference-id", caret: false, offset: 4 });
  trigger.element.classList.add("reference-anchor");
  trigger.element.append(announcement);
  trigger.trigger.setAttribute("aria-label", "文献 " + short + "，显示完整 SHA-256");
  let dismiss, noticeTimer;
  let pinned = false;
  panel.addEventListener("beforetoggle", ({ newState }) => { if (newState === "closed") pinned = false; });
  trigger.trigger.addEventListener("click", (event) => {
    if (panel.matches(":popover-open") && !pinned) {
      event.preventDefault();
      event.stopImmediatePropagation();
      pinned = true;
      return;
    }
    pinned = !pinned;
  }, { capture: true });
  function leave() {
    clearTimeout(dismiss);
    dismiss = setTimeout(() => {
      if (!pinned && !panel.matches(":hover, :focus-within") && !trigger.trigger.matches(":hover")) trigger.close();
    }, 230);
  }
  trigger.trigger.addEventListener("mouseenter", () => { clearTimeout(dismiss); trigger.open(); });
  trigger.trigger.addEventListener("mouseleave", leave);
  panel.addEventListener("mouseenter", () => clearTimeout(dismiss));
  panel.addEventListener("mouseleave", leave);
  return trigger.element;
}

function businessEditor(ctx, task) {
  const form = $("form", "business-editor");
  const inputFor = (name, label, numeric = false) => {
    const item = $("label", "field");
    const input = $("input");
    Object.assign(input, { name, required: true, type: numeric ? "number" : "text" });
    if (numeric) Object.assign(input, { min: "1", max: name === "necessity" ? "5" : "100000" });
    if (!numeric) input.maxLength = name === "references" ? 1030 : 500;
    item.append($("span", "", label), input);
    return item;
  };
  const fields = $("div", "business-fields");
  fields.append(inputFor("necessity", "岗位刚需度（1–5）", true), inputFor("expert_minutes", "专家平均耗时（分钟）", true),
    inputFor("frequency", "岗位工作频率"), inputFor("loss", "实际错误损失"), inputFor("handoff", "下游接收环节"),
    inputFor("references", "已核验文献 ID（逗号分隔）"));
  const save = button("保存价值与证据", null, "primary");
  save.type = "submit";
  save.disabled = true;
  const note = $("p", "meta");
  note.setAttribute("role", "status");
  form.append($("h3", "", "价值评估"), fields, actions(save, note));
  const query = new URLSearchParams({ domain: ctx.data.domain.id, task: task.id });
  request("/api/workbench/task-value?" + query).then((value) => {
    const existing = (task.details?.references || []).map((item) => item.id).filter(Boolean);
    for (const key of ["necessity", "expert_minutes", "frequency", "loss", "handoff"]) {
      form.elements[key].value = value[key] ?? (key === "handoff" ? task.details?.workflow?.recipient || "" : "");
    }
    form.elements.references.value = (value.references || existing).join(", ");
    save.disabled = false;
  }).catch((error) => { note.textContent = error.message; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      const data = Object.fromEntries(new FormData(form));
      data.necessity = Number(data.necessity);
      data.expert_minutes = Number(data.expert_minutes);
      data.references = data.references.split(/[,，、\s]+/).filter(Boolean);
      await post("/api/workbench/task-value", { ...data, domain_id: ctx.data.domain.id, task: task.id });
      note.textContent = "已保存";
    } catch (error) { note.textContent = error.message; }
    finally { save.disabled = false; }
  });
  return form;
}

export function tasksView(ctx) {
  const content = $("div");
  const header = heading("构题工坊");
  const page = column(header, content);
  content.append($("p", "muted", "正在读取题目…"));
  taskData(ctx, true).then(({ items: rows, admission }) => {
    if (!page.isConnected) return;
    if (!rows.length) {
      content.replaceChildren(ctx.item
        ? empty("task", "当前领域没有此题目", "", actions(href("查看全部题目", ctx.route("tasks"), "button")))
        : empty("task", "从一项真实工业任务开始", "沿已确认的工艺交付关系，使用真实样本和技术条款构建题目。",
          taskControls(ctx, admission)));
      return;
    }
    header.append(taskControls(ctx, admission));
    const stage = (row) => row.details?.stage || row.details?.workflow?.stage || "";
    const readiness = (row) => {
      if (row.verifier?.is_stale) return "待同步";
      if (row.verifier?.ready || (!row.scoped && ["offline_verified", "verified", "passed"].includes(row.status))) return "已就绪";
      if (row.verifier || row.details?.verifier) return "待验收";
      return "待配置";
    };
    const complexity = (row) => row.details?.difficulty || "";
    const filters = $("div", "task-filters");
    const list = $("div", "task-list");
    const search = $("input");
    search.type = "search";
    search.placeholder = "搜索题目、工序或编号";
    search.value = ctx.query || "";
    search.setAttribute("aria-label", "查找题目");
    filters.append(search);
    const choices = [
      ["工艺环节", rows.map(stage), stage], ["预估难度", rows.map(complexity), complexity],
      ["验证状态", rows.map(readiness), readiness],
    ].map(([label, values, selectValue]) => {
      const options = [...new Set(values.filter(Boolean))].map((value) => [value, value]);
      const select = popoverSelect(label, [["", "全部" + label], ...options]);
      filters.append(select.element);
      select.element.addEventListener("change", paint);
      return [select, selectValue];
    });
    const open = (row) => taskDialog(ctx, row, admission, businessEditor);
    content.replaceChildren(...(ctx.item ? [actions(href("查看全部题目", ctx.route("tasks"), "button ghost"))] : []), filters, list);
    search.addEventListener("input", paint);
    paint();
    if (ctx.item && rows[0]) open(rows[0]);

    function paint() {
      const needle = search.value.trim().toLocaleLowerCase();
      const found = rows.filter((row) => [row.title, row.id, taskNumber(row), stage(row)].join(" ").toLocaleLowerCase().includes(needle) &&
        choices.every(([control, selectValue]) => !control.value || control.value === selectValue(row)));
      if (!found.length) {
        list.replaceChildren(empty("search", "没有匹配的题目", "", actions(button("清除筛选", () => {
          search.value = "";
          for (const [select] of choices) select.value = "";
          paint();
        }))));
        return;
      }
      const table = gridTable(["题目编号与名称", "所属工序", "岗位价值", "下游交付", "预估难度", "Verifier 状态", "详情"],
        found.map((row) => {
          const status = readiness(row);
          const caption = actions(recordText(taskNumber(row), "meta mono"),
            ...(!row.quality_approved || row.is_stale ? [
              $("span", "badge warning", row.is_stale ? "待同步" : "历史 · 待复核"),
            ] : []));
          caption.classList.add("cell-caption");
          return [column(recordText(row.title), caption), recordText(stage(row) || "待登记"),
            valueSummary(row), recordText(row.value?.downstream || row.value?.handoff || row.details?.workflow?.recipient || "待登记"),
            $("span", "badge", complexity(row) || "待定"),
            $("span", "badge " + (status === "已就绪" ? "success" : status === "待同步" ? "warning" : ""), status),
            button("查看详情", () => open(row), "ghost record-action")];
        }), "tasks-table record-table");
      bindRecordRows(table, found, open);
      list.replaceChildren(table);
    }
  }).catch((error) => content.replaceChildren($("p", "inline-error", error.message)));
  return page;
}

const layers = [
  ["交付物完备性", "文件、字段、版本及格式契约", ["文件是否齐全", "数据结构与版本一致性"]],
  ["规范静态检查", "适用条款、容差与工程规则", ["依据版本", "检查条件与失败原因"]],
  ["物理／电路仿真", "工况边界、仿真与功能结果", ["配置适用工况", "核对功能判定条件"]],
  ["验收倒推与下道接口", "下道工序接收、放行与交接", ["核对接收接口", "保留可追溯的验收结论"]],
];

export function verifierView(ctx) {
  const domain = ctx.data.domain.id;
  const endpoint = "/api/workbench/verifier";
  const panel = $("div", "verifier");
  const tree = $("nav", "layer-tree");
  tree.setAttribute("aria-label", "验证层级");
  const right = $("div", "verifier-editor");
  const code = $("section", "code-panel");
  const codeTitle = $("div", "code-header");
  const codeName = $("span", "mono");
  const format = $("div", "verifier-formats");
  codeTitle.append(codeName, format);
  const rules = $("textarea", "rule-editor");
  rules.spellcheck = false;
  rules.wrap = "off";
  const result = $("div", "sandbox-results");
  const settings = $("div", "verifier-configuration");
  const bindingNote = $("p", "meta verifier-binding-note");
  bindingNote.setAttribute("role", "status");
  const info = $("p", "muted");
  info.setAttribute("role", "status");
  const image = $("input");
  image.placeholder = "kicad/kicad:9.0";
  image.setAttribute("aria-label", "Docker 工具链镜像");
  const imageStatus = $("span", "meta");
  imageStatus.setAttribute("role", "status");
  const inspect = button("检测镜像", async () => {
    inspect.disabled = true;
    imageStatus.textContent = "正在检查本机镜像…";
    const checked = image.value.trim();
    try {
      await post(endpoint, { action: "inspect_image", image: checked });
      if (checked === image.value.trim()) imageStatus.textContent = "本机镜像可用";
    } catch (error) {
      if (checked === image.value.trim()) imageStatus.textContent = error.message;
    } finally { inspect.disabled = false; }
  }, "outline");
  image.addEventListener("input", () => { imageStatus.textContent = ""; });
  const strategy = popoverSelect("验证策略", [["acceptance_backwards", "验收倒推策略"], ["progressive", "分层渐进策略"]]);
  const binding = popoverSelect("当前绑定题目", [["", "工序通用验证模板"]], { placeholder: "选择绑定题目" });
  const fixtures = $("details", "fold verifier-fixtures");
  const mounts = $("div", "fixture-mounts");
  fixtures.append($("summary", "", "挂载测试工程样本"), mounts);
  const synopsis = $("div", "sandbox-environment");
  synopsis.append($("span", "field-label", "沙箱镜像"), image, inspect, imageStatus);
  const targetContext = $("div", "verifier-context");
  const bindingLabel = actions($("span", "field-label", "当前绑定题目"), targetContext);
  bindingLabel.classList.add("verifier-binding-label");
  settings.append(column(bindingLabel, binding.element), strategy.element);
  const toolbar = $("div", "verifier-toolbar");
  toolbar.append(settings, bindingNote);
  const page = column(heading("验证器工程", "按题目管理 L1–L4 规则与正反样本。"), toolbar, panel);
  code.append(codeTitle, rules);
  right.append(code, synopsis, fixtures);
  panel.append(tree, right);

  let snapshot = null;
  let target = "";
  let choice = Math.min(ctx.viewState.layer || 0, layers.length - 1);
  let language = "json";
  let busy = false;
  let bindingOptions = null;
  let refreshingBindings = false;
  const drafts = new Map();
  const mounted = new Map();
  const variants = [["standard", "正向标准件"], ["incomplete", "负向缺陷件"]];
  const formats = [["json", "规则 JSON"], ["python", "Python"], ["shell", "Shell"]];
  const controls = actions();
  const saveControl = button("保存验证配置", save, "primary");
  const trials = variants.map(([key, label]) => button("用" + label + "试跑", () => run(key)));
  controls.append(saveControl, ...trials);
  right.append(controls, info, result);

  function interactive() {
    binding.disabled = busy || bindingOptions === null;
    rules.disabled = !snapshot || busy;
    image.disabled = !snapshot || busy;
    strategy.disabled = !snapshot || busy;
    saveControl.disabled = !snapshot || busy;
    for (const button of trials) button.disabled = !snapshot?.version || busy;
    for (const button of mounts.querySelectorAll("button")) button.disabled = !snapshot || busy;
  }

  function show(index) {
    choice = ctx.viewState.layer = index;
    for (const [i, item] of Array.from(tree.children).entries()) item.setAttribute("aria-pressed", String(i === index));
    const key = "L" + (index + 1);
    const extension = { json: "json", python: "py", shell: "sh" }[language];
    codeName.textContent = key + "." + extension;
    rules.setAttribute("aria-label", key + " " + formats.find(([name]) => name === language)[1]);
    const raw = drafts.get(key) ?? JSON.stringify(snapshot?.spec.layers[key] || [], null, 2);
    if (language === "json") {
      rules.value = raw;
      rules.placeholder = "";
      return;
    }
    try {
      rules.value = JSON.parse(raw).find((entry) => entry.language === language)?.script || "";
      rules.placeholder = language === "python" ? "# 退出码 0 表示通过，非 0 表示失败" : "# 使用退出码报告试跑结果";
    } catch {
      language = "json";
      info.textContent = "请先修正该层的规则 JSON。";
      show(index);
    }
  }

  function renderFormats() {
    format.replaceChildren(segments("验证规则编辑模式", formats, language, (next) => {
      language = next;
      show(choice);
      renderFormats();
      format.querySelector('[aria-pressed="true"]')?.focus();
    }));
  }

  rules.addEventListener("input", () => {
    const key = "L" + (choice + 1);
    if (language === "json") {
      drafts.set(key, rules.value);
      return;
    }
    const entries = JSON.parse(drafts.get(key) ?? JSON.stringify(snapshot.spec.layers[key]));
    const index = entries.findIndex((entry) => entry.language === language);
    if (rules.value.trim()) {
      const entry = { title: entries[index]?.title || key + (language === "python" ? " Python 检查" : " Shell 检查"),
        language, script: rules.value };
      if (index < 0) entries.push(entry);
      else entries[index] = entry;
    } else if (index >= 0) entries.splice(index, 1);
    drafts.set(key, JSON.stringify(entries, null, 2));
  });

  function drawMounts() {
    mounts.replaceChildren();
    for (const [key, label] of variants) {
      const section = $("section", "fixture-mount");
      const paths = $("ul", "fixture-paths");
      const files = mounted.get(key) || snapshot?.spec.fixtures[key] || {};
      for (const [name, metadata] of Object.entries(files)) {
        const row = $("li");
        const archive = metadata?.archive || metadata instanceof File && name.toLowerCase().endsWith(".zip");
        row.append($("code", "mono", archive ? name : "/work/" + name));
        if (archive) row.append($("span", "meta", "解包到 /work"));
        row.append(iconButton("取消挂载 " + name, () => {
          const { [name]: removed, ...remaining } = files;
          mounted.set(key, remaining);
          drawMounts();
        }, "close"));
        paths.append(row);
      }
      const picker = (label, folder) => {
        const input = $("input");
        Object.assign(input, { type: "file", multiple: true, hidden: true });
        if (folder) input.setAttribute("webkitdirectory", "");
        const select = button(label, () => input.click(), "outline");
        input.addEventListener("change", () => {
          const picks = Array.from(input.files).map((file) => ({ file, name: file.webkitRelativePath || file.name }));
          mountFiles(key, picks);
        });
        return [select, input];
      };
      section.append($("h3", "", label), $("p", "meta", "独立挂载到 /work"),
        actions(...picker("选择文件或 ZIP", false), ...picker("选择目录", true)));
      if (paths.children.length) section.append(paths);
      mounts.append(section);
    }
    interactive();
  }

  function mountFiles(key, picks) {
    if (!snapshot || busy || !picks.length) return;
    const total = picks.reduce((size, { file }) => size + file.size, 0);
    const limit = 16 * 1024 * 1024;
    const existing = mounted.get(key) || snapshot.spec.fixtures[key];
    if (new Set([...Object.keys(existing), ...picks.map(({ name }) => name)]).size > 128 || total > limit) {
      info.textContent = "每组最多挂载 128 个文件，单批不超过 16 MiB。";
      return;
    }
    mounted.set(key, { ...existing, ...Object.fromEntries(picks.map(({ name, file }) => [name, file])) });
    info.textContent = "保存配置后可试跑。";
    drawMounts();
  }

  function drawResults() {
    result.replaceChildren();
    for (const [key, label] of variants) {
      const test = snapshot.tests[key];
      if (!test) continue;
      const entries = Object.entries(test.checks).flatMap(([layer, rows]) => rows.map((row) => [
        $("span", "mono", layer), row.title, stateBadge(row.passed ? "passed" : "failed"),
        $("span", "mono", row.latency_ms + " 毫秒"),
      ]));
      result.append(column($("h3", "", label + (test.expected ? "符合预期" : "偏离预期")),
        gridTable(["层级", "检查项", "结果", "耗时"], entries)));
    }
  }

  async function load(next) {
    target = next;
    snapshot = null;
    drafts.clear();
    mounted.clear();
    busy = true;
    interactive();
    drawMounts();
    show(choice);
    renderFormats();
    targetContext.replaceChildren();
    info.textContent = "正在读取验证器…";
    result.replaceChildren();
    try {
      snapshot = await request(endpoint + "?" + new URLSearchParams({ domain, task: target }));
      image.value = snapshot.spec.image;
      imageStatus.textContent = "";
      strategy.value = snapshot.spec.strategy;
      if (snapshot.is_stale) targetContext.append($("span", "badge warning", "上游已更新 · 待同步"));
      if (snapshot.version) targetContext.append($("code", "meta mono", "v" + snapshot.version));
      drawResults();
      info.textContent = "";
    } catch (error) { info.textContent = error.message; }
    finally {
      busy = false;
      drawMounts();
      show(choice);
      renderFormats();
    }
  }

  async function save() {
    if (!snapshot || busy) return;
    busy = true;
    interactive();
    try {
      const checks = Object.fromEntries(layers.map((_, index) => {
        const key = "L" + (index + 1);
        return [key, drafts.has(key) ? JSON.parse(drafts.get(key)) : snapshot.spec.layers[key]];
      }));
      const fixtures = { ...snapshot.spec.fixtures };
      for (const [key, files] of mounted) {
        const entries = new Map();
        for (const [name, file] of Object.entries(files)) {
          if (file instanceof File) {
            info.textContent = "正在挂载 " + name;
            entries.set(name, await request("/api/workbench/verifier-sample?" + new URLSearchParams({ domain, name }), {
              method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
            }));
          } else entries.set(name, file);
        }
        fixtures[key] = Object.fromEntries(entries);
      }
      const spec = { image: image.value.trim(), strategy: strategy.value, fixtures, layers: checks };
      snapshot = await post(endpoint, { domain_id: domain, task: target, version: snapshot.version, spec });
      drafts.clear();
      mounted.clear();
      targetContext.replaceChildren($("code", "meta mono", "v" + snapshot.version));
      info.textContent = "已保存验证配置";
      drawResults();
      show(choice);
    } catch (error) { info.textContent = error.message; }
    finally { busy = false; drawMounts(); }
  }

  async function run(variant) {
    if (!snapshot || busy) return;
    if (drafts.size || mounted.size || snapshot.is_stale || image.value.trim() !== snapshot.spec.image || strategy.value !== snapshot.spec.strategy) {
      info.textContent = "请先保存要试跑的版本。";
      return;
    }
    busy = true;
    interactive();
    info.textContent = "正在执行已保存的规则版本…";
    try {
      snapshot.tests[variant] = await post(endpoint, { domain_id: domain, task: target, version: snapshot.version,
        action: "trial", variant });
      drawResults();
      info.textContent = "试跑结束";
    } catch (error) { info.textContent = error.message; }
    finally { busy = false; interactive(); }
  }

  for (const [i, [title, description]] of layers.entries()) {
    const item = button("", () => { show(i); renderFormats(); }, "layer-button");
    item.append($("span", "badge mono", "L" + (i + 1)), column($("strong", "", title), $("small", "meta", description)));
    tree.append(item);
  }
  binding.element.addEventListener("change", () => {
    if ((drafts.size || mounted.size || (snapshot && (image.value.trim() !== snapshot.spec.image ||
        strategy.value !== snapshot.spec.strategy))) && !window.confirm("切换绑定会丢弃未保存的验证配置，继续切换？")) {
      binding.value = target;
      return;
    }
    ctx.viewState.verifierTask = binding.value;
    load(binding.value);
  });

  async function syncBindings() {
    if (refreshingBindings) return;
    refreshingBindings = true;
    try {
      const query = new URLSearchParams({ domain, view: "bindings" });
      const { items } = await request("/api/workbench/scope-tasks?" + query);
      if (!page.isConnected) return;
      const first = bindingOptions === null;
      const selected = first ? ctx.item || ctx.viewState.verifierTask || "" : binding.value;
      const missing = selected && !items.some(({ id }) => id === selected);
      const options = [["", "工序通用验证模板"], ...items.map((row) => [row.id, taskNumber(row) + " · " + row.title])];
      if (missing && !first) options.push([selected, "当前绑定已不可用"]);
      const key = JSON.stringify(options);
      if (key === bindingOptions) return;
      bindingOptions = key;
      binding.setOptions(options, selected);
      bindingNote.replaceChildren();
      if (missing && (!first || ctx.item)) bindingNote.textContent = "当前绑定题目已不可用，请选择其他题目。";
      else if (!items.length) bindingNote.append("当前领域暂无可绑定的题目。", href("前往构题工坊", ctx.route("tasks"), "button ghost size-sm"));
      if (first) load(binding.value);
    } catch (error) {
      if (bindingOptions === null) bindingNote.textContent = "暂未读取到题目列表。" + error.message;
    } finally { refreshingBindings = false; }
  }

  show(choice);
  renderFormats();
  interactive();
  binding.trigger.addEventListener("click", syncBindings);
  ctx.onRefresh(syncBindings);
  syncBindings();
  return page;
}

const object = (value) => value != null && typeof value === "object" && !Array.isArray(value);

function mergeJson(base, changes) {
  return Object.fromEntries([...new Set([...Object.keys(base), ...Object.keys(changes)])].map((key) => [
    key, Object.hasOwn(changes, key)
      ? (object(base[key]) && object(changes[key]) ? mergeJson(base[key], changes[key]) : changes[key])
      : base[key],
  ]));
}

const exportColumns = [
  ["id", "编号"], ["title", "标题"], ["description", "题面"], ["inputs", "输入"],
  ["references", "参考文献"], ["engineering", "工程工件与条款"], ["verifier", "验证器"], ["evaluations", "评测结果"],
];

function schemaFieldControls(textarea, inherited) {
  const details = $("details", "schema-fields");
  const rows = $("div", "schema-field-list");
  const controls = exportColumns.map(([key, title]) => {
    const row = $("div");
    const label = $("label");
    const enabled = $("input");
    const name = $("input", "mono");
    enabled.type = "checkbox";
    name.maxLength = 100;
    name.setAttribute("aria-label", title + "导出字段名");
    label.append(enabled, title);
    row.append(label, name);
    rows.append(row);
    function change() {
      try {
        const draft = JSON.parse(textarea.value);
        if (!object(draft)) return;
        const mapping = object(draft["x-export-fields"]) ? draft["x-export-fields"] : {};
        mapping[key] = enabled.checked ? name.value.trim() || key : false;
        draft["x-export-fields"] = mapping;
        textarea.value = JSON.stringify(draft, null, 2);
        textarea.dispatchEvent(new Event("input"));
      } catch {}
    }
    enabled.addEventListener("change", change);
    name.addEventListener("change", change);
    return { key, enabled, name };
  });
  function sync() {
    try {
      const draft = JSON.parse(textarea.value);
      if (!object(draft)) throw new Error();
      const mapping = { ...inherited(), ...draft["x-export-fields"] };
      for (const { key, enabled, name } of controls) {
        enabled.checked = mapping[key] !== false;
        enabled.disabled = false;
        name.disabled = !enabled.checked;
        name.value = typeof mapping[key] === "string" ? mapping[key] : key;
      }
    } catch {
      for (const { enabled, name } of controls) enabled.disabled = name.disabled = true;
    }
  }
  details.append($("summary", "", "字段勾选与映射"), rows);
  return { details, sync };
}

export function schemaView(ctx) {
  const body = $("div");
  const page = column(heading("默认导出格式", "", settingsTabs(ctx)), body);
  renderSchema(ctx, body);
  return page;
}

function renderSchema(ctx, panel) {
  const domain = ctx.data.domain?.id;
  const scopes = domain ? ["global", "domain"] : ["global"];
  const editors = $("div", "schema-grid");
  const preview = $("section", "code-panel schema-preview");
  const pre = $("pre", "code-view");
  const label = $("div", "code-header");
  const download = button("下载 JSON Schema", () => {
    const blob = new Blob([JSON.stringify(combined, null, 2) + "\n"], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = href("", url);
    link.download = domain ? "domain-" + domain + ".schema.json" : "default.schema.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }, "", "download");
  download.disabled = true;
  label.append($("span", "", domain ? "合并后预览" : "格式预览"), download);
  preview.append(label, pre);
  const message = $("p", "schema-note", "对象字段按层合并，数组与单值使用领域覆写。");
  const feedback = $("p", "inline-error");
  feedback.setAttribute("role", "status");
  feedback.hidden = true;
  let combined;
  panel.append(...(domain ? [message] : []), editors, feedback, preview);
  function validate(text) {
    const value = JSON.parse(text);
    if (!object(value)) throw new Error("格式须为 JSON 对象。");
    return value;
  }
  request("/api/workbench/schema" + (domain ? "?domain=" + domain : "")).then((result) => {
    const fields = {};
    const fieldControls = [];
    function update(showError = true) {
      try {
        combined = mergeJson(validate(fields.global.value), fields.domain ? validate(fields.domain.value) : {});
        if (combined.type !== "object") throw new Error("题包格式须声明 object 类型。");
        jsonCode(pre, combined);
        download.disabled = false;
        if (showError) feedback.hidden = true;
      } catch (error) {
        download.disabled = true;
        pre.textContent = "等待有效 JSON";
        feedback.className = "inline-error";
        feedback.textContent = error.message;
        feedback.hidden = false;
      }
    }
    for (const key of scopes) {
      const text = key === "global" ? "全局默认格式" : "领域专属覆写";
      const value = key === "global" ? result.global : result.override;
      const section = $("section", "schema-editor");
      const header = $("header");
      const textarea = $("textarea");
      textarea.spellcheck = false;
      textarea.setAttribute("aria-label", text);
      const draftKey = "schema-" + key;
      textarea.value = ctx.viewState[draftKey] ?? JSON.stringify(value, null, 2);
      fields[key] = textarea;
      textarea.addEventListener("input", () => {
        ctx.viewState[draftKey] = textarea.value;
        for (const control of fieldControls) control.sync();
        update();
      });
      const save = button("保存", async () => {
        save.disabled = true;
        try {
          const schema = validate(textarea.value);
          await post("/api/workbench/schema", { domain_id: domain, scope: key, schema });
          delete ctx.viewState[draftKey];
          feedback.className = "muted";
          feedback.textContent = text + "已保存";
          feedback.hidden = false;
          update(false);
        } catch (error) {
          feedback.className = "inline-error";
          feedback.textContent = error.message;
          feedback.hidden = false;
        } finally {
          save.disabled = false;
        }
      });
      const control = schemaFieldControls(textarea, () =>
        key === "domain" ? validate(fields.global.value)["x-export-fields"] : {});
      fieldControls.push(control);
      control.sync();
      header.append($("h2", "", text), save);
      section.append(header, textarea, control.details);
      editors.append(section);
    }
    update();
  }).catch((error) => {
    feedback.textContent = error.message;
    feedback.hidden = false;
  });
}

function calibrationCell(evaluations, tier, k) {
  const records = evaluations.filter((item) => item.tier === tier);
  const trials = records.filter((item) => item.status === "scored" && typeof item.passed === "boolean");
  if (!trials.length) return $("span", "eval-missing", records.length ? "接口异常" : "待测");
  const speakers = new Map();
  for (const trial of trials) {
    const key = trial.profile_id || trial.profile || trial.model;
    if (!speakers.has(key)) speakers.set(key, []);
    speakers.get(key).push(trial);
  }
  const estimate = [...speakers.values()].filter((list) => list.length >= k).map((list) => {
    const n = list.length;
    const missed = list.filter((trial) => !trial.passed).length;
    let probability = 1;
    for (let index = 0; index < k; index++) probability *= Math.max(0, missed - index) / (n - index);
    return 1 - probability;
  });
  const result = column($("strong", "mono", estimate.length
    ? Math.round(100 * estimate.reduce((sum, value) => sum + value, 0) / estimate.length) + "%" : "样本不足"),
    $("p", "meta", estimate.length + " 个模型 · " + trials.length + " 个有效试次"));
  const heat = $("div", "layer-heat");
  for (const name of ["L1", "L2", "L3", "L4"]) {
    const observed = trials.flatMap((item) => item.checks || []).filter((row) =>
      String(row.layer || row.id || "").toUpperCase() === name && typeof row.passed === "boolean");
    if (!observed.length) continue;
    const failed = observed.filter((item) => !item.passed).length;
    const marker = $("span", "badge " + (failed ? "danger" : "success"), name + " " + failed + "/" + observed.length);
    marker.title = "失败检查次数 / 已检查次数";
    heat.append(marker);
  }
  if (heat.children.length) result.append(heat);
  if (trials.length < records.length) result.append($("p", "meta", "另有接口异常"));
  return result;
}

async function downloadDomain(id, kind) {
  const response = await fetch("/api/workbench/export?" + new URLSearchParams({ domain: id, format: kind }));
  if (!response.ok) throw new Error((await response.json()).error || "导出失败");
  const url = URL.createObjectURL(await response.blob());
  const link = href("", url);
  link.download = "domain-" + id + "-benchmark." + kind;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function renderCalibration(ctx, panel) {
  const action = () => href("配置模型评测", ctx.route("export/evaluate"), "button primary", "chart");
  const pending = $("p", "muted", "正在读取评测结果…");
  panel.append(pending);
  try {
    const [rows, { profiles }] = await Promise.all([taskData(ctx), request("/api/model-profiles")]);
    if (!rows.length) {
      panel.replaceChildren(empty("chart", "还没有可评测的题目", "先准备带验收规则的题包，再对同一题进行多档模型比较。",
        actions(href("前往构题工坊", ctx.route("tasks"), "button primary"))));
      return;
    }
    const title = $("div", "eval-toolbar actions");
    const pass = popoverSelect("通过率指标", [1, 2, 3].map((k) => [k, "Pass@" + k]));
    const matrix = $("div");
    const exportable = rows.filter((row) => row.exportable);
    title.append(action(), pass.element);
    if (exportable.length) {
      const feedback = $("span", "meta");
      for (const [format, label] of [["jsonl", "JSONL"], ["parquet", "Parquet"]]) {
        const download = button("导出领域 " + label, async () => {
          download.disabled = true;
          feedback.textContent = "正在打包";
          try {
            await downloadDomain(ctx.data.domain.id, format);
            feedback.textContent = "下载已开始";
          } catch (error) { feedback.textContent = error.message; }
          finally { download.disabled = false; }
        }, "", "download");
        title.append(download);
      }
      title.append(feedback);
    }
    const modelColumns = Object.entries(tierNames).map(([tier, label]) => {
      const names = [...new Set(profiles.filter((profile) => profile.tier === tier && profileUsable(profile))
        .map(({ model }) => model))];
      return column($("span", "", label), ...names.map((name) => $("code", "meta eval-model", name)));
    });
    function paint() {
      matrix.replaceChildren(gridTable(["题目", ...modelColumns, "实测分档", "题包"], rows.map((row) => {
        const data = row.details?.evaluations || [];
        const scored = data.filter((trial) => trial.status === "scored" && Object.hasOwn(tierNames, trial.tier));
        const uniform = new Set(scored.map((trial) => trial.tier)).size === 3 && scored.every((trial) => trial.passed);
        const title = row.scoped ? column($("span", "", row.title),
          ...(row.evaluation?.is_stale ? [staleTag(ctx, { ...row.evaluation, title: row.title },
            "STAGE_6_EVAL_EXPORT", { task_id: row.id })] : [])) : href(row.title, ctx.route("export/run/" + row.run_id), "button");
        const pack = row.exportable && row.run_id
          ? href("导出题包", "/api/runs/" + row.run_id + "/benchmark", "button outline", "download")
          : row.exportable ? stateBadge("ready") : $("span", "badge", "未生成");
        return [title,
          ...Object.keys(tierNames).map((tier) => calibrationCell(data, tier, Number(pass.value))),
          column($("span", "meta", row.details?.measured_difficulty || "待校准"),
            ...(uniform ? [$("span", "badge warning", "全部通过，区分度待复核")] : [])),
          pack];
      }), "eval-matrix"));
    }
    pass.element.addEventListener("change", paint);
    paint();
    panel.replaceChildren(title, matrix);
  } catch (error) { panel.replaceChildren($("p", "inline-error", error.message)); }
}

function evaluationRecord(ctx) {
  const back = href("查看领域评测", ctx.route("export"), "button ghost");
  const panel = $("div");
  const page = column(heading("评测记录", "", back), panel);
  const params = new URLSearchParams({ domain: ctx.data.domain.id, item: ctx.item });
  panel.append($("p", "meta", "正在读取评测…"));
  request("/api/workbench/asset-evaluation?" + params).then((record) => {
    const stats = actions(stateBadge(record.is_stale ? "stale" : record.status), $("code", "meta", "Pass@" + record.pass_at_k),
      $("code", "meta", "Verifier v" + record.verifier_version));
    panel.replaceChildren(heading(record.title, record.confirmed_at, stats),
      gridTable(["模型", "评测梯度", "结果", "记录"], record.trials.map((trial) => [
        trial.model || trial.profile || "", tierNames[trial.tier] || "未指定",
        stateBadge(trial.status === "scored" ? trial.passed ? "passed" : "failed" : trial.status), trial.reason || "",
      ])),
      href("查看题目", ctx.route("tasks", ctx.data.domain.id, { item: record.task_id }), "button ghost"));
  }).catch((error) => panel.replaceChildren($("p", "inline-error", error.message)));
  return page;
}

export function exportView(ctx) {
  if (ctx.item) return evaluationRecord(ctx);
  const controls = $("div");
  const panel = $("div");
  const page = column(heading("评测与导出", "比较多档模型结果，管理可复用的题包格式。", controls), panel);
  function show() {
    const selected = ctx.viewState.exportTab || "eval";
    controls.replaceChildren(segments("评测与导出视图", [
      ["eval", "多档模型梯度评测"], ["schema", "输出格式 Schema 与导出"],
    ], selected, (value) => {
      ctx.viewState.exportTab = value;
      show();
      controls.querySelector('[aria-pressed="true"]').focus();
    }));
    const body = $("div");
    panel.replaceChildren(body);
    if (selected === "schema") renderSchema(ctx, body);
    else renderCalibration(ctx, body);
  }
  show();
  return page;
}

export function settingsView(ctx) {
  const models = $("section", "gateway-section");
  const preferences = $("section", "gateway-section");
  const runtime = $("section", "gateway-section");
  const approvals = $("section", "gateway-section");
  const sections = $("div", "gateway-sections");
  let currentCatalog;
  const add = button("添加服务商", () => ctx.openProvider(currentCatalog, {}, loadModels), "primary", "plus");
  const status = (text) => {
    const node = $("p", "muted", text);
    node.setAttribute("role", "status");
    return node;
  };
  const sectionHeading = (title, action) => {
    const header = $("header", "gateway-heading");
    header.append($("h2", "", title));
    if (action) header.append(action);
    return header;
  };
  sections.append(models, preferences, runtime, approvals);

  function menu(label, contents) {
    const panel = $("div", "connection-menu");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", label);
    const choice = popover("", panel, { symbol: "more", caret: false, align: "end", appearance: "ghost connection-more" });
    choice.trigger.setAttribute("aria-label", label);
    choice.trigger.title = label;
    panel.append(...contents(() => choice.close(true)));
    return choice.element;
  }

  function subscriptionCard(initial, prefs, report) {
    const card = $("article", "connection-card codex-connection-card");
    const installCommand = "npm install -g @openai/codex@latest";
    let native = initial;
    let busy = false;
    let loginURL = "";
    let pollTimer;

    async function save(changes) {
      busy = true;
      render();
      try {
        const { preferences } = await post("/api/settings", changes);
        Object.assign(prefs, preferences);
        report("已保存");
        await ctx.reloadConfig();
      } catch (error) { report(error.message, true); }
      finally { busy = false; render(); }
    }

    async function detect(refresh) {
      if (busy || native.login_pending) return;
      busy = true;
      render();
      try {
        native = await post("/api/codex-local", { action: "detect", refresh });
        await ctx.reloadConfig();
      } catch (error) {
        native = { status: "probe_failed", models: [], message: error.message };
      }
      finally { busy = false; render(); }
    }

    function poll() {
      clearTimeout(pollTimer);
      if (!native.login_pending) return;
      pollTimer = setTimeout(async () => {
        if (!card.isConnected) return;
        try {
          native = await request("/api/codex-local");
          if (!native.login_pending) {
            loginURL = "";
            await ctx.reloadConfig();
            if (native.status !== "ready_subscription") report("登录尚未完成，可重新检测或在 CLI 中登录。");
          }
          render();
          poll();
        } catch (error) { report(error.message, true); }
      }, 1500);
    }

    async function login() {
      const tab = window.open("about:blank", "_blank");
      if (tab) tab.opener = null;
      busy = true;
      render();
      try {
        const { auth_url: url } = await post("/api/codex-local", { action: "login" });
        loginURL = url;
        if (tab) tab.location.replace(url);
        native = { ...native, login_pending: true };
        poll();
      } catch (error) { tab?.close(); report(error.message, true); }
      finally { busy = false; render(); }
    }

    function chooseBinary() {
      const input = $("input");
      input.name = "path";
      input.value = prefs.codex_executable;
      input.placeholder = "自动选择已安装的最高版本";
      input.spellcheck = false;
      const field = $("label", "field wide");
      field.append($("span", "field-label", "Codex CLI 可执行文件的绝对路径"), input);
      ctx.editor("Codex CLI 路径", [field], "保存", async ({ path }) => {
        const { preferences } = await post("/api/settings", { codex_executable: path.trim() });
        Object.assign(prefs, preferences);
        await detect(true);
      });
    }

    function render() {
      const title = $("div", "connection-identity");
      title.append(icon("terminal"), $("h3", "", "Codex 本地订阅"), $("span", "protocol-label", "复用 CLI / Desktop"));
      const identity = $("div", "connection-meta");
      identity.append(title);
      const refresh = button(busy ? "正在检测…" : "重新检测", () => detect(true), "ghost codex-compact");
      const configure = iconButton("Codex CLI 路径", chooseBinary, "settings", "ghost codex-compact");
      refresh.disabled = configure.disabled = busy || native.login_pending;
      const controls = actions(nativeStatus(native.status, native.login_pending ? "等待浏览器登录" : undefined), refresh, configure);
      if (native.status === "unauthenticated" && !native.login_pending) {
        const signIn = button("登录 ChatGPT", login, "primary codex-compact");
        signIn.disabled = busy;
        controls.append(signIn);
      }
      if (loginURL && native.login_pending) {
        const link = href("继续登录", loginURL, "button ghost codex-compact");
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        controls.append(link);
      }
      const header = $("header", "connection-header");
      header.append(identity, controls);
      const summary = $("div", "codex-connection-summary meta");
      if (native.status === "ready_subscription") {
        for (const value of [native.email, native.plan]) {
          if (value) summary.append($("span", "", value));
        }
        summary.append($("span", "", "免 API Key · 临时会话仅在工作台保留记录"));
      } else {
        summary.append($("span", "", native.message || "检测本机 CLI 或桌面端登录。"));
        if (native.status === "cli_missing") summary.append($("code", "", installCommand));
      }
      card.replaceChildren(header, summary);
      if (native.binary_path) {
        const runtime = $("div", "codex-connection-summary meta");
        const signature = [nativeVersion(native), native.catalog_source].filter(Boolean).join(" · ");
        runtime.append($("code", "", signature), $("code", "codex-binary-path", native.binary_path));
        card.append(runtime);
      }
      if (native.version_notice) {
        const upgrade = $("p", "codex-connection-summary codex-warning");
        upgrade.append($("span", "", native.version_notice), $("code", "", installCommand));
        card.append(upgrade);
      }
      if (native.catalog_error) card.append($("p", "codex-connection-summary inline-error", native.catalog_error));
      if (native.status !== "ready_subscription" || native.catalog_error || !native.models?.length) return;
      const list = $("div", "connection-models");
      const defaults = defaultNativeModel(native, prefs)?.id;
      const disabled = new Set(prefs.disabled_codex_models || []);
      for (const item of native.models) {
        const model = $("div", "connection-model codex-model-row");
        const name = $("span", "truncate", item.name);
        name.title = item.id;
        const capabilities = $("span", "model-capabilities");
        const effort = nativeEffortSummary(item);
        if (effort) capabilities.append(effort);
        const toggle = button("", () => {
          const next = new Set(disabled);
          if (next.has(item.id)) next.delete(item.id);
          else next.add(item.id);
          return save({ disabled_codex_models: [...next],
            ...(next.has(prefs.default_codex_model) ? { default_codex_model: "" } : {}) });
        }, "connection-switch");
        toggle.append($("span"));
        toggle.setAttribute("role", "switch");
        toggle.setAttribute("aria-label", "启用 " + item.name);
        toggle.setAttribute("aria-checked", String(!disabled.has(item.id)));
        toggle.disabled = busy;
        const primary = item.id === defaults ? $("span", "meta", "默认") :
          button("设为默认", () => save({ default_codex_model: item.id }), "ghost codex-compact");
        if (primary.tagName === "BUTTON") primary.disabled = busy || disabled.has(item.id);
        model.append(name, capabilities, actions(toggle, primary));
        list.append(model);
      }
      card.append(list);
    }
    render();
    poll();
    return { card, detect };
  }

  function testAction(label, payload) {
    const result = status("");
    result.classList.add("connection-result");
    result.hidden = true;
    const trigger = button(label, async () => {
      trigger.disabled = true;
      result.hidden = false;
      result.textContent = "测试中…";
      result.classList.remove("inline-error");
      try {
        const reply = await post("/api/model-profiles/test", payload);
        result.replaceChildren(icon("check"), $("span", "", "连接成功"), $("code", "", reply.latency_ms + " ms"));
      } catch (error) {
        result.textContent = error.message;
        result.classList.add("inline-error");
      } finally { trigger.disabled = false; }
    }, "ghost");
    return { result, trigger };
  }

  async function refreshModels() {
    await ctx.reloadConfig();
    await loadModels();
  }

  function providerCard(provider, protocolName, saved, report) {
    const card = $("article", "connection-card");
    const heading = $("div", "connection-header");
    const meta = $("div", "connection-meta");
    const identity = $("div", "connection-identity");
    const configured = $("span", "credential-state");
    const dot = $("span", "dot");
    if (provider.has_key) dot.dataset.tone = "success";
    configured.append(dot, $("span", "", provider.has_key ? "API Key 已配置"
      : profileUsable(provider) ? "本地端点" : "未配置 API Key"));
    identity.append($("h3", "", provider.name), $("span", "protocol-label", protocolName));
    meta.append(identity, $("code", "connection-url", provider.base_url), configured);
    const check = testAction("测试连接", { provider_id: provider.id });
    const edit = button("编辑", () => ctx.openProvider(currentCatalog, { provider }, loadModels), "ghost");
    const more = menu(provider.name + "服务商操作", (close) => [
      button("添加模型", () => { close(); ctx.openProvider(currentCatalog, { provider, focusModels: true }, loadModels); }, "ghost"),
      $("hr"),
      button("移除服务商", () => {
        close();
        ctx.editor("移除服务商", [
          $("p", "muted", "将同时移除「" + provider.name + "」服务商下的 " + provider.models.length + " 个模型。"),
        ], "确认移除", async () => {
          await post("/api/model-profiles/remove", { provider_id: provider.id });
          await refreshModels();
        });
      }, "ghost dangerous-action"),
    ]);
    heading.append(meta, actions(check.trigger, edit, more));
    const rows = $("div", "connection-models");
    for (const item of provider.models) rows.append(modelRow(item));
    if (!provider.models.length) rows.append($("p", "meta connection-empty", "尚未添加模型。"));
    card.append(heading, check.result, rows);
    return card;

    function modelRow(item) {
      const model = $("div", "connection-model");
      const modelID = $("code", "connection-model-id", item.model);
      modelID.title = item.model;
      const check = testAction("单模型测试", { id: item.id });
      const toggle = button("", async () => {
        toggle.disabled = true;
        try {
          await post("/api/model-profiles/model", { action: "enabled", id: item.id, enabled: !item.enabled });
          await refreshModels();
        } catch (error) {
          report(error.message, true);
          toggle.disabled = false;
        }
      }, "connection-switch");
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-checked", String(item.enabled));
      toggle.setAttribute("aria-label", "启用模型 " + item.model);
      toggle.title = item.enabled ? "停用该模型" : "启用该模型";
      toggle.append($("span"));
      const option = menu(item.model + " 模型操作", (close) => {
        const changeDefault = (text, key, unavailable) => {
          const row = button(text, async () => {
            close();
            try {
              await post("/api/settings", { [key]: item.id });
              await refreshModels();
            } catch (error) { report(error.message, true); }
          }, "ghost");
          row.disabled = !profileUsable(item) || unavailable || saved[key] === item.id;
          if (unavailable) row.title = "此模型不可用于子代理";
          return row;
        };
        return [
          changeDefault("设为默认主控", "default_model", false),
          changeDefault("设为默认子代理", "default_worker_model", /astra/i.test(item.model)),
          $("hr"),
          button("从列表移除该模型", () => {
            close();
            ctx.editor("移除模型", [$("p", "muted", "移除「" + item.model + "」模型。")], "确认移除", async () => {
              await post("/api/model-profiles/model", { action: "remove", id: item.id });
              await refreshModels();
            });
          }, "ghost dangerous-action"),
        ];
      });
      model.append(modelID, capabilityTags(item), $("span", "connection-tier", evaluationTiers[item.tier]),
        actions(toggle, check.trigger, option), check.result);
      return model;
    }
  }

  async function loadModels() {
    add.disabled = true;
    models.replaceChildren(sectionHeading("模型连接", add), status("正在读取模型配置…"));
    try {
      const [catalog, settings, native] = await Promise.all([
        request("/api/model-profiles"), request("/api/settings"), request("/api/codex-local"),
      ]);
      currentCatalog = catalog;
      add.disabled = false;
      const prefs = settings.preferences;
      const labels = new Map(catalog.protocols.map(({ id, label }) => [id, label]));
      const feedback = status("");
      feedback.hidden = true;
      const report = (text, failed = false) => {
        feedback.hidden = false;
        feedback.textContent = text;
        feedback.classList.toggle("inline-error", failed);
      };
      const cards = $("div", "connection-cards");
      for (const provider of catalog.providers) cards.append(providerCard(provider, labels.get(provider.protocol), prefs, report));
      const subscription = subscriptionCard(native, prefs, report);
      const modeTabs = executionModeTabs(prefs.default_execution_mode, async (mode) => {
        if (mode === prefs.default_execution_mode) return;
        const previous = prefs.default_execution_mode;
        const buttons = [...modeTabs.element.children];
        buttons.forEach((item) => { item.disabled = true; });
        modeTabs.update(mode);
        try {
          const result = await post("/api/settings", { default_execution_mode: mode });
          Object.assign(prefs, result.preferences);
          await ctx.reloadConfig();
          report("已保存默认执行模式");
          if (mode === codexSubscription) void subscription.detect(false);
        } catch (error) {
          modeTabs.update(previous);
          report(error.message, true);
        } finally { buttons.forEach((item) => { item.disabled = false; }); }
      });
      const modeBar = $("div", "execution-mode-bar");
      modeBar.append(modeTabs.element, $("span", "meta", "可在对话输入框中随时临时切换模式"));
      const apiHeading = sectionHeading("API 模型服务商", add);
      apiHeading.classList.add("api-service-heading");
      models.replaceChildren(modeBar, feedback, subscription.card, apiHeading,
        catalog.providers.length ? cards : $("p", "muted", "添加服务商后，可在指挥台选择 API 模型。"));
      if (prefs.default_execution_mode === codexSubscription || native.status === "ready_subscription") void subscription.detect(false);

      const enabled = catalog.profiles.filter(profileUsable);
      const choices = (profiles) => profiles.map(({ id, name, model }) => [id, name + " · " + model]);
      const primary = popoverSelect("默认 API 主控模型", [["", "自动选择"], ...choices(enabled)], { value: prefs.default_model });
      const worker = popoverSelect("默认子代理并发模型", [["", "沿用主控模型"],
        ...choices(enabled.filter(({ model }) => !/astra/i.test(model)))], { value: prefs.default_worker_model });
      worker.trigger.title = "用于仓库 CLI 中使用 API 主控的原生委派";
      const saveFeedback = status("");
      const save = button("保存", async () => {
        save.disabled = true;
        saveFeedback.textContent = "正在保存…";
        saveFeedback.classList.remove("inline-error");
        try {
          const result = await post("/api/settings", { default_model: primary.value, default_worker_model: worker.value });
          Object.assign(prefs, result.preferences);
          saveFeedback.textContent = "已保存";
          await ctx.reloadConfig();
        } catch (error) {
          saveFeedback.textContent = error.message;
          saveFeedback.classList.add("inline-error");
        } finally { save.disabled = false; }
      }, "primary");
      const fields = $("div", "gateway-default-fields");
      for (const [label, select] of [["默认 API 主控模型", primary], ["CLI 默认子代理模型", worker]]) {
        const field = $("div", "field");
        field.append($("span", "field-label", label), select.element);
        fields.append(field);
      }
      preferences.replaceChildren(sectionHeading("默认运行设置"), fields, actions(save, saveFeedback));
    } catch (error) {
      models.replaceChildren(sectionHeading("模型连接", add), $("p", "inline-error", error.message),
        button("重试", loadModels));
    }
  }

  const refresh = button("重新检测", probe, "ghost");
  async function probe() {
    refresh.disabled = true;
    runtime.replaceChildren(sectionHeading("工具链与 MCP", refresh), status("正在检测本地环境…"));
    try {
      const { tools } = await request("/api/workbench/tools");
      const rows = tools.map(({ label, available, detail }) => [
        label, available ? $("span", "badge success", "可用") : $("span", "badge", "未连接"),
        $("span", "muted", detail),
      ]);
      runtime.replaceChildren(sectionHeading("工具链与 MCP", refresh), gridTable(["工具", "状态", "检测结果"], rows, "gateway-table"));
    } catch (error) {
      runtime.replaceChildren(sectionHeading("工具链与 MCP", refresh), $("p", "inline-error", error.message));
    } finally { refresh.disabled = false; }
  }

  function showApprovals({ destinations }) {
    approvals.replaceChildren(sectionHeading("领域网络授权"));
    if (!destinations.length) {
      approvals.append($("p", "muted", "当前领域暂无持久授权的 HTTPS 目标。"));
      return;
    }
    const rows = destinations.map((destination) => {
      const feedback = $("span", "inline-error");
      const revoke = button("撤销", async () => {
        revoke.disabled = true;
        try {
          showApprovals(await post("/api/workbench/network-approvals", {
            domain_id: ctx.data.domain.id, destination,
          }));
        } catch (error) {
          feedback.textContent = error.message;
          revoke.disabled = false;
        }
      }, "ghost");
      return [$("code", "", destination), actions(revoke, feedback)];
    });
    approvals.append(gridTable(["授权目标", "操作"], rows, "gateway-table"));
  }

  loadModels();
  probe();
  if (ctx.data.domain) request("/api/workbench/network-approvals?domain=" + ctx.data.domain.id).then(showApprovals)
    .catch((error) => approvals.replaceChildren(sectionHeading("领域网络授权"), $("p", "inline-error", error.message)));
  else approvals.remove();
  return column(heading("网关与设置", "", settingsTabs(ctx)), sections);
}

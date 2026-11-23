import { $, actions, button, column, empty, gridTable, heading, href, icon, popover, popoverSelect, request,
  segments, stateBadge } from "./workbench-ui.js";
import { stageNames } from "./workbench-scope.js";

const assetTabs = [
  ["sources", "全部文献与信源"], ["tasks", "全部工业难题"], ["verifiers", "全部 Verifier"],
  ["evaluations", "全平台评测与批量导出"],
];
const authorities = {
  T1_STANDARD: "T1 行业标准", T2_MANUFACTURER: "T2 产线规范", T3_TOOLCHAIN: "T3 工具文档", T4_CASE_SAMPLE: "T4 工程样板",
};
const difficulties = { easy: "基础", medium: "中等", hard: "困难", frontier: "前沿" };

export function timeLabel(value) {
  const time = new Date(value);
  const node = $("time", "meta", Number.isNaN(time.getTime()) ? value :
    new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(time));
  node.dateTime = value;
  node.title = Number.isNaN(time.getTime()) ? value : time.toLocaleString("zh-CN");
  return node;
}

function fitDomainTags(row) {
  const pills = [...row.children];
  const more = pills.pop();
  pills.forEach((pill) => { pill.hidden = false; });
  more.hidden = true;
  const edge = row.getBoundingClientRect().bottom + 0.5;
  if (pills.at(-1).getBoundingClientRect().bottom <= edge) return;
  more.hidden = false;
  let hidden = 0;
  do {
    pills[pills.length - ++hidden].hidden = true;
    more.textContent = `+${hidden}`;
  } while (more.getBoundingClientRect().bottom > edge && hidden < pills.length);
  const names = pills.slice(-hidden).map((pill) => pill.textContent).join("、");
  more.title = names;
  more.setAttribute("aria-label", `其余 ${hidden} 个标签，${names}`);
}

export function domainsView(ctx) {
  const title = heading("领域资料库");
  if (!ctx.data.domains.length) {
    return column(title, empty("book", "暂无领域资料库", "派发研究目标后，Agent 会匹配已有领域或建立专属资料库。",
      actions(href("前往派发研究任务", ctx.route("scope", null), "button primary"), button("手动新建", ctx.addDomain, "ghost"))));
  }
  const grid = $("div", "domain-grid");
  const observer = new ResizeObserver((entries) => {
    if (!grid.isConnected) {
      observer.disconnect();
      return;
    }
    entries.forEach(({ target }) => fitDomainTags(target));
  });
  for (const domain of ctx.data.domains) {
    const tile = $("article", "domain-tile");
    const card = href("", ctx.route("scope", domain.id), "domain-card");
    const stage = $("div", "domain-stage");
    stage.append(icon("book"), $("span", "truncate", stageNames[domain.stage] || "领域定位"));
    if (domain.local_import_count > 0) {
      stage.append($("span", "domain-pending", `· ${domain.local_import_count.toLocaleString("zh-CN")} 条待核验`));
    }
    const tags = $("div", "domain-tags");
    const keywords = domain.keywords.length ? domain.keywords : ["待分类"];
    for (const keyword of keywords) {
      const tag = $("span", "protocol-label", keyword);
      tag.title = keyword;
      tags.append(tag);
    }
    const more = $("span", "protocol-label");
    more.hidden = true;
    tags.append(more);
    observer.observe(tags);
    card.append(stage, $("h2", "", domain.name), tags);
    const metrics = $("dl", "domain-assets");
    for (const [key, label] of [["nodes", "工艺节点"], ["references", "核验文献"], ["tasks", "构题"], ["evaluated", "已评测"]]) {
      const item = $("div");
      item.append($("dt", "meta", label), $("dd", "mono", domain.counts[key]));
      metrics.append(item);
    }
    const updated = $("span", "meta", "更新于 ");
    updated.append(timeLabel(domain.updated_at));
    card.append(metrics, updated);
    const panel = $("div", "domain-actions");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", domain.name + "的资料库管理");
    const menu = popover("管理「" + domain.name + "」", panel, {
      appearance: "icon-button ghost icon-trigger", symbol: "more", caret: false, align: "end",
    });
    menu.element.classList.add("domain-menu");
    panel.append(href("查看数据集", ctx.route("sources/datasets", domain.id), "button ghost"));
    for (const [op, label] of [["rename", "重命名"], ["merge", "合并重复领域库"], ["delete", "删除"]]) {
      const item = button(label, () => { menu.close(true); ctx.manageDomain(op, domain); },
        "ghost" + (op === "delete" ? " domain-delete" : ""));
      item.disabled = op === "merge" && ctx.data.domains.length < 2;
      panel.append(item);
    }
    tile.append(card, menu.element);
    grid.append(tile);
  }
  return column(title, grid);
}

function recordLink(ctx, row, kind) {
  const domain = row.domain_id;
  if (!domain || row.origin === "archive" && kind !== "tasks") {
    return row.run_id ? ctx.route(kind === "evaluations" ? "export/run/" + row.run_id : "tasks/run/" + row.run_id, domain) : null;
  }
  const sourcePage = { frontier: "sources/frontier", import: "sources/imports" }[row.origin] || "sources/references";
  const page = { sources: sourcePage,
    tasks: "tasks", verifiers: "verifiers", evaluations: "export" }[kind];
  return ctx.route(page, domain, { item: row.id });
}

function batchDialog(ctx, selected) {
  const choices = $("div", "batch-domains wide");
  const notice = $("p", "meta wide", "按所选领域的导出格式打包已通过离线验收的题目。");
  for (const domain of ctx.data.domains) {
    const row = $("label", "preference-toggle");
    const check = $("input");
    check.type = "checkbox";
    check.value = domain.id;
    check.checked = selected ? String(domain.id) === selected : true;
    row.append(check, $("span", "", domain.name));
    choices.append(row);
  }
  ctx.editor("批量导出题包", [notice, choices], "下载 ZIP", async () => {
    const ids = [...choices.querySelectorAll("input:checked")].map((input) => input.value);
    if (!ids.length) throw new Error("请选择至少一个领域。");
    const params = new URLSearchParams(ids.map((id) => ["domain", id]));
    const response = await fetch("/api/workbench/batch-export?" + params);
    if (!response.ok) throw new Error((await response.json()).error || "导出失败");
    const url = URL.createObjectURL(await response.blob());
    const link = href("", url);
    link.download = "industrial-benchmarks.zip";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  });
}

export function assetsView(ctx) {
  const kind = ctx.section || "sources";
  const route = (next, filters) => ctx.route("assets" + (next === "sources" ? "" : "/" + next), null, filters);
  const tabBar = segments("汇总资产类型", assetTabs, kind, (value) => {
    const { domain, q, stage } = ctx.filters;
    ctx.navigate(route(value, Object.fromEntries(Object.entries({ domain, q, stage }).filter(([, item]) => item))));
  });
  tabBar.classList.add("asset-tabs");
  const exportButton = kind === "evaluations" && ctx.data.domains.length ?
    button("批量导出题包", () => batchDialog(ctx, ctx.filters.domain), "primary", "download") : null;
  const filters = $("form", "asset-filters");
  const input = $("input");
  input.type = "search";
  input.maxLength = 120;
  input.placeholder = "搜索资产名称、工序或编号";
  input.setAttribute("aria-label", "搜索资产");
  input.value = ctx.query;
  const submit = button("搜索", null);
  submit.type = "submit";
  const queryGroup = $("div", "asset-query");
  queryGroup.append(input, submit);
  filters.append(queryGroup);
  const content = $("div");
  const page = column(heading("全局数据汇总", "", exportButton), tabBar, filters, content);
  const allowed = kind === "sources" ? ["stage", "authority"] : ["stage", "difficulty"];
  const update = (changes = {}) => {
    const next = { ...ctx.filters, q: input.value.trim(), ...changes };
    ctx.navigate(route(kind, Object.fromEntries(Object.entries(next).filter(([, value]) => value))));
  };
  filters.addEventListener("submit", (event) => { event.preventDefault(); update(); });
  function select(key, label, pairs) {
    const current = ctx.filters[key] || "";
    if (current && !pairs.some(([value]) => String(value) === current)) pairs.unshift([current, current]);
    const control = popoverSelect(label, [["", "全部" + label], ...pairs], { value: current });
    control.element.addEventListener("change", () => update(key === "domain"
      ? { domain: control.value, stage: "", authority: "", difficulty: "" } : { [key]: control.value }));
    return control.element;
  }
  filters.append(select("domain", "领域", ctx.data.domains.map(({ id, name }) => [id, name])));
  let offset = 0;
  let rows = [];
  async function load(append = false) {
    if (!append) content.replaceChildren($("p", "meta", "正在读取资产…"));
    content.setAttribute("aria-busy", "true");
    try {
      const params = new URLSearchParams({ ...ctx.filters, kind, offset });
      const result = await request("/api/workbench/assets?" + params);
      if (!page.isConnected) return;
      if (!append) {
        const labels = { stage: "工序", authority: "权威等级", difficulty: "难度档位" };
        const names = { authority: authorities, difficulty: difficulties };
        for (const key of allowed) filters.append(select(key, labels[key], result.options[key].map((value) => [
          value, names[key]?.[value] || value,
        ])));
        if (Object.keys(ctx.filters).length) filters.append(href("清除筛选", route(kind), "button ghost"));
      }
      rows = [...rows, ...result.items];
      offset += result.items.length;
      if (!rows.length) {
        content.replaceChildren(Object.keys(ctx.filters).length
          ? empty("search", "未找到匹配资产", "", actions(href("清除筛选", route(kind), "button")))
          : empty("archive", "暂无可汇总的资产", "在领域工作区登记的文献、题目、验证器与评测会自动汇总到这里。",
            actions(href("打开领域资料库", ctx.route("domains", null), "button primary"))));
        return;
      }
      const label = kind === "sources" ? "名称与来源" : "资产名称";
      const attribute = kind === "sources" ? "权威等级" : "难度档位";
      content.replaceChildren(gridTable([label, "所属领域", "工序", attribute, "状态", "更新时间"], rows.map((row) => {
        const path = recordLink(ctx, row, kind);
        const title = row.title || row.id;
        const titleElement = path ? href(title, path, "asset-record-link") : $("span", "", title);
        if (/^https?:\/\//i.test(title)) {
          titleElement.classList.add("asset-url", "truncate", "mono");
          titleElement.title = title;
        }
        const value = row.subtitle === title ? "" : row.subtitle;
        const fingerprint = value?.match(/^(?:sha256:)?([a-f0-9]{64})$/i);
        const url = /^https?:\/\//i.test(value || "");
        const subtitle = value && kind !== "tasks" ?
          $("span", "asset-subtitle truncate meta" + (url ? " asset-url mono" : fingerprint || kind !== "sources" ? " mono" : ""),
            fingerprint ? "#" + fingerprint[1].slice(0, 8) : value) : null;
        if (subtitle) subtitle.title = value;
        return [column(titleElement, ...(subtitle ? [subtitle] : [])),
          row.domain_id ? href(row.domain_name, ctx.route("scope", row.domain_id), "asset-domain-link") : "未归属领域",
          row.stage || "—", (kind === "sources" ? authorities[row.authority] || row.authority :
            difficulties[row.difficulty] || row.difficulty) || "—",
          stateBadge(row.status), row.updated_at ? timeLabel(row.updated_at) : "—"];
      }), "global-assets-table"));
      if (result.more) {
        const more = button("加载更多", () => { more.disabled = true; load(true); }, "table-footer");
        content.append(more);
      }
    } catch (error) {
      content.append($("p", "inline-error", error.message), button("重试", (event) => {
        event.currentTarget.remove();
        load(append);
      }, "ghost"));
    } finally {
      content.setAttribute("aria-busy", "false");
    }
  }
  load();
  return page;
}

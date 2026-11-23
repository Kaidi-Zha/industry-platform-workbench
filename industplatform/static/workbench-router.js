const globalPages = { scope: "research", domains: "library", assets: "assets", settings: "settings" };
const domainPages = { scope: "research", graph: "graph", sources: "sources", tasks: "tasks", verifiers: "verifiers", export: "export" };
const runId = /^[a-f0-9]{32}$/;
const entityId = /^[\w.:-]{1,200}$/;
const domainId = (value) => /^[1-9]\d{0,15}$/.test(value || "") && Number.isSafeInteger(Number(value)) ? String(value) : null;
export const settingsSections = [
  ["", "网关"], ["permissions", "权限"], ["agent", "Agent"], ["appearance", "外观"],
  ["storage", "存储"], ["collaboration", "资料同步"], ["shortcuts", "快捷键"], ["schema", "默认导出格式"],
];

export function workspacePath(fragment = "scope", domain = null, params) {
  const [page, ...parts] = fragment.split("/");
  const id = domainId(domain);
  const scoped = id && Object.hasOwn(domainPages, page);
  if (!scoped && ["tasks", "export"].includes(page) && parts[0] === "run" && runId.test(parts[1] || "")) {
    return "/global/assets/record/" + parts[1];
  }
  const slug = (scoped ? domainPages : globalPages)[page] || "research";
  const prefix = scoped ? "/domains/" + id : "/global";
  const query = new URLSearchParams(params).toString();
  return prefix + "/" + slug + (parts.length ? "/" + parts.join("/") : "") + (query ? "?" + query : "");
}

export function workspaceRoute(url) {
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts[0] === "global" && parts[1] === "assets" && parts[2] === "record" && parts.length === 4 && runId.test(parts[3])) {
    return { level: "global", page: "tasks", domain: null, run: parts[3],
      path: workspacePath("tasks/run/" + parts[3]), query: "", item: "", filters: {} };
  }
  let domain, page, detail, key, extra;
  let filters = url.searchParams;
  if (parts[0] === "global" || parts[0] === "domains") {
    domain = parts[0] === "domains" ? domainId(parts[1]) : null;
    const [slug, ...rest] = parts.slice(domain ? 2 : 1);
    const pages = domain ? domainPages : globalPages;
    page = Object.keys(pages).find((id) => pages[id] === slug) || "scope";
    [detail, key, extra] = rest;
  } else {
    const [fragment, query = ""] = url.hash.slice(1).split("?", 2);
    [page, detail, key, extra] = fragment.split("/");
    domain = domainId(filters.get("domain")) || domainId(filters.get("workspace"));
    const bench = filters.get("bench");
    filters = new URLSearchParams(query);
    if (page === "chat") page = "scope";
    if (page === "references") page = "domains";
    if (page === "datasets") [page, detail] = ["sources", "datasets"];
    if (page === "run") [page, detail, key] = ["tasks", "run", detail];
    if (page === "tasks" && !detail && bench === "case") detail = "case";
    if (page === "tasks" && !detail && bench === "eval") [page, detail] = ["export", "evaluate"];
    if (page === "settings" && ["general", "models"].includes(detail)) detail = "";
    if (!Object.hasOwn(globalPages, page) && !Object.hasOwn(domainPages, page)) [page, detail, key] = ["scope", "", ""];
    if (["settings", "assets", "domains"].includes(page)) domain = null;
    if (!domain && ["graph", "sources", "tasks", "verifiers", "export"].includes(page) && detail !== "run") {
      [page, detail, key] = ["domains", "", ""];
    }
  }
  const thread = page === "scope" && !key && runId.test(detail || "") ? detail : null;
  const node = page === "graph" && !key && /^[a-zA-Z0-9_-]{1,120}$/.test(detail || "") ? detail : null;
  const run = ["tasks", "export"].includes(page) && !extra && detail === "run" && runId.test(key || "") ? key : null;
  let section = "";
  if (!key) {
    if (page === "settings" && settingsSections.some(([id]) => id === detail)) section = detail;
    if (page === "tasks" && ["case", "build", "template"].includes(detail)) section = detail;
    if (page === "export" && detail === "evaluate") section = detail;
    if (page === "sources" && ["datasets", "frontier", "imports", "references"].includes(detail)) section = detail;
    if (page === "assets" && ["tasks", "verifiers", "evaluations"].includes(detail)) section = detail;
  }
  const focus = thread || node || (run ? "run/" + run : section);
  const query = !run && ["assets", "sources", "tasks"].includes(page) ? (filters.get("q") || "").slice(0, 120) : "";
  const item = domain && !run && ["sources", "tasks", "verifiers", "export"].includes(page) &&
    entityId.test(filters.get("item") || "") ? filters.get("item") : "";
  const params = {};
  if (query) params.q = query;
  if (item) params.item = item;
  if (section === "imports" && /^S[0-8]$/.test(filters.get("stage") || "")) params.stage = filters.get("stage");
  if (page === "sources" && (!section || section === "references")) {
    if (entityId.test(filters.get("node") || "")) params.node = filters.get("node");
    if (/^T[1-4]_[A-Z_]+$/.test(filters.get("tier") || "")) params.tier = filters.get("tier");
    if (/^C[1-7]_[A-Z_]+$/.test(filters.get("channel") || "")) params.channel = filters.get("channel");
    if (filters.get("view") === "flat") params.view = "flat";
  }
  if (page === "assets") {
    for (const field of ["domain", "stage", "authority", "difficulty"]) {
      const value = (filters.get(field) || "").slice(0, 120);
      if (value && (field !== "domain" || domainId(value))) params[field] = value;
    }
  }
  const path = workspacePath(page + (focus ? "/" + focus : ""), domain, params);
  return { level: domain ? "domain" : "global", page, domain, thread, node, run, section, query, item, filters: params, path };
}

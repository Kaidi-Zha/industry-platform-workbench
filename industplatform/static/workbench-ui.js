export const get = (id) => document.getElementById(id);
export const svgNS = "http://www.w3.org/2000/svg";

export function saveLocal(filename, content, type) {
  const url = URL.createObjectURL(content instanceof Blob ? content : new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function $(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export function icon(name) {
  const svg = document.createElementNS(svgNS, "svg");
  const use = document.createElementNS(svgNS, "use");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", "#" + name);
  svg.append(use);
  return svg;
}

export function href(text, url, appearance = "", symbol) {
  const a = $("a", appearance, text);
  a.href = url;
  if (symbol) a.prepend(icon(symbol));
  return a;
}

export function button(text, action, appearance = "", symbol) {
  const b = $("button", "button " + appearance, text);
  b.type = "button";
  if (symbol) b.prepend(icon(symbol));
  if (action) b.addEventListener("click", action);
  return b;
}

export function iconButton(label, action, symbol, appearance = "ghost") {
  const b = button("", action, "icon-button " + appearance, symbol);
  b.setAttribute("aria-label", label);
  b.title = label;
  return b;
}

let popoverSequence = 0;

export function popover(label, panel, { appearance = "", symbol, caret = true, align = "start", preferAbove = false, offset = 8 } = {}) {
  const element = $("div", "popover-anchor");
  const trigger = button("", null, "popover-trigger " + appearance);
  const caption = $("span", "popover-caption truncate", label);
  trigger.setAttribute("aria-label", label);
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-haspopup", panel.getAttribute("role") || "dialog");
  panel.id = "workbench-popover-" + (++popoverSequence);
  panel.classList.add("popover-panel");
  panel.setAttribute("popover", "auto");
  if (symbol) trigger.append(icon(symbol));
  trigger.append(caption);
  if (caret) trigger.append(icon("chevron"));
  trigger.setAttribute("aria-controls", panel.id);
  trigger.popoverTargetElement = panel;
  element.append(trigger, panel);

  function place() {
    const anchor = trigger.getBoundingClientRect();
    const width = document.documentElement.clientWidth;
    const height = document.documentElement.clientHeight;
    const edge = 8;
    panel.style.minWidth = Math.min(width - edge * 2, anchor.width) + "px";
    const size = panel.getBoundingClientRect();
    const before = anchor.top - edge * 2;
    const after = height - anchor.bottom - edge * 2;
    const above = size.height > after ? before > after : preferAbove && before >= size.height;
    const room = Math.max(0, above ? before : after);
    panel.style.maxHeight = room + "px";
    const left = align === "end" ? anchor.right - size.width : anchor.left;
    panel.style.left = Math.max(edge, Math.min(left, width - size.width - edge)) + "px";
    panel.style.top = (above ? Math.max(edge, anchor.top - Math.min(size.height, room) - offset) : anchor.bottom + offset) + "px";
  }

  function close(focus = false) {
    if (panel.matches(":popover-open")) panel.hidePopover();
    if (focus) trigger.focus();
  }
  function open() {
    if (!panel.matches(":popover-open")) {
      panel.style.maxHeight = "";
      panel.showPopover({ source: trigger });
      place();
    }
  }
  trigger.addEventListener("click", (event) => {
    event.preventDefault();
    if (panel.matches(":popover-open")) close();
    else open();
  });
  panel.addEventListener("beforetoggle", ({ newState }) => {
    trigger.setAttribute("aria-expanded", String(newState === "open"));
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.defaultPrevented) {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    }
  });
  return { element, trigger, caption, open, close,
    reposition() { if (panel.matches(":popover-open")) { panel.style.maxHeight = ""; place(); } } };
}

export function popoverSelect(label, options = [], {
  value = "", name, appearance = "", prefix = "", placeholder = "请选择", preferAbove = false, align,
  renderCaption, decorateOption, symbol,
} = {}) {
  const list = $("div", "select-options");
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", label);
  const { element, trigger, caption, open, close } = popover(label, list, { appearance, preferAbove, align, symbol });
  element.classList.add("popover-select");
  if (prefix) caption.before($("span", "select-prefix", prefix));
  const input = name ? $("input") : null;
  if (input) {
    input.type = "hidden";
    input.name = name;
    element.append(input);
  }
  let selected = String(value);
  let choices = [];
  function update() {
    const option = choices.find(({ key }) => key === selected);
    caption.textContent = option?.title ?? placeholder;
    trigger.setAttribute("aria-label", label + "，" + caption.textContent);
    renderCaption?.(caption, selected);
    if (input) input.value = selected;
    for (const { row, key } of choices) row.setAttribute("aria-selected", String(key === selected));
  }
  function setOptions(entries, preferred = selected) {
    choices = entries.map(([key, title]) => {
      key = String(key);
      const row = button("", () => {
        const changed = selected !== key;
        selected = key;
        update();
        close(true);
        if (changed) element.dispatchEvent(new Event("change", { bubbles: true }));
      }, "select-option");
      row.setAttribute("role", "option");
      row.title = title;
      row.tabIndex = -1;
      row.append($("span", "truncate", title), icon("check"));
      decorateOption?.(row, key);
      return { row, key, title };
    });
    selected = choices.some(({ key }) => key === String(preferred)) ? String(preferred) : choices[0]?.key ?? "";
    list.replaceChildren(...choices.map(({ row }) => row));
    update();
  }
  function focusOption(last = false) {
    (choices.find(({ key }) => key === selected) ?? (last ? choices.at(-1) : choices[0]))?.row.focus();
  }
  trigger.addEventListener("click", () => {
    if (list.matches(":popover-open")) focusOption();
  });
  trigger.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    open();
    focusOption(event.key === "ArrowUp");
  });
  list.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      close(true);
      return;
    }
    const index = choices.findIndex(({ row }) => row === document.activeElement);
    const target = {
      ArrowDown: Math.min(choices.length - 1, index + 1),
      ArrowUp: Math.max(0, index - 1), Home: 0, End: choices.length - 1,
    }[event.key];
    if (target === undefined) return;
    event.preventDefault();
    choices[target]?.row.focus();
  });
  setOptions(options, value);
  return {
    element, trigger, setOptions,
    get value() { return selected; },
    set value(key) {
      selected = choices.some((choice) => choice.key === String(key)) ? String(key) : choices[0]?.key ?? "";
      update();
    },
    set disabled(value) {
      if (value) close();
      trigger.disabled = value;
      if (input) input.disabled = value;
    },
  };
}

export function field(name, label, { value = "", as = "input", type = "text", required = false, limit, wide = false, options = [] } = {}) {
  if (as === "select") {
    const outer = $("div", "field" + (wide ? " wide" : ""));
    const select = popoverSelect(label, options, { name, value });
    if (required) select.element.dataset.required = label;
    outer.append($("span", "field-label", label), select.element);
    return outer;
  }
  const outer = $("label", "field" + (wide ? " wide" : ""));
  const input = $(as);
  input.name = name;
  input.required = required;
  if (as === "textarea") input.rows = 2;
  else input.type = type;
  input.value = value;
  if (limit) input.maxLength = limit;
  outer.append($("span", "field-label", label), input);
  return outer;
}

export function column(...children) {
  const container = $("div");
  container.append(...children);
  return container;
}

export function actions(...children) {
  const container = $("div", "actions");
  container.append(...children);
  return container;
}

export function heading(title, description, controls) {
  const container = $("header", "view-heading");
  const primary = column($("h1", "", title));
  if (description) primary.append($("p", "subtitle", description));
  container.append(primary);
  if (controls) container.append(controls);
  return container;
}

export function empty(symbol, title, message, controls) {
  const panel = $("section", "empty");
  const mark = $("div", "feature-icon");
  mark.append(icon(symbol));
  panel.append(mark, $("h2", "", title), $("p", "", message));
  if (controls) panel.append(controls);
  return panel;
}

export function segments(groupLabel, options, selection, change) {
  const row = $("div", "segments");
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", groupLabel);
  for (const [key, text, symbol] of options) {
    const item = button(text, () => change(key), "", symbol);
    item.setAttribute("aria-pressed", String(selection === key));
    row.append(item);
  }
  return row;
}

const stateLabels = {
  draft: ["有草稿", "warning"],
  awaiting_approval: ["待审批", "warning"], stale: ["待同步", "warning"], registered: ["已登记", "success"],
  ready: ["已就绪", "success"], parsed: ["已解析", "success"], rejected: ["已排除", ""],
  awaiting_scope: ["待确认范围", "warning"],
  awaiting_stage: ["待确认下一阶段", "warning"],
  queued: ["已排队", "active"], running: ["进行中", "active"], pendingInit: ["启动中", "active"],
  completed: ["已完成", "success"], needs_review: ["待复核", "warning"], paused: ["已暂停", "warning"],
  pausing: ["正在暂停", "warning"], failed: ["失败", "danger"], cancelling: ["正在取消", ""], cancelled: ["已取消", ""],
  leased: ["抓取中", "active"], retry: ["待重试", "warning"], blocked: ["访问受阻", "warning"],
  filtered: ["低规范价值 · 已过滤", "warning"], legacy_review: ["待复核", "warning"],
  done: ["已完成", "success"], errored: ["运行异常", "danger"], interrupted: ["已中断", "warning"],
  shutdown: ["已关闭", ""], notFound: ["已离线", ""], passed: ["通过", "success"], accepted: ["已登记", "success"],
  verified: ["已核验", "success"], synthetic: ["合成数据", ""], offline_verified: ["离线校验", "success"],
  qualified: ["已核验", "success"], sampled: ["已取样", "success"], limited: ["部分核验", "warning"],
  discovered: ["候选", ""], candidate: ["候选", ""], provider_error: ["接口异常", "warning"],
};

export function stateBadge(status) {
  const [label, tone] = stateLabels[status] || ["待处理", ""];
  return $("span", "badge " + tone, label);
}

export function stateTone(status) {
  return stateLabels[status]?.[1] || "";
}

export function external(text, url) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return null;
    const link = href(text, parsed.href, "button");
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch {
    return null;
  }
}

export function gridTable(headers, rows, className = "") {
  const wrap = $("div", "table-wrap " + className);
  if (!rows.length) return wrap;
  const table = $("table", "data-table");
  const header = table.createTHead().insertRow();
  for (const label of headers) {
    const cell = $("th");
    cell.append(label);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const cells of rows) {
    const row = body.insertRow();
    for (const item of cells) row.insertCell().append(item ?? "");
  }
  wrap.append(table);
  return wrap;
}

export async function request(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}

export function post(path, data) {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
}

export function jsonCode(pre, value) {
  const json = JSON.stringify(value, null, 2);
  const tokens = /("(?:[^"\\]|\\.)*")(\s*:)?|\b(?:true|false|null)\b|-?\b\d+(?:\.\d+)?\b/g;
  const fragment = document.createDocumentFragment();
  let offset = 0;
  for (const match of json.matchAll(tokens)) {
    fragment.append(json.slice(offset, match.index));
    fragment.append($("span", match[2] ? "key" : match[1] ? "string" : "literal", match[1] || match[0]));
    if (match[2]) fragment.append(match[2]);
    offset = match.index + match[0].length;
  }
  fragment.append(json.slice(offset));
  pre.replaceChildren(fragment);
}

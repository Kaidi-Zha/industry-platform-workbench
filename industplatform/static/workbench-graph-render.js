import { svgNS } from "./workbench-ui.js";

const font = 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
const mono = 'ui-monospace, "SF Mono", Consolas, monospace';
const textColor = "#18181b";
const muted = "#71717a";
const edgeColor = "#a1a1aa";
const accent = "#4f46e5";
const padding = 32;
let measure;

export function shape(tag, attributes = {}, text) {
  const element = document.createElementNS(svgNS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  if (text != null) element.textContent = text;
  return element;
}

function label(value, x, y, size, attrs) {
  return shape("text", { x, y, "font-size": size, fill: muted, ...attrs }, value);
}

function wrapTitle(title, width, size, lines = 2) {
  measure ??= document.createElement("canvas").getContext("2d");
  measure.font = "500 " + size + "px " + font;
  const characters = Array.from(title);
  const result = [];
  let line = "";
  while (characters.length && result.length < lines) {
    const char = characters.shift();
    const suffix = result.length === lines - 1 && characters.length ? "…" : "";
    if (line && measure.measureText(line + char + suffix).width > width) {
      result.push(line.trimEnd() + (suffix || (result.length === lines - 1 ? "…" : "")));
      line = char.trimStart();
    } else {
      line += char;
    }
  }
  if (line && result.length < lines) result.push(line);
  return result;
}

function roundedOrthogonal(section) {
  const points = [section.startPoint, ...(section.bendPoints || []), section.endPoint];
  const commands = ["M" + points[0].x + " " + points[0].y];
  const near = (from, to, radius) => {
    const distance = Math.hypot(to.x - from.x, to.y - from.y);
    const fraction = distance ? Math.min(radius / distance, .5) : 0;
    return (from.x + fraction * (to.x - from.x)) + " " + (from.y + fraction * (to.y - from.y));
  };
  for (let i = 1; i < points.length - 1; i++) {
    const bend = points[i];
    const radius = Math.min(7, Math.hypot(bend.x - points[i - 1].x, bend.y - points[i - 1].y) / 2,
      Math.hypot(bend.x - points[i + 1].x, bend.y - points[i + 1].y) / 2);
    commands.push("L" + near(bend, points[i - 1], radius), "Q" + bend.x + " " + bend.y + " " + near(bend, points[i + 1], radius));
  }
  const end = points.at(-1);
  return commands.join(" ") + " L" + end.x + " " + end.y;
}

function activate(element, name, action) {
  element.setAttribute("role", "button");
  element.setAttribute("tabindex", "0");
  element.setAttribute("aria-label", name);
  element.setAttribute("data-graph-action", "");
  element.addEventListener("click", action);
  element.addEventListener("keydown", (event) => {
    if (event.target === element && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      action(event);
    }
  });
}

function groupHeading(group, index, focusGroup) {
  const header = shape("g", { transform: "translate(" + group.x + " " + group.y + ")", class: "graph-group-heading" });
  const number = String(index + 1).padStart(2, "0");
  const available = group.width - (focusGroup ? 64 : 36);
  const heading = wrapTitle(number + "  " + group.title, available, 13, 1)[0];
  const coverage = Math.round(group.nodes.reduce((sum, node) => sum + node.data.coverage, 0) / group.nodes.length);
  header.append(label(heading, 16, 24, 13, { fill: textColor, "font-weight": 500 }),
    label(group.nodes.length + " 道工序 · " + coverage + "%", 16, 43, 12, { "font-family": mono }));
  if (focusGroup) {
    header.prepend(shape("rect", { x: 4, y: 4, width: group.width - 8, height: 48, rx: 8, fill: "transparent" }));
    header.append(shape("path", {
      d: "M0 5V0H5 M10 0H15V5 M15 10V15H10 M5 15H0V10",
      transform: "translate(" + (group.width - 33) + " 18)", fill: "none",
      stroke: muted, "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round",
    }), shape("title", {}, "放大聚焦「" + group.title + "」"));
    activate(header, "放大聚焦「" + group.title + "」", () => focusGroup(group));
  }
  return header;
}

function appendTitle(node, title, width, low) {
  const size = low ? 20 : 14;
  const y = low ? 54 : 41;
  const group = shape("g", { class: low ? "graph-low-detail" : "graph-full-detail" });
  for (const [i, line] of wrapTitle(title, width - 24, size).entries()) {
    group.append(label(line, 12, y + i * (low ? 23 : 18), size, { fill: textColor, "font-weight": 500 }));
  }
  node.append(group);
}

function nodeCard(box, { references, openNode, navigate }) {
  const node = box.data;
  const card = shape("g", {
    class: "graph-card", transform: "translate(" + box.x + " " + box.y + ")", "data-node-id": node.id,
  });
  card.append(shape("rect", { class: "graph-card-bg", width: box.width, height: box.height, rx: 8, fill: "#fff", stroke: "#e4e4e7" }));
  const position = String(node.position).padStart(2, "0");
  card.append(label(position, 12, 20, 12, { class: "graph-full-detail", "font-family": mono }),
    shape("title", {}, node.title));
  appendTitle(card, node.title, box.width, false);
  if (openNode) {
    card.append(label(position, 12, 24, 16, { class: "graph-low-detail", "font-family": mono }));
    appendTitle(card, node.title, box.width, true);
  }
  const full = shape("g", { class: "graph-full-detail" });
  let right = box.width - 10;
  for (const [show, name, width] of [
    [node.is_stale, "上游已更新 · 待同步", 114], [node.missing_t1_standard, "缺 T1 标准", 68],
  ]) {
    if (!show) continue;
    right -= width;
    full.append(shape("rect", { x: right, y: 7, width, height: 18, rx: 4, fill: "#fffbeb" }),
      label(name, right + width / 2, 20, 12, { fill: "#92400e", "text-anchor": "middle" }));
    right -= 4;
  }
  const stats = node.sources || node.verified || node.coverage
    ? "信源 " + node.sources + " / 已核验 " + node.verified + " · " + node.coverage + "%"
    : "待采集";
  let line = label(stats, 12, 83, 12, { "font-family": mono });
  if (references && (node.sources || node.verified)) {
    const url = references(node);
    const link = shape("a", { href: url, class: "graph-card-references", "data-graph-action": "",
      "aria-label": "查看「" + node.title + "」的工序文献" });
    link.addEventListener("click", (event) => {
      event.stopPropagation();
      if (navigate && event.button === 0 && !event.altKey && !event.shiftKey && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        navigate(url);
      }
    });
    link.append(line);
    line = link;
  }
  full.append(line);
  const bar = shape("g", { role: "progressbar", "aria-label": node.title + "采集进度",
    "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": node.coverage });
  bar.append(shape("rect", { x: 12, y: 94, width: box.width - 24, height: 3, rx: 1.5, fill: "#f0f0f1" }));
  if (node.coverage) bar.append(shape("rect", { x: 12, y: 94, width: (box.width - 24) * node.coverage / 100,
    height: 3, rx: 1.5, fill: node.crawl_status === "exhausted" ? "#15803d" : "#27272a" }));
  full.append(bar);
  card.append(full);
  if (openNode) {
    card.setAttribute("aria-haspopup", "dialog");
    activate(card, position + " " + node.title, () => openNode(box));
  }
  return card;
}

export function scene(layout, interactions = {}) {
  const root = shape("g", { "font-family": font, class: "graph-scene" });
  const defs = shape("defs");
  for (const [id, color] of [["graph-arrow", edgeColor], ["graph-arrow-active", accent]]) {
    const marker = shape("marker", { id, viewBox: "0 0 8 8", markerWidth: 7, markerHeight: 7,
      refX: 7, refY: 4, orient: "auto", markerUnits: "userSpaceOnUse" });
    marker.append(shape("path", { d: "M0 0 L8 4 L0 8 Z", fill: color }));
    defs.append(marker);
  }
  root.append(defs);
  for (const group of layout.groups) root.append(shape("rect", {
    "data-flow-group-id": group.id, x: group.x, y: group.y, width: group.width, height: group.height,
    rx: 12, fill: "#f7f8fa", "fill-opacity": ".75", stroke: "#d4d4d8", "stroke-dasharray": "5 4",
  }));
  const edgeLayer = shape("g", { "aria-hidden": true, "pointer-events": "none" });
  for (const edge of layout.edges) {
    const entry = shape("g", { "data-edge-id": edge.id, "data-from": edge.upstream_id, "data-to": edge.downstream_id,
      class: "graph-edge" });
    edge.sections.forEach((section, index) => {
      const end = section.outgoingShape || (!section.outgoingSections?.length && index === edge.sections.length - 1);
      entry.append(shape("path", { d: roundedOrthogonal(section), fill: "none", stroke: edgeColor,
        "stroke-width": "1.6", ...(end ? { "marker-end": "url(#graph-arrow)" } : {}) }));
    });
    edgeLayer.append(entry);
  }
  root.append(edgeLayer);
  layout.groups.forEach((group, index) => {
    root.append(groupHeading(group, index, interactions.focusGroup));
    root.append(...group.nodes.map((node) => nodeCard(node, interactions)));
  });
  return root;
}

export function svgMarkup(layout, title) {
  const width = Math.ceil(layout.width + padding * 2);
  const height = Math.ceil(layout.height + padding * 2);
  const svg = shape("svg", { width, height, viewBox: "0 0 " + width + " " + height });
  const drawing = scene(layout);
  drawing.setAttribute("transform", "translate(" + padding + " " + padding + ")");
  svg.append(shape("title", {}, title), shape("rect", { width, height, fill: "#fff" }), drawing);
  return { width, height, markup: new XMLSerializer().serializeToString(svg) };
}

export async function pngImage(svg) {
  const image = new Image();
  image.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg.markup);
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = svg.width * 2;
  canvas.height = svg.height * 2;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("图谱像素超出当前浏览器的画布容量");
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) => canvas.toBlob((blob) => {
    if (blob) resolve(blob);
    else reject(new Error("图谱像素超出当前浏览器的 PNG 容量"));
  }, "image/png"));
}

export function mermaidSource(layout, direction) {
  const escape = (value) => value.replace(/[\r\n]+/g, " ").replace(/[&"#<>%\[\]\\]/g, (char) => "#" + char.charCodeAt(0) + ";");
  const ids = new Map();
  const lines = ["flowchart " + (direction === "LR" ? "LR" : "TD")];
  for (const [i, group] of layout.groups.entries()) {
    lines.push('  subgraph flow' + (i + 1) + ' ["' + escape(group.title) + '"]');
    for (const { data } of group.nodes) {
      const id = "node" + (ids.size + 1);
      ids.set(data.id, id);
      lines.push("    " + id + '["' + escape(data.title) + '"]');
    }
    lines.push("  end");
  }
  for (const { upstream_id, downstream_id } of layout.edges) lines.push("  " + ids.get(upstream_id) + " --> " + ids.get(downstream_id));
  return lines.join("\n") + "\n";
}

export function graphJson(data, layout, direction) {
  const jobs = new Map();
  for (const node of data.nodes) for (const name of node.occupations || []) {
    if (!jobs.has(name)) jobs.set(name, []);
    jobs.get(name).push(node.id);
  }
  return JSON.stringify({
    domain: { id: data.domain.id, name: data.domain.name },
    direction,
    flow_groups: data.flow_groups,
    nodes: data.nodes,
    edges: data.edges,
    node_links: data.node_links || [],
    occupation_distribution: [...jobs].map(([name, process_node_ids]) => ({ name, process_node_ids })),
    standard_coverage: data.nodes.map(({ id, coverage, authority_counts, missing_t1_standard }) =>
      ({ process_node_id: id, coverage, authority_counts, missing_t1_standard })),
    layout: layout.groups.map(({ id, x, y, width, height, nodes }) => ({ id, x, y, width, height,
      nodes: nodes.map(({ id, x, y, width, height }) => ({ id, x, y, width, height })) })),
  }, null, 2) + "\n";
}

import { $, actions, button, iconButton, popover, saveLocal, segments } from "./workbench-ui.js";
import { arrangeDiagram, diagramModel } from "./workbench-graph-layout.js";
import { graphJson, mermaidSource, pngImage, scene, shape, svgMarkup } from "./workbench-graph-render.js";

const MIN_ZOOM = .25;
const MAX_ZOOM = 2;
const MAP_WIDTH = 160;
const MAP_HEIGHT = 100;
const clamp = (n, min, max) => Math.max(min, Math.min(max, n));

export function processCanvas(ctx, { openNode, references, emptyState }) {
  const model = diagramModel(ctx.data);
  const element = $("section", "graph");
  element.setAttribute("aria-label", "工艺流程画布");
  const tools = $("div", "graph-tools");
  tools.append(button("节点", ctx.addNode, "ghost", "plus"));
  element.append(tools);
  if (!model.nodes.length) {
    const empty = $("div", "graph-empty");
    empty.append(emptyState());
    element.append(empty);
    return { element, focusNode() {} };
  }

  const preference = ctx.viewState.processCanvas ||= { direction: "LR", fitted: true };
  const surface = $("div", "graph-surface");
  surface.tabIndex = 0;
  surface.setAttribute("aria-label", "工艺图谱画布，方向键平移，正负键缩放");
  const svg = shape("svg", { width: "100%", height: "100%", class: "graph-diagram", "aria-label": "工序依赖图" });
  const minimap = shape("svg", { width: MAP_WIDTH, height: MAP_HEIGHT, viewBox: "0 0 160 100", class: "graph-minimap",
    "aria-label": "图谱小地图，点击或拖动定位", role: "img" });
  minimap.style.display = "none";
  const notice = $("p", "graph-notice meta");
  notice.setAttribute("role", "status");
  notice.hidden = true;
  surface.append(svg, minimap);
  element.append(notice, surface);

  let layout, drawing, mapBox, mapWindow;
  let width = 0;
  let height = 0;
  let fitted = preference.fitted;
  let focusedGroup = preference.focusedGroup;
  let view = preference.view ? { ...preference.view } : { x: 0, y: 0, zoom: 1 };
  let detail = true;
  let turn = 0;
  let selected = preference.selected;
  let layoutDirection;
  let exporting = false;
  let arranging = false;
  const pointers = new Map();

  function report(message, error = false) {
    notice.textContent = message;
    notice.hidden = !message;
    notice.classList.toggle("inline-error", error);
  }

  function updateMap() {
    if (!layout || !mapBox) return;
    const overflow = layout.width * view.zoom > width + 1 || layout.height * view.zoom > height + 1;
    minimap.style.display = overflow ? "" : "none";
    if (!overflow) return;
    const left = clamp(-view.x / view.zoom, 0, layout.width);
    const top = clamp(-view.y / view.zoom, 0, layout.height);
    const right = clamp((width - view.x) / view.zoom, 0, layout.width);
    const bottom = clamp((height - view.y) / view.zoom, 0, layout.height);
    mapWindow.setAttribute("x", mapBox.x + left * mapBox.scale);
    mapWindow.setAttribute("y", mapBox.y + top * mapBox.scale);
    mapWindow.setAttribute("width", Math.max(0, right - left) * mapBox.scale);
    mapWindow.setAttribute("height", Math.max(0, bottom - top) * mapBox.scale);
  }

  function setView(x, y, zoom) {
    if (!drawing || !width || !height) return;
    const fullWidth = layout.width * zoom;
    const fullHeight = layout.height * zoom;
    view = {
      zoom,
      x: fullWidth <= width ? (width - fullWidth) / 2 : clamp(x, width / 2 - fullWidth, width / 2),
      y: fullHeight <= height ? (height - fullHeight) / 2 : clamp(y, height / 2 - fullHeight, height / 2),
    };
    Object.assign(preference, { view, fitted, focusedGroup, width, height });
    if (zoom < .60) detail = false;
    if (zoom >= .80) detail = true;
    drawing.dataset.detail = detail ? "full" : "low";
    drawing.setAttribute("transform", "translate(" + view.x + " " + view.y + ") scale(" + zoom + ")");
    reset.textContent = Math.round(zoom * 100) + "%";
    zoomOut.disabled = arranging || zoom <= MIN_ZOOM;
    zoomIn.disabled = arranging || zoom >= MAX_ZOOM;
    updateMap();
  }

  function centered(box, zoom) {
    setView(width / 2 - (box.x + box.width / 2) * zoom, height / 2 - (box.y + box.height / 2) * zoom, zoom);
  }

  function ratio(box) {
    return Math.min(Math.max(1, width - 42) / box.width, Math.max(1, height - 42) / box.height);
  }

  function fit() {
    if (!layout || !width || !height) return;
    fitted = true;
    focusedGroup = null;
    centered({ x: 0, y: 0, ...layout }, Math.min(ratio(layout), 1.65));
  }

  function focusGroup(group) {
    fitted = false;
    focusedGroup = group.id;
    centered(group, Math.min(ratio(group), 1.65));
  }

  function focusNode(id) {
    const box = layout?.groups.flatMap((group) => group.nodes).find((node) => node.id === id);
    if (!box) {
      pendingNode = id;
      return;
    }
    fitted = false;
    focusedGroup = null;
    selected = preference.selected = id;
    const zoom = clamp(ratio(box), MIN_ZOOM, Math.max(1.15, view.zoom));
    centered(box, zoom);
    for (const card of drawing.querySelectorAll(".graph-card")) {
      card.classList.toggle("is-selected", card.dataset.nodeId === selected);
    }
  }

  function zoomTo(zoom, x = width / 2, y = height / 2) {
    if (!layout || arranging) return;
    fitted = false;
    focusedGroup = null;
    const next = clamp(zoom, Math.min(view.zoom, MIN_ZOOM), MAX_ZOOM);
    setView(x - (x - view.x) * next / view.zoom, y - (y - view.y) * next / view.zoom, next);
  }

  function pan(dx, dy) {
    fitted = false;
    focusedGroup = null;
    setView(view.x + dx, view.y + dy, view.zoom);
  }

  function drawMap() {
    const scale = Math.min((MAP_WIDTH - 12) / layout.width, (MAP_HEIGHT - 12) / layout.height);
    mapBox = { scale, x: (MAP_WIDTH - layout.width * scale) / 2, y: (MAP_HEIGHT - layout.height * scale) / 2 };
    const regions = layout.groups.map((group) => shape("rect", {
      x: mapBox.x + group.x * scale, y: mapBox.y + group.y * scale,
      width: group.width * scale, height: group.height * scale, rx: 2, fill: "#d4d4d8", stroke: "#a1a1aa", "stroke-width": ".5",
    }));
    mapWindow = shape("rect", { rx: 2, fill: "#4f46e5", "fill-opacity": ".10", stroke: "#4f46e5", "stroke-width": "1.25" });
    minimap.replaceChildren(...regions, mapWindow);
  }

  const locateOnMap = (event) => {
    if (!mapBox) return;
    event.stopPropagation();
    const rect = minimap.getBoundingClientRect();
    const x = (event.clientX - rect.left - mapBox.x) / mapBox.scale;
    const y = (event.clientY - rect.top - mapBox.y) / mapBox.scale;
    fitted = false;
    focusedGroup = null;
    centered({ x, y, width: 0, height: 0 }, view.zoom);
  };
  minimap.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    minimap.setPointerCapture(event.pointerId);
    locateOnMap(event);
  });
  minimap.addEventListener("pointermove", (event) => {
    if (minimap.hasPointerCapture(event.pointerId)) locateOnMap(event);
  });

  function highlight(id) {
    for (const edge of drawing.querySelectorAll(".graph-edge")) {
      const connected = edge.dataset.from === id || edge.dataset.to === id;
      edge.classList.toggle("is-active", Boolean(id && connected));
      edge.classList.toggle("is-dim", Boolean(id && !connected));
    }
  }

  function loadScene(next) {
    layout = next;
    drawing = scene(next, {
      references,
      navigate: ctx.navigate,
      openNode: (box) => { focusNode(box.id); openNode(box.id); },
      focusGroup,
    });
    for (const card of drawing.querySelectorAll(".graph-card")) {
      const id = card.dataset.nodeId;
      card.classList.toggle("is-selected", id === selected);
      card.addEventListener("pointerenter", () => highlight(id));
      card.addEventListener("pointerleave", () => highlight(""));
      card.addEventListener("focus", () => highlight(id));
      card.addEventListener("blur", () => highlight(""));
    }
    svg.replaceChildren(drawing);
    drawMap();
    const remembered = preference.view;
    const group = next.groups.find(({ id }) => id === focusedGroup);
    if (!remembered && ratio(layout) < .60) focusGroup(layout.groups[0]);
    else if (fitted || !remembered) fit();
    else if (group) focusGroup(group);
    else setView(remembered.x + (width - preference.width) / 2, remembered.y + (height - preference.height) / 2, remembered.zoom);
    if (pendingNode) {
      const id = pendingNode;
      pendingNode = "";
      if (model.nodes.some((node) => node.id === id)) focusNode(id);
    }
  }

  let pendingNode = "";
  const menu = $("div", "graph-export-menu");
  const exporter = popover("导出", menu, { appearance: "ghost", align: "end", symbol: "download" });
  const arrange = button("自动排布", () => relayout(true), "ghost", "layout");
  const zoomOut = iconButton("缩小图谱", () => zoomTo(view.zoom / 1.2), "minus");
  const zoomIn = iconButton("放大图谱", () => zoomTo(Math.max(MIN_ZOOM, view.zoom * 1.2)), "plus");
  const reset = button("100%", () => zoomTo(1), "ghost graph-zoom-value mono");
  reset.setAttribute("aria-label", "复位为 100%");
  reset.title = "复位为 100%";
  const fitButton = button("适应全图", fit, "ghost", "fit");
  const zoomControls = actions(zoomOut, reset, zoomIn);
  zoomControls.classList.add("graph-zoom");
  const modes = [["LR", "横版流向 (LR)"], ["TB", "树状纵版 (TB)"]];
  const orientation = segments("图谱排布方向", modes, preference.direction, (direction) => {
    if (preference.direction === direction && layout) return;
    preference.direction = direction;
    [...orientation.children].forEach((b, index) => b.setAttribute("aria-pressed", String(modes[index][0] === direction)));
    relayout(true);
  });
  tools.append(orientation, arrange, zoomControls, fitButton, exporter.element);

  function syncControls() {
    const busy = !layout || arranging || layoutDirection !== preference.direction;
    exporter.trigger.disabled = busy || exporting;
    fitButton.disabled = reset.disabled = busy;
    zoomOut.disabled = busy || view.zoom <= MIN_ZOOM;
    zoomIn.disabled = busy || view.zoom >= MAX_ZOOM;
  }

  async function relayout(resetView = false) {
    const id = ++turn;
    arranging = true;
    surface.setAttribute("aria-busy", "true");
    if (resetView) fitted = true;
    report("正在自动排布…");
    syncControls();
    try {
      const direction = preference.direction;
      const next = await arrangeDiagram(model, direction);
      if (turn !== id || !element.isConnected) return;
      layoutDirection = direction;
      loadScene(next);
      report("");
    } catch (error) {
      if (turn === id && element.isConnected) report("图谱排布失败。可点击自动排布重试。" + (error.message || ""), true);
    } finally {
      if (turn === id) {
        arranging = false;
        surface.setAttribute("aria-busy", "false");
        syncControls();
      }
    }
  }

  async function exportGraph(kind) {
    if (!layout || arranging || exporting || layoutDirection !== preference.direction) return;
    exporter.close();
    exporting = true;
    report(kind === "copy" ? "" : "正在导出完整图谱…");
    syncControls();
    try {
      const title = ctx.data.domain.name + " · 工艺图谱";
      const filename = title.replace(/[\/\\:*?"<>|]/g, "-");
      const direction = layoutDirection;
      if (kind === "svg" || kind === "png") {
        const vector = svgMarkup(layout, title);
        if (kind === "svg") saveLocal(filename + ".svg", vector.markup, "image/svg+xml");
        else saveLocal(filename + ".png", await pngImage(vector), "image/png");
      } else if (kind === "json") {
        saveLocal(filename + ".json", graphJson(ctx.data, layout, direction), "application/json");
      } else {
        const source = mermaidSource(layout, direction);
        if (kind === "copy") await navigator.clipboard.writeText(source);
        else saveLocal(filename + ".mmd", source, "text/plain;charset=utf-8");
      }
      report(kind === "copy" ? "已复制 Mermaid 源码" : "已导出完整图谱");
    } catch (error) {
      report(kind === "copy" ? "浏览器未允许写入剪贴板，可下载 Mermaid 源码。" : "导出失败。" + error.message, true);
    } finally {
      exporting = false;
      syncControls();
    }
  }

  for (const [kind, label] of [["svg", "下载 SVG 矢量图"], ["png", "下载 PNG 图片（2×）"],
    ["mermaid", "下载 Mermaid 源码"], ["copy", "复制 Mermaid 源码"], ["json", "下载结构化 JSON"]]) {
    menu.append(button(label, () => exportGraph(kind), "ghost"));
  }

  function point(event) {
    const rect = surface.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  surface.addEventListener("wheel", (event) => {
    if (!layout || event.target.closest(".graph-minimap")) return;
    event.preventDefault();
    const { x, y } = point(event);
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1);
    zoomTo(view.zoom * Math.exp(-delta * (event.ctrlKey ? .007 : .0018)), x, y);
  }, { passive: false });

  const gesture = () => {
    const [a, b] = pointers.values();
    return b ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, distance: Math.hypot(a.x - b.x, a.y - b.y) } : a;
  };

  surface.addEventListener("pointerdown", (event) => {
    if (!layout || arranging || event.button !== 0 || event.target.closest(".graph-minimap") || pointers.size >= 2) return;
    const control = Boolean(event.target.closest("[data-graph-action]"));
    if (control && event.pointerType !== "touch") return;
    pointers.set(event.pointerId, { ...point(event), control });
    if (!control || pointers.size === 2) {
      for (const id of pointers.keys()) surface.setPointerCapture(id);
      surface.classList.add("is-panning");
    }
  });
  surface.addEventListener("pointermove", (event) => {
    const pointer = pointers.get(event.pointerId);
    if (!pointer) return;
    const before = gesture();
    pointers.set(event.pointerId, { ...point(event), control: pointer.control });
    const after = gesture();
    if (pointers.size === 2 && before.distance) zoomTo(view.zoom * after.distance / before.distance, before.x, before.y);
    if (pointers.size === 2 || !pointer.control) pan(after.x - before.x, after.y - before.y);
  });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) surface.addEventListener(name, (event) => {
    pointers.delete(event.pointerId);
    if (!pointers.size) surface.classList.remove("is-panning");
  });
  surface.addEventListener("keydown", (event) => {
    if (!layout || event.defaultPrevented) return;
    const step = 48;
    const offset = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[event.key];
    if (offset) pan(...offset);
    else if (event.key === "=" || event.key === "+") zoomTo(Math.max(MIN_ZOOM, view.zoom * 1.2));
    else if (event.key === "-") zoomTo(view.zoom / 1.2);
    else if (event.key === "0") fit();
    else return;
    event.preventDefault();
  });

  const resize = new ResizeObserver(([entry]) => {
    if (!surface.isConnected) {
      resize.disconnect();
      return;
    }
    const previous = { width, height };
    ({ width, height } = entry.contentRect);
    if (!layout) return;
    const group = layout.groups.find(({ id }) => id === focusedGroup);
    if (fitted) fit();
    else if (group) focusGroup(group);
    else setView(view.x + (width - previous.width) / 2, view.y + (height - previous.height) / 2, view.zoom);
  });
  queueMicrotask(() => {
    if (!element.isConnected) return;
    width = surface.clientWidth;
    height = surface.clientHeight;
    resize.observe(surface);
    relayout();
  });
  syncControls();
  return { element, focusNode };
}

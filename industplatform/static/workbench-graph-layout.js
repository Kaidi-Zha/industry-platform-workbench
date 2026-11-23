const NODE_WIDTH = 232;
const NODE_HEIGHT = 104;

export function diagramModel(data) {
  const aliases = new Map((data.node_links || []).map(({ node_id, canonical_id }) => [node_id, canonical_id]));
  const nodes = data.nodes.filter((node) => !aliases.has(node.id));
  const ids = new Set(nodes.map(({ id }) => id));
  const visible = new Map();
  for (const edge of data.edges) {
    const upstream_id = aliases.get(edge.upstream_id) || edge.upstream_id;
    const downstream_id = aliases.get(edge.downstream_id) || edge.downstream_id;
    if (upstream_id !== downstream_id && ids.has(upstream_id) && ids.has(downstream_id)) {
      visible.set(upstream_id + "/" + downstream_id, { upstream_id, downstream_id });
    }
  }
  const groups = data.flow_groups.map((group) => ({ ...group, node_ids: group.node_ids.filter((id) => ids.has(id)) }))
    .filter((group) => group.node_ids.length);
  return { nodes, groups, edges: [...visible.values()] };
}

const GROUP_PADDING = { top: 62, left: 20, right: 20, bottom: 22 };
const inset = { x: GROUP_PADDING.left, y: GROUP_PADDING.top };
const boxX = NODE_WIDTH + GROUP_PADDING.left + GROUP_PADDING.right;
const boxY = NODE_HEIGHT + GROUP_PADDING.top + GROUP_PADDING.bottom;

function crosses(a, b, box) {
  const pad = 3;
  if (a.x === b.x) return a.x > box.x - pad && a.x < box.x + box.width + pad &&
    Math.min(a.y, b.y) < box.y + box.height + pad && Math.max(a.y, b.y) > box.y - pad;
  return a.y > box.y - pad && a.y < box.y + box.height + pad &&
    Math.min(a.x, b.x) < box.x + box.width + pad && Math.max(a.x, b.x) > box.x - pad;
}

function routed(points, obstacles, horizontal) {
  const polyline = [points[0]];
  for (let i = 1; i < points.length; i++) {
    const a = polyline.at(-1);
    const b = points[i];
    if (a.x !== b.x && a.y !== b.y) {
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      const column = [{ x: mx, y: a.y }, { x: mx, y: b.y }];
      const row = [{ x: a.x, y: my }, { x: b.x, y: my }];
      const alternatives = horizontal ? [column, row] : [row, column];
      alternatives.push([{ x: a.x, y: b.y }], [{ x: b.x, y: a.y }]);
      const clear = alternatives.find((corners) => {
        const route = [a, ...corners, b];
        return route.slice(1).every((end, j) => !obstacles.some((box) => crosses(route[j], end, box)));
      });
      // When an orthogonal detour would cross a card, keep the original Dagre waypoint segment.
      if (clear) polyline.push(...clear);
    }
    polyline.push(b);
  }
  return polyline.filter((point, index) => {
    const current = polyline[index - 1];
    const next = polyline[index + 1];
    if (!current || !next) return true;
    return !((current.x === point.x && point.x === next.x) || (current.y === point.y && point.y === next.y));
  });
}

function groupBox(group, nodes) {
  const x = Math.min(...nodes.map((node) => node.x)) - GROUP_PADDING.left;
  const y = Math.min(...nodes.map((node) => node.y)) - GROUP_PADDING.top;
  const right = Math.max(...nodes.map((node) => node.x + node.width)) + GROUP_PADDING.right;
  const bottom = Math.max(...nodes.map((node) => node.y + node.height)) + GROUP_PADDING.bottom;
  return { ...group, x, y, width: right - x, height: bottom - y, nodes };
}

function port(box, direction, outgoing) {
  const { x, y, width, height } = box;
  return direction === "LR"
    ? { x: x + (outgoing ? width : 0), y: y + height / 2 }
    : { x: x + width / 2, y: y + (outgoing ? height : 0) };
}

export function arrangeWithDagre(model, direction, dagre) {
  const graph = new dagre.Graph({ compound: true }).setGraph({
    rankdir: direction, nodesep: 56, ranksep: 50, edgesep: 20, marginx: 4, marginy: 4,
  });
  const keys = new Map(model.nodes.map(({ id }, index) => [id, "n" + index]));
  for (const { id, node_ids } of model.groups) {
    graph.setNode(id, {});
    for (const original of node_ids) {
      const key = keys.get(original);
      graph.setNode(key, { width: boxX, height: boxY });
      graph.setParent(key, id);
    }
  }
  for (const { upstream_id, downstream_id } of model.edges) graph.setEdge(keys.get(upstream_id), keys.get(downstream_id), {});
  dagre.layout(graph);

  const boxes = new Map(model.nodes.map((data) => {
    const position = graph.node(keys.get(data.id));
    const x = position.x - boxX / 2 + inset.x;
    const y = position.y - boxY / 2 + inset.y;
    return [data.id, { id: data.id, data, x, y, width: NODE_WIDTH, height: NODE_HEIGHT }];
  }));
  const groups = model.groups.map((group) => groupBox(group, group.node_ids.map((id) => boxes.get(id))));
  const headers = groups.map(({ x, y, width }) => ({ x, y, width, height: 51 }));
  const allBoxes = [...boxes.values()];
  const edges = model.edges.map((edge, i) => {
    const from = boxes.get(edge.upstream_id);
    const to = boxes.get(edge.downstream_id);
    const bends = graph.edge(keys.get(from.id), keys.get(to.id)).points.slice(1, -1);
    const obstacles = [...headers, ...allBoxes.filter((box) => box !== from && box !== to)];
    const points = routed([port(from, direction, true), ...bends, port(to, direction, false)], obstacles, direction === "LR");
    return { ...edge, id: "edge-" + i, sections: [{
      startPoint: points[0], endPoint: points.at(-1), bendPoints: points.slice(1, -1), outgoingShape: to.id,
    }] };
  });
  const bounds = graph.graph();
  return { groups, edges, width: bounds.width, height: bounds.height };
}

export async function arrangeDiagram(model, direction) {
  const dagre = await import("./vendor/dagre-3.1.3.mjs");
  return arrangeWithDagre(model, direction, dagre);
}

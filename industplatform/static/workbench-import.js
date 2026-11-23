import { $, actions, button, external, get, post, request } from "./workbench-ui.js";

const path = "/api/workbench/collaboration/imports";
const fileType = /\.(html?|pdf|csv)$/i;
const mib = 1024 * 1024;

function batchLabel(batch) {
  if (batch.status === "staging") return `已读取 ${batch.received} / ${batch.expected} 个文件`;
  if (batch.status === "queued") return "等待 Agent 整理";
  if (batch.status === "running") return `正在整理 ${batch.processed} / ${batch.candidates} 条线索`;
  if (batch.status === "failed") return "整理中断";
  if (batch.status === "empty") return "未生成可共享的条目";
  const skipped = batch.candidates - batch.count;
  return `${batch.count} 条待核验信源` + (skipped > 0 ? ` · 筛除 ${skipped} 条` : "");
}

function batchDetails(batch) {
  const details = $("details");
  const summary = $("summary", "", "查看条目与文件反馈");
  const body = $("div", "sync-import-records");
  details.append(summary, body);
  let loaded = false;
  async function show(offset) {
    body.textContent = "正在读取…";
    try {
      const result = await request(path + "?" + new URLSearchParams({ batch: batch.id, offset }));
      const nodes = [];
      if (result.records.length) {
        const list = $("ul");
        for (const record of result.records) {
          const row = $("li");
          const link = external(record.title, record.url);
          link.title = record.url;
          row.append(link, $("p", "sync-meta sync-mono", [record.l2_code, record.object_cluster,
            record.lifecycle_stages.join(", ")].join(" · ")), $("p", "sync-meta", record.summary_zh));
          list.append(row);
        }
        nodes.push(list);
      }
      if (result.notes.length) {
        const warnings = $("ul", "sync-meta");
        for (const note of result.notes) warnings.append($("li", "", note.name + " · " + note.message));
        nodes.push(warnings);
      }
      if (offset > 0 || result.next_offset !== null) {
        const back = button("上一页", () => show(Math.max(0, offset - 40)), "ghost sync-text-button");
        const next = button("下一页", () => show(result.next_offset), "ghost sync-text-button");
        back.disabled = offset === 0;
        next.disabled = result.next_offset === null;
        nodes.push(actions(back, $("span", "sync-meta", `${offset + 1}–${offset + result.records.length} / ${batch.count}`), next));
      }
      body.replaceChildren(...(nodes.length ? nodes : [$("p", "sync-meta", "暂无条目或文件反馈")]));
    } catch (error) {
      body.textContent = error.message;
      loaded = false;
    }
  }
  details.addEventListener("toggle", () => {
    if (!details.open || loaded) return;
    loaded = true;
    show(0);
  });
  return details;
}

export function localImport(refresh, selectBatch) {
  const toggle = get("sync-import-toggle");
  const picker = get("sync-import-picker");
  const readFiles = get("sync-import-files");
  const readFolder = get("sync-import-folder");
  const chooseFiles = get("sync-import-choose-files");
  const chooseFolder = get("sync-import-choose-folder");
  const prepare = get("sync-import-prepare");
  const source = get("sync-import-source");
  const selection = get("sync-import-selection");
  const message = get("sync-import-message");
  const container = get("sync-import-batches");
  let limits;
  let files = [];
  let batchId = "";
  let selectedBatch = "";
  let sent = 0;
  let busy = false;
  const rows = new Map();

  function notice(text, danger = false) {
    message.textContent = text;
    message.hidden = !text;
    message.classList.toggle("sync-danger", danger);
  }

  function inputState() {
    prepare.disabled = !files.length || busy;
    prepare.textContent = busy ? "正在读取…" : sent ? "继续整理" : "整理并入库";
    chooseFiles.disabled = chooseFolder.disabled = busy || !limits;
    source.disabled = busy || sent > 0;
    get("sync-import-source-label").hidden = files.length !== 1;
  }

  function open(value) {
    picker.hidden = !value;
    toggle.setAttribute("aria-expanded", String(value));
    if (value) chooseFiles.focus();
  }

  function take(input) {
    const picked = [...input.files];
    input.value = "";
    if (!picked.length) return;
    batchId = "";
    sent = 0;
    source.value = "";
    selection.hidden = true;
    files = picked.filter((file) => fileType.test(file.name) && file.size > 0 && file.size <= limits.file_bytes
      && !(file.webkitRelativePath || file.name).split("/").some((part) => part.startsWith(".")));
    const ignored = picked.length - files.length;
    const bytes = files.reduce((sum, file) => sum + file.size, 0);
    if (files.length > limits.files || bytes > limits.batch_bytes) {
      files = [];
      notice(`每批最多 ${limits.files} 个文件，合计 ${limits.batch_bytes / mib} MiB。请选择较小的批次。`, true);
    } else if (!files.length) {
      notice(`未找到可读取的文件。支持单个 ${limits.file_bytes / mib} MiB 以内的 HTML、PDF 和 CSV。`, true);
    } else {
      notice("");
      selection.hidden = false;
      selection.textContent = (files.length === 1 ? files[0].name : `已选择 ${files.length} 个文件`)
        + (ignored ? ` · 略过 ${ignored} 个不支持的文件` : "");
    }
    inputState();
  }

  toggle.addEventListener("click", () => open(picker.hidden));
  chooseFiles.addEventListener("click", () => readFiles.click());
  chooseFolder.addEventListener("click", () => readFolder.click());
  readFiles.addEventListener("change", () => take(readFiles));
  readFolder.addEventListener("change", () => take(readFolder));
  prepare.addEventListener("click", async () => {
    const origin = files.length === 1 ? source.value.trim() : "";
    if (origin) {
      try {
        if (new URL(origin).protocol !== "https:") throw new Error();
      } catch {
        notice("原文链接须为公开 HTTPS 地址", true);
        source.focus();
        return;
      }
    }
    busy = true;
    inputState();
    try {
      if (!batchId) batchId = (await post(path, { action: "create", count: files.length })).id;
      for (; sent < files.length; sent++) {
        notice(`正在本机读取 ${sent + 1} / ${files.length}…`);
        const file = files[sent];
        const params = new URLSearchParams({ batch: batchId, index: String(sent) });
        await request(path + "/file?" + params, { method: "POST", body: file, headers: {
          "Content-Type": "application/octet-stream", "X-Indust-File-Name": encodeURIComponent(file.name),
          "X-Indust-Source-URL": encodeURIComponent(origin),
        } });
      }
      await post(path, { action: "prepare", batch: batchId });
      selectedBatch = batchId;
      files = [];
      batchId = "";
      sent = 0;
      source.value = "";
      selection.hidden = true;
      if (!picker.hidden) toggle.focus();
      open(false);
      notice("");
    } catch (error) {
      notice(error.message, true);
    } finally {
      busy = false;
      inputState();
      refresh(true);
    }
  });

  async function change(action, batch, trigger) {
    trigger.disabled = true;
    try {
      await post(path, { action, batch });
      if (action === "prepare") selectedBatch = batch;
      if (action === "discard" && batch === batchId) {
        batchId = "";
        sent = 0;
        inputState();
      }
      notice("");
    } catch (error) {
      notice(error.message, true);
    } finally {
      trigger.disabled = false;
      refresh(true);
    }
  }

  function makeRow(batch) {
    const row = $("article", "sync-import-batch");
    const heading = $("div", "sync-import-batch-head");
    heading.append($("span", "", `${batch.expected} 个本地文件 · ${batchLabel(batch)}`));
    if (["staging", "ready", "empty", "failed"].includes(batch.status)) {
      const controls = actions();
      if (batch.status === "failed" || (batch.status === "staging" && batch.received === batch.expected)) {
        controls.append(button(batch.status === "failed" ? "重试整理" : "整理", (event) =>
          change("prepare", batch.id, event.currentTarget), "ghost sync-text-button"));
      }
      if (!batch.library_imported) controls.append(button("移除", (event) => change("discard", batch.id, event.currentTarget), "ghost sync-text-button"));
      if (controls.children.length) heading.append(controls);
    }
    row.append(heading);
    if (batch.model) row.append($("p", "sync-meta", batch.model));
    if (batch.error) row.append($("p", "sync-error", batch.error));
    if (batch.status === "staging" && batch.received < batch.expected && batch.id !== batchId) {
      row.append($("p", "sync-meta", "如已离开选取页面，请移除这批后重新选择文件。"));
    }
    if (batch.status === "running") {
      const progress = $("progress");
      progress.max = batch.candidates;
      progress.value = batch.processed;
      progress.setAttribute("aria-label", "整理进度");
      row.append(progress);
    }
    if (["ready", "empty", "failed"].includes(batch.status)) row.append(batchDetails(batch));
    return row;
  }

  return (state) => {
    limits = state?.import_limits;
    inputState();
    if (selectedBatch && state?.runs.some((run) => run.id === selectedBatch && run.selectable)) {
      selectBatch(selectedBatch);
      selectedBatch = "";
    }
    const batches = state?.imports || [];
    const current = new Set();
    let location = container.firstElementChild;
    for (const batch of batches) {
      current.add(batch.id);
      const key = JSON.stringify(batch);
      const cached = rows.get(batch.id);
      const node = cached?.key === key ? cached.node : makeRow(batch);
      if (cached && cached.node !== node) {
        if (cached.node === location) location = location.nextElementSibling;
        cached.node.remove();
      }
      if (node !== location) container.insertBefore(node, location);
      location = node.nextElementSibling;
      rows.set(batch.id, { key, node });
    }
    for (const [id, { node }] of rows) if (!current.has(id)) {
      node.remove();
      rows.delete(id);
    }
  };
}

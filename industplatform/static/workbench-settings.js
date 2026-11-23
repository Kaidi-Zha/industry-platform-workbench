import { $, actions, button, column, field, heading, href, post, request } from "./workbench-ui.js";
import { profileUsable } from "./workbench-models.js";
import { settingsSections } from "./workbench-router.js";
import { collaborationSettings } from "./workbench-collaboration.js";

export function settingsTabs(ctx) {
  const nav = $("nav", "segments settings-tabs");
  nav.setAttribute("aria-label", "设置分类");
  for (const [id, label] of settingsSections) {
    const link = href(label, ctx.route("settings" + (id ? "/" + id : "")), "button");
    if (ctx.section === id) link.setAttribute("aria-current", "page");
    nav.append(link);
  }
  return nav;
}

function labelFor(name, label, value, choices) {
  if (typeof value === "boolean") {
    const row = $("label", "preference-toggle");
    const input = $("input");
    Object.assign(input, { type: "checkbox", name, checked: value });
    row.append(input, $("span", "", label));
    return row;
  }
  const control = field(name, label, { value, as: choices ? "select" : "textarea", options: choices, limit: 1200 });
  if (name === "agent_guidance") control.classList.add("wide");
  return control;
}

function settingsFields(ctx, snapshot, profiles) {
  const preferences = snapshot.preferences;
  const integer = (min, max) => Array.from({ length: max - min + 1 }, (_, i) => [min + i, String(min + i)]);
  const sections = {
    permissions: [
      ["sandbox_mode", "文件权限", [["read-only", "只读"], ["workspace-write", "工作区可写"]]],
      ["approval_mode", "默认审批方式", [["never", "拒绝需审批的操作"], ["ask", "问我"], ["auto_review", "帮我审批"]]],
      ["workspace_network_access", "允许可写沙箱访问终端网络"],
      ["approval_model_native", "本机 Codex 审批模型", [["", "Codex 默认"],
        ...snapshot.native_approval_models.map(({ id, name }) => [id, name])]],
      ["approval_model_api", "API 对话审批模型", [["", "使用对话模型"], ...profiles
        .filter((item) => profileUsable(item) && !/astra/i.test(item.model))
        .map(({ id, name, model }) => [id, name + " · " + model])]],
    ],
    agent: [
      ["history_turns", "带入最近对话轮数", integer(0, 3)], ["max_model_turns", "每轮模型请求数", integer(2, 8)],
      ["max_child_runs", "每轮子工作流数", integer(0, 2)], ["max_authored_tasks", "每轮合成题数", integer(0, 2)],
      ["max_tool_calls", "每轮工业工具调用数", integer(1, 12)], ["wait_seconds", "单次等待上限（秒）", integer(1, 15)],
      ["allow_keyword_tools", "开放词表与工程规则工具"], ["agent_guidance", "工作区指引"],
    ],
    appearance: [
      ["theme", "主题", [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]]],
      ["density", "显示密度", [["comfortable", "舒适"], ["compact", "紧凑"]]],
    ],
    shortcuts: [
      ["send_shortcut", "发送消息", [["mod-enter", "⌘ / Ctrl + Enter"], ["enter", "Enter"]]],
      ["resume_last_chat", "启动时打开最近的对话"],
    ],
  };
  const definitions = sections[ctx.section];
  const form = $("form", "bench-form");
  const inputs = $("div", "business-fields");
  inputs.append(...definitions.map(([name, label, choices]) => labelFor(name, label, preferences[name], choices)));
  if (ctx.section === "permissions") {
    const row = inputs.querySelector('[name="workspace_network_access"]').closest("label");
    const sandbox = inputs.querySelector('[name="sandbox_mode"]');
    const update = () => { row.hidden = sandbox.value !== "workspace-write"; };
    sandbox.closest(".popover-select").addEventListener("change", update);
    update();
  }
  if (ctx.section === "shortcuts") inputs.append($("p", "meta wide", "Shift + Enter 换行 · ⌘ / Ctrl + Shift + , 打开设置"));
  const feedback = $("p", "meta");
  feedback.setAttribute("role", "status");
  const save = button("保存", null, "primary");
  save.type = "submit";
  form.append(inputs, actions(save, feedback));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    const values = new FormData(form);
    const changes = Object.fromEntries(definitions.map(([name]) => {
      const old = preferences[name];
      return [name, typeof old === "boolean" ? values.has(name)
        : typeof old === "number" ? Number(values.get(name)) : values.get(name)];
    }));
    try {
      await post("/api/settings", changes);
      await ctx.reloadConfig();
      feedback.textContent = "已保存";
      feedback.classList.remove("inline-error");
    } catch (error) {
      feedback.textContent = error.message;
      feedback.classList.add("inline-error");
    } finally { save.disabled = false; }
  });
  return form;
}

function storageFields(ctx, snapshot, content) {
  const { storage } = snapshot;
  const { database_path, database_bytes, run_count, conversation_count, profile_count } = storage;
  const size = database_bytes >= 1024 ** 2 ? [database_bytes / 1024 ** 2, "MiB"] : [database_bytes / 1024, "KiB"];
  const rows = $("dl", "storage-details");
  const credentials = { "encrypted-file": "本地加密凭据库", session: "仅当前会话", unavailable: "暂不可用" };
  for (const [term, value] of [
    ["数据库路径", database_path], ["数据库大小", size[0].toFixed(1) + " " + size[1]],
    ["运行记录", run_count], ["对话", conversation_count], ["模型配置", profile_count],
    ["凭据存储", credentials[storage.credential_store] || "未知"],
  ]) {
    rows.append($("dt", "", term), $("dd", term === "数据库路径" ? "mono" : "", String(value)));
  }
  const move = button("更换位置", () => ctx.editor("更换数据库位置", [
    field("path", "新数据库文件的绝对路径", { required: true, limit: 1024 }),
    $("p", "meta", "迁移后立即切换，原位置保留备份。"),
  ], "迁移并切换", async (fields) => {
    const updated = await post("/api/storage/move", fields);
    await ctx.reloadConfig();
    content.replaceChildren(storageFields(ctx, updated, content));
  }));
  return column(rows, move);
}

export function settingsSectionView(ctx) {
  const content = $("div", "settings-section-body");
  const label = settingsSections.find(([id]) => id === ctx.section)[1];
  const title = heading("网关与设置", "", settingsTabs(ctx));
  const page = column(title, $("h2", "", label), content);
  if (ctx.section === "collaboration") {
    content.append(collaborationSettings(ctx));
    return page;
  }
  const status = $("p", "muted", "正在读取设置…");
  content.append(status);
  Promise.all([request("/api/settings"), ctx.section === "permissions"
    ? request("/api/model-profiles").then(({ profiles }) => profiles) : []]).then(([snapshot, profiles]) => {
    content.replaceChildren(ctx.section === "storage" ? storageFields(ctx, snapshot, content) : settingsFields(ctx, snapshot, profiles));
  }).catch((error) => { status.textContent = error.message; });
  return page;
}

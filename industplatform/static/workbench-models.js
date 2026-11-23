import { $, actions, button, icon, iconButton, popover, popoverSelect, post } from "./workbench-ui.js";

const profilePath = "/api/model-profiles";
export const effortNames = { none: "关闭", minimal: "极低", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高", ultra: "超高" };
export const codexSubscription = "codex_subscription";
export const apiGateway = "api_gateway";
export const executionModes = [[codexSubscription, "本地 Codex 订阅"], [apiGateway, "API 模型网关"]];
export const nativeStatusLabels = {
  unchecked: "尚未检测", ready_subscription: "已复用本地 Codex 登录", unauthenticated: "需要登录 ChatGPT",
  conflict_apikey: "本机使用 API Key", cli_missing: "未检测到 CLI", probe_failed: "检测失败",
};
export const tierNames = { general: "轻量 (Tier 1)", advanced: "均衡 (Tier 2)", frontier: "旗舰 (Tier 3)" };
export const evaluationTiers = { "": "不参与", ...tierNames };
const loopback = new Set(["localhost", "127.0.0.1", "[::1]"]);

export function profileUsable(profile) {
  try {
    return profile.enabled !== false && (profile.has_key || loopback.has(new URL(profile.base_url).hostname));
  } catch {
    return false;
  }
}

export function capabilityTags(model) {
  const tags = $("span", "model-capabilities");
  const levels = (model.reasoning_efforts || []).filter(Boolean);
  if (levels.length) {
    const effort = $("span", "protocol-label", "思考 · " + levels.length + " 档");
    effort.title = levels.map((level) => effortNames[level] || level).join("、");
    tags.append(effort);
  }
  if (model.vision_supported) tags.append($("span", "protocol-label", "视觉"));
  if (model.parallel_tools_supported) tags.append($("span", "protocol-label", "并行工具"));
  return tags;
}

export function enabledNativeModels(native, settings) {
  const disabled = new Set(settings.disabled_codex_models || []);
  return native?.status === "ready_subscription" && !native.catalog_error ?
    (native.models || []).filter(({ id }) => !disabled.has(id)) : [];
}

export function defaultNativeModel(native, settings, requested = settings.default_codex_model) {
  const models = enabledNativeModels(native, settings);
  for (const choice of [requested, settings.default_codex_model]) {
    const model = models.find(({ id }) => id === choice) || models.find(({ model }) => model === choice);
    if (model) return model;
  }
  return models.find(({ is_default }) => is_default) || models[0];
}

export function nativeEffortSummary(model) {
  const levels = model.reasoning_efforts;
  const effort = model.default_effort;
  if (!effort && !levels.length) return null;
  const summary = $("span", "meta", effort ? "默认思考 " + (effortNames[effort] || effort) : "思考 " + levels.length + " 档");
  if (levels.length) summary.title = "支持 " + levels.map((level) => effortNames[level] || level).join("、");
  return summary;
}

export function executionModeTabs(selected, change) {
  const group = $("div", "execution-mode-tabs");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "执行模式");
  const buttons = new Map(executionModes.map(([mode, title]) => [mode, button(title, () => change(mode), "ghost")]));
  group.append(...buttons.values());
  const update = (mode) => {
    for (const [id, option] of buttons) option.setAttribute("aria-pressed", String(id === mode));
  };
  update(selected);
  return { element: group, update };
}

export function nativeStatus(status, text = nativeStatusLabels[status] || nativeStatusLabels.unchecked) {
  const line = $("span", "codex-status");
  const dot = $("span", "dot");
  dot.dataset.tone = status === "ready_subscription" ? "success" :
    status === "unchecked" || status === "cli_missing" ? "neutral" : "warning";
  dot.setAttribute("aria-hidden", "true");
  line.append(dot, $("span", "", text));
  return line;
}

export function nativeVersion(native) {
  return native?.version ? "v" + native.version : "";
}

export function modelPicker(config, value, onSelect, openSettings, detect) {
  const list = $("div", "grouped-models execution-models");
  list.setAttribute("role", "dialog");
  list.setAttribute("aria-label", "选择模型");
  const popup = popover("选择模型", list, { appearance: "execution-model-trigger", align: "end", preferAbove: true });
  popup.element.classList.add("model-picker");
  const body = $("div", "execution-model-body");
  const tabs = executionModeTabs(value.mode, showMode);
  const refreshButton = iconButton("刷新本机订阅模型", () => probe(true), "retry", "ghost codex-model-refresh");
  let detecting = false;
  let forceRefresh = false;
  let retryWith;
  list.append(tabs.element, body);

  function updateCaption() {
    const local = value.mode === codexSubscription;
    const model = local ? enabledNativeModels(config.native, config.settings).find(({ id }) => id === value.codexModel) :
      config.profiles.find(({ id }) => id === value.profile);
    const prefix = local ? "Codex 订阅" : model?.name || "API 模型";
    const label = model && (local ? model.name : model.model);
    const marker = local ? nativeStatus(config.native?.status).firstChild : icon("bolt");
    if (local && config.native?.status !== "ready_subscription") marker.dataset.tone = "warning";
    popup.caption.replaceChildren(marker, $("span", "truncate", label ? prefix + " · " + label : prefix));
    popup.trigger.setAttribute("aria-label", "模型连接，" + popup.caption.textContent);
  }

  function row(mode, item) {
    const selected = item.id === (mode === codexSubscription ? value.codexModel : value.profile);
    const pick = button("", () => {
      const retry = retryWith;
      onSelect(mode, item.id);
      refresh();
      popup.close(true);
      retry?.(mode, item);
    }, "select-option model-option execution-model-row");
    pick.dataset.selected = String(selected);
    pick.setAttribute("aria-pressed", String(selected));
    const local = mode === codexSubscription;
    const title = local ? item.name : item.model;
    const hint = $("span", "execution-model-hint");
    if (local) {
      const summary = nativeEffortSummary(item);
      if (summary) hint.append(summary);
      if (item.is_default) hint.append($("span", "protocol-label", "默认"));
    } else {
      const tier = tierNames[item.tier]?.match(/Tier \d/)?.[0];
      if (tier) hint.append($("span", "meta", tier));
    }
    pick.title = title;
    pick.append($("span", "truncate", title));
    if (hint.childElementCount) pick.append(hint);
    pick.append(icon("check"));
    return pick;
  }

  function settingsButton(label) {
    return button(label, () => { popup.close(); openSettings(); }, "ghost size-sm", "arrow");
  }

  function refresh() {
    tabs.update(value.mode);
    updateCaption();
    body.replaceChildren();
    body.setAttribute("aria-busy", "false");
    if (value.mode === codexSubscription) {
      const status = config.native?.status || "unchecked";
      const loading = detecting && (forceRefresh || status !== "ready_subscription");
      const line = $("div", "execution-model-status-line");
      const label = nativeStatus(status, loading ? "正在读取本机订阅模型…" : undefined);
      label.setAttribute("role", "status");
      const version = nativeVersion(config.native);
      if (version) {
        const signature = $("span", "meta codex-runtime-version", version);
        const details = [version, config.native.binary_path, config.native.catalog_source].filter(Boolean);
        signature.title = line.title = details.join("\n");
        signature.tabIndex = 0;
        signature.setAttribute("aria-description", details.slice(1).join(" · "));
        label.append(signature);
      }
      refreshButton.disabled = detecting || Boolean(config.native?.login_pending);
      line.append(label, refreshButton);
      body.append(line);
      body.setAttribute("aria-busy", String(loading));
      if (!loading && config.native?.version_notice) body.append($("p", "execution-model-status codex-warning", config.native.version_notice));
      if (!loading && status === "ready_subscription") {
        const models = enabledNativeModels(config.native, config.settings);
        body.append(...models.map((model) => row(codexSubscription, model)));
        if (config.native.catalog_error) body.append($("p", "execution-model-status codex-warning", config.native.catalog_error));
        else if (!models.length) body.append(settingsButton("管理订阅模型"));
      } else if (!loading) {
        const guide = status === "unauthenticated" ? "前往登录" : status === "cli_missing" ? "查看安装方式" : "打开模型设置";
        body.append($("p", "execution-model-status codex-warning", config.native?.message || "请检测本机 Codex 登录。"),
          actions(settingsButton(guide), button("切到 API 模型", () => {
            onSelect(apiGateway, value.profile || config.profiles.find(profileUsable)?.id || "");
            refresh();
            popup.close(true);
          }, "ghost size-sm")));
      }
    } else {
      for (const provider of config.models?.providers || []) {
        const models = provider.models.filter(profileUsable);
        if (!models.length) continue;
        const group = $("section", "model-family");
        group.append($("p", "model-family-title", provider.name), ...models.map((model) => row(apiGateway, model)));
        body.append(group);
      }
      if (!body.childElementCount) body.append($("p", "execution-model-status meta", "请配置一个 API 模型连接。"));
      body.append(settingsButton("管理模型连接"));
    }
    popup.reposition();
  }

  async function probe(force = false) {
    if (detecting || config.native?.login_pending) return;
    const restoreRefresh = refreshButton === document.activeElement;
    const restoreRow = body.contains(document.activeElement);
    detecting = true;
    forceRefresh = force;
    refresh();
    if (restoreRow && !force) focusFirst();
    try {
      config.native = await detect(force);
    } catch (error) {
      config.native = { status: "probe_failed", models: [], message: error.message || "无法读取本机订阅模型。" };
    } finally {
      const rowFocused = body.contains(document.activeElement);
      detecting = forceRefresh = false;
      if (value.mode === codexSubscription && popup.element.isConnected) {
        const current = defaultNativeModel(config.native, config.settings, value.codexModel);
        onSelect(codexSubscription, current?.id || value.codexModel, false);
        refresh();
        if (restoreRefresh && list.matches(":popover-open")) refreshButton.focus();
        else if (rowFocused) focusFirst();
      }
    }
  }

  function showMode(mode) {
    if (mode === value.mode) return;
    const id = mode === codexSubscription ? defaultNativeModel(config.native, config.settings, value.codexModel)?.id || "" :
      value.profile || config.profiles.find(profileUsable)?.id || "";
    onSelect(mode, id);
    refresh();
    if (mode === codexSubscription) void probe();
  }

  function focusFirst() {
    if (list.matches(":popover-open")) (list.querySelector('button[data-selected="true"]') ||
      list.querySelector('button[aria-pressed="true"]'))?.focus();
  }
  popup.trigger.addEventListener("click", focusFirst);
  list.addEventListener("toggle", ({ newState }) => {
    if (newState === "closed") retryWith = null;
    if (newState === "open" && value.mode === codexSubscription && config.ready) void probe();
  });
  popup.trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      popup.open();
      focusFirst();
    }
  });
  refresh();
  if (value.mode === codexSubscription && config.ready) void probe();
  return { ...popup, refresh, openForRetry(callback) {
    retryWith = callback;
    popup.open();
    focusFirst();
  } };
}

function inputField(label, type, value, limit = 128) {
  const wrap = $("label", "field");
  const input = $("input");
  input.type = type;
  input.value = value;
  input.maxLength = limit;
  input.spellcheck = false;
  wrap.append($("span", "field-label", label), input);
  return { wrap, input };
}

export function editProvider(catalog, { presetId = "", provider = null, focusKey = false, focusModels = false } = {}, onSaved) {
  if (!catalog?.presets?.length || document.querySelector(".provider-dialog[open]")) return;
  const dialog = $("dialog", "dialog provider-dialog");
  dialog.setAttribute("aria-labelledby", "provider-dialog-title");
  const form = $("form");
  form.autocomplete = "off";
  const title = $("h2", "", provider ? "编辑服务商" : "添加服务商");
  title.id = "provider-dialog-title";
  const dismiss = () => { dialog.close(); dialog.remove(); };
  const close = iconButton("关闭", dismiss, "close");
  const header = $("header");
  header.append(title, close);

  const name = inputField("服务商名称", "text", provider?.name || "", 80);
  name.input.required = true;
  const address = inputField("Base URL", "url", provider?.base_url || "", 512);
  address.input.classList.add("mono");
  const key = inputField("API Key", "password", "", 2048);
  key.input.autocomplete = "new-password";
  key.input.setAttribute("autocapitalize", "none");
  const protocol = popoverSelect("API 协议", catalog.protocols.map(({ id, label }) => [id, label]), {
    value: provider?.protocol || "openai-chat-completions",
  });
  const protocolField = $("div", "field");
  protocolField.append($("span", "field-label", "API 协议"), protocol.element);
  const fields = $("div", "provider-fields");
  fields.append(name.wrap, protocolField, address.wrap, key.wrap);

  const modelSection = $("section", "provider-models");
  const modelHeader = $("header", "provider-model-heading");
  const modelList = $("tbody");
  const manual = button("手动添加", () => appendModel().focus(), "", "plus");
  const discover = button("从远端拉取模型", () => fetchDirectory(true), "", "retry");
  modelHeader.append($("h2", "", "模型列表"), actions(discover, manual));
  const table = $("table", "provider-model-table");
  table.setAttribute("aria-label", "模型列表");
  const labels = $("tr");
  for (const label of ["模型 ID", "默认思考", "评测梯度", "能力", "操作"]) {
    const cell = $("th", "", label);
    cell.scope = "col";
    labels.append(cell);
  }
  const thead = $("thead");
  thead.append(labels);
  table.append(thead, modelList);
  const tableWrap = $("div", "provider-model-scroll");
  tableWrap.append(table);
  const emptyModels = $("p", "meta provider-model-empty", "从远端目录选择模型，或填写模型 ID。");
  const remoteList = $("div", "remote-model-list");
  remoteList.hidden = true;
  modelSection.append(modelHeader, tableWrap, emptyModels, remoteList);

  const feedback = $("span", "meta provider-feedback");
  feedback.setAttribute("role", "status");
  const probe = button("测试连接", () => fetchDirectory(false), "ghost");
  const test = actions(probe, feedback);
  test.classList.add("provider-test");
  const cancel = button("取消", dismiss, "ghost");
  const save = button("保存", null, "primary");
  save.type = "submit";
  const footer = $("footer");
  footer.append(cancel, save);
  const presetGrid = $("div", "provider-presets");
  form.append(header);
  if (!provider) form.append(presetGrid);
  form.append(fields, test, modelSection, footer);
  dialog.append(form);

  let activePreset = provider?.preset || "";
  let busy = false;
  const modelRows = new Map();
  let remote = new Map();
  const rowModels = () => [...modelRows.values()].map(({ read }) => read());
  const currentPreset = () => catalog.presets.find(({ id }) => id === activePreset);
  const currentBase = () => address.input.value.trim() || address.input.placeholder;
  const deepseek = catalog.presets.find(({ id }) => id === "deepseek");

  function defaultTier(model) {
    let preset = currentPreset();
    try {
      if (deepseek && new URL(currentBase()).hostname === new URL(deepseek.base_url).hostname) preset = deepseek;
    } catch {}
    return preset?.tiers?.[model.toLowerCase()] || "";
  }

  function setState(text = "", error = false) {
    feedback.textContent = text;
    feedback.classList.toggle("inline-error", error);
  }

  function updateFields() {
    const preset = currentPreset();
    address.input.placeholder = (preset?.protocol === protocol.value && preset.base_url)
      || (preset?.alternate?.protocol === protocol.value && preset.alternate.base_url)
      || catalog.protocols.find(({ id }) => id === protocol.value)?.base_url || "";
    const normalized = currentBase().replace(/\/+$/, "");
    const reusable = provider?.has_key && provider.protocol === protocol.value && provider.base_url === normalized;
    let local = false;
    try { local = loopback.has(new URL(normalized).hostname); } catch {}
    key.input.required = !reusable && !local;
    key.input.placeholder = reusable ? "已保存，留空沿用" : local ? "本地接口可留空" : "输入 API Key";
    for (const row of modelRows.values()) row.refresh();
  }

  function appendModel(record = {}) {
    const row = $("tr");
    const id = $("input", "mono");
    Object.assign(id, { value: record.model || "", required: true, maxLength: 128, spellcheck: false });
    id.setAttribute("aria-label", "模型 ID");
    id.placeholder = "输入模型 ID";
    const effort = popoverSelect("默认思考", [["", "厂商默认"]]);
    const tier = popoverSelect("评测梯度", Object.entries(evaluationTiers), { value: record.tier || "" });
    let tierSelected = record.tier != null;
    const summary = $("div", "provider-model-summary");
    const panel = $("div", "model-configuration");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "模型能力配置");
    const config = popover("配置", panel, { symbol: "settings", appearance: "ghost", align: "end" });
    const remove = iconButton("从表单移除模型", () => {
      config.close();
      modelRows.delete(row);
      row.remove();
      emptyModels.hidden = Boolean(modelRows.size);
    }, "close", "ghost");
    const chosen = $("input");
    chosen.type = "checkbox";
    const levelsLabel = $("label", "preference-toggle");
    levelsLabel.append(chosen, $("span", "", "手动指定思考档位"));
    const levelsInput = $("div", "provider-levels");
    const parallel = inputField("并行工具调用", "checkbox", "");
    const vision = inputField("多模态视觉", "checkbox", "");
    const window = inputField("上下文上限（Token）", "number", "");
    Object.assign(window.input, { min: 1, max: 100000000, step: 1 });
    for (const control of [parallel, vision]) control.wrap.classList.add("provider-cap-toggle");
    panel.append($("h3", "", "模型能力"), levelsLabel, levelsInput, parallel.wrap, vision.wrap, window.wrap);
    let modelIdentity;
    let override = record.reasoning_level_override ?? null;
    let desired = record.reasoning_effort || "";
    let capabilities = { ...record.capabilities };
    const original = () => record.model === id.value.trim() && record.protocol === protocol.value
      && record.base_url === currentBase().replace(/\/+$/, "");
    const mapped = () => {
      const model = id.value.trim();
      const protocolLevels = catalog.protocols.find(({ id }) => id === protocol.value)?.reasoning_efforts || [];
      const automatic = remote.get(model)?.reasoning_efforts
        || (original() && record.automatic_reasoning_efforts)
        || currentPreset()?.mappings?.[protocol.value]?.[model] || [""];
      return { automatic, choices: [...new Set([...protocolLevels, ...automatic])].filter(Boolean) };
    };

    const visionDefault = () => original() ? record.vision_supported :
      Boolean(currentPreset()?.vision?.[protocol.value]?.includes(id.value.trim()));
    function updateSummary() {
      const effective = override || mapped().automatic;
      effort.setOptions([["", "厂商默认"], ...effective.filter(Boolean).map((level) => [level, effortNames[level] || level])], desired);
      desired = effort.value;
      summary.replaceChildren(capabilityTags({
        reasoning_efforts: effective,
        vision_supported: capabilities.vision ?? visionDefault(),
        parallel_tools_supported: capabilities.parallel_tool_calls ?? (original() && record.parallel_tools_supported),
      }));
    }
    function refresh() {
      const nextIdentity = [protocol.value, currentBase(), id.value.trim()].join("|");
      if (!tierSelected) tier.value = defaultTier(id.value.trim());
      if (modelIdentity && modelIdentity !== nextIdentity) {
        override = original() ? record.reasoning_level_override ?? null : null;
        capabilities = original() ? { ...record.capabilities } : {};
        desired = original() ? record.reasoning_effort || "" : "";
      }
      modelIdentity = nextIdentity;
      chosen.checked = override !== null;
      levelsInput.hidden = override === null;
      levelsInput.replaceChildren(...mapped().choices.map((level) => {
        const label = $("label", "preference-toggle");
        const choice = $("input");
        Object.assign(choice, { type: "checkbox", value: level, checked: override?.includes(level) || false });
        label.append(choice, $("span", "", effortNames[level] || level));
        return label;
      }));
      parallel.input.checked = capabilities.parallel_tool_calls ?? (original() && record.parallel_tools_supported) ?? false;
      vision.input.checked = capabilities.vision ?? visionDefault() ?? false;
      window.input.value = capabilities.context_window || "";
      updateSummary();
    }
    function read() {
      const model = id.value.trim();
      const levels = remote.get(model)?.discovered_reasoning_levels;
      return { model, tier: tier.value, reasoning_effort: effort.value, reasoning_level_override: override, capabilities,
        ...(record.id ? { id: record.id } : {}),
        ...(levels !== undefined ? { discovered_reasoning_levels: levels } : {}) };
    }
    effort.element.addEventListener("change", () => { desired = effort.value; });
    tier.element.addEventListener("change", () => { tierSelected = true; });
    id.addEventListener("input", refresh);
    chosen.addEventListener("change", () => {
      override = chosen.checked ? mapped().automatic.filter(Boolean) : null;
      refresh();
    });
    levelsInput.addEventListener("change", () => {
      override = [...levelsInput.querySelectorAll("input:checked")].map((item) => item.value);
      updateSummary();
    });
    for (const [control, capability] of [[vision, "vision"], [parallel, "parallel_tool_calls"]]) {
      control.input.addEventListener("change", () => { capabilities[capability] = control.input.checked; updateSummary(); });
    }
    window.input.addEventListener("input", () => {
      if (window.input.value) capabilities.context_window = Number(window.input.value);
      else delete capabilities.context_window;
    });
    for (const content of [id, effort.element, tier.element, summary, actions(config.element, remove)]) {
      const cell = $("td");
      cell.append(content);
      row.append(cell);
    }
    modelRows.set(row, { read, refresh });
    modelList.append(row);
    emptyModels.hidden = true;
    refresh();
    return id;
  }

  function clearRemote() {
    remote.clear();
    remoteList.hidden = true;
    remoteList.replaceChildren();
    setState();
  }

  function selectPreset(preset, focus = true) {
    activePreset = preset.id;
    for (const item of presetGrid.querySelectorAll("[data-preset]")) {
      item.setAttribute("aria-pressed", String(item.dataset.preset === activePreset));
    }
    clearRemote();
    name.input.value = preset.name;
    address.input.value = preset.base_url;
    protocol.value = preset.protocol;
    key.input.value = "";
    modelRows.clear();
    modelList.replaceChildren();
    emptyModels.hidden = Boolean(preset.models.length);
    updateFields();
    for (const model of preset.models) appendModel({ model, tier: preset.tiers?.[model] });
    if (focus) (preset.name ? key.input : name.input).focus();
  }

  if (!provider) {
    const groups = new Map();
    for (const preset of catalog.presets) {
      if (!groups.has(preset.group)) {
        const section = $("section");
        const choices = $("div", "preset-choices");
        section.append($("h3", "field-label", preset.group), choices);
        groups.set(preset.group, choices);
        presetGrid.append(section);
      }
      const item = button(preset.label, () => { if (preset.id !== activePreset) selectPreset(preset); }, "ghost");
      item.dataset.preset = preset.id;
      item.setAttribute("aria-pressed", "false");
      groups.get(preset.group).append(item);
    }
  }

  protocol.element.addEventListener("change", () => {
    const preset = currentPreset();
    if (!provider && preset) {
      address.input.value = protocol.value === preset.alternate?.protocol ? preset.alternate.base_url
        : protocol.value === preset.protocol ? preset.base_url : "";
    }
    clearRemote();
    updateFields();
  });
  address.input.addEventListener("input", () => { clearRemote(); updateFields(); });
  key.input.addEventListener("input", () => setState());

  function connection() {
    return { name: name.input.value.trim(), protocol: protocol.value, base_url: currentBase(),
      api_key: key.input.value.trim(), preset: activePreset, ...(provider?.id ? { provider_id: provider.id } : {}) };
  }

  function showRemote(models) {
    const filter = inputField("搜索远端模型", "search", "");
    const results = $("div", "remote-model-options");
    function draw() {
      const term = filter.input.value.trim().toLowerCase();
      const configured = new Set(rowModels().map(({ model }) => model));
      const matches = models.filter(({ id, name }) => (id + " " + name).toLowerCase().includes(term));
      results.replaceChildren(...matches.slice(0, 32).map((item) => {
        const option = button(item.id, () => { appendModel({ model: item.id }); draw(); }, "ghost");
        option.disabled = configured.has(item.id);
        return option;
      }));
      if (!matches.length) results.append($("span", "meta", "没有匹配的远端模型"));
    }
    filter.input.addEventListener("input", draw);
    remoteList.replaceChildren(filter.wrap, results);
    remoteList.hidden = false;
    draw();
    filter.input.focus();
  }

  async function waitFor(task, note) {
    if (busy) return;
    setState(note);
    busy = true;
    for (const control of [probe, discover, manual, save, cancel, close]) control.disabled = true;
    fields.inert = presetGrid.inert = tableWrap.inert = true;
    try {
      await task();
    } catch (error) {
      setState(error.message, true);
    } finally {
      busy = false;
      for (const control of [probe, discover, manual, save, cancel, close]) control.disabled = false;
      fields.inert = presetGrid.inert = tableWrap.inert = false;
    }
  }

  function fetchDirectory(show) {
    if (!address.input.reportValidity() || !key.input.reportValidity()) return;
    const start = performance.now();
    waitFor(async () => {
      const { models, truncated } = await post(profilePath + "/discover", connection());
      if (!show) {
        setState("连接成功 · " + Math.round(performance.now() - start) + " ms");
        return;
      }
      remote = new Map(models.map((model) => [model.id, model]));
      updateFields();
      showRemote(models);
      setState("已拉取 " + models.length + " 个模型" + (truncated ? "（目录已截取）" : ""));
    }, show ? "正在拉取模型…" : "正在测试连接…");
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    waitFor(async () => {
      const result = await post(profilePath, { ...connection(), models: rowModels() });
      dismiss();
      onSaved(result.provider);
    }, "正在保存…");
  });
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  const preset = catalog.presets.find(({ id }) => id === presetId);
  if (provider) {
    provider.models.forEach(appendModel);
    emptyModels.hidden = Boolean(provider.models.length);
    updateFields();
    (focusModels ? discover : focusKey ? key.input : name.input).focus();
  } else if (preset) selectPreset(preset);
  else {
    updateFields();
    presetGrid.querySelector("button")?.focus();
  }
}

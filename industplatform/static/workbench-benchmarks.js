import { $, actions, button, column, empty, external, field, gridTable, heading, href, popoverSelect,
  post, request, stateBadge } from "./workbench-ui.js";
import { tierNames as tiers } from "./workbench-models.js";

const api = "/api/benchmarks";
const verifierNames = { "cnc-fai": "首件尺寸复核", "qif-intake": "QIF 来料结果交接" };
const fieldsOf = (form) => Object.fromEntries(new FormData(form));
const layoutsKey = ({ id, version }) => id + "/" + version;

function selectControl(label, options, value) {
  const control = popoverSelect(label, options, { value });
  const wrap = $("div", "field");
  wrap.append($("span", "field-label", label), control.element);
  return { ...control, wrap, control };
}

function check(name, label, value = "") {
  const row = $("label", "preference-toggle");
  const input = $("input");
  Object.assign(input, { type: "checkbox", name, value });
  row.append(input, $("span", "", label));
  return row;
}

function submit(form, label, perform) {
  const send = button(label, null, "primary");
  send.type = "submit";
  const note = $("p", "meta");
  note.setAttribute("role", "status");
  form.append(actions(send, note));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (send.disabled) return;
    send.disabled = true;
    note.classList.remove("inline-error");
    note.textContent = "正在提交…";
    try {
      note.textContent = await perform(new FormData(form)) || "";
    } catch (error) {
      note.classList.add("inline-error");
      note.textContent = error.message;
    } finally { send.disabled = false; }
  });
  return send;
}

function registerCase(ctx, catalog) {
  const form = $("form", "bench-form");
  const verifier = selectControl("验证器", Object.keys(catalog.inputs).map((id) => [id, verifierNames[id] || id]));
  const inputs = $("div", "business-fields");
  const details = $("div", "business-fields");
  details.append(verifier.wrap, ...[
    ["title", "案例名称", 150], ["part_id", "零件标识", 80], ["revision", "版本", 80], ["report_id", "记录编号", 80],
    ["scenario", "工业场景", 1200], ["derivation", "来源与加工说明", 900], ["reference_ids", "已核验文献 ID", 780],
  ].map(([id, label, limit]) => field(id, label, { limit, required: true,
    as: limit > 500 ? "textarea" : "input", wide: limit > 500 })));
  const update = () => inputs.replaceChildren(...catalog.inputs[verifier.control.value].map((name) =>
    field(name, name, { as: "textarea", required: true, limit: 56 * 1024, wide: true })));
  verifier.element.addEventListener("change", update);
  update();
  form.append(details, inputs, check("share_allowed", "许可允许将该案例发送给评测模型"));
  submit(form, "保存案例", async () => {
    const data = fieldsOf(form);
    const files = {};
    for (const name of catalog.inputs[verifier.control.value]) {
      files[name] = data[name];
      delete data[name];
    }
    const saved = await post(api + "/cases", { ...data, verifier: verifier.control.value, domain: ctx.data.domain.name, inputs: files,
      share_allowed: Boolean(form.elements.share_allowed.checked), reference_ids: data.reference_ids.split(/[,，、\s]+/).filter(Boolean) });
    ctx.viewState.caseID = saved.id;
    ctx.navigate(ctx.route("tasks/build"));
  });
  return form;
}

function editTemplate(ctx, catalog) {
  const form = $("form", "bench-form");
  const options = catalog.templates.filter((item) => item.default || item.domain === ctx.data.domain.name);
  const source = selectControl("模板", options.map((item) => [layoutsKey(item), item.title + " · v" + item.version]));
  const data = $("div", "business-fields");
  const names = [["title", "模板名称", 120], ["role", "岗位", 140], ["recipient", "下游接收方", 140],
    ["business_value", "工业价值", 500], ["fields", "输出字段 JSON", 7500]];
  const selected = () => options.find((item) => layoutsKey(item) === source.control.value);
  function update() {
    const item = selected();
    data.replaceChildren(...names.map(([id, label, limit]) => field(id, label, {
      value: id === "fields" ? JSON.stringify(item.fields, null, 2) : item[id],
      limit, required: true, as: limit > 140 ? "textarea" : "input", wide: limit > 140,
    })));
  }
  source.element.addEventListener("change", update);
  update();
  form.append(source.wrap, data);
  submit(form, "保存模板版本", async () => {
    const fields = fieldsOf(form);
    const prior = selected();
    try { fields.fields = JSON.parse(fields.fields); }
    catch { throw new Error("请填写有效的输出字段 JSON"); }
    const saved = await post(api + "/templates", { ...fields, domain: ctx.data.domain.name, verifier: prior.verifier,
      ...(prior.default ? {} : { id: prior.id, version: prior.version }) });
    if (!options.some((option) => option.id === saved.id)) options.push(saved);
    else options[options.findIndex((option) => option.id === saved.id)] = saved;
    source.control.setOptions(options.map((item) => [layoutsKey(item), item.title + " · v" + item.version]), layoutsKey(saved));
    return "已保存 v" + saved.version;
  });
  return form;
}

function configureBenchmark(ctx, catalog) {
  const cases = catalog.cases.filter((item) => item.domain === ctx.data.domain.name);
  if (!cases.length) return empty("task", "先登记工业案例", "构题与模型评测共用带核验依据的实际样本。",
    actions(href("登记案例", ctx.route("tasks/case"), "button primary")));
  const form = $("form", "bench-form");
  const casePicker = selectControl("工业案例", cases.map(({ id, title }) => [id, title]), ctx.viewState.caseID);
  const template = selectControl("输出模板", []);
  const variant = selectControl("测试工况", Object.entries(catalog.variants), "as_recorded");
  const method = selectControl("执行方式", [["offline", "离线验收"], ["live", "模型 API 盲测"]],
    ctx.section === "evaluate" ? "live" : "offline");
  const panelMode = selectControl("盲测类型", [["calibration", "梯度校准"], ["probe", "少量试测"]], "calibration");
  const repetitions = selectControl("每个模型重复次数", [1, 2, 3].map((i) => [i, String(i)]));
  const metadata = $("div", "business-fields");
  const panel = $("section", "benchmark-panel");
  const models = $("div", "benchmark-models");
  const authorization = check("confirm_case_permission", "已核对原始样本许可并允许本次模型外发");
  const sendConfirmation = check("confirm_model_send", "确认向所选模型发送题面与输入");
  const liveControls = $("div", "business-fields");
  liveControls.append(panelMode.wrap, repetitions.wrap);
  models.append(...catalog.panel.map((item) => check("profile_ids",
    [tiers[item.tier], item.name, item.model, item.base_url].join(" · "), item.id)));
  if (!models.children.length) models.append(href("前往模型设置", ctx.route("settings"), "button ghost"));
  panel.append(liveControls, models, authorization, sendConfirmation);
  const basics = $("div", "business-fields");
  basics.append(casePicker.wrap, template.wrap, variant.wrap, method.wrap,
    field("assignment", "任务补充要求", { limit: 800, as: "textarea", wide: true }));
  form.append(basics, metadata, panel);

  const currentCase = () => cases.find((item) => item.id === casePicker.control.value);
  const currentLayout = () => catalog.templates.find((item) => layoutsKey(item) === template.control.value);
  function paintMetadata() {
    metadata.replaceChildren(...Object.entries(currentLayout()?.fields || {}).map(([key, label]) =>
      field("meta-" + key, label, { limit: 500 })));
  }

  function paintMode() {
    const item = currentCase();
    const eligible = item.input_run_id && item.references.some((ref) => ref.permission === "stated");
    method.control.setOptions([["offline", "离线验收"], ...(item.share_allowed || eligible ? [["live", "模型 API 盲测"]] : [])]);
    panel.hidden = method.control.value !== "live";
    authorization.hidden = Boolean(item.share_allowed);
    authorization.querySelector("input").checked = false;
    sendConfirmation.querySelector("input").checked = false;
  }

  function paintCase() {
    const item = currentCase();
    ctx.viewState.caseID = item.id;
    const choices = catalog.templates.filter((row) => row.verifier === item.verifier &&
      (row.domain === item.domain || (row.default && item.input_run_id)));
    template.control.setOptions(choices.map((row) => [layoutsKey(row), row.title + " · v" + row.version]));
    paintMetadata();
    paintMode();
    send.disabled = !choices.length;
  }

  const send = submit(form, "开始运行", async (values) => {
    const layout = currentLayout();
    if (!layout) throw new Error("请选择同领域的输出模板");
    const cohort = method.control.value;
    if (cohort === "live" && (!values.has("confirm_model_send") ||
        !currentCase().share_allowed && !values.has("confirm_case_permission"))) {
      throw new Error("请先核对案例许可并确认向模型发送题面与输入");
    }
    const data = {
      case_id: currentCase().id, template_id: layout.id, template_version: layout.version,
      variant: variant.control.value, assignment: values.get("assignment").trim(), cohort,
      metadata: Object.fromEntries([...values].filter(([key]) => key.startsWith("meta-"))
        .map(([key, value]) => [key.slice(5), value])),
    };
    if (cohort === "live") Object.assign(data, {
      profile_ids: values.getAll("profile_ids"), panel_mode: panelMode.control.value, repetitions: Number(repetitions.control.value),
      confirm_model_send: values.has("confirm_model_send"), confirm_case_permission: values.has("confirm_case_permission"),
    });
    const run = await post(api, data);
    ctx.navigate(ctx.route((ctx.section === "evaluate" ? "export" : "tasks") + "/run/" + run.id));
  });
  casePicker.element.addEventListener("change", paintCase);
  template.element.addEventListener("change", paintMetadata);
  method.element.addEventListener("change", paintMode);
  paintCase();
  return form;
}

export function benchmarkFormView(ctx) {
  const titles = { case: "新建工业难题", template: "题包模板", build: "生成离线题包", evaluate: "配置模型评测" };
  const controls = actions(...[["tasks/case", "登记案例"], ["tasks/template", "题包模板"], ["tasks/build", "离线构题"]]
    .filter(([route]) => route !== "tasks/" + ctx.section).map(([route, title]) => href(title, ctx.route(route), "button ghost")));
  const panel = $("div");
  const status = $("p", "muted", "正在读取案例与模板…");
  panel.append(status);
  request(api).then((catalog) => {
    const renderer = { case: registerCase, template: editTemplate }[ctx.section] || configureBenchmark;
    panel.replaceChildren(renderer(ctx, catalog));
  }).catch((error) => { status.textContent = error.message; });
  return column(heading(titles[ctx.section], ctx.data.domain.name, controls), panel);
}

function resultSection(title, children) {
  const section = $("section", "run-detail-section");
  section.append($("h2", "", title), ...children);
  return section;
}

function runTask(task) {
  const inputs = Object.entries(task.inputs || {}).map(([name, content]) => {
    const preview = $("details", "fold");
    preview.append($("summary", "", name), $("pre", "mono file-preview",
      typeof content === "string" ? content : JSON.stringify(content, null, 2)));
    return preview;
  });
  return resultSection(task.title || task.id, [
    $("p", "run-description", task.description || ""),
    ...(inputs.length ? [$("h3", "", "交付输入"), ...inputs] : []),
  ]);
}

export function runDetailView(ctx) {
  const panel = $("div", "run-detail");
  panel.append($("p", "muted", "正在读取运行记录…"));
  let previous;
  async function refresh() {
    try {
      const run = await request("/api/runs/" + ctx.run);
      if (!panel.isConnected) return;
      if (run.domain_id !== (ctx.data.domain?.id ?? null)) {
        const page = ctx.page === "export" ? "export" : "tasks";
        ctx.navigate(ctx.route(page + "/run/" + run.id, run.domain_id), { replace: true });
        return;
      }
      const result = run.result || {};
      if (JSON.stringify(run) !== previous) {
        previous = JSON.stringify(run);
        const controls = actions(stateBadge(run.status));
        if (run.benchmark_ready) controls.append(href("下载题包", "/api/runs/" + run.id + "/benchmark", "button primary", "download"));
        if (run.conversation_id) controls.append(href("查看对话", ctx.route("scope/" + run.conversation_id), "button"));
        const references = (result.assets || []).map((item) =>
          [external(item.title || item.id, item.source), item.version || "", stateBadge(item.status)]);
        const evaluations = (result.evaluations || []).map((item) =>
          [item.task_id, tiers[item.tier] || item.tier, item.model || "", stateBadge(item.status === "scored" ?
            item.passed ? "passed" : "failed" : item.status), item.reason || ""]);
        const checks = (result.tool?.checks || []).map((check) => [
          check.label || check.name || check.id || "", stateBadge(check.passed ? "passed" : "failed"), check.detail || "",
        ]);
        panel.replaceChildren(heading(run.prompt || "运行记录", run.id, controls),
          ...(run.error ? [$("p", "inline-error", run.error)] : []),
          resultSection("执行进度", [gridTable(["环节", "状态", "记录"],
            (run.steps || []).map((step) => [step.label, stateBadge(step.status), step.summary]))]),
          ...(references.length ? [resultSection("来源与证据", [gridTable(["资料", "版本", "状态"], references)])] : []),
          ...(result.tasks || []).map(runTask),
          ...(checks.length ? [resultSection("工具验收", [gridTable(["检查", "状态", "记录"], checks)])] : []),
          ...(evaluations.length ? [resultSection("评测结果", [gridTable(["题目", "档位", "模型", "判分", "记录"], evaluations)])] : []),
        );
      }
      if (["queued", "running", "pausing", "cancelling"].includes(run.status)) setTimeout(refresh, 2200);
    } catch (error) {
      if (panel.isConnected) panel.replaceChildren(heading("运行记录"), $("p", "inline-error", error.message),
        button("重试", refresh, "ghost"));
    }
  }
  refresh();
  return panel;
}

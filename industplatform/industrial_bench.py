"""Source-linked benchmark cases, versioned formats, and layered verifiers."""

import csv
import hashlib
import io
import json
import re
import uuid
import xml.etree.ElementTree as ET
from contextlib import closing
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from . import collection, metrology


ROUTE = "industrial-benchmark"
VERSION = "1.1"
INPUTS = {
    "cnc-fai": ("requirements.csv", "measurements.csv"),
    "qif-intake": ("inspection.qif",),
}
VARIANTS = {
    "as_recorded": "原记录",
    "missing": "缺测",
    "wrong_revision": "错版",
    "duplicate": "重复",
}
DEFAULT_FIELDS = {
    "role": "岗位", "recipient": "下游接收方", "business_value": "工程价值",
    "frequency": "工作频率", "expert_time": "专家耗时", "error_cost": "错判影响",
}
DEFAULTS = {
    "cnc-fai": {"title": "首件尺寸复核", "domain": "机械与离散制造",
                "role": "质量检验员", "recipient": "质量放行岗",
                "business_value": "把待复核、超差和可提交放行复核的记录分流"},
    "qif-intake": {"title": "QIF 来料结果交接", "domain": "机械与离散制造",
                   "role": "计量数据工程师", "recipient": "来料质量系统",
                   "business_value": "在导入前识别不完整、重复或版本不符的计量结果"},
}
ID = re.compile(r"[a-z][a-z0-9_-]{0,47}")
REF = re.compile(r"[0-9a-f]{64}")
SUPPORTED_QIF = {"2.0", "2.0.0", "2.1", "2.1.0", "3.0", "3.0.0"}
DATA_LIMIT = 56 * 1024


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS industrial_cases (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS benchmark_templates (
        id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id, version)
    )""")


def templates(store):
    defaults = [{"id": key, "version": 1, "verifier": key, "default": True,
                 "fields": DEFAULT_FIELDS, **DEFAULTS[key]} for key in DEFAULTS]
    with closing(store.connect()) as db:
        rows = db.execute("""SELECT body FROM benchmark_templates WHERE (id, version) IN
            (SELECT id, MAX(version) FROM benchmark_templates GROUP BY id) ORDER BY id""").fetchall()
    return defaults + [json.loads(row[0]) for row in rows]


def save_template(store, data):
    core = {key: data[key] for key in ("title", "domain", "role", "recipient", "business_value") if key in data}
    core = collection.fields(core, dict(title=120, domain=100, role=140, recipient=140, business_value=500),
                             {"title", "domain", "role", "recipient", "business_value"}, "模板")
    verifier = data.get("verifier")
    labels = data.get("fields", DEFAULT_FIELDS)
    if not isinstance(verifier, str) or verifier not in INPUTS:
        raise ValueError("请选择已有验证器")
    if (not isinstance(labels, dict) or len(labels) > 20
            or any(not ID.fullmatch(key) or not isinstance(label, str) or not label.strip() or len(label) > 80
                   for key, label in labels.items())):
        raise ValueError("模板字段须使用短英文键和非空显示名称")
    identity = data.get("id") or "tpl-" + uuid.uuid4().hex
    if not isinstance(identity, str) or not identity.startswith("tpl-") or not ID.fullmatch(identity):
        raise ValueError("请保存为新的自定义模板")
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT version, body FROM benchmark_templates WHERE id=? ORDER BY version DESC LIMIT 1",
                         (identity,)).fetchone()
        if data.get("id") and (not row or data.get("version") != row[0]):
            raise ValueError("模板已更新，请载入最新版本后保存")
        value = {"id": identity, "version": row[0] + 1 if row else 1, "verifier": verifier,
                 "fields": labels, **core}
        db.execute("INSERT INTO benchmark_templates VALUES (?, ?, ?)",
                   (identity, value["version"], json.dumps(value, ensure_ascii=False)))
    return value


def template(store, identity, version):
    if not isinstance(identity, str) or type(version) is not int or version < 1:
        raise ValueError("请选择模板版本")
    if identity in DEFAULTS and version == 1:
        return next(row for row in templates(store) if row["id"] == identity)
    with closing(store.connect()) as db:
        row = db.execute("SELECT body FROM benchmark_templates WHERE id=? AND version=?",
                         (identity, version)).fetchone()
    if not row:
        raise ValueError("未找到模板版本")
    return json.loads(row[0])


def matches_template(value, layout):
    return value["verifier"] == layout["verifier"] and (
        value["domain"] == layout["domain"] or (layout.get("default") and value.get("input_run_id"))
    )


def prepare_case(store, data):
    names = dict(title=150, domain=100, scenario=1200, part_id=80, revision=80, report_id=80, derivation=900)
    values = collection.fields({key: data[key] for key in names if key in data}, names, set(names), "案例")
    verifier, inputs, ids = (data.get(key) for key in ("verifier", "inputs", "reference_ids"))
    if not isinstance(verifier, str) or verifier not in INPUTS:
        raise ValueError("请选择已有验证器")
    if (not isinstance(inputs, dict) or inputs.keys() != set(INPUTS[verifier])
            or any(not isinstance(value, str) or not value.strip() for value in inputs.values())
            or sum(len(value.encode()) for value in inputs.values()) > DATA_LIMIT):
        raise ValueError("输入文件须符合验证器约定，合计不得超过 56 KiB")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 12
            or any(not isinstance(item, str) or not REF.fullmatch(item) for item in ids)):
        raise ValueError("请选择 1 至 12 条已核验 Reference")
    refs = [collection.reference(store, item) for item in dict.fromkeys(ids)]
    if any(not record or not record.get("verification") or record["domain"] != values["domain"] for record in refs):
        raise ValueError("Reference 须已核验并归属同一领域")
    if not any(record["verification"].get("level") in {"sample", "acceptance"} for record in refs):
        raise ValueError("至少需要一条已核验的实际样本或验收记录")
    share = data.get("share_allowed", False)
    if type(share) is not bool:
        raise ValueError("模型外发授权的值无效")
    if share and not any(record["verification"].get("permission") == "stated" for record in refs):
        raise ValueError("允许模型外发时，须关联已登记许可依据的 Reference")
    references = [{key: record[key] for key in ("id", "title", "url", "locator", "publisher", "version")}
                  | {"sha256": record["verification"]["sha256"], "level": record["verification"]["level"],
                     "permission": record["verification"]["permission"],
                     "relation_type": record.get("relation", {}).get("type", ""),
                     "license_quote": record["verification"].get("license_quote", ""),
                     "license_source": record["verification"].get("license_source", "")}
                  for record in refs]
    value = {**values, "verifier": verifier, "inputs": inputs, "references": references, "share_allowed": share}
    value["id"] = "case-" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    # Intake failures are valid wrong cases. CNC still needs a readable specification and at least one finding.
    oracle(value, DEFAULTS[verifier]["recipient"])
    return value


def save_case(db, value, input_run_id=None):
    body = {**case_summary(value), "input_run_id": input_run_id} if input_run_id else value
    db.execute("INSERT OR IGNORE INTO industrial_cases VALUES (?, ?)",
               (value["id"], json.dumps(body, ensure_ascii=False)))


def register(store, data):
    value = prepare_case(store, data)
    with closing(store.connect()) as db, db:
        save_case(db, value)
    return case_summary(value)


def case_summary(value):
    return {key: item for key, item in value.items() if key != "inputs"}


def cases(store):
    with closing(store.connect()) as db:
        return [case_summary(json.loads(row[0])) for row in db.execute(
            "SELECT body FROM industrial_cases ORDER BY rowid DESC LIMIT 100"
        )]


def case(store, identity):
    if not isinstance(identity, str) or not ID.fullmatch(identity):
        raise ValueError("请选择工业案例")
    with closing(store.connect()) as db:
        row = db.execute("SELECT body FROM industrial_cases WHERE id=?", (identity,)).fetchone()
    if not row:
        raise ValueError("未找到工业案例")
    value = json.loads(row[0])
    if value.get("input_run_id"):
        package = store.get(value["input_run_id"])
        tasks = (package or {}).get("result", {}).get("tasks", [])
        if not tasks or (package or {}).get("benchmark", {}).get("case_id") != identity:
            raise ValueError("找不到该案例的已验收题包输入")
        value["inputs"] = {name: tasks[0]["inputs"][name] for name in INPUTS[value["verifier"]]}
    return value


def variant(value, mode):
    if not isinstance(mode, str) or mode not in VARIANTS:
        raise ValueError("请选择测试工况")
    if mode == "as_recorded":
        return value
    value = deepcopy(value)
    files = value["inputs"]
    if value["verifier"] == "cnc-fai":
        rows = list(csv.reader(io.StringIO(files["measurements.csv"])))
        while len(rows) > 1 and not rows[-1]:
            rows.pop()
        if len(rows) < 2 or len(rows[1]) != len(metrology.MEASUREMENT_COLUMNS):
            raise ValueError("该案例无法生成此反例")
        if mode == "missing":
            rows.pop(1)
        elif mode == "wrong_revision":
            rows[1][1] = "benchmark-mismatch" if value["revision"] != "benchmark-mismatch" else "benchmark-mismatch-2"
        else:
            rows.append(rows[1][:])
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(rows)
        files["measurements.csv"] = stream.getvalue()
    else:
        try:
            root = ET.fromstring(files["inspection.qif"])
            for node in root.iter():
                node.tag = node.tag.rsplit("}", 1)[-1]
            container = next(node for node in root.iter("CharacteristicActuals") if len(node))
        except (ET.ParseError, StopIteration):
            raise ValueError("该案例无法生成此反例") from None
        if mode == "missing":
            node = container[0].find("Value")
            if node is not None:
                container[0].remove(node)
        elif mode == "wrong_revision":
            ET.SubElement(root, "PartVersion").text = ("benchmark-mismatch" if value["revision"] != "benchmark-mismatch"
                                                    else "benchmark-mismatch-2")
        else:
            container.append(deepcopy(container[0]))
        files["inspection.qif"] = ET.tostring(root, encoding="unicode")
    value["variant"] = mode
    return value


def qif_records(text, revision):
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("QIF 实例不接受 DTD 或实体声明")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], ["invalid_xml"]
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag != "QIFDocument":
        return [], ["wrong_root"]
    errors = set()
    version = root.get("versionQIF", "")
    if version not in SUPPORTED_QIF:
        errors.add("unsupported_qif_version")
    revisions = {node.text.strip() for node in [*root.iter("PartVersion"), *root.findall(".//PrintedDrawing/Version")]
                 if node.text and node.text.strip()}
    if not revisions:
        errors.add("missing_revision")
    elif revisions != {revision}:
        errors.add("revision_mismatch")
    records = []
    seen = set()
    for container in root.iter("CharacteristicActuals"):
        for node in container:
            if not node.tag.endswith("CharacteristicActual"):
                continue
            identity = node.get("id", "")
            if not metrology.IDENTIFIER.fullmatch(identity):
                errors.add("missing_actual_id")
                continue
            if identity in seen:
                errors.add("duplicate_actual_id")
                continue
            seen.add(identity)
            number = node.findtext("Value")
            try:
                measured = Decimal(number or "")
                if not measured.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                errors.add("invalid_measurement")
                continue
            source_status = node.findtext("Status/CharacteristicStatusEnum")
            if not source_status:
                errors.add("missing_reported_status")
                continue
            records.append({"id": identity, "value": number.strip(), "reported_status": source_status.strip()})
    if not seen:
        errors.add("no_actuals")
    return records, sorted(errors)


def oracle(value, recipient):
    files = value["inputs"]
    handoff = {"part_id": value["part_id"], "revision": value["revision"],
               "report_id": value["report_id"], "recipient": recipient}
    if value["verifier"] == "cnc-fai":
        requirements, measurements = files["requirements.csv"], files["measurements.csv"]
        verdict = metrology.inspect_csv(requirements, measurements)
        if "input_error" in verdict or not verdict["findings"]:
            raise ValueError(verdict.get("input_error") or "CSV 无可判定特性")
        rows = csv.DictReader(io.StringIO(requirements.removeprefix("\ufeff")))
        same_revision = {row["revision"].strip() for row in rows} == {value["revision"]}
        shipment = "hold" if not same_revision else {"hold": "hold", "fail": "segregate", "pass": "review_release"}[verdict["status"]]
        return {**verdict, "handoff": {**handoff, "action": shipment}}
    records, issues = qif_records(files["inspection.qif"], value["revision"])
    return {"records": records, "issues": issues, "handoff": {**handoff, "action": "hold" if issues else "import_candidate"}}


CNC_CONTRACT = metrology.ANSWER_CONTRACT.replace(
    "顶层字段为 status 和 findings 数组", "顶层字段为 status、findings 数组和 handoff",
) + (
    " characteristic_id 必须使用 CSV 原文对应的 JSON 字符串，纯数字 ID 也用字符串并保留前导零。"
    " 对象只包含规定的字段。另输出 handoff 对象，包含 job.json 中的 part_id、revision、report_id、recipient 及 action。"
    "规格版本与 job.json 不一致或整件为 hold 时 action 为 hold，超差为 segregate，合格为 review_release。"
)
QIF_CONTRACT = (
    "交付 JSON 对象，顶层为 records、issues、handoff。读取定义为抽样结果文件的 inspection.qif，"
    "检查 QIFDocument 根及 versionQIF（2.0、2.1、3.0 或对应三段版本号）。"
    "PartVersion 和 PrintedDrawing/Version 中至少一处有版本，全部出现的版本须与 job.json 的 revision 一致。"
    "从 CharacteristicActuals 中提取以 CharacteristicActual 结尾的直接子项，ID 用原样的 JSON 字符串。"
    "ID 须由字母数字开头，"
    "其余仅字母数字及点、下划线、连字符，总长最多 64。按序保留每个 ID 的首项。"
    "每条记录含 id、可解析的十进制有限数 Value 对应 value，以及 Status/CharacteristicStatusEnum 对应 reported_status。"
    "跳过坏记录，issues 去重。错误代码依次使用 invalid_xml、wrong_root、unsupported_qif_version、missing_revision、"
    "revision_mismatch、missing_actual_id、duplicate_actual_id、invalid_measurement、missing_reported_status、no_actuals（没有有效 ID）。"
    "handoff 包含 job.json 中 part_id、revision、report_id、recipient；无 issues 时 action 为 import_candidate，否则为 hold。"
)
RULE_SETS = {
    "cnc-fai": ("提交契约与特性表结构", "全部特性、单位、公差及整件状态", "零件、版本、报告、接收人及处置"),
    "qif-intake": ("交付 JSON 结构和特性 ID 唯一性", "报告字段映射、数值和证据缺口", "来料版本、接收方与导入状态"),
}


def verifier_layers(value):
    sample = [item["id"] for item in value["references"] if item["level"] in {"sample", "acceptance"}]
    handoff = [item["id"] for item in value["references"] if item.get("relation_type") in {
        "supplier_requirement", "actual_delivery",
    }]
    sources = (sample, sample, handoff)
    names = RULE_SETS[value["verifier"]]
    return [{"id": f"{value['verifier']}@{VERSION}:{index}", "name": name, "references": refs,
             "basis": "样本字段与项目判定规则" if index < 3 else "项目交接契约及已登记的交付依据" if refs else "项目交接契约"}
            for index, (name, refs) in enumerate(zip(names, sources), 1)]


def make_task(value, layout, metadata):
    fields = collection.fields(metadata, dict.fromkeys(layout["fields"], 500), set(), "输出字段")
    context = {key: fields.get(key) or layout.get(key, "") or "待核实" for key in layout["fields"]}
    handoff = {key: value[key] for key in ("part_id", "revision", "report_id")} | {"recipient": layout["recipient"]}
    inputs = {**value["inputs"], "job.json": json.dumps(handoff, ensure_ascii=False)}
    contract = CNC_CONTRACT if value["verifier"] == "cnc-fai" else QIF_CONTRACT
    complexity = {"input_files": len(inputs), "input_bytes": sum(len(text.encode()) for text in inputs.values()),
                  "verification_layers": len(RULE_SETS[value["verifier"]])}
    if value["verifier"] == "cnc-fai":
        specifications = list(csv.DictReader(io.StringIO(value["inputs"]["requirements.csv"])))
        complexity.update(characteristics=len(specifications), units=len({row.get("unit") for row in specifications}))
    else:
        rows, _ = qif_records(value["inputs"]["inspection.qif"], value["revision"])
        complexity["records"] = len(rows)
    scenario = value["scenario"]
    stem = scenario + ("\n" + layout["assignment"] if layout.get("assignment") else "")
    description = f"岗位：{layout['role']}。下游接收方：{layout['recipient']}。\n{stem}\n\n{contract}"
    return {
        "id": value["id"] + "-" + hashlib.sha256(json.dumps(
            [layout["id"], layout["version"], VERSION, fields, value.get("variant", "as_recorded"), layout.get("assignment")],
            ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()[:12],
        "title": value["title"], "description": description, "inputs": inputs,
        "difficulty": "工程复杂度见输入规模与验收层", "complexity": complexity,
        "checks": list(RULE_SETS[value["verifier"]]), "rule_ids": [row["id"] for row in verifier_layers(value)],
        "data_origin": "source_linked_modified" if value.get("variant") else "source_linked",
        "derivation": value["derivation"], "variant": value.get("variant", "as_recorded"), "references": value["references"],
        "template": {"id": layout["id"], "version": layout["version"], "domain": layout["domain"],
                     "fields": layout["fields"]},
        "workflow": {key: layout[key] for key in ("role", "recipient", "business_value")},
        "fields": context, "verifier": {"id": value["verifier"], "version": VERSION},
    }


def score(value, expected, answer):
    cnc = value["verifier"] == "cnc-fai"
    answer = answer if isinstance(answer, dict) else {}
    fields = {"status", "findings"} if cnc else {"records", "issues"}
    shape = answer.keys() == fields | {"handoff"} and isinstance(answer.get("handoff"), dict)
    if cnc:
        items = metrology._index_findings(answer.get("findings"))
        shape = shape and items is not None and all(row.keys() == {
            "characteristic_id", "status", "code", "value_in_requirement_unit", "unit",
        } for row in items.values())
        engineering = metrology.verify_answer({"reference": expected}, answer)["passed"]
    else:
        records = answer.get("records")
        shape = shape and isinstance(records, list) and all(
            isinstance(row, dict) and row.keys() == {"id", "value", "reported_status"} and isinstance(row["id"], str)
            for row in records
        ) and isinstance(answer.get("issues"), list) and all(isinstance(item, str) for item in answer["issues"])
        actual = {row["id"]: row for row in records} if shape else {}
        reference = {row["id"]: row for row in expected["records"]}
        shape = shape and len(actual) == len(records)
        engineering = shape and sorted(answer["issues"]) == expected["issues"] and actual.keys() == reference.keys() and all(
            actual[key]["reported_status"] == row["reported_status"]
            and metrology._equal_number(actual[key]["value"], row["value"]) for key, row in reference.items()
        )
    passed = [bool(shape), bool(engineering), answer.get("handoff") == expected["handoff"]]
    checks = [{**layer, "passed": good} for layer, good in zip(verifier_layers(value), passed)]
    return {"passed": all(passed), "checks": checks,
            "reason": "通过全部验收层" if all(passed) else "；".join(check["name"] for check in checks if not check["passed"])}


def audit(value, expected):
    if value["verifier"] == "cnc-fai":
        records = deepcopy(expected["findings"])
        if records:
            records[0]["code"] = "unsupported_reason"
        altered = {**expected, "findings": records}
    else:
        records = deepcopy(expected["records"])
        if records:
            records[0]["reported_status"] = "unsupported_status"
        altered = {**expected, "records": records} if records else {**expected, "issues": ["unsupported_reason"]}
    mutations = (
        ("合格提交", expected, True),
        ("缺少交付字段", {key: item for key, item in expected.items() if key != "handoff"}, False),
        ("特性结果遗漏", {**expected, ("findings" if value["verifier"] == "cnc-fai" else "records"): None}, False),
        ("下游工件版本错配", {**expected, "handoff": {**expected["handoff"], "revision": ""}}, False),
        ("擅自更改处置", {**expected, "handoff": {**expected["handoff"], "action": "release"}}, False),
        ("特性判断或报告字段被篡改", altered, False),
    )
    checks = [{"name": name, "passed": score(value, expected, fixture)["passed"] == accepted}
              for name, fixture, accepted in mutations]
    return {"name": "分层验证器回归", "status": "passed" if all(row["passed"] for row in checks) else "failed",
            "scope": "离线提交、工程证据和下游交接检查。实物放行及未量化工况由现场复核。",
            "checks": checks}

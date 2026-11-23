"""Admission checks for scoped industrial references, artifacts and task definitions."""

import csv
import hashlib
import io
import json
import re
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import reference_catalog as catalog
from .scope_guard import ScopeViolationError, dumps

AUTHORITIES = ("acceptance", "standard", "manufacturer", "open_record", "expert", "research")
CLAUSE_KINDS = ("format", "threshold", "command", "process", "acceptance", "failure_case")
MIN_TASK_CLAUSES = 3
REFERENCE_FIELDS = ("title", "summary_zh", "publisher_info", "version_or_date", "applicable_scope", "limitations", "extracted_clauses")
FILTERED_SOURCE = "低规范价值 · 已过滤。原文短引未提供可执行的参数、格式、命令、验收条件或故障判例。"
READY_REF = "json_array_length(quality_json,'$.extracted_clauses') > 0"
PLACEHOLDER = re.compile(
    r"^(?:待补(?:充)?|待(?:定|查|核实|确认)|未知|暂无|不详|不适用|无|TBD|TODO|N/?A|unknown|placeholder|probe(?:\d*| (?:limit value|role|provenance|task|title|value))|no[- ]?op|test only|dummy|lorem ipsum)[。.!！]?$"
    r"|本题不据此推算|无需评估|无法估算|按常规处理|根据实际情况而定|以实际情况为准|遵守相关标准",
    re.I,
)

# These are evidence checks, not a parser or a certification of the source.
MEASUREMENT = re.compile(
    r"[±≤≥<>]=?\s*\d|\d(?:[.,]\d+)?\s*(?:%|mm\b|μm\b|µm\b|microns?\b|mil\b|inches\b|\bV\b|kPa\b|°C|毫米|微米|摄氏度|倍|层|位|字符|孔)"
    r"|\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:decimal places|digits|bytes|degrees|holes)\b"
    r"|\b(?:must (?:now )?be set to|range[s]? from)\s*\d+", re.I)
HANDOFF_RULE = re.compile(
    r"\b(?:generated|prepared|reviewed|received)\s+by\b.+\b(?:from|before|after|according to)\b"
    r"|\b(?:before|after|prior to|once)\b.{0,130}\b(?:undergo(?:es)?|are (?:transferred|reviewed|inspected|tested|assembled|etched|plated)|"
    r"is (?:inspected|tested|performed)|proceed to (?:testing|packaging|cleaning))\b", re.I)
PHYSICAL_INSPECTION = re.compile(
    r"\b(?:spi|aoi|x-ray|flying probe|net test(?:ing)?)\b.{0,100}\b(?:measur\w+|detect\w+|inspect\w+|verif\w+|tests?)\b"
    r".{0,100}\b(?:height|area|volume|offset|bridg\w*|joints?|polarity|opens?|shorts?|misalignment)\b", re.I)
FABRICATION_MATERIAL = re.compile(
    r"\b(?:manufactur\w+|builds?|fabricat\w+)\b.{0,80}\b(?:using|with|on)\b.{0,75}"
    r"\b(?:fr[- ]?4|prepreg)\b.{0,25}\b(?:laminates? from|supplied by)\b", re.I)
PROCESS_CONTROL = re.compile(
    r"\b(?:check\w*|compar\w*|inspect\w*)\b.{0,100}\bagainst\b.{0,45}"
    r"\b(?:drill file|cam image|approved data|netlist|reference (?:data|image))\b"
    r"|\b(?:plac\w*|position\w*)\s+according to\b.{0,65}\bcoordinates?\b"
    r"|\breflow\b.{0,125}\b(?:controlled thermal profile|multi-zone reflow oven)\b"
    r"|\bBOM\s+data\b.{0,100}\bmatch\b.{0,35}\b(?:part numbers|package types)\b", re.I)
NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?")
COMMAND = re.compile(r"(?:\s|^)--[a-z][\w-]+|\bkicad-cli\s+\w+\s+\w+|\b(?:gerbv|freecadcmd)\s+-", re.I)
ENGINEERING_TERM = re.compile(
    r"\b(?:gerber|excellon|RS-274X|QIF|DMIS|STEP|CMM|CAD|ERC|DRC|DFM|BOM|units?|coordinates?|apertures?|"
    r"drill\w*|outline|holes?|layer\w*|dimension\w*|digits?|datum|toleranc\w*|pinout|inspection|revision|measurement|"
    r"differential|electroplating|laminat\w*|prepreg|reflow|solder\w*|etch\w*|pcb(?:a|s)?|"
    r"cpl|spi|aoi|x-ray|fr-4|designators?|(?:production|fabrication|dimensioning) files?)\b"
    r"|钻孔|坐标|板框|外形|图层|厚度|公差|测量|基准|版本|文件|退回|拒收|检验|引脚|装配|温度"
    r"|位号|锡膏|阻焊|回流|内层|外层|层压|覆铜板|预浸料|蚀刻|孔壁|裸板|贴装|焊点"
    r"|\.[a-z][a-z0-9_]{1,8}\b", re.I)
NORMATIVE = re.compile(
    r"\b(?:must|shall|required|only|ensure|reject\w*|prohibit\w*|specif\w*|format|encoding|"
    r"accept(?:ance|ed)?|include[ds]?|contain[ds]?|separate\w*|fail\w*|mismatch\w*)\b"
    r"|必须|要求|仅限|拒绝|拒收|不允许|应当|不可|不得|采用|规定|超出|至少|格式为|分开", re.I)
PROMOTIONAL = re.compile(
    r"\b(?:best.in.class|world.leading|improves productivity|automatically reconstructs|automatically rebuilds|"
    r"automatically identifies|exports an enhanced version|restores the intelligence|intuitiv\w+|effortlessly|"
    r"contact sales|product overview|learn more)\b"
    r"|功能介绍|产品亮点|显著提高效率|自动重建|一键还原|行业领先", re.I)


def object_fields(value, required, optional=(), *, label):
    if not isinstance(value, dict):
        raise ScopeViolationError(label + "须为对象")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        fields = "、".join(sorted(missing or extra))
        raise ScopeViolationError(f"{label}{'缺少' if missing else '含未知字段'} {fields}")
    return value


def meaningful(value, label, limit=600):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or "\x00" in value:
        raise ScopeViolationError(f"{label}须为 1–{limit} 字符的具体内容")
    if PLACEHOLDER.search(value.strip()):
        raise ScopeViolationError(f"{label}需要具体内容及依据")
    return value.strip()


def entries(value, label, maximum=12):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ScopeViolationError(f"{label}须包含 1–{maximum} 项")
    return value


def _original(text, kind):
    measured = bool(MEASUREMENT.search(text))
    command = bool(COMMAND.search(text))
    rule = bool(ENGINEERING_TERM.search(text) and NORMATIVE.search(text) and not PROMOTIONAL.search(text))
    process = bool(ENGINEERING_TERM.search(text) and not PROMOTIONAL.search(text) and (
        HANDOFF_RULE.search(text) or PHYSICAL_INSPECTION.search(text) or FABRICATION_MATERIAL.search(text)
        or PROCESS_CONTROL.search(text)))
    return {"threshold": measured, "command": command, "format": rule or measured,
            "process": rule or command or process,
            "acceptance": rule or measured, "failure_case": rule or measured}[kind]


def technical_excerpt(quote):
    return any(_original(quote, kind) for kind in CLAUSE_KINDS)


def marketing_excerpt(quote):
    return bool(PROMOTIONAL.search(quote) and not technical_excerpt(quote))


def reference_document(value, passages):
    object_fields(value, REFERENCE_FIELDS, label="核验文献")
    publisher = object_fields(value["publisher_info"], ("name", "authority", "qualification"), label="发布方")
    name = meaningful(publisher["name"], "发布机构全称", 160)
    qualification = meaningful(publisher["qualification"], "发布方资质", 300)
    authority = publisher["authority"]
    if authority not in AUTHORITIES:
        raise ScopeViolationError("请选择发布方的权威分类")
    release = object_fields(value["version_or_date"], (), ("version", "published_on"), label="发布版本")
    if not release:
        raise ScopeViolationError("须提供原文版本、标准编号或发布日期")
    version = meaningful(release["version"], "发布版本或标准号", 120) if "version" in release else None
    if version and (re.fullmatch(r"[a-f0-9]{64}", version, re.I) or
                    re.fullmatch(r"(?:latest|current|official|最新版|官网|最新|无版本)", version, re.I)):
        raise ScopeViolationError("请使用原文版本或发布日期")
    published = release.get("published_on")
    if "published_on" in release:
        try:
            if date.fromisoformat(published).isoformat() != published:
                raise ValueError
        except (ValueError, TypeError):
            raise ScopeViolationError("发布日期须为 YYYY-MM-DD") from None
    scope = object_fields(value["applicable_scope"], ("applies_to", "preconditions"), label="适用范围")
    clauses = {}
    lines = {row["locator"]: " ".join(row["text"].split()) for row in passages}
    for item in entries(value["extracted_clauses"], "结构化条款", 8):
        object_fields(item, ("kind", "locator", "quote", "requirement", "check"), label="技术条款")
        kind = item["kind"]
        if kind not in CLAUSE_KINDS:
            raise ScopeViolationError("技术条款类型无效")
        locator = meaningful(item["locator"], "原文定位", 240)
        quote = " ".join(meaningful(item["quote"], "原文短引", 220).split())
        if len(quote) < 12 or quote.casefold() not in lines.get(locator, "").casefold():
            raise ScopeViolationError(f"技术短引须在当前工序的固定原文版本中精确定位。请复核 {locator}")
        requirement = meaningful(item["requirement"], "技术条款", 480)
        check = meaningful(item["check"], "可执行检查", 480)
        if not _original(quote, kind):
            continue
        if kind == "command" and re.search(r"\[--[a-z]", quote) and re.search(
                r"必须|强制|必选|不得省略|\bmust\b", requirement, re.I):
            raise ScopeViolationError("命令用法中的方括号表示可选参数。请按原文区分可用选项与工单要求")
        if not ENGINEERING_TERM.search(requirement + " " + check) and not (MEASUREMENT.search(requirement) or COMMAND.search(requirement)):
            raise ScopeViolationError("条款及检查方法须指明可核对的参数、文件、命令或工程条件")
        identity = hashlib.sha256(dumps([locator, quote.casefold()]).encode()).hexdigest()[:16]
        clauses[identity] = {"id": identity, "kind": kind, "locator": locator, "quote": quote,
                             "requirement": requirement, "check": check}
    result = {
        "title": meaningful(value["title"], "文档正式名称", 220),
        "summary_zh": meaningful(value["summary_zh"], "核心技术定位", 480),
        "publisher_info": {"name": name, "authority": authority, "qualification": qualification},
        "version_or_date": {key: item for key, item in (("version", version), ("published_on", published)) if item},
        "applicable_scope": {"applies_to": meaningful(scope["applies_to"], "适用对象"),
                             "preconditions": [meaningful(item, "工程前提") for item in entries(scope["preconditions"], "工程前提", 8)]},
        "limitations": [meaningful(item, "不适用边界") for item in entries(value["limitations"], "不适用边界", 8)],
        "extracted_clauses": list(clauses.values()),
    }
    return result


def eligible_references(db, domain, node=None):
    clause = "" if node is None else " AND " + catalog.LINK.format(node="?")
    args = (domain,) if node is None else (domain, node)
    return {identity: (process, json.loads(profile)) for identity, process, profile in db.execute(f"""
        SELECT id,node_id,quality_json FROM verified_references v WHERE domain_id=? AND {READY_REF}{clause}
        ORDER BY verified_at DESC, id
    """, args)}


def task_admission(db, domain, nodes=()):
    selected = set(nodes)
    relationships = {row[0] for row in db.execute("SELECT node_id FROM grounded_process_relations WHERE domain_id=?", (domain,))}
    refs = eligible_references(db, domain)
    links = {}
    for node, ref in db.execute("SELECT node_id,reference_id FROM verified_reference_nodes WHERE domain_id=?", (domain,)):
        if ref in refs:
            links.setdefault(node, []).append(refs[ref][1])
    output = []
    for identity, title in db.execute("SELECT id,title FROM process_nodes WHERE domain_id=? ORDER BY position", (domain,)):
        if selected and identity not in selected:
            continue
        applicable = links.get(identity, [])
        clauses = {" ".join(item["quote"].casefold().split())
                   for profile in applicable for item in profile["extracted_clauses"]}
        issues = []
        if identity not in relationships:
            issues.append({"stage": 2, "message": "缺少有原文依据的企业交付关系。请注明发起方、接收方和交付物。"})
        if len(clauses) < MIN_TASK_CLAUSES:
            issues.append({"stage": 3, "message": f"已核验技术条款 {len(clauses)}/{MIN_TASK_CLAUSES}。请补充格式、参数、容差或验收条款。"})
        output.append({"id": identity, "title": title, "ready": not issues, "issues": issues, "clause_count": len(clauses)})
    return output


def resolve_citations(value, references):
    citations = {}
    for item in entries(value, "条款引用", 16):
        object_fields(item, ("reference_id", "clause_id", "applies_to"), label="条款引用")
        ref = item["reference_id"]
        clause = item["clause_id"]
        if not isinstance(ref, str) or not isinstance(clause, str) or ref not in references or not any(
                row["id"] == clause for row in references[ref][1]["extracted_clauses"]):
            raise ScopeViolationError("引用须使用当前工序已核验的 reference_id 和 clause_id")
        citations[ref, clause] = {"reference_id": ref, "clause_id": clause,
                                  "applies_to": meaningful(item["applies_to"], "条款在本题的用途", 500)}
    return list(citations.values())


def artifact_format(path, content):
    """Recognize engineering file content as well as its filename, never by extension alone."""
    suffix = PurePosixPath(path).suffix.lower()
    text = content.decode("utf-8-sig", "replace").strip()
    if "\x00" in text or not text or text.startswith(("{", "[")):
        return None
    if (re.fullmatch(r"\.(?:gbr|ger|gerber|art|g[bt][olps]|g[mk][o1]|gm\d|gko)", suffix)
            and re.search(r"%FS[LT]?[AI]X\d{2}Y\d{2}\*%", text)
            and re.search(r"%MO(?:MM|IN)\*%", text) and re.search(r"%ADD\d+[^*]+\*%", text)
            and re.search(r"[XY][+-]?\d+[XYD][^*]*\*", text) and text.endswith("M02*")):
        return "Gerber"
    if (suffix in (".drl", ".xln", ".exc", ".ncd", ".txt") and re.match(r"(?:;[^\n]*\n\s*)*M48\b", text)
            and re.search(r"^(?:METRIC|INCH|M7[12])\b", text, re.M)
            and re.search(r"^T\d+C[\d.]+", text, re.M)
            and re.search(r"^[XY][+-]?[\d.]+[XY]?", text, re.M) and re.search(r"^M30\b", text, re.M)):
        return "Excellon"
    if suffix in (".kicad_pcb", ".kicad_sch") and re.match(r"\(kicad_(?:pcb|sch)\s+\(version\s+\d+", text) and text.endswith(")"):
        return "KiCad"
    if suffix in (".step", ".stp") and text.startswith("ISO-10303-21;") and "DATA;" in text and "END-ISO-10303-21;" in text:
        return "STEP"
    if suffix == ".dxf" and re.search(r"^\s*0\s*\n\s*SECTION\s*\n", text, re.M) and text.endswith("EOF"):
        return "DXF"
    if suffix in (".nc", ".tap", ".gcode") and re.search(r"^(?:N\d+\s*)?G(?:0?[0123])\s*[XYZ]-?[\d.]+", text, re.M):
        return "NC"
    if suffix in (".qif", ".xml") and re.search(r"<(?:\w+:)?QIFDocument\b", text):
        return "QIF"
    if suffix in (".dmi", ".dmis") and re.search(r"^DMISMN/", text, re.M) and "ENDFIL" in text:
        return "DMIS"
    if suffix in (".cir", ".sp", ".spice") and re.search(r"^[RCLVI]\w+\s+\S+\s+\S+\s+\S+", text, re.M | re.I) and re.search(
            r"^\.end\b", text, re.M | re.I):
        return "SPICE"
    if suffix in (".d356", ".ipc") and re.search(r"^3(?:17|27|67)\S*\s", text, re.M) and re.search(r"^999\b", text, re.M):
        return "IPC-D-356"
    if suffix in (".csv", ".pos"):
        try:
            rows = csv.reader(io.StringIO(text))
            header = set(re.sub(r"[^a-z0-9_]", "", field.casefold()) for field in next(rows, []))
            record = next(rows, [])
        except csv.Error:
            return None
        if len(record) != len(header) or not {"", " "}.isdisjoint(header):
            return None
        if {"x", "y"} <= header and header & {"designator", "ref", "rotation", "side", "feature_id", "timestamp"}:
            return "坐标表"
        if {"nominal", "actual"} <= header and header & {"characteristic_id", "feature_id", "unit", "units"}:
            return "测量记录"
        if header & {"time", "time_s", "timestamp"} and header & {"temperature", "temperature_c", "pressure", "rpm", "voltage"}:
            return "工艺实测记录"
        if header & {"designator", "ref", "reference"} and header & {"mpn", "part_number", "footprint"}:
            return "BOM"
    return None


def _metric(value, label, extra, *, integral=False):
    details = object_fields(value, ("min", "max", "basis", "source_status", *extra), label=label)
    low, high = details["min"], details["max"]
    accepted = (int,) if integral else (int, float)
    if (type(low) not in accepted or type(high) not in accepted or not 0 <= low <= high <= 1_000_000_000
            or high <= 0):
        raise ScopeViolationError(label + "须提供有效的量化区间")
    if details["source_status"] not in ("documented", "estimate"):
        raise ScopeViolationError(label + "须标明实录或估算")
    return {**details, "basis": meaningful(details["basis"], label + "依据", 600),
            **{name: meaningful(details[name], label + " " + name, 300) for name in extra}}


TASK_FIELDS = ("industrial_context", "applicable_standards", "boundary_conditions", "edge_cases_and_rejection",
               "downstream_handoff", "input_artifacts", "deliverables", "value_assessment", "reference_citations")


def task_definition(db, domain, node, definition, files, downstream):
    missing = [name for name in TASK_FIELDS if name not in definition]
    if missing:
        raise ScopeViolationError("题目定义缺少 " + "、".join(missing))
    context = object_fields(definition["industrial_context"], ("upstream_input", "responsibility", "pain_point"), label="工业背景")
    references = eligible_references(db, domain, node)
    citations = resolve_citations(definition["reference_citations"], references)
    pairs = {(item["reference_id"], item["clause_id"]) for item in citations}

    def trace(item):
        if not isinstance(item["reference_id"], str) or not isinstance(item["clause_id"], str) or (
                item["reference_id"], item["clause_id"]) not in pairs:
            raise ScopeViolationError("适用标准及规范限值须关联本题已引用的具体条款")
        return {key: item[key] for key in ("reference_id", "clause_id")}

    standards = []
    for item in entries(definition["applicable_standards"], "适用标准", 8):
        object_fields(item, ("name", "version", "applies_to", "reference_id", "clause_id"), label="适用标准")
        citation = trace(item)
        source_version = references[item["reference_id"]][1]["version_or_date"]
        version = meaningful(item["version"], "适用版本", 120)
        if version not in source_version.values():
            raise ScopeViolationError("适用版本须与引用的原文版本或日期一致")
        standards.append({**citation, "version": version, "name": meaningful(item["name"], "标准名称", 220),
                          "applies_to": meaningful(item["applies_to"], "适用工艺或产品")})

    boundaries = []
    for item in entries(definition["boundary_conditions"], "边界条件"):
        keys = ("parameter", "limit", "unit", "precision", "exceptions", "basis")
        object_fields(item, keys, ("reference_id", "clause_id"), label="边界条件")
        boundary = {key: meaningful(item[key], "边界条件 " + key, 420) for key in keys}
        if item["basis"] == "normative":
            if not {"reference_id", "clause_id"} <= item.keys():
                raise ScopeViolationError("规范限值须关联原文条款")
            boundary.update(trace(item))
            original = next(row["quote"] for row in references[item["reference_id"]][1]["extracted_clauses"]
                            if row["id"] == item["clause_id"])
            if set(NUMBER.findall(boundary["limit"])) - set(NUMBER.findall(original)):
                raise ScopeViolationError("规范数值须能在引用短引中逐一核对；工单特有数值应标为本题工况")
        elif item["basis"] != "case" or {"reference_id", "clause_id"} & item.keys():
            raise ScopeViolationError("边界依据请选择有原文的 normative，或明确属本题工况的 case")
        boundaries.append(boundary)

    defects = []
    for item in entries(definition["edge_cases_and_rejection"], "异常处置"):
        object_fields(item, ("condition", "detection", "disposition"), label="异常处置")
        defects.append({key: meaningful(item[key], "异常处置 " + key) for key in item})

    handoff = object_fields(definition["downstream_handoff"], ("recipient", "process", "interface", "acceptance"), label="下游交付")
    if downstream and not db.execute("SELECT 1 FROM process_edges WHERE (domain_id,upstream_id,downstream_id)=(?,?,?)",
                                     (domain, node, downstream)).fetchone():
        raise ScopeViolationError("所选下道工序必须与当前工序存在直接交付连线")
    deliverables = []
    for item in entries(definition["deliverables"], "交付物", 8):
        object_fields(item, ("path", "format", "interface", "acceptance"), label="交付物")
        deliverables.append({key: meaningful(item[key], "交付物 " + key, 500) for key in item})
    artifacts = []
    for item in entries(definition["input_artifacts"], "工程输入", 20):
        object_fields(item, ("path", "role", "origin", "provenance"),
                      ("source_id", "format", "sha256", "bytes", "url"), label="工程输入文件")
        path = meaningful(item["path"], "输入工件路径", 240)
        origin = item["origin"]
        record = {"path": path, "origin": origin, "role": meaningful(item["role"], "工件角色", 250),
                  "provenance": meaningful(item["provenance"], "工件出处或生成说明", 500)}
        if origin == "public_source":
            src = item.get("source_id")
            if not isinstance(src, str) or path in files:
                raise ScopeViolationError("公开工件须携带实样信源 ID，且同名文件不能同时内联")
            row = db.execute("""SELECT w.artifact_json,s.canonical_url,s.sha256 FROM workflow_source_passages w
                JOIN raw_sources s ON (w.domain_id,w.source_id)=(s.domain_id,s.id)
                WHERE (w.domain_id,w.node_id,w.source_id)=(?,?,?)""", (domain, node, src)).fetchone()
            if not row or not (meta := json.loads(row[0])).get("format"):
                raise ScopeViolationError("公开工件须由上一阶段在此工序读取并核验真实文件格式")
            if PurePosixPath(path).suffix.casefold() != PurePosixPath(urlsplit(row[1]).path).suffix.casefold():
                raise ScopeViolationError("公开工件路径须保留原件扩展名")
            record.update(source_id=src, format=meta["format"], bytes=meta["bytes"], sha256=row[2], url=row[1])
        elif origin in ("sandbox_fixture", "supporting") and path in files and "source_id" not in item:
            data = files[path].encode()
            detected = artifact_format(path, data)
            if origin == "sandbox_fixture" and detected is None:
                raise ScopeViolationError(f"工程样本 {path} 内容不符合其工程格式。请提供实际工件，或登记公开实样。")
            record.update(format=detected or "辅助文件", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        else:
            raise ScopeViolationError("工件须指向已经核验的实样，或明确标记可运行的小样本及辅助文件")
        artifacts.append(record)

    if not set(files) <= {item["path"] for item in artifacts}:
        raise ScopeViolationError("输入文件须逐项登记在 input_artifacts")
    formats = {item["format"] for item in artifacts if item["origin"] != "supporting"}
    process = db.execute("SELECT title FROM process_nodes WHERE (domain_id,id)=(?,?)", (domain, node)).fetchone()[0]
    if not formats:
        raise ScopeViolationError("至少需要一份真实工程格式文件或可执行工程样本。JSON 清单只能用作辅助材料。")
    if re.search(r"CAM.*(?:入厂|收件|intake)|(?:Gerber|杰博).*(?:钻孔|drill)|(?:钻孔|drill).*Gerber", process, re.I) and not {
            "Gerber", "Excellon"} <= formats:
        raise ScopeViolationError("CAM 入厂核对须同时提供可解析的 Gerber 图层和 Excellon 钻孔工件")
    if not any(ENGINEERING_TERM.search(item["acceptance"]) or MEASUREMENT.search(item["acceptance"])
               or COMMAND.search(item["acceptance"]) for item in deliverables):
        raise ScopeViolationError("下游至少须有一项可检查的工程交付验收条件")

    value = object_fields(definition["value_assessment"],
                          ("frequency", "expert_minutes", "error_loss", "automation_value"), label="岗位价值")
    frequency = _metric(value["frequency"], "岗位频次", ("period",))
    if frequency["period"] not in ("shift", "day", "week", "month", "year", "batch"):
        raise ScopeViolationError("岗位频次须注明 shift、day、week、month、year 或 batch")
    expert = _metric(value["expert_minutes"], "专家耗时（分钟）", (), integral=True)
    loss = _metric(value["error_loss"], "错误损失", ("currency", "scenario"))
    if not re.fullmatch(r"[A-Z]{3}", loss["currency"]):
        raise ScopeViolationError("直接错误损失须给出 ISO 币种，例如 CNY 或 USD")
    automation = object_fields(value["automation_value"], ("benefit", "basis"), label="自动化替代价值")

    return {**definition, "industrial_context": {key: meaningful(text, "工业背景 " + key) for key, text in context.items()},
            "applicable_standards": standards, "boundary_conditions": boundaries, "edge_cases_and_rejection": defects,
            "downstream_handoff": {key: meaningful(text, "下游交付 " + key) for key, text in handoff.items()},
            "input_artifacts": artifacts, "deliverables": deliverables, "reference_citations": citations,
            "value_assessment": {"frequency": frequency, "expert_minutes": expert, "error_loss": loss,
                                 "automation_value": {key: meaningful(text, "自动化替代价值 " + key) for key, text in automation.items()}}}

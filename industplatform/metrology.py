"""Offline CNC measurement checks from the project's synthetic, normalized CSV."""

import argparse
import csv
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Optional


REQUIREMENT_COLUMNS = ("characteristic_id", "revision", "unit", "lower_limit", "upper_limit")
MEASUREMENT_COLUMNS = ("characteristic_id", "revision", "unit", "value")
MM_PER_UNIT = {"um": Decimal("0.001"), "mm": Decimal("1"), "cm": Decimal("10")}
UNIT_ALIASES = {"µm": "um", "μm": "um"}
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
DECIMAL_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


class InputError(ValueError):
    """A CSV file is structurally invalid."""


def _read_table(text: str, columns: tuple, label: str) -> list:
    try:
        reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
        fields = reader.fieldnames
        if fields is None or len(fields) != len(columns) or set(fields) != set(columns):
            raise InputError(f"{label} 应包含这些列：{', '.join(columns)}")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise InputError(f"{label} 第 {reader.line_num} 行的列数有误")
            rows.append({key: value.strip() for key, value in row.items()})
        return rows
    except csv.Error as error:
        raise InputError(f"{label} 无法解析：{error}") from error


def _decimal(value: str) -> Decimal:
    if len(value) > 48 or not DECIMAL_NUMBER.fullmatch(value):
        raise ValueError("预期长度不超过 48 字符的有限十进制数")
    return Decimal(value)


def _unit(value: str) -> str:
    return UNIT_ALIASES.get(value, value)


def _number_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _finding(characteristic_id: str, status: str, code: str, value=None, unit=None) -> dict:
    return {
        "characteristic_id": characteristic_id,
        "status": status,
        "code": code,
        "value_in_requirement_unit": value,
        "unit": unit,
    }


def _read_requirements(text: str) -> dict:
    requirements = {}
    for row in _read_table(text, REQUIREMENT_COLUMNS, "requirements.csv"):
        characteristic_id = row["characteristic_id"]
        if not IDENTIFIER.fullmatch(characteristic_id) or not IDENTIFIER.fullmatch(row["revision"]):
            raise InputError("规格文件中的特征 ID 和版本必须是有效标识")
        if characteristic_id in requirements:
            raise InputError(f"规格重复：{characteristic_id}")
        unit = _unit(row["unit"])
        if unit not in MM_PER_UNIT:
            raise InputError(f"{characteristic_id} 使用了未支持的规格单位")
        try:
            lower = _decimal(row["lower_limit"])
            upper = _decimal(row["upper_limit"])
        except ValueError as error:
            raise InputError(f"{characteristic_id} 的公差上下限格式有误") from error
        if lower > upper:
            raise InputError(f"{characteristic_id} 的公差下限高于上限")
        requirements[characteristic_id] = (row["revision"], unit, lower, upper)
    if not requirements:
        raise InputError("规格文件至少要定义一个检验特征")
    return requirements


def _evaluate_characteristic(characteristic_id: str, requirement, measurements: list) -> dict:
    if requirement is None:
        return _finding(characteristic_id, "hold", "unknown_characteristic")
    if not measurements:
        return _finding(characteristic_id, "hold", "missing_measurement")
    if len(measurements) > 1:
        return _finding(characteristic_id, "hold", "duplicate_measurement")

    revision, expected_unit, lower, upper = requirement
    sample = measurements[0]
    if sample["revision"] != revision:
        return _finding(characteristic_id, "hold", "revision_mismatch")
    if not sample["value"]:
        return _finding(characteristic_id, "hold", "missing_value")
    unit = _unit(sample["unit"])
    if unit not in MM_PER_UNIT:
        return _finding(characteristic_id, "hold", "unsupported_unit")
    try:
        value = _decimal(sample["value"])
    except ValueError:
        return _finding(characteristic_id, "hold", "invalid_number")

    # Metric conversion is exact for the supported units and input lengths.
    with localcontext() as context:
        context.prec = 96
        converted = value * MM_PER_UNIT[unit] / MM_PER_UNIT[expected_unit]
    value_text = _number_text(converted)
    if lower <= converted <= upper:
        return _finding(characteristic_id, "pass", "within_limits", value_text, expected_unit)
    return _finding(characteristic_id, "fail", "outside_limits", value_text, expected_unit)


def inspect_csv(requirements_text: str, measurements_text: str) -> dict:
    """Apply version checks, unit conversion and inclusive tolerance bounds."""
    try:
        requirements = _read_requirements(requirements_text)
        measurements = {}
        for row in _read_table(measurements_text, MEASUREMENT_COLUMNS, "measurements.csv"):
            characteristic_id = row["characteristic_id"]
            if not IDENTIFIER.fullmatch(characteristic_id):
                raise InputError("测量文件中的特征 ID 必须是有效标识")
            measurements.setdefault(characteristic_id, []).append(row)
    except InputError as error:
        return {"status": "hold", "findings": [], "input_error": str(error)}

    findings = [
        _evaluate_characteristic(
            characteristic_id, requirements.get(characteristic_id), measurements.get(characteristic_id, [])
        )
        for characteristic_id in sorted(requirements.keys() | measurements.keys())
    ]
    statuses = {finding["status"] for finding in findings}
    if "hold" in statuses:
        overall = "hold"
    elif "fail" in statuses:
        overall = "fail"
    else:
        overall = "pass"
    return {"status": overall, "findings": findings}


def rules() -> list:
    source = "industplatform v0.1.0 合成首件检验规则"
    definitions = [
        ("ID-REVISION", "特征与版本核对", "按特征 ID 连接两份文件，并要求检验规格与测量记录的版本相同。"),
        ("UNITS-BOUNDS", "单位换算与公差判定", "支持 mm、um、cm。换算到规格单位后按闭区间判定。"),
        ("HOLD-EVIDENCE", "证据不足时待复核", "缺测、非法数值、未知单位、额外特征和重复记录均需复核。"),
        ("OVERALL-RESULT", "汇总顺序", "任一特征待复核则整件待复核，否则任一超差则整件不合格，其余合格。"),
    ]
    return [
        {"id": rule_id, "title": title, "source": source, "description": description}
        for rule_id, title, description in definitions
    ]


def demo_asset() -> dict:
    return {
        "id": "demo-cnc-csv",
        "title": "CNC 首件离线检验样本",
        "source": "industplatform v0.1.0 项目合成夹具",
        "license": "本项目合成演示数据",
        "status": "accepted",
        "fields": ["特征 ID", "规格版本", "测量版本", "长度单位", "公差上下限", "测量值"],
        "summary": "使用两张规范化 CSV 验证特征连接、单位换算、公差判定和缺证处置。",
        "data_origin": "synthetic",
        "format": "CSV",
    }


ANSWER_CHECKS = [
    "整件状态符合规格、测量证据及汇总规则",
    "逐项包含全部特征，且特征状态正确",
    "逐项原因码与具体工况匹配",
    "按规格单位输出数值，待复核项的数值与单位留空",
]

ANSWER_CONTRACT = (
    "输入为 requirements.csv 与 measurements.csv。按 characteristic_id 关联，"
    "版本必须相同，规格单位支持 mm、um、cm。规格区间含端点。"
    "未知特征、缺测、重复测量、版本不匹配、空值、未支持单位和非法十进制数均为 hold。"
    "逐项校验先检查特征是否在规格中，再检查测量存在与唯一性，然后依次检查版本、空值、"
    "单位和数值。整件状态优先 hold，其次 fail，其余为 pass。"
    "只交付 JSON 对象，顶层字段为 status 和 findings 数组。"
    "每项含 characteristic_id、status、code、value_in_requirement_unit 和 unit。"
    "合格和超差分别使用 within_limits、outside_limits。"
    "待复核使用 unknown_characteristic、missing_measurement、duplicate_measurement、"
    "revision_mismatch、missing_value、unsupported_unit 或 invalid_number，"
    "并将 value_in_requirement_unit 和 unit 设为 null。"
)


def _csv_inputs(requirements: str, measurements: str) -> dict:
    return {
        "requirements.csv": ",".join(REQUIREMENT_COLUMNS) + "\n" + requirements,
        "measurements.csv": ",".join(MEASUREMENT_COLUMNS) + "\n" + measurements,
    }


def _task(task_id: str, title: str, difficulty: str, scenario: str,
          requirements: str, measurements: str, reference: dict) -> dict:
    return {
        "id": task_id,
        "title": title,
        "difficulty": difficulty,
        "description": f"{scenario}\n\n{ANSWER_CONTRACT}",
        "checks": list(ANSWER_CHECKS),
        "inputs": _csv_inputs(requirements, measurements),
        "rule_ids": [rule["id"] for rule in rules()],
        "reference": reference,
        "data_origin": "synthetic",
    }


def make_tasks(seed=None) -> list:
    """Create handcrafted fixtures or a reproducible set of numeric variants."""
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF):
        raise ValueError("seed must be an integer between 0 and 4294967295")
    tasks = [
        _task(
            "cnc-01-boundary", "首件尺寸边界核对", "基础候选", "检验单个特征，并按闭区间判定端点。",
            "WIDTH,A,mm,9.98,10.02\n", "WIDTH,A,mm,10.02\n",
            {"status": "pass", "findings": [
                {"characteristic_id": "WIDTH", "status": "pass", "code": "within_limits",
                 "value_in_requirement_unit": "10.02", "unit": "mm"},
            ]},
        ),
        _task(
            "cnc-02-cross-file", "多特征跨表与单位换算", "进阶候选", "将顺序不同的三条测量与规格关联，再统一单位。",
            "WIDTH,B,mm,9.98,10.02\nHEIGHT,B,um,1990,2010\nOFFSET,B,mm,-0.10,0.10\n",
            "OFFSET,B,um,-100\nHEIGHT,B,mm,2.005\nWIDTH,B,cm,1.003\n",
            {"status": "fail", "findings": [
                {"characteristic_id": "HEIGHT", "status": "pass", "code": "within_limits",
                 "value_in_requirement_unit": "2005", "unit": "um"},
                {"characteristic_id": "OFFSET", "status": "pass", "code": "within_limits",
                 "value_in_requirement_unit": "-0.1", "unit": "mm"},
                {"characteristic_id": "WIDTH", "status": "fail", "code": "outside_limits",
                 "value_in_requirement_unit": "10.03", "unit": "mm"},
            ]},
        ),
        _task(
            "cnc-03-revision-and-missing", "旧版检验记录与缺测", "异常候选", "确认已有测量能否支持当前版本的放行结论。",
            "WIDTH,C,mm,9.98,10.02\nHEIGHT,C,mm,4.95,5.05\nDIAMETER,C,mm,5.9,6.1\n",
            "WIDTH,B,mm,10.00\nDIAMETER,C,mm,6.3\n",
            {"status": "hold", "findings": [
                {"characteristic_id": "DIAMETER", "status": "fail", "code": "outside_limits",
                 "value_in_requirement_unit": "6.3", "unit": "mm"},
                {"characteristic_id": "HEIGHT", "status": "hold", "code": "missing_measurement",
                 "value_in_requirement_unit": None, "unit": None},
                {"characteristic_id": "WIDTH", "status": "hold", "code": "revision_mismatch",
                 "value_in_requirement_unit": None, "unit": None},
            ]},
        ),
        _task(
            "cnc-04-invalid-evidence", "混合异常测量证据", "异常候选", "识别重复测量、非法数值及规格之外的特征。",
            "WIDTH,A,mm,9.98,10.02\nHEIGHT,A,mm,4.95,5.05\nDEPTH,A,mm,1.95,2.05\n",
            "WIDTH,A,mm,9.99\nWIDTH,A,mm,10.00\nHEIGHT,A,mm,NaN\n"
            "DEPTH,A,in,0.079\nUNLISTED,A,mm,1\n",
            {"status": "hold", "findings": [
                {"characteristic_id": "DEPTH", "status": "hold", "code": "unsupported_unit",
                 "value_in_requirement_unit": None, "unit": None},
                {"characteristic_id": "HEIGHT", "status": "hold", "code": "invalid_number",
                 "value_in_requirement_unit": None, "unit": None},
                {"characteristic_id": "UNLISTED", "status": "hold", "code": "unknown_characteristic",
                 "value_in_requirement_unit": None, "unit": None},
                {"characteristic_id": "WIDTH", "status": "hold", "code": "duplicate_measurement",
                 "value_in_requirement_unit": None, "unit": None},
            ]},
        ),
    ]
    if seed is not None:
        _apply_seed_variants(tasks, seed)
    return tasks


def _format_micrometres(value: int, scale: int = 1000) -> str:
    """Convert integer micrometres to an exact decimal string for fixture generation."""
    whole, remainder = divmod(abs(value), scale)
    sign = "-" if value < 0 else ""
    if remainder == 0:
        return f"{sign}{whole}"
    places = len(str(scale)) - 1
    fraction = f"{remainder:0{places}d}".rstrip("0")
    return f"{sign}{whole}.{fraction}"


def _apply_seed_variants(tasks: list, seed: int) -> None:
    # The generator uses integer micrometres, and constructs each result by
    # selecting values on a known side of a boundary. It never runs the tool.
    entropy = hashlib.sha256(seed.to_bytes(4, "big")).digest()
    width = 9000 + entropy[0] * 11
    height = 1900 + entropy[1] * 2
    diameter = 5800 + entropy[2] * 3
    depth = 1800 + entropy[3]
    tolerance = 10 + entropy[4] % 42
    offset = 40 + entropy[5]
    excess = 1 + entropy[6] % 27
    expected_revision = f"R{1 + entropy[7]}"
    obsolete_revision = f"R{entropy[7]}"
    direction = 1 if entropy[8] % 2 else -1
    boundary = width + direction * tolerance
    off_width = boundary + direction * excess
    off_diameter = diameter + tolerance + excess
    measured_height = height + tolerance // 2
    bad_number = "NaN" if entropy[9] % 2 else "Infinity"
    bad_unit = "in" if entropy[10] % 2 else "mil"
    mm = _format_micrometres

    def limits(feature, centre):
        return f"{feature},{expected_revision},mm,{mm(centre - tolerance)},{mm(centre + tolerance)}\n"

    def measured(feature, value, unit="mm", revision=expected_revision):
        return f"{feature},{revision},{unit},{value}\n"

    variants = [
        (
            limits("WIDTH", width),
            measured("WIDTH", mm(boundary)),
            "pass",
            [_finding("WIDTH", "pass", "within_limits", mm(boundary), "mm")],
        ),
        (
            limits("WIDTH", width)
            + f"HEIGHT,{expected_revision},um,{height - tolerance},{height + tolerance}\n"
            + f"OFFSET,{expected_revision},mm,{mm(-offset)},{mm(offset)}\n",
            measured("OFFSET", -offset, "um")
            + measured("HEIGHT", mm(measured_height))
            + measured("WIDTH", mm(off_width, 10000), "cm"),
            "fail",
            [
                _finding("HEIGHT", "pass", "within_limits", str(measured_height), "um"),
                _finding("OFFSET", "pass", "within_limits", mm(-offset), "mm"),
                _finding("WIDTH", "fail", "outside_limits", mm(off_width), "mm"),
            ],
        ),
        (
            limits("WIDTH", width) + limits("HEIGHT", height) + limits("DIAMETER", diameter),
            measured("WIDTH", mm(width), revision=obsolete_revision)
            + measured("DIAMETER", mm(off_diameter)),
            "hold",
            [
                _finding("DIAMETER", "fail", "outside_limits", mm(off_diameter), "mm"),
                _finding("HEIGHT", "hold", "missing_measurement"),
                _finding("WIDTH", "hold", "revision_mismatch"),
            ],
        ),
        (
            limits("WIDTH", width) + limits("HEIGHT", height) + limits("DEPTH", depth),
            measured("WIDTH", mm(width)) + measured("WIDTH", mm(width + 1))
            + measured("HEIGHT", bad_number) + measured("DEPTH", mm(depth), bad_unit)
            + measured("UNLISTED", mm(width)),
            "hold",
            [
                _finding("DEPTH", "hold", "unsupported_unit"),
                _finding("HEIGHT", "hold", "invalid_number"),
                _finding("UNLISTED", "hold", "unknown_characteristic"),
                _finding("WIDTH", "hold", "duplicate_measurement"),
            ],
        ),
    ]
    for task, (requirements, measurements, status, findings) in zip(tasks, variants):
        task["inputs"] = _csv_inputs(requirements, measurements)
        task["reference"] = {"status": status, "findings": findings}
        task["generator_seed"] = seed


def _index_findings(findings) -> Optional[dict]:
    if not isinstance(findings, list):
        return None
    indexed = {}
    for finding in findings:
        if not isinstance(finding, dict):
            return None
        key = finding.get("characteristic_id")
        if not isinstance(key, str) or key in indexed:
            return None
        indexed[key] = finding
    return indexed


def _equal_number(left, right) -> bool:
    if left is None or right is None:
        return left is right
    if type(left) not in (str, int, float) or type(right) not in (str, int, float):
        return False
    try:
        left_decimal = Decimal(str(left))
        right_decimal = Decimal(str(right))
        return left_decimal.is_finite() and right_decimal.is_finite() and left_decimal == right_decimal
    except (ValueError, InvalidOperation):
        return False


def verify_answer(task: dict, answer) -> dict:
    """Score against the frozen answer without calling the inspection engine."""
    expected = _index_findings(task["reference"]["findings"])
    actual = _index_findings(answer.get("findings")) if isinstance(answer, dict) else None
    same_features = actual is not None and actual.keys() == expected.keys()
    results = [
        isinstance(answer, dict) and answer.get("status") == task["reference"]["status"],
        same_features and all(actual[key].get("status") == expected[key]["status"] for key in expected),
        same_features and all(actual[key].get("code") == expected[key]["code"] for key in expected),
        same_features and all(
            "unit" in actual[key]
            and "value_in_requirement_unit" in actual[key]
            and actual[key]["unit"] == expected[key]["unit"]
            and _equal_number(actual[key]["value_in_requirement_unit"], expected[key]["value_in_requirement_unit"])
            for key in expected
        ),
    ]
    checks = [{"name": name, "passed": passed} for name, passed in zip(ANSWER_CHECKS, results)]
    failed = [check["name"] for check in checks if not check["passed"]]
    return {"passed": not failed, "reason": "；".join(failed) if failed else "通过全部校验", "checks": checks}


def audit_tool() -> dict:
    checks = []
    for task in make_tasks():
        inputs = task["inputs"]
        actual = inspect_csv(inputs["requirements.csv"], inputs["measurements.csv"])
        checks.append({"name": task["title"], "passed": verify_answer(task, actual)["passed"]})

    fixture = make_tasks()[0]["inputs"]
    measured_mm = inspect_csv(fixture["requirements.csv"], fixture["measurements.csv"])
    measured_um = inspect_csv(
        fixture["requirements.csv"],
        fixture["measurements.csv"].replace("mm,10.02", "um,10020"),
    )
    checks.append({"name": "等价单位保持判定一致", "passed": measured_mm == measured_um})
    invalid = inspect_csv(fixture["requirements.csv"], "unknown,column\n")
    checks.append({"name": "异常文件进入待复核", "passed": invalid["status"] == "hold" and "input_error" in invalid})
    return {
        "name": "CNC 首件离线检验 CLI",
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "checks": checks,
        "scope": "合成 CSV 夹具和离线规则",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="对规范化 CSV 执行离线首件检验")
    parser.add_argument("requirements", type=Path)
    parser.add_argument("measurements", type=Path)
    args = parser.parse_args()
    try:
        requirements = args.requirements.read_text(encoding="utf-8-sig")
        measurements = args.measurements.read_text(encoding="utf-8-sig")
        report = inspect_csv(requirements, measurements)
    except (OSError, UnicodeError) as error:
        report = {"status": "hold", "findings": [], "input_error": str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if "input_error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())

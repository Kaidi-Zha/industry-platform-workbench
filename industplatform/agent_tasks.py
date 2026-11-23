"""Author synthetic CNC benchmark inputs with public validation metadata."""

import csv
import hashlib
import io
import json

from . import metrology


MAX_CSV_CHARS = 3000
TOOL_NAME = "author_cnc_task"
DIFFICULTY = "工程预设 · 未评级"
VERIFICATION = "offline_verified"
FINDING_CODES = {
    "pass": {"within_limits"},
    "fail": {"outside_limits"},
    "hold": {
        "unknown_characteristic", "missing_measurement", "duplicate_measurement",
        "revision_mismatch", "missing_value", "unsupported_unit", "invalid_number",
    },
}


def author(title, scenario, requirements_csv, measurements_csv):
    title, scenario = title.strip(), scenario.strip()
    if not title or not scenario:
        raise ValueError("请提供题目标题和检验场景")

    inputs = {"requirements.csv": requirements_csv, "measurements.csv": measurements_csv}
    result = metrology.inspect_csv(requirements_csv, measurements_csv)
    if "input_error" in result:
        raise ValueError(result["input_error"])

    # Verify that every input feature has exactly one result and that the
    # summary agrees with the reported finding statuses.
    feature_ids = set()
    for source in inputs.values():
        rows = csv.DictReader(io.StringIO(source.removeprefix("\ufeff"), newline=""))
        feature_ids.update(row["characteristic_id"].strip() for row in rows)
    findings = result["findings"]
    finding_ids = {finding["characteristic_id"] for finding in findings}
    statuses = {finding["status"] for finding in findings}
    expected_status = next((status for status in ("hold", "fail", "pass") if status in statuses), None)
    if (
        finding_ids != feature_ids or len(findings) != len(feature_ids)
        or result["status"] != expected_status or not all(_valid_finding(finding) for finding in findings)
    ):
        raise ValueError("检验结果未通过特征覆盖和整件状态核验")

    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {
        "id": "agent-cnc-" + digest,
        "title": title,
        "difficulty": DIFFICULTY,
        "description": f"{scenario}\n\n{metrology.ANSWER_CONTRACT}",
        "checks": list(metrology.ANSWER_CHECKS),
        "inputs": inputs,
        "rule_ids": [rule["id"] for rule in metrology.rules()],
        "data_origin": "synthetic",
        "author": "codex",
        "verification": VERIFICATION,
    }

def _valid_finding(finding):
    status = finding["status"]
    value, unit = finding["value_in_requirement_unit"], finding["unit"]
    if finding["code"] not in FINDING_CODES.get(status, ()):
        return False
    if status == "hold":
        return value is None and unit is None
    return isinstance(value, str) and bool(metrology.DECIMAL_NUMBER.fullmatch(value)) and unit in metrology.MM_PER_UNIT

"""Import two narrow benchmark cases from the public NIST/MTC inspection file."""

import argparse
import csv
import hashlib
import io
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import archive_sample, collection, industrial_bench, metrology
from .storage import RunStore


ARCHIVE = "https://data.nist.gov/od/ds/73AE20D288CF02F1E05324570681502B1955/mtc.zip"
DATASET = "https://catalog.data.gov/dataset/design-manufacturing-and-inspection-data-for-a-box-assembly"
MEMBER = "QIF-IncomingInspection/CoverResults_20_samples.QIF"
SHA256 = "de1bd5f538fecc5c7fed0884da2a89f548b0efeb56ba9c0581bb8160a926da24"
ACTUALS = ("16", "24", "63", "76")
DOMAIN = "机械与离散制造"
DISCOVERY_URLS = frozenset({
    DATASET,
    "https://www.nist.gov/publications/design-manufacturing-and-inspection-data-three-component-assembly",
    "https://nvlpubs.nist.gov/nistpubs/jres/124/jres.124.004.pdf",
    "https://smstestbed.nist.gov/tdp/mtc",
})


def table(columns, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue()


def import_cases(store, path, *, share_allowed=False):
    with Path(path).open("rb") as handle:
        payload = handle.read(archive_sample.MAX_MEMBER + 1)
    cases, _reference = prepare_cases(store, payload, share_allowed=share_allowed)
    return [industrial_bench.register(store, case) for case in cases]


def prepare_cases(store, payload, *, share_allowed=False, run=None, discovery=None):
    if hashlib.sha256(payload).hexdigest() != SHA256:
        raise ValueError(f"文件校验值不符，请使用 NIST 数据包的 {MEMBER}")
    source = ET.fromstring(payload)
    for node in source.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    revision = source.findtext(".//PrintedDrawing/Version")
    unit = source.findtext(".//PrimaryUnits/LinearUnit/UnitName")
    result = source.find(".//MeasurementResults[@id='157']")
    nodes = {node.get("id"): node for node in source.iter() if node.get("id")}
    if revision != "1.0.0" or unit != "mm" or result is None:
        raise ValueError("公开原件的绘图版本或线性单位未能重现")
    selected = {row.get("id"): row for row in result.findall("MeasuredCharacteristics/CharacteristicActuals/*")}
    requirements, measurements, original_status = [], [], {}
    excerpt = ET.Element("QIFDocument", {"versionQIF": source.get("versionQIF")})
    ET.SubElement(excerpt, "PartVersion").text = revision
    group = ET.SubElement(excerpt, "CharacteristicActuals")
    for identity in ACTUALS:
        actual = selected[identity]
        item = nodes[actual.findtext("CharacteristicItemId")]
        nominal = nodes[item.findtext("CharacteristicNominalId")]
        definition = nodes[nominal.findtext("CharacteristicDefinitionId")]
        if actual.tag == "FlatnessCharacteristicActual":
            lower, upper = Decimal(0), Decimal(definition.findtext("ToleranceValue"))
        elif actual.tag == "DiameterCharacteristicActual":
            offset = Decimal(0) if definition.findtext("Tolerance/DefinedAsLimit") == "true" else Decimal(nominal.findtext("TargetValue"))
            lower = offset + Decimal(definition.findtext("Tolerance/MinValue"))
            upper = offset + Decimal(definition.findtext("Tolerance/MaxValue"))
        else:
            raise ValueError("该案例只映射平面度和孔径")
        measured = actual.findtext("Value").strip()
        reported = actual.findtext("Status/CharacteristicStatusEnum").strip()
        original_status[identity] = reported.casefold()
        requirements.append((identity, revision, unit, str(lower), str(upper)))
        measurements.append((identity, revision, unit, measured))
        node = ET.SubElement(group, actual.tag, {"id": identity})
        ET.SubElement(ET.SubElement(node, "Status"), "CharacteristicStatusEnum").text = reported
        ET.SubElement(node, "Value").text = measured
    csv_inputs = {"requirements.csv": table(metrology.REQUIREMENT_COLUMNS, requirements),
                  "measurements.csv": table(metrology.MEASUREMENT_COLUMNS, measurements)}
    findings = metrology.inspect_csv(*csv_inputs.values())["findings"]
    if {item["characteristic_id"]: item["status"] for item in findings} != original_status:
        raise ValueError("CSV 重算与公开 QIF 记录的逐项结果不一致")
    domain = run["plan"]["domain"] if run else DOMAIN
    published = collection.save_reference(store, {
        "title": "NIST/MTC Box Assembly · Cover 来料检验", "domain": domain,
        "scenario": run["plan"]["scenario"] if run else "来料检验及尺寸复核",
        "stage": "S5", "kind": "公开实验数据", "publisher": "NIST / MTC",
        "url": ARCHIVE, "version": "DOI 10.18434/T4/1503154",
        "locator": MEMBER + " · MeasurementResults 157",
        "claim": "原始 QIF 记录提供 mm 单位、实际值、实际状态及同文件关联的公差定义。",
    }, run["id"] if run else None, verification={
        "target": "measurement", "level": "sample", "quote": "<Value>0.042</Value>",
        "permission": "stated", "license_quote": "Data.gov catalog: https://www.nist.gov/open/license",
        "license_source": DATASET, "sha256": SHA256, "bytes": len(payload), "mime": "application/xml",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structured": {"format": "QIF 2.1.0", "root": "QIFDocument"}, "authority": "nist",
        "search_queries": discovery["verification"].get("search_queries", []) if discovery else [],
    })
    refs = [published["id"]] + ([discovery["id"]] if discovery else [])
    options = {"domain": domain, "part_id": source.findtext(".//Part/ModelNumber"), "revision": revision,
               "report_id": source.findtext("QPId") + "-157", "reference_ids": refs,
               "share_allowed": share_allowed}
    return [
        {**options, "verifier": "cnc-fai", "title": "盖板四项尺寸复核",
            "scenario": "按质量岗位复核试验件盖板抽样的四项实测特性，并提交下游处置建议。",
            "derivation": "NIST／MTC 来料实测结果 157。2026-11-20 选取 ID 16、24、63、76。"
                          "原值未改，平面度映射为零至上限，孔径按名义值和偏差计算区间，拆分成两张 CSV。",
            "inputs": csv_inputs},
        {**options, "verifier": "qif-intake", "title": "盖板 QIF 接收与追溯",
            "scenario": "从试验件盖板 QIF 抽样提取四条特性结果，判断能否把记录提交给来料系统导入。",
            "derivation": "NIST／MTC 来料实测结果 157。2026-11-20 只投影四项特性实际值与原始状态，"
                          "从 PrintedDrawing 复制版本到 PartVersion。投影用于本项目接收契约。",
            "inputs": {"inspection.qif": ET.tostring(excerpt, encoding="unicode")}},
    ], published


def sample_case(store, run, selection, stop):
    discovery = collection.reference(store, selection["reference_id"])
    if not discovery or discovery["url"] not in DISCOVERY_URLS or discovery["domain"] != run["plan"]["domain"]:
        raise ValueError("找不到支持采样的已核验 NIST/MTC 来源")
    payload, transferred = archive_sample.read_member(ARCHIVE, MEMBER, stop)
    cases, reference = prepare_cases(store, payload, run=run, discovery=discovery)
    draft = next(case for case in cases if case["verifier"] == selection["verifier"])
    return industrial_bench.prepare_case(store, draft), reference, transferred


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qif", type=Path, help=MEMBER)
    parser.add_argument("--db", type=Path, default=Path(".local/industplatform.sqlite3"))
    parser.add_argument("--allow-model-send", action="store_true", help="record operator approval for this data use")
    args = parser.parse_args()
    print(json.dumps(import_cases(RunStore(args.db), args.qif, share_allowed=args.allow_model_send),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Read-only access to the workspace's Forest keyword index."""

import csv
import os
from pathlib import Path

ATLAS = Path(__file__).resolve().parents[1] / "forest_atlas"
FIELD_MAP = {
    "领域": "domain",
    "主环节": "stage",
    "工程子场景": "title",
    "软件与求解器词": "software",
    "文件格式与协议词": "formats",
    "工业标准与规则词": "standards",
    "首选检索平台路由": "platforms",
    "L2_PEU": "scenario",
    "现成检索式": "query",
}


def _records():
    configured = os.environ.get("INDUSTPLATFORM_FOREST_DIR")
    atlas = Path(configured).expanduser() if configured else ATLAS
    file_path = atlas / "final" / "04_keyword_master.csv"
    if not file_path.is_file():
        return
    with file_path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            yield {target: row.get(field, "") or "" for field, target in FIELD_MAP.items()}


def domains():
    return list(dict.fromkeys(row["domain"] for row in _records() if row["domain"]))


def technical_terms():
    for row in _records():
        for field in ("software", "formats", "standards"):
            yield from (term.strip() for term in row[field].split(","))


def search_keywords(query="", limit=15):
    terms = query.casefold().split()
    found = []
    for record in _records():
        haystack = " ".join(record.values()).casefold()
        if all(term in haystack for term in terms):
            found.append(record)
            if len(found) >= limit:
                break
    return found

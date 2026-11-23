"""Frozen industry codes and advisory engineering-work coverage."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = "schemas/taxonomy_l2_registry.json"
LIFECYCLE_PATH = "schemas/lifecycle_stages.json"
HINTS_PATH = "schemas/taxonomy_l3_l4_hints.json"
L1_CODES = tuple(f"D{i:02d}" for i in range(1, 12))
L2_PATTERN = r"D(?:0[1-9]|1[01])\.[1-9][0-9]*"
CLUSTER_PATTERN = r"(?=.*[a-z])[a-z0-9]+(?:-[a-z0-9]+)*"
CLUSTER_LIMIT = 64
STAGES = tuple(f"S{i}" for i in range(9))
FIELDS = ("l2_code", "object_cluster", "lifecycle_stages")


def valid_cluster(value: str) -> bool:
    return len(value) <= CLUSTER_LIMIT and bool(re.fullmatch(CLUSTER_PATTERN, value))


def _rows(document: dict[str, Any], key: str, id_field: str) -> dict[str, dict[str, Any]]:
    items = document.get(key)
    if not isinstance(items, list):
        raise ValueError(f"分类码表缺少 {key}")
    rows = {}
    for item in items:
        identity = item.get(id_field) if isinstance(item, dict) else None
        if not isinstance(identity, str) or identity in rows:
            raise ValueError(f"分类码表的 {id_field} 缺失或重复")
        rows[identity] = item
    return rows


def registry_rows(document: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(document, dict) or document.get("type") != "string":
        raise ValueError("L2 码表须为 JSON 对象")
    l1 = _rows(document, "l1", "l1_code")
    l2 = _rows(document, "l2", "l2_code")
    if set(l1) != set(L1_CODES) or list(l2) != document.get("enum"):
        raise ValueError("L1 编码范围或 L2 枚举与分类记录不一致")
    parents: set[str] = set()
    for code, row in l2.items():
        parent = code.split(".", 1)[0]
        if (not re.fullmatch(L2_PATTERN, code) or row.get("l1_code") != parent
                or parent not in l1 or row.get("scope_kind") != l1[parent].get("scope_kind")):
            raise ValueError(f"分类码表的 L2 归属无效 {code}")
        parents.add(parent)
    if parents != set(l1):
        raise ValueError("每个 L1 均须关联实际存在的 L2")
    for rows in (l1, l2):
        slugs: set[str] = set()
        for row in rows.values():
            slug, name = row.get("english_id"), row.get("name_zh")
            if (not isinstance(slug, str) or not valid_cluster(slug) or slug in slugs
                    or not isinstance(name, str) or not name.strip()
                    or row.get("scope_kind") not in ("industry", "shared")):
                raise ValueError("分类码表缺少中文名、唯一英文标识或有效分类类型")
            slugs.add(slug)
    return l1, l2


def lifecycle_rows(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("type") != "string" or document.get("enum") != list(STAGES):
        raise ValueError("生命周期枚举须包含 S0 至 S8")
    stages = _rows(document, "stages", "code")
    if tuple(stages) != STAGES or any(
            not isinstance(item.get("name_zh"), str) or not item["name_zh"].strip() for item in stages.values()):
        raise ValueError("生命周期枚举与阶段定义不一致")
    return stages


@lru_cache(maxsize=1)
def registry() -> tuple[dict[str, Any], dict[str, Any]]:
    return registry_rows(json.loads((ROOT / REGISTRY_PATH).read_bytes()))


@lru_cache(maxsize=1)
def lifecycle() -> dict[str, Any]:
    return lifecycle_rows(json.loads((ROOT / LIFECYCLE_PATH).read_bytes()))


def require_l2(code: Any) -> str:
    if not isinstance(code, str) or code not in registry()[1]:
        raise ValueError("L2 编码须与官方码表完全一致，例如 D01.2")
    return code


def classification(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError("请提供唯一 l2_code、object_cluster 和 lifecycle_stages")
    code = require_l2(value["l2_code"])
    cluster = value["object_cluster"]
    if not isinstance(cluster, str) or not valid_cluster(cluster):
        raise ValueError("object_cluster 须为小写英文短横线标识，例如 pcb-assembly")
    stages = value["lifecycle_stages"]
    if (not isinstance(stages, list) or not stages
            or any(not isinstance(stage, str) or stage not in lifecycle() for stage in stages)
            or len(set(stages)) != len(stages)):
        raise ValueError("lifecycle_stages 须为 S0 至 S8 的非空无重复子集")
    return {"l2_code": code, "object_cluster": cluster, "lifecycle_stages": sorted(stages)}


def check_registry_change(path: str, current: bytes, previous: bytes | None) -> None:
    document = json.loads(current)
    if path == LIFECYCLE_PATH:
        lifecycle_rows(document)
    elif path == REGISTRY_PATH:
        clusters, industries = registry_rows(document)
        if previous is None:
            return
        old_clusters, old_industries = registry_rows(json.loads(previous))
        for before, after, fields in (
            (old_clusters, clusters, ("name_zh", "english_id", "scope_kind")),
            (old_industries, industries, ("l1_code", "name_zh", "english_id", "scope_kind")),
        ):
            if before.keys() != after.keys() or any(
                    any(row[key] != after[code][key] for key in fields) for code, row in before.items()):
                raise ValueError("分类编号和身份已固化，不能删除、改号、重分配或复用")


@lru_cache(maxsize=1)
def hints() -> dict[str, Any]:
    try:
        document = json.loads((ROOT / HINTS_PATH).read_bytes())
    except (OSError, ValueError):
        return {}
    groups = document.get("by_l2") if isinstance(document, dict) else None
    return groups if isinstance(groups, dict) else {}


def lookup(l2_code: str | None = None, offset: int = 0) -> dict[str, Any]:
    l1, l2 = registry()
    stages = [{"code": code, "name_zh": row["name_zh"]} for code, row in lifecycle().items()]
    if l2_code is None:
        return {
            "l1": [{key: row[key] for key in ("l1_code", "name_zh")} for row in l1.values()],
            "l2": [{key: row[key] for key in ("l2_code", "name_zh")} for row in l2.values()],
            "lifecycle_stages": stages,
        }
    code = require_l2(l2_code)
    group = hints().get(code, {})
    if not isinstance(group, dict):
        group = {}
    objects = group.get("objects", [])
    if not isinstance(objects, list):
        objects = []
    end = offset + 4
    return {"industry": l2[code], "lifecycle_stages": stages,
            "baseline": {key: group[key] for key in ("object_count", "scenario_count", "task_count") if key in group},
            "object_hints": objects[offset:end], "next_hint_offset": end if end < len(objects) else None}

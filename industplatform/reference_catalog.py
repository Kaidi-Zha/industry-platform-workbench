"""Classification and process links for admitted engineering references."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Any
from urllib.parse import unquote, urlsplit

from . import taxonomy

TIERS = {
    "T1_STANDARD": ("T1 行业标准", "T1 标准"),
    "T2_MANUFACTURER": ("T2 产线规范", "T2 产线"),
    "T3_TOOLCHAIN": ("T3 工具文档", "T3 工具"),
    "T4_CASE_SAMPLE": ("T4 工程样板", "T4 样板"),
}
CHANNELS = {
    "C1_STANDARD_ORG": "标准组织与法规库",
    "C2_OEM_FAB_SPEC": "头部制造／代工企业工艺手册",
    "C3_TOOL_FORMAT_DOC": "主流工程软件与格式官方文档",
    "C4_EQUIP_MAT_VENDOR": "设备与原材料原厂技术资料",
    "C5_ASSOC_WHITEPAPER": "行业协会与权威白皮书／研究报告",
    "C6_OPEN_ENG_REPO": "开源工程项目与基准测试样板",
    "C7_FAILURE_CASE": "工程失效复盘与真实工单／质量案例",
}
CHANNEL_TIERS = dict(zip(CHANNELS, (
    "T1_STANDARD", "T2_MANUFACTURER", "T3_TOOLCHAIN", "T2_MANUFACTURER",
    "T4_CASE_SAMPLE", "T4_CASE_SAMPLE", "T4_CASE_SAMPLE",
)))
UNASSIGNED = "unassigned"
NODE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}"
CLASSIFICATION_FIELDS = ("authority_tier", "source_channel", "process_node_ids", "object_cluster", "lifecycle_stages")
CASE_PAGE = re.compile(r"(?:case[- ]stud(?:y|ies)|failure analysis|root cause|failure report|案例|失效复盘)", re.I)
FORMAT_SPEC = re.compile(r"(?:gerber(?:[- ](?:job|layer))?|xnc|excellon)[- ][\w -]{0,35}(?:specification|format|规范)", re.I)


def validate_pair(tier: Any, channel: Any) -> None:
    if not isinstance(channel, str) or channel not in CHANNELS or tier != CHANNEL_TIERS[channel]:
        raise ValueError("请选择相符的文献权威层次和信源渠道")


def process_ids(value: Any) -> list[str]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not re.fullmatch(NODE_ID_PATTERN, item) for item in value)
            or len(set(value)) != len(value) or UNASSIGNED in value and value != [UNASSIGNED]):
        raise ValueError("process_node_ids 须为不重复的工序 ID，或仅填写 unassigned")
    return [] if value == [UNASSIGNED] else value


def object_and_stages(cluster: Any, stages: Any) -> tuple[str, list[str]]:
    if not isinstance(cluster, str) or not taxonomy.valid_cluster(cluster):
        raise ValueError("object_cluster 须为小写英文短横线标识")
    if (not isinstance(stages, list) or not stages or
            any(not isinstance(stage, str) or stage not in taxonomy.STAGES for stage in stages)
            or len(set(stages)) != len(stages)):
        raise ValueError("lifecycle_stages 须为 S0 至 S8 的非空无重复子集")
    return cluster, sorted(stages)


def at_host(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def known_channel(url, title=""):
    """Identify strong, type-specific signals; leave other origins for evidence review."""
    parts = urlsplit(url)
    host, path = parts.hostname or "", unquote(parts.path).casefold()
    pieces = path.strip("/").split("/")
    repository = host in {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "zenodo.org"}
    if repository and re.search(r"/(?:issues|pulls?|merge_requests)(?:/|$)", path):
        return "C7_FAILURE_CASE"
    if CASE_PAGE.search(path + " " + title):
        return "C7_FAILURE_CASE"
    if (at_host(host, ("docs.kicad.org", "docs.freecad.org"))
            or at_host(host, ("ucamco.com",)) and FORMAT_SPEC.search(path + " " + title)
            or repository and len(pieces) >= 2 and pieces[:2] in (["kicad", "kicad-doc"], ["freecad", "freecad-documentation"])):
        return "C3_TOOL_FORMAT_DOC"
    if at_host(host, ("jlcpcb.com", "eurocircuits.com", "pcbway.com")):
        return "C2_OEM_FAB_SPEC"
    if (at_host(host, ("iso.org", "iec.ch", "asme.org", "ipc.org", "ieee.org", "samr.gov.cn"))
            and re.search(r"/(?:std|standard|standards|obp|standards-documents|bz)\b|\b(?:ISO|IEC|IPC|GB/T|IEEE|ASME)\s+[A-Z]?\d", path + " " + title, re.I)):
        return "C1_STANDARD_ORG"
    return None


def legacy_channel(url, title, authority):
    channel = known_channel(url, title)
    if channel:
        return channel
    host = urlsplit(url).hostname or ""
    if host in {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}:
        return "C6_OPEN_ENG_REPO"
    return {
        "standard": "C1_STANDARD_ORG", "manufacturer": "C2_OEM_FAB_SPEC",
        "acceptance": "C7_FAILURE_CASE", "open_record": "C7_FAILURE_CASE",
        "expert": "C5_ASSOC_WHITEPAPER", "research": "C5_ASSOC_WHITEPAPER",
    }.get(authority, "C5_ASSOC_WHITEPAPER")


def linked_ids(db: sqlite3.Connection, domain: int, reference: str) -> list[str]:
    return [row[0] for row in db.execute("""SELECT l.node_id FROM verified_reference_nodes l
        JOIN process_nodes p ON (p.domain_id,p.id)=(l.domain_id,l.node_id)
        WHERE l.domain_id=? AND l.reference_id=? ORDER BY p.position""", (domain, reference))]


# The legacy node_id anchors the checked passages. Explicit links describe where the evidence applies.
LINK = """EXISTS(SELECT 1 FROM verified_reference_nodes link
    WHERE (link.domain_id,link.reference_id)=(v.domain_id,v.id) AND link.node_id={node})"""


def resolve_nodes(db, domain, ids):
    """Accept graph ordinals as input and persist durable process IDs."""
    nodes = db.execute("SELECT id,position FROM process_nodes WHERE domain_id=?", (domain,)).fetchall()
    exact = {node for node, _ in nodes}
    numbered = {str(position).zfill(2): node for node, position in nodes}
    resolved = [item if item in exact else numbered.get(item) for item in ids]
    if None in resolved or len(set(resolved)) != len(resolved):
        raise ValueError("工序须属于当前领域，且不能同时填写同一工序的编号与 ID")
    return resolved


def missing_standard(tiers):
    return (not tiers.get("T1_STANDARD") and bool(tiers.get("T2_MANUFACTURER") or tiers.get("T3_TOOLCHAIN")))


def coverage(db: sqlite3.Connection, domain: int) -> dict[str | None, dict[str, Any]]:
    from .engineering_quality import READY_REF

    totals: dict[str | None, dict[str, Any]] = {}
    for node, tier, channel, count in db.execute(f"""
        WITH admitted AS (SELECT id,domain_id,authority_tier,source_channel FROM verified_references
                          WHERE domain_id=? AND {READY_REF})
        SELECT NULL,authority_tier,source_channel,count(*) FROM admitted GROUP BY authority_tier,source_channel
        UNION ALL
        SELECT coalesce(l.node_id,?),v.authority_tier,v.source_channel,count(*) FROM admitted v LEFT JOIN verified_reference_nodes l
             ON (v.domain_id,v.id)=(l.domain_id,l.reference_id)
        GROUP BY l.node_id,v.authority_tier,v.source_channel
    """, (domain, UNASSIGNED)):
        item = totals.setdefault(node, {"tiers": {}, "channels": {}, "total": 0})
        item["tiers"][tier] = item["tiers"].get(tier, 0) + count
        item["channels"][channel] = item["channels"].get(channel, 0) + count
        item["total"] += count
    return totals


def _norm(title):
    return unicodedata.normalize("NFKC", title).casefold().strip()


def _legacy_node(db, domain, primary, profile):
    if primary:
        return primary
    labels = {value for field in ("stage", "process", "process_node")
              if isinstance(value := profile.get(field), str) and value.strip()}
    normalized = {_norm(re.sub(r"^\s*\d+\s*[.、：:·-]?\s+", "", label)) for label in labels}
    matches = [identity for identity, title in db.execute("SELECT id,title FROM process_nodes WHERE domain_id=?", (domain,))
               if _norm(title) in normalized]
    return matches[0] if len(matches) == 1 else None


def migrate(db):
    """Reclassify historical rows without changing reference IDs, clauses or their evidence links."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(verified_references)")}
    for column, definition in {
        "authority_tier": "TEXT REFERENCES reference_authority_tiers(id)",
        "source_channel": "TEXT REFERENCES crawl_channels(id)",
        "object_cluster": "TEXT",
        "lifecycle_stages_json": "TEXT CHECK(lifecycle_stages_json IS NULL OR json_valid(lifecycle_stages_json))",
    }.items():
        if column not in columns:
            db.execute(f"ALTER TABLE verified_references ADD COLUMN {column} {definition}")
    db.execute("""CREATE INDEX IF NOT EXISTS scope_audit_reference_output
        ON workflow_scope_audit(output_id) WHERE tool='promote_verified_ref' AND kind='applied'""")
    for domain, identity, node, title, authority, body, url, raw_label, run_label in db.execute("""
        SELECT v.domain_id,v.id,v.node_id,v.title,v.authority,v.quality_json,s.canonical_url,
            (SELECT i.import_metadata_json FROM raw_sources i
             WHERE i.domain_id=v.domain_id AND i.canonical_url=s.canonical_url AND i.import_metadata_json<>'{}'
             ORDER BY i.id LIMIT 1),
            (SELECT json_extract(r.body,'$.task_taxonomy') FROM workflow_scope_audit a
             JOIN workflow_scope_contracts c ON c.id=a.contract_id JOIN runs r ON r.id=c.run_id
             WHERE a.output_id=v.id AND a.kind='applied' AND a.tool='promote_verified_ref'
               AND CAST(json_extract(c.body,'$.domain_id') AS INTEGER)=v.domain_id
               AND json_type(r.body,'$.task_taxonomy')='object' ORDER BY a.sequence DESC LIMIT 1)
        FROM verified_references v JOIN raw_sources s ON (v.domain_id,v.source_id)=(s.domain_id,s.id)
        WHERE v.authority_tier IS NULL AND json_array_length(v.quality_json,'$.extracted_clauses')>0
    """).fetchall():
        profile = json.loads(body)
        channel = legacy_channel(url, title, profile.get("publisher_info", {}).get("authority") or authority)
        cluster, stages = None, None
        for label in (raw_label, run_label):
            if label:
                try:
                    properties = json.loads(label)
                    cluster, stages = object_and_stages(properties.get("object_cluster"), properties.get("lifecycle_stages"))
                    break
                except ValueError:
                    continue
        db.execute("""UPDATE verified_references SET authority_tier=?,source_channel=?,object_cluster=?,lifecycle_stages_json=?
            WHERE (domain_id,id)=(?,?)""", (CHANNEL_TIERS[channel], channel, cluster, json.dumps(stages) if stages else None,
                                            domain, identity))
        if matched := _legacy_node(db, domain, node, profile):
            db.execute("INSERT OR IGNORE INTO verified_reference_nodes VALUES (?,?,?)", (domain, identity, matched))

    db.execute("""CREATE INDEX IF NOT EXISTS reference_catalog_filter
        ON verified_references(domain_id,authority_tier,source_channel)""")
    # Old references may preserve historical taxonomy gaps; newly admitted references require every field.
    for event in ("INSERT", "UPDATE OF quality_json,authority_tier,source_channel,object_cluster,lifecycle_stages_json"):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS reference_classification_{event.split()[0].lower()}
            BEFORE {event} ON verified_references
            WHEN json_array_length(NEW.quality_json,'$.extracted_clauses')>0 AND (
                NEW.authority_tier IS NULL OR NEW.source_channel IS NULL OR NEW.object_cluster IS NULL
                OR NEW.lifecycle_stages_json IS NULL OR json_type(NEW.lifecycle_stages_json)<>'array'
                OR json_array_length(NEW.lifecycle_stages_json)=0
                OR EXISTS(SELECT 1 FROM json_each(NEW.lifecycle_stages_json) WHERE type<>'text'
                    OR value NOT IN ('S0','S1','S2','S3','S4','S5','S6','S7','S8'))
                OR json_array_length(NEW.lifecycle_stages_json)<>(SELECT count(DISTINCT value) FROM json_each(NEW.lifecycle_stages_json))
                OR length(NEW.object_cluster)>64 OR NEW.object_cluster GLOB '*[^a-z0-9-]*'
                OR NEW.object_cluster NOT GLOB '*[a-z]*' OR NEW.object_cluster GLOB '-*'
                OR NEW.object_cluster GLOB '*-' OR instr(NEW.object_cluster,'--')>0
                OR NOT EXISTS(SELECT 1 FROM reference_channel_tiers
                    WHERE channel=NEW.source_channel AND tier=NEW.authority_tier))
            BEGIN SELECT RAISE(ABORT,'admitted reference requires valid classification'); END""")

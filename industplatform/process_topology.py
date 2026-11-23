"""Classification, duplicate detection and audited display links for process DAGs."""

import hashlib
import re
import unicodedata
from collections import defaultdict, deque
from difflib import SequenceMatcher

from .taxonomy import STAGES


PREPARATION = "设计与工艺准备"
FABRICATION = "核心制造与加工"
ASSEMBLY = "装配与齐套"
INSPECTION = "检验与交付"
OPERATIONS = "运行与维护"
RECOVERY = "退役与再利用"
OTHER = "其他工序"
STAGE_GROUPS = {
    "S1": PREPARATION, "S2": PREPARATION, "S3": PREPARATION,
    "S4": FABRICATION, "S5": INSPECTION, "S6": OPERATIONS, "S8": RECOVERY,
}
NAME_GROUPS = tuple((re.compile(pattern, re.I), group) for pattern, group in (
    (r"设计定版|原理图|制造文件.*导出|生产文件确认|编程准备|\bcam\b|\bdfm\b|\bdesign\b", PREPARATION),
    (r"成品检验|终检|出货|验收|检验.*交付|测试.*交付|final inspection|acceptance|shipment", INSPECTION),
    (r"装配|齐套|贴装|锡膏|回流|焊接|\bsmt\b|\bbom\b|\bcpl\b|\bassembly\b", ASSEMBLY),
    (r"钻孔|层压|叠层|蚀刻|电镀|阻焊|裸板电测|金属化|成型|铸造|加工|开料|成像|电解"
     r"|表面处理|内层|外层|\bmanufactur|\bfabricat|\bdrill|\bmachining\b", FABRICATION),
    (r"维修|运维|巡检|检修|状态监测|maintenance|repair", OPERATIONS),
    (r"再利用|拆解|退役|回收|recycl|decommission", RECOVERY),
    (r"检验|检测|试验|验证|测量|inspection|testing|validation", INSPECTION),
    (r"研发|规划|工程准备|调研|工艺规划|planning|preparation", PREPARATION),
))
SEPARATORS = re.compile(r"[\s\W_]+", re.UNICODE)
PROCESS_ALIASES = (
    (r"(?<![a-z0-9])cam\s*(?:入件|入厂|接单|intake)", "cam接单"),
    (r"核查|校核|复核|查验", "核对"),
    (r"manufacturing\s+(?:files?|data)\s+export|fabrication\s+(?:files?|data)\s+export", "制造文件导出"),
    (r"制造数据导出|生产资料导出", "制造文件导出"),
)


def group_name(value):
    if not isinstance(value, str) or len(value) > 60 or any(ord(char) < 32 for char in value):
        raise ValueError("流程分区名称最多 60 字符")
    return " ".join(unicodedata.normalize("NFKC", value).split())


def lifecycle_stages(value):
    if (not isinstance(value, list) or any(not isinstance(item, str) or item not in STAGES for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("工序生命周期阶段须为不重复的 S0 至 S8")
    return sorted(value)


def title_key(title):
    normalized = unicodedata.normalize("NFKC", title).casefold().strip()
    normalized = re.sub(r"^\d{1,3}(?:[、:]\s*|\.\s+)", "", normalized)
    for pattern, replacement in PROCESS_ALIASES:
        normalized = re.sub(pattern, replacement, normalized)
    return SEPARATORS.sub("", normalized)


def matching_node(db, domain, title, *, exclude=None):
    key = title_key(title)
    if not key:
        raise ValueError("工序名称须包含文字或数字")
    return next(((identity, name) for identity, name in db.execute(
        "SELECT id,title FROM process_nodes WHERE domain_id=? ORDER BY position", (domain,))
        if identity != exclude and title_key(name) == key), None)


def similar_nodes(nodes, links):
    """Flag overlapping names for review, including broader stages containing a detailed step."""
    linked = {item["node_id"] for item in links}
    keys = [(node["id"], title_key(re.sub(r"\s*[（(][^()（）]+[）)]\s*$", "", node["title"])))
            for node in nodes if node["id"] not in linked]
    suggestions = defaultdict(list)
    for index, (left, name) in enumerate(keys):
        for right, candidate in keys[index + 1:]:
            smaller, larger = sorted((name, candidate), key=len)
            if not smaller or (smaller != larger and not (
                len(smaller) >= 6 and (smaller in larger or
                    len(smaller) / len(larger) >= .65 and SequenceMatcher(None, smaller, larger).ratio() >= .78)
            )):
                continue
            suggestions[left].append(right)
            suggestions[right].append(left)
    return dict(suggestions)


def _inferred_group(node):
    explicit = node.get("flow_group")
    if explicit:
        return explicit
    named = next((group for pattern, group in NAME_GROUPS if pattern.search(node["title"])), None)
    candidates = {STAGE_GROUPS[stage] for stage in node.get("lifecycle_stages", []) if stage in STAGE_GROUPS}
    if named in candidates or named == ASSEMBLY and FABRICATION in candidates:
        return named
    return next(iter(candidates)) if len(candidates) == 1 else named


def flow_groups(nodes, edges):
    incident = defaultdict(set)
    for edge in edges:
        left, right = edge["upstream_id"], edge["downstream_id"]
        incident[left].add(right)
        incident[right].add(left)
    assigned = {node["id"]: group for node in nodes if (group := _inferred_group(node))}
    remaining = {node["id"] for node in nodes} - assigned.keys()
    while remaining:
        inferred = {}
        for identity in remaining:
            groups = {assigned[other] for other in incident[identity] if other in assigned}
            if len(groups) == 1:
                inferred[identity] = next(iter(groups))
        if not inferred:
            break
        assigned.update(inferred)
        remaining.difference_update(inferred)

    grouped = defaultdict(list)
    for node in nodes:
        grouped[assigned.get(node["id"], OTHER)].append(node["id"])
    return [{"id": "flow-" + hashlib.sha256(name.encode()).hexdigest()[:12], "title": name, "node_ids": members}
            for name, members in grouped.items()]


def links_for(db, domain):
    return [dict(zip(("node_id", "canonical_id", "reason", "created_at"), row)) for row in db.execute("""
        SELECT node_id,canonical_id,reason,created_at FROM process_node_links
        WHERE domain_id=? ORDER BY node_id
    """, (domain,))]


def assert_linked_dag(db, domain):
    aliases = {row["node_id"]: row["canonical_id"] for row in links_for(db, domain)}
    if not aliases:
        return
    edges = {(aliases.get(left, left), aliases.get(right, right)) for left, right in db.execute(
        "SELECT upstream_id,downstream_id FROM process_edges WHERE domain_id=?", (domain,))}
    outgoing = defaultdict(list)
    incoming = defaultdict(int)
    for left, right in edges:
        if left != right:
            outgoing[left].append(right)
            incoming.setdefault(left, 0)
            incoming[right] += 1
    ready = deque(node for node, degree in incoming.items() if degree == 0)
    while ready:
        for target in outgoing[ready.popleft()]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if any(incoming.values()):
        raise ValueError("整理后的工艺图谱会形成循环，请选择其他工序")


def change_link(db, domain, node_id, value):
    action = value["action"]
    if action == "unlink":
        found = db.execute("SELECT canonical_id FROM process_node_links WHERE domain_id=? AND node_id=?",
                           (domain, node_id)).fetchone()
        if not found:
            return {"id": node_id, "changed": False}
        canonical_id = found[0]
        db.execute("DELETE FROM process_node_links WHERE domain_id=? AND node_id=?", (domain, node_id))
        reason = "恢复为独立工序"
    else:
        canonical_id, reason = value.get("canonical_id"), value.get("reason")
        if (not isinstance(canonical_id, str) or canonical_id == node_id or not db.execute(
            "SELECT 1 FROM process_nodes WHERE domain_id=? AND id=?", (domain, canonical_id)
        ).fetchone()):
            raise ValueError("请选择当前领域的另一道工序")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("请填写 1–500 字符的工序关联依据")
        reason = reason.strip()
        previous = db.execute("SELECT canonical_id,reason FROM process_node_links WHERE domain_id=? AND node_id=?",
                              (domain, node_id)).fetchone()
        if previous == (canonical_id, reason):
            return {"id": node_id, "changed": False}
        if previous or db.execute("""
            SELECT 1 FROM process_node_links WHERE domain_id=? AND (node_id=? OR canonical_id=?)
        """, (domain, canonical_id, node_id)).fetchone():
            raise ValueError("请先解除已有的工序关联，再重新整理")
        db.execute("""INSERT INTO process_node_links(domain_id,node_id,canonical_id,reason) VALUES (?,?,?,?)""",
                   (domain, node_id, canonical_id, reason))
        assert_linked_dag(db, domain)
    db.execute("""INSERT INTO process_node_link_audit(domain_id,node_id,canonical_id,action,reason)
        VALUES (?,?,?,?,?)""", (domain, node_id, canonical_id, action, reason))
    return {"id": node_id, "canonical_id": canonical_id, "changed": True}

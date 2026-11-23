"""Extract concise library keywords from existing engineering evidence."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from functools import lru_cache

from . import forest, taxonomy

LIMIT = 6
CJK = re.compile(r"[\u3400-\u9fff]")
LABEL = re.compile(r"\.?[^\W_][\w .+&/()-]{1,23}")
IDENTIFIER = re.compile(
    r"(?<![a-zA-Z0-9_.])(?:"
    r"(?P<standard>(?:ISO|IEC|IPC|IEEE|ASTM|ASME|JEDEC|JESD|GB(?:/T)?|DIN|EN|SAE|RS)[ -]?\d[A-Za-z0-9./-]{1,13})"
    r"|[A-Z][a-z]{2,} \d[A-Z]"
    r"|[A-Z]{3,9}(?:[- ]?\d{1,4})?"
    r"|[A-Z][a-z]{2,}[A-Z][A-Za-z0-9]{1,10}"
    r")(?![a-zA-Z0-9_])"
)
GENERIC = frozenset(("api arxiv csv doi en epa faq gb github gpl html http https json jrc llm mit ml na nist pdf readme rfc "
                     "sr srd tool ui url usd v1 v2 www zip 记录 模拟 协议 资源 topology").split())
REPORT_HEADING = re.compile(r"\bS\d{2}\s*·\s*")
HOSTNAME = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|cn|co|io)\b", re.I)
CATALOG_FIELDS = re.compile(r"(?:^|;)\s*(?:category|stage|owner|publisher|version|licen[cs]e|kind|segment|subsegment)=[^;]*", re.I)
TERMS = {
    "硫回收": ("sulfur recovery", "sulphur recovery"), "技改": ("技术改造",), "环评": ("环境影响评价",),
    "反应动力学": ("chemical kinetics", "reaction kinetics", "SABIO-RK"),
    "热物性": ("thermophysical", "DIPPR 801", "Dortmund Data Bank"), "热化学": ("thermochemical",),
    "溶解度": ("solubility",), "离子液体": ("ionic liquid", "ionic liquids", "ILThermo"),
    "热力学": ("thermodynamics",), "化学反应": ("CAS Reactions", "Open Reaction Database"),
    "有机合成": ("Organic Syntheses", "organic synthesis"), "ThermoML": (), "分离过程": ("separation process",),
    "油井故障": ("oil well failure dataset", "oil well failure"),
    "排放监测": ("discharge monitoring", "DMR Monitoring Data"), "负荷计算": ("计算负荷",),
    "碳捕集": ("carbon capture", "CO2 capture"), "排污许可": (), "中试": ("pilot plant",),
    "PCB": ("printed circuit board", "printed circuit boards"), "PCB 组装": ("PCB assembly",),
    "缺陷检测": ("defect detection",), "布局布线": ("place and route",), "封装": ("chip packaging",),
    "Gerber": (".gbr",), "Gerber X2": (), "KiCad": (".kicad_pcb",), "Excellon": (), "钻孔文件": ("drill file", "drill files"),
    "阻抗": ("impedance",), "现场失效": ("field failure",), "失效分析": ("failure analysis",),
    "逻辑分析仪": ("logic analyzer",), "示波器": ("oscilloscope",), "协议解码": ("protocol decoder",),
    "电源系统": ("power system",), "电力调度": ("power dispatch",), "故障定位": ("fault localization",),
    "车辆投诉": ("vehicle complaints",), "电子模块": ("electronic module",), "低压电缆": ("lv cable sizing",),
    "冷却风扇": ("cooling fan",), "Noctua": (),
    "电子废弃物": ("e-waste", "electronic waste"), "高光谱": ("hyperspectral",),
    "机器视觉": ("machine vision",), "光学检测": ("optical inspection",), "桩基": ("foundation piling",),
}


def _text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).replace("–", "-").replace("—", "-").split())


def short_label(text: str) -> bool:
    text = _text(text)
    return bool(LABEL.fullmatch(text) and len(CJK.findall(text)) <= 8 and sum(c.isalpha() for c in text) >= 2)


def merge(*groups: Iterable[str]) -> list[str]:
    labels: dict[str, str] = {}
    for group in groups:
        for label in group:
            label = _text(label)
            if label:
                key = "".join(char for char in label.casefold() if char.isalnum())
                labels.setdefault(key, label)
    return list(labels.values())[:LIMIT]


def _industry_terms(code: str | None) -> list[str]:
    industry = taxonomy.registry()[1].get(code or "")
    if not industry:
        return []
    return re.split(r"[、，；]|及相关|等", industry.get("include_zh", ""))


@lru_cache(maxsize=1)
def _common_terms() -> dict[str, str]:
    terms = {_text(term).casefold(): _text(term) for term in forest.technical_terms()
             if short_label(term) and term.casefold().lstrip(".") not in GENERIC}
    for label, aliases in TERMS.items():
        terms.update((_text(term).casefold(), label) for term in (label, *aliases))
    return terms


@lru_cache(maxsize=64)
def _matcher(code: str | None) -> tuple[dict[str, str], re.Pattern[str]]:
    terms = _common_terms().copy()
    objects = taxonomy.hints().get(code or "", {}).get("objects", [])
    hints = (term for row in objects for term in re.split(r"[与及、]", row.get("name_zh", "")))
    for term in (*_industry_terms(code), *hints):
        if short_label(term) and term.casefold() not in GENERIC:
            terms.setdefault(_text(term).casefold(), term)
    patterns = sorted(terms, key=lambda term: (-len(term), term))
    matcher = re.compile(r"(?<![a-zA-Z0-9_])(?:" + "|".join(map(re.escape, patterns)) + r")(?![a-zA-Z0-9_])")
    return terms, matcher


def extract(documents: Iterable[tuple[str | None, str | None]], code: str | None = None) -> list[str]:
    """Rank terms present in titles or summaries, using taxonomy as a matching vocabulary."""
    terms, matcher = _matcher(code)
    counts: Counter[str] = Counter()
    vocabulary = set(terms.values())
    seen = set()
    for title, summary in documents:
        title = HOSTNAME.sub(" ", _text(title or "")).strip()
        summary = HOSTNAME.sub(" ", CATALOG_FIELDS.sub("", _text(summary or "")))
        # HTML link context can include adjacent numbered sections. Prefer the section containing the anchor title.
        sections = REPORT_HEADING.split(summary)
        if title and len(sections) > 1:
            summary = next((part for part in sections if title.casefold() in part.casefold()), summary)
        if (title, summary) in seen:
            continue
        seen.add((title, summary))
        for content, weight in ((title, 4), (summary, 1)):
            matches = list(matcher.finditer(content.casefold()))
            labels = {terms[hit[0]] for hit in matches}
            counts.update({label: weight * 2 for label in labels})
            identifiers = set()
            for hit in IDENTIFIER.finditer(content):
                term = hit[0]
                if (short_label(term) and term.casefold() not in GENERIC
                        and not any(hit.start() < match.end() and match.start() < hit.end() for match in matches)):
                    identifiers.add(term)
                    if hit["standard"]:
                        vocabulary.add(term)
            counts.update({term: weight for term in identifiers})
    return merge(sorted(counts, key=lambda term: (term not in vocabulary, -counts[term], -len(term), term.casefold())))


def fallback(name: str, code: str | None = None, cluster: str = "") -> list[str]:
    """Use the assigned L3 object and official L2 scope when evidence is sparse."""
    if not code:
        code = next((key for key, row in taxonomy.registry()[1].items() if row["name_zh"] == name), None)
    object_name = cluster.replace("-", " ")
    object_label = _common_terms().get(object_name.casefold(), object_name)
    if short_label(object_label):
        return [object_label]
    return merge((term for term in _industry_terms(code) if short_label(term)), (name,) if short_label(name) else ())

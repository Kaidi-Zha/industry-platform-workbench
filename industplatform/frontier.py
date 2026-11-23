"""Persistent public-source queue scoped to an approved industrial process."""

import hashlib
import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit
from uuid import uuid4

from . import public_web, reference_catalog as catalog

CHANNEL_TERMS = {
    "C1_STANDARD_ORG": ("行业标准 强制规范 标准条款", "standards organization normative specification"),
    "C2_OEM_FAB_SPEC": ("制造企业 代工 工艺能力书", "manufacturer fabrication process capabilities"),
    "C3_TOOL_FORMAT_DOC": ("工程软件 官方手册 文件格式", "engineering tool official manual file format specification"),
    "C4_EQUIP_MAT_VENDOR": ("设备 原材料 原厂规格书", "equipment materials original vendor datasheet"),
    "C5_ASSOC_WHITEPAPER": ("行业协会 白皮书 研究报告", "industry association technical report research dataset"),
    "C6_OPEN_ENG_REPO": ("开源工程 版本历史 基准样板", "open engineering repository release history benchmark"),
    "C7_FAILURE_CASE": ("质量案例 工单 缺陷 故障复盘", "quality case work order defect root cause"),
}
PUBLICATION_CHANNEL = "C5_ASSOC_WHITEPAPER"
# Historical queue items can be resumed under their original label.
LEGACY_CHANNELS = {"git": "C6_OPEN_ENG_REPO", "papers": PUBLICATION_CHANNEL, "official": None,
                   "reviews": "C7_FAILURE_CASE", "business": None, "supply": None, "experts": None}

_TRACKING = frozenset(("fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid",
                       "mc_cid", "mc_eid", "_ga", "_gl"))
_PAPER_HOSTS = ("doi.org", "arxiv.org", "biorxiv.org", "medrxiv.org", "chemrxiv.org", "ssrn.com", "preprints.org",
                "researchsquare.com", "osf.io", "europepmc.org", "pubmed.ncbi.nlm.nih.gov",
                "ieeexplore.ieee.org", "sciencedirect.com", "link.springer.com", "mdpi.com", "onlinelibrary.wiley.com")
_GENERIC = frozenset(("open", "source", "repository", "release", "history", "research", "supplementary", "materials",
                      "dataset", "manufacturer", "official", "example", "sdk", "technical", "report", "review",
                      "defect", "root", "cause", "public", "engineering", "system", "workflow", "export", "supplier",
                      "acceptance", "inspection", "record", "professional", "service", "guide", "sample", "data",
                      "process", "standard", "文件", "资料", "资料库", "数据", "样本", "示例", "开源", "工程",
                      "历史", "官方", "制造", "工业", "规范", "标准", "环节", "工艺", "检验", "结果", "质量"))


def now(delay=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope(db, domain_id, node_id):
    name = db.execute("""
        SELECT d.name, p.title FROM process_nodes p JOIN domains d ON d.id = p.domain_id
        WHERE p.domain_id = ? AND p.id = ?
    """, (domain_id, node_id)).fetchone()
    if not name:
        raise ValueError("请选择当前领域的工序")
    return name


def _open_round(db, scope):
    domain, node = _scope(db, *scope)
    number = db.execute("""
        SELECT coalesce(max(number), 0) + 1 FROM frontier_rounds WHERE domain_id = ? AND node_id = ?
    """, scope).fetchone()[0]
    db.execute("INSERT INTO frontier_rounds(domain_id,node_id,number) VALUES (?,?,?)", (*scope, number))
    suffix = ("标准 文件", "数据 实例", "过程记录 样本", "结果 质量")
    for channel, terms in CHANNEL_TERMS.items():
        for index, expansion in enumerate(("synonym", "long_tail")):
            target = " ".join((domain, node, terms[(index + number) % 2], suffix[(number + index) % len(suffix)]))
            db.execute("""
                INSERT INTO crawl_frontier(domain_id,node_id,id,round_number,channel_id,kind,target,query_expansion,state)
                VALUES (?,?,?,?,?,'query',?,?,'queued')
            """, (*scope, uuid4().hex, number, channel, target, expansion))
    return number


def start(store, domain_id, node_id):
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _scope(db, domain_id, node_id)
        scope = (domain_id, node_id)
        inserted = db.execute("INSERT OR IGNORE INTO frontier_campaigns VALUES (?,?,'running')", scope).rowcount
        stale_exhaustion = (not inserted and db.execute("""SELECT 1 FROM frontier_campaigns
            WHERE (domain_id,node_id)=(?,?) AND status='exhausted'""", scope).fetchone() and not _decide(db, scope)[0])
        if inserted or stale_exhaustion:
            if stale_exhaustion:
                db.execute("UPDATE frontier_campaigns SET status='running' WHERE (domain_id,node_id)=(?,?)", scope)
            _open_round(db, scope)
    return status(store, domain_id, node_id)


def _decide(db, scope):
    work = db.execute("""
        SELECT state, count(*) FROM crawl_frontier WHERE domain_id=? AND node_id=? GROUP BY state
    """, scope).fetchall()
    counts = dict(work)
    waves = db.execute("""
        SELECT number, assessed, novel, pending, closed_at FROM frontier_round_metrics
        WHERE domain_id=? AND node_id=? ORDER BY number DESC LIMIT 3
    """, scope).fetchall()
    enough = db.execute("""
        SELECT channel_id FROM crawl_frontier f JOIN reference_channel_tiers c ON c.channel=f.channel_id
        WHERE domain_id=? AND node_id=? AND state='done' AND kind='query'
        GROUP BY channel_id HAVING count(DISTINCT CASE WHEN query_expansion='synonym' THEN round_number END) >= 2
            AND count(DISTINCT CASE WHEN query_expansion='long_tail' THEN round_number END) >= 2
            AND count(DISTINCT CASE WHEN query_expansion='synonym' THEN target END) >= 2
            AND count(DISTINCT CASE WHEN query_expansion='long_tail' THEN target END) >= 2
    """, scope).fetchall()
    complete = (len(enough) == len(CHANNEL_TERMS) and len(waves) == 3 and
                not any(counts.get(state) for state in ("queued", "leased", "retry", "blocked")) and
                all(closed and not pending and (novel * 100 < assessed * 2 if assessed else novel == 0)
                    for _, assessed, novel, pending, closed in waves))
    return complete, counts, waves


def _advance(db, scope):
    complete, counts, waves = _decide(db, scope)
    if complete:
        db.execute("UPDATE frontier_campaigns SET status='exhausted' WHERE domain_id=? AND node_id=?", scope)
        return
    if not waves:
        return
    latest, _, _, pending, closed = waves[0]
    active = any(counts.get(key) for key in ("queued", "leased", "retry"))
    if counts.get("blocked") and not active:
        db.execute("UPDATE frontier_campaigns SET status='blocked' WHERE domain_id=? AND node_id=?", scope)
        return
    if active or pending or closed:
        return
    db.execute("UPDATE frontier_rounds SET closed_at=? WHERE domain_id=? AND node_id=? AND number=?",
               (now(), *scope, latest))
    if _decide(db, scope)[0]:
        db.execute("UPDATE frontier_campaigns SET status='exhausted' WHERE domain_id=? AND node_id=?", scope)
    else:
        _open_round(db, scope)


def claim(store):
    stamp = now()
    with closing(store.connect()) as db, db:
        db.row_factory = None
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            UPDATE crawl_frontier SET state='retry', lease_token=NULL, lease_expires_at=NULL, next_attempt_at=?
            WHERE state='leased' AND lease_expires_at <= ?
        """, (stamp, stamp))
        row = db.execute("""
            SELECT f.domain_id, f.node_id, f.id, f.round_number, f.channel_id, f.kind, f.target, f.attempts
            FROM crawl_frontier f JOIN frontier_campaigns c
              ON (f.domain_id,f.node_id)=(c.domain_id,c.node_id)
            LEFT JOIN domain_library l ON l.domain_id=f.domain_id
            WHERE coalesce(l.archived,0)=0 AND c.status='running'
            AND NOT EXISTS (SELECT 1 FROM workflow_frontier_nodes s WHERE s.domain_id=f.domain_id AND s.node_id=f.node_id)
            AND (f.state='queued' OR (f.state='retry' AND f.next_attempt_at <= ?))
            ORDER BY f.attempts, f.round_number, CASE f.kind WHEN 'query' THEN 0 ELSE 1 END, f.rowid LIMIT 1
        """, (stamp,)).fetchone()
        if not row:
            return None
        token = uuid4().hex
        db.execute("""
            UPDATE crawl_frontier SET state='leased', attempts=attempts+1, lease_token=?, lease_expires_at=?
            WHERE domain_id=? AND node_id=? AND id=?
        """, (token, now(120), *row[:3]))
        return (*row, token)


def normalized_url(url):
    """Keep functional parameters and the original fetch URL separate from its queue key."""
    parts = urlsplit(public_web.public_url(url))
    parameters = []
    for parameter in parts.query.split("&"):
        key = unquote_plus(parameter.partition("=")[0]).casefold()
        if parameter and not key.startswith("utm_") and key not in _TRACKING:
            parameters.append(parameter)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", "&".join(parameters), ""))


def initialize(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='frontier_url_once_per_domain'").fetchone():
        return
    if "normalized_url" not in {column[1] for column in db.execute("PRAGMA table_info(crawl_frontier)")}:
        db.execute("ALTER TABLE crawl_frontier ADD COLUMN normalized_url TEXT")
    for rowid, target in db.execute("SELECT rowid,target FROM crawl_frontier WHERE kind='url'").fetchall():
        try:
            key = normalized_url(target)
        except ValueError:
            # Earlier versions only accepted public URLs. Retain any manually imported invalid URL as blocked.
            db.execute("""UPDATE crawl_frontier SET state='blocked',lease_token=NULL,lease_expires_at=NULL,
                last_error='原文链接须为公开 HTTPS 地址' WHERE rowid=?""", (rowid,))
            key = target
        db.execute("UPDATE crawl_frontier SET normalized_url=? WHERE rowid=?", (key, rowid))
    _deduplicate(db, "domain_id,")
    db.execute("CREATE UNIQUE INDEX frontier_url_once_per_domain ON crawl_frontier(domain_id,normalized_url)")
    for event in ("INSERT", "UPDATE"):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS frontier_url_key_{event.lower()} BEFORE {event} ON crawl_frontier
            WHEN (NEW.kind='url') <> (NEW.normalized_url IS NOT NULL)
            BEGIN SELECT RAISE(ABORT,'frontier URL key required'); END""")


def _deduplicate(db, partition, condition="", parameters=()):
    db.execute(f"""DELETE FROM crawl_frontier WHERE rowid IN (
        SELECT identity FROM (
            SELECT rowid AS identity, row_number() OVER (PARTITION BY {partition}normalized_url ORDER BY
                CASE WHEN state='done' AND source_id IS NOT NULL THEN 0 WHEN state='leased' THEN 1
                     WHEN state='done' THEN 2 WHEN state='queued' THEN 3 WHEN state='retry' THEN 4 ELSE 5 END,
                rowid) AS rank
            FROM crawl_frontier WHERE normalized_url IS NOT NULL {condition}
        ) WHERE rank>1
    )""", parameters)


def reconcile_domains(db, source, destination):
    _deduplicate(db, "", "AND domain_id IN (?,?)", (source, destination))


def _topic_terms(value):
    citations = set(re.findall(r"\b([a-z][a-z'-]+)[\s_*]+et[\s_*]+al\b", value.casefold()))
    words = re.findall(r"[a-z][a-z0-9]{2,}", value.casefold())
    for chunk in re.findall(r"[\u3400-\u9fff]+", value):
        words.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return set(words) - _GENERIC - citations


def candidates(db, scope, query, channel, search, reclassified=None):
    domain, node = _scope(db, *scope)
    trusted = _topic_terms(domain + " " + node)
    query_terms = _topic_terms(re.sub(r"(?<!\S)-?site:\S+", " ", query, flags=re.I))
    expansions = set().union(*(_topic_terms(" ".join(pair)) for pair in CHANNEL_TERMS.values()))
    terms = trusted | (query_terms - expansions)
    seen = set()
    for item in search["results"]:
        try:
            url = public_web.public_url(item.get("url"))
            key = normalized_url(url)
        except ValueError:
            continue
        parts = urlsplit(url)
        host, path = parts.hostname, unquote(parts.path).casefold()
        publication = (search["scope"] == "publications" or any(
            host == name or host.endswith("." + name) for name in _PAPER_HOSTS)
            or bool(re.search(r"/preprints?(?:/|$)", path)
                    or re.search(r"\bpreprint\b", item.get("title", ""), re.I)
                    or re.search(r"/articles?(?:/|$)", path) and re.search(r"\b(doi|journal)\b", item.get("snippet", ""), re.I)))
        repository = host in {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"} and len(path.strip("/").split("/")) >= 2
        requested_channel = LEGACY_CHANNELS.get(channel, channel)
        actual_channel = (catalog.known_channel(url, item.get("title", "")) or
                          (PUBLICATION_CHANNEL if publication else "C6_OPEN_ENG_REPO" if repository else requested_channel))
        if actual_channel != requested_channel:
            if reclassified is not None:
                reclassified.append({"url": url, "channel": actual_channel})
            continue
        if search["scope"] != "open_web" and requested_channel != PUBLICATION_CHANNEL:
            continue
        match = terms & _topic_terms(" ".join((item.get("title", ""), item.get("snippet", ""), host, path)))
        if not match or (not (match & trusted) and len(match) == 1 and len(next(iter(match))) < 5):
            continue
        if key not in seen:
            seen.add(key)
            yield {**item, "url": url}, key


def enqueue_urls(db, scope, wave, channel, query, search, *, reclassified=None):
    results = []
    for item, key in candidates(db, scope, query, channel, search, reclassified):
        inserted = db.execute("""
            INSERT INTO crawl_frontier(domain_id,node_id,id,round_number,channel_id,kind,target,state,normalized_url)
            VALUES (?,?,?,?,?,'url',?,'queued',?) ON CONFLICT(domain_id,normalized_url) DO NOTHING
        """, (*scope, uuid4().hex, wave, channel, item["url"], key)).rowcount
        item = {**item, "queued": bool(inserted)}
        if not inserted:
            item["queued_channel"] = db.execute("""SELECT channel_id FROM crawl_frontier
                WHERE domain_id=? AND normalized_url=?""", (scope[0], key)).fetchone()[0]
        results.append(item)
    return results


def run_one(store):
    lease = claim(store)
    if not lease:
        return False
    domain, node, identity, wave, channel, kind, target, attempts, token = lease
    error, blocked = "", False
    result, retained = None, None
    try:
        if kind == "query":
            result = public_web.search(target)
            if LEGACY_CHANNELS.get(channel, channel) != PUBLICATION_CHANNEL and result["scope"] != "open_web":
                error, blocked = "该渠道需要开放网页检索并核对原始入口", True
        else:
            final, headers, body = public_web.fetch(target)
            digest = hashlib.sha256(body).hexdigest()
            retained = (public_web.public_url(final), digest, digest)
    except ValueError as exc:
        error = str(exc)[:240]
        blocked = attempts >= 2
    scope = (domain, node)
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            SELECT 1 FROM crawl_frontier WHERE domain_id=? AND node_id=? AND id=? AND lease_token=?
                AND state='leased' AND lease_expires_at>?
        """, (*scope, identity, token, now())).fetchone()
        if not row:
            return True
        if result and not error:
            enqueue_urls(db, scope, wave, channel, target, result)
        source_id = None
        if retained:
            url, version, digest = retained
            source_id = hashlib.sha256((url + "\n" + version).encode()).hexdigest()
            db.execute("""
                INSERT OR IGNORE INTO raw_sources(domain_id,id,canonical_url,version,sha256,status,retrieved_at)
                VALUES (?,?,?,?,?,'parsed',?)
            """, (domain, source_id, url, version, digest, now()))
            db.execute("""
                INSERT OR IGNORE INTO frontier_candidates(domain_id,node_id,round_number,source_id,assessment)
                VALUES (?,?,?,?,'pending')
            """, (*scope, wave, source_id))
        state = ("blocked" if blocked else "retry") if error else "done"
        db.execute("""
            UPDATE crawl_frontier SET state=?, lease_token=NULL, lease_expires_at=NULL, next_attempt_at=?,
                last_error=?, source_id=? WHERE domain_id=? AND node_id=? AND id=?
        """, (state, now(15 * (attempts + 1)) if state == "retry" else None, error or None, source_id, *scope, identity))
        _advance(db, scope)
    return True


def retry(store, domain_id, node_id, identity):
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute("""
            UPDATE crawl_frontier SET state='queued', attempts=0, next_attempt_at=NULL, last_error=NULL
            WHERE domain_id=? AND node_id=? AND id=? AND state IN ('blocked','retry')
        """, (domain_id, node_id, identity)).rowcount
        if not updated:
            raise ValueError("这条采集任务当前无法重试")
        db.execute("UPDATE frontier_campaigns SET status='running' WHERE domain_id=? AND node_id=?", (domain_id, node_id))


def assess(store, domain, node, round_number, source_id, *, note, reference_id=None):
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        candidate = db.execute("""
            SELECT s.sha256, s.canonical_url FROM frontier_candidates f JOIN raw_sources s
                ON (f.domain_id,f.source_id)=(s.domain_id,s.id)
            WHERE f.domain_id=? AND f.node_id=? AND f.round_number=? AND f.source_id=? AND f.assessment='pending'
        """, (domain, node, round_number, source_id)).fetchone()
        if not candidate:
            raise ValueError("该候选来源没有待处理的核验记录")
        fingerprint = None
        if reference_id:
            reference = db.execute("""
                SELECT r.body FROM evidence_references r JOIN domains d ON d.name=r.domain WHERE d.id=? AND r.id=?
            """, (domain, reference_id)).fetchone()
            proof = json.loads(reference[0]).get("verification", {}) if reference else {}
            if proof.get("sha256") != candidate[0]:
                raise ValueError("Reference 需核验当前领域中同一版本的原文")
            fingerprint = candidate[0]
            db.execute("INSERT OR IGNORE INTO entity_fingerprints VALUES (?,?,?,?,?)",
                       (domain, node, fingerprint, source_id, round_number))
        db.execute("""
            UPDATE frontier_candidates SET assessment=?, fingerprint=?
            WHERE domain_id=? AND node_id=? AND round_number=? AND source_id=?
        """, ("eligible" if fingerprint else "ineligible", fingerprint, domain, node, round_number, source_id))
        db.execute("INSERT INTO frontier_reviews VALUES (?,?,?,?,?)", (domain, node, round_number, source_id, note))
        _advance(db, (domain, node))


def status(store, domain_id, node_id):
    scope = (domain_id, node_id)
    with closing(store.connect()) as db:
        _scope(db, *scope)
        row = db.execute("SELECT status FROM frontier_campaigns WHERE domain_id=? AND node_id=?", scope).fetchone()
        complete, counts, waves = _decide(db, scope)
        return {"status": row[0] if row else "idle", "exhausted": complete, "counts": counts,
                "rounds": [{"number": n, "checked": c, "novel": v, "pending": p, "closed_at": at}
                           for n, c, v, p, at in waves]}

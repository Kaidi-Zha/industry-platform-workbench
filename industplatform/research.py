"""Persistent evidence and coverage checkpoints for public collection tasks."""

import hashlib
import json
import re
from contextlib import closing

from . import collection, public_web


LEVELS = {"document": 1, "sample": 2, "acceptance": 3}
RELATION_TYPES = {"supplier_requirement", "actual_delivery", "collaboration", "demonstration", "compatibility"}
MAX_SEARCHES = 12
MAX_READS = 12
MAX_ITEMS = 24
CNC_TARGETS = (
    ("design", "S1", "带版本的设计图样与公差要求", "sample"),
    ("program", "S4", "关联零件版本的加工程序", "sample"),
    ("measurement", "S5", "CMM 测量记录与特性映射", "sample"),
    ("fair", "S5", "正式首件检验与放行结果", "acceptance"),
    ("handoff", "S5/S6", "供方交付与采购方接收条件", "document"),
)


def stage_codes(value):
    value = value.upper()
    codes = set(re.findall(r"(?<![A-Z0-9])S[1-8](?![A-Z0-9])", value))
    for start, stop in re.findall(r"S([1-8])\s*[-–—~至]\s*S([1-8])", value):
        codes.update(f"S{number}" for number in range(int(start), int(stop) + 1))
    return codes


def initialize(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS research_checkpoints (
            id TEXT PRIMARY KEY,
            body TEXT NOT NULL
        )
    """)


def checkpoint_id(conversation_id, scope):
    return hashlib.sha256((conversation_id + json.dumps(scope, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()


class Research:
    def __init__(self, store, run):
        self.store = store
        self.run = run
        self.scope = run["plan"]
        self.stages = stage_codes(self.scope["stages"])
        self.id = checkpoint_id(run["conversation_id"], self.scope)
        with closing(store.connect()) as connection:
            row = connection.execute("SELECT body FROM research_checkpoints WHERE id = ?", (self.id,)).fetchone()
        if row:
            self.state = json.loads(row[0])
        else:
            context = " ".join(self.scope[key] for key in ("domain", "scenario", "stages", "deliverables")).lower()
            defaults = CNC_TARGETS if "首件" in context or re.search(r"\b(fai|first.article)\b", context) else ()
            self.state = {
                "targets": {key: {"stage": stage, "deliverable": item, "level": level, "basis": "项目待查清单"}
                            for key, stage, item, level in defaults
                            if not self.stages or self.stages & stage_codes(stage)},
                "sources": {}, "queries": {}, "evidence": {}, "attempts": {}, "rounds": [],
                "current": {"queries": [], "new": [], "checks": {}},
            }
        self.attempts = self.state["attempts"].setdefault(run["id"], {"search": 0, "read": 0})

    def persist(self):
        with closing(self.store.connect()) as connection, connection:
            self._save(connection)

    def _record_source(self, url, status, *, connection, version="", stamp=None):
        canonical = public_web.public_url(url)
        identity = hashlib.sha256((canonical + "\n" + version).encode()).hexdigest()
        connection.execute("""
            INSERT INTO raw_sources(domain_id,id,canonical_url,version,sha256,status,retrieved_at)
            SELECT id,?,?,?,?,?,? FROM domains WHERE name=?
            ON CONFLICT(domain_id,id) DO UPDATE SET status=excluded.status,retrieved_at=excluded.retrieved_at
            WHERE raw_sources.status <> 'verified' AND excluded.status <> 'discovered'
        """, (identity, canonical, version, version or None, status, stamp, self.scope["domain"]))
        if version:
            connection.execute("""
                DELETE FROM raw_sources WHERE domain_id=(SELECT id FROM domains WHERE name=?)
                  AND canonical_url=? AND version='' AND status='discovered'
            """, (self.scope["domain"], canonical))

    def _save(self, connection):
        connection.execute("""
            INSERT INTO research_checkpoints(id, body) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET body = excluded.body
        """, (self.id, json.dumps(self.state, ensure_ascii=False)))

    def status(self):
        gaps = []
        for key, target in self.state["targets"].items():
            evidence = self.state["evidence"].get(key, {})
            eligible = [record for record in evidence.values() if LEVELS[record["level"]] >= LEVELS[target["level"]]]
            count = len({record["url"] for record in eligible})
            gaps.append({"id": key, **target, "eligible_sources": count, "reference_count": len(evidence),
                         "covered": count >= target.get("expected_count", 1)})
        rounds = self.state["rounds"]
        verified = {item for refs in self.state["evidence"].values() for item in refs}
        pending = any(not row["covered"] for row in gaps)
        zero = len(rounds) >= 2 and all(row["new"] == 0 for row in rounds[-2:])
        # Only general-web search can support the general-web saturation claim.
        current = self.state["current"]
        done = (not current["queries"] and not current["new"] and not current.get("checks")
                and bool(gaps) and zero
                and all(row["open_web_queries"] and not row.get("unresolved_sources") for row in rounds[-2:]))
        return {"items": gaps, "rounds": rounds[-6:], "round_count": len(rounds), "verified_references": len(verified),
                "sources": len(self.state["sources"]), "backend": public_web.backend(), "complete": done,
                **({"benchmark_source": self.state["benchmark_source"]} if "benchmark_source" in self.state else {}),
                "pending_queries": [self.state["queries"][key]["query"]
                                    for key in self.state["current"]["queries"]],
                "reason": ("targets_covered" if not pending else "alternative_paths_exhausted") if done else
                ("limited_index" if public_web.backend() == "crossref" else "coverage_in_progress")}

    def recent_locators(self):
        return [{"source_id": row["id"], "url": row["url"],
                 "locators": [item["locator"] for item in row["passages"][:4]]}
                for row in list(self.state["sources"].values())[-4:]]

    def apply(self, name, args):
        if name == "authority_sources":
            return {"entries": public_web.ENTRIES, "search_backend": public_web.backend(),
                    "coverage": self.status()["items"]}
        if name == "coverage_status":
            return self.status()
        if name == "web_search":
            return self.search(args)
        if name == "read_public_source":
            return self.read(args)
        if name == "record_evidence":
            return self.record(args)
        if name == "set_coverage_target":
            return self.target(args)
        if name == "select_benchmark_source":
            return self.select_benchmark_source(args)
        return self.finish_round(args)

    def select_benchmark_source(self, args):
        from .nist_benchmark import DISCOVERY_URLS

        identity = args["reference_id"]
        if not any(identity in refs for refs in self.state["evidence"].values()):
            raise ValueError("须选择本次采集计划已找到并核验入库的 Reference")
        proof = collection.reference(self.store, identity)
        context = " ".join(self.scope[key] for key in ("domain", "scenario", "stages", "deliverables")).casefold()
        relevant = ("首件" in context or "来料检验" in context
                    or bool(re.search(r"\b(cnc|cmm|fai|qif|first.article)\b", context)))
        if (not proof or not proof["verification"].get("search_queries") or proof["url"] not in DISCOVERY_URLS or not relevant
                or (self.stages and "S5" not in self.stages)):
            raise ValueError("该来源与当前已实现的 CNC／CMM 公开检验取样入口不匹配")
        reason = args["scope_match"].strip()
        if not reason:
            raise ValueError("请说明来源与已确认工序、地区和时间范围的匹配依据")
        selection = {"reference_id": identity, "verifier": args["verifier"], "scope_match": reason}
        self.state["benchmark_source"] = selection
        self.persist()
        return selection

    def track_sample(self, record):
        target = "measurement"
        self.state["targets"].setdefault(target, {
            "stage": "S5", "deliverable": "CMM 测量记录与特性映射", "level": "sample", "basis": "项目待查清单",
        })
        self.state["evidence"].setdefault(target, {})[record["id"]] = {"url": record["url"], "level": "sample"}
        self.persist()

    def search(self, args):
        query = " ".join(args["query"].split())
        key = public_web.backend() + "|" + query.casefold()
        if key in self.state["queries"]:
            return {**self.state["queries"][key], "already_searched": True}
        if self.attempts["search"] >= MAX_SEARCHES:
            raise ValueError("已达到检索预算，保留缺口供后续续查")
        self.attempts["search"] += 1
        self.persist()
        result = public_web.search(query, args.get("limit", 5))
        self.state["queries"][key] = result
        self.state["current"]["queries"].append(key)
        with closing(self.store.connect()) as db, db:
            for item in result["results"]:
                try:
                    self._record_source(item["url"], "discovered", connection=db)
                except ValueError:
                    continue
            self._save(db)
        return result

    def read(self, args):
        url = public_web.public_url(args["url"])
        if self.attempts["read"] >= MAX_READS:
            raise ValueError("已达到原文读取预算，保留未查入口")
        self.attempts["read"] += 1
        self.persist()
        checks = self.state["current"].setdefault("checks", {})
        try:
            final, headers, body = public_web.fetch(url)
            exclusive = public_web.extract(final, headers, body, args.get("focus", ""), page=args.get("page"))
        except ValueError as exc:
            checks[url] = str(exc)
            self.persist()
            raise
        checks[url] = "read"
        matches = [search["query"] for search in self.state["queries"].values()
                   if any(row["url"] in (url, final) for row in search["results"])]
        identifier = hashlib.sha256((final + exclusive["sha256"]).encode()).hexdigest()[:24]
        previous = self.state["sources"].get(identifier, {})
        source = {**exclusive, "id": identifier, "requested_url": url,
                  "search_queries": list(dict.fromkeys([*previous.get("search_queries", []), *matches]))}
        # Different focus terms can locate different passages in the same document.
        blocks = {row["locator"]: row for row in [*previous.get("passages", []), *source["passages"]]}
        self.state["sources"][identifier] = {**source, "passages": list(blocks.values())[-32:]}
        with closing(self.store.connect()) as db, db:
            self._record_source(source["url"], "parsed", version=source["sha256"],
                                stamp=source["retrieved_at"], connection=db)
            self._save(db)
        return source

    def record(self, args):
        source = self.state["sources"].get(args["source_id"])
        target = args["target_id"]
        if source is None or target not in self.state["targets"]:
            raise ValueError("请先读取原文并选择已登记的覆盖目标")
        evidence = next((row for row in source["passages"] if row["locator"] == args["locator"]), None)
        level = args["level"]
        if not evidence:
            raise ValueError("请复制 read_public_source 返回的完整 locator")
        text = evidence["text"]
        if "quote" not in args and level != "acceptance" and not args.get("relation_type"):
            quote = text if len(text) <= 240 else text[:240].rsplit(maxsplit=1)[0]
        else:
            quote = " ".join(args.get("quote", "").split())
        if len(quote) < 12 or quote.casefold() not in text.casefold():
            raise ValueError("证据短引须逐字匹配指定原文片段")
        if level == "sample" and not source["structured"]:
            raise ValueError("样本级需读取可解析的结构化原始文件")
        if level == "acceptance" and any(
            not args.get(key, "").strip() or args[key].strip().casefold() not in quote.casefold()
            for key in ("record_id", "outcome")
        ):
            raise ValueError("验收级须在原文短引中给出实际记录标识和结果")
        if args["permission"] == "stated":
            license_quote = " ".join(args.get("license_quote", "").split())
            if len(license_quote) < 8 or not any(license_quote.casefold() in row["text"].casefold() for row in source["passages"]):
                raise ValueError("已声明的许可须提供可核对的原文短引")
        relation_type = args.get("relation_type", "")
        relation_fields = ("from_party", "to_party", "artifact", "applicability")
        if (relation_type and not all(args.get(key, "").strip() for key in relation_fields)
                or not relation_type and any(args.get(key) for key in relation_fields)):
            raise ValueError("关系须同时注明类型、双方、交付物和适用范围")
        relation = {"type": relation_type, **{key: args[key] for key in relation_fields}} if relation_type else None
        if relation_type == "actual_delivery" and any(
            args[key].strip().casefold() not in quote.casefold() for key in ("from_party", "to_party")
        ):
            raise ValueError("实际交付关系须在同一原文短引中能定位双方")
        verification = {key: source[key] for key in (
            "sha256", "bytes", "mime", "retrieved_at", "structured", "authority", "search_queries",
        )}
        verification.update(level=level, quote=quote, permission=args["permission"],
                            license_quote=args.get("license_quote", ""), target=target,
                            record_id=args.get("record_id", ""), outcome=args.get("outcome", ""))
        item = {key: args[key] for key in ("title", "publisher", "version", "kind", "claim") if key in args}
        item.update(domain=self.scope["domain"], scenario=self.scope["scenario"], url=source["url"],
                    stage=self.state["targets"][target]["stage"], locator=args["locator"])
        def checkpoint(connection, saved):
            self._record_source(source["url"], "verified", version=source["sha256"],
                                stamp=source["retrieved_at"], connection=connection)
            records = self.state["evidence"].setdefault(target, {})
            if saved["id"] in records:
                return
            observation = {"url": saved["url"], "level": saved["verification"]["level"]}
            if not any(prior["url"] == observation["url"] and LEVELS[prior["level"]] >= LEVELS[observation["level"]]
                       for prior in records.values()):
                self.state["current"]["new"].append(saved["id"])
            records[saved["id"]] = observation
            self._save(connection)

        saved = collection.save_reference(self.store, item, self.run["id"], verification=verification,
                                          relation=relation, checkpoint=checkpoint)
        return {"id": saved["id"], "title": saved["title"], "target": target, "level": saved["verification"]["level"],
                "outside_entries": not bool(source["authority"])}

    def target(self, args):
        key = args["target_id"]
        prior = self.state["targets"].get(key)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", key) or not args["deliverable"].strip():
            raise ValueError("目标 ID 或交付物无效")
        if not args["stage"].strip() or (self.stages and not stage_codes(args["stage"]) & self.stages):
            raise ValueError("请选择采集计划内的工序")
        if (not prior and len(self.state["targets"]) >= MAX_ITEMS) or (prior and prior["level"] != args["level"]):
            raise ValueError("目标数量达到上限，或已有目标证据级别不可改写")
        basis = args.get("basis_ref", "")
        proof = collection.reference(self.store, basis) if basis else None
        if basis and (not proof or not proof.get("verification") or proof["domain"] != self.scope["domain"]):
            raise ValueError("覆盖依据须为本领域已核验 Reference")
        expected = args.get("expected_count")
        explanation = args.get("count_basis", "")
        if expected and (not proof or not explanation.strip()
                         or explanation.casefold() not in proof["verification"]["quote"].casefold()
                         or not re.search(rf"\b{expected}\b", explanation)):
            raise ValueError("预期数量须可从已核验短引中精确定位")
        target = {"stage": args["stage"], "deliverable": args["deliverable"], "level": args["level"],
                  "basis": basis or "项目待查清单"}
        if expected:
            target.update(expected_count=expected, count_basis=explanation)
        self.state["targets"][key] = target
        self.persist()
        return target

    def finish_round(self, args):
        current = self.state["current"]
        queries = current["queries"]
        if not queries:
            raise ValueError("本轮需要至少一次新的成功搜索，复用已有结果不能计为补查")
        if not args["path"].strip() or not args["next_step"].strip():
            raise ValueError("请写明本轮检索路径和下一步")
        if any(row["path"].casefold() == args["path"].strip().casefold() for row in self.state["rounds"]):
            raise ValueError("请改用另一条检索路径")
        checks = current.get("checks", {})
        screened = {source["requested_url"] for source in self.state["sources"].values()} | set(checks)
        candidates = {row["url"] for key in queries for row in self.state["queries"][key]["results"]}
        self.state["rounds"].append({
            "path": args["path"].strip(), "queries": [self.state["queries"][key]["query"] for key in queries],
            "open_web_queries": sum(self.state["queries"][key]["scope"] == "open_web" for key in queries),
            "new": len(set(current["new"])), "next_step": args["next_step"].strip(),
            "unresolved_sources": len(candidates - screened) + sum(result != "read" for result in checks.values()),
            "remaining": [row["id"] for row in self.status()["items"] if not row["covered"]],
        })
        self.state["current"] = {"queries": [], "new": [], "checks": {}}
        self.persist()
        return self.status()

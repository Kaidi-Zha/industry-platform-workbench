"""Map published metadata to the workbench's domain and engineering records."""

import json
import re
from collections import defaultdict

from . import engineering_quality as quality, process_topology as topology, reference_catalog as catalog
from .shared_metadata import canonical_key

SOURCE_STATES = ("discovered", "parsed", "verified", "rejected", "blocked")
RELATION_KINDS = ("actual_delivery", "supplier_requirement", "collaboration", "demonstration", "compatibility")
TASK_FIELDS = (
    "description", "constraints", "expected_output", "industrial_context", "boundary_conditions",
    "edge_cases_and_rejection", "downstream_handoff", "deliverables", "value_assessment", "input_artifacts",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _list(value):
    return value if isinstance(value, list) else []


def _object(value):
    return value if isinstance(value, dict) else {}


def _url(value):
    if value := _text(value):
        try:
            return canonical_key("sources", value)
        except ValueError:
            return ""
    return ""


def _digest(value):
    value = _text(value).lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def _node_id(value):
    return value if isinstance(value, str) and re.fullmatch(catalog.NODE_ID_PATTERN, value) and (
        value != catalog.UNASSIGNED) else ""


def _reaches(edges, start, target):
    frontier = [start]
    seen = set()
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            frontier.extend(edges[current])
    return False


def _admitted_document(row):
    payload = row["payload"]
    publisher = _object(payload.get("publisher_info"))
    authority = publisher.get("authority")
    permission = _text(payload.get("permission")) or "unknown"
    release = _object(payload.get("version_or_date"))
    scope = _object(payload.get("applicable_scope"))
    if (authority not in quality.AUTHORITIES or permission not in ("stated", "unknown")
            or not all(map(_text, (publisher.get("name"), publisher.get("qualification"), payload.get("summary_zh"),
                                  scope.get("applies_to"))))
            or not any(map(_text, (release.get("version"), release.get("published_on"))))
            or not any(map(_text, _list(scope.get("preconditions"))))
            or not any(map(_text, _list(payload.get("limitations"))))):
        return None
    clauses = [clause for clause in _list(payload.get("extracted_clauses")) if isinstance(clause, dict)
               and clause.get("kind") in quality.CLAUSE_KINDS
               and all(_text(clause.get(key)) for key in ("id", "locator", "requirement", "check"))]
    if not clauses:
        return None
    fields = {key: payload[key] for key in quality.REFERENCE_FIELDS if key in payload}
    return {**fields, "title": row["title"], "extracted_clauses": clauses}


class _DomainAssets:
    def __init__(self, db, writer, domain):
        self.db, self.writer, self.domain = db, writer, domain
        self.aliases = {}
        self.titles = {}
        self.sources = defaultdict(list)
        self.references = defaultdict(list)
        self.current_references = set()

    def keys(self, **fields):
        return {"domain_id": self.domain, **fields}

    def process(self, value):
        value = _text(value)
        return self.aliases.get(value) or self.titles.get(topology.title_key(value))

    def processes(self, records):
        existing = {identity: (title, position) for identity, title, position in self.db.execute(
            "SELECT id,title,position FROM process_nodes WHERE domain_id=?", (self.domain,))}
        initially = set(existing)
        by_title = {topology.title_key(title): identity for identity, (title, _) in existing.items()}
        positions = {position for _, position in existing.values()}
        last = max(positions, default=0)
        selected = {}
        for row in sorted(records, key=lambda item: (
                item["payload"]["position"] if type(item["payload"].get("position")) is int else last + 1,
                item["title"], item["canonical_id"])):
            payload, title = row["payload"], row["title"]
            remote_id = _node_id(payload.get("process_node_id"))
            stable_id = remote_id or f"shared-{row['canonical_id']}"
            identity = stable_id if stable_id in existing else (
                by_title.get(topology.title_key(title)) or stable_id)
            if identity in existing:
                position = existing[identity][1]
            else:
                wanted = payload.get("position")
                position = wanted if type(wanted) is int and 0 < wanted <= 2**31 - 1 and wanted not in positions else last + 1
            if identity in selected:
                self.aliases[stable_id] = identity
                self.titles[topology.title_key(title)] = identity
                continue
            roles = list(dict.fromkeys(role for role in map(_text, _list(payload.get("occupations"))) if role))
            try:
                group = topology.group_name(payload.get("flow_group", ""))
            except ValueError:
                group = ""
            if not self.writer.put("process_nodes", self.keys(id=identity), {
                "title": title, "position": position, "occupations_json": _json(roles),
                "flow_group": group, "lifecycle_stages_json": _json(row["lifecycle_stages"]),
            }):
                continue
            positions.add(position)
            last = max(last, position)
            existing.setdefault(identity, (title, position))
            by_title[topology.title_key(title)] = identity
            self.aliases[stable_id] = identity
            self.titles[topology.title_key(title)] = identity
            selected[identity] = row
        for identity, (title, _) in existing.items():
            if identity not in selected and self.writer.owns("process_nodes", self.keys(id=identity)):
                continue
            self.aliases.setdefault(identity, identity)
            self.titles.setdefault(topology.title_key(title), identity)

        desired = set()
        for identity, row in selected.items():
            payload = row["payload"]
            for side in ("upstream", "downstream"):
                values = _list(payload.get(side + "_ids")) + _list(payload.get(side))
                for value in values:
                    resolved = self.process(value)
                    if resolved and resolved != identity:
                        desired.add((resolved, identity) if side == "upstream" else (identity, resolved))
        graph = defaultdict(set)
        existing_edges = set()
        for upstream, downstream in self.db.execute("""SELECT upstream_id,downstream_id FROM process_edges
                WHERE domain_id=?""", (self.domain,)):
            keys = self.keys(upstream_id=upstream, downstream_id=downstream)
            if (upstream, downstream) in desired or not self.writer.owns("process_edges", keys):
                graph[upstream].add(downstream)
                existing_edges.add((upstream, downstream))
        for upstream, downstream in sorted(desired):
            edge = (upstream, downstream)
            if edge not in existing_edges and _reaches(graph, downstream, upstream):
                continue
            graph[upstream].add(downstream)
            self.writer.put("process_edges", self.keys(upstream_id=upstream, downstream_id=downstream), {})
        for identity in selected.keys() - initially:
            self.writer.put("process_nodes", self.keys(id=identity), {"is_stale": 0, "stale_reason": ""})

    def read_sources(self):
        for identity, url, version, sha in self.db.execute("""SELECT id,canonical_url,version,sha256
                FROM raw_sources WHERE domain_id=?
                ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'parsed' THEN 1 WHEN 'discovered' THEN 2 ELSE 3 END,id""",
                (self.domain,)):
            if url := _url(url):
                self.sources[url].append((identity, version, sha))

    def source(self, row, *, literature=False, matching_document=None, exclude=None):
        payload = row["payload"]
        url = row["canonical_key"]
        sha = _digest(payload.get("sha256"))
        proof = _digest(matching_document["payload"].get("sha256")) if matching_document else None
        sha = sha or proof
        version = _text(payload.get("source_version"))
        candidates = self.sources[url]
        exact = next((item for item in candidates if item[0] != exclude and sha and item[2] == sha), None)
        same_version = next((item for item in candidates if item[0] != exclude and not sha and
                             (not version or item[1] == version)), None)
        managed_url = next((item for item in candidates if item[0] != exclude and not item[2]
                            and self.writer.owns("raw_sources", self.keys(id=item[0]))), None)
        chosen = exact or same_version or managed_url
        if chosen:
            identity = chosen[0]
            version = version or chosen[1]
        else:
            identity = f"shared-{row['canonical_id']}"
            if sha and any(candidate[0] == identity for candidate in candidates):
                version = "sha256:" + sha
                identity += "-" + sha[:16]
        if chosen and not self.writer.owns("raw_sources", self.keys(id=identity)):
            return identity
        if any(candidate[0] != identity and candidate[1] == version for candidate in candidates):
            version = "sha256:" + sha if sha else "shared:" + row["canonical_id"]
        # Only preserve an explicitly recorded fetch date. A publication date is not a fetch time.
        state = payload.get("verification_status")
        if state not in SOURCE_STATES:
            state = "parsed" if sha else "discovered"
        if literature or state != "rejected" and matching_document and (not proof or proof == sha):
            state = "verified"
        values = {"canonical_url": url, "sha256": sha, "version": version, "status": state,
                  "retrieved_at": _text(payload.get("retrieved_at")) or None, "import_metadata_json": "{}"}
        if not literature:
            values["import_metadata_json"] = _json({
                "batch_id": row["task_id"], "title": row["title"], "summary": _text(payload.get("summary_zh")),
                "object_cluster": row["object_cluster"], "lifecycle_stages": row["lifecycle_stages"],
                "l2_code": row["l2_code"], "origin": "shared",
            })
        if not self.writer.put("raw_sources", self.keys(id=identity), values):
            return None
        candidates[:] = [item for item in candidates if item[0] != identity]
        candidates.append((identity, version, sha))
        return identity

    def read_references(self):
        for identity, url, sha, profile in self.db.execute(f"""SELECT v.id,s.canonical_url,s.sha256,
                v.quality_json FROM verified_references v
                JOIN raw_sources s ON (s.domain_id,s.id)=(v.domain_id,v.source_id)
                WHERE v.domain_id=? AND v.permission<>'restricted' AND {quality.READY_REF}
                ORDER BY v.verified_at DESC,v.id""", (self.domain,)):
            if url := _url(url):
                clauses = [clause for clause in json.loads(profile).get("extracted_clauses", []) if isinstance(clause, dict)]
                self.references[url].append((identity, sha, clauses))

    def document(self, row, document, source):
        payload, url = row["payload"], row["canonical_key"]
        sha = _digest(payload.get("sha256"))
        previous = next((entry for entry in self.references[url] if (
            entry[1] == sha or not sha) and not self.writer.owns("verified_references", self.keys(id=entry[0]))), None)
        identity = previous[0] if previous else f"shared-{row['canonical_id']}"
        requested = _list(payload.get("process_node_ids"))
        nodes = list(dict.fromkeys(node for value in requested if (node := self.process(value))))
        if requested != [catalog.UNASSIGNED] and not nodes and (node := self.process(payload.get("process"))):
            nodes.append(node)
        publisher = document["publisher_info"]
        authority = publisher["authority"] if publisher["authority"] != "open_record" else "research"
        clause = document["extracted_clauses"][0]
        if not self.writer.put("verified_references", self.keys(id=identity), {
            "source_id": source, "node_id": nodes[0] if nodes else None, "publisher": publisher["name"],
            "title": row["title"], "authority": authority, "authority_tier": payload["authority_tier"],
            "source_channel": payload["source_channel"], "locator": clause["locator"], "finding": clause["requirement"],
            "verified_at": _text(payload.get("verified_at")) or row["updated_at"],
            "permission": _text(payload.get("permission")) or "unknown", "excerpt": _text(clause.get("quote")),
            "summary_zh": _text(document.get("summary_zh")), "quality_json": _json(document),
            "object_cluster": row["object_cluster"], "lifecycle_stages_json": _json(row["lifecycle_stages"]),
        }):
            return None
        for node in nodes:
            self.writer.put("verified_reference_nodes", self.keys(reference_id=identity, node_id=node), {})
        if not previous:
            self.references[url][:] = [entry for entry in self.references[url] if entry[0] != identity]
            self.references[url].insert(0, (identity, sha, document["extracted_clauses"]))
        return identity

    def evidence(self, url, locator="", clause_id=""):
        for identity, _, clauses in self.references[_url(url)]:
            if self.writer.owns("verified_references", self.keys(id=identity)) and identity not in self.current_references:
                continue
            for clause in clauses:
                if clause_id and clause.get("id") == clause_id or not clause_id and (
                        not locator or clause.get("locator") == locator):
                    return identity, clause
        return None

    def entity(self, row):
        payload = row["payload"]
        node = self.process(payload.get("process_node_id")) or self.process(payload.get("process"))
        if not node:
            return
        clause_id = _text(payload.get("clause_id"))
        if not (evidence := self.evidence(payload.get("evidence_url"), _text(payload.get("locator")), clause_id)):
            return
        reference, clause = evidence
        name, role = _text(payload.get("name")), payload.get("role")
        if name and role in ("upstream", "manufacturer", "downstream"):
            self.writer.put("process_participants", self.keys(node_id=node, name=name, role=role),
                            {"reference_id": reference})
        relation = payload.get("relationship")
        sender, recipient, deliverable = (_text(payload.get(field)) for field in ("sender", "recipient", "deliverable"))
        if (relation in RELATION_KINDS and sender and recipient and deliverable and sender.casefold() != recipient.casefold()
                and clause_id and clause.get("kind") in ("process", "acceptance", "failure_case")):
            self.writer.put("verified_reference_nodes", self.keys(reference_id=reference, node_id=node), {})
            identity = self.db.execute("""SELECT id FROM process_relations WHERE domain_id=? AND node_id=?
                AND sender=? AND recipient=? AND deliverable=? AND kind=? LIMIT 1""",
                (self.domain, node, sender, recipient, deliverable, relation)).fetchone()
            self.writer.put("process_relations", self.keys(node_id=node, id=identity[0] if identity else f"shared-{row['canonical_id']}"), {
                "sender": sender, "recipient": recipient, "deliverable": deliverable, "kind": relation,
                "evidence_id": reference, "evidence_clause_id": clause_id, "state": "verified",
            })

    def task(self, row):
        payload, title = row["payload"], row["title"]
        node = self.process(payload.get("process_node_id")) or self.process(payload.get("process"))
        if not node or not _text(payload.get("description")):
            return
        existing = self.db.execute("""SELECT id FROM benchmark_tasks WHERE domain_id=? AND node_id=?
            AND title=? COLLATE NOCASE ORDER BY rowid LIMIT 1""", (self.domain, node, title)).fetchone()
        identity = existing[0] if existing else f"shared-{row['canonical_id']}"
        definition = {field: payload[field] for field in TASK_FIELDS if field in payload}
        handoff = _object(payload.get("downstream_handoff"))
        if "downstream_handoff" in definition:
            definition["downstream_handoff"] = handoff
        citations = []
        for citation in _list(payload.get("citations")):
            citation = _object(citation)
            if citation.get("permission") == "restricted":
                continue
            evidence = self.evidence(citation.get("url"), _text(citation.get("locator")), _text(citation.get("clause_id")))
            if evidence:
                reference, clause = evidence
                citations.append({"reference_id": reference, "clause_id": clause["id"], "applies_to": clause["requirement"]})
        if citations:
            definition["reference_citations"] = citations
        complexity = payload.get("complexity")
        if not self.writer.put("benchmark_tasks", self.keys(id=identity), {
            "node_id": node, "title": title, "definition_json": _json(definition), "state": "draft",
            "downstream_id": self.process(handoff.get("process")), "quality_approved": 0,
            "complexity": complexity if complexity in ("L1", "L2", "L3", "L4") else None,
        }):
            return
        if self.writer.owns("benchmark_tasks", self.keys(id=identity)):
            for reference in {item["reference_id"] for item in citations}:
                self.writer.put("task_references", self.keys(task_id=identity, reference_id=reference), {})

    def populate(self, records):
        self.processes(records["processes"])
        self.read_sources()
        documents = {row["canonical_key"]: (row, document) for row in records["literature"]
                     if (document := _admitted_document(row))}
        sources = {}
        for row in records["sources"]:
            url = row["canonical_key"]
            sources[url] = self.source(row, matching_document=documents.get(url, (None,))[0])
        self.read_references()
        for url, (row, document) in documents.items():
            source = sources.get(url)
            sha = _digest(row["payload"].get("sha256"))
            if not source or sha and not any(item[0] == source and item[2] == sha for item in self.sources[url]):
                source = self.source(row, literature=True, exclude=source)
            if source and (identity := self.document(row, document, source)):
                self.current_references.add(identity)
        for row in records["literature"]:
            if row["canonical_key"] not in documents and row["canonical_key"] not in sources and (
                    row["payload"].get("permission") != "restricted"):
                # Legacy references without usable clauses remain visible as candidate sources.
                self.source(row)
        for row in records["entities"]:
            self.entity(row)
        for row in records["tasks"]:
            self.task(row)


def project(db, writer, records):
    domains = defaultdict(lambda: defaultdict(list))
    for row in records:
        domains[row["l2_code"]][row["kind"]].append(row)
    for code, items in domains.items():
        if (domain := writer.domain(code)) is not None:
            _DomainAssets(db, writer, domain).populate(items)

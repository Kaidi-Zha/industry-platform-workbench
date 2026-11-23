"""Reconcile shared metadata with the tables used by the local workbench."""

import hashlib
import json
import sqlite3
from collections import defaultdict

from . import domain_keywords, domain_libraries, import_library, shared_metadata, taxonomy

VERSION = 1

# Remove children before parents. Local rows and edits always win a collision.
ROW_KEYS = {
    "task_references": ("domain_id", "task_id", "reference_id"),
    "benchmark_tasks": ("domain_id", "id"),
    "process_relations": ("domain_id", "node_id", "id"),
    "process_participants": ("domain_id", "node_id", "name", "role"),
    "verified_reference_nodes": ("domain_id", "reference_id", "node_id"),
    "verified_references": ("domain_id", "id"),
    "raw_sources": ("domain_id", "id"),
    "process_edges": ("domain_id", "upstream_id", "downstream_id"),
    "process_nodes": ("domain_id", "id"),
    "domains": ("id",),
    "domain_library": ("domain_id",),
}


def _fingerprint(row):
    fields = {key: value for key, value in row.items() if key not in ("is_stale", "stale_reason")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Projection:
    def __init__(self, db):
        self.db = db
        self.catalog = domain_libraries.catalog(db)
        self.domain_ids = {}
        self.previous = {(table, tuple(json.loads(key))): digest for table, key, digest in db.execute(
            "SELECT table_name,row_key,fingerprint FROM shared_workbench_rows") if table in ROW_KEYS}
        self.owned = {}

    def _key(self, table, keys):
        return tuple(keys[column] for column in ROW_KEYS[table])

    def _where(self, table):
        return " AND ".join(column + "=?" for column in ROW_KEYS[table])

    def _row(self, table, key):
        cursor = self.db.execute(f"SELECT * FROM {table} WHERE " + self._where(table), key)
        row = cursor.fetchone()
        return dict(zip((column[0] for column in cursor.description), row)) if row is not None else None

    def owns(self, table, keys):
        key = self._key(table, keys)
        locator = table, key
        expected = self.owned.get(locator, self.previous.get(locator))
        row = self._row(table, key)
        return row is not None and expected == _fingerprint(row)

    def keep(self, table, keys):
        key = self._key(table, keys)
        locator = table, key
        if locator in self.owned or locator not in self.previous:
            return
        row = self._row(table, key)
        if row is None or _fingerprint(row) == self.previous[locator]:
            self.owned[locator] = self.previous[locator]

    def put(self, table, keys, values):
        key = self._key(table, keys)
        locator = table, key
        row = self._row(table, key)
        expected = self.owned.get(locator, self.previous.get(locator))
        if row is None:
            if expected is not None:
                # A locally deleted row stays deleted on a later synchronization.
                self.owned[locator] = expected
                return False
            columns = {**keys, **values}
            self.db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                            tuple(columns.values()))
        elif _fingerprint(row) != expected:
            return True
        elif values:
            patch = {column: value for column, value in values.items() if row[column] != value}
            if patch:
                self.db.execute(f"UPDATE {table} SET " + ",".join(name + "=?" for name in patch)
                                + " WHERE " + self._where(table), (*patch.values(), *key))
        self.owned[locator] = _fingerprint(self._row(table, key))
        return True

    def domain(self, code):
        if code in self.domain_ids:
            return self.domain_ids[code]
        name = taxonomy.registry()[1][code]["name_zh"]
        target, _ = import_library.domain_scopes(self.db, code, self.catalog)
        if target:
            identity = target["id"]
        else:
            existing = self.db.execute("SELECT id FROM domains WHERE name=?", (name,)).fetchone()
            if existing:
                return None
            identity = self.db.execute("INSERT INTO domains(name) VALUES (?)", (name,)).lastrowid
            self.owned["domains", (identity,)] = _fingerprint(self._row("domains", (identity,)))
        self.domain_ids[code] = identity
        self.keep("domains", {"id": identity})
        return identity

    def labels(self, records_by_code):
        for code, identity in self.domain_ids.items():
            if ("domains", (identity,)) not in self.owned:
                continue
            records = records_by_code[code]
            summaries = []
            for row in records:
                if row["payload"].get("verification_status") != "rejected":
                    summary = row["payload"].get("summary_zh")
                    summaries.append((row["title"], summary[:500] if isinstance(summary, str) else ""))
            keywords = domain_keywords.extract(summaries, code)
            self.put("domain_library", {"domain_id": identity}, {
                "description": "由已连接的资料仓库同步。",
                "keywords": json.dumps(keywords, ensure_ascii=False),
            })

    def _locally_used(self, table, key, row):
        if table == "domains":
            identity, = key
            library = self._row("domain_library", key)
            return (
                library is not None and _fingerprint(library) != self.previous.get(("domain_library", key))
                or self.db.execute("SELECT 1 FROM local_import_domains WHERE domain_id=?", key).fetchone()
                or domain_libraries._named_use(self.db, row["name"])
                or self.db.execute("""SELECT 1 FROM runs WHERE CAST(coalesce(
                        json_extract(body,'$.scope_domain_id'),json_extract(body,'$.execution_scope.domain_id'),
                        json_extract(body,'$.scope_request.domain_id')) AS INTEGER)=?""", (identity,)).fetchone()
            )
        if table == "verified_references":
            return self.db.execute("SELECT 1 FROM verified_reference_nodes WHERE domain_id=? AND reference_id=?",
                                   key).fetchone()
        if table == "process_nodes":
            return self.db.execute("SELECT 1 FROM process_participants WHERE domain_id=? AND node_id=?", key).fetchone()
        return False

    def finish(self):
        retired = defaultdict(list)
        for table, key in self.previous.keys() - self.owned.keys():
            retired[table].append(key)
        for table in ROW_KEYS:
            if table == "domain_library":
                # Deleting a purely shared domain removes these labels through its FK.
                continue
            for key in retired[table]:
                row = self._row(table, key)
                if row is None or _fingerprint(row) != self.previous[table, key] or self._locally_used(table, key, row):
                    continue
                try:
                    self.db.execute(f"DELETE FROM {table} WHERE " + self._where(table), key)
                except sqlite3.IntegrityError:
                    # A local record can adopt an otherwise unchanged shared parent.
                    pass
        self.db.execute("DELETE FROM shared_workbench_rows")
        self.db.executemany("INSERT INTO shared_workbench_rows VALUES (?,?,?)",
                            ((table, json.dumps(key, ensure_ascii=False), digest)
                             for (table, key), digest in self.owned.items()))


def materialize(db, repository, head, *, rebuild=False):
    """Apply a full logical view in the caller's SQLite transaction."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='domain_library'").fetchone():
        return
    db.execute("""CREATE TABLE IF NOT EXISTS shared_workbench_rows (
        table_name TEXT NOT NULL, row_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
        PRIMARY KEY (table_name,row_key))""")
    db.execute("""CREATE TABLE IF NOT EXISTS shared_workbench_projection (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), repository TEXT, head TEXT, version INTEGER NOT NULL
    )""")
    marker = (repository, head, VERSION)
    if not rebuild and db.execute(
        "SELECT repository,head,version FROM shared_workbench_projection WHERE singleton=1"
    ).fetchone() == marker:
        return
    writer = Projection(db)
    if repository and head:
        from .shared_projection_records import project

        groups = defaultdict(list)
        records = []
        for (body,) in db.execute("SELECT body FROM shared_metadata WHERE status='active' ORDER BY path"):
            item = json.loads(body)
            groups[item["l2_code"]].append(item)
            records.append(item)
        project(db, writer, records)
        writer.labels(groups)
    writer.finish()
    db.execute("INSERT OR REPLACE INTO shared_workbench_projection VALUES (1,?,?,?)", marker)


def restore_cached(db):
    cached = db.execute("""SELECT s.repository,s.last_synced_commit FROM shared_metadata_sync s
        JOIN collaboration_repository c ON c.repository=s.repository WHERE s.source_ref=?
        AND c.repository<>''""", (shared_metadata.PUBLISHED_REF,)).fetchone()
    if cached:
        materialize(db, *cached)

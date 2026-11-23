"""Project committed shared metadata into the local SQLite workbench."""

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from industplatform.data_location import resolve_database
from scripts.data_policy import metadata, record_blob
from scripts.repo_support import Repository, fail

PUBLISHED_REF = metadata.PUBLISHED_REF


def database_path(repo, override=None):
    path = Path(override or os.environ.get("INDUST_DB", ".local/industplatform.sqlite3"))
    return resolve_database(path if path.is_absolute() else repo.root / path)


def synchronize(repo, database, *, check=False, ref=None, rebuild=False, automatic=False, repository=None):
    if ref not in (None, "HEAD", PUBLISHED_REF):
        raise ValueError("共享视图只接受 HEAD 或 origin/main")
    if check and not database.is_file():
        raise ValueError("共享视图尚未初始化，请运行同步")
    if repository and not check:
        from industplatform.shared_projection import materialize

    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(database), timeout=15)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.create_function("workflow_contract_id", 0, lambda: None)
        db.execute("BEGIN IMMEDIATE" if not check else "BEGIN")
        if not check:
            metadata.initialize(db)
            configured = metadata.configured_repository(db)
            if configured != (repository or ""):
                if automatic and repository is None:
                    return None, 0, 0
                raise ValueError("同步来源与当前配置的数据仓库不一致")
            if automatic and db.execute("SELECT 1 FROM collaboration_repository").fetchone():
                return None, 0, 0
            if automatic and not metadata.sync_preferences(db)["auto_import"]:
                return None, 0, 0
        has_state = db.execute("SELECT 1 FROM sqlite_master WHERE name='shared_metadata_sync'").fetchone()
        columns = {row[1] for row in db.execute("PRAGMA table_info(shared_metadata_sync)")}
        source_column = "source_ref" if "source_ref" in columns else "'HEAD'"
        repository_column = "repository" if "repository" in columns else "NULL"
        saved = db.execute(f"SELECT checkout,last_synced_commit,{source_column},{repository_column} "
                           "FROM shared_metadata_sync").fetchone() if has_state else None
        selected = ref or (saved[2] if saved else "HEAD")
        head = repo.revision(selected)
        checkout = str(repo.root)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if saved and (saved[3] != repository or (repository is None and saved[0] != checkout)):
            raise ValueError("此共享视图已绑定另一检出目录；请为每个工作树保留独立数据库")
        if not rebuild and saved and saved[1:3] == (head, selected):
            if ref == PUBLISHED_REF and not check:
                if repository:
                    materialize(db, repository, head)
                db.execute("UPDATE shared_metadata_sync SET synced_at=? WHERE singleton=1", (stamp,))
            return head, 0, 0
        if check:
            raise ValueError("共享视图落后于当前同步来源，请运行 scripts/sync_meta_to_local.py")
        tree = repo.tree(head, metadata.PREFIX)
        previous = repo.revision(saved[1], optional=True) if saved and not rebuild else None
        changes = repo.changes(previous, head, metadata.PREFIX) if previous else [("A", path) for path in tree]
        if not previous:
            db.execute("DELETE FROM shared_metadata")
        applied = removed = 0
        for status, path in changes:
            if status == "D":
                db.execute("DELETE FROM shared_metadata WHERE path=?", (path,))
                removed += 1
                continue
            record = record_blob(repo, path, tree[path])
            db.execute("""INSERT INTO shared_metadata(path,domain_id,kind,canonical_id,title,version,status,body)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                title=excluded.title, version=excluded.version, status=excluded.status, body=excluded.body""",
                (path, *(record[key] for key in ("l2_code", "kind", "canonical_id", "title", "version", "status")),
                 metadata.json_bytes(record).decode("utf-8")))
            applied += 1
        if repository:
            materialize(db, repository, head, rebuild=rebuild)
        if repo.revision(selected) != head:
            raise ValueError("同步来源已变化，请重试")
        db.execute("""INSERT INTO shared_metadata_sync(singleton,checkout,last_synced_commit,source_ref,repository,synced_at)
            VALUES (1,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET checkout=excluded.checkout,
            last_synced_commit=excluded.last_synced_commit, source_ref=excluded.source_ref,
            repository=excluded.repository,synced_at=excluded.synced_at""", (checkout, head, selected, repository, stamp))
        return head, applied, removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ref", choices=("HEAD", "origin/main"))
    args = parser.parse_args()
    try:
        repo = Repository()
        ref = PUBLISHED_REF if args.ref == "origin/main" else args.ref
        head, updated, removed = synchronize(repo, database_path(repo, args.db), check=args.check, ref=ref,
                                             automatic=args.automatic)
        if head:
            print(f"共享元数据 {head[:12]} · 更新 {updated} · 移出当前视图 {removed}")
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

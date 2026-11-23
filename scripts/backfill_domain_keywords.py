"""Backfill domain keywords using the imported titles and summaries in an existing local database."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from industplatform import import_library


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(".local/industplatform.sqlite3"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    uri = args.db.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        updated = import_library.backfill_keywords(db)
        if args.dry_run:
            db.rollback()
    print(json.dumps({"dry_run": args.dry_run, "updated": len(updated), "domains": updated}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

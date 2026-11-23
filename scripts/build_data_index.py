"""Incrementally refresh the bot-owned index from committed domain trees."""

import argparse
import hashlib
import json
from collections import defaultdict

try:
    from .repo_support import Repository, fail, safe_path
    from .data_policy import metadata, record_blob
except ImportError:
    from repo_support import Repository, fail, safe_path
    from data_policy import metadata, record_blob

MANIFEST = metadata.INDEX + "manifest.json"
CHUNK_BYTES = 400_000


def trees(repo, ref):
    groups = defaultdict(dict)
    for path, entry in repo.tree(ref, metadata.PREFIX).items():
        domain, _, _ = metadata.path_parts(path)
        groups[domain][path] = entry
    return groups


def fingerprint(entries):
    lines = [path + "\t" + entry.mode + "\t" + entry.oid for path, entry in sorted(entries.items())]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def manifest(repo, index):
    if MANIFEST not in index:
        return {}
    entry = index[MANIFEST]
    if entry.size >= metadata.MAX_BYTES:
        raise ValueError("索引清单过大")
    value = metadata.json_object(repo.blob(entry))
    if not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("domains"), dict):
        return {}
    return value["domains"]


def index_info(repo, domain, entries, index, old):
    cached = old.get(domain)
    if not isinstance(cached, dict) or cached.get("tree") != fingerprint(entries):
        return None
    files = cached.get("chunks")
    prefix = metadata.INDEX + f"domains/{domain}/"
    if (not isinstance(files, list) or not files or any(not isinstance(path, str) or not path.startswith(prefix)
            or path not in index or index[path].size > CHUNK_BYTES for path in files)):
        return None
    return cached


def known_paths(repo, ref, domains):
    groups = trees(repo, ref)
    index = repo.tree(ref, metadata.INDEX)
    old = manifest(repo, index)
    known = set()
    indexed = 0
    for domain in sorted(domains):
        entries = groups.get(domain, {})
        cached = index_info(repo, domain, entries, index, old)
        if cached:
            paths = {json.loads(line)["path"] for path in cached["chunks"]
                     for line in repo.blob(index[path]).splitlines()}
            if paths == entries.keys():
                known.update(paths)
                indexed += 1
                continue
        known.update(entries)
    return known, indexed


def build(repo, ref):
    index = repo.tree(ref, metadata.INDEX)
    old = manifest(repo, index)
    planned = {}
    unchanged = set()
    domains = {}
    rebuilt = 0
    for domain, entries in sorted(trees(repo, ref).items()):
        cached = index_info(repo, domain, entries, index, old)
        if cached:
            domains[domain] = cached
            unchanged.update(cached["chunks"])
            continue
        rebuilt += 1
        chunks = []
        buffer = bytearray()

        def flush():
            path = metadata.INDEX + f"domains/{domain}/{len(chunks) + 1:04d}.jsonl"
            planned[path] = bytes(buffer)
            chunks.append(path)
            buffer.clear()

        for path, entry in sorted(entries.items()):
            record = record_blob(repo, path, entry)
            row = {"path": path, **{key: record[key] for key in (
                "canonical_id", "canonical_key", "status", "version", "object_cluster", "lifecycle_stages")}}
            line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(buffer) + len(line) > CHUNK_BYTES:
                flush()
            buffer.extend(line)
        if buffer:
            flush()
        domains[domain] = {"tree": fingerprint(entries), "records": len(entries), "chunks": chunks}
    planned[MANIFEST] = metadata.json_bytes({"format": 1, "domains": domains})
    if len(planned[MANIFEST]) >= metadata.MAX_BYTES:
        raise ValueError("索引清单达到 500 KB，请增加索引分区")
    return planned, sorted(set(index) - unchanged - planned.keys()), rebuilt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        repo = Repository()
        planned, removed, rebuilt = build(repo, repo.revision("HEAD"))
        if args.write:
            for path, data in planned.items():
                target = safe_path(repo.root, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            for path in removed:
                safe_path(repo.root, path).unlink(missing_ok=True)
        print(f"索引 · 重建 {rebuilt} 个领域，清理 {len(removed)} 个旧索引文件")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Synchronize shared metadata using the local GitHub CLI session."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from industplatform.local_import import publication_records

from .build_data_index import build
from .data_policy import check_content, check_path, metadata, record_blob
from .export_completed_task import task_records
from .github_automation import GitHub
from .pr_guard import check
from .repo_support import Entry, SHA, indexed_commit
from .rollback_data_task import plan_rollback
from .sync_meta_to_local import PUBLISHED_REF, synchronize

RUN_ID = re.compile(r"[0-9a-f]{32}")
MAIN_REF = "refs/heads/main"
PUSH_ATTEMPTS = 4
INDEX_AUTHOR = {
    "GIT_AUTHOR_NAME": "indust-index", "GIT_AUTHOR_EMAIL": "indust-index@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "indust-index", "GIT_COMMITTER_EMAIL": "indust-index@users.noreply.github.com",
}


def published_data(repo, api, database, *, rebuild=False):
    api.private_repository()
    api.fetch_main()
    head, received, removed = synchronize(repo, database, ref=PUBLISHED_REF, rebuild=rebuild,
                                          repository=api.name.casefold())
    return {"commit": head, "updated": received, "removed": removed}


def _blobs(repo, files):
    return {path: repo.git("hash-object", "-w", "--stdin", data=body).decode().strip() for path, body in files.items()}


def _git_identity(repo, api):
    try:
        repo.git("var", "GIT_AUTHOR_IDENT")
        repo.git("var", "GIT_COMMITTER_IDENT")
        return None
    except ValueError:
        account = json.loads(api.gh("api", "user"))
        login, identity = account["login"], int(account["id"])
        if not re.fullmatch(r"[A-Za-z0-9-]+", login):
            raise ValueError("无法确认提交者的 GitHub 账号") from None
        email = f"{identity}+{login}@users.noreply.github.com"
        return {"GIT_AUTHOR_NAME": login, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": login, "GIT_COMMITTER_EMAIL": email}


@dataclass
class Publication:
    records: dict[str, dict]
    corrections: dict[str, tuple[Entry, dict]]
    baseline: str | None


def _record_entries(repo, head, paths):
    if not head or not paths:
        return {}
    if len(paths) <= 128:
        return repo.tree(head, paths)
    return {path: entry for code in {path.split("/")[2] for path in paths}
            for path, entry in repo.tree(head, metadata.PREFIX + code).items() if path in paths}


def _same_record(before, after):
    return all(before.get(field) == after.get(field) for field in
               ("title", "status", "payload", "lifecycle_stages", "object_cluster", "superseded_by"))


def publication(repo, db, identity, *, repository=None):
    if not isinstance(identity, str) or not RUN_ID.fullmatch(identity):
        raise ValueError("请选择有效的采集任务")
    imported = publication_records(db, identity)
    if imported is not None:
        return Publication(imported, {}, None)
    run_scope = db.execute("""SELECT json_extract(body,'$.shared_metadata_base'),
        json_extract(body,'$.shared_metadata_repository') FROM runs WHERE id=?""", (identity,)).fetchone()
    if not run_scope:
        raise ValueError("未找到要同步的研究任务")
    baseline, baseline_repository = run_scope
    if repository and (baseline_repository is not None or baseline is not None) and (
        not isinstance(baseline_repository, str) or baseline_repository.casefold() != repository.casefold()
    ):
        raise ValueError("任务的共享基线来自其他或未确认的数据仓库，请核对数据仓库后重新运行")
    if baseline is not None and (
        not isinstance(baseline, str) or not SHA.fullmatch(baseline) or not repo.revision(baseline, optional=True)
    ):
        raise ValueError("任务开始时的共享基线不可用，请恢复该版本后重试")
    retired: set[str] = set()
    records = task_records(db, identity, retired=retired)
    entries = _record_entries(repo, baseline, records.keys() | retired)
    corrections = {}
    for path, entry in entries.items():
        original = record_blob(repo, path, entry)
        if path in retired:
            if original["status"] == "deprecated":
                continue
            records[path] = {**original, "status": "deprecated", "task_id": identity}
        elif _same_record(original, records[path]):
            continue
        corrections[path] = (entry, original)
    return Publication(records, corrections, baseline)


def publication_preview(repo, db, identity, head, *, repository=None):
    draft = publication(repo, db, identity, repository=repository)
    existing = _record_entries(repo, head, draft.records.keys())
    counts: dict[str, int] = {}
    for path, row in draft.records.items():
        if path not in existing and path not in draft.corrections:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return {"counts": counts, "correction": bool(draft.corrections), "total": len(draft.records)}


def _checked_files(draft, reason):
    checked = {}
    for path, proposed in draft.records.items():
        change = draft.corrections.get(path)
        check_path("data", "M" if change else "A", path)
        if change:
            _, original = change
            if not reason:
                raise ValueError(f"{path} · 修改既有记录须填写纠错说明")
            proposed = {**proposed, "version": original["version"] + 1, "correction_reason": reason,
                        "updated_at": max(proposed["updated_at"], original["updated_at"])}
        body = metadata.json_bytes(proposed)
        check_content(path, body)
        try:
            metadata.validate_record(path, body)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"{path} · {exc}") from None
        checked[path] = body
    return checked


def _rebase_files(repo, draft, checked, base):
    existing = _record_entries(repo, base, checked.keys())
    files = {}
    for path, body in checked.items():
        found = existing.get(path)
        if path in draft.corrections:
            expected, _ = draft.corrections[path]
            if found == expected:
                files[path] = body
            elif found:
                published = record_blob(repo, path, found)
                requested = metadata.json_object(body)
                if (published["task_id"], published.get("correction_reason")) == (
                        requested["task_id"], requested["correction_reason"]) and _same_record(published, requested):
                    continue
                raise ValueError(f"{path} · 远端已修改该记录，请核对新版后重做纠错")
            else:
                raise ValueError(f"{path} · 远端记录已移出，请核对新版")
        elif not found:
            files[path] = body
    return files


def _push_or_latest(repo, api, base, head):
    try:
        api.git("push", "origin", f"{head}:{MAIN_REF}")
    except ValueError:
        latest = api.fetch_main()
        if latest == base:
            raise
        ancestor = repo.merge_base(latest, head)
        if ancestor == head:
            return None
        if ancestor != base:
            raise ValueError("共享主分支的历史已改写，请核对远端后再同步") from None
        return latest
    return None


def publish_run(repo, api, database, identity, *, reason="", progress=lambda phase: None):
    progress("validate")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        draft = publication(repo, db, identity, repository=api.name)
    checked = _checked_files(draft, reason)
    if not checked:
        return {"status": "empty", "added": 0}

    progress("rebase")
    info = api.private_repository()
    if not info.get("permissions", {}).get("push"):
        raise ValueError("当前 GitHub 账号需要此仓库的写入权限")
    base = api.fetch_main()
    if draft.baseline and repo.merge_base(draft.baseline, base) != draft.baseline:
        raise ValueError("共享基线历史已变化，请核对新版后重试")
    for _ in range(PUSH_ATTEMPTS):
        files = _rebase_files(repo, draft, checked, base)
        if not files:
            return {"status": "duplicate", "added": 0, "skipped": len(checked), "commit": base}
        head = indexed_commit(repo, base, _blobs(repo, files), f"data: publish run {identity}",
                              _git_identity(repo, api))
        check(repo, base, head, "data/run-" + identity, bool(draft.corrections))
        corrected = len(files.keys() & draft.corrections.keys())
        receipt = {"status": "pushed", "commit": head, "added": len(files) - corrected,
                   "skipped": len(checked) - len(files)}
        if corrected:
            receipt["corrected"] = corrected
        progress("push")
        latest = _push_or_latest(repo, api, base, head)
        if latest is None:
            return receipt
        progress("rebase")
        base = latest
    raise ValueError("共享主分支仍在更新，请稍后重试同步")


def publish_rollback(repo, api, sha, *, reason):
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("请选择有效的资料提交")
    reason = metadata.required_text(reason, 2000, "回滚原因")
    permissions = api.private_repository().get("permissions", {})
    if not permissions.get("push"):
        raise ValueError("当前 GitHub 账号需要此仓库的写入权限")
    base = api.fetch_main()
    for _ in range(PUSH_ATTEMPTS):
        _, files, _ = plan_rollback(repo, sha, reason, base)
        head = indexed_commit(repo, base, _blobs(repo, files), f"data: roll back {sha[:12]}",
                              _git_identity(repo, api))
        check(repo, base, head, "data/rollback-" + sha[:12], True)
        latest = _push_or_latest(repo, api, base, head)
        if latest is None:
            return {"status": "pushed", "commit": head, "rolled_back": sha, "corrected": len(files)}
        base = latest
    raise ValueError("共享主分支仍在更新，请稍后重试回滚")


def recent_publications(repo, ref):
    if not ref:
        return []
    commits = repo.git("log", "-20", "--no-merges", "--format=%H%x09%P%x09%cI", ref,
                       "--", metadata.PREFIX).decode().splitlines()
    recent = []
    order = ("tasks", "processes", "literature", "entities", "sources")
    for line in commits:
        head, parent, timestamp = line.split("\t")
        if not parent:
            continue
        changes = repo.changes(parent, head)
        if not changes or any(status not in ("A", "M") or not path.startswith(metadata.PREFIX)
                              for status, path in changes):
            continue
        try:
            paths = {path: metadata.path_parts(path) for _, path in changes}
            sample = min(paths, key=lambda path: (order.index(paths[path][1]), path))
            record = record_blob(repo, sample, repo.tree(head, sample)[sample])
        except (ValueError, KeyError):
            continue
        recent.append({"commit": head, "time": timestamp, "l2_code": record["l2_code"],
                       "title": record["title"], "run_id": record["task_id"]})
        if len(recent) == 5:
            break
    return recent


def update_index(repo, api, base):
    if not (repo.tree(base, metadata.PREFIX) or repo.tree(base, metadata.INDEX)):
        return base
    files, removed, _ = build(repo, base)
    head = indexed_commit(repo, base, {**_blobs(repo, files), **dict.fromkeys(removed)},
                          "Update shared metadata index", INDEX_AUTHOR)
    if head != base:
        api.git("push", "origin", f"{head}:{MAIN_REF}")
    return head


def maintain(repo, api):
    api.private_repository(maintainer=True)
    return {"commit": update_index(repo, api, api.fetch_main())}

"""Undo a published data task using auditable version increments."""

import argparse
import json
from datetime import datetime, timezone

try:
    from .repo_support import Repository, fail, safe_path
    from .data_policy import metadata, record_blob
except ImportError:
    from repo_support import Repository, fail, safe_path
    from data_policy import metadata, record_blob


def plan_rollback(repo, revision, reason, head):
    commit = repo.revision(revision)
    parents = repo.git("show", "-s", "--format=%P", commit).decode().split()
    if len(parents) != 1:
        raise ValueError("请选择包含一次资料批次的单父提交")
    if repo.merge_base(commit, head) != commit:
        raise ValueError("目标任务尚未合入当前分支")
    metadata.required_text(reason, 2000, "回滚原因")
    before, delivered, current = (repo.tree(ref, metadata.PREFIX) for ref in (parents[0], commit, head))
    changes = repo.changes(parents[0], commit)
    batch = {}
    originals = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for status, path in changes:
        metadata.path_parts(path)
        if status not in ("A", "M") or delivered[path] != current.get(path):
            raise ValueError(f"{path} · 后续已有改动或当前记录缺失，请人工提交纠错")
        value = record_blob(repo, path, current[path])
        prior = record_blob(repo, path, before[path]) if status == "M" else {**value, "status": "deprecated"}
        corrected = {**prior, "version": value["version"] + 1, "updated_at": max(now, value["updated_at"]),
                     "task_id": "rollback-" + commit[:24], "correction_reason": reason}
        batch[path] = metadata.json_bytes(corrected)
        metadata.validate_record(path, batch[path])
        originals[path] = repo.blob(current[path])
    if not batch:
        raise ValueError("找不到要回滚的资料条目")
    return commit, batch, originals


def rollback(repo, revision, reason, *, apply=False):
    commit, batch, originals = plan_rollback(repo, revision, reason, repo.revision("HEAD"))
    if apply:
        if not repo.branch().startswith("data/"):
            raise ValueError("请在基于最新 main 的 data/ 分支应用回滚")
        targets = {path: safe_path(repo.root, path) for path in batch}
        for path, target in targets.items():
            if target.is_symlink() or target.read_bytes() != originals[path]:
                raise ValueError(f"{path} · 工作区中已有编辑，请先保存")
        written = []
        try:
            for path, target in targets.items():
                written.append(path)
                target.write_bytes(batch[path])
        except OSError:
            for path in written:
                targets[path].write_bytes(originals[path])
            raise
    return {"commit": commit, "applied": apply, "records": list(batch), "correction_reason": reason}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = rollback(Repository(), args.commit, args.reason, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

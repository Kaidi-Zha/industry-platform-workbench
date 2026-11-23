"""Validate the objects sent to the shared main ref, including command-line pushes."""

import sys

from . import data_policy as policy
from .repo_support import Repository, SHA, fail


def validate_push(repo, refs):
    for line in refs:
        _, head, ref, base = line.split()
        if ref != "refs/heads/main":
            continue
        if not SHA.fullmatch(head) or not SHA.fullmatch(base) or not head.strip("0"):
            raise ValueError("禁止删除共享 main")
        if not base.strip("0"):
            raise ValueError("请先初始化共享仓库的 main 分支")
        if repo.revision(base, optional=True) is None:
            raise ValueError("请先 git fetch origin main，再重试推送")
        if repo.merge_base(base, head) != base:
            raise ValueError("请基于最新远端 main 提交，禁止覆盖共享历史")
        changes = repo.changes(base, head)
        if not changes:
            policy.validate_changes(repo, base, head, "feat/push")
        elif all(path.startswith(policy.metadata.INDEX) for _, path in changes):
            # The optional index maintainer appends one generated index commit.
            if repo.git("show", "-s", "--format=%P", head).decode().strip() != base:
                raise ValueError("共享索引须独立提交")
            entries = repo.tree(head, policy.metadata.INDEX)
            for status, path in changes:
                if status != "D":
                    policy.inspect_blob(repo, path, entries[path])
        elif any(path.startswith(policy.metadata.PREFIX) for _, path in changes):
            if repo.git("show", "-s", "--format=%P", head).decode().strip() != base:
                raise ValueError("资料推送须为单任务单提交")
            policy.validate_changes(repo, base, head, "data/push", correction=True, history=False)
        else:
            policy.validate_changes(repo, base, head, "feat/push")


if __name__ == "__main__":
    try:
        validate_push(Repository(), sys.stdin)
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(fail(error))

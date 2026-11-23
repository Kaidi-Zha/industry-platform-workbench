"""Check staged, committed, and newly produced repository files."""

import argparse
import os
from pathlib import Path

try:
    from .repo_support import Repository, fail, safe_path
    from . import data_policy as policy
except ImportError:
    from repo_support import Repository, fail, safe_path
    import data_policy as policy


def ignore_locally(repo, paths):
    kind = policy.branch_kind(repo.branch())
    if kind in ("feat", "chore"):
        target = repo.root / ".gitignore"
    elif kind == "data":
        target = Path(os.fsdecode(repo.git("rev-parse", "--git-path", "info/exclude").strip()))
        if not target.is_absolute():
            target = repo.root / target
    else:
        raise ValueError("请在 feat/、chore/ 或 data/ 分支整理忽略规则")
    count = append_ignores(target, paths)
    if count:
        print(f"已追加 {count} 条忽略规则到 {target}")


def append_ignores(target, paths):
    if target.is_symlink():
        raise ValueError("忽略规则文件不能是符号链接")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_bytes() if target.exists() else b""
    present = set(existing.decode("utf-8").splitlines())
    escaped = ["/" + "".join("\\" + char if char in "\\[]*?# !" else char for char in path)
               for path in paths if all(32 <= ord(char) != 127 for char in path)]
    lines = [item for item in escaped if item not in present]
    if lines:
        with target.open("ab") as stream:
            if existing and not existing.endswith(b"\n"):
                stream.write(b"\n")
            stream.write(("\n# 本地运行产物\n" + "\n".join(lines) + "\n").encode("utf-8"))
    return len(lines)


def audit_run_ignores(repo):
    paths = [path for path in repo.untracked() if policy.forbidden_path(path)]
    if not paths:
        return 0
    if repo.branch().startswith("data/"):
        target = Path(os.fsdecode(repo.git("rev-parse", "--git-path", "info/exclude").strip()))
        return append_ignores(repo.root / target, paths)
    return append_ignores(repo.root / ".gitignore", paths)


def run(repo, *, staged=False, fix_ignores=False):
    unsafe = [path for path in repo.untracked() if policy.forbidden_path(path)]
    if unsafe and fix_ignores:
        ignore_locally(repo, unsafe)
        unsafe = [path for path in repo.untracked() if policy.forbidden_path(path)]
    if unsafe:
        raise ValueError("这些本地产物尚未忽略：" + "、".join(unsafe))
    head = repo.revision("HEAD")
    index = repo.git("write-tree").decode().strip()
    if staged:
        policy.validate_changes(repo, head, index, repo.branch(),
                                correction=bool(os.environ.get("INDUST_CORRECTION_REASON", "").strip()), history=False)
    else:
        staged_tree = repo.tree(index)
        for path in sorted(staged_tree.keys() | set(repo.untracked())):
            cause = policy.forbidden_path(path)
            if cause:
                raise ValueError(f"{path} · {cause}")
            file = safe_path(repo.root, path)
            if file.is_symlink() or path in staged_tree and staged_tree[path].mode == "160000":
                raise ValueError(f"{path} · 不接受符号链接或 Git 子模块")
            if file.is_file():
                with file.open("rb") as stream:
                    data = stream.read(policy.metadata.MAX_BYTES)
                if len(data) >= policy.metadata.MAX_BYTES:
                    raise ValueError(f"{path} · 单文件达到 500 KB")
                policy.check_content(path, data)
        before = repo.tree(head)
        for status, path in repo.changes(head, index):
            if status != "D":
                policy.inspect_blob(repo, path, staged_tree[path])
        if ".gitignore" in before:
            current = repo.root / ".gitignore"
            if not current.is_file() or ".gitignore" not in staged_tree:
                raise ValueError("不能删除根 .gitignore")
            old = repo.blob(before[".gitignore"])
            policy.check_ignore(old, current.read_bytes())
            policy.check_ignore(old, repo.blob(staged_tree[".gitignore"]))
    print("仓库卫生检查通过")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--fix-ignores", action="store_true")
    args = parser.parse_args()
    try:
        run(Repository(), staged=args.staged, fix_ignores=args.fix_ignores)
    except (ValueError, OSError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

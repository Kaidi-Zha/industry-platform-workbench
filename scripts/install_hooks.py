"""Install this repository's hooks and initialize the current SQLite projection."""

import argparse
import os
import sqlite3

try:
    from .repo_support import Repository, fail
    from .sync_meta_to_local import database_path, synchronize
except ImportError:
    from repo_support import Repository, fail
    from sync_meta_to_local import database_path, synchronize


HOOKS = ("pre-push", "post-merge")


def hook_state(repo):
    if not repo:
        return dict.fromkeys(HOOKS, False)
    try:
        path = repo.git("config", "--get", "core.hooksPath").decode().strip()
    except ValueError:
        path = ""
    return {name: bool(path == ".githooks" and os.access(repo.root / path / name, os.X_OK)
                       and (repo.root / path / name).is_file()) for name in HOOKS}


def enable_hooks(repo):
    try:
        existing = repo.git("config", "--get", "core.hooksPath").decode().strip()
    except ValueError:
        existing = ""
    if existing not in ("", ".githooks"):
        raise ValueError("已配置其他 Git Hook，请先将现有 Hook 与 .githooks 合并")
    if not all((path := repo.root / ".githooks" / name).is_file() and os.access(path, os.X_OK) for name in HOOKS):
        raise ValueError("请从带有可执行 pre-push 和 post-merge 的仓库检出运行")
    if not existing:
        repo.git("config", "--local", "core.hooksPath", ".githooks")


def install(repo, database):
    enable_hooks(repo)
    head, _, _ = synchronize(repo, database, automatic=True)
    return head


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        repo = Repository()
        head = install(repo, database_path(repo))
        print(f"Git Hooks 已安装，共享视图已同步到 {head[:12]}" if head else "Git Hooks 已安装，已关闭自动入库")
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

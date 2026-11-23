"""Read Git objects and assemble batches with an isolated index."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class Entry:
    mode: str
    oid: str
    size: int


class Repository:
    def __init__(self, cwd: Path | str | None = None):
        self.root = Path(cwd or Path.cwd()).resolve()
        self.root = Path(os.fsdecode(self.git("rev-parse", "--show-toplevel").rstrip(b"\n")))

    def git(self, *arguments: str, data: bytes | None = None, env: Mapping[str, str] | None = None,
            timeout: int | None = None) -> bytes:
        try:
            result = subprocess.run(["git", "-C", str(self.root), *arguments], input=data, capture_output=True,
                                    env={**os.environ, **env} if env else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ValueError("Git 操作超时，请检查网络") from None
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", "replace").strip() or "Git 操作失败")
        return result.stdout

    def revision(self, name: str, *, optional: bool = False) -> str | None:
        try:
            return self.git("rev-parse", "--verify", "--end-of-options", name + "^{commit}").decode().strip()
        except ValueError:
            if optional:
                return None
            raise

    def tree(self, revision: str, prefix: str | Iterable[str] | None = None) -> dict[str, Entry]:
        paths = (prefix,) if isinstance(prefix, str) else tuple(prefix or ())
        args = ("--", *paths) if paths else ()
        entries = {}
        for raw in self.git("ls-tree", "-rlz", revision, *args).split(b"\0"):
            if raw:
                descriptor, path = raw.split(b"\t", 1)
                mode, _, oid, size = descriptor.decode("ascii").split()
                entries[path.decode("utf-8")] = Entry(mode, oid, int(size) if size != "-" else 0)
        return entries

    def changes(self, base: str, head: str, prefix: str | None = None) -> list[tuple[str, str]]:
        args = ("--", prefix) if prefix else ()
        data = self.git("diff", "--no-renames", "--name-status", "-z", base, head, *args).split(b"\0")[:-1]
        return [(status.decode("ascii"), path.decode("utf-8")) for status, path in zip(data[::2], data[1::2])]

    def blob(self, entry: Entry) -> bytes:
        if entry.mode not in ("100644", "100755"):
            raise ValueError("仅支持普通文件")
        return self.git("cat-file", "blob", entry.oid)

    def merge_base(self, base: str, head: str) -> str:
        values = self.git("merge-base", "--all", base, head).decode().splitlines()
        if len(values) != 1:
            raise ValueError("没有唯一共同祖先，请先基于 main 更新分支")
        return values[0]

    def introduced(self, base: str, head: str) -> Iterator[tuple[str, str, str]]:
        """Yield changed blobs, including those only found in intermediate commits."""
        data = self.git("log", "--diff-merges=first-parent", "--format=", "--raw", "-z", "--no-abbrev", "--no-renames",
                        "--diff-filter=AMT", base + ".." + head).split(b"\0")
        for header, path in zip(data[::2], data[1::2]):
            _, mode, _, oid, _ = header.strip().decode("ascii").removeprefix(":").split()
            yield path.decode("utf-8"), mode, oid

    def branch(self) -> str:
        return self.git("branch", "--show-current").decode().strip()

    def untracked(self) -> tuple[str, ...]:
        return tuple(path.decode("utf-8") for path in self.git(
            "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if path)


def indexed_commit(repo: Repository, base: str, blobs: Mapping[str, str | None], message: str,
                   author: Mapping[str, str] | None = None) -> str:
    """Use an isolated index, keeping the live checkout and staged work intact."""
    with tempfile.TemporaryDirectory() as directory:
        env = {**(author or {}), "GIT_INDEX_FILE": str(Path(directory) / "index")}
        repo.git("read-tree", base, env=env)
        entries = b"".join(f"{'100644' if oid else '0'} {oid or '0' * len(base)}\t{path}\0".encode()
                           for path, oid in sorted(blobs.items()))
        repo.git("update-index", "-z", "--index-info", data=entries, env=env)
        tree = repo.git("write-tree", env=env).decode().strip()
        if tree == repo.git("rev-parse", base + "^{tree}").decode().strip():
            return base
        return repo.git("commit-tree", tree, "-p", base, "-m", message, env=env).decode().strip()


def safe_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*relative.split("/"))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"路径超出工作区 {relative}")
    return path


def fail(error: Exception) -> int:
    print(f"错误 · {error}", file=sys.stderr)
    return 1

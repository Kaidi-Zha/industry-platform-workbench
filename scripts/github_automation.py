"""Use the current GitHub CLI login for repository-scoped synchronization."""

import json
import os
import re
import subprocess
from pathlib import Path

from .repo_support import Repository

ORIGIN = re.compile(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?")
GIT_CREDENTIAL = "credential.https://github.com.helper=!gh auth git-credential"
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}")


def github_name(url):
    match = ORIGIN.fullmatch(url.strip())
    name = match[1].removesuffix(".git") if match else ""
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("请将 origin 连接到 github.com 上的仓库")
    return name


def data_repository_name(value):
    if not isinstance(value, str):
        raise ValueError("请输入 GitHub 仓库的 owner/name")
    name = value.strip()
    if not name:
        return ""
    if not REPOSITORY_NAME.fullmatch(name):
        try:
            name = github_name(name)
        except ValueError:
            raise ValueError("请输入 GitHub 仓库的 owner/name 或仓库地址") from None
    if not REPOSITORY_NAME.fullmatch(name) or name.split("/")[1] in (".", ".."):
        raise ValueError("请输入有效的 GitHub 仓库名称")
    return name.lower()


def data_checkout(root: Path, name: str, *, create=True):
    name = data_repository_name(name)
    if not name:
        raise ValueError("请先选择私有数据仓库")
    parent = root.resolve()
    path = parent.joinpath(*name.split("/"))
    if path.resolve() != path or (path / ".git").is_symlink():
        raise ValueError("本地数据仓库目录不可使用符号链接")
    if not (path / ".git").is_dir():
        if not create:
            return None
        path.mkdir(parents=True, exist_ok=True)
        if any(path.iterdir()):
            raise ValueError("本地数据仓库目录已被占用")
        result = subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(path)], capture_output=True)
        if result.returncode:
            raise ValueError("无法初始化本地数据仓库")
        repo = Repository(path)
        repo.git("config", "--local", "core.hooksPath", ".git/disabled-hooks")
        repo.git("remote", "add", "origin", f"https://github.com/{name}.git")
    repo = Repository(path)
    if repo.root != path or GitHub.from_origin(repo).name.casefold() != name:
        raise ValueError("本地数据检出与已配置的数据仓库不一致")
    return repo


class GitHub:
    def __init__(self, repo, name):
        self.repo = repo
        self.name = name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.name):
            raise ValueError("无效的 GitHub 仓库")
        self.path = "repos/" + self.name

    @classmethod
    def from_origin(cls, repo):
        try:
            origin = repo.git("remote", "get-url", "origin").decode()
        except ValueError:
            raise ValueError("尚未配置 GitHub origin") from None
        return cls(repo, github_name(origin))

    def gh(self, *args):
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_HOST": "github.com"}
        try:
            result = subprocess.run(["gh", *args], cwd=self.repo.root, env=env, capture_output=True, timeout=40)
        except FileNotFoundError:
            raise ValueError("请安装 GitHub CLI 并运行 gh auth login") from None
        except subprocess.TimeoutExpired:
            raise ValueError("GitHub 请求超时，可重试") from None
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", "replace").strip() or "GitHub API 调用失败")
        return result.stdout

    def private_repository(self, *, maintainer=False):
        info = json.loads(self.gh("api", self.path))
        if (info["full_name"].casefold() != self.name.casefold() or not info["private"]
                or info["default_branch"] != "main"):
            raise ValueError("请选择默认分支为 main 的私有 GitHub 仓库")
        if maintainer and not (info.get("permissions", {}).get("admin") or info.get("permissions", {}).get("maintain")):
            raise ValueError("共享索引须由 GitHub 仓库维护者更新")
        return info

    def git(self, action, *args):
        flags = ("--push", "--all") if action == "push" else ("--all",)
        urls = self.repo.git("remote", "get-url", *flags, "origin").decode().splitlines()
        if len(urls) != 1 or github_name(urls[0]).casefold() != self.name.casefold():
            raise ValueError("Git origin 与已确认的 GitHub 仓库不一致")
        return self.repo.git("-c", "credential.helper=", "-c", GIT_CREDENTIAL, "-c", "push.followTags=false",
                             action, *args, env={"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1",
                                                  "GH_HOST": "github.com"}, timeout=40)

    def fetch_main(self):
        candidate = "refs/industplatform/candidate/main"
        self.git("fetch", "--quiet", "--no-tags", "--refmap=", "origin", f"+refs/heads/main:{candidate}")
        head = self.repo.revision(candidate)
        for entry in self.repo.git("ls-tree", "-z", head).split(b"\0"):
            if not entry:
                continue
            descriptor, path = entry.split(b"\t", 1)
            mode = descriptor.split()[0]
            if path == b"data" and mode == b"040000":
                names = set(self.repo.git("ls-tree", "--name-only", "-z", f"{head}:data").split(b"\0"))
                if names <= {b"", b"domains", b"_index"}:
                    continue
            if path in (b"README.md", b".gitignore", b"LICENSE", b"LICENSE.md") and mode == b"100644":
                continue
            raise ValueError("所选仓库的 main 包含平台文件或非资料目录，请选择独立的资料仓库")
        self.repo.git("update-ref", "refs/remotes/origin/main", head)
        return head

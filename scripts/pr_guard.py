"""Validate the local batch against the shared branch before pushing."""

import argparse

try:
    from .repo_support import Repository, fail
    from . import data_policy as policy
except ImportError:
    from repo_support import Repository, fail
    import data_policy as policy


def check(repo, base, head, branch, correction):
    ancestor = repo.merge_base(base, head)
    records, modified = policy.validate_changes(repo, ancestor, head, branch, correction=correction)
    if modified and ancestor != base:
        policy.validate_changes(repo, base, head, branch, correction=correction, history=False)
    return ancestor, records, modified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--branch")
    parser.add_argument("--correction-reason")
    args = parser.parse_args()
    try:
        repo = Repository()
        base, head = repo.revision(args.base), repo.revision(args.head)
        ancestor, records, modified = check(repo, base, head, args.branch or repo.branch(),
                                            bool(args.correction_reason and args.correction_reason.strip()))
        if ancestor != base:
            raise ValueError("请先将待推送批次更新到最新共享分支")
        print(f"推送前校验通过，共 {len(records)} 条共享记录，{len(modified)} 条纠错")
    except (ValueError, OSError, KeyError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

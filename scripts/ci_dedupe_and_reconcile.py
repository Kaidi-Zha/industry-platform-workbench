"""Preview the new and duplicate records in a local data batch."""

import argparse
import json
from dataclasses import dataclass

try:
    from .repo_support import Repository, fail
    from .data_policy import branch_kind
    from .build_data_index import known_paths
    from .pr_guard import check
except ImportError:
    from repo_support import Repository, fail
    from data_policy import branch_kind
    from build_data_index import known_paths
    from pr_guard import check


@dataclass
class Plan:
    ancestor: str
    keep: dict
    duplicate: dict
    indexed_domains: int


def reconcile(repo, base, head, branch, correction=False):
    if branch_kind(branch) != "data":
        raise ValueError("仅 data/ 批次可执行资料去重")
    ancestor, records, _ = check(repo, base, head, branch, correction)
    if correction:
        return Plan(ancestor, records, {}, 0)
    existing, count = known_paths(repo, base, {record["l2_code"] for record in records.values()})
    return Plan(ancestor, {path: record for path, record in records.items() if path not in existing},
                {path: record for path, record in records.items() if path in existing}, count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--branch")
    parser.add_argument("--correction", action="store_true")
    args = parser.parse_args()
    try:
        repo = Repository()
        plan = reconcile(repo, repo.revision(args.base), repo.revision(args.head), args.branch or repo.branch(),
                         args.correction)
        print(json.dumps({"keep": list(plan.keep), "duplicates": list(plan.duplicate),
                          "indexed_domains": plan.indexed_domains}, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

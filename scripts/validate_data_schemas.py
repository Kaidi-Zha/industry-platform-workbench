"""Validate the shared metadata tree and its canonical record identities."""

import argparse

try:
    from .repo_support import Repository, fail
    from .data_policy import check_content, metadata, record_blob, taxonomy
except ImportError:
    from repo_support import Repository, fail
    from data_policy import check_content, metadata, record_blob, taxonomy


def load_records(repo, ref, prefix=metadata.PREFIX):
    return {path: record_blob(repo, path, entry) for path, entry in repo.tree(ref, prefix).items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="Git revision. Defaults to all staged and working-tree metadata.")
    args = parser.parse_args()
    try:
        repo = Repository()
        taxonomy.registry()
        taxonomy.lifecycle()
        if args.ref:
            count = len(load_records(repo, repo.revision(args.ref)))
        else:
            count = 0
            for file in sorted((repo.root / metadata.PREFIX).rglob("*")):
                path = file.relative_to(repo.root).as_posix()
                if file.is_symlink():
                    raise ValueError(f"{path} · 共享数据不能使用符号链接")
                if file.is_file():
                    metadata.path_parts(path)
                    with file.open("rb") as stream:
                        data = stream.read(metadata.MAX_BYTES)
                    try:
                        check_content(path, data)
                        metadata.validate_record(path, data)
                    except ValueError as error:
                        raise ValueError(f"{path} · {error}") from None
                    count += 1
        print(f"共享元数据校验通过，共 {count} 条")
    except (ValueError, OSError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Pack one committed domain into JSONL, optionally compressed with zstd."""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from .repo_support import Repository, fail, safe_path
    from .data_policy import metadata, record_blob, taxonomy
except ImportError:
    from repo_support import Repository, fail, safe_path
    from data_policy import metadata, record_blob, taxonomy


def archive(repo, ref, domain, fmt):
    taxonomy.require_l2(domain)
    entries = repo.tree(ref, metadata.PREFIX + domain + "/")
    if not entries:
        raise ValueError("提交中没有此领域")
    executable = shutil.which("zstd") if fmt == "zst" else None
    if fmt == "zst" and not executable:
        raise ValueError("压缩归档需要 zstd，可先选择 --format jsonl")
    root = safe_path(repo.root, ".staging/archives")
    root.mkdir(parents=True, exist_ok=True)
    target = root / (f"{domain}-{ref[:12]}.jsonl" + (".zst" if executable else ""))
    with tempfile.TemporaryDirectory(dir=root) as directory:
        plain = Path(directory) / "rollup.jsonl"
        with plain.open("w", encoding="utf-8", newline="\n") as stream:
            for path, entry in sorted(entries.items()):
                row = record_blob(repo, path, entry)
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
        if executable:
            compressed = plain.with_suffix(".zst")
            subprocess.run([executable, "-q", str(plain), "-o", str(compressed)], check=True)
            compressed.replace(target)
        else:
            plain.replace(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--format", choices=("jsonl", "zst"), default="zst")
    args = parser.parse_args()
    try:
        repo = Repository()
        print(archive(repo, repo.revision(args.ref), args.domain, args.format))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        return fail(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicitly attached small engineering documents in repository-local storage."""

import hashlib
import os
import re
import tempfile
from contextlib import closing
from pathlib import Path

from .codex_runtime import ROOT

MAX_SIZE = 8 * 1024 * 1024
SUPPORTED = {".pdf", ".step", ".stp", ".dxf", ".stl", ".iges", ".igs"}


def _folder(domain_id, identity):
    root = (ROOT / ".local" / "attachments").resolve()
    path = (root / str(domain_id) / identity).resolve()
    if not root.is_relative_to(ROOT) or not path.is_relative_to(root):
        raise ValueError("附件存储位置无效")
    return path


def save(store, domain_id, name, content):
    if (len(name) > 140 or name in ("", ".", "..") or any(char in name for char in '\\/:*?"<>|')
            or any(ord(char) < 32 for char in name)):
        raise ValueError("附件文件名无效")
    extension = Path(name).suffix.casefold()
    if extension not in SUPPORTED or not 1 <= len(content) <= MAX_SIZE:
        raise ValueError("请选择 8 MiB 以内的 PDF 或 CAD 样例")
    if extension == ".pdf" and not content.startswith(b"%PDF-"):
        raise ValueError("附件内容不符合 PDF 文件标识")
    if extension in {".step", ".stp"} and not content.lstrip().startswith(b"ISO-10303-21;"):
        raise ValueError("附件内容不符合 STEP 文件标识")
    digest = hashlib.sha256(content).hexdigest()
    identity = hashlib.sha256((digest + "\n" + name).encode()).hexdigest()
    with closing(store.connect()) as db, db:
        if not db.execute("SELECT 1 FROM domains WHERE id=?", (domain_id,)).fetchone():
            raise ValueError("未找到附件所属领域")
        directory = _folder(domain_id, identity)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / ("source" + extension)
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
                name_on_disk = temporary.name
                temporary.write(content)
            try:
                os.replace(name_on_disk, path)
            finally:
                Path(name_on_disk).unlink(missing_ok=True)
        db.execute("INSERT OR IGNORE INTO domain_attachments VALUES (?,?,?,?,?)",
                   (domain_id, identity, name, len(content), digest))
    return {"id": identity, "name": name, "bytes": len(content), "sha256": digest}


def resolve(store, domain, ids):
    if (not isinstance(ids, list) or len(ids) > 4 or any(not isinstance(identity, str) or
            not re.fullmatch(r"[0-9a-f]{64}", identity) for identity in ids)):
        raise ValueError("每个任务可关联最多 4 个已上传附件")
    with closing(store.connect()) as db:
        owner = db.execute("SELECT id FROM domains WHERE name=?", (domain,)).fetchone()
        if not owner:
            raise ValueError("未找到附件所属领域")
        result = []
        for identity in dict.fromkeys(ids):
            item = db.execute("""
                SELECT a.domain_id,a.name,a.bytes,a.sha256 FROM domain_attachments a
                LEFT JOIN domain_library l ON l.domain_id=a.domain_id
                WHERE a.id=? AND (a.domain_id=? OR l.merged_into=?)
                ORDER BY a.domain_id<>? LIMIT 1
            """, (identity, owner[0], owner[0], owner[0])).fetchone()
            if not item:
                raise ValueError("附件不属于当前领域")
            stored_domain, name, length, digest = item
            path = _folder(stored_domain, identity) / ("source" + Path(name).suffix.casefold())
            if not path.is_file() or not path.resolve().is_relative_to(ROOT) or path.stat().st_size != length:
                raise ValueError("本机附件已不可用，请重新上传")
            result.append({"id": identity, "name": name, "bytes": length, "sha256": digest,
                           "path": str(path.relative_to(ROOT))})
        return result

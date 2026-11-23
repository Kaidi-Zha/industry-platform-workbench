"""Relocate local SQLite state and encrypted credential records."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path

# Python 3.9 does not expose these SQLite result code constants.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6


def location_file(anchor: Path) -> Path:
    return anchor.with_name(anchor.name + ".location.json")


def _absolute_file(text: str) -> Path:
    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("请输入数据库文件的完整绝对路径。")
    path = Path(text)
    separators = (os.sep, os.altsep) if os.altsep else (os.sep,)
    if not path.is_absolute() or not path.name or text.endswith(separators):
        raise ValueError("请输入数据库文件的完整绝对路径，包含文件名。")
    if os.name == "nt" and (path.is_reserved() or any(":" in part for part in path.parts[1:])):
        raise ValueError("请输入有效的数据库文件路径。")
    return path


def resolve_database(anchor: Path) -> Path:
    anchor = Path(anchor).absolute()
    pointer = location_file(anchor)
    try:
        info = pointer.lstat()
    except FileNotFoundError:
        return anchor
    except OSError:
        raise RuntimeError("无法读取数据库位置设置，请检查文件权限。") from None
    try:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError
        with pointer.open(encoding="utf-8") as stream:
            data = json.loads(stream.read(65537))
        target = _absolute_file(data["database"])
        if not target.is_file() or target.samefile(pointer):
            raise ValueError
        return target
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("数据库位置设置无效，请检查设置文件和所指向的数据库。") from None


def _credentials(database: Path) -> Path:
    return database.with_name(database.name + ".credentials")


def _check_target_available(target: Path) -> None:
    sidecars = (target.with_name(target.name + suffix) for suffix in ("-wal", "-shm", "-journal"))
    for path in (target, _credentials(target), *sidecars):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise ValueError("目标数据库或它的关联文件已存在，请选择新的文件名。")


def _sync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _backup_database(source: Path, staged: Path) -> None:
    deadline = time.monotonic() + 15

    def check_busy(status, _remaining, _total):
        if status in (_SQLITE_BUSY, _SQLITE_LOCKED) and time.monotonic() >= deadline:
            raise RuntimeError("源数据库仍被占用，请关闭其他使用它的应用后重试。")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(staged)) as backup:
            original.backup(backup, pages=256, progress=check_busy)
            if backup.execute("PRAGMA journal_mode = DELETE").fetchone() != ("delete",):
                raise RuntimeError("无法关闭目标数据库的写入日志，请检查目标目录。")
            if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("数据库完整性检查失败，请检查源数据库。")
    _sync_file(staged)
    staged.chmod(stat.S_IMODE(source.stat().st_mode))


def _credential_mode(path: Path, *, kind) -> int:
    info = path.lstat()
    if not kind(info.st_mode) or (os.name == "posix" and
                                 (info.st_uid != os.getuid() or info.st_mode & 0o077)):
        raise RuntimeError("源加密凭据目录或文件权限不安全，请检查类型、所有者和权限。")
    return stat.S_IMODE(info.st_mode)


def _stage_credentials(source: Path, staged: Path) -> int | None:
    from .credentials import EncryptedFileVault, _key_path

    try:
        mode = _credential_mode(source, kind=stat.S_ISDIR)
    except FileNotFoundError:
        return None
    try:
        key_file, _ = _key_path()
    except RuntimeError:
        raise RuntimeError("无法确定凭据主密钥的位置，请检查本机账户配置。") from None
    staged.mkdir(mode=0o700)
    for record in source.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}\.vault", record.name):
            continue
        permissions = _credential_mode(record, kind=stat.S_ISREG)
        if key_file is not None and key_file.is_file() and record.samefile(key_file):
            raise RuntimeError("主密钥文件与凭据记录冲突，请检查主密钥路径。")
        limit = EncryptedFileVault._MAX_CIPHERTEXT
        with record.open("rb") as stream:
            ciphertext = stream.read(limit + 1)
        if len(ciphertext) > limit:
            raise RuntimeError("源加密凭据记录大小无效，请检查原凭据目录。")
        copy = staged / record.name
        with copy.open("xb") as stream:
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        copy.chmod(permissions)
        if copy.read_bytes() != ciphertext:
            raise RuntimeError("加密凭据验证失败，请检查目标存储。")
    return mode


def _publish_file(staged: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(staged, target)
    else:
        os.link(staged, target)


def copy_database(source: Path, target_text: str) -> Path:
    """Publish a verified copy while the caller holds exclusive access to app writes."""
    target = _absolute_file(target_text)
    source = Path(source).absolute()
    try:
        target = target.parent.resolve() / target.name
        if not source.is_file():
            raise ValueError("源数据库文件不存在，请检查当前数据位置。")
        _check_target_available(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".data-move-", dir=target.parent) as temporary:
            staged_db = Path(temporary) / "database"
            staged_credentials = Path(temporary) / "credentials"
            _backup_database(source, staged_db)
            mode = _stage_credentials(_credentials(source), staged_credentials)
            _check_target_available(target)
            destination = _credentials(target)
            published_records = []
            created_directory = False
            try:
                if mode is not None:
                    destination.mkdir(mode=0o700)
                    created_directory = True
                    for staged in staged_credentials.iterdir():
                        record = destination / staged.name
                        _publish_file(staged, record)
                        published_records.append(record)
                    destination.chmod(mode)
                _publish_file(staged_db, target)
            except OSError:
                if created_directory:
                    destination.chmod(0o700)
                    for record in published_records:
                        if os.name == "nt":
                            record.chmod(stat.S_IWRITE)
                        record.unlink()
                    destination.rmdir()
                raise
    except FileExistsError:
        raise ValueError("目标数据库或它的关联文件已存在，请选择新的文件名。") from None
    except (OSError, sqlite3.Error):
        raise RuntimeError("无法复制数据库或加密凭据，请检查源文件、目标目录权限和可用空间。") from None
    return target


def persist_database(anchor: Path, target: Path) -> None:
    """Atomically select a copied database after the caller opens it successfully."""
    pointer = location_file(Path(anchor).absolute())
    target = _absolute_file(str(target))
    try:
        if (target.resolve() == pointer.resolve() or not target.is_file()
                or (pointer.exists() and target.samefile(pointer))):
            raise ValueError("请选择已验证且与位置设置文件不同的数据库文件。")
        pointer.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".data-location-", dir=pointer.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"database": str(target)}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, pointer)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        raise RuntimeError("无法保存数据库位置，请检查原数据目录的写入权限。") from None

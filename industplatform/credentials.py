"""Store model API keys in a local, authenticated encrypted vault."""

import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None


class CredentialError(ValueError):
    """A user-facing vault failure whose message contains no credential data."""


class UnavailableVault:
    identifier = "unavailable"

    def get(self, reference):
        raise CredentialError("本地加密凭据库不可用，无法读取 API 密钥。")

    def put(self, reference, key):
        raise CredentialError("本地加密凭据库不可用，无法保存 API 密钥。")

    def delete(self, reference):
        raise CredentialError("本地加密凭据库不可用，无法删除 API 密钥。")


def _check_permissions(info):
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise CredentialError("本地加密凭据库的目录或文件权限不安全。")


def _private_directory(path, *, create):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise CredentialError("本地加密凭据库目录无效。")
    _check_permissions(info)


def _read(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError("本地加密凭据库文件无效。")
    _check_permissions(info)
    with path.open("rb") as handle:
        return handle.read(limit + 1)


def _write_new(path, data):
    # Publish a complete file without replacing a concurrent writer's file.
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            # Windows rename fails when the destination already exists.
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _key_path():
    explicit = os.environ.get("INDUST_VAULT_KEY_FILE")
    if explicit:
        return Path(explicit).expanduser().absolute(), False
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            return None, False
        directory = Path(root) / "industplatform"
    elif sys.platform == "darwin":
        directory = Path.home() / "Library" / "Application Support" / "industplatform"
    else:
        root = os.environ.get("XDG_DATA_HOME")
        directory = (Path(root) if root else Path.home() / ".local" / "share") / "industplatform"
    return directory / "vault-v1.key", True


class EncryptedFileVault:
    identifier = "encrypted-file"
    _FORMAT = b"IPV1"
    _NONCE_BYTES = 12
    _KEY_BYTES = 32
    _MAX_CIPHERTEXT = len(_FORMAT) + _NONCE_BYTES + 2048 + 16

    def __init__(self, directory, key_file, *, private_key_directory=False):
        self.directory = directory
        self.key_file = key_file
        self.private_key_directory = private_key_directory

    def _record(self, reference):
        if not re.fullmatch(r"[0-9a-f]{32}", reference):
            raise CredentialError("本地加密凭据库引用无效。")
        return self.directory / (reference + ".vault")

    def _master_key(self, *, create):
        try:
            if self.private_key_directory:
                _private_directory(self.key_file.parent, create=False)
            key = _read(self.key_file, self._KEY_BYTES)
        except FileNotFoundError:
            if not create or any(self.directory.glob("*.vault")):
                raise CredentialError("本地加密凭据库的主密钥缺失，请恢复原主密钥。") from None
            if self.private_key_directory:
                _private_directory(self.key_file.parent, create=True)
            try:
                _write_new(self.key_file, secrets.token_bytes(self._KEY_BYTES))
            except FileExistsError:
                pass
            key = _read(self.key_file, self._KEY_BYTES)
        if len(key) != self._KEY_BYTES:
            raise CredentialError("本地加密凭据库的主密钥格式无效。")
        return key

    def _aad(self, reference):
        return self._FORMAT + b"/model-profile/" + reference.encode("ascii")

    def get(self, reference):
        record = self._record(reference)
        try:
            try:
                _private_directory(self.directory, create=False)
                encrypted = _read(record, self._MAX_CIPHERTEXT)
            except FileNotFoundError:
                return None
            if (not encrypted.startswith(self._FORMAT)
                    or not 33 <= len(encrypted) <= self._MAX_CIPHERTEXT):
                raise CredentialError("本地加密凭据库中的记录格式无效。")
            nonce_start = len(self._FORMAT)
            ciphertext_start = nonce_start + self._NONCE_BYTES
            secret = AESGCM(self._master_key(create=False)).decrypt(
                encrypted[nonce_start:ciphertext_start], encrypted[ciphertext_start:], self._aad(reference)
            )
            return secret.decode("ascii")
        except (InvalidTag, UnicodeDecodeError):
            raise CredentialError("无法解密已保存的 API 密钥，请检查主密钥或重新填写。") from None
        except OSError:
            raise CredentialError("无法读取本地加密凭据库，请检查文件权限。") from None

    def put(self, reference, key):
        record = self._record(reference)
        try:
            _private_directory(self.directory, create=True)
            master = self._master_key(create=True)
            nonce = secrets.token_bytes(self._NONCE_BYTES)
            encrypted = AESGCM(master).encrypt(nonce, key.encode("ascii"), self._aad(reference))
            _write_new(record, self._FORMAT + nonce + encrypted)
        except OSError:
            raise CredentialError("无法写入本地加密凭据库，请检查文件路径和权限。") from None

    def delete(self, reference):
        record = self._record(reference)
        try:
            _private_directory(self.directory, create=False)
            record.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise CredentialError("无法删除本地加密凭据，请检查文件权限。") from None


def local_vault(db_path):
    if AESGCM is None:
        return UnavailableVault()
    try:
        key_file, private_directory = _key_path()
    except RuntimeError:
        return UnavailableVault()
    if key_file is None:
        return UnavailableVault()
    database = Path(db_path).absolute()
    return EncryptedFileVault(database.with_name(database.name + ".credentials"), key_file,
                              private_key_directory=private_directory)

import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from industplatform.credentials import AESGCM, CredentialError, local_vault
from industplatform.model_profiles import ModelProfiles
from industplatform.storage import RunStore


@unittest.skipIf(AESGCM is None, "cryptography dependency is unavailable")
class EncryptedFileVaultTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "models.sqlite3"
        self.master = self.root / "master.key"
        self.secret = "test-token-for-encrypted-vault"
        self.fields = {"name": "Test connection", "protocol": "openai-responses", "model": "test-model",
                       "api_key": self.secret}
        environment = patch.dict(os.environ, {"INDUST_VAULT_KEY_FILE": str(self.master)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_restart_and_copy_require_original_master(self):
        profile = ModelProfiles(RunStore(self.database)).save(self.fields)
        self.assertEqual(ModelProfiles(RunStore(self.database)).get(profile["id"])["api_key"], self.secret)

        source = local_vault(self.database)
        record = next(source.directory.glob("*.vault"))
        self.assertNotIn(self.secret.encode(), record.read_bytes())
        self.assertEqual(len(self.master.read_bytes()), 32)
        target = self.root / "moved"
        target.mkdir()
        moved_database = target / self.database.name
        shutil.copyfile(self.database, moved_database)
        with patch.dict(os.environ, {"INDUST_VAULT_KEY_FILE": str(target / "master.key")}):
            destination = local_vault(moved_database)
            shutil.copytree(source.directory, destination.directory)
            moved = ModelProfiles(RunStore(moved_database))
            with self.assertRaisesRegex(CredentialError, "主密钥缺失"):
                moved.get(profile["id"])
            self.assertFalse(destination.key_file.exists())
            shutil.copy2(self.master, destination.key_file)
            self.assertEqual(moved.get(profile["id"])["api_key"], self.secret)

    def test_wrong_master_tampering_and_swapped_records_fail_authentication(self):
        vault = local_vault(self.database)
        first, second = uuid.uuid4().hex, uuid.uuid4().hex
        vault.put(first, self.secret)
        vault.put(second, "second-fake-key")
        original = self.master.read_bytes()
        self.master.write_bytes(os.urandom(32))
        with self.assertRaisesRegex(CredentialError, "无法解密"):
            vault.get(first)
        self.master.write_bytes(original)

        first_path = vault.directory / (first + ".vault")
        content = first_path.read_bytes()
        first_path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        with self.assertRaisesRegex(CredentialError, "无法解密"):
            vault.get(first)
        first_path.write_bytes((vault.directory / (second + ".vault")).read_bytes())
        with self.assertRaisesRegex(CredentialError, "无法解密"):
            vault.get(first)

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes")
    def test_rejects_insecure_master_permissions(self):
        vault = local_vault(self.database)
        reference = uuid.uuid4().hex
        vault.put(reference, self.secret)
        self.master.chmod(0o644)
        with self.assertRaisesRegex(CredentialError, "权限不安全"):
            vault.get(reference)

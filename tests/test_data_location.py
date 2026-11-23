import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from industplatform.credentials import AESGCM, local_vault
from industplatform.data_location import copy_database, location_file, persist_database, resolve_database
from industplatform.model_profiles import ModelProfiles
from industplatform.storage import RunStore


class DataLocationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "state.sqlite3"
        self.target = self.root / "新位置" / "project state.sqlite3"
        self.store = RunStore(self.source)

    @unittest.skipIf(AESGCM is None, "cryptography dependency is unavailable")
    def test_copies_wal_and_encrypted_profiles_then_switches_on_persist(self):
        master = self.root / "master.key"
        key = "test-token-for-storage-relocation"
        with patch.dict(os.environ, {"INDUST_VAULT_KEY_FILE": str(master)}):
            models = ModelProfiles(self.store)
            with closing(self.store.connect()) as connection:
                connection.execute("PRAGMA wal_autocheckpoint = 0")
                connection.execute("CREATE TABLE migration_probe (value TEXT)")
                connection.execute("INSERT INTO migration_probe VALUES ('committed in WAL')")
                connection.commit()
                profile = models.save({"name": "Test connection", "protocol": "openai-responses",
                                       "model": "test-model", "api_key": key})
                self.assertTrue(self.source.with_name(self.source.name + "-wal").is_file())
                destination = copy_database(self.source, str(self.target))

            self.assertEqual(destination, self.target.parent.resolve() / self.target.name)
            self.assertEqual(resolve_database(self.source), self.source)
            with closing(sqlite3.connect(destination)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM migration_probe").fetchone(),
                                 ("committed in WAL",))
            self.assertEqual(ModelProfiles(RunStore(destination)).get(profile["id"])["api_key"], key)
            original_vault = local_vault(self.source).directory
            relocated_vault = local_vault(destination).directory
            for record in original_vault.glob("*.vault"):
                moved_record = relocated_vault / record.name
                self.assertEqual(moved_record.read_bytes(), record.read_bytes())
                if os.name == "posix":
                    self.assertEqual(moved_record.stat().st_mode, record.stat().st_mode)
            if os.name == "posix":
                self.assertEqual(relocated_vault.stat().st_mode, original_vault.stat().st_mode)
            self.assertFalse((destination.parent / master.name).exists())
            self.assertTrue(master.exists())

            persist_database(self.source, destination)
            self.assertEqual(resolve_database(self.source), destination)
            self.assertTrue(self.source.is_file())

    def test_rejects_existing_database_or_sidecar(self):
        self.target.parent.mkdir()
        for suffix in ("", "-wal", "-shm", "-journal", ".credentials"):
            with self.subTest(suffix=suffix):
                occupied = self.target.with_name(self.target.name + suffix)
                occupied.write_bytes(b"existing user data")
                with self.assertRaisesRegex(ValueError, "已存在"):
                    copy_database(self.source, str(self.target))
                self.assertEqual(occupied.read_bytes(), b"existing user data")
                occupied.unlink()
        self.assertFalse(self.target.exists())
        self.assertFalse(location_file(self.source).exists())

    def test_rejects_corrupted_source_without_publishing(self):
        self.source.write_bytes(b"invalid database")
        with self.assertRaises(RuntimeError):
            copy_database(self.source, str(self.target))
        self.assertEqual(list(self.target.parent.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), b"invalid database")
        self.assertEqual(resolve_database(self.source), self.source)

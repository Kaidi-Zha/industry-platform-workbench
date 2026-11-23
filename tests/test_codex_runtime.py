import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from industplatform import codex_runtime
from run_codex import codex_args


class CodexRuntimeTest(unittest.TestCase):
    def test_codex_profile_stays_in_repo_and_keeps_local_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / ".local" / "codex"
            inherited = {
                "CODEX_HOME": str(root / "original"),
                "CODEX_SQLITE_HOME": str(root / "original-db"),
                "OPENAI_API_KEY": "fixture-shared-openai",
                "CODEX_API_KEY": "fixture-shared-codex",
            }
            with patch.object(codex_runtime, "ROOT", root), patch.object(codex_runtime, "CODEX_STATE_DIR", state):
                with patch.dict(os.environ, inherited):
                    first = codex_runtime.codex_environment()
                    self.assertEqual(first["CODEX_HOME"], str(state))
                    self.assertEqual(first["CODEX_SQLITE_HOME"], str(state))
                    self.assertNotIn("OPENAI_API_KEY", first)
                    self.assertNotIn("CODEX_API_KEY", first)
                    self.assertEqual({key: os.environ[key] for key in inherited}, inherited)
                    config = state / "config.toml"
                    self.assertIn("apps = false", config.read_text(encoding="utf-8"))
                    config.write_text('model = "workspace-model"\n', encoding="utf-8")
                    codex_runtime.codex_environment()
                    self.assertEqual(config.read_text(encoding="utf-8"), 'model = "workspace-model"\n')
            self.assertFalse((root / "original").exists())

    def test_rejects_external_config_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "repository"
            state = root / ".local" / "codex"
            state.mkdir(parents=True)
            external = root.parent / "original.toml"
            external.write_text("external fixture")
            try:
                (state / "config.toml").symlink_to(external)
            except OSError as exc:
                self.skipTest(str(exc))
            with patch.object(codex_runtime, "ROOT", root), patch.object(codex_runtime, "CODEX_STATE_DIR", state):
                with self.assertRaisesRegex(OSError, "必须位于本仓库"):
                    codex_runtime.codex_environment()
            self.assertEqual(external.read_text(), "external fixture")

    def test_codex_flags_select_file_backed_auth_and_local_logs(self):
        args = codex_args("/fixture/codex", 18765)
        self.assertIn("--no-daemon", args)
        self.assertIn('cli_auth_credentials_store="file"', args)
        self.assertIn('mcp_oauth_credentials_store="file"', args)
        self.assertTrue(any(arg.startswith("sqlite_home=") and ".local" in arg for arg in args))
        self.assertTrue(any(arg.startswith("log_dir=") and ".local" in arg for arg in args))

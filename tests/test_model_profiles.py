import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from industplatform import metrology
from industplatform.credentials import CredentialError, UnavailableVault
from industplatform.model_profiles import ModelProfiles
from industplatform.server import PROFILE_PATH, RequestHandler
from industplatform.settings import Settings
from industplatform.storage import RunStore
from industplatform.workflow import accept_request, catalog, process_run


class ModelProfilesTest(unittest.TestCase):
    def setUp(self):
        self.profiles = ModelProfiles()
        self.fields = {"name": "Lab model", "protocol": "openai-responses", "model": "lab-1",
                       "api_key": "test-credential" + "-not-a-real-key"}

    def test_profile_defaults_edit_and_credential_boundary(self):
        public = self.profiles.save(self.fields)
        self.assertEqual(public["base_url"], "https://api.openai.com/v1")
        self.assertTrue(public["has_key"])
        self.assertNotIn(self.fields["api_key"], json.dumps(self.profiles.catalog()))
        edited = {"id": public["id"], "name": "Renamed", "protocol": public["protocol"], "model": "lab-2"}
        self.profiles.save(edited)
        self.assertEqual(self.profiles.get(public["id"])["api_key"], self.fields["api_key"])
        with self.assertRaises(ValueError):
            self.profiles.save({**edited, "base_url": "https://gateway.example/v1"})
        with self.assertRaises(ValueError):
            self.profiles.save({**self.fields, "base_url": "https://example.test/v1?key=secret"})
        self.assertFalse(self.profiles.save({**edited, "base_url": "http://127.0.0.1:1234/v1"})["has_key"])

    @patch("industplatform.workflow.codex_agent.executable", return_value="/fixture/codex")
    def test_queued_run_keeps_selection_without_storing_key(self, _):
        public = self.profiles.save(self.fields)
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            args = accept_request({"route": "api-agent", "prompt": "Check QIF", "model_profile_id": public["id"]})
            run = self.profiles.enqueue(store, args, profile=self.profiles.get(public["id"]))
            self.profiles.remove(public["id"])
            self.assertIsNone(store.claim(False))
            queued = store.claim(True)
            connection = self.profiles.take(run["id"])
            self.assertEqual(connection["api_key"], self.fields["api_key"])
            self.assertNotIn(self.fields["api_key"], json.dumps(queued))
            with patch("industplatform.codex_agent.execute", return_value="Checked QIF") as execute:
                process_run(store, queued, port=1234, connections=connection)
            execute.assert_called_once()
            self.assertEqual(execute.call_args.kwargs["profile"]["model"], public["model"])
            self.assertEqual(store.get(run["id"])["result"]["agent_summary"], "Checked QIF")

    def test_unsaved_discovery_and_probe_do_not_save_or_expose_credentials(self):
        fields = {**self.fields, "model": "gpt-5.2", "reasoning_effort": "xhigh"}
        with patch("industplatform.model_profiles.discover_models", return_value={"models": [], "truncated": False}) as discover:
            self.assertEqual(self.profiles.discover({**fields, "model": ""}), {"models": [], "truncated": False})
        self.assertEqual(discover.call_args.args[0]["api_key"], fields["api_key"])
        self.assertNotIn("model", discover.call_args.args[0])
        with patch("industplatform.model_profiles.generate", return_value=fields["api_key"]) as generate:
            result = self.profiles.probe(fields)
        self.assertEqual(generate.call_args.args[0]["reasoning_effort"], "xhigh")
        self.assertEqual(result["model"], fields["model"])
        self.assertIs(type(result["latency_ms"]), int)
        self.assertTrue(result["ok"])
        self.assertNotIn(fields["api_key"], json.dumps(result))
        self.assertEqual(self.profiles.catalog()["profiles"], [])

    def test_reasoning_offered_and_validated_for_saved_models(self):
        gpt = self.profiles.save({**self.fields, "model": "gpt-5.2", "reasoning_effort": "xhigh"})
        self.assertIn("xhigh", gpt["reasoning_efforts"])
        self.assertEqual(self.profiles.get(gpt["id"])["reasoning_effort"], "xhigh")
        self.assertEqual(self.profiles.save({**self.fields, "id": gpt["id"], "model": gpt["model"]})["reasoning_effort"], "xhigh")
        with self.assertRaisesRegex(ValueError, "推理强度"):
            self.profiles.save({**self.fields, "model": "gpt-4.1", "reasoning_effort": "high"})
        custom = self.profiles.save({**self.fields, "base_url": "https://gateway.example/v1", "reasoning_effort": "high"})
        self.assertEqual(custom["reasoning_effort"], "high")

    def test_draft_probe_resolves_saved_levels_for_the_matching_model(self):
        provider, _ = self.profiles.save_provider({**self.fields, "models": [
            {"model": "first", "reasoning_level_override": ["low"]},
            {"model": "second", "discovered_reasoning_levels": ["high"]},
        ]})
        fields = {**self.fields, "provider_id": provider["id"], "api_key": "", "model": "second",
                  "reasoning_effort": "high"}
        with patch("industplatform.model_profiles.generate") as generate:
            self.profiles.probe(fields)
            runtime = generate.call_args.args[0]
            self.assertIsNone(runtime["reasoning_level_override"])
            self.assertEqual(runtime["discovered_reasoning_levels"], ["high"])
            self.assertEqual(runtime["reasoning_effort"], "high")
            for changes in ({"model": "first"}, {"reasoning_level_override": []},
                            {"discovered_reasoning_levels": None}):
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "推理强度"):
                    self.profiles.probe({**fields, **changes})
            generate.assert_called_once()

    def test_mixed_protocol_panel_uses_existing_local_verifier(self):
        protocols = ("openai-responses", "anthropic-messages", "gemini-generate-content")
        for tier, protocol in zip(("general", "advanced", "frontier"), protocols):
            self.profiles.save({**self.fields, "protocol": protocol, "model": tier, "tier": tier})
        panel = self.profiles.panel()
        self.assertIn("live", [cohort["id"] for cohort in catalog(self.profiles)["cohorts"]])

        def answer(task, profile):
            return metrology.inspect_csv(task["inputs"]["requirements.csv"], task["inputs"]["measurements.csv"])

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            run = self.profiles.enqueue(store, accept_request({"cohort": "live", "seed": 77}, native_panel=True), panel=panel)
            with patch("industplatform.workflow.solve_with_profile", side_effect=answer):
                process_run(store, store.claim(False), connections=self.profiles.take(run["id"]))
            result = store.get(run["id"])["result"]
        self.assertEqual(result["metrics"]["passed"], 12)
        self.assertEqual({item["provider"] for item in result["evaluations"]}, set(protocols))
        self.assertNotIn(self.fields["api_key"], json.dumps(result))


class FakeVault:
    identifier = "test-vault"

    def __init__(self):
        self.entries = {}
        self.reads = []
        self.write_failure = False
        self.delete_failure = False

    def get(self, reference):
        self.reads.append(reference)
        return self.entries.get(reference)

    def put(self, reference, key):
        if self.write_failure:
            raise CredentialError("Vault write failed")
        self.entries[reference] = key

    def delete(self, reference):
        if self.delete_failure:
            raise CredentialError("Vault delete failed")
        self.entries.pop(reference, None)


class PersistentModelProfilesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "runs.sqlite3"
        self.vault = FakeVault()
        self.profiles = self.reopen()
        self.fields = {"name": "Lab model", "protocol": "openai-responses", "model": "lab-1",
                       "api_key": "test-credential" + "-not-a-real-key"}

    def reopen(self):
        return ModelProfiles(RunStore(self.path), vault=self.vault)

    def test_restart_restores_public_profile_and_resolves_key_only_on_use(self):
        public = self.profiles.save(self.fields)
        restarted = self.reopen()
        self.assertEqual(restarted.catalog()["profiles"], [public])
        self.assertEqual(restarted.catalog()["credential_store"], "test-vault")
        self.assertEqual(self.vault.reads, [])
        with patch("industplatform.model_profiles.generate") as generate:
            restarted.test(public["id"])
        self.assertEqual(generate.call_args.args[0]["api_key"], self.fields["api_key"])
        self.assertEqual(len(self.vault.reads), 1)
        for path in self.path.parent.glob("runs.sqlite3*"):
            self.assertNotIn(self.fields["api_key"].encode(), path.read_bytes())

    def test_saved_key_reused_only_for_same_normalized_address_and_protocol(self):
        public = self.profiles.save(self.fields)
        draft = {**self.fields, "id": public["id"], "api_key": "", "base_url": "HTTPS://API.OPENAI.COM:443/v1/"}
        with patch("industplatform.model_profiles.discover_models") as discover:
            self.profiles.discover({"id": public["id"]})
            self.assertEqual(discover.call_args.args[0]["api_key"], self.fields["api_key"])
            self.vault.reads.clear()
            self.profiles.discover(draft)
            self.assertEqual(discover.call_args.args[0]["api_key"], self.fields["api_key"])
            self.assertEqual(discover.call_args.args[0]["base_url"], "https://api.openai.com/v1")
            self.assertEqual(len(self.vault.reads), 1)
            for change in (
                {"base_url": "https://api.openai.com/v2"},
                {"base_url": "https://example.test/v1"},
                {"protocol": "anthropic-messages"},
            ):
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, "API 密钥"):
                    self.profiles.discover({**draft, **change})
            self.assertEqual(len(self.vault.reads), 1)
            self.profiles.discover({**draft, "api_key": "override-key", "base_url": "https://example.test/v1"})
            self.assertEqual(discover.call_args.args[0]["api_key"], "override-key")
            self.assertEqual(len(self.vault.reads), 1)

    def test_migrates_original_profile_table_and_persists_reasoning(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE model_profiles")
            connection.execute("""
                CREATE TABLE model_profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL,
                    base_url TEXT NOT NULL, model TEXT NOT NULL, tier TEXT NOT NULL,
                    credential_ref TEXT
                )
            """)
            connection.execute("""
                INSERT INTO model_profiles VALUES
                ('original', 'Legacy', 'openai-responses', 'https://api.openai.com/v1', 'gpt-5.2', '', 'original-ref')
            """)
        self.vault.put("original-ref", self.fields["api_key"])
        migrated = self.reopen()
        catalog = migrated.catalog()
        original = catalog["profiles"][0]
        self.assertEqual(original["reasoning_effort"], "")
        self.assertIsNone(original["reasoning_level_override"])
        self.assertIsNone(original["discovered_reasoning_levels"])
        self.assertEqual(original["reasoning_level_source"], "preset")
        self.assertEqual(catalog["providers"][0]["id"], "original")
        self.assertEqual(catalog["providers"][0]["models"], [original])
        self.assertEqual(migrated.get("original")["api_key"], self.fields["api_key"])
        self.assertEqual(list(self.vault.entries), ["original-ref"])
        migrated.save({**original, "reasoning_effort": "xhigh"})
        self.assertEqual(self.reopen().get("original")["reasoning_effort"], "xhigh")

    def test_reasoning_level_metadata_survives_restart_and_legacy_edits(self):
        public = self.profiles.save({**self.fields, "model": "gpt-5.2", "reasoning_level_override": [],
                                     "discovered_reasoning_levels": ["medium"]})
        restarted = self.reopen()
        self.assertEqual(restarted.catalog()["profiles"], [public])
        self.assertEqual(public["reasoning_efforts"], [""])
        self.assertEqual(public["automatic_reasoning_efforts"], ["", "medium"])
        self.assertEqual(public["reasoning_level_source"], "manual")
        fields = {**self.fields, "id": public["id"], "model": public["model"], "api_key": "",
                  "base_url": "HTTPS://API.OPENAI.COM:443/v1/"}
        edited = restarted.save({**fields, "name": "Renamed"})
        self.assertEqual(edited["reasoning_level_override"], [])
        self.assertEqual(edited["discovered_reasoning_levels"], ["medium"])
        with self.assertRaisesRegex(ValueError, "推理强度"):
            restarted.get(public["id"], run_options={"reasoning_effort": "medium"})
        automatic = restarted.save({**fields, "reasoning_level_override": None})
        self.assertEqual(automatic["reasoning_level_source"], "api")
        runtime = restarted.get(public["id"], run_options={"reasoning_effort": "medium"})
        self.assertEqual(runtime["reasoning_effort"], "medium")
        self.assertEqual(runtime["discovered_reasoning_levels"], ["medium"])
        for invalid in ("high", [""], [False], ["unrecognized"]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "推理强度"):
                restarted.save({**fields, "reasoning_level_override": invalid})
        replaced = restarted.save({**fields, "model": "gpt-5.1"})
        self.assertEqual(replaced["id"], public["id"])
        self.assertIsNone(replaced["reasoning_level_override"])
        self.assertIsNone(replaced["discovered_reasoning_levels"])
        self.assertEqual(replaced["reasoning_level_source"], "preset")

    def test_legacy_address_edit_clears_reasoning_metadata_for_all_models(self):
        provider, _ = self.profiles.save_provider({**self.fields, "models": [
            {"model": "first", "reasoning_level_override": ["low"], "reasoning_effort": "low"},
            {"model": "second", "discovered_reasoning_levels": ["high"], "reasoning_effort": "high"},
        ]})
        first, second = provider["models"]
        self.profiles.save({**self.fields, "id": first["id"], "model": first["model"],
                            "base_url": "https://gateway.example/v1"})
        updated = self.reopen().catalog()["providers"][0]
        self.assertEqual([model["id"] for model in updated["models"]], [first["id"], second["id"]])
        for model in updated["models"]:
            self.assertIsNone(model["reasoning_level_override"])
            self.assertIsNone(model["discovered_reasoning_levels"])
            self.assertEqual(model["reasoning_level_source"], "preset")

    def test_rotation_failure_preserves_previous_key_and_removal_retries(self):
        profile_id = self.profiles.save(self.fields)["id"]
        self.vault.write_failure = True
        with self.assertRaises(CredentialError):
            self.profiles.save({**self.fields, "id": profile_id, "api_key": "new-test-credential"})
        self.assertEqual(self.reopen().get(profile_id)["api_key"], self.fields["api_key"])

        self.vault.write_failure = False
        self.profiles.save({**self.fields, "id": profile_id, "api_key": "new-test-credential"})
        self.assertEqual(list(self.vault.entries.values()), ["new-test-credential"])

        self.vault.delete_failure = True
        self.profiles.remove(profile_id)
        self.assertEqual(self.reopen().catalog()["profiles"], [])
        self.vault.delete_failure = False
        self.reopen()
        self.assertEqual(self.vault.entries, {})

    def test_provider_shares_key_and_preserves_each_models_settings(self):
        fields = {**self.fields, "models": [
            {"model": "gpt-5.2", "tier": "frontier", "reasoning_effort": "xhigh"},
            {"model": "gpt-4.1", "tier": "general", "reasoning_effort": ""},
        ]}
        provider, removed = self.profiles.save_provider(fields)
        self.assertEqual(removed, set())
        first, second = provider["models"]
        self.assertEqual(len(self.vault.entries), 1)
        restarted = self.reopen()
        self.assertEqual(restarted.catalog()["providers"], [provider])
        self.assertNotIn(self.fields["api_key"], json.dumps(restarted.catalog()))
        self.assertEqual(restarted.get(first["id"])["reasoning_effort"], "xhigh")
        self.assertEqual(restarted.get(second["id"])["reasoning_effort"], "")
        self.assertEqual(restarted.get(first["id"])["tier"], "frontier")
        self.assertEqual(restarted.get(second["id"])["tier"], "general")

        draft = {**fields, "provider_id": provider["id"], "api_key": ""}
        with patch("industplatform.model_profiles.discover_models") as discover:
            restarted.discover(draft)
            self.assertEqual(discover.call_args.args[0]["api_key"], fields["api_key"])
            with self.assertRaisesRegex(ValueError, "API 密钥"):
                restarted.discover({**draft, "base_url": "https://other.example/v1"})
            discover.assert_called_once()

        with self.assertRaisesRegex(ValueError, "推理强度"):
            restarted.save_provider({**draft, "api_key": "unused-new-key",
                                     "models": [{**second, "reasoning_effort": "high"}]})
        with self.assertRaisesRegex(ValueError, "模型 ID 不能重复"):
            restarted.save_provider({**draft, "models": [{"model": first["model"]}] * 2})
        self.assertEqual(restarted.catalog()["providers"], [provider])
        self.assertEqual(list(self.vault.entries.values()), [fields["api_key"]])

        # The legacy update preserves siblings and rotates the group's single key.
        restarted.save({**first, "api_key": "rotated-test-key"})
        self.assertEqual(restarted.get(second["id"])["api_key"], "rotated-test-key")
        self.assertEqual(list(self.vault.entries.values()), ["rotated-test-key"])
        restarted.remove(first["id"])
        self.assertEqual(restarted.get(second["id"])["api_key"], "rotated-test-key")
        self.assertEqual(list(self.vault.entries.values()), ["rotated-test-key"])
        restarted.remove(provider_id=provider["id"])
        self.assertEqual(restarted.catalog()["providers"], [])
        self.assertEqual(self.vault.entries, {})

    def test_provider_routes_clear_default_only_when_selected_model_is_removed(self):
        settings = Settings(RunStore(self.path))
        handler = object.__new__(RequestHandler)
        handler.server = SimpleNamespace(profiles=self.profiles, settings=settings)
        fields = {**self.fields, "models": [{"model": "first"}, {"model": "second"}]}
        status, created = handler.write_route(PROFILE_PATH, fields)
        self.assertEqual(status, 200)
        provider = created["provider"]
        first, second = provider["models"]
        settings.update({"default_model": first["id"]})
        edit = {**fields, "provider_id": provider["id"], "api_key": "", "models": [first]}
        handler.write_route(PROFILE_PATH, edit)
        self.assertEqual(settings.get()["default_model"], first["id"])
        with self.assertRaises(ValueError):
            self.profiles.get(second["id"])
        edit["models"] = [{"model": "replacement"}]
        _, updated = handler.write_route(PROFILE_PATH, edit)
        replacement = updated["provider"]["models"][0]
        self.assertEqual(settings.get()["default_model"], "")
        self.assertEqual(self.profiles.get(replacement["id"])["api_key"], fields["api_key"])
        settings.update({"default_model": replacement["id"]})
        handler.write_route(PROFILE_PATH + "/remove", {"provider_id": provider["id"]})
        self.assertEqual(settings.get()["default_model"], "")
        self.assertEqual(self.vault.entries, {})

    @patch("industplatform.workflow.codex_agent.executable", return_value="/fixture/codex")
    def test_api_run_effort_is_validated_and_snapshotted_per_turn(self, _):
        provider, _ = self.profiles.save_provider({**self.fields, "models": [
            {"model": "gpt-5.2", "reasoning_effort": "high"},
            {"model": "gpt-5.1", "reasoning_effort": "medium"},
        ]})
        first, second = provider["models"]
        store = RunStore(self.path)
        handler = object.__new__(RequestHandler)
        handler.server = SimpleNamespace(profiles=self.profiles, store=store, settings=Settings(store))
        payload = {"route": "api-agent", "model_profile_id": first["id"], "prompt": "Check QIF"}
        for override, expected in (({}, "high"), ({"reasoning_effort": "xhigh"}, "xhigh"),
                                   ({"reasoning_effort": ""}, "")):
            with self.subTest(override=override):
                status, run = handler.write_route("/api/runs", {**payload, **override})
                self.assertEqual(status, 202)
                connection = self.profiles.take(run["id"])
                self.assertEqual(connection["reasoning_effort"], expected)
                self.assertEqual(connection["api_key"], self.fields["api_key"])
                saved = store.get(run["id"])
                self.assertEqual(saved["model_profile"]["reasoning_effort"], expected)
                self.assertNotIn("reasoning_level_override", saved["model_profile"])
                self.assertNotIn("discovered_reasoning_levels", saved["model_profile"])
                self.assertNotIn(self.fields["api_key"], json.dumps(saved))
        for invalid in (None, False, [], "unknown", " high "):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "推理强度"):
                handler.write_route("/api/runs", {**payload, "reasoning_effort": invalid})
        with self.assertRaisesRegex(ValueError, "推理强度"):
            handler.write_route("/api/runs", {**payload, "model_profile_id": second["id"], "reasoning_effort": "xhigh"})
        self.assertEqual(len(store.recent()), 3)
        self.assertEqual(self.reopen().catalog()["providers"], [provider])

    @patch("industplatform.workflow.codex_agent.executable", return_value="/fixture/codex")
    def test_api_run_multi_agent_is_validated_and_snapshotted_per_turn(self, _):
        provider, _ = self.profiles.save_provider({**self.fields, "models": [
            {"model": "gpt-6.2-sol", "reasoning_effort": "high"},
            {"model": "gpt-6.2-astra", "reasoning_effort": "high"},
        ]})
        sol, astra = provider["models"]
        gateway = self.profiles.save({**self.fields, "base_url": "https://gateway.example/v1",
                                      "model": "gpt-6.2-sol"})
        self.assertTrue(sol["multi_agent_supported"])
        self.assertFalse(astra["multi_agent_supported"])
        self.assertFalse(gateway["multi_agent_supported"])
        model_catalog = self.profiles.catalog()
        self.assertEqual(model_catalog["multi_agent_default_concurrency"], 3)
        self.assertNotIn(self.fields["api_key"], json.dumps(model_catalog))

        store = RunStore(self.path)
        handler = object.__new__(RequestHandler)
        handler.server = SimpleNamespace(profiles=self.profiles, store=store, settings=Settings(store))
        payload = {"route": "api-agent", "model_profile_id": sol["id"], "prompt": "Check QIF"}
        for override, expected in (
            ({}, None),
            ({"multi_agent": {"enabled": False}}, None),
            ({"multi_agent": {"enabled": True}}, {"enabled": True, "max_concurrent_subagents": 3}),
            ({"multi_agent": {"enabled": True, "max_concurrent_subagents": 1}},
             {"enabled": True, "max_concurrent_subagents": 1}),
        ):
            with self.subTest(override=override):
                status, run = handler.write_route("/api/runs", {**payload, **override})
                self.assertEqual(status, 202)
                connection = self.profiles.take(run["id"])
                self.assertEqual(connection["api_key"], self.fields["api_key"])
                self.assertEqual(connection["reasoning_effort"], "high")
                self.assertEqual(connection.get("multi_agent"), expected)
                saved = store.get(run["id"])
                self.assertEqual(saved["model_profile"], run["model_profile"])
                self.assertEqual(saved["model_profile"]["reasoning_effort"], "high")
                if expected is None:
                    self.assertNotIn("multi_agent", saved["model_profile"])
                    self.assertNotIn("multi_agent", connection)
                else:
                    self.assertEqual(saved["model_profile"]["multi_agent"], expected)
                self.assertNotIn("multi_agent_supported", saved["model_profile"])
                self.assertNotIn(self.fields["api_key"], json.dumps(saved))

        for invalid in (None, True, {}, {"enabled": "yes"}, {"enabled": False, "max_concurrent_subagents": -1},
                        {"enabled": True, "api_key": self.fields["api_key"]},
                        *({"enabled": True, "max_concurrent_subagents": value}
                          for value in (0, 1.5, True, "2"))):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                handler.write_route("/api/runs", {**payload, "multi_agent": invalid})
        for excluded in (astra, gateway):
            with self.subTest(model=excluded), self.assertRaises(ValueError):
                handler.write_route("/api/runs", {**payload, "model_profile_id": excluded["id"],
                                                  "multi_agent": {"enabled": True}})
        for route in ("demo-cnc", "codex-agent"):
            with self.subTest(route=route), self.assertRaisesRegex(ValueError, "仅 API Agent"):
                handler.write_route("/api/runs", {**payload, "route": route, "multi_agent": {"enabled": False}})
        self.assertEqual(len(store.recent()), 4)
        self.assertNotIn("multi_agent", self.reopen().get(sol["id"]))
        self.assertEqual(self.reopen().catalog(), model_catalog)

    def test_legacy_deepseek_effort_is_canonicalized_when_read(self):
        profile = self.profiles.save({**self.fields, "base_url": "https://api.deepseek.com",
                                      "model": "deepseek-v4-pro", "reasoning_effort": "xhigh"})
        self.assertEqual(profile["reasoning_effort"], "high")
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE model_profiles SET reasoning_effort = 'medium' WHERE id = ?", (profile["id"],))
        reopened = self.reopen()
        self.assertEqual(reopened.catalog()["profiles"], [profile])
        self.assertEqual(profile["reasoning_efforts"], ["", "none", "low", "high", "max"])
        self.assertEqual(reopened.get(profile["id"])["reasoning_effort"], "high")
        with self.assertRaisesRegex(ValueError, "推理强度"):
            reopened.get(profile["id"], run_options={"reasoning_effort": "medium"})

    def test_corrects_saved_deepseek_tiers_once_and_keeps_opt_out(self):
        fields = {**self.fields, "base_url": "https://api.deepseek.com"}
        flash = self.profiles.save({**fields, "model": "deepseek-flash"})
        pro = self.profiles.save({**fields, "model": "deepseek-v4-pro"})
        opt_out = self.profiles.save({**fields, "model": "deepseek-flash", "tier": ""})
        unrelated = self.profiles.save({**self.fields, "model": "deepseek-v4-pro", "tier": "advanced"})
        self.assertEqual((flash["tier"], pro["tier"]), ("general", "frontier"))
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM global_settings WHERE key='model_profiles/deepseek_tier_defaults_v1'")
            db.executemany("UPDATE model_profiles SET tier=? WHERE id=?",
                           [("advanced", flash["id"]), ("general", pro["id"])])
        reopened = self.reopen()
        self.assertEqual(reopened.catalog()["profiles"], [flash, pro, opt_out, unrelated])
        reopened.save({**fields, "id": flash["id"], "model": "deepseek-flash", "tier": ""})
        self.assertEqual(self.reopen().catalog()["profiles"][0]["tier"], "")

    def test_unavailable_vault_supports_keyless_local_profiles(self):
        self.vault = UnavailableVault()
        profiles = self.reopen()
        with self.assertRaises(CredentialError):
            profiles.save(self.fields)
        self.assertEqual(profiles.catalog()["profiles"], [])

        public = profiles.save({**self.fields, "base_url": "http://127.0.0.1:1234/v1", "api_key": ""})
        restarted = self.reopen()
        self.assertFalse(public["has_key"])
        self.assertEqual(restarted.catalog()["profiles"], [public])
        self.assertEqual(restarted.catalog()["credential_store"], "unavailable")
        self.assertEqual(restarted.get(public["id"])["api_key"], "")

    def test_last_model_can_be_removed_without_deleting_provider(self):
        provider, _ = self.profiles.save_provider({**self.fields, "models": [{"model": "worker-v1"}]})
        model = provider["models"][0]
        store = RunStore(self.path)
        settings = Settings(store)
        handler = SimpleNamespace(server=SimpleNamespace(store=store, profiles=self.profiles, settings=settings),
                                  read_settings=lambda: {"preferences": settings.get()})
        RequestHandler.write_route(handler, "/api/settings", {
            "default_model": model["id"], "default_worker_model": model["id"],
        })
        RequestHandler.write_route(handler, PROFILE_PATH + "/model", {"id": model["id"], "action": "remove"})
        reopened = self.reopen()
        self.assertEqual(reopened.catalog()["providers"], [{**provider, "models": []}])
        self.assertEqual(settings.get()["default_worker_model"], "")
        self.assertEqual(settings.get()["default_model"], "")
        with patch("industplatform.model_profiles.discover_models", return_value={"models": []}) as discover:
            self.assertTrue(reopened.test_provider(provider["id"])["ok"])
        self.assertEqual(discover.call_args.args[0]["api_key"], self.fields["api_key"])
        reopened.remove(provider_id=provider["id"])
        self.assertEqual(self.vault.entries, {})

    def test_disabled_models_can_be_tested_but_do_not_take_new_runs(self):
        saved = self.profiles.save({**self.fields, "model": "worker-v1", "tier": "general",
                                    "capabilities": {"vision": True, "parallel_tool_calls": True, "context_window": 96000}})
        self.profiles.set_enabled(saved["id"], False)
        restarted = self.reopen()
        model = restarted.catalog()["profiles"][0]
        self.assertFalse(model["enabled"])
        self.assertTrue(model["parallel_tools_supported"])
        self.assertTrue(model["vision_supported"])
        with self.assertRaisesRegex(ValueError, "停用"):
            restarted.get(model["id"])
        with patch("industplatform.model_profiles.generate", return_value="OK"):
            self.assertTrue(restarted.test(model["id"])["ok"])
        restarted.set_enabled(model["id"], True)
        self.assertEqual(restarted.get(model["id"])["capabilities"]["context_window"], 96000)

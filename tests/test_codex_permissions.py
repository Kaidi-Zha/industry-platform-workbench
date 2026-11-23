import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from industplatform import codex_gateway, codex_permissions, provider_api
from industplatform.model_profiles import ModelProfiles
from industplatform.server import RequestHandler
from industplatform.settings import Settings
from industplatform.storage import RunStore


class CodexPermissionsTest(unittest.TestCase):
    def test_legacy_defaults_and_workspace_permissions(self):
        self.assertEqual(codex_permissions.cli_permissions(None), (
            "read-only", "never",
            {"approvals_reviewer": "user", "sandbox_workspace_write.network_access": False},
        ))
        options = {"sandbox_mode": "workspace-write", "workspace_network_access": True, "approval_mode": "auto_review"}
        sandbox, policy, config = codex_permissions.cli_permissions(options)
        self.assertEqual((sandbox, policy), ("workspace-write", "on-request"))
        self.assertTrue(config["features.guardian_approval"])
        self.assertTrue(config["sandbox_workspace_write.network_access"])
        self.assertEqual(config["approvals_reviewer"], "auto_review")
        _, _, config = codex_permissions.cli_permissions({**options, "sandbox_mode": "read-only"})
        self.assertFalse(config["sandbox_workspace_write.network_access"])
        _, manual_policy, manual = codex_permissions.cli_permissions({"approval_mode": "ask"})
        self.assertEqual((manual_policy, manual["approvals_reviewer"]), ("on-request", "user"))

    def test_catalog_keeps_main_model_and_uses_hidden_review_alias(self):
        catalog = [
            {"slug": "luna", "display_name": "Luna", "visibility": "list", "use_responses_lite": True},
            {"slug": "gpt-astra", "display_name": "Astra", "visibility": "list", "use_responses_lite": True},
            {"slug": "codex-auto-review", "display_name": "Reviewer", "visibility": "hide",
             "use_responses_lite": False, "model_messages": {"auto_review": {"policy": "fixture"}}},
        ]
        with patch.object(codex_permissions, "bundled_models", return_value=catalog):
            self.assertEqual(codex_permissions.native_review_models("codex"), [{"id": "luna", "name": "Luna"}])
            native = codex_permissions.review_catalog("codex", None, "luna")
            self.assertTrue(all(model["auto_review_model_override"] == "luna" for model in native["models"]))
            translated = codex_permissions.review_catalog("codex", "provider-main", "provider-review")
        entries = {model["slug"]: model for model in translated["models"]}
        alias = codex_permissions.REVIEW_ALIAS
        self.assertEqual(entries["provider-main"]["auto_review_model_override"], alias)
        self.assertEqual(entries[alias]["visibility"], "hide")
        self.assertFalse(entries[alias]["use_responses_lite"])
        self.assertEqual(entries[alias]["model_messages"], catalog[-1]["model_messages"])
        self.assertNotIn("auto_review_model_override", catalog[0])

    def test_gateway_sends_only_guardian_requests_to_selected_profile(self):
        main = {"model": "primary", "protocol": provider_api.OPENAI, "api_key": "primary-fixture"}
        reviewer = {"model": "reviewer", "protocol": provider_api.GEMINI, "api_key": "review-fixture",
                    "multi_agent": {"enabled": True}}
        gateway = codex_gateway.Gateway(main, None, 5, reviewer=reviewer)
        alias = codex_permissions.REVIEW_ALIAS
        self.assertIs(gateway.request_model({"model": "primary"}, {})[0], gateway)
        with self.assertRaises(provider_api.ProviderError):
            gateway.request_model({"model": alias}, {})
        target, payload = gateway.request_model(
            {"model": alias, "client_metadata": {"x-openai-subagent": "guardian"}}, {},
        )
        self.assertIs(target, gateway.reviewer)
        self.assertEqual(payload["model"], "reviewer")
        self.assertEqual(target.profile["protocol"], provider_api.GEMINI)
        self.assertEqual(target.profile["api_key"], "review-fixture")
        self.assertNotIn("multi_agent", target.profile)
        self.assertNotIn("review-fixture", gateway.environment().values())

    @patch("industplatform.workflow.codex_agent.executable", return_value="/fixture/codex")
    def test_gui_settings_are_snapshotted_without_reviewer_secrets(self, _):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            settings = Settings(store)
            profiles = ModelProfiles(None)
            fields = {"protocol": provider_api.OPENAI, "base_url": "https://example.test/v1",
                      "api_key": "reviewer-fixture-key", "name": "Fixture"}
            primary = profiles.save({**fields, "model": "primary", "api_key": "primary-fixture-key"})
            reviewer = profiles.save({**fields, "model": "reviewer"})
            astra = profiles.save({**fields, "model": "gpt-6-astra"})
            handler = object.__new__(RequestHandler)
            handler.server = SimpleNamespace(profiles=profiles, store=store, settings=settings)
            handler.write_route("/api/settings", {"sandbox_mode": "workspace-write", "approval_mode": "auto_review",
                                                  "approval_model_api": reviewer["id"]})
            self.assertRaisesRegex(ValueError, "非 Astra", handler.write_route, "/api/settings",
                                   {"approval_model_api": astra["id"]})
            payload = {"route": "api-agent", "model_profile_id": primary["id"], "prompt": "Inspect"}
            status, run = handler.write_route("/api/runs", payload)
            self.assertEqual(status, 202)
            self.assertEqual(run["agent_options"]["approval_model_label"], "reviewer")
            self.assertEqual(run["agent_options"]["sandbox_mode"], "workspace-write")
            self.assertNotIn("fixture-key", json.dumps(store.get(run["id"])))
            snapshot = profiles.take(run["id"])
            self.assertEqual(snapshot["approval_profile"]["api_key"], "reviewer-fixture-key")
            _, ask = handler.write_route("/api/runs", {**payload, "approval_mode": "ask"})
            self.assertEqual(store.get(ask["id"])["agent_options"]["approval_mode"], "ask")
            self.assertEqual(settings.get()["approval_mode"], "auto_review")
            self.assertNotIn("approval_profile", profiles.take(ask["id"]))
            with self.assertRaisesRegex(ValueError, "包含本次连接的密钥"):
                handler.write_route("/api/runs", {**payload, "prompt": "reviewer-fixture-key"})
            handler.write_route("/api/model-profiles/remove", {"id": reviewer["id"]})
            self.assertEqual(settings.get()["approval_model_api"], "")
            self.assertEqual(run["agent_options"]["approval_model_api"], reviewer["id"])

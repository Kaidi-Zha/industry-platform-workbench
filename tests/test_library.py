import tempfile
import unittest
from pathlib import Path

from industplatform import library
from industplatform.metrology import demo_asset
from industplatform.storage import API_AGENT_ROUTE, RunStore


class LibraryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RunStore(Path(directory.name) / "runs.sqlite3")

    def save_run(self, route, **result):
        run = self.store.create(route, "offline", "")
        run["status"] = "completed"
        run["result"].update(result)
        self.store.update(run)
        return run

    def test_sources_keep_best_evidence_and_datasets_require_proof(self):
        asset = {
            "id": "sample-123", "title": "Pipe Inspection", "version": "v1",
            "source": "https://example.com/inspection", "license": "CC-BY-4.0",
            "license_source": "https://example.com/license", "status": "candidate",
        }
        evidence = {
            "file": "pipe.csv", "fields": ["wall_thickness"], "sha256": "a" * 64,
            "bytes": 32, "rows": 1, "url": "https://example.com/pipe.csv",
        }
        sampled = self.save_run("zenodo", assets=[dict(asset, status="sampled", evidence=evidence)])
        self.save_run("zenodo", assets=[
            asset,
            dict(asset, id="sample-456", status="sampled", evidence={"file": "unverified.csv"}),
            dict(asset, id="sample-789", version="v2"),
        ])
        self.save_run("demo-cnc", assets=[demo_asset(), {"id": "unknown", "status": "accepted"}])

        sources = library.read(self.store, "sources", "inspection")
        self.assertEqual(sources["total"], 3)
        sampled_source = next(row for row in sources["items"] if row["status"] == "sampled")
        self.assertEqual(sampled_source["details"]["observations"], 2)
        self.assertEqual(sampled_source["run_id"], sampled["id"])

        datasets = library.read(self.store, "datasets")
        self.assertEqual([row["status"] for row in datasets["items"]], ["synthetic", "sampled"])
        row = datasets["items"][1]
        self.assertEqual(row["details"]["evidence"], evidence)
        self.assertIn({"label": "许可依据", "url": asset["license_source"]}, row["links"])
        self.assertEqual(library.read(self.store, "datasets", "WALL_THICKNESS")["total"], 1)

    def test_public_tasks_and_agent_provenance(self):
        child = self.save_run(
            "demo-cnc",
            tasks=[{
                "id": "case-1", "title": "Test", "description": "Scenario\n\nPublic contract",
                "inputs": {"sample.csv": "public,data"}, "reference": {"answer": "hidden"},
            }],
            evaluations=[{"task_id": "case-1", "passed": True, "status": "scored", "answer": "hidden"}],
            api_key="hidden",
        )
        parent = self.save_run(API_AGENT_ROUTE, agent_run_ids=[child["id"]])
        row = library.read(self.store, "tasks")["items"][0]
        self.assertEqual(row["conversation_id"], parent["id"])
        self.assertEqual(row["details"]["agent_run_id"], parent["id"])
        self.assertEqual(row["run_id"], child["id"])
        self.assertTrue(row["agent_triggered"])
        self.assertTrue(row["exportable"])
        self.assertTrue(row["links"][0]["url"].endswith("/benchmark"))
        self.assertEqual(row["details"]["inputs"], {"sample.csv": "public,data"})
        self.assertNotIn("hidden", str(row))

        parent["conversation_id"] = "persistent-conversation"
        self.store.update(parent)
        self.assertEqual(library.read(self.store, "tasks")["items"][0]["conversation_id"], "persistent-conversation")

    def test_archive_pagination_and_validation(self):
        for number in range(30):
            self.save_run("demo-cnc", tasks=[{"id": "case", "title": "Item {}".format(number)}])
        page = library.read(self.store, "tasks", query="ITEM", offset=26, limit=2)
        self.assertEqual(page["total"], 30)
        self.assertEqual([row["title"] for row in page["items"]], ["Item 3", "Item 2"])
        self.assertEqual(page["offset"], 26)
        self.assertEqual(page["limit"], 2)
        for args in ({"offset": -1}, {"limit": True}, {"limit": 101}, {"query": "a" * 281}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                library.read(self.store, "tasks", **args)


if __name__ == "__main__":
    unittest.main()

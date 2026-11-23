import tempfile
import unittest
from pathlib import Path

from industplatform import collection
from industplatform.storage import API_AGENT_ROUTE, RunStore


class CollectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RunStore(Path(directory.name) / "runs.sqlite3")
        self.scope = collection.plan({
            "domain": "CNC", "scenario": "首件检验", "region": "不限", "time_window": "2025 年",
            "stages": "S5", "asset_types": "检测记录", "deliverables": "匿名记录及原文",
        })

    def test_restart_preserves_plan_children_and_references(self):
        owner = self.store.create(API_AGENT_ROUTE, "offline", "首件", plan=self.scope)
        self.store.claim()
        child_options = {"parent_run_id": owner["id"]}
        child = self.store.create("demo-cnc", "offline", "样例", 15, **child_options)
        self.store.claim()
        reference = {
            "domain": "CNC", "title": "首件检验", "kind": "标准", "publisher": "测试",
            "url": "https://example.test/cnc", "locator": "第 2 页", "claim": "记录要求",
        }
        first = collection.save_reference(self.store, reference, owner["id"])
        self.assertEqual(first, collection.save_reference(self.store, reference, owner["id"]))
        with self.assertRaises(ValueError):
            collection.save_reference(self.store, {**reference, "domain": "PCB"}, owner["id"])
        legacy = self.store.create(API_AGENT_ROUTE, "offline", "旧任务")
        self.store.claim()

        store = RunStore(self.store.path)
        store.recover()
        resumed = store.claim(True)
        self.assertEqual((resumed["id"], resumed["plan"], resumed["restarts"]), (owner["id"], self.scope, 1))
        self.assertEqual(store.create("demo-cnc", "offline", "样例", 15, **child_options)["id"], child["id"])
        self.assertEqual(store.claim(False)["id"], child["id"])
        self.assertEqual(store.get(legacy["id"])["status"], "needs_review")
        results = collection.references(store, domain="CNC", query="第 2 页", run_id=owner["id"])
        self.assertEqual(results["items"], [{**first, "run_ids": [owner["id"]]}])
        self.assertFalse(collection.references(store, domain="PCB")["items"])

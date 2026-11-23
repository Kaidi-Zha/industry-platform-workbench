import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from industplatform import metrology
from industplatform.storage import RunStore
from industplatform.workflow import accept_request, benchmark_zip, process_run


class OfflineWorkflowTest(unittest.TestCase):
    def test_seeded_run_and_public_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            original = store.create(*accept_request({"seed": 77}))
            processing = store.claim()
            process_run(store, processing)
            run = store.get(original["id"])

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["result"]["metrics"]["passed"], 4)
        self.assertTrue(all(check["passed"] for check in run["result"]["tool"]["checks"]))
        self.assertEqual(metrology.make_tasks(77), metrology.make_tasks(77))
        self.assertNotEqual(metrology.make_tasks(77), metrology.make_tasks(78))

        with ZipFile(io.BytesIO(benchmark_zip(run))) as bundle:
            manifest = json.loads(bundle.read("benchmark.json"))
            self.assertEqual(manifest["seed"], 77)
            self.assertEqual(len(manifest["tasks"]), 4)
            for task in manifest["tasks"]:
                self.assertNotIn("reference", task)
                for file in task["inputs"].values():
                    self.assertEqual(hashlib.sha256(bundle.read(file["path"])).hexdigest(), file["sha256"])

    def test_golden_and_wrong_answers(self):
        for task in metrology.make_tasks(77):
            with self.subTest(task=task["id"]):
                self.assertTrue(metrology.verify_answer(task, task["reference"])["passed"])
                answer = dict(task["reference"], status="pass" if task["reference"]["status"] != "pass" else "fail")
                self.assertFalse(metrology.verify_answer(task, answer)["passed"])

    def test_queue_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            interrupted = store.create("demo-cnc", "offline", "")
            next_run = store.create("demo-cnc", "offline", "")
            self.assertEqual(store.claim()["id"], interrupted["id"])
            store.recover()
            self.assertEqual(store.get(interrupted["id"])["status"], "needs_review")
            self.assertEqual(store.claim()["id"], next_run["id"])


if __name__ == "__main__":
    unittest.main()

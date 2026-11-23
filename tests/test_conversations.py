import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from industplatform.server import chat_turn
from industplatform.storage import API_AGENT_ROUTE, RunStore
from industplatform.workflow import _agent_prompt


class ConversationsTest(unittest.TestCase):
    def test_append_and_load_scoped_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            initial = store.create(API_AGENT_ROUTE, "offline", "Find source A")
            other = store.create(API_AGENT_ROUTE, "offline", "Find source B")
            child = store.create("demo-cnc", "offline", "")
            with self.assertRaisesRegex(ValueError, "本轮 Agent"):
                store.create(API_AGENT_ROUTE, "offline", "Follow up", conversation_id=initial["conversation_id"])

            initial["result"].update(agent_summary="Result A", agent_run_ids=[child["id"]])
            initial["status"] = "completed"
            store.update(initial)
            follow = store.create(API_AGENT_ROUTE, "offline", "Read the file", conversation_id=initial["id"])
            self.assertEqual([run["id"] for run in store.conversation(initial["id"])], [initial["id"], follow["id"]])
            self.assertEqual([row["id"] for row in store.conversations()], [initial["id"], other["id"]])
            prompt, child_ids = _agent_prompt(store, follow)
            self.assertIn("Read the file", prompt)
            self.assertIn("Result A", prompt)
            self.assertNotIn("Find source B", prompt)
            self.assertEqual(child_ids, [child["id"]])

    def test_migrates_older_agent_runs_without_merging_other_records(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "runs.sqlite3"
            previous = {"id": "1" * 32, "route": API_AGENT_ROUTE, "prompt": "Earlier conversation", "status": "completed"}
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT, sequence INTEGER, body TEXT, route TEXT)")
                connection.execute("INSERT INTO runs VALUES (?, ?, 1, ?, ?)",
                                   (previous["id"], previous["status"], json.dumps(previous), previous["route"]))
            store = RunStore(db)
            self.assertEqual(store.conversations()[0]["title"], "Earlier conversation")
            self.assertEqual(store.conversation(previous["id"]), [previous])

    def test_chat_only_returns_task_identity_and_title(self):
        task = {"id": "task-1", "title": "Unit check", "inputs": {"file": "csv data"}, "reference": {"value": "answer"}}
        turn = chat_turn({"id": "run-1", "result": {"tasks": [task], "agent_summary": "Created a task"}})
        self.assertEqual(turn["result"]["tasks"], [{"id": "task-1", "title": "Unit check"}])


class PlanAndControlTest(unittest.TestCase):
    scope = {"domain": "Machining", "scenario": "CNC/CMM first article", "stages": "S5", "region": "US",
             "time_window": "Current", "asset_types": "Reports", "sources": "", "deliverables": "FAI",
             "open_questions": ""}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RunStore(Path(directory.name) / "runs.sqlite3")

    def test_revision_while_running_is_persistent_and_applies_to_future_turn(self):
        original = self.store.create(API_AGENT_ROUTE, "offline", "Initial", plan=self.scope, public_research=True)
        running = self.store.claim()
        other = {**self.scope, "region": "EU"}
        self.assertEqual(original["plan_version"], 1)
        with self.assertRaisesRegex(ValueError, "修订理由"):
            self.store.confirm_plan(original["id"], other, "research", "", 1)
        confirmed = self.store.confirm_plan(original["id"], other, "research", "Add European suppliers", 1)
        self.assertEqual(confirmed["version"], 2)
        with self.assertRaisesRegex(ValueError, "新版"):
            self.store.confirm_plan(original["id"], self.scope, "research", "Late edit", 1)
        self.assertEqual(self.store.get(original["id"])["plan"]["region"], "US")

        reopened = RunStore(self.store.path)
        self.assertEqual(reopened.plan_history(original["id"])[-1], confirmed)
        running["status"] = "completed"
        reopened.update(running)
        with self.assertRaisesRegex(ValueError, "新版"):
            reopened.create(API_AGENT_ROUTE, "offline", "Stale", conversation_id=original["id"], plan_version=1)
        follow = reopened.create(API_AGENT_ROUTE, "offline", "Follow up", conversation_id=original["id"])
        self.assertEqual(follow["plan"], other)
        self.assertEqual(follow["plan_version"], 2)
        self.assertEqual(follow["plan_reason"], "Add European suppliers")
        self.assertTrue(follow["research"])

    def test_stop_cascades_and_resume_waits_for_running_child(self):
        from industplatform.storage import RunInterrupted

        first = self.store.create(API_AGENT_ROUTE, "offline", "Pause", plan=self.scope)
        running = self.store.claim(True)
        other = self.store.create(API_AGENT_ROUTE, "offline", "Continue separately", plan=self.scope)
        child = self.store.create("demo-cnc", "offline", "", parent_run_id=first["id"])
        running_child = self.store.claim(False)
        parent, stopped, _ = self.store.control(first["id"], "pause")
        self.assertEqual(parent["status"], "pausing")
        self.assertEqual(set(stopped), {first["id"], child["id"]})
        with self.assertRaises(RunInterrupted):
            self.store.update(running)
        self.store.settle_control(running)
        self.assertEqual(self.store.get(first["id"])["status"], "pausing")
        with self.assertRaises(ValueError):
            self.store.control(first["id"], "resume")
        self.store.settle_control(running_child)
        self.assertEqual(self.store.get(first["id"])["status"], "paused")
        self.assertEqual(self.store.get(other["id"])["status"], "queued")
        self.store.recover()
        self.assertEqual(self.store.get(first["id"])["status"], "paused")
        resumed, _, _ = self.store.control(first["id"], "resume")
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["restarts"], 1)
        self.assertEqual(self.store.get(child["id"])["status"], "queued")
        cancelled, _, _ = self.store.control(first["id"], "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.store.get(child["id"])["status"], "cancelled")
        self.assertEqual(self.store.claim(True)["id"], other["id"])

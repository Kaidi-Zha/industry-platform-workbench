import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from industplatform import frontier, workbench
from industplatform.storage import RunStore


class FrontierTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "queue.sqlite3"
        self.store = RunStore(self.path)
        self.domain = workbench.add_domain(self.store, {"name": "首件检验"})["id"]
        self.node = workbench.add_process_node(self.store, {"domain_id": self.domain, "title": "量具校准"})["id"]
        frontier.start(self.store, self.domain, self.node)

    @patch("industplatform.frontier.public_web.search", return_value={"scope": "open_web", "results": []})
    def test_three_completed_waves_are_required(self, query):
        for _ in range(41):
            self.assertTrue(frontier.run_one(self.store))
        self.assertFalse(frontier.status(self.store, self.domain, self.node)["exhausted"])
        reopened = RunStore(self.path)
        self.assertTrue(frontier.run_one(reopened))
        result = frontier.status(reopened, self.domain, self.node)
        self.assertTrue(result["exhausted"])
        self.assertTrue(all(row["closed_at"] for row in result["rounds"]))
        self.assertFalse(frontier.run_one(reopened))
        self.assertEqual(query.call_count, 42)

    @patch("industplatform.frontier.public_web.search", return_value={"scope": "publications", "results": []})
    def test_publication_index_cannot_complete_other_channels(self, _):
        for _ in range(14):
            self.assertTrue(frontier.run_one(self.store))
        result = frontier.status(self.store, self.domain, self.node)
        self.assertEqual((result["status"], result["counts"]["blocked"]), ("blocked", 12))
        self.assertFalse(result["exhausted"])
        self.assertFalse(frontier.run_one(self.store))

    def test_expired_lease_is_recovered(self):
        lease = frontier.claim(self.store)
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE crawl_frontier SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id = ?", (lease[2],))
            db.execute("UPDATE crawl_frontier SET attempts=2 WHERE id<>?", (lease[2],))
        renewed = frontier.claim(RunStore(self.path))
        self.assertEqual(renewed[2], lease[2])
        self.assertNotEqual(renewed[-1], lease[-1])

    def test_url_is_unique_across_channels_nodes_and_rounds(self):
        sibling = workbench.add_process_node(self.store, {"domain_id": self.domain, "title": "量具校准复查"})["id"]
        frontier.start(self.store, self.domain, sibling)
        data = {"scope": "open_web", "results": [
            {"url": "https://example.org/calibration/?utm_source=provider&revision=2#section",
             "title": "量具校准方法", "snippet": ""},
            {"url": "https://arxiv.org/abs/2412.12345", "title": "量具校准 preprint", "snippet": ""},
            {"url": "https://example.org/lodging", "title": "旅店住宿优惠", "snippet": ""},
        ]}
        scope = (self.domain, self.node)
        with closing(self.store.connect()) as db, db:
            first = frontier.enqueue_urls(db, scope, 1, "C3_TOOL_FORMAT_DOC", "量具校准", data)
            self.assertEqual([(item["title"], item["queued"]) for item in first], [("量具校准方法", True)])
            papers = frontier.enqueue_urls(db, scope, 1, "C5_ASSOC_WHITEPAPER", "量具校准", data)
            self.assertTrue(next(item for item in papers if "arxiv.org" in item["url"])["queued"])
            same = {"scope": "open_web", "results": [
                {"url": "https://EXAMPLE.org/calibration?revision=2&gclid=abc", "title": "量具校准方法"},
                {"url": "https://example.org/calibration?revision=3", "title": "量具校准方法"},
            ]}
            again = frontier.enqueue_urls(db, (self.domain, sibling), 1, "C4_EQUIP_MAT_VENDOR", "量具校准", same)
            self.assertEqual([item["queued"] for item in again], [False, True])
            self.assertEqual(db.execute("""SELECT normalized_url,channel_id FROM crawl_frontier
                WHERE kind='url' ORDER BY normalized_url""").fetchall(), [
                    ("https://arxiv.org/abs/2412.12345", "C5_ASSOC_WHITEPAPER"),
                    ("https://example.org/calibration?revision=2", "C3_TOOL_FORMAT_DOC"),
                    ("https://example.org/calibration?revision=3", "C4_EQUIP_MAT_VENDOR"),
                ])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("""UPDATE crawl_frontier SET normalized_url='https://example.org/calibration?revision=2'
                    WHERE normalized_url='https://example.org/calibration?revision=3'""")

    def test_migration_and_library_merge_keep_one_url(self):
        legacy_schema = self.path.parent / "legacy.sql"
        legacy_schema.write_text(workbench.SCHEMA.read_text().replace("    normalized_url TEXT,\n", ""))
        path = self.path.parent / "old.sqlite3"
        with patch.object(workbench, "SCHEMA", legacy_schema), patch.object(frontier, "initialize"):
            old = RunStore(path)
        source = workbench.add_domain(old, {"name": "PCB 样例"})["id"]
        target = workbench.add_domain(old, {"name": "PCB 制造"})["id"]
        nodes = [workbench.add_process_node(old, {"domain_id": domain, "title": "Gerber 导出"})["id"]
                 for domain in (source, target)]
        for domain, node in zip((source, target), nodes):
            frontier.start(old, domain, node)
        with closing(old.connect()) as db, db:
            db.executemany("INSERT INTO crawl_channels VALUES (?,?)", [("git", "开源工程"), ("official", "旧版官方样例")])
            db.executemany("""INSERT INTO crawl_frontier
                (domain_id,node_id,id,round_number,channel_id,kind,target,state) VALUES (?,?,?,1,?,'url',?,?)""", [
                    (source, nodes[0], "original", "git", "https://example.org/gerber", "done"),
                    (source, nodes[0], "duplicate", "official", "https://example.org/gerber/?utm_medium=web", "queued"),
                    (target, nodes[1], "after-merge", "git", "https://example.org/gerber/", "queued"),
                ])
        store = RunStore(path)
        with closing(store.connect()) as db:
            self.assertEqual(db.execute("SELECT id FROM crawl_frontier WHERE kind='url' ORDER BY domain_id").fetchall(),
                             [("original",), ("after-merge",)])
        workbench.domain_libraries.manage(store, {"action": "merge", "id": source, "target_id": target})
        with closing(RunStore(path).connect()) as db:
            self.assertEqual(db.execute("SELECT id,domain_id,state FROM crawl_frontier WHERE kind='url'").fetchall(),
                             [("original", target, "done")])

    def test_search_host_operator_and_author_citation_do_not_count_as_topic(self):
        domain = workbench.add_domain(self.store, {"name": "PCB Gerber 制造交付"})["id"]
        node = workbench.add_process_node(self.store, {"domain_id": domain, "title": "板厂 CAM 入厂核对"})["id"]
        search = {"scope": "open_web", "results": [
            {"url": "https://export.arxiv.org/pdf/2203.00678", "title": "* Gerber _et al",
             "snippet": "Gerber _et al._ (2017)S. Gerber, S.-L. Yang, D. Zhu, H. Soifer, J."},
            {"url": "https://arxiv.org/html/2502.13255v1", "title": "PCB Renewal",
             "snippet": "We convert all remaining unique nets into polygons for comparison."},
        ]}
        with closing(self.store.connect()) as db:
            matches = list(frontier.candidates(db, (domain, node),
                "site:arxiv.org printed circuit board Gerber fabrication CAM design rule check", "C5_ASSOC_WHITEPAPER", search))
        self.assertEqual([item["title"] for item, _ in matches], ["PCB Renewal"])

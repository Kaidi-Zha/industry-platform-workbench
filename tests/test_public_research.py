import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from industplatform import collection, public_web
from industplatform.research import Research
from industplatform.storage import API_AGENT_ROUTE, RunStore


class PublicResearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RunStore(Path(directory.name) / "runs.sqlite3")
        scope = collection.plan({
            "domain": "机械制造", "scenario": "CNC 首件检验", "region": "不限", "time_window": "不限",
            "stages": "S5", "asset_types": "计量和供方条件", "deliverables": "检验报告",
        })
        self.run = self.store.create(API_AGENT_ROUTE, "offline", "查 CMM", plan=scope, public_research=True)
        self.run = self.store.claim(True)
        self.study = Research(self.store, self.run)

    def test_source_receipt_and_relation_provenance(self):
        url = "https://supplier.example.org/terms"
        headers = Message()
        headers["Content-Type"] = "text/html; charset=UTF-8"
        body = b"<h1>Supply conditions</h1><script>ignored text</script><p>Suppliers must submit 2 FAIRs per batch.</p>"
        with patch.object(public_web, "search", return_value={
            "query": "FAIR handoff", "backend": "searxng", "scope": "open_web", "results": [{"url": url}],
        }), patch.object(public_web, "fetch", return_value=(url, headers, body)):
            self.study.search({"query": "FAIR handoff"})
            receipt = self.study.read({"url": url, "focus": "FAIR"})
        block = receipt["passages"][0]
        self.assertNotIn("ignored text", block["text"])
        args = {"source_id": receipt["id"], "target_id": "handoff", "locator": block["locator"],
                "quote": "Suppliers must submit 2 FAIRs per batch.", "level": "document", "permission": "unknown",
                "title": "Supply conditions", "publisher": "Supplier council", "kind": "供应商规范",
                "claim": "要求提供两份报告", "relation_type": "supplier_requirement", "from_party": "供应方",
                "to_party": "采购方", "artifact": "FAIR", "applicability": "本条款"}
        for changes in (
            {"quote": "Everyone must submit 5 FAIRs per batch."},
            {"level": "sample"},
            {"relation_type": "actual_delivery"},
            {"permission": "stated", "license_quote": "MIT license"},
            {"level": "acceptance", "record_id": "FAIRs", "outcome": "PASS"},
        ):
            with self.assertRaises(ValueError):
                self.study.record({**args, **changes})
        proof = self.study.record(args)
        self.assertEqual(self.study.record({**args, "claim": "两份报告为必需"})["id"], proof["id"])
        records = collection.references(self.store, domain="机械制造", run_id=self.run["id"])
        self.assertEqual(records["total"], 1)
        self.assertEqual(records["items"][0]["verification"]["sha256"], receipt["sha256"])
        self.assertEqual(records["items"][0]["relation"]["type"], "supplier_requirement")
        self.assertEqual(records["items"][0]["verification"]["search_queries"], ["FAIR handoff"])
        self.study.finish_round({"path": "采购方条款", "next_step": "定位实测样本"})
        self.assertEqual(Research(self.store, self.run).status()["rounds"][0]["new"], 1)

        target = {"target_id": "reports", "stage": "S5", "deliverable": "批次报告", "level": "document",
                  "basis_ref": proof["id"], "count_basis": "2 FAIRs per batch"}
        self.assertEqual(self.study.target({**target, "expected_count": 2})["expected_count"], 2)
        with self.assertRaises(ValueError):
            self.study.target({**target, "expected_count": 3})

    def test_server_extracts_an_exact_excerpt_for_documents_without_relations(self):
        url = "https://publisher.example.org/process"
        text = "The inspection plan documents the scope and traceability of each feature."
        headers = Message()
        headers["Content-Type"] = "text/html; charset=UTF-8"
        with patch.object(public_web, "fetch", return_value=(url, headers, f"<p>{text}</p>".encode())):
            source = self.study.read({"url": url})
        locator = source["passages"][0]["locator"]
        args = {"source_id": source["id"], "target_id": "measurement", "locator": locator,
                "level": "document", "title": "Inspection Plan", "publisher": "Publisher",
                "kind": "document", "claim": "Tests specify traceability", "permission": "unknown"}
        for fields in ({"level": "acceptance"}, {"relation_type": "actual_delivery"}, {"locator": "wrong"}):
            with self.assertRaises(ValueError):
                self.study.record({**args, **fields})
        record = self.study.record(args)
        self.assertEqual(collection.reference(self.store, record["id"])["verification"]["quote"], text)

    def test_saturation_requires_distinct_general_search_rounds(self):
        with patch.object(public_web, "search", side_effect=lambda query, _: {
            "query": query, "backend": "crossref", "scope": "publications", "results": [],
        }):
            for query in ("CMM data release", "FAIR supplement"):
                self.study.search({"query": query})
                result = self.study.finish_round({"path": query, "next_step": "查下一入口"})
            self.assertFalse(result["complete"])
            self.assertTrue(self.study.search({"query": "FAIR supplement"})["already_searched"])
            with self.assertRaises(ValueError):
                self.study.finish_round({"path": "same query", "next_step": "查下一入口"})
        with patch.object(public_web, "search", side_effect=lambda query, _: {
            "query": query, "backend": "searxng", "scope": "open_web", "results": [],
        }):
            for query in ("supplier acceptance record", "CMM public metrology instrument"):
                self.study.search({"query": query})
                result = self.study.finish_round({"path": query, "next_step": "记录公开证据缺口"})
        self.assertTrue(result["complete"])
        self.assertEqual(result["reason"], "alternative_paths_exhausted")

    def test_public_https_targets_and_html_search_results(self):
        for url in ("http://outside.org", "https://user@outside.org", "https://localhost",
                    "https://127.1", "https://169.254.169.254/", "https://api.outside.org:8888/"):
            with self.assertRaises(ValueError):
                public_web.public_url(url)
        raw = json.dumps({"results": [{"url": "https://publisher.org/index#notes", "title": "<b>Guide</b>"}]}).encode()
        with patch.dict("os.environ", {"INDUST_SEARXNG_URL": "http://127.0.0.1:9000"}, clear=True), \
             patch("industplatform.public_web.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = raw
            result = public_web.search("CNC CMM FAIR")
        self.assertEqual(result["scope"], "open_web")
        self.assertEqual(result["results"][0]["url"], "https://publisher.org/index")

    def test_deepseek_search_uses_official_post_endpoint_and_preserves_snippet(self):
        raw = b'{"results":[{"url":"https://arxiv.org/abs/1234.5678","title":"Gerber preprint","snippet":"PCB fabrication data"}]}'
        with patch.dict("os.environ", {}, clear=True), patch("industplatform.public_web.build_opener") as opener:
            remote = opener.return_value.open
            remote.return_value.__enter__.return_value.read.return_value = raw
            result = public_web.search("Gerber fabrication", 6, deepseek_key="fixture-only")
        request = remote.call_args.args[0]
        self.assertEqual((request.get_method(), request.full_url), ("POST", "https://api.deepseek.com/v1/tools/search"))
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-only")
        self.assertEqual(json.loads(request.data), {"query": "Gerber fabrication", "count": 6, "need_content": False})
        self.assertEqual(result["backend"], "deepseek")
        self.assertEqual(result["results"][0]["snippet"], "PCB fabrication data")

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from industplatform import asset_index, codex_agent, collaboration, import_documents, local_import, shared_metadata as metadata, taxonomy, workbench
from industplatform.storage import RunStore
from scripts import build_data_index, data_policy
from scripts.ci_dedupe_and_reconcile import reconcile
from scripts.export_completed_task import export, task_records
from scripts.github_automation import GitHub, github_name
from scripts.pr_guard import check
from scripts.repo_support import ROOT, Repository, indexed_commit
from scripts.rollback_data_task import rollback
from scripts.sync_meta_to_local import synchronize
from scripts.workbench_sync import publication, publish_rollback, update_index
from tests.test_engineering_quality import seed_qualified_reference

PCB_CLASSIFICATION = {"l2_code": "D01.2", "object_cluster": "pcb-assembly", "lifecycle_stages": ["S5"]}
DATA_REPOSITORY = "owner/industry"


class MonorepoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        self.repo = Repository(self.root)
        self.repo.git("config", "user.name", "Local Test")
        self.repo.git("config", "user.email", "local@example.org")
        (self.root / ".gitignore").write_text(".local/\n.staging/\n.env*\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Repository fixture\n", encoding="utf-8")
        self.base = self.commit("Base")

    def use_mock_github(self):
        find_executable = shutil.which

        def which(name, *args, **kwargs):
            return "/test-fixtures/gh" if name == "gh" else find_executable(name, *args, **kwargs)

        self.enterContext(patch.object(collaboration.shutil, "which", side_effect=which))

    def commit(self, title):
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", title)
        return self.repo.revision("HEAD")

    def record(self, address, task="run-one"):
        return metadata.new_record(PCB_CLASSIFICATION, "sources", address, "Primary source", task,
                                   {"url": address, "domain_name": "PCB"})

    def write(self, record):
        relative = metadata.record_path(record)
        file = self.root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(metadata.json_bytes(record))
        return relative

    def collaboration_worker(self, store, *, repository=DATA_REPOSITORY, bind=True, data_checkout=None):
        worker = collaboration.Collaboration(store, self.repo, data_root=self.directory / "datasets")
        if data_checkout is not None:
            self.enterContext(patch.object(worker, "data_checkout", return_value=data_checkout))
        if bind:
            worker.submit({"action": "repository", "repository": repository})
        return worker

    def test_dedupe_previews_only_new_data_against_latest_main(self):
        self.repo.git("switch", "-qc", "data/collection")
        duplicate = self.write(self.record("https://www.example.org/board/?utm_source=probe"))
        new = self.write(self.record("https://www.example.org/drill"))
        head = self.commit("Two records")
        self.repo.git("switch", "-q", "main")
        self.assertEqual(duplicate, self.write(self.record("https://www.example.org/board", "run-two")))
        data_head = self.commit("Other member got there first")
        planned, _, rebuilt = build_data_index.build(self.repo, data_head)
        self.assertEqual(rebuilt, 1)
        for path, body in planned.items():
            file = self.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(body)
        main = self.commit("Index bot")

        plan = reconcile(self.repo, main, head, "data/collection")
        self.assertEqual((list(plan.keep), list(plan.duplicate), plan.indexed_domains), ([new], [duplicate], 1))
        self.assertEqual(build_data_index.build(self.repo, main)[2], 0)

    def test_frozen_l2_and_open_object_identifiers_in_the_data_guard(self):
        self.repo.git("switch", "-qc", "data/taxonomy")
        label = {**PCB_CLASSIFICATION, "object_cluster": "3d-board-sample-alignment", "lifecycle_stages": ["S5", "S3"]}
        entry = metadata.new_record(label, "processes", "Board sample alignment", "Board sample alignment",
                                    "job-taxonomy", {"process": "Board sample alignment"})
        path = self.write(entry)
        self.assertTrue(path.startswith("data/domains/D01.2/processes/"))
        self.assertEqual(entry["lifecycle_stages"], ["S3", "S5"])
        head = self.commit("Add new engineering object")
        with patch.object(taxonomy, "hints", side_effect=AssertionError("data guard must not consult hints")):
            self.assertEqual(check(self.repo, self.base, head, "data/taxonomy", False)[1][path], entry)
        other_process = metadata.new_record(PCB_CLASSIFICATION, "processes", entry["canonical_key"],
                                            entry["title"], "job-other-object", entry["payload"])
        self.assertNotEqual(metadata.record_path(other_process), path)
        source = self.record("https://example.org/sample")
        same_url = metadata.new_record(label, "sources", source["canonical_key"], source["title"],
                                       "job-taxonomy", source["payload"])
        self.assertEqual(metadata.record_path(same_url), metadata.record_path(source))

        for code in ("D12.1", "D01.999", "d01.2", "PCB 组装"):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "官方码表"):
                metadata.validate_record(path.replace("D01.2", code), metadata.json_bytes(entry))
        for changes in ({"l2_code": "D02.1"}, {"domain_id": "D02.1"}, {"lifecycle_stages": []},
                        {"lifecycle_stages": ["S9"]}, {"lifecycle_stages": ["S5", "S5"]},
                        {"object_cluster": "PCB Assembly"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                metadata.validate_record(path, metadata.json_bytes({**entry, **changes}))
        incomplete = {key: value for key, value in entry.items() if key != "object_cluster"}
        with self.assertRaisesRegex(ValueError, "缺少必填"):
            metadata.validate_record(path, metadata.json_bytes(incomplete))

    def test_taxonomy_registry_protection(self):
        l1, l2 = taxonomy.registry()
        self.assertEqual((len(l1), len(l2), tuple(taxonomy.lifecycle())), (11, 58, taxonomy.STAGES))
        self.assertEqual(l2["D11.1"]["name_zh"], "跨行业工程支撑")
        registry = self.root / taxonomy.REGISTRY_PATH
        registry.parent.mkdir()
        shutil.copyfile(ROOT / taxonomy.REGISTRY_PATH, registry)
        base = self.commit("Install official registry")
        self.repo.git("switch", "-qc", "data/registry")
        document = json.loads(registry.read_bytes())
        document["l2"][0]["english_id"] = "other-industry"
        registry.write_bytes(metadata.json_bytes(document))
        head = self.commit("Change official identity")
        with self.assertRaisesRegex(ValueError, "不能修改分类码表"):
            check(self.repo, base, head, "data/registry", False)
        with self.assertRaisesRegex(ValueError, "身份已固化"):
            check(self.repo, base, head, "chore/registry", False)

    def test_prevents_overwrites_deletions_and_index_changes(self):
        path = self.write(self.record("https://example.org/board"))
        main = self.commit("Initial data")
        self.repo.git("switch", "-qc", "data/correction")
        body = self.record("https://example.org/board", "correction")
        body.update(version=2, correction_reason="Correct the published requirements", status="deprecated")
        self.write(body)
        head = self.commit("Correct a row")
        with self.assertRaisesRegex(ValueError, "纠错"):
            check(self.repo, main, head, "data/correction", False)
        self.assertEqual(check(self.repo, main, head, "data/correction", True)[2], [path])
        (self.root / path).unlink()
        removed = self.commit("Attempt to delete")
        with self.assertRaisesRegex(ValueError, "物理删除"):
            check(self.repo, main, removed, "data/correction", True)
        self.repo.git("switch", "-qc", "feat/sample", main)
        index = self.root / build_data_index.MANIFEST
        index.parent.mkdir(parents=True)
        index.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "自动索引"):
            check(self.repo, main, self.commit("Manual index"), "feat/sample", False)

    def test_forbidden_blob_in_batch_history_and_monotonic_ignore(self):
        self.repo.git("switch", "-qc", "chore/hygiene")
        path = self.root / "scripts" / "throwaway.pdf"
        path.parent.mkdir()
        path.write_bytes(b"%PDF-1.7")
        self.commit("Prohibited intermediate file")
        path.unlink()
        with (self.root / ".gitignore").open("a", encoding="utf-8") as stream:
            stream.write("*.pdf\n")
        with self.assertRaisesRegex(ValueError, "批次历史包含受限文件"):
            check(self.repo, self.base, self.commit("Append rule"), "chore/hygiene", False)

        with self.assertRaisesRegex(ValueError, "反选"):
            data_policy.check_ignore(b".env*\n", b".env*\n!.env\n")

    def test_local_keys_and_rollouts_are_ignored_and_rejected_by_the_guard(self):
        local_paths = [
            "model.env.local", "ops/app-key.pem", "id_ed25519_sk", ".ssh/custom", ".aws/credentials",
            "ops/credentials.json", ".codex/sessions/run.jsonl", "rollout-test.jsonl", "data/raw/capture.txt",
            "data/local/working-copy.json",
        ]
        ignored = Repository(ROOT).git("check-ignore", "--no-index", "--", *local_paths).decode().splitlines()
        self.assertEqual(ignored, local_paths)
        for path in local_paths:
            with self.subTest(path=path):
                self.assertEqual(data_policy.forbidden_path(path), "本地运行产物或凭据")
        self.assertIsNone(data_policy.forbidden_path(".codex/config.toml"))

        self.repo.git("switch", "-qc", "feat/credential-check")
        file = self.root / "app-key.pem"
        file.write_text("test fixture\n", encoding="utf-8")
        self.repo.git("add", "-f", "--", file.name)
        with self.assertRaisesRegex(ValueError, "本地运行产物或凭据"):
            check(self.repo, self.base, self.commit("Try to add local key"), "feat/credential-check", False)

    def test_rollback_versions_new_entries_to_deprecated(self):
        path = self.write(self.record("https://example.org/board"))
        delivery = self.commit("Squash merge")
        self.repo.git("switch", "-qc", "data/rollback")
        result = rollback(self.repo, delivery, "Wrong board type", apply=True)
        self.assertEqual(result["records"], [path])
        head = self.commit("Undo batch")
        _, records, _ = check(self.repo, delivery, head, "data/rollback", True)
        self.assertEqual(records[path]["status"], "deprecated")
        self.assertEqual(records[path]["version"], 2)

    def test_rollback_pushes_a_correction_on_the_data_branch_and_preserves_git_history(self):
        path = self.write(self.record("https://example.org/board"))
        published = self.commit("Data publication")
        remote = self.directory / "data-remote.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
        self.repo.git("remote", "add", "origin", str(remote))
        self.repo.git("push", "-q", "origin", "main")
        api = Mock()
        api.private_repository.return_value = {"permissions": {"push": True}}
        api.git.side_effect = self.repo.git

        def fetch():
            self.repo.git("fetch", "-q", "origin", "main")
            return self.repo.revision("origin/main")

        api.fetch_main.side_effect = fetch
        with self.assertRaises(ValueError):
            publish_rollback(self.repo, api, published, reason="")
        api.fetch_main.assert_not_called()
        receipt = publish_rollback(self.repo, api, published, reason="资料归类错误")
        head = fetch()
        self.assertEqual(receipt, {"status": "pushed", "commit": head, "rolled_back": published, "corrected": 1})
        self.assertEqual(self.repo.revision("HEAD"), published)
        self.assertEqual(self.repo.merge_base(head, published), published)
        row = data_policy.record_blob(self.repo, path, self.repo.tree(head)[path])
        self.assertEqual((row["status"], row["version"], row["correction_reason"]), ("deprecated", 2, "资料归类错误"))
        with self.assertRaisesRegex(ValueError, "后续已有改动"):
            publish_rollback(self.repo, api, published, reason="不要覆盖已有纠错")
        api.git.assert_called_once_with("push", "origin", f"{head}:refs/heads/main")

    def test_collaboration_requires_explicit_data_repository_and_locks_both_remote_directions(self):
        self.use_mock_github()
        store = RunStore(self.directory / "local.db")
        self.repo.git("remote", "add", "origin", "https://github.com/owner/platform.git")
        worker = self.collaboration_worker(store, bind=False)
        self.assertFalse(worker.snapshot()["repository"])
        with self.assertRaises(ValueError):
            worker.submit({"action": "pull"})
        self.assertFalse(worker.run_job())

        state = worker.submit({"action": "repository", "repository": "https://github.com/owner/industry.git"})
        self.assertEqual(state["repository"], DATA_REPOSITORY)
        restored = self.collaboration_worker(RunStore(store.path), bind=False)
        self.assertEqual(restored.snapshot()["repository"], DATA_REPOSITORY)
        data_repo = restored.data_checkout(DATA_REPOSITORY, create=True)
        self.assertNotEqual(data_repo.root, self.repo.root)
        self.assertEqual(github_name(data_repo.git("remote", "get-url", "origin").decode()), DATA_REPOSITORY)
        api = GitHub(data_repo, DATA_REPOSITORY)

        with patch.object(GitHub, "gh", return_value=json.dumps({
                "full_name": DATA_REPOSITORY, "private": False, "default_branch": "main"
        }).encode()), patch.object(GitHub, "fetch_main") as fetch:
            restored.submit({"action": "pull"})
            self.assertTrue(restored.run_job())
            self.assertEqual(restored.snapshot()["pull"]["status"], "failed")
            fetch.assert_not_called()

        data_repo.git("remote", "set-url", "--push", "origin", "https://github.com/owner/platform.git")
        with self.assertRaisesRegex(ValueError, "不一致"):
            api.git("push", "origin", "HEAD:refs/heads/main")
        data_repo.git("remote", "set-url", "origin", "https://github.com/owner/platform.git")
        with self.assertRaisesRegex(ValueError, "不一致"):
            api.fetch_main()

    def test_fetch_rejects_mixed_platform_tree_before_advancing_data_ref(self):
        remote = self.directory / "data-remote.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
        self.write(self.record("https://example.org/board"))
        accepted = self.commit("Valid data repository")
        self.repo.git("push", "-q", str(remote), "main")
        worker = self.collaboration_worker(RunStore(self.directory / "local.db"))
        data_repo = worker.data_checkout(DATA_REPOSITORY, create=True)
        api = GitHub(data_repo, DATA_REPOSITORY)

        def transport(action, *args):
            return data_repo.git(action, *(str(remote) if argument == "origin" else argument for argument in args))

        with patch.object(api, "git", side_effect=transport):
            self.assertEqual(api.fetch_main(), accepted)
            platform = self.root / "industplatform" / "__init__.py"
            platform.parent.mkdir()
            platform.write_text("version = 1\n", encoding="utf-8")
            self.commit("Mixed platform and data checkout")
            self.repo.git("push", "-q", str(remote), "main")
            with self.assertRaisesRegex(ValueError, "包含平台文件"):
                api.fetch_main()
        self.assertEqual(data_repo.revision(metadata.PUBLISHED_REF), accepted)

    def test_switching_data_repositories_cancels_old_jobs_and_rejects_foreign_correction_baselines(self):
        self.use_mock_github()
        store = RunStore(self.directory / "local.db")
        identity = "b" * 32
        self.completed_task(store, identity, label=PCB_CLASSIFICATION)
        worker = self.collaboration_worker(store, data_checkout=self.repo)
        path = self.write(self.record("https://example.org/a"))
        baseline = self.commit("Records from the first dataset")
        self.repo.git("update-ref", metadata.PUBLISHED_REF, baseline)
        synchronize(self.repo, store.path, ref=metadata.PUBLISHED_REF, repository=DATA_REPOSITORY)
        active = store.create("api-agent", "offline", "Pinned baseline", scope_request={})
        self.assertEqual((active["shared_metadata_base"], active["shared_metadata_repository"]),
                         (baseline, DATA_REPOSITORY))
        with self.assertRaises(ValueError):
            worker.submit({"action": "repository", "repository": "owner/other-data"})
        with closing(store.connect()) as db, db:
            db.execute("UPDATE runs SET status='completed' WHERE id=?", (active["id"],))
            db.execute("""UPDATE runs SET body=json_set(body,'$.shared_metadata_base',?,
                '$.shared_metadata_repository',?) WHERE id=?""", (baseline, DATA_REPOSITORY, identity))
        old_api = Mock(name="Previous dataset")
        old_api.name = DATA_REPOSITORY
        with patch.object(worker, "client", return_value=old_api):
            worker.submit({"action": "sync", "run_ids": [identity]})
            worker.submit({"action": "auto_sync", "enabled": True})

        name = "owner/other-data"
        worker.submit({"action": "repository", "repository": name})
        restored = self.collaboration_worker(RunStore(store.path), bind=False, data_checkout=self.repo)
        other_api = Mock(name="Current dataset", repo=self.repo)
        other_api.name = name
        with patch.object(restored, "client", return_value=other_api):
            self.assertFalse(restored.run_job())
            self.assertFalse(restored.snapshot()["automatic"])
            with closing(store.connect()) as db:
                self.assertIsNone(db.execute("SELECT path FROM shared_metadata WHERE path=?", (path,)).fetchone())
                self.assertEqual(db.execute("SELECT status FROM runs WHERE id=?", (identity,)).fetchone(), ("completed",))
                self.assertIsNotNone(db.execute("SELECT 1 FROM runs WHERE id=?", (active["id"],)).fetchone())
                self.assertEqual(db.execute("SELECT repository,status FROM collaboration_jobs WHERE id=?",
                                            (identity,)).fetchone(), (DATA_REPOSITORY, "failed"))
            # A matching commit object is insufficient when the signed-off baseline belongs to another dataset.
            self.assertEqual(self.repo.revision(baseline), baseline)
            with closing(store.connect()) as db, self.assertRaisesRegex(ValueError, "共享基线来自其他"):
                publication(self.repo, db, identity, repository=name)
            with self.assertRaisesRegex(ValueError, "当前数据仓库"):
                restored.submit({"action": "publish", "run_ids": [identity], "correction_reason": "保留旧基线"})
            other_api.fetch_main.assert_not_called()
            other_api.git.assert_not_called()

    def test_real_pull_and_checkout_hooks_preserve_local_drafts(self):
        for directory in (".githooks", "scripts", "schemas"):
            shutil.copytree(ROOT / directory, self.root / directory, ignore=shutil.ignore_patterns("__pycache__"))
        package = self.root / "industplatform"
        package.mkdir()
        for file in ("__init__.py", "shared_metadata.py", "taxonomy.py", "frontier.py", "public_web.py", "data_location.py",
                     "reference_catalog.py"):
            shutil.copy(ROOT / "industplatform" / file, package / file)
        bootstrap = self.commit("Hook installation files")
        remote = self.directory / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
        self.repo.git("remote", "add", "origin", str(remote))
        self.repo.git("push", "-q", "origin", "main")
        checkout = self.directory / "member"
        self.repo.git("clone", "-q", str(remote), str(checkout))
        member = Repository(checkout)
        subprocess.run([sys.executable, "-B", "scripts/install_hooks.py"], cwd=checkout, check=True, capture_output=True)
        database = checkout / ".local" / "industplatform.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TABLE drafts (value TEXT)")
            db.execute("INSERT INTO drafts VALUES ('keep local work')")

        path = self.write(self.record("https://example.org/a"))
        accepted = self.commit("Published")
        self.repo.git("push", "-q", "origin", "main")
        member.git("pull", "-q", "--ff-only")
        self.assertEqual(synchronize(member, database, check=True)[0], accepted)
        blocked_path = "workspace.db"
        oid = member.git("hash-object", "-w", "--stdin", data=b"SQLite format 3\0").decode().strip()
        candidate = indexed_commit(member, accepted, {blocked_path: oid}, "Reject a database before upload",
                                   {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org",
                                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.org"})
        with self.assertRaisesRegex(ValueError, r"workspace\.db"):
            member.git("push", "origin", candidate + ":refs/heads/main")
        self.assertEqual(member.revision("origin/main"), accepted)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT path FROM shared_metadata").fetchall(), [(path,)])
        member.git("switch", "-q", "--detach", bootstrap)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM shared_metadata").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT value FROM drafts").fetchone(), ("keep local work",))
        member.git("switch", "-q", "main")
        self.assertEqual(synchronize(member, database, check=True)[0], accepted)
        correction = self.record("https://example.org/a", "revision")
        correction.update(version=2, status="deprecated", correction_reason="Replaced published source")
        self.write(correction)
        corrected = self.commit("Deprecate source")
        self.repo.git("push", "-q", "origin", "main")
        member.git("pull", "-q", "--ff-only")
        self.assertEqual(synchronize(member, database, check=True)[0], corrected)
        malformed = self.root / path
        malformed.write_text("{}", encoding="utf-8")
        self.commit("Malformed metadata")
        self.repo.git("push", "-q", "origin", "main")
        member.git("pull", "-q", "--ff-only")
        with self.assertRaises(ValueError):
            synchronize(member, database)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT last_synced_commit FROM shared_metadata_sync").fetchone()[0], corrected)
            self.assertEqual(db.execute("SELECT version,status FROM shared_metadata").fetchone(), (2, "deprecated"))
            self.assertEqual(db.execute("SELECT value FROM drafts").fetchone(), ("keep local work",))

    def completed_task(self, store, identity, sequence=1, label=None):
        with closing(store.connect()) as db, db:
            domain = db.execute("INSERT INTO domains(name) VALUES (?)", (f"PCB {identity}",)).lastrowid
            db.execute("""INSERT INTO process_nodes(domain_id,id,position,title) VALUES (?, 'cam', 1, 'CAM intake')""",
                       (domain,))
            db.execute("""INSERT INTO process_nodes(domain_id,id,position,title) VALUES (?, 'other', 2, 'Other')""",
                       (domain,))
            run = {"scope_domain_id": domain, "scoped_workflow": True, "prompt": "Research PCB CAM",
                   "execution_scope": {"state": "completed", "stage_queue": ["STAGE_2_TOPOLOGY"], "stage_index": 0}}
            if label:
                run["task_taxonomy"] = label
            if repository := metadata.configured_repository(db):
                run["shared_metadata_repository"] = repository
            db.execute("INSERT INTO runs(id,status,sequence,body) VALUES (?,'completed',?,?)",
                       (identity, sequence, json.dumps(run)))
            db.execute("INSERT INTO workflow_scope_contracts(id,run_id,body,state) VALUES (?,?,'{}','completed')",
                       (identity, identity))
            db.execute("""INSERT INTO workflow_scope_audit(contract_id,kind,tool,entity_id,detail)
                VALUES (?,'applied','upsert_process_node','cam','New operation')""", (identity,))

    def test_export_uses_only_completed_workbench_output(self):
        db_path = self.directory / "local.db"
        store = RunStore(db_path)
        self.completed_task(store, "job-one")
        self.repo.git("switch", "-qc", "data/export")
        self.assertFalse((self.root / "data" / "domains").exists())
        with self.assertRaisesRegex(ValueError, "尚未记录分类"):
            export(self.repo, db_path, "job-one")
        self.assertEqual(export(self.repo, db_path, "job-one", PCB_CLASSIFICATION)[:2], (1, 0))
        self.assertFalse((self.root / "data" / "domains").exists())
        with closing(store.connect()) as db, db:
            db.execute("UPDATE runs SET body=json_set(body,'$.task_taxonomy',json(?)) WHERE id='job-one'",
                       (json.dumps(PCB_CLASSIFICATION),))
        with self.assertRaisesRegex(ValueError, "与任务已确认的分类不一致"):
            export(self.repo, db_path, "job-one", {**PCB_CLASSIFICATION, "lifecycle_stages": ["S6"]})
        count, skipped, root = export(self.repo, db_path, "job-one", apply=True)
        self.assertEqual((count, skipped, root), (1, 0, self.root))
        path, = (root / "data" / "domains" / "D01.2").rglob("*.json")
        entry = json.loads(path.read_bytes())
        self.assertEqual(entry["title"], "CAM intake")
        self.assertEqual({key: entry[key] for key in taxonomy.FIELDS}, PCB_CLASSIFICATION)
        with closing(store.connect()) as db:
            self.assertEqual(metadata.existing_object_clusters(db, "D01.2", root),
                             [{"object_cluster": "pcb-assembly", "example": "CAM intake"}])
        with closing(store.connect()) as db, db:
            db.execute("UPDATE runs SET status='awaiting_stage' WHERE id='job-one'")
        with self.assertRaisesRegex(ValueError, "尚未跑完"):
            export(self.repo, db_path, "job-one")

    def test_sync_exports_merged_process_links_for_one_verified_document_version(self):
        store = RunStore(self.directory / "local.db")
        self.completed_task(store, "ref-job", label=PCB_CLASSIFICATION)
        url = "https://example.org/fabrication"
        with closing(store.connect()) as db, db:
            domain = db.execute("SELECT id FROM domains WHERE name='PCB ref-job'").fetchone()[0]
            db.execute("""INSERT INTO raw_sources(domain_id,id,canonical_url,status,sha256)
                          VALUES(?,'ref-source',?,'verified',?)""", (domain, url, "a" * 64))
            for node in ("cam", "other"):
                seed_qualified_reference(db, domain, node, "ref-" + node)
                db.execute("""INSERT INTO workflow_scope_audit(contract_id,kind,tool,entity_id,detail,output_id)
                    VALUES ('ref-job','applied','promote_verified_ref',?,'核验文献',?)""", (node, "ref-" + node))
            references = [value for value in task_records(db, "ref-job").values() if value["kind"] == "literature"]
        self.assertEqual(len(references), 1)
        reference, = references
        payload = reference["payload"]
        self.assertEqual(set(payload["process_node_ids"]), {"cam", "other"})
        self.assertEqual((payload["authority_tier"], payload["source_channel"]), ("T2_MANUFACTURER", "C2_OEM_FAB_SPEC"))
        self.assertEqual((reference["object_cluster"], reference["lifecycle_stages"]), ("pcb-intake", ["S3", "S5"]))
        self.assertNotIn("quote", payload["extracted_clauses"][0])
        for patch in ({"authority_tier": "T1_STANDARD"}, {"process_node_ids": ["cam", "unassigned"]}, {"source_channel": None}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                metadata.validate_record(metadata.record_path(reference), metadata.json_bytes({
                    **reference, "payload": {**payload, **patch},
                }))

    def test_workbench_sync_retries_a_concurrent_push_and_keeps_the_index(self):
        self.use_mock_github()
        store = RunStore(self.directory / "local.db")
        identity = "1" * 32
        self.completed_task(store, identity, label=PCB_CLASSIFICATION)
        with closing(store.connect()) as db, db:
            db.execute("""INSERT INTO workflow_scope_audit(contract_id,kind,tool,entity_id,detail)
                VALUES (?,'applied','upsert_process_node','other','Another operation')""", (identity,))
        remote = self.directory / "data-remote.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
        self.repo.git("push", "-q", str(remote), "main")
        self.repo.git("remote", "add", "origin", "https://github.com/owner/platform.git")
        peer_path = self.directory / "peer"
        self.repo.git("clone", "-q", str(remote), str(peer_path))
        peer = Repository(peer_path)
        peer.git("config", "user.name", "Other Member")
        peer.git("config", "user.email", "other@example.org")
        duplicate = metadata.new_record(PCB_CLASSIFICATION, "processes", "CAM intake", "CAM intake", "peer-run",
                                        {"domain_name": "PCB", "occupations": [], "upstream": [], "downstream": []})
        peer_file = peer.root / metadata.record_path(duplicate)
        peer_file.parent.mkdir(parents=True)
        peer_file.write_bytes(metadata.json_bytes(duplicate))
        peer.git("add", "--", str(peer_file))
        peer.git("commit", "-qm", "Peer task")
        peer_head = peer.revision("HEAD")
        (self.root / "README.md").write_text("Local work in progress\n", encoding="utf-8")
        self.repo.git("add", "README.md")
        index = self.repo.git("write-tree")
        worker = self.collaboration_worker(store)
        data_repo = worker.data_checkout(DATA_REPOSITORY, create=True)
        data_repo.git("config", "user.name", "Local Test")
        data_repo.git("config", "user.email", "local@example.org")
        pushes = []

        def transport(client, action, *args):
            self.assertEqual(client.repo.root, data_repo.root)
            self.assertEqual(client.name, DATA_REPOSITORY)
            routed = tuple(str(remote) if argument == "origin" else argument for argument in args)
            if action == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    peer.git("push", "-q", "origin", "main")
                else:
                    data_repo.git(action, *routed)
                    raise ValueError("lost acknowledgement")
            return data_repo.git(action, *routed)

        private = {"permissions": {"push": True}}
        with patch.object(GitHub, "private_repository", return_value=private), patch.object(
                GitHub, "git", autospec=True, side_effect=transport):
            worker.submit({"action": "sync", "run_ids": [identity, identity]})
            self.assertTrue(worker.run_job())  # Initial pull.
            self.assertTrue(worker.run_job())  # Concurrent push and retry.
            self.assertTrue(worker.run_job())  # Refresh shared SQLite.
            self.assertFalse(worker.run_job())
            snapshot = worker.snapshot()
        published = data_repo.revision("origin/main")
        self.assertEqual(snapshot["runs"][0]["job"], {"status": "completed", "outcome":
                         {"status": "pushed", "commit": published, "added": 1, "skipped": 1}})
        self.assertEqual(snapshot["commit"], published)
        self.assertEqual(data_repo.merge_base(peer_head, published), peer_head)
        self.assertEqual(data_repo.blob(data_repo.tree(published)[metadata.record_path(duplicate)]),
                         peer_file.read_bytes())
        self.assertEqual(len(pushes), 2)
        for args in pushes:
            self.assertEqual(args[0], "origin")
            self.assertTrue(args[1].endswith(":refs/heads/main"))
        self.assertEqual(self.repo.git("write-tree"), index)
        self.assertEqual(self.repo.revision("HEAD"), self.base)
        self.assertEqual(self.repo.branch(), "main")
        self.assertIsNone(self.repo.revision("origin/main", optional=True))

        with closing(store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM shared_metadata").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT repository,checkout FROM shared_metadata_sync").fetchone(),
                             (DATA_REPOSITORY, str(data_repo.root)))
        api = Mock()
        api.git.side_effect = lambda action, remote_name, *args: data_repo.git(action, str(remote), *args)
        indexed = update_index(data_repo, api, published)
        self.assertIn("data/_index/manifest.json", data_repo.tree(indexed))
        self.assertEqual(update_index(data_repo, api, indexed), indexed)
        self.assertEqual(self.repo.git("write-tree"), index)

    def test_local_import_only_publishes_classified_sources_after_explicit_selection(self):
        self.use_mock_github()
        existing = self.record("https://example.org/cam", "published-by-peer")
        existing_path = self.write(existing)
        main = self.commit("Peer already shared CAM source")
        remote = self.directory / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
        self.repo.git("remote", "add", "origin", str(remote))
        self.repo.git("push", "-q", "origin", "main")
        store = RunStore(self.directory / "import.db")
        with closing(store.connect()) as db, db:
            name = taxonomy.registry()[1]["D01.2"]["name_zh"]
            workbench.register_domain(db, name)
            domain = db.execute("SELECT id FROM domains WHERE name=?", (name,)).fetchone()[0]
            db.execute("""INSERT INTO raw_sources(domain_id,id,canonical_url,status)
                VALUES (?,'existing-cam','https://example.org/cam?utm_source=earlier','verified')""", (domain,))
        worker = self.collaboration_worker(store, repository="owner/project", data_checkout=self.repo)
        importer = local_import.Importer(store, None, None, 18765)
        batch = importer.submit({"action": "create", "count": 4})["id"]
        html = b'''<html><h1>PCB CAM</h1><p>Intake specification</p>
            <a href="https://example.org/cam/?utm_source=report#version">CAM intake</a>
            <a href="https://example.org/drill">Drill specification</a>
            <script>https://example.org/private-instruction Never send full original</script></html>'''
        csv = "标题,链接,摘要\n钻孔交付,https://example.org/drill/,Excellon format specification\n".encode("gb18030")
        importer.accept_file(batch, 0, "PCB 调研.html", html)
        importer.accept_file(batch, 1, "PCB 数据.csv", csv)
        importer.accept_file(batch, 2, "重复报告.html", html)
        importer.accept_file(batch, 3, "只有测量结果.csv", b"id,measurement\n1,0.37\n")
        with self.assertRaises(ValueError):
            importer.accept_file(batch, 0, "../invalid.html", html)
        api = Mock(name="GitHub fixture", repo=self.repo)
        api.name = "owner/project"
        api.private_repository.return_value = {"permissions": {"push": True}}
        api.git.side_effect = self.repo.git

        def fetch():
            self.repo.git("fetch", "-q", "origin", "main")
            return self.repo.revision("origin/main")

        api.fetch_main.side_effect = fetch

        def classify(_profile, leads, _existing):
            self.assertEqual([lead["url"] for lead in leads], ["https://example.org/cam", "https://example.org/drill"])
            self.assertNotIn("Never send full original", json.dumps(leads))
            return json.dumps({"decisions": [{"id": lead["id"], "classification": PCB_CLASSIFICATION,
                "title": "板厂 CAM 交付规范", "summary_zh": "说明 Gerber X2 和 Excellon 钻孔文件的交付要求"} for lead in leads]})

        with patch.object(worker, "client", return_value=api):
            worker.submit({"action": "auto_sync", "enabled": True})
            with self.assertRaisesRegex(ValueError, "已完成"):
                worker.submit({"action": "publish", "run_ids": [batch]})
            importer.submit({"action": "prepare", "batch": batch})
            with patch.object(importer, "choose_model", return_value=({}, "local fixture")), patch.object(
                    importer, "classify", side_effect=classify):
                self.assertTrue(importer.run_job())
            self.assertFalse(worker.run_job())
            api.git.assert_not_called()
            state = worker.snapshot(details=True)
            self.assertEqual(state["runs"][0]["preview"], {"counts": {"sources": 1}, "correction": False, "total": 2})
            self.assertEqual(state["imports"][0]["count"], 2)
            task = workbench.overview(store, domain)["threads"][0]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["progress"]["candidates"], 2)
            self.assertEqual(task["progress"]["domains"], [
                {"id": domain, "name": name, "l2_code": "D01.2", "added": 1, "duplicates": 1}
            ])
            summary = workbench.overview(store, domain)
            self.assertEqual(summary["domain"]["local_import_count"], 1)
            self.assertEqual(summary["import_coverage"], [{"code": "S5", "title": "检验、验证与验收", "count": 1}])
            self.assertEqual(summary["nodes"], [])
            items = workbench.source_page(store, domain, "imports", "钻孔", stage="S5")["items"]
            self.assertEqual([(row["url"], row["state"]) for row in items], [("https://example.org/drill", "needs_review")])
            self.assertEqual(workbench.source_page(store, domain, "imports", stage="S4")["items"], [])
            self.assertEqual(workbench.source_page(store, domain, "references")["items"], [])
            self.assertEqual(asset_index.read(store, "sources", query="example.org/drill")["items"][0]["origin"], "import")
            with closing(store.connect()) as db:
                saved, = db.execute("SELECT keywords FROM domain_library WHERE domain_id=?", (domain,)).fetchone()
                self.assertTrue({"Gerber X2", "Excellon", "钻孔文件"}.issubset(json.loads(saved)))
                batch_details = local_import.details(db, batch)
                self.assertEqual(len(batch_details["notes"]), 2)
                self.assertEqual(db.execute("SELECT candidates FROM local_import_batches WHERE id=?", (batch,)).fetchone()[0], "[]")
            worker.submit({"action": "publish", "run_ids": [batch]})
            self.assertTrue(worker.run_job())
            self.assertTrue(worker.run_job())  # Refresh the local shared view.
            current = worker.snapshot()
        outcome = current["runs"][0]["job"]["outcome"]
        self.assertEqual((outcome["added"], outcome["skipped"]), (1, 1))
        published = outcome["commit"]
        self.assertEqual(self.repo.revision("HEAD"), main)
        self.assertEqual(self.repo.git("show", "-s", "--format=%P", published).decode().strip(), main)
        changes = self.repo.changes(main, published)
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0][1].endswith(".json"))
        self.assertEqual(self.repo.blob(self.repo.tree(published)[existing_path]), metadata.json_bytes(existing))
        record = data_policy.record_blob(self.repo, changes[0][1], self.repo.tree(published)[changes[0][1]])
        self.assertEqual(record["payload"]["verification_status"], "unverified")
        self.assertEqual(record["payload"]["url"], "https://example.org/drill")
        self.assertEqual(current["commit"], published)

    def test_local_import_pdf_annotations_and_retries_stay_within_the_original_leads(self):
        store = RunStore(self.directory / "pdf-import.db")
        self.collaboration_worker(store, bind=False)
        importer = local_import.Importer(store, None, None, 18765)
        batch = importer.submit({"action": "create", "count": 1})["id"]
        output = [Mock(stdout=b"PCB assembly intake\n Read the manufacturing guide"),
                  Mock(stdout=b"1  Annotation  https://example.org/manufacturing.pdf?utm_medium=pdf\n"
                              b"2  Annotation  https://example.org/signed?X-Obs-Signature=not-shareable")]
        with patch.object(import_documents.subprocess, "run", side_effect=output) as poppler:
            importer.accept_file(batch, 0, "manual.pdf", b"%PDF-1.7\nlocal PDF bytes")
        self.assertEqual(poppler.call_count, 2)
        importer.submit({"action": "prepare", "batch": batch})
        ungrounded = json.dumps({"decisions": [{"id": 2, "classification": None}]})
        with patch.object(importer, "choose_model", return_value=(None, "fixture")), patch.object(
                importer, "classify", return_value=ungrounded):
            self.assertTrue(importer.run_job())
        with closing(store.connect()) as db:
            self.assertEqual(local_import.summaries(db)[0]["status"], "failed")
            with self.assertRaisesRegex(ValueError, "已整理"):
                local_import.publication_records(db, batch)

        restored = local_import.Importer(RunStore(store.path), None, None, 18765)
        restored.submit({"action": "prepare", "batch": batch})

        def classify(_profile, leads, _existing):
            self.assertEqual([lead["url"] for lead in leads], ["https://example.org/manufacturing.pdf"])
            return json.dumps({"decisions": [{"id": 1, "classification": PCB_CLASSIFICATION,
                "title": "制造指南", "summary_zh": "涉及 PCB 来料核对。"}]})

        with patch.object(restored, "choose_model", return_value=(None, "fixture")), patch.object(
                restored, "classify", side_effect=classify):
            self.assertTrue(restored.run_job())
        with closing(store.connect()) as db:
            self.assertEqual(len(local_import.publication_records(db, batch)), 1)

    def test_imports_embedded_html_catalog_without_running_embedded_code(self):
        html = '''<h1>PCB 制造资料</h1><p>https://example.org/main看样例</p>
          <script type="application/json">{"sources":[{"name":"钻孔规则", "url":"https://example.org/embedded",
          "downloads":["https://example.org/drill.zip", "https://example.org/main/?utm_medium=web"],
          "summary":"API key is required"}],"samples":[{"url":"https://example.org/raw"}]}</script>
          <script>var results = {"url":"https://example.org/script"};</script>'''.encode()
        links = {row["url"]: row for row in import_documents.extract("offline.html", html)}
        self.assertEqual(set(links), {"https://example.org/main", "https://example.org/embedded", "https://example.org/drill.zip"})
        self.assertEqual(links["https://example.org/embedded"]["title"], "钻孔规则")
        self.assertNotIn("API key", json.dumps(links, ensure_ascii=False))

    def test_import_retries_timed_out_model_group_in_smaller_batches(self):
        store = RunStore(self.directory / "retry-import.db")
        importer = local_import.Importer(store, None, None, 18765)
        batch = importer.submit({"action": "create", "count": 1})["id"]
        html = "".join(f'<a href="https://example.org/spec/{number}">PCB 规范 {number}</a>'
                       for number in range(12))
        importer.accept_file(batch, 0, "report.html", html.encode())
        importer.submit({"action": "prepare", "batch": batch})
        sizes = []

        def classify(_profile, leads, _existing):
            sizes.append(len(leads))
            if len(leads) > 8:
                raise codex_agent.RunTimedOut("fixture deadline")
            groups = [{"ids": [lead["id"] for lead in leads], **PCB_CLASSIFICATION}]
            return local_import.grouped_decisions(json.dumps({"groups": groups, "skip": []}), leads)

        with patch.object(importer, "choose_model", return_value=(None, "fixture")), patch.object(
                importer, "classify", side_effect=classify):
            self.assertTrue(importer.run_job())
        self.assertEqual(sizes, [12, 6, 6])
        with closing(store.connect()) as db:
            self.assertEqual(len(local_import.publication_records(db, batch)), 12)

    def test_automatic_publication_is_opt_in_and_preserves_its_receipt_after_restart(self):
        self.use_mock_github()
        store = RunStore(self.directory / "local.db")
        earlier, eligible = "2" * 32, "3" * 32
        self.completed_task(store, earlier, label=PCB_CLASSIFICATION)
        worker = self.collaboration_worker(store, data_checkout=self.repo)
        api = Mock(repo=self.repo)
        api.name = "owner/industry"
        with patch.object(worker, "client", return_value=api):
            self.assertFalse(worker.run_job())
            worker.submit({"action": "auto_sync", "enabled": True})
            self.assertFalse(worker.run_job())
            self.completed_task(store, eligible, sequence=2, label=PCB_CLASSIFICATION)
            with patch.object(collaboration, "publish_run", return_value={"status": "duplicate"}) as publish:
                self.assertTrue(worker.run_job())
                publish.assert_called_once_with(self.repo, api, store.path, eligible, reason="", progress=ANY)
            self.assertFalse(worker.run_job())
        restored = self.collaboration_worker(RunStore(store.path), bind=False, data_checkout=self.repo)
        with patch.object(restored, "client", return_value=api):
            runs = {item["id"]: item["job"] for item in restored.snapshot()["runs"]}
        self.assertIsNone(runs[earlier])
        self.assertEqual(runs[eligible], {"status": "completed", "outcome": {"status": "duplicate"}})

    def test_sync_defers_publication_after_failed_pull_across_restart(self):
        self.use_mock_github()
        store = RunStore(self.directory / "local.db")
        identity = "4" * 32
        self.completed_task(store, identity, label=PCB_CLASSIFICATION)
        worker = self.collaboration_worker(store, data_checkout=self.repo)
        api = Mock(repo=self.repo)
        api.name = "owner/industry"
        with patch.object(worker, "client", return_value=api):
            with self.assertRaisesRegex(ValueError, "已完成"):
                worker.submit({"action": "sync", "run_ids": [identity, "5" * 32]})
            self.assertFalse(worker.run_job())
            worker.submit({"action": "sync", "run_ids": [identity]})
            with patch.object(collaboration, "published_data", side_effect=ValueError("offline")):
                self.assertTrue(worker.run_job())
            self.assertFalse(worker.run_job())
            self.assertEqual(worker.snapshot()["runs"][0]["job"]["status"], "waiting_pull")
        restored = self.collaboration_worker(RunStore(store.path), bind=False, data_checkout=self.repo)
        with patch.object(restored, "client", return_value=api):
            self.assertFalse(restored.run_job())
            restored.submit({"action": "sync"})
            with patch.object(collaboration, "published_data", return_value={"commit": self.base}), patch.object(
                    collaboration, "publish_run", return_value={"status": "pushed"}) as publish:
                self.assertTrue(restored.run_job())
                publish.assert_not_called()
                self.assertTrue(restored.run_job())
                publish.assert_called_once_with(self.repo, api, store.path, identity, reason="", progress=ANY)
                self.assertTrue(restored.run_job())
                self.assertFalse(restored.run_job())

    def test_versioned_publication_requires_a_reason_and_refuses_concurrent_corrections(self):
        self.use_mock_github()
        store = RunStore(self.directory / "local.db")
        identity = "a" * 32
        self.completed_task(store, identity, label=PCB_CLASSIFICATION)
        baseline_record = metadata.new_record(PCB_CLASSIFICATION, "processes", "CAM intake", "CAM intake",
            "earlier-task", {"domain_name": f"PCB {identity}", "occupations": ["PCB designer"],
                             "upstream": [], "downstream": []})
        path = self.write(baseline_record)
        baseline = self.commit("Previously shared CAM intake")
        remote = self.directory / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
        self.repo.git("remote", "add", "origin", str(remote))
        self.repo.git("push", "-q", "origin", "main")
        worker = self.collaboration_worker(store, data_checkout=self.repo)
        synchronize(self.repo, store.path, ref=metadata.PUBLISHED_REF, repository=DATA_REPOSITORY)
        with closing(store.connect()) as db, db:
            db.execute("""UPDATE runs SET body=json_set(body,'$.shared_metadata_base',?,
                '$.shared_metadata_repository',?) WHERE id=?""", (baseline, DATA_REPOSITORY, identity))
        api = Mock(repo=self.repo)
        api.name = "owner/industry"
        api.private_repository.return_value = {"permissions": {"push": True}}
        api.fetch_main.return_value = baseline
        api.git.side_effect = self.repo.git
        with patch.object(worker, "client", return_value=api):
            preview = worker.snapshot(details=True)["runs"][0]["preview"]
            self.assertTrue(preview["correction"])
            self.assertEqual(preview["counts"], {})
            worker.submit({"action": "publish", "run_ids": [identity]})
            self.assertTrue(worker.run_job())
            failure = worker.snapshot()["runs"][0]["job"]
            self.assertEqual(failure["phase"], "validate")
            self.assertIn(path, failure["error"])
            self.assertIn("纠错说明", failure["error"])
            api.fetch_main.assert_not_called()
            api.git.assert_not_called()

            correction = {"action": "publish", "run_ids": [identity], "correction_reason": "核对岗位后补充范围"}
            self.write({**baseline_record, "version": 2, "correction_reason": "another edit",
                        "payload": {**baseline_record["payload"], "occupations": ["Other role"]}})
            competing = self.commit("Other member corrected the same record")
            api.fetch_main.return_value = competing
            worker.submit(correction)
            self.assertTrue(worker.run_job())
            failure = worker.snapshot()["runs"][0]["job"]
            self.assertEqual(failure["phase"], "rebase")
            self.assertIn("远端已修改", failure["error"])
            api.git.assert_not_called()

            api.fetch_main.return_value = baseline
            worker.submit(correction)
            self.assertTrue(worker.run_job())
            head = self.repo.revision("origin/main")
            api.fetch_main.return_value = head
            self.assertTrue(worker.run_job())  # Refresh after the confirmed push.
            snapshot = worker.snapshot(details=True)
            self.assertEqual(snapshot["pending_count"], 0)
            self.assertEqual(snapshot["commit"], head)
            self.assertIsNotNone(snapshot["synced_at"])
            self.assertEqual(snapshot["recent"][0]["commit"], head)
            self.assertEqual(snapshot["recent"][0]["title"], "Research PCB CAM")
            published = metadata.json_object(self.repo.blob(self.repo.tree(head)[path]))
            self.assertEqual((published["version"], published["correction_reason"]), (2, correction["correction_reason"]))
            self.assertEqual(published["payload"]["occupations"], [])
            self.assertEqual(self.repo.revision("HEAD"), competing)

    def test_background_preferences_do_not_disable_explicit_sqlite_rebuild(self):
        store = RunStore(self.directory / "local.db")
        record = self.record("https://example.org/reference")
        path = self.write(record)
        head = self.commit("Published reference")
        self.repo.git("update-ref", metadata.PUBLISHED_REF, head)
        worker = self.collaboration_worker(store, bind=False)
        worker.submit({"action": "preference", "name": "auto_import", "enabled": False})
        worker.submit({"action": "preference", "name": "audit_ignores", "enabled": False})
        untracked = self.root / "tool-output.db"
        untracked.touch()
        worker.audit_run()
        self.assertIn(untracked.name, self.repo.untracked())
        worker.submit({"action": "preference", "name": "audit_ignores", "enabled": True})
        original = (self.root / ".gitignore").read_bytes()
        worker.audit_run()
        self.assertTrue((self.root / ".gitignore").read_bytes().startswith(original))
        self.assertNotIn(untracked.name, self.repo.untracked())
        self.assertEqual(synchronize(self.repo, store.path, automatic=True), (None, 0, 0))
        synchronize(self.repo, store.path, ref=metadata.PUBLISHED_REF)
        with closing(store.connect()) as db, db:
            db.execute("DELETE FROM shared_metadata")  # Stale index contents at the same commit.
            db.execute("CREATE TABLE local_draft (value TEXT)")
            db.execute("INSERT INTO local_draft VALUES ('keep')")
        self.assertEqual(synchronize(self.repo, store.path, ref=metadata.PUBLISHED_REF, rebuild=True), (head, 1, 0))
        with closing(store.connect()) as db:
            self.assertEqual(db.execute("SELECT path FROM shared_metadata").fetchone(), (path,))
            self.assertEqual(db.execute("SELECT value FROM local_draft").fetchone(), ("keep",))

    def test_old_publication_queue_requires_new_push_authorization(self):
        store = RunStore(self.directory / "local.db")
        identity = "6" * 32
        self.completed_task(store, identity, label=PCB_CLASSIFICATION)
        with closing(store.connect()) as db, db:
            db.execute("""CREATE TABLE collaboration_jobs (
                id TEXT PRIMARY KEY, repository TEXT, status TEXT, automatic INTEGER, claim TEXT,
                lease_until REAL, result TEXT)""")
            db.execute("INSERT INTO collaboration_jobs VALUES (?,'owner/industry','queued',1,NULL,0,'{}')", (identity,))
            db.execute("CREATE TABLE collaboration_preferences (singleton INTEGER, repository TEXT, after_sequence INTEGER)")
            db.execute("INSERT INTO collaboration_preferences VALUES (1,'owner/industry',0)")
        worker = self.collaboration_worker(store, data_checkout=self.repo)
        api = Mock()
        api.name = "owner/industry"
        with patch.object(worker, "client", return_value=api):
            state = worker.snapshot()
            self.assertFalse(state["automatic"])
            self.assertEqual(state["runs"][0]["job"]["status"], "failed")
            self.assertFalse(worker.run_job())


if __name__ == "__main__":
    unittest.main()

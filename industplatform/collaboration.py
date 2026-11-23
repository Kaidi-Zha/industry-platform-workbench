"""Durable publication and refresh jobs for the local workbench."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from contextlib import closing
from time import monotonic, time
from uuid import uuid4

from scripts.check_repo_hygiene import audit_run_ignores
from scripts.github_automation import GitHub, data_checkout, data_repository_name
from scripts.repo_support import ROOT, SHA, Repository
from scripts.workbench_sync import (
    RUN_ID, maintain, publication_preview, publish_rollback, publish_run, published_data, recent_publications,
)

from . import local_import
from .shared_metadata import PUBLISHED_REF, SYNC_DEFAULTS, configured_repository, required_text, sync_preferences

logger = logging.getLogger(__name__)
COLLABORATION_PATH = "/api/workbench/collaboration"
IMPORTS_PATH = COLLABORATION_PATH + "/imports"
SYNC_LIMIT = 40
PULL_JOBS = ("pull", "refresh", "rebuild")
RETRYABLE = """
    (j.status IS NULL OR j.status='failed'
     OR json_extract(j.result,'$.outcome.status') IN ('open','closed'))
"""
ELIGIBLE = """
    r.status='completed' AND json_extract(r.body,'$.scoped_workflow')=1
    AND json_extract(r.body,'$.execution_scope.state')='completed'
    AND coalesce(json_extract(r.body,'$.result.scope_continue'),0)=0
    AND (coalesce(json_array_length(r.body,'$.execution_scope.stage_queue'),0)=0
         OR coalesce(json_extract(r.body,'$.execution_scope.stage_index'),0)+1
            =json_array_length(r.body,'$.execution_scope.stage_queue'))
    AND json_type(r.body,'$.task_taxonomy')='object'
    AND EXISTS (SELECT 1 FROM workflow_scope_contracts c JOIN workflow_scope_audit a ON a.contract_id=c.id
                WHERE c.run_id=r.id AND c.state='completed' AND a.kind='applied'
                AND (a.tool IN ('upsert_process_node','upsert_process_edge','upsert_process_relation',
                                'save_raw_source','promote_verified_ref','upsert_benchmark_task')
                     OR a.tool='delete_process_node' AND json_extract(r.body,'$.shared_metadata_base') IS NOT NULL))
"""


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS collaboration_jobs (
        id TEXT PRIMARY KEY, repository TEXT NOT NULL, status TEXT NOT NULL, automatic INTEGER NOT NULL DEFAULT 0,
        claim TEXT, lease_until REAL NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}',
        requires_pull INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
        phase TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS collaboration_preferences (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), repository TEXT NOT NULL, after_sequence INTEGER NOT NULL)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(collaboration_jobs)")}
    if "requires_pull" not in columns:
        db.execute("ALTER TABLE collaboration_jobs ADD COLUMN requires_pull INTEGER NOT NULL DEFAULT 0")
        # Previous publication requests were authorized to create PRs.
        db.execute("DELETE FROM collaboration_preferences")
        db.execute("""UPDATE collaboration_jobs SET status='failed',claim=NULL,lease_until=0,result=?
            WHERE id<>'pull' AND status IN ('queued','running')""",
                   (json.dumps({"error": "同步已改为直接提交共享 main，请重新选择任务"}, ensure_ascii=False),))
    for name, definition in (("reason", "TEXT NOT NULL DEFAULT ''"), ("phase", "TEXT NOT NULL DEFAULT ''"),
                             ("updated_at", "REAL NOT NULL DEFAULT 0")):
        if name not in columns:
            db.execute(f"ALTER TABLE collaboration_jobs ADD COLUMN {name} {definition}")


def checkout():
    try:
        repo = Repository(ROOT)
    except (OSError, ValueError):
        return None
    return repo if repo.root == ROOT else None


def _queued(db, identity, repository, *, requires_pull=False, reason=""):
    db.execute("""INSERT INTO collaboration_jobs(id,repository,status,requires_pull,reason,updated_at)
        VALUES (?,?,'queued',?,?,?)
        ON CONFLICT(id) DO UPDATE SET status='queued',repository=excluded.repository,automatic=0,
            claim=NULL,lease_until=0,result='{}',requires_pull=excluded.requires_pull,reason=excluded.reason,
            phase='',updated_at=excluded.updated_at
        WHERE collaboration_jobs.status='failed'
           OR (collaboration_jobs.status='queued' AND excluded.id NOT IN ('pull','refresh','rebuild','probe'))
           OR (collaboration_jobs.status='completed' AND (excluded.id IN ('pull','refresh','rebuild','probe')
               OR json_extract(collaboration_jobs.result,'$.outcome.status') IN ('open','closed')))""",
               (identity, repository, int(requires_pull), reason, time()))


class Collaboration:
    def __init__(self, store, repo=None, *, maintainer=False, data_root=None):
        self.store, self.repo = store, repo
        self.data_root = data_root if data_root is not None else ROOT / ".local" / "data-repositories"
        self.maintainer = maintainer
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.audit_lock = threading.Lock()
        self.thread = threading.Thread(target=self.work, name="indust-collaboration", daemon=True)
        self.maintenance = {}
        self.remote = {}
        self.recent: tuple[str | None, list[dict[str, str]]] = (None, [])
        self.distance: tuple[str | None, str | None, int | None] = (None, None, None)
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            initialize(db)
            db.execute("INSERT OR IGNORE INTO collaboration_repository VALUES (1,'')")

    def data_checkout(self, name, *, create=True):
        return data_checkout(self.data_root, name, create=create)

    def client(self):
        with closing(self.store.connect()) as db:
            name = configured_repository(db)
        if not name:
            raise ValueError("请先配置私有数据仓库")
        if not shutil.which("gh"):
            raise ValueError("请安装 GitHub CLI 并运行 gh auth login")
        return GitHub(self.data_checkout(name), name)

    def remote_status(self, repository, head, checkout=None):
        remote = self.remote if self.remote.get("repository") == repository else {}
        observed = remote.get("commit") or (checkout.revision(PUBLISHED_REF, optional=True) if checkout else None)
        distance = self.distance
        if (head, observed) != distance[:2]:
            ahead = None
            if head and observed and checkout:
                try:
                    ahead = int(checkout.git("rev-list", "--count", f"{head}..{observed}"))
                except (OSError, ValueError):
                    pass
            self.distance = distance = (head, observed, ahead)
        return observed, distance[2], remote.get("error", "")

    def publications(self, checkout, head):
        cache = self.recent
        if head != cache[0]:
            self.recent = cache = (head, recent_publications(checkout, head))
        return cache[1]

    def conversation_candidates(self, identity):
        with closing(self.store.connect()) as db:
            return {row[0] for row in db.execute(f"""SELECT r.id FROM runs r LEFT JOIN collaboration_jobs j ON r.id=j.id
                WHERE r.conversation_id=? AND {ELIGIBLE} AND {RETRYABLE}""", (identity,))}

    def snapshot(self, *, details=False, target=""):
        with closing(self.store.connect()) as db:
            db.execute("BEGIN")
            repo = configured_repository(db)
            error = ""
            data = None
            if repo:
                if not shutil.which("gh"):
                    error = "请安装 GitHub CLI 并运行 gh auth login"
                try:
                    data = self.data_checkout(repo, create=False)
                except (ValueError, OSError) as exc:
                    error = str(exc)
            pref = db.execute("SELECT repository FROM collaboration_preferences").fetchone()
            receipts = {identity: {"status": status, "updated_at": updated, **json.loads(result)}
                        for identity, status, result, updated in
                        db.execute("""SELECT id,status,result,updated_at FROM collaboration_jobs
                            WHERE repository=? COLLATE NOCASE AND id IN ('pull','refresh','rebuild','probe')""", (repo,))}
            pull_failed = receipts.get("pull", {}).get("status") == "failed"
            applied = db.execute("""SELECT last_synced_commit,synced_at FROM shared_metadata_sync
                WHERE source_ref=? AND repository=?""", (PUBLISHED_REF, repo)).fetchone()
            head, stamp = applied if applied else (None, None)
            observed, ahead, remote_error = self.remote_status(repo, head, data)
            rows = db.execute(f"""WITH items AS (
                SELECT r.id, json_extract(r.body,'$.prompt') AS title, json_extract(r.body,'$.task_taxonomy.l2_code') AS code,
                       ({ELIGIBLE}) AS eligible, r.sequence AS position, 0 AS files,
                       coalesce(json_extract(r.body,'$.shared_metadata_repository'),'') AS dataset FROM runs r
                UNION ALL
                SELECT id, null, l2_code, status='ready', updated_at, expected, ''
                FROM local_import_batches)
                SELECT i.id,i.title,i.code,j.status,j.result,j.requires_pull,j.phase,j.updated_at,
                       ({RETRYABLE}) AND i.eligible AND (i.dataset='' OR i.dataset=?),i.files
                FROM items i LEFT JOIN collaboration_jobs j ON j.id=i.id WHERE i.eligible OR j.id IS NOT NULL
                ORDER BY (i.id=?) DESC, (coalesce(j.status,'') IN ('queued','running')) DESC,
                (j.status IS NULL OR {RETRYABLE}) DESC, i.position DESC LIMIT ?""", (repo, target, SYNC_LIMIT))
            runs = [{"id": identity, "title": local_import.title(files) if files else (title or "").strip()[:140] or identity[:8],
                     "l2_code": code, **({"imported": True} if files else {}),
                     "selectable": bool(selectable),
                     "job": {"status": "waiting_pull" if pull_failed and requires_pull and status == "queued" else status,
                             **json.loads(result), **({"phase": phase} if phase and status != "completed" else {})}
                     if status else None, "activity_at": updated}
                    for identity, title, code, status, result, requires_pull, phase, updated, selectable, files in rows]
            count = db.execute(f"""SELECT count(*) FROM runs r LEFT JOIN collaboration_jobs j ON j.id=r.id
                WHERE {ELIGIBLE} AND ({RETRYABLE} OR j.status IN ('queued','running'))
                AND coalesce(json_extract(r.body,'$.shared_metadata_repository'),?)=?""", (repo, repo)).fetchone()[0]
            count += db.execute(f"""SELECT count(*) FROM local_import_batches b LEFT JOIN collaboration_jobs j ON j.id=b.id
                WHERE b.status='ready' AND ({RETRYABLE} OR j.status IN ('queued','running'))""").fetchone()[0]
            imports = local_import.summaries(db)
            if details and data:
                for row in runs:
                    if not row["selectable"]:
                        continue
                    try:
                        row["preview"] = publication_preview(data, db, row["id"], observed or head, repository=repo)
                    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                        row["preview"] = {"error": str(exc)[:600]}
            preferences = sync_preferences(db)
            recent = self.publications(data, observed) if details and data else []
            if recent:
                keys = ",".join("?" for _ in recent)
                titles = dict(db.execute(f"""SELECT id,json_extract(body,'$.prompt') FROM runs
                    WHERE id IN ({keys})""", tuple(item["run_id"] for item in recent)))
                recent = [{**item, "title": (titles.get(item["run_id"]) or "").strip()[:140] or item["title"]}
                          for item in recent]
            rollback = db.execute("""SELECT status,result FROM collaboration_jobs WHERE repository=? COLLATE NOCASE
                AND id LIKE 'rollback-%' ORDER BY updated_at DESC LIMIT 1""", (repo,)).fetchone()
        return {"repository": repo, "error": error, "runs": runs, "commit": head, "synced_at": stamp,
                "writable": self.remote.get("writable") if self.remote.get("repository") == repo else None,
                "rollback": {"status": rollback[0], **json.loads(rollback[1])} if rollback else None,
                "remote_commit": observed, "remote_ahead": ahead, "remote_error": remote_error, "pending_count": count,
                "recent": recent, "imports": imports, "import_limits": local_import.LIMITS,
                "preferences": preferences, **receipts,
                "pull": receipts.get("pull"), "refresh": receipts.get("refresh"),
                "automatic": bool(pref and repo and pref[0].casefold() == repo.casefold()),
                "maintainer": self.maintainer, "maintenance": self.maintenance}

    def configure(self, value):
        name = data_repository_name(value)
        with closing(self.store.connect()) as db:
            if configured_repository(db) == name:
                return
        if not self.lock.acquire(blocking=False):
            raise ValueError("请等待当前资料同步完成后更换数据仓库")
        try:
            with closing(self.store.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if configured_repository(db) == name:
                    return
                if db.execute("""SELECT 1 FROM runs WHERE status IN
                    ('queued','running','pausing','cancelling','paused','awaiting_scope','awaiting_stage') LIMIT 1""").fetchone():
                    raise ValueError("请先结束或取消当前研究任务，再更换数据仓库")
                if db.execute("SELECT 1 FROM collaboration_jobs WHERE status='running' AND lease_until>?",
                              (time(),)).fetchone():
                    raise ValueError("请等待当前资料同步完成后更换数据仓库")
                db.execute("INSERT OR REPLACE INTO collaboration_repository VALUES (1,?)", (name,))
                db.execute("DELETE FROM shared_metadata")
                db.execute("DELETE FROM shared_metadata_sync")
                db.execute("DELETE FROM collaboration_preferences")
                db.execute("DELETE FROM collaboration_jobs WHERE id IN ('pull','refresh','rebuild','probe')")
                db.execute("""UPDATE collaboration_jobs SET status='failed',claim=NULL,lease_until=0,phase='',result=?
                    WHERE status IN ('queued','running')""", (json.dumps({"error": "数据仓库已更换，请重新发起操作"},
                                                                          ensure_ascii=False),))
            self.remote, self.maintenance = {}, {}
            self.recent, self.distance = (None, []), (None, None, None)
        finally:
            self.lock.release()
        self.wake.set()

    def submit(self, payload):
        action = payload.get("action")
        if action == "repository":
            self.configure(payload.get("repository"))
            return self.snapshot()
        if action == "preference":
            name, value = payload.get("name"), payload.get("enabled")
            if not isinstance(name, str) or name not in SYNC_DEFAULTS or type(value) is not bool:
                raise ValueError("请选择资料同步偏好")
            with closing(self.store.connect()) as db, db:
                db.execute("INSERT OR REPLACE INTO shared_metadata_preferences VALUES (?,?)", (name, value))
            return self.snapshot()

        if not shutil.which("gh"):
            raise ValueError("请安装 GitHub CLI 并运行 gh auth login")
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            repo = configured_repository(db)
            if not repo:
                raise ValueError("请先配置私有数据仓库")
            if "repository" in payload and data_repository_name(payload["repository"]) != repo:
                raise ValueError("数据仓库已更换，请刷新后重试")
            if action == "rollback_command" and payload.get("confirmed") is True:
                data = self.data_checkout(repo, create=False)
                head, _, _ = self.remote_status(repo, None, data)
                recent = self.publications(data, head) if data else []
                if not recent:
                    raise ValueError("共享 main 暂无可回滚的资料提交")
                sha = recent[0]["commit"]
                command = f"python3 -m industplatform sync --repository {repo} --rollback {sha} --correction-reason '填写回滚原因'"
                return {**self.snapshot(), "rollback_command": command}
            if action in ("sync", "publish", "pull"):
                identities = payload.get("run_ids", []) if action != "pull" else []
                if not isinstance(identities, list) or len(identities) > SYNC_LIMIT or any(
                        not isinstance(identity, str) or not RUN_ID.fullmatch(identity) for identity in identities):
                    raise ValueError(f"请选择最多 {SYNC_LIMIT} 个有效任务")
                if action == "publish" and not identities:
                    raise ValueError("请勾选要发布的任务")
                reason = payload.get("correction_reason", "")
                if not isinstance(reason, str):
                    raise ValueError("请填写有效的纠错说明")
                if reason := reason.strip():
                    required_text(reason, 2000, "纠错说明")
                for identity in dict.fromkeys(identities):
                    if not db.execute(f"""SELECT 1 FROM runs r WHERE r.id=? AND {ELIGIBLE}
                        AND coalesce(json_extract(r.body,'$.shared_metadata_repository'),?)=?
                        UNION ALL SELECT 1 FROM local_import_batches WHERE id=? AND status='ready'""",
                                      (identity, repo, repo, identity)).fetchone():
                        raise ValueError("请选择在当前数据仓库下已完成且有共享资料的任务或导入批次")
                    _queued(db, identity, repo, requires_pull=action == "sync", reason=reason)
                if action != "publish":
                    _queued(db, "pull", repo)
                    db.execute("DELETE FROM collaboration_jobs WHERE id='refresh' AND status<>'running'")
            elif action == "rollback":
                sha, reason = payload.get("commit"), payload.get("correction_reason")
                if not isinstance(sha, str) or not SHA.fullmatch(sha):
                    raise ValueError("请选择有效的资料提交")
                required_text(reason, 2000, "回滚原因")
                _queued(db, f"rollback-{repo}-{sha}", repo, reason=reason.strip())
            elif action == "test":
                _queued(db, "probe", repo)
            elif action == "rebuild" and payload.get("confirmed") is True:
                _queued(db, "rebuild", repo)
            elif action == "auto_sync" and type(payload.get("enabled")) is bool:
                if payload["enabled"]:
                    db.execute("""INSERT INTO collaboration_preferences
                        SELECT 1,?,coalesce(max(sequence),0) FROM runs WHERE true
                        ON CONFLICT(singleton) DO UPDATE SET repository=excluded.repository,after_sequence=excluded.after_sequence
                        WHERE collaboration_preferences.repository<>excluded.repository""", (repo,))
                else:
                    db.execute("DELETE FROM collaboration_preferences")
                    db.execute("DELETE FROM collaboration_jobs WHERE automatic=1 AND status='queued'")
            else:
                raise ValueError("请选择资料协作操作")
        self.wake.set()
        return self.snapshot()

    def claim(self):
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            repository = configured_repository(db)
            if not repository:
                return None
            now = time()
            db.execute(f"""INSERT OR IGNORE INTO collaboration_jobs(id,repository,status,automatic,updated_at)
                SELECT r.id,p.repository,'queued',1,? FROM runs r CROSS JOIN collaboration_preferences p
                WHERE p.repository=? AND json_extract(r.body,'$.shared_metadata_repository')=p.repository
                AND r.sequence>p.after_sequence AND {ELIGIBLE}""", (now, repository))
            if db.execute("SELECT 1 FROM collaboration_jobs WHERE status='running' AND lease_until>?", (now,)).fetchone():
                return None
            job = db.execute("""SELECT j.id,j.repository,j.reason,j.automatic FROM collaboration_jobs j
                WHERE (j.status='queued' OR (j.status='running' AND j.lease_until<=?))
                AND j.repository=? COLLATE NOCASE
                AND (j.requires_pull=0 OR EXISTS (SELECT 1 FROM collaboration_jobs p
                     WHERE p.id='pull' AND p.repository=j.repository COLLATE NOCASE AND p.status='completed'))
                ORDER BY CASE j.id WHEN 'pull' THEN 0 WHEN 'rebuild' THEN 0 WHEN 'refresh' THEN 2 WHEN 'probe' THEN 3
                         ELSE 1 END,j.rowid LIMIT 1""",
                             (now, repository)).fetchone()
            if not job:
                return None
            claim = uuid4().hex
            db.execute("UPDATE collaboration_jobs SET status='running',claim=?,lease_until=?,updated_at=? WHERE id=?",
                       (claim, now + 600, now, job[0]))
            return (*job, claim)

    def phase(self, identity, claim, step):
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE collaboration_jobs SET phase=?,updated_at=? WHERE id=? AND claim=?",
                       (step, time(), identity, claim))

    def finish(self, identity, claim, state, result, *, refresh=None):
        with closing(self.store.connect()) as db, db:
            saved = db.execute("""UPDATE collaboration_jobs SET status=?,result=?,claim=NULL,lease_until=0,reason='',updated_at=?
                WHERE id=? AND claim=?""", (state, json.dumps(result, ensure_ascii=False), time(), identity, claim))
            if saved.rowcount and refresh:
                _queued(db, "refresh", refresh)

    def run_job(self):
        job = self.claim()
        if not job:
            return False
        identity, repository, reason, automatic, claim = job
        try:
            api = self.client()
            if api.name.casefold() != repository.casefold():
                raise ValueError("数据仓库已更换，请重新发起操作")
            if identity in PULL_JOBS:
                result = published_data(api.repo, api, self.store.path, rebuild=True) if identity == "rebuild" else (
                    published_data(api.repo, api, self.store.path))
            elif identity == "probe":
                result = self.probe(api)
            elif identity.startswith("rollback-"):
                result = publish_rollback(api.repo, api, identity.rsplit("-", 1)[1], reason=reason)
            else:
                result = publish_run(api.repo, api, self.store.path, identity, reason=reason,
                                     progress=lambda step: self.phase(identity, claim, step))
            if result.get("commit"):
                previous = self.remote if self.remote.get("repository") == repository else {}
                self.remote = {"repository": repository, "commit": result["commit"],
                               "writable": result.get("writable", previous.get("writable"))}
            with closing(self.store.connect()) as db:
                refresh = result.get("status") == "pushed" and (not automatic or sync_preferences(db)["auto_import"])
            self.finish(identity, claim, "completed", {"outcome": result},
                        refresh=repository if refresh else None)
        except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
            self.finish(identity, claim, "failed", {"error": str(exc)[:800] or "协作操作失败"})
        return True

    def probe(self, api):
        info = api.private_repository()
        return {"commit": api.fetch_main(), "writable": bool(info.get("permissions", {}).get("push"))}

    def audit_run(self):
        if self.repo:
            try:
                with self.audit_lock, closing(self.store.connect()) as db:
                    if sync_preferences(db)["audit_ignores"]:
                        audit_run_ignores(self.repo)
            except (ValueError, OSError, sqlite3.Error):
                logger.exception("任务收尾时无法更新本地忽略规则")

    def work(self):
        next_check = next_probe = 0.0
        while not self.stop.is_set():
            try:
                with self.lock:
                    if self.stop.is_set():
                        break
                    handled = self.run_job()
                    if self.maintainer and not handled and monotonic() >= next_check:
                        next_check = monotonic() + 30
                        try:
                            api = self.client()
                            self.maintenance = maintain(api.repo, api)
                        except (ValueError, OSError, KeyError) as exc:
                            self.maintenance = {"error": str(exc)[:400]}
                    if not handled and monotonic() >= next_probe:
                        next_probe = monotonic() + 45
                        repo = ""
                        try:
                            api = self.client()
                            repo = api.name
                            self.remote = {"repository": repo, **self.probe(api)}
                        except (ValueError, OSError, KeyError) as exc:
                            self.remote = {"repository": repo, "error": str(exc)[:400]}
            except Exception:
                logger.exception("资料协作队列执行失败")
            if not self.wake.wait(1):
                continue
            self.wake.clear()

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()

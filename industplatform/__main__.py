"""Launch the local web app, export the offline demo, or expose Agent tools."""

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

from . import __version__


def main():
    parser = argparse.ArgumentParser(prog="industplatform")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="start the dashboard and API")
    server.add_argument("--port", type=int, default=8765)
    start = commands.add_parser("start", help="start the local workbench and open the dashboard")
    start.add_argument("--port", type=int, default=18765)
    start.add_argument("--no-browser", action="store_true")
    start.add_argument("--maintain", action="store_true", help="refresh the shared metadata index")
    for entry in (server, start):
        entry.add_argument("--db", type=Path, default=Path(os.environ.get("INDUST_DB", ".local/industplatform.sqlite3")))
    demo = commands.add_parser("demo", help="run the offline benchmark and optionally export its ZIP")
    demo.add_argument("--output", type=Path)
    demo.add_argument("--seed", type=int, metavar="0–999999")
    mcp = commands.add_parser("mcp", help="expose the local HTTP service to MCP clients")
    mcp.add_argument("--port", type=int, default=8765)
    mcp.add_argument("--session-port", type=int, help=argparse.SUPPRESS)
    sync = commands.add_parser("sync", help="queue the workbench Git synchronization")
    sync.add_argument("--port", type=int, default=18765)
    sync.add_argument("--run-id", action="append", default=[], help="check and publish a completed run")
    sync.add_argument("--correction-reason", help="explain a versioned correction")
    sync.add_argument("--status", action="store_true", help="read synchronization receipts")
    sync.add_argument("--repository", help="select a private GitHub data repository (owner/name or URL)")
    sync.add_argument("--rollback", metavar="SHA", help="undo a shared publication with a versioned correction")
    options = parser.parse_args()
    if options.command == "demo" and options.seed is not None and not 0 <= options.seed <= 999999:
        parser.error("--seed must be between 0 and 999999")

    logging.basicConfig(level=logging.INFO)
    if options.command in ("serve", "start"):
        from .server import serve

        opening = options.command == "start"
        if opening and not options.db.is_absolute():
            from scripts.repo_support import ROOT

            options.db = ROOT / options.db
        previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
        try:
            serve(options.port, options.db, browser=opening and not options.no_browser,
                  maintainer=opening and options.maintain)
        finally:
            signal.signal(signal.SIGTERM, previous)
    elif options.command == "mcp":
        from .mcp import listen

        listen(options.port, session_port=options.session_port)
    elif options.command == "sync":
        from .collaboration import COLLABORATION_PATH
        from .mcp import call_api

        if sum(bool(value) for value in (options.status, options.run_id, options.rollback)) > 1:
            sync.error("use only one of --status, --run-id and --rollback")
        if options.correction_reason and not (options.run_id or options.rollback):
            sync.error("--correction-reason requires --run-id or --rollback")
        if options.rollback and not (options.correction_reason or "").strip():
            sync.error("--rollback requires --correction-reason")
        payload = None if options.status else {"action": "pull"}
        if options.run_id:
            payload = {"action": "publish", "run_ids": options.run_id,
                       "correction_reason": options.correction_reason or ""}
        if options.rollback:
            payload = {"action": "rollback", "commit": options.rollback,
                       "correction_reason": options.correction_reason}
        base_url = f"http://127.0.0.1:{options.port}"
        try:
            if options.repository is not None and (options.run_id or options.rollback or options.status):
                from scripts.github_automation import data_repository_name

                expected = data_repository_name(options.repository)
                current = call_api(base_url, COLLABORATION_PATH)["repository"]
                if not expected or current != expected:
                    raise ValueError("所选数据仓库与工作台当前配置不一致")
                if payload:
                    payload["repository"] = expected
            elif options.repository is not None:
                configured = call_api(base_url, COLLABORATION_PATH,
                                      {"action": "repository", "repository": options.repository})
                payload = {"action": "pull", "repository": configured["repository"]} if configured["repository"] else None
            result = call_api(base_url, COLLABORATION_PATH, payload)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        fields = ("repository", "commit", "remote_commit", "remote_ahead", "pending_count", "pull", "refresh", "rollback", "error")
        print(json.dumps({**{field: result[field] for field in fields}, "jobs": [
            {"run_id": run["id"], **run["job"]} for run in result["runs"]
            if run["job"] and (not options.run_id or run["id"] in options.run_id)
        ]}, ensure_ascii=False, indent=2))
    elif options.command == "demo":
        from .storage import RunStore
        from .workflow import benchmark_zip, process_run

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs.sqlite3")
            store.create("demo-cnc", "offline", "", options.seed)
            run = store.claim()
            process_run(store, run)
            if run["status"] != "completed":
                print(json.dumps(run, ensure_ascii=False, indent=2))
                return 1
            if options.output:
                options.output.parent.mkdir(parents=True, exist_ok=True)
                options.output.write_bytes(benchmark_zip(run))
            print(json.dumps({
                "status": run["status"],
                "metrics": run["result"]["metrics"],
                "tool": run["result"]["tool"],
                "export": str(options.output) if options.output else None,
            }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

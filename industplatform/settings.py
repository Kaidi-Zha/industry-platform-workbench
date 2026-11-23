"""Local workbench preferences stored alongside the run queue."""

import json
import re
from contextlib import closing
from pathlib import Path
from typing import Any

from .codex_local import API_GATEWAY, MODEL_ID, MODES, SUBSCRIPTION


DEFAULTS: dict[str, Any] = {
    "default_execution_mode": API_GATEWAY,
    "default_codex_model": "",
    "disabled_codex_models": [],
    "codex_executable": "",
    "default_model": "",
    "default_worker_model": "",
    "resume_last_chat": False,
    "send_shortcut": "mod-enter",
    "theme": "system",
    "density": "comfortable",
    "history_turns": 3,
    "max_child_runs": 2,
    "max_authored_tasks": 2,
    "max_model_turns": 8,
    "max_tool_calls": 12,
    "wait_seconds": 15,
    "allow_keyword_tools": True,
    "agent_guidance": "",
    "sandbox_mode": "read-only",
    "workspace_network_access": False,
    "approval_mode": "never",
    "approval_model_native": "",
    "approval_model_api": "",
}
PERMISSION_PREFERENCES = (
    "sandbox_mode", "workspace_network_access", "approval_mode", "approval_model_native", "approval_model_api",
)
APPROVAL_MODES = ("never", "ask", "auto_review")
AGENT_PREFERENCES = (
    "history_turns", "max_child_runs", "max_authored_tasks", "max_model_turns",
    "max_tool_calls", "wait_seconds", "allow_keyword_tools", "agent_guidance",
)
_AGENT_INTEGER_MINIMUMS = {"max_model_turns": 2, "max_tool_calls": 1, "wait_seconds": 1}
MAX_AGENT_GUIDANCE_CHARS = 1200
_CHOICES = {
    "default_execution_mode": MODES,
    "send_shortcut": ("mod-enter", "enter"),
    "theme": ("system", "light", "dark"),
    "density": ("comfortable", "compact"),
    "sandbox_mode": ("read-only", "workspace-write"),
    "approval_mode": APPROVAL_MODES,
}


def _valid(key: str, value: Any) -> bool:
    if type(value) is not type(DEFAULTS[key]):
        return False
    if key in _CHOICES:
        return value in _CHOICES[key]
    if key == "default_codex_model":
        return not value or MODEL_ID.fullmatch(value) is not None
    if key == "disabled_codex_models":
        return (len(value) <= 400 and all(isinstance(item, str) and MODEL_ID.fullmatch(item) for item in value)
                and len(set(value)) == len(value))
    if key == "codex_executable":
        return not value or (len(value) <= 1024 and "\0" not in value and Path(value).expanduser().is_absolute())
    if key == "default_model":
        return value in ("", "@codex") or bool(re.fullmatch(r"[0-9a-f]{32}", value))
    if key in ("approval_model_api", "default_worker_model"):
        return not value or bool(re.fullmatch(r"[0-9a-f]{32}", value))
    if key == "approval_model_native":
        return not value or ("astra" not in value.lower() and bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value)))
    if key == "agent_guidance":
        return len(value) <= MAX_AGENT_GUIDANCE_CHARS
    if type(value) is int:
        return _AGENT_INTEGER_MINIMUMS.get(key, 0) <= value <= DEFAULTS[key]
    return True


class Settings:
    def __init__(self, store):
        self.store = store
        with closing(store.connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

    @staticmethod
    def _read(connection):
        settings = DEFAULTS.copy()
        has_mode = False
        for key, value in connection.execute("SELECT key, value FROM preferences"):
            if key in settings:
                settings[key] = json.loads(value)
                has_mode |= key == "default_execution_mode"
        if not has_mode and settings["default_model"] == "@codex":
            settings["default_execution_mode"] = SUBSCRIPTION
        return settings

    def get(self):
        with closing(self.store.connect()) as connection:
            return self._read(connection)

    def update(self, changes):
        if not isinstance(changes, dict):
            raise ValueError("设置必须为 JSON 对象")
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise ValueError("未知设置项")
            if not _valid(key, value):
                raise ValueError("设置值无效：{}".format(key))
        with closing(self.store.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany("""
                INSERT INTO preferences(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, [(key, json.dumps(value)) for key, value in changes.items()])
            return self._read(connection)

    def storage(self):
        path = self.store.path.resolve()
        with closing(self.store.connect()) as connection:
            run_count, conversation_count = connection.execute("""
                SELECT COUNT(*), COUNT(DISTINCT conversation_id) FROM runs
            """).fetchone()
            database_bytes = path.stat().st_size
            try:
                database_bytes += Path(str(path) + "-wal").stat().st_size
            except FileNotFoundError:
                pass
        return {
            "database_path": str(path),
            "database_bytes": database_bytes,
            "run_count": run_count,
            "conversation_count": conversation_count,
        }

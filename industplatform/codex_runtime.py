"""Repository-local state for Codex subprocesses."""

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX_STATE_DIR = ROOT / ".local" / "codex"


def prepare_codex_state():
    state_paths = ("config.toml", "auth.json", "log", "sessions")
    for path in (CODEX_STATE_DIR, *(CODEX_STATE_DIR / name for name in state_paths)):
        if not path.resolve().is_relative_to(ROOT):
            raise OSError("Codex 配置与运行状态必须位于本仓库内")

    CODEX_STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = CODEX_STATE_DIR / "config.toml"
    try:
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write('# industplatform Codex runtime\n[features]\napps = false\n')
    return CODEX_STATE_DIR


def codex_environment():
    state = str(prepare_codex_state())
    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "INDUST_BRAVE_SEARCH_KEY", "DEEPSEEK_API_KEY"):
        env.pop(name, None)
    env.update(CODEX_HOME=state, CODEX_SQLITE_HOME=state)
    return env

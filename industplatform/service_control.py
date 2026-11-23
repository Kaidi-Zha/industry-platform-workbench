"""Local command-line control of a workbench instance on a specific port."""

import os
import secrets
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep

from scripts.repo_support import ROOT

STOP_PATH = "/api/workbench/stop"


def token_path(port: int) -> Path:
    return ROOT / ".local" / "workbench-ports" / f"{port}.token"


@contextmanager
def register(port):
    path = token_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".start-", delete=False, mode="w", encoding="ascii") as stream:
        temporary = Path(stream.name)
        stream.write(token)
    try:
        os.replace(temporary, path)
        try:
            yield token
        finally:
            path.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except ConnectionRefusedError:
        return False


def stop(port):
    from .mcp import call_api

    address = f"localhost:{port}"
    if not listening(port):
        return f"No workbench is running on {address}."
    path = token_path(port)
    try:
        token = path.read_text(encoding="ascii")
    except (FileNotFoundError, UnicodeError):
        raise ValueError(f"Cannot verify the workbench on {address}. Use Ctrl+C in its original terminal.") from None
    reply = call_api(f"http://127.0.0.1:{port}", STOP_PATH, {"token": token})
    if reply != {"stopping": True}:
        raise ValueError(f"The workbench on {address} did not confirm shutdown.")
    deadline = monotonic() + 12
    while monotonic() < deadline:
        if not path.exists() and not listening(port):
            return f"Workbench stopped on {address}."
        sleep(0.1)
    raise ValueError(f"Could not confirm shutdown on {address}. Check the original terminal.")

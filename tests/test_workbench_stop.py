import re
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def loopback_available():
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("the environment disallows local listeners")


def stop(port):
    return subprocess.run([sys.executable, "-m", "industplatform", "stop", "--port", str(port)],
                          cwd=ROOT, capture_output=True, text=True, timeout=20)


@pytest.fixture
def workbench(tmp_path):
    database = tmp_path / "workbench.sqlite3"
    output = tmp_path / "workbench.log"
    with output.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "industplatform", "start", "--no-browser",
                                    "--port", "0", "--db", str(database)], cwd=ROOT,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            ready = re.search(r"http://127\.0\.0\.1:(\d+)", output.read_text(encoding="utf-8", errors="replace"))
            if ready:
                yield process, int(ready[1]), database
                return
            time.sleep(0.05)
        pytest.fail("Workbench startup failed:\n" + output.read_text(encoding="utf-8", errors="replace"))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_stop_closes_listener_and_confirms_repeated_stop(workbench):
    process, port, _ = workbench
    token = ROOT / ".local" / "workbench-ports" / f"{port}.token"
    assert token.is_file()
    result = stop(port)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"Workbench stopped on localhost:{port}."
    assert process.wait(timeout=5) == 0
    assert not token.exists()
    with pytest.raises(ConnectionRefusedError):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pytest.fail("the stopped workbench was still listening")
    result = stop(port)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"No workbench is running on localhost:{port}."


def test_stop_refuses_active_publication(workbench):
    process, port, database = workbench
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("""INSERT INTO collaboration_jobs(id,repository,status,lease_until)
            VALUES ('busy-fixture','owner/library','running',?)""", (time.time() + 60,))
    try:
        result = stop(port)
        assert result.returncode != 0
        assert "Workbench stopped" not in result.stdout
        assert process.poll() is None
    finally:
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("DELETE FROM collaboration_jobs WHERE id='busy-fixture'")
    result = stop(port)
    assert result.returncode == 0, result.stderr
    assert process.wait(timeout=5) == 0


def test_stop_leaves_foreign_listener_running():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = stop(port)
        assert result.returncode != 0
        assert "Workbench stopped" not in result.stdout
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass

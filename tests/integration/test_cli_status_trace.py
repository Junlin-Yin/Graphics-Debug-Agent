from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _subprocess_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home)}


def _write_fake_config(home: Path, response: str = "integration answer") -> None:
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        f"""
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "{response}"
""".strip(),
        encoding="utf-8",
    )


def _run_one_shot(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home)
    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable, "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    session_id = re.search(
        r"sess_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-[0-9a-f]{4}",
        result.stderr + result.stdout,
    )
    if session_id is None:
        db_path = workspace / ".sessions" / "runtime.db"
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            session_id = re.match(
                r"(.*)",
                conn.execute("SELECT session_id FROM sessions").fetchone()[0],
            )
    return executable, home, workspace, session_id.group(0)


def test_status_and_trace_commands_inspect_completed_one_shot(tmp_path) -> None:
    executable, home, workspace, session_id = _run_one_shot(tmp_path)

    status = subprocess.run(
        [executable, "status", session_id],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    trace = subprocess.run(
        [executable, "trace", session_id],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert status.returncode == 0
    assert f"session_id: {session_id}" in status.stdout
    assert "session_status: completed" in status.stdout
    assert "latest_run_status: completed" in status.stdout
    assert "latest_checkpoint_id:" in status.stdout

    assert trace.returncode == 0
    assert "trace_path:" in trace.stdout
    assert f"session_id: {session_id}" in trace.stdout
    assert "terminal_status: completed" in trace.stdout
    trace_path = workspace / ".sessions" / session_id / "trace.md"
    assert trace_path.is_file()
    trace_text = trace_path.read_text(encoding="utf-8")
    assert "## Session Summary" in trace_text
    assert "model_call_started" in trace_text
    assert "checkpoint_written" in trace_text
    assert "session_completed" in trace_text


def test_one_shot_writes_engine_log_json_lines(tmp_path) -> None:
    _, _, workspace, session_id = _run_one_shot(tmp_path)

    log_path = workspace / ".sessions" / session_id / "logs" / "engine.log"
    assert log_path.is_file()
    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert entries
    assert {"timestamp", "session_id", "run_id", "step_id", "level", "event", "message", "metadata"} <= set(entries[0])
    assert "session_started" in {entry["event"] for entry in entries}
    assert "model_call_started" in {entry["event"] for entry in entries}
    assert "checkpoint_written" in {entry["event"] for entry in entries}


def test_status_and_trace_missing_session_errors(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    executable = str(Path(sys.executable).parent / "debug-agent")

    status = subprocess.run(
        [executable, "status", "sess_missing"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    trace = subprocess.run(
        [executable, "trace", "sess_missing"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert status.returncode == 1
    assert "No session found for id: sess_missing" in status.stderr
    assert trace.returncode == 1
    assert "No session found for id: sess_missing" in trace.stderr

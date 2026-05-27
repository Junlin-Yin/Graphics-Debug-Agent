from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.contracts import RunEvent, utc_now_iso


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


def test_trace_command_renders_phase1_skill_approval_tool_and_compression_events(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home)
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection, db.path.parent)
        session = sessions.create(
            workspace_root=workspace,
            approval_mode="normal",
            config_snapshot={"provider": "fake", "model": "fake-model"},
            session_id="sess_phase1_trace",
        )
        run = runs.create_prompt_run(session.session_id, run_id="run_phase1_trace")
        sessions.set_active_run(session.session_id, run.run_id)
        for index, (kind, payload) in enumerate(
            [
                (
                    "skill_snapshot_created",
                    {
                        "skill_name": "alpha",
                        "execution_mode": "prompt",
                        "source_scope": "project",
                        "content_hash": "sha256:alpha",
                        "reference_count": 1,
                    },
                ),
                (
                    "skill_activated",
                    {
                        "skill_name": "alpha",
                        "content_hash": "sha256:alpha",
                        "activation_reason": "model_requested",
                        "scope": "run",
                    },
                ),
                (
                    "approval_requested",
                    {
                        "tool_name": "write_file",
                        "risk_level": "write",
                        "scope_signature": "write:src/out.txt",
                        "target": "src/out.txt",
                    },
                ),
                (
                    "approval_decision_recorded",
                    {
                        "tool_name": "write_file",
                        "decision": "approved_once",
                        "grant_scope": "once",
                        "scope_signature": "write:src/out.txt",
                    },
                ),
                (
                    "tool_call_denied",
                    {
                        "tool_name": "shell_exec",
                        "arguments": {"argv": ["git", "status"]},
                        "result": {
                            "error": {
                                "error_class": "policy_denied",
                                "message": "Shell command denied by policy.",
                            }
                        },
                    },
                ),
                (
                    "context_optimized",
                    {
                        "trigger": "compression",
                        "context_snapshot_id": "ctx_phase1",
                        "checkpoint_id": "chk_context",
                        "omitted_tool_result_count": 0,
                        "evicted_message_count": 2,
                        "evicted_model_call_group_count": 1,
                        "artifact_refs": [],
                        "reduced_from_tokens": 1200,
                        "reduced_to_tokens": 450,
                    },
                ),
            ]
        ):
            events.append(
                RunEvent(
                    event_id=f"evt_phase1_{index}",
                    timestamp=utc_now_iso(),
                    session_id=session.session_id,
                    run_id=run.run_id,
                    step_id=None,
                    kind=kind,
                    payload=payload,
                )
            )
    finally:
        db.close()

    executable = str(Path(sys.executable).parent / "debug-agent")
    trace = subprocess.run(
        [executable, "trace", "sess_phase1_trace"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert trace.returncode == 0
    trace_text = (
        workspace / ".sessions" / "sess_phase1_trace" / "trace.md"
    ).read_text(encoding="utf-8")
    assert "skill_snapshot_created" in trace_text
    assert "skill_activated" in trace_text
    assert "approval_requested" in trace_text
    assert "approval_decision_recorded" in trace_text
    assert "tool_call_denied" in trace_text
    assert "Shell command denied by policy." in trace_text
    assert "context_optimized" in trace_text
    assert "trigger=compression" in trace_text
    assert "reduced=1200->450" in trace_text


def test_startup_status_and_trace_fail_closed_on_legacy_schema(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home)
    sessions_dir = workspace / ".sessions"
    sessions_dir.mkdir()
    with sqlite3.connect(sessions_dir / "runtime.db") as conn:
        conn.execute("PRAGMA user_version = 0")
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions (session_id, status) VALUES ('sess_legacy', 'running')"
        )
        conn.commit()

    executable = str(Path(sys.executable).parent / "debug-agent")
    startup = subprocess.run(
        [executable, "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        [executable, "status", "sess_legacy"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    trace = subprocess.run(
        [executable, "trace", "sess_legacy"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    expected = (
        "Phase 0/0.5 runtime databases are unsupported by Phase 1. Move or "
        "remove .sessions/ or use a fresh workspace."
    )
    assert startup.returncode == 4
    assert expected in startup.stderr
    assert status.returncode == 4
    assert expected in status.stderr
    assert trace.returncode == 4
    assert expected in trace.stderr
    with sqlite3.connect(sessions_dir / "runtime.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT status FROM sessions WHERE session_id = 'sess_legacy'"
            ).fetchone()[0]
            == "running"
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "run_events" not in tables

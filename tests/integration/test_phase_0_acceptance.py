from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.tools.broker import ToolBroker


def _subprocess_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home)}


def _write_fake_config(home: Path, response: str = "acceptance answer") -> None:
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


def _console_script() -> str:
    return str(Path(sys.executable).parent / "debug-agent")


def _session_id(workspace: Path) -> str:
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        return conn.execute("SELECT session_id FROM sessions").fetchone()[0]


def test_phase_0_one_shot_status_trace_and_persistence_acceptance(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home)
    executable = _console_script()

    one_shot = subprocess.run(
        [executable, "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    session_id = _session_id(workspace)
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

    assert one_shot.returncode == 0
    assert one_shot.stdout == "acceptance answer\n"
    assert status.returncode == 0
    assert f"session_id: {session_id}" in status.stdout
    assert "session_status: completed" in status.stdout
    assert "approval_mode: yolo" in status.stdout
    assert "latest_checkpoint_id:" in status.stdout
    assert "updated_at:" in status.stdout
    assert trace.returncode == 0
    assert "terminal_status: completed" in trace.stdout

    db_path = workspace / ".sessions" / "runtime.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        event_rows = conn.execute("SELECT rowid FROM run_events ORDER BY rowid").fetchall()
        assert event_rows == sorted(event_rows)

    log_path = workspace / ".sessions" / session_id / "logs" / "engine.log"
    trace_path = workspace / ".sessions" / session_id / "trace.md"
    assert log_path.is_file()
    assert trace_path.is_file()
    log_events = {
        json.loads(line)["event"]
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    }
    assert {"session_started", "model_call_started", "checkpoint_written"} <= log_events
    trace_text = trace_path.read_text(encoding="utf-8")
    assert "session_started" in trace_text
    assert "model_call_started" in trace_text
    assert "checkpoint_written" in trace_text
    assert "session_completed" in trace_text


def test_phase_0_repl_two_turn_acceptance(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "repl acceptance answer")

    repl = subprocess.run(
        [_console_script()],
        cwd=workspace,
        env=_subprocess_env(home),
        input="hello\n/status\ntell me one more thing\n/exit\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert repl.returncode == 0
    assert repl.stdout.count("repl acceptance answer\n") == 2
    assert "session_id:" in repl.stdout
    assert "session_status: running" in repl.stdout
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "completed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "completed"
        assert (
            conn.execute("SELECT COUNT(*) FROM run_events WHERE kind = 'user_message'").fetchone()[0]
            == 2
        )


def test_phase_0_active_workspace_conflict_acceptance(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": "answer",
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": "You are debug-agent, a local debugging assistant. Answer concisely and use only tools exposed by the runtime.",
    }
    from debug_agent.runtime.orchestrator import RuntimeOrchestrator

    first = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        conn.execute("UPDATE sessions SET status = 'running' WHERE session_id = ?", (first.session_id,))
        conn.commit()

    conflict = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert conflict.exit_code == 3
    assert "An active debug-agent session already owns this workspace." in conflict.message


def test_phase_0_toolbroker_artifact_and_trace_acceptance(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection)
        artifacts = ArtifactStore(db.connection)
        session = sessions.create(
            workspace_root=workspace,
            approval_mode="yolo",
            config_snapshot={},
            session_id="sess_tools",
        )
        run = runs.create_prompt_run(session.session_id, run_id="run_tools")
        sessions.set_active_run(session.session_id, run.run_id)
        (workspace / "large.txt").write_text("x" * (16 * 1024 + 1), encoding="utf-8")
        broker = ToolBroker(event_writer=events, artifact_store=artifacts)

        result = broker.invoke(
            session.session_id,
            run.run_id,
            "read_file",
            {"path": "large.txt"},
            {"workspace_root": str(workspace)},
        )
        from debug_agent.observability.trace_writer import TraceWriter

        trace = TraceWriter(db.connection).refresh_if_stale(session.session_id)

        assert result.status == "ok"
        assert result.output is None
        assert len(result.artifacts) == 1
        assert artifacts.get(result.artifacts[0]).artifact_type == "text"
        event_kinds = [event.kind for event in events.list_for_run(run.run_id)]
        assert event_kinds == [
            "tool_call_started",
            "artifact_registered",
            "tool_call_completed",
        ]
        trace_text = trace.trace_path.read_text(encoding="utf-8")
        assert "artifact_registered" in trace_text
        assert "tool_call_completed" in trace_text
        assert result.artifacts[0] in trace_text
    finally:
        db.close()


def test_phase_0_reserved_commands_are_not_exposed(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    executable = _console_script()

    for args in (
        ["resume"],
        ["plugins", "list"],
        ["/resume"],
        ["/compress"],
        ["/skills"],
        ["/agents"],
        ["/models"],
    ):
        result = subprocess.run(
            [executable, *args],
            cwd=workspace,
            env=_subprocess_env(home),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "Usage:" in result.stderr


def test_phase_0_invalid_config_exits_4_without_session(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_dir = home / ".debug-agent"
    home.mkdir()
    workspace.mkdir()
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults
provider = "fake"
""".strip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [_console_script(), "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4
    assert "Invalid config.toml" in result.stderr
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_phase_0_model_config_failure_after_session_records_config_error(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = {
        "provider": "anthropic",
        "model": "kimi-k2.5",
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "auth": {"api_key_env": "ANTHROPIC_API_KEY", "api_key_present": False},
        "provider_settings": {
            "base_url_env": "ANTHROPIC_BASE_URL",
            "base_url_present": False,
        },
    }
    from debug_agent.runtime.orchestrator import RuntimeOrchestrator

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 4
    assert result.session_id is not None
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        assert conn.execute("SELECT kind FROM checkpoints").fetchone()[0] == "error"
        payloads = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload_json FROM run_events WHERE kind IN ('run_failed', 'session_failed')"
            )
        ]
    assert payloads
    assert {payload["error_class"] for payload in payloads} == {"config_error"}


def test_phase_0_trace_surfaces_missing_artifact_path(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        artifacts = ArtifactStore(db.connection)
        session = sessions.create(
            workspace_root=workspace,
            approval_mode="yolo",
            config_snapshot={},
            session_id="sess_missing_artifact",
        )
        run = runs.create_prompt_run(session.session_id, run_id="run_missing_artifact")
        artifact = artifacts.write_text(
            session_id=session.session_id,
            run_id=run.run_id,
            filename="gone.txt",
            content="artifact text",
            metadata={"source": "test"},
            artifact_id="art_missing_path",
        )
        artifacts.resolve_path(artifact.artifact_id).unlink()
        from debug_agent.observability.trace_writer import TraceWriter

        trace = TraceWriter(db.connection).refresh_if_stale(session.session_id)

        trace_text = trace.trace_path.read_text(encoding="utf-8")
        assert "art_missing_path" in trace_text
        assert "exists=false" in trace_text
        assert "missing" in trace_text
    finally:
        db.close()


def test_phase_0_sqlite_bootstrap_failure_is_surfaced(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home)
    (workspace / ".sessions").write_text("not a directory", encoding="utf-8")

    result = subprocess.run(
        [_console_script(), "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Runtime database bootstrap failed" in result.stderr
    assert "Traceback" not in result.stderr

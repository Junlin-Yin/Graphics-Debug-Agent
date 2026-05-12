from __future__ import annotations

import sqlite3

from debug_agent.runtime.orchestrator import RuntimeOrchestrator


def _config(response: str = "fake answer") -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": response,
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer concisely "
            "and use only tools exposed by the runtime."
        ),
    }


def test_one_shot_success_persists_lifecycle_and_completes_session(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config("one shot answer")
    )

    assert result.exit_code == 0
    assert result.assistant_output == "one shot answer"
    assert result.error is None

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_row = conn.execute(
            "SELECT status, approval_mode, active_run_id FROM sessions"
        ).fetchone()
        run_row = conn.execute("SELECT status, run_type FROM runs").fetchone()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_count = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]

    assert session_row == ("completed", "yolo", None)
    assert run_row == ("completed", "prompt")
    assert event_kinds == [
        "session_started",
        "run_started",
        "user_message",
        "model_call_started",
        "model_call_completed",
        "assistant_message",
        "checkpoint_written",
        "run_completed",
        "session_completed",
    ]
    assert checkpoint_count == 1


def test_one_shot_model_failure_marks_run_and_session_failed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config()
    config["fake_error"] = "provider failed"

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 1
    assert result.assistant_output is None
    assert result.error["error_class"] == "model_error"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
    assert "run_failed" in event_kinds
    assert "session_failed" in event_kinds


def test_one_shot_active_workspace_conflict_returns_policy_exit(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", _config())
    assert first.exit_code == 0

    db_path = workspace / ".sessions" / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET status = 'running' WHERE session_id = ?", (first.session_id,))
        conn.commit()

    conflict = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", _config())

    assert conflict.exit_code == 3
    assert conflict.error["error_class"] == "user_error"
    assert "An active debug-agent session already owns this workspace." in conflict.message
    assert f"Session: {first.session_id}" in conflict.message

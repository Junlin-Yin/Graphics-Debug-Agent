from __future__ import annotations

import sqlite3

from debug_agent.persistence.sqlite import (
    PHASE_3_SCHEMA_USER_VERSION,
    READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
    STARTUP_LEGACY_RESET_GUIDANCE,
)
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
        "development": {
            "allow_incomplete_phase3_prompt_execution": True,
        },
    }


def test_status_and_trace_fail_closed_while_startup_resets_legacy_database(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute(
            "INSERT INTO sessions (session_id, status) VALUES ('sess_legacy', 'running')"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    orchestrator = RuntimeOrchestrator(workspace_root=workspace)

    status = orchestrator.status("sess_legacy")
    trace = orchestrator.trace("sess_legacy")
    one_shot = orchestrator.run_one_shot("hello", _config())

    for result in (status, trace):
        assert result.exit_code == 6
        assert READ_ONLY_SCHEMA_FAILURE_GUIDANCE in result.message
    assert one_shot.exit_code == 0
    assert STARTUP_LEGACY_RESET_GUIDANCE in one_shot.message
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PHASE_3_SCHEMA_USER_VERSION
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "run_events" in tables
    assert "todo_plans" in tables

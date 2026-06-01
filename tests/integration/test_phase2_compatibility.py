from __future__ import annotations

import sqlite3

from debug_agent.persistence.sqlite import UNSUPPORTED_PHASE_2_DATABASE_MESSAGE
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


def test_startup_status_and_trace_fail_closed_without_rewriting_legacy_database(
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
    before_bytes = db_path.read_bytes()

    orchestrator = RuntimeOrchestrator(workspace_root=workspace)

    status = orchestrator.status("sess_legacy")
    trace = orchestrator.trace("sess_legacy")
    one_shot = orchestrator.run_one_shot("hello", _config())

    for result in (status, trace, one_shot):
        assert result.exit_code == 4
        assert UNSUPPORTED_PHASE_2_DATABASE_MESSAGE in result.message
    assert one_shot.error["error_class"] == "config_error"
    assert db_path.read_bytes() == before_bytes
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
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
            )
        }
    assert "run_events" not in tables
    assert "todo_plans" not in tables

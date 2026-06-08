from __future__ import annotations

import sqlite3

import pytest

from debug_agent.persistence.sqlite import (
    PHASE_3_SCHEMA_USER_VERSION,
    READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
    RuntimeBootstrapError,
    RuntimeDatabase,
)


def test_fresh_bootstrap_creates_phase_3_schema_and_todo_plan_table(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            PHASE_3_SCHEMA_USER_VERSION
        )
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        todo_columns = [
            row[1] for row in conn.execute("PRAGMA table_info(todo_plans)")
        ]

    assert {
        "sessions",
        "runs",
        "run_events",
        "checkpoints",
        "artifacts",
        "approval_grants",
        "skill_snapshots",
        "skill_resource_snapshots",
        "context_snapshots",
        "todo_plans",
    }.issubset(table_names)
    assert todo_columns == [
        "run_id",
        "session_id",
        "plan_version",
        "items_json",
        "created_at",
        "updated_at",
        "version",
    ]


@pytest.mark.parametrize("user_version", [0, 1, 2])
def test_startup_legacy_database_resets_without_rewrite(
    tmp_path, user_version: int
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute(
            "INSERT INTO sessions (session_id, status) VALUES ('sess_legacy', 'running')"
        )
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()
    finally:
        conn.close()
    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PHASE_3_SCHEMA_USER_VERSION
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_existing_future_database_fails_closed_without_rewrite(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute(
            "INSERT INTO sessions (session_id, status) VALUES ('sess_future', 'running')"
        )
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    before_bytes = db_path.read_bytes()

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap(workspace)

    assert exc.value.error_class == "config_error"
    assert exc.value.reason == "unknown_schema_version"
    assert READ_ONLY_SCHEMA_FAILURE_GUIDANCE in str(exc.value)
    assert db_path.read_bytes() == before_bytes
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
        assert (
            conn.execute(
                "SELECT status FROM sessions WHERE session_id = 'sess_future'"
            ).fetchone()[0]
            == "running"
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "runs" not in tables
    assert "todo_plans" not in tables

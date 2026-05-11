import sqlite3

from debug_agent.persistence.sqlite import RuntimeDatabase


def test_runtime_database_bootstrap_creates_phase_0_tables(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    db_path = workspace / ".sessions" / "runtime.db"
    assert db_path.is_file()

    with sqlite3.connect(db_path) as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "sessions",
        "runs",
        "run_events",
        "checkpoints",
        "artifacts",
    }.issubset(table_names)


def test_runtime_database_schema_matches_contract_columns(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        columns = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in ("sessions", "runs", "run_events", "checkpoints", "artifacts")
        }

    assert columns["sessions"] == [
        "session_id",
        "workspace_root",
        "status",
        "approval_mode",
        "active_run_id",
        "artifact_root",
        "config_snapshot_json",
        "latest_checkpoint_id",
        "created_at",
        "updated_at",
        "error_summary",
        "version",
    ]
    assert columns["runs"] == [
        "run_id",
        "session_id",
        "parent_run_id",
        "run_type",
        "status",
        "active_skills_json",
        "latest_checkpoint_id",
        "context_snapshot_id",
        "created_at",
        "updated_at",
        "error_summary",
        "version",
    ]
    assert columns["run_events"] == [
        "event_id",
        "timestamp",
        "session_id",
        "run_id",
        "step_id",
        "kind",
        "payload_json",
        "version",
    ]
    assert columns["checkpoints"] == [
        "checkpoint_id",
        "session_id",
        "run_id",
        "kind",
        "state_json",
        "summary",
        "created_at",
        "version",
    ]
    assert columns["artifacts"] == [
        "artifact_id",
        "session_id",
        "run_id",
        "relative_path",
        "artifact_type",
        "metadata_json",
        "created_at",
        "version",
    ]


def test_runtime_database_bootstrap_is_idempotent_and_enables_foreign_keys(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = RuntimeDatabase.bootstrap(workspace)
    first.close()
    second = RuntimeDatabase.bootstrap(workspace)

    assert second.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    second.close()


def test_sessions_table_rejects_two_running_sessions_for_same_workspace(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    row = {
        "workspace_root": str(workspace),
        "status": "running",
        "approval_mode": "yolo",
        "active_run_id": None,
        "artifact_root": str(workspace / ".sessions" / "sess" / "artifacts"),
        "config_snapshot_json": "{}",
        "latest_checkpoint_id": None,
        "created_at": "2026-05-11T00:00:00Z",
        "updated_at": "2026-05-11T00:00:00Z",
        "error_summary": None,
        "version": 1,
    }
    db.connection.execute(
        """
        INSERT INTO sessions (
            session_id, workspace_root, status, approval_mode, active_run_id,
            artifact_root, config_snapshot_json, latest_checkpoint_id, created_at,
            updated_at, error_summary, version
        )
        VALUES (
            :session_id, :workspace_root, :status, :approval_mode, :active_run_id,
            :artifact_root, :config_snapshot_json, :latest_checkpoint_id,
            :created_at, :updated_at, :error_summary, :version
        )
        """,
        {"session_id": "sess_1", **row},
    )

    try:
        db.connection.execute(
            """
            INSERT INTO sessions (
                session_id, workspace_root, status, approval_mode, active_run_id,
                artifact_root, config_snapshot_json, latest_checkpoint_id,
                created_at, updated_at, error_summary, version
            )
            VALUES (
                :session_id, :workspace_root, :status, :approval_mode,
                :active_run_id, :artifact_root, :config_snapshot_json,
                :latest_checkpoint_id, :created_at, :updated_at,
                :error_summary, :version
            )
            """,
            {"session_id": "sess_2", **row},
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("SQLite must reject concurrent running sessions")
    finally:
        db.close()

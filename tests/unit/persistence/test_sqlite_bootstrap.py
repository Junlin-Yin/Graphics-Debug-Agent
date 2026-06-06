import sqlite3

import pytest

from debug_agent.persistence.sqlite import (
    PHASE_3_SCHEMA_USER_VERSION,
    RuntimeBootstrapError,
    RuntimeDatabase,
    READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
)


def test_runtime_database_bootstrap_creates_phase_3_tables_and_user_version(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    db_path = workspace / ".sessions" / "runtime.db"
    assert db_path.is_file()

    with sqlite3.connect(db_path) as conn:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert user_version == PHASE_3_SCHEMA_USER_VERSION
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


def test_runtime_database_schema_matches_contract_columns(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        columns = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in (
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
            )
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
    assert columns["approval_grants"] == [
        "grant_id",
        "session_id",
        "run_id",
        "tool_name",
        "risk_level",
        "scope_signature",
        "decision",
        "grant_scope",
        "approval_request",
        "created_at",
        "version",
    ]
    assert columns["skill_snapshots"] == [
        "skill_snapshot_id",
        "session_id",
        "run_id",
        "skill_name",
        "execution_mode",
        "source_scope",
        "source_path",
        "manifest_json",
        "skill_md_content",
        "skill_md_content_hash",
        "overall_content_hash",
        "payload_artifact_id",
        "created_at",
        "version",
    ]
    assert columns["skill_resource_snapshots"] == [
        "resource_snapshot_id",
        "skill_snapshot_id",
        "session_id",
        "run_id",
        "skill_name",
        "resource_path",
        "resource_kind",
        "media_kind",
        "size_bytes",
        "content_hash",
        "inline_text_payload",
        "payload_artifact_id",
        "created_at",
        "version",
    ]
    assert columns["context_snapshots"] == [
        "context_snapshot_id",
        "session_id",
        "run_id",
        "trigger",
        "source_checkpoint_id",
        "active_skill_records_json",
        "summary",
        "retained_messages_json",
        "omitted_tool_result_count",
        "evicted_message_count",
        "evicted_model_call_group_count",
        "artifact_refs_json",
        "token_estimate_json",
        "payload_artifact_id",
        "created_at",
        "version",
    ]
    assert columns["todo_plans"] == [
        "run_id",
        "session_id",
        "plan_version",
        "items_json",
        "created_at",
        "updated_at",
        "version",
    ]


def test_context_snapshot_trigger_constraint_rejects_unknown_values(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)

    db.connection.execute(
        """
        INSERT INTO context_snapshots (
            context_snapshot_id, session_id, run_id, trigger,
            active_skill_records_json, retained_messages_json,
            omitted_tool_result_count, artifact_refs_json, token_estimate_json,
            created_at, version
        )
        VALUES (
            'ctx_1', 'sess_1', 'run_1', 'omission | compression',
            '[]', '[]', 0, '[]', '{}', '2026-05-25T00:00:00Z', 1
        )
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO context_snapshots (
                context_snapshot_id, session_id, run_id, trigger,
                active_skill_records_json, retained_messages_json,
                omitted_tool_result_count, artifact_refs_json, token_estimate_json,
                created_at, version
            )
            VALUES (
                'ctx_2', 'sess_1', 'run_1', 'unknown',
                '[]', '[]', 0, '[]', '{}', '2026-05-25T00:00:00Z', 1
            )
            """
        )
    db.close()


def test_skill_snapshot_uniqueness_and_resource_foreign_key(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)

    db.connection.execute(
        """
        INSERT INTO skill_snapshots (
            skill_snapshot_id, session_id, run_id, skill_name, execution_mode,
            source_scope, source_path, manifest_json, skill_md_content,
            skill_md_content_hash, overall_content_hash, created_at, version
        )
        VALUES (
            'sk_1', 'sess_1', 'run_1', 'debugging', 'prompt',
            'project', '/repo/.debug-agent/skills/debugging', '{}', '# Body',
            'sha256:1', 'sha256:2', '2026-05-25T00:00:00Z', 1
        )
        """
    )
    db.connection.execute(
        """
        INSERT INTO skill_resource_snapshots (
            resource_snapshot_id, skill_snapshot_id, session_id, run_id,
            skill_name, resource_path, resource_kind, media_kind, size_bytes, content_hash,
            inline_text_payload, created_at, version
        )
        VALUES (
            'ref_1', 'sk_1', 'sess_1', 'run_1', 'debugging',
            'references/a.md', 'reference', 'text', 4, 'sha256:3', 'body',
            '2026-05-25T00:00:00Z', 1
        )
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO skill_snapshots (
                skill_snapshot_id, session_id, run_id, skill_name, execution_mode,
                source_scope, source_path, manifest_json, skill_md_content,
                skill_md_content_hash, overall_content_hash, created_at, version
            )
            VALUES (
                'sk_2', 'sess_1', 'run_1', 'debugging', 'prompt',
                'project', '/repo/other', '{}', '# Other',
                'sha256:4', 'sha256:5', '2026-05-25T00:00:00Z', 1
            )
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO skill_resource_snapshots (
                resource_snapshot_id, skill_snapshot_id, session_id, run_id,
                skill_name, resource_path, resource_kind, media_kind, size_bytes, content_hash,
                created_at, version
            )
            VALUES (
                'ref_2', 'missing', 'sess_1', 'run_1', 'debugging',
                'references/b.md', 'reference', 'text', 4, 'sha256:6',
                '2026-05-25T00:00:00Z', 1
            )
            """
        )
    db.close()


def test_startup_legacy_user_version_resets_only_runtime_db(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    orphan = db_dir / "sess_legacy" / "artifacts" / "old.txt"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("legacy artifact", encoding="utf-8")
    with sqlite3.connect(db_dir / "runtime.db") as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy')")
        conn.execute("PRAGMA user_version = 0")

    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    assert orphan.read_text(encoding="utf-8") == "legacy artifact"
    with sqlite3.connect(db_dir / "runtime.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_startup_unknown_future_user_version_fails_closed_without_deleting(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('future')")
        conn.execute("PRAGMA user_version = 99")

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap(workspace)

    assert exc.value.error_class == "config_error"
    assert exc.value.reason == "unknown_schema_version"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "future"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99


def test_read_only_bootstrap_missing_database_does_not_create_runtime_db(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = RuntimeDatabase.bootstrap_read_only(workspace)

    assert db is None
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_read_only_bootstrap_legacy_user_version_fails_closed_without_deleting(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy')")
        conn.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap_read_only(workspace)

    assert exc.value.error_class == "config_error"
    assert exc.value.reason == "legacy_schema_version"
    assert READ_ONLY_SCHEMA_FAILURE_GUIDANCE in str(exc.value)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "legacy"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


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

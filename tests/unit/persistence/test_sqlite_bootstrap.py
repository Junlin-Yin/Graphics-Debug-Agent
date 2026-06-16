import sqlite3
import json

import pytest

from debug_agent.persistence.settings import (
    PHASE_4_SCHEMA_USER_VERSION,
    PHASE_3_5_SCHEMA_USER_VERSION,
    PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
    READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
)
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeBootstrapError, RuntimeDatabase


def test_runtime_database_bootstrap_creates_phase_4_tables_and_user_version(
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

    assert user_version == PHASE_4_SCHEMA_USER_VERSION
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
        "terminal_reason",
        "terminal_error_json",
        "non_resumable_startup_failure",
        "owner_pid",
        "owner_host_id",
        "owner_token",
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
        "terminal_reason",
        "terminal_error_json",
        "non_resumable_startup_failure",
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


def test_startup_legacy_user_version_fails_closed_without_reset(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    orphan = db_dir / "sess_legacy" / "artifacts" / "old.txt"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("legacy artifact", encoding="utf-8")
    conn = sqlite3.connect(db_dir / "runtime.db")
    try:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy')")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap(workspace)

    assert exc.value.error_class == "config_error"
    assert exc.value.reason == "schema_version_missing"
    assert orphan.read_text(encoding="utf-8") == "legacy artifact"
    with sqlite3.connect(db_dir / "runtime.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "legacy"


def test_phase_4_internal_bootstrap_creates_user_version_5(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    db.close()

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            PHASE_4_SCHEMA_USER_VERSION
        )


def test_phase_4_startup_user_version_4_upgrades_and_backfills_thinking(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    seeded = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    seeded.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, workspace_root, status, approval_mode, active_run_id,
                artifact_root, config_snapshot_json, latest_checkpoint_id,
                created_at, updated_at, error_summary, terminal_reason,
                terminal_error_json, non_resumable_startup_failure, version
            )
            VALUES (
                'sess_v4', ?, 'completed', 'normal', NULL, ?,
                ?, NULL, '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z',
                NULL, NULL, NULL, 0, 1
            )
            """,
            (
                str(workspace.resolve()),
                str(workspace / ".sessions" / "sess_v4" / "artifacts"),
                json.dumps({"provider": "fake", "model": "fake-model"}, sort_keys=True),
            ),
        )
        conn.execute(f"PRAGMA user_version = {PHASE_3_5_SCHEMA_USER_VERSION}")
        conn.commit()

    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    db.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            PHASE_4_SCHEMA_USER_VERSION
        )
        snapshot = json.loads(
            conn.execute(
                "SELECT config_snapshot_json FROM sessions WHERE session_id = 'sess_v4'"
            ).fetchone()[0]
        )
    assert snapshot["thinking"] == {"enabled": False, "effort": "high"}


def test_phase_4_startup_user_version_4_preserves_runtime_truth_rows(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_path = workspace / ".sessions" / "runtime.db"
    seeded = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    seeded.close()
    state_json = json.dumps(
        {
            "manifest_schema_version": 2,
            "checkpoint_kind": "terminal_recovery",
            "payload_sha256": "sha256:unchanged",
        },
        sort_keys=True,
    )
    config_json = json.dumps(
        {
            "provider": "fake",
            "model": "fake-model",
            "execution": {
                "default_tool_timeout_seconds": 10,
                "default_shell_timeout_seconds": 11,
                "max_shell_timeout_seconds": 22,
                "cancellation_timeout_seconds": 3,
            },
            "multimodal": {
                "view_image_enabled": False,
                "view_image_disabled_reason": "missing_multimodal_config",
                "timeout_seconds": 60,
                "max_tokens": 4096,
                "max_query_chars": 8192,
                "max_analysis_chars": 8192,
            },
        },
        sort_keys=True,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, workspace_root, status, approval_mode, active_run_id,
                artifact_root, config_snapshot_json, latest_checkpoint_id,
                created_at, updated_at, error_summary, terminal_reason,
                terminal_error_json, non_resumable_startup_failure,
                owner_pid, owner_host_id, owner_token, version
            )
            VALUES (
                'sess_v4', ?, 'running', 'semi-auto', 'run_v4', ?,
                ?, 'chk_v4', '2026-06-06T00:00:00Z', '2026-06-06T00:00:01Z',
                NULL, NULL, NULL, 0, 12345, 'host-a', 'token-a', 1
            )
            """,
            (
                str(workspace.resolve()),
                str(workspace / ".sessions" / "sess_v4" / "artifacts"),
                config_json,
            ),
        )
        conn.execute(
            """
            INSERT INTO runs (
                run_id, session_id, parent_run_id, run_type, status,
                active_skills_json, latest_checkpoint_id, context_snapshot_id,
                created_at, updated_at, error_summary, terminal_reason,
                terminal_error_json, non_resumable_startup_failure, version
            )
            VALUES (
                'run_v4', 'sess_v4', NULL, 'prompt', 'running', '[]',
                'chk_v4', NULL, '2026-06-06T00:00:00Z',
                '2026-06-06T00:00:01Z', NULL, NULL, NULL, 0, 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkpoints (
                checkpoint_id, session_id, run_id, kind, state_json, summary,
                created_at, version
            )
            VALUES (
                'chk_v4', 'sess_v4', 'run_v4', 'terminal_recovery', ?,
                'summary', '2026-06-06T00:00:02Z', 1
            )
            """,
            (state_json,),
        )
        conn.execute(
            """
            INSERT INTO run_events (
                event_id, timestamp, session_id, run_id, step_id, kind,
                payload_json, version
            )
            VALUES (
                'evt_v4', '2026-06-06T00:00:03Z', 'sess_v4', 'run_v4',
                NULL, 'model_completed', '{"ok": true}', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, session_id, run_id, relative_path, artifact_type,
                metadata_json, created_at, version
            )
            VALUES (
                'art_v4', 'sess_v4', 'run_v4', 'artifacts/out.txt', 'text',
                '{"payload_sha256": "sha256:artifact"}',
                '2026-06-06T00:00:04Z', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO approval_grants (
                grant_id, session_id, run_id, tool_name, risk_level,
                scope_signature, decision, grant_scope, approval_request,
                created_at, version
            )
            VALUES (
                'grant_v4', 'sess_v4', 'run_v4', 'shell_exec', 'write',
                'scope', 'approved_for_session', 'session', '{}',
                '2026-06-06T00:00:05Z', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO todo_plans (
                run_id, session_id, plan_version, items_json, created_at,
                updated_at, version
            )
            VALUES (
                'run_v4', 'sess_v4', 1,
                '[{"index":1,"content":"keep","status":"pending","metadata":{}}]',
                '2026-06-06T00:00:06Z', '2026-06-06T00:00:07Z', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_messages (
                session_id, run_id, turn_id, message_index, message_group_id,
                model_call_id, group_position, group_status, group_row_count,
                role, kind, content_json, artifact_id, content_sha256,
                metadata_json, tool_call_id, source_event_id, accepted_at, version
            )
            VALUES (
                'sess_v4', 'run_v4', 'turn_1', 1, 'group_1',
                NULL, 0, 'closed', 1, 'user', 'user_input',
                '{"content": "hello"}', NULL, 'sha256:content', '{}',
                NULL, NULL, '2026-06-06T00:00:08Z', 1
            )
            """
        )
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "runs",
                "run_events",
                "checkpoints",
                "artifacts",
                "approval_grants",
                "todo_plans",
                "conversation_messages",
            )
        }
        before_session = conn.execute(
            """
            SELECT session_id, workspace_root, status, approval_mode,
                   active_run_id, artifact_root, latest_checkpoint_id,
                   created_at, updated_at, error_summary, terminal_reason,
                   terminal_error_json, non_resumable_startup_failure,
                   owner_pid, owner_host_id, owner_token, version
            FROM sessions
            """
        ).fetchall()
        conn.execute(f"PRAGMA user_version = {PHASE_3_5_SCHEMA_USER_VERSION}")
        conn.commit()

    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    db.close()

    with sqlite3.connect(db_path) as conn:
        after = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in before
        }
        after_session = conn.execute(
            """
            SELECT session_id, workspace_root, status, approval_mode,
                   active_run_id, artifact_root, latest_checkpoint_id,
                   created_at, updated_at, error_summary, terminal_reason,
                   terminal_error_json, non_resumable_startup_failure,
                   owner_pid, owner_host_id, owner_token, version
            FROM sessions
            """
        ).fetchall()
        upgraded_snapshot = json.loads(
            conn.execute("SELECT config_snapshot_json FROM sessions").fetchone()[0]
        )

    assert after == before
    assert after_session == before_session
    assert upgraded_snapshot["thinking"] == {"enabled": False, "effort": "high"}
    assert not (workspace / ".sessions" / "sess_v4" / "logs" / "trace.md").exists()
    assert not list((workspace / ".sessions").glob("**/run_metrics_*.json"))


def test_phase_4_startup_legacy_schema_fails_without_reset(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy')")
        conn.execute("PRAGMA user_version = 3")

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)

    assert exc.value.error_class == "config_error"
    assert exc.value.reason == "legacy_schema_version"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "legacy"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("legacy_version", [0, 1, 2, 3, 4])
def test_phase_3_5_read_only_legacy_schema_fails_without_deleting(
    tmp_path, legacy_version
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy')")
        conn.execute(f"PRAGMA user_version = {legacy_version}")

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap_phase_3_5_read_only_internal(workspace)

    assert exc.value.error_class == "config_error"
    assert exc.value.reason in {"schema_version_missing", "legacy_schema_version"}
    assert PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE in str(exc.value)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "legacy"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == legacy_version


def test_phase_3_5_startup_corrupt_database_fails_without_reset(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    db_path.write_bytes(b"not sqlite")

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)

    assert exc.value.error_class == "persistence_error"
    assert exc.value.reason == "persistence_read_failed"
    assert db_path.read_bytes() == b"not sqlite"


def test_phase_4_read_only_schema_4_fails_without_upgrading(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('phase35')")
        conn.execute(f"PRAGMA user_version = {PHASE_3_5_SCHEMA_USER_VERSION}")

    with pytest.raises(RuntimeBootstrapError) as exc:
        RuntimeDatabase.bootstrap_phase_3_5_read_only_internal(workspace)

    assert exc.value.error_class == "config_error"
    assert exc.value.reason == "legacy_schema_version"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "phase35"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            PHASE_3_5_SCHEMA_USER_VERSION
        )


def test_phase_3_5_session_path_collision_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orphan_session = workspace / ".sessions" / "sess_orphan"
    orphan_session.mkdir(parents=True)
    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)

    with pytest.raises(RuntimeBootstrapError) as exc:
        SessionStore(db.connection).create(
            workspace_root=workspace,
            approval_mode="normal",
            config_snapshot={},
            session_id="sess_orphan",
            require_fresh_phase_3_5_paths=True,
        )

    assert exc.value.error_class == "persistence_error"
    assert exc.value.reason == "persistence_write_failed"
    assert orphan_session.is_dir()
    db.close()


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

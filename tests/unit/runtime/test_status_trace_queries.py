from __future__ import annotations

import sqlite3

from debug_agent.runtime.orchestrator import RuntimeOrchestrator
from debug_agent.cli.exit_codes import ERROR_LOOKUP_NOT_FOUND, ERROR_STARTUP_PERSISTENCE
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.settings import (
    PHASE_3_5_SCHEMA_USER_VERSION,
    PHASE_4_SCHEMA_USER_VERSION,
    PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE,
)
from debug_agent.runtime.contracts import RunEvent, utc_now_iso
from debug_agent.runtime.settings import SYSTEM_PROMPT


def _config(response: str = "fake answer") -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": response,
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": SYSTEM_PROMPT,
        "development": {
            "allow_incomplete_phase3_prompt_execution": True,
        },
    }


def test_status_query_returns_documented_fields_after_one_shot(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RuntimeOrchestrator(workspace_root=workspace)
    one_shot = orchestrator.run_one_shot("hello", _config())

    status = RuntimeOrchestrator(workspace_root=workspace).status(one_shot.session_id)

    assert status.exit_code == 0
    assert status.fields["session_id"] == one_shot.session_id
    assert status.fields["workspace_root"] == str(workspace.resolve())
    assert status.fields["session_status"] == "completed"
    assert status.fields["approval_mode"] == "normal"
    assert status.fields["active_run_id"] is None
    assert status.fields["latest_run_id"] == one_shot.run_id
    assert status.fields["latest_run_status"] == "completed"
    assert status.fields["latest_checkpoint_id"]
    assert status.fields["created_at"]
    assert status.fields["updated_at"]
    assert status.fields["error_summary"] is None


def test_trace_query_refreshes_trace_and_returns_summary_after_one_shot(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )

    trace = RuntimeOrchestrator(workspace_root=workspace).trace(one_shot.session_id)

    assert trace.exit_code == 0
    assert trace.summary["session_id"] == one_shot.session_id
    assert trace.summary["workspace_root"] == str(workspace.resolve())
    assert trace.summary["run_count"] == 1
    assert trace.summary["artifact_count"] == 0
    assert trace.summary["terminal_status"] == "completed"
    assert trace.trace_path.is_file()
    assert trace.trace_path.parent.name == "logs"
    trace_text = trace.trace_path.read_text(encoding="utf-8")
    assert "## 👤 User" in trace_text
    assert "## 🤖 Assistant" in trace_text
    assert "## Timeline" not in trace_text


def test_terminal_checkpoint_eligibility_requires_current_terminal_lifecycle(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )
    trace_path = (
        workspace / ".sessions" / one_shot.session_id / "logs" / "trace.md"
    )
    trace_path.write_text("trace before resume", encoding="utf-8")

    completed_status = RuntimeOrchestrator(workspace_root=workspace).status(
        one_shot.session_id
    )
    assert completed_status.fields["terminal_checkpoint"]["eligible"] is True

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)
    assert resume.exit_code == 0
    assert trace_path.read_text(encoding="utf-8") == "trace before resume"

    running_status = RuntimeOrchestrator(workspace_root=workspace).status(
        one_shot.session_id
    )
    running_trace = RuntimeOrchestrator(workspace_root=workspace).trace(
        one_shot.session_id
    )

    assert running_status.fields["session_status"] == "running"
    assert running_status.fields["latest_run_status"] == "running"
    assert running_status.fields["latest_checkpoint_id"] == completed_status.fields[
        "latest_checkpoint_id"
    ]
    assert running_status.fields["terminal_checkpoint"]["checkpoint_valid"] is True
    assert running_status.fields["terminal_checkpoint"]["eligible"] is False
    trace_text = running_trace.trace_path.read_text(encoding="utf-8")
    assert "- **Status**: `running`" in trace_text
    assert "Terminal Reason" not in trace_text
    assert "checkpoint_valid=true" not in trace_text
    assert "eligible=false" not in trace_text


def test_status_and_trace_render_phase3_observability_without_recovery_authority(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )
    db_path = workspace / ".sessions" / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        events = EventWriter(connection, db_path.parent)
        for index, (kind, payload) in enumerate(
            [
                (
                    "model_call_failed",
                    {
                        "error": {
                            "schema_version": 1,
                            "error_class": "model_error",
                            "reason": "provider_timeout",
                            "message": "Provider timed out.",
                            "scope": "provider",
                            "recoverability": "retryable",
                            "metadata": {"transient": True},
                            "artifact_ids": [],
                        }
                    },
                ),
                (
                    "model_call_failed",
                    {
                        "retry": {
                            "strategy": "repeat_call",
                            "attempt": 1,
                            "max_attempts": 2,
                            "source_error_class": "model_error",
                            "source_reason": "provider_timeout",
                            "exhausted": False,
                        }
                    },
                ),
                (
                    "model_call_failed",
                    {
                        "retry": {
                            "strategy": "repeat_call",
                            "attempt": 2,
                            "max_attempts": 2,
                            "source_error_class": "model_error",
                            "source_reason": "provider_timeout",
                            "exhausted": True,
                            "result_error_class": "model_error",
                            "result_reason": "provider_timeout",
                        }
                    },
                ),
                (
                    "model_call_failed",
                    {
                        "error": {
                            "schema_version": 1,
                            "error_class": "cancelled",
                            "reason": "model_call_cancelled",
                            "message": "Local provider cancellation observed.",
                            "scope": "provider",
                            "recoverability": "turn_recoverable",
                            "metadata": {
                                "remote_stop_confirmed": False,
                                "billing_stop_confirmed": False,
                            },
                            "artifact_ids": [],
                        }
                    },
                ),
                (
                    "run_resumed",
                    {
                        "session_id": one_shot.session_id,
                        "run_id": one_shot.run_id,
                        "outcome": "succeeded",
                    },
                ),
                (
                    "stale_fail_closed",
                    {
                        "session_id": one_shot.session_id,
                        "run_id": one_shot.run_id,
                        "terminal_reason": "terminal_stale",
                        "stale_proof_summary": {
                            "host_match": True,
                            "pid_absent": True,
                            "token_fenced": True,
                        },
                    },
                ),
            ]
        ):
            events.append(
                RunEvent(
                    event_id=f"evt_phase3_obs_{index}",
                    timestamp=utc_now_iso(),
                    session_id=one_shot.session_id,
                    run_id=one_shot.run_id,
                    step_id=None,
                    kind=kind,
                    payload=payload,
                )
            )

    status = RuntimeOrchestrator(workspace_root=workspace).status(one_shot.session_id)
    trace = RuntimeOrchestrator(workspace_root=workspace).trace(one_shot.session_id)

    assert status.exit_code == 0
    assert status.fields["terminal_checkpoint"]["checkpoint_id"] == status.fields["latest_checkpoint_id"]
    assert status.fields["terminal_checkpoint"]["terminal_reason"] == "terminal_completion"
    assert status.fields["terminal_checkpoint"]["terminal_status"] == "completed"
    assert status.fields["terminal_checkpoint"]["eligible"] is True
    assert status.fields["durable_conversation"]["high_watermark"] == 2
    assert status.fields["durable_conversation"]["message_count"] == 2
    assert status.fields["durable_conversation"]["projection_high_watermark"] == 2
    assert status.fields["normalized_errors"][0]["reason"] == "provider_timeout"
    assert status.fields["normalized_errors"][0]["scope"] == "provider"
    assert status.fields["normalized_errors"][0]["model_visible_projection"] == {
        "error_class": "model_error",
        "reason": "provider_timeout",
        "message": "Provider timed out.",
        "artifact_ids": [],
    }
    assert status.fields["retry"]["attempts"][0]["strategy"] == "repeat_call"
    assert status.fields["retry"]["exhausted"][0]["result_reason"] == "provider_timeout"
    assert status.fields["cancellation"][0]["remote_stop_confirmed"] is False
    assert status.fields["cancellation"][0]["billing_stop_confirmed"] is False
    assert status.fields["resume"][0]["outcome"] == "succeeded"
    assert status.fields["stale_fail_close"][0]["terminal_reason"] == "terminal_stale"
    assert status.fields["stale_fail_close"][0]["stale_proof_summary"] == {
        "host_match": True,
        "pid_absent": True,
        "token_fenced": True,
    }

    trace_text = trace.trace_path.read_text(encoding="utf-8")
    assert "## Phase 3 Observability" not in trace_text
    assert "model_error/provider_timeout" not in trace_text
    assert "retry_attempt strategy=repeat_call attempt=1/2" not in trace_text
    assert "resume outcome=succeeded" not in trace_text
    assert "stale_fail_closed terminal_reason=terminal_stale" not in trace_text
    assert "hello" in trace_text
    assert "fake answer" in trace_text


def test_status_query_returns_missing_session_error(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    status = RuntimeOrchestrator(workspace_root=workspace).status("sess_missing")

    assert status.exit_code == 0
    assert status.fields == {"runtime_database": "missing", "active_session": None}
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_status_trace_resume_and_startup_fail_closed_for_legacy_schema(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "runtime.db")
    try:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy_session')")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()

    orchestrator = RuntimeOrchestrator(workspace_root=workspace)

    status = orchestrator.status("legacy_session")
    trace = orchestrator.trace("legacy_session")
    resume = orchestrator.resume("legacy_session")
    one_shot = orchestrator.run_one_shot("hello", _config())

    for result in (status, trace, resume):
        assert result.exit_code == ERROR_STARTUP_PERSISTENCE
        assert PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE in result.message
    assert one_shot.exit_code == ERROR_STARTUP_PERSISTENCE
    assert PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE in one_shot.message
    with sqlite3.connect(db_dir / "runtime.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == (
            "legacy_session"
        )


def test_trace_and_resume_missing_database_return_lookup_without_creating_db(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RuntimeOrchestrator(workspace_root=workspace)

    trace = orchestrator.trace("sess_missing")
    resume = orchestrator.resume("sess_missing")

    assert trace.exit_code == ERROR_LOOKUP_NOT_FOUND
    assert trace.message == "No session found for id: sess_missing"
    assert resume.exit_code == ERROR_LOOKUP_NOT_FOUND
    assert resume.message == "No session found for id: sess_missing"
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_resume_write_handoff_missing_database_does_not_create_db(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import debug_agent.runtime.orchestrator as orchestrator_module

    class MissingAfterPreflight:
        def close(self) -> None:
            db_path = workspace / ".sessions" / "runtime.db"
            if db_path.exists():
                db_path.unlink()

    monkeypatch.setattr(
        orchestrator_module.RuntimeDatabase,
        "bootstrap_phase_3_5_read_only_internal",
        classmethod(lambda cls, _workspace: MissingAfterPreflight()),
    )

    resume = RuntimeOrchestrator(workspace_root=workspace).resume("sess_missing")

    assert resume.exit_code == ERROR_LOOKUP_NOT_FOUND
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_status_trace_resume_missing_database_do_not_create_db(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RuntimeOrchestrator(workspace_root=workspace)

    status = orchestrator.status("sess_missing")
    trace = orchestrator.trace("sess_missing")
    resume = orchestrator.resume("sess_missing")

    assert status.exit_code == 0
    assert status.fields == {"runtime_database": "missing", "active_session": None}
    assert trace.exit_code == ERROR_LOOKUP_NOT_FOUND
    assert resume.exit_code == ERROR_LOOKUP_NOT_FOUND
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_status_trace_resume_fail_closed_for_legacy_schema(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy_session')")
        conn.execute("PRAGMA user_version = 3")

    orchestrator = RuntimeOrchestrator(workspace_root=workspace)

    status = orchestrator.status("legacy_session")
    trace = orchestrator.trace("legacy_session")
    resume = orchestrator.resume("legacy_session")

    for result in (status, trace, resume):
        assert result.exit_code == ERROR_STARTUP_PERSISTENCE
        assert PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE in result.message
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == (
            "legacy_session"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_resume_write_handoff_legacy_database_does_not_reset(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('legacy_session')")
        conn.execute("PRAGMA user_version = 3")

    import debug_agent.runtime.orchestrator as orchestrator_module

    class LegacyAfterPreflight:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        orchestrator_module.RuntimeDatabase,
        "bootstrap_phase_3_5_read_only_internal",
        classmethod(lambda cls, _workspace: LegacyAfterPreflight()),
    )

    resume = RuntimeOrchestrator(workspace_root=workspace).resume("legacy_session")

    assert resume.exit_code == ERROR_STARTUP_PERSISTENCE
    assert PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE in resume.message
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == (
            "legacy_session"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_status_trace_resume_fail_closed_for_future_schema(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    db_dir = workspace / ".sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('future_session')")
        conn.execute(f"PRAGMA user_version = {PHASE_4_SCHEMA_USER_VERSION + 1}")

    orchestrator = RuntimeOrchestrator(workspace_root=workspace)

    status = orchestrator.status("future_session")
    trace = orchestrator.trace("future_session")
    resume = orchestrator.resume("future_session")

    for result in (status, trace, resume):
        assert result.exit_code == ERROR_STARTUP_PERSISTENCE
        assert PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE in result.message
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == (
            "future_session"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            PHASE_4_SCHEMA_USER_VERSION + 1
        )


def test_startup_rejects_invalid_agent_policy_before_creating_database(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    agent_dir = home / ".debug-agent"
    workspace = tmp_path / "workspace"
    agent_dir.mkdir(parents=True)
    workspace.mkdir()
    (agent_dir / "agent.toml").write_text(
        """
[[path_policies]]
scope = "allow"
paths = ["src/"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )

    assert result.exit_code == 4
    assert result.error["error_class"] == "config_error"
    assert not (workspace / ".sessions" / "runtime.db").exists()

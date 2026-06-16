import json

import pytest

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.conversation import (
    ConversationStore,
    canonical_json_bytes,
    sha256_hex,
)
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.contracts import Checkpoint


def _stores(tmp_path, *, config_snapshot=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="normal",
        config_snapshot=config_snapshot
        or {
            "provider": "fake",
            "execution": {
                "default_shell_timeout_seconds": 300,
                "max_shell_timeout_seconds": 900,
                "cancellation_timeout_seconds": 10,
            },
            "multimodal": {
                "view_image_enabled": False,
                "view_image_disabled_reason": "missing_multimodal_config",
                "timeout_seconds": 60,
                "max_tokens": 4096,
                "max_query_chars": 8192,
                "max_analysis_chars": 8192,
            },
            "policy": {"snapshot": "policy"},
        },
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    session = sessions.set_active_run(session.session_id, run.run_id)
    return db, sessions, runs, artifacts, session, run


def _checkpoint_store(db, artifacts):
    return CheckpointStore(
        db.connection,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        todo_plan_store=TodoPlanStore(db.connection),
        approval_grant_store=ApprovalGrantStore(db.connection),
        artifact_store=artifacts,
    )


def test_checkpoint_store_rejects_non_terminal_recovery_prompt_checkpoint(tmp_path):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts)

    with pytest.raises(StoreError, match="terminal_recovery"):
        checkpoints.save(
            Checkpoint(
                checkpoint_id="chk_turn",
                session_id=session.session_id,
                run_id=run.run_id,
                kind="turn",
                state={"not": "terminal"},
                summary=None,
                created_at="2026-06-06T00:00:00Z",
            )
        )

    assert checkpoints.latest_for_run(run.run_id) is None
    assert _sessions.get(session.session_id).latest_checkpoint_id is None
    assert _runs.get(run.run_id).latest_checkpoint_id is None


def test_default_terminal_recovery_checkpoint_remains_phase_3_shaped(tmp_path):
    db, sessions, runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts)

    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )

    assert checkpoint.kind == "terminal_recovery"
    assert checkpoint.state["manifest_schema_version"] == 1
    assert checkpoint.state["checkpoint_kind"] == "terminal_recovery"
    assert checkpoint.state["terminal_reason"] == "user_exit"
    assert checkpoint.state["conversation"]["fact_cut"]["highest_message_index"] == 0
    assert checkpoint.state["conversation"]["projection_snapshot"]["message_refs"] == []
    assert checkpoint.state["tool_availability"]["shell_exec"]["max_timeout_seconds"] == 900
    assert "native_tools_contract" not in checkpoint.state["tool_availability"]
    assert checkpoint.state["tool_availability"]["view_image"]["enabled"] is False
    assert checkpoint.state["tool_availability"]["view_image"]["timeout_seconds"] == 60
    assert checkpoint.state["tool_availability"]["view_image"]["max_tokens"] == 4096
    assert checkpoint.state["tool_availability"]["view_image"]["max_query_chars"] == 8192
    assert checkpoint.state["tool_availability"]["view_image"]["max_analysis_chars"] == 8192
    assert checkpoint.state["payload_sha256"].startswith("sha256:")
    checkpoints.validate_terminal_recovery(checkpoint)
    assert sessions.get(session.session_id).latest_checkpoint_id == "chk_terminal"
    assert runs.get(run.run_id).latest_checkpoint_id == "chk_terminal"


def test_phase_3_5_terminal_recovery_checkpoint_requires_v2_manifest(tmp_path):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts).for_phase_3_5_internal()
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    assert checkpoint.state["manifest_schema_version"] == 2
    assert checkpoint.state["tool_availability"]["native_tools_contract"] == {
        "phase": "3.5",
        "contract_marker": "phase-3.5-native-tools-v1",
    }
    tool_facts_without_checksum = dict(checkpoint.state["tool_availability"])
    tool_checksum = tool_facts_without_checksum.pop("checksum")
    assert tool_checksum == sha256_hex(canonical_json_bytes(tool_facts_without_checksum))
    mutated_state = dict(checkpoint.state)
    mutated_state["manifest_schema_version"] = 1

    with pytest.raises(StoreError, match="manifest schema version"):
        checkpoints.validate_terminal_recovery(
            Checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                session_id=checkpoint.session_id,
                run_id=checkpoint.run_id,
                kind=checkpoint.kind,
                state=mutated_state,
                summary=checkpoint.summary,
                created_at=checkpoint.created_at,
            )
        )


def test_phase_3_5_terminal_recovery_rejects_missing_native_tool_marker(tmp_path):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts).for_phase_3_5_internal()
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    mutated_state = dict(checkpoint.state)
    mutated_tools = dict(mutated_state["tool_availability"])
    mutated_tools.pop("native_tools_contract")
    mutated_state["tool_availability"] = mutated_tools
    comparable = dict(mutated_state)
    comparable.pop("payload_sha256", None)
    mutated_state["payload_sha256"] = sha256_hex(canonical_json_bytes(comparable))

    with pytest.raises(StoreError, match="tool availability"):
        checkpoints.validate_terminal_recovery(
            Checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                session_id=checkpoint.session_id,
                run_id=checkpoint.run_id,
                kind=checkpoint.kind,
                state=mutated_state,
                summary=checkpoint.summary,
                created_at=checkpoint.created_at,
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shell_exec", {"max_timeout_seconds": 901}),
        (
            "view_image",
            {
                "enabled": False,
                "disabled_reason": "different_reason",
                "timeout_seconds": 60,
                "max_tokens": 4096,
                "max_query_chars": 8192,
                "max_analysis_chars": 8192,
            },
        ),
    ],
)
def test_phase_3_5_terminal_recovery_rejects_dynamic_tool_fact_mismatch(
    tmp_path, field, value
):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts).for_phase_3_5_internal()
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    mutated_state = dict(checkpoint.state)
    mutated_tools = dict(mutated_state["tool_availability"])
    mutated_tools[field] = value
    comparable_tools = dict(mutated_tools)
    comparable_tools.pop("checksum", None)
    mutated_tools["checksum"] = sha256_hex(canonical_json_bytes(comparable_tools))
    mutated_state["tool_availability"] = mutated_tools
    comparable = dict(mutated_state)
    comparable.pop("payload_sha256", None)
    mutated_state["payload_sha256"] = sha256_hex(canonical_json_bytes(comparable))

    with pytest.raises(StoreError, match="tool availability"):
        checkpoints.validate_terminal_recovery(
            Checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                session_id=checkpoint.session_id,
                run_id=checkpoint.run_id,
                kind=checkpoint.kind,
                state=mutated_state,
                summary=checkpoint.summary,
                created_at=checkpoint.created_at,
            )
        )


def test_phase_3_5_terminal_recovery_rejects_tool_availability_checksum_mismatch(
    tmp_path,
):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts).for_phase_3_5_internal()
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    mutated_state = dict(checkpoint.state)
    mutated_tools = dict(mutated_state["tool_availability"])
    mutated_tools["checksum"] = "sha256:" + ("0" * 64)
    mutated_state["tool_availability"] = mutated_tools
    comparable = dict(mutated_state)
    comparable.pop("payload_sha256", None)
    mutated_state["payload_sha256"] = sha256_hex(canonical_json_bytes(comparable))

    with pytest.raises(StoreError, match="tool availability"):
        checkpoints.validate_terminal_recovery(
            Checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                session_id=checkpoint.session_id,
                run_id=checkpoint.run_id,
                kind=checkpoint.kind,
                state=mutated_state,
                summary=checkpoint.summary,
                created_at=checkpoint.created_at,
            )
        )


def test_phase_4_resume_checksum_fallback_accepts_default_disabled_thinking_without_mutation(
    tmp_path,
):
    db, sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts).for_phase_3_5_internal()
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    before_state_json = db.connection.execute(
        "SELECT state_json FROM checkpoints WHERE checkpoint_id = ?",
        (checkpoint.checkpoint_id,),
    ).fetchone()[0]
    upgraded = dict(session.config_snapshot)
    upgraded["thinking"] = {"enabled": False, "effort": "high"}
    db.connection.execute(
        "UPDATE sessions SET config_snapshot_json = ? WHERE session_id = ?",
        (
            json.dumps(upgraded, ensure_ascii=False, sort_keys=True),
            session.session_id,
        ),
    )
    db.connection.commit()

    checkpoints.validate_terminal_recovery(checkpoint)

    assert sessions.get(session.session_id).config_snapshot["thinking"] == {
        "enabled": False,
        "effort": "high",
    }
    after_state_json = db.connection.execute(
        "SELECT state_json FROM checkpoints WHERE checkpoint_id = ?",
        (checkpoint.checkpoint_id,),
    ).fetchone()[0]
    assert after_state_json == before_state_json


@pytest.mark.parametrize(
    "thinking",
    [
        {"enabled": True, "effort": "high"},
        {"enabled": False, "effort": "low"},
    ],
)
def test_phase_4_resume_checksum_fallback_rejects_non_default_thinking(
    tmp_path, thinking
):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts).for_phase_3_5_internal()
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    upgraded = dict(session.config_snapshot)
    upgraded["thinking"] = thinking
    db.connection.execute(
        "UPDATE sessions SET config_snapshot_json = ? WHERE session_id = ?",
        (
            json.dumps(upgraded, ensure_ascii=False, sort_keys=True),
            session.session_id,
        ),
    )
    db.connection.commit()

    with pytest.raises(StoreError, match="frozen snapshot"):
        checkpoints.validate_terminal_recovery(checkpoint)


def test_phase_4_resume_checksum_fallback_rejects_other_config_drift(tmp_path):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts).for_phase_3_5_internal()
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    upgraded = dict(session.config_snapshot)
    upgraded["model"] = "different-model"
    upgraded["thinking"] = {"enabled": False, "effort": "high"}
    db.connection.execute(
        "UPDATE sessions SET config_snapshot_json = ? WHERE session_id = ?",
        (
            json.dumps(upgraded, ensure_ascii=False, sort_keys=True),
            session.session_id,
        ),
    )
    db.connection.commit()

    with pytest.raises(StoreError, match="frozen snapshot"):
        checkpoints.validate_terminal_recovery(checkpoint)


def test_terminal_recovery_rejects_zero_message_failure(tmp_path):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts)

    with pytest.raises(StoreError, match="zero-message"):
        checkpoints.create_terminal_recovery(
            checkpoint_id="chk_failure",
            session_id=session.session_id,
            run_id=run.run_id,
            terminal_status="failed",
            terminal_reason="terminal_failure",
            terminal_error={
                "schema_version": 1,
                "error_class": "model_error",
                "reason": "model_call_failed",
                "message": "failed",
                "scope": "turn",
                "recoverability": "terminal_recoverable",
                "metadata": {},
                "artifact_ids": [],
            },
            created_at="2026-06-06T00:00:00Z",
        )


def test_terminal_recovery_rejects_zero_message_terminal_completion(tmp_path):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts)

    with pytest.raises(StoreError, match="zero-message"):
        checkpoints.create_terminal_recovery(
            checkpoint_id="chk_completion",
            session_id=session.session_id,
            run_id=run.run_id,
            terminal_status="completed",
            terminal_reason="terminal_completion",
            terminal_error=None,
            created_at="2026-06-06T00:00:00Z",
        )


def test_terminal_recovery_validation_rejects_checksum_mutation(tmp_path):
    db, _sessions, _runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts)
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_terminal",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-06-06T00:00:00Z",
    )
    mutated_state = dict(checkpoint.state)
    mutated_state["terminal_reason"] = "terminal_completion"

    with pytest.raises(StoreError, match="checksum"):
        checkpoints.validate_terminal_recovery(
            Checkpoint(
                checkpoint_id=checkpoint.checkpoint_id,
                session_id=checkpoint.session_id,
                run_id=checkpoint.run_id,
                kind=checkpoint.kind,
                state=mutated_state,
                summary=checkpoint.summary,
                created_at=checkpoint.created_at,
            )
        )


def test_terminal_recovery_requires_frozen_execution_timeouts(tmp_path):
    config_snapshot = {
        "provider": "fake",
        "execution": {"default_shell_timeout_seconds": 300},
        "multimodal": {
            "view_image_enabled": True,
            "timeout_seconds": 60,
            "max_tokens": 4096,
            "max_query_chars": 8192,
            "max_analysis_chars": 8192,
        },
        "policy": {},
    }
    db, _sessions, _runs, artifacts, session, run = _stores(
        tmp_path, config_snapshot=config_snapshot
    )
    checkpoints = _checkpoint_store(db, artifacts)

    with pytest.raises(StoreError, match="max_shell_timeout_seconds"):
        checkpoints.create_terminal_recovery(
            checkpoint_id="chk_terminal",
            session_id=session.session_id,
            run_id=run.run_id,
            terminal_status="completed",
            terminal_reason="user_exit",
            terminal_error=None,
            created_at="2026-06-06T00:00:00Z",
        )


def test_terminalize_with_recovery_checkpoint_rolls_back_on_transition_failure(
    tmp_path, monkeypatch
):
    db, sessions, runs, artifacts, session, run = _stores(tmp_path)
    checkpoints = _checkpoint_store(db, artifacts)

    def fail_session_transition(self, **_kwargs):
        raise StoreError(error_class="internal_error", message="injected failure")

    monkeypatch.setattr(
        CheckpointStore,
        "_mark_session_terminal_with_checkpoint",
        fail_session_transition,
    )

    with pytest.raises(StoreError, match="injected failure"):
        checkpoints.terminalize_with_recovery_checkpoint(
            checkpoint_id="chk_terminal",
            session_id=session.session_id,
            run_id=run.run_id,
            terminal_status="completed",
            terminal_reason="user_exit",
            terminal_error=None,
            error_summary=None,
            created_at="2026-06-06T00:00:00Z",
        )

    assert db.connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert sessions.get(session.session_id).status == "running"
    assert runs.get(run.run_id).status == "running"
    assert sessions.get(session.session_id).latest_checkpoint_id is None
    assert runs.get(run.run_id).latest_checkpoint_id is None


def test_startup_failure_marker_leaves_latest_checkpoint_unset(tmp_path):
    db, sessions, runs, _artifacts, session, run = _stores(tmp_path)

    run = runs.mark_startup_failure(run.run_id, "startup failed")
    session = sessions.mark_startup_failure(session.session_id, "startup failed")

    assert session.status == "failed"
    assert run.status == "failed"
    assert session.latest_checkpoint_id is None
    assert run.latest_checkpoint_id is None
    assert session.non_resumable_startup_failure is True
    assert run.non_resumable_startup_failure is True
    assert db.connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0

from debug_agent.runtime.contracts import (
    Artifact,
    AgentRunResult,
    AGENT_RUN_RESULT_STATUSES,
    Checkpoint,
    ERROR_CLASSES,
    LEGACY_ERROR_CLASSES,
    Run,
    RunEvent,
    Session,
    TOOL_RESULT_STATUSES,
    ToolResult,
)


def test_session_serializes_required_contract_fields() -> None:
    session = Session(
        session_id="sess_1",
        workspace_root="/repo",
        status="running",
        approval_mode="yolo",
        active_run_id="run_1",
        artifact_root="/repo/.sessions/sess_1/artifacts",
        config_snapshot={"provider": "anthropic", "auth": {"api_key_present": True}},
        latest_checkpoint_id=None,
        created_at="2026-05-11T00:00:00Z",
        updated_at="2026-05-11T00:00:01Z",
        error_summary=None,
    )

    assert session.to_dict() == {
        "session_id": "sess_1",
        "workspace_root": "/repo",
        "status": "running",
        "approval_mode": "yolo",
        "active_run_id": "run_1",
        "artifact_root": "/repo/.sessions/sess_1/artifacts",
        "config_snapshot": {
            "provider": "anthropic",
            "auth": {"api_key_present": True},
        },
        "latest_checkpoint_id": None,
        "created_at": "2026-05-11T00:00:00Z",
        "updated_at": "2026-05-11T00:00:01Z",
        "error_summary": None,
        "terminal_reason": None,
        "terminal_error": None,
        "non_resumable_startup_failure": False,
        "version": 1,
    }


def test_runtime_contracts_round_trip_json_safe_fields() -> None:
    run = Run(
        run_id="run_1",
        session_id="sess_1",
        parent_run_id=None,
        run_type="prompt",
        status="running",
        active_skills=[],
        latest_checkpoint_id=None,
        context_snapshot_id=None,
        created_at="2026-05-11T00:00:00Z",
        updated_at="2026-05-11T00:00:00Z",
        error_summary=None,
    )
    event = RunEvent(
        event_id="evt_1",
        timestamp="2026-05-11T00:00:01Z",
        session_id="sess_1",
        run_id="run_1",
        step_id=None,
        kind="run_started",
        payload={"run_type": "prompt"},
    )
    checkpoint = Checkpoint(
        checkpoint_id="chk_1",
        session_id="sess_1",
        run_id="run_1",
        kind="turn",
        state={"run_status": "running"},
        summary=None,
        created_at="2026-05-11T00:00:02Z",
    )
    artifact = Artifact(
        artifact_id="art_1",
        session_id="sess_1",
        run_id="run_1",
        relative_path="sess_1/artifacts/output.txt",
        artifact_type="text",
        metadata={"bytes": 12},
        created_at="2026-05-11T00:00:03Z",
    )
    tool_result = ToolResult(
        status="ok",
        output={"entries": []},
        error=None,
        artifacts=["art_1"],
        metadata={"duration_ms": 4},
        redacted_output=None,
    )

    assert Run.from_dict(run.to_dict()) == run
    assert RunEvent.from_dict(event.to_dict()) == event
    assert Checkpoint.from_dict(checkpoint.to_dict()) == checkpoint
    assert Artifact.from_dict(artifact.to_dict()) == artifact
    assert ToolResult.from_dict(tool_result.to_dict()) == tool_result


def test_contracts_reject_values_outside_phase_0() -> None:
    try:
        Run(
            run_id="run_1",
            session_id="sess_1",
            parent_run_id=None,
            run_type="workflow",
            status="running",
            active_skills=[],
            latest_checkpoint_id=None,
            context_snapshot_id=None,
            created_at="2026-05-11T00:00:00Z",
            updated_at="2026-05-11T00:00:00Z",
            error_summary=None,
        )
    except ValueError as exc:
        assert "run_type" in str(exc)
    else:
        raise AssertionError("workflow run type must not be accepted in Phase 0")


def test_agent_run_result_rejects_status_outside_adapter_contract() -> None:
    try:
        AgentRunResult(
            status="interrupted",
            assistant_output=None,
            tool_results=[],
            usage={},
            error=None,
            metadata={},
        )
    except ValueError as exc:
        assert "agent run result status" in str(exc)
    else:
        raise AssertionError("invalid adapter result status must not be accepted")


def test_agent_run_result_accepts_phase_0_adapter_statuses() -> None:
    for status in ("completed", "failed", "timeout", "cancelled"):
        result = AgentRunResult(
            status=status,
            assistant_output="answer" if status == "completed" else None,
            tool_results=[],
            usage={},
            error=None,
            metadata={},
        )

        assert result.status == status


def test_error_classes_are_phase_3_normalized_runtime_truth() -> None:
    assert ERROR_CLASSES == frozenset(
        {
            "user_error",
            "config_error",
            "policy_error",
            "model_error",
            "tool_error",
            "skill_error",
            "persistence_error",
            "runtime_error",
            "ui_error",
            "cancelled",
        }
    )


def test_legacy_error_classes_are_separate_from_runtime_truth() -> None:
    assert LEGACY_ERROR_CLASSES == frozenset(
        {
            "timeout",
            "internal_error",
            "policy_denied",
            "compression_failed",
            "context_limit_exceeded",
        }
    )
    assert ERROR_CLASSES.isdisjoint(LEGACY_ERROR_CLASSES)


def test_timeout_remains_a_status_not_an_error_class() -> None:
    assert "timeout" in TOOL_RESULT_STATUSES
    assert "timeout" in AGENT_RUN_RESULT_STATUSES
    assert "timeout" not in ERROR_CLASSES

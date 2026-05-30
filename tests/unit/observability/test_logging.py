from __future__ import annotations

import json

from debug_agent.observability.logging import (
    EngineLogWriter,
    _level_for_event,
    approval_log_message,
    artifact_log_message,
    context_log_message,
    policy_log_message,
    skill_log_message,
    write_event_log,
)
from debug_agent.runtime.contracts import RunEvent


def test_engine_log_writer_emits_json_lines_with_required_fields(tmp_path) -> None:
    log_path = tmp_path / "sess_1" / "logs" / "engine.log"
    writer = EngineLogWriter(log_path)

    writer.write(
        timestamp="2026-05-12T00:00:00Z",
        session_id="sess_1",
        run_id="run_1",
        step_id=None,
        level="INFO",
        event="run_started",
        message="run_started",
        metadata={"kind": "run_started"},
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "timestamp": "2026-05-12T00:00:00Z",
        "session_id": "sess_1",
        "run_id": "run_1",
        "step_id": None,
        "level": "INFO",
        "event": "run_started",
        "message": "run_started",
        "metadata": {"kind": "run_started"},
    }


def test_failed_events_are_error_level_without_special_cases() -> None:
    assert _level_for_event("model_call_failed") == "ERROR"
    assert _level_for_event("tool_call_failed") == "ERROR"
    assert _level_for_event("tool_call_denied") == "WARN"
    assert _level_for_event("run_started") == "INFO"


def test_skill_observability_events_are_info_level() -> None:
    assert _level_for_event("skill_snapshot_created") == "INFO"
    assert _level_for_event("skill_activated") == "INFO"
    assert _level_for_event("skill_resource_loaded") == "INFO"


def test_skill_events_write_engine_log_fact_messages(tmp_path) -> None:
    event = RunEvent(
        event_id="evt_skill_resource",
        timestamp="2026-05-12T00:00:00Z",
        session_id="sess_1",
        run_id="run_1",
        step_id=None,
        kind="skill_resource_loaded",
        payload={
            "skill_name": "alpha",
            "skill_content_hash": "sha256:alpha",
            "resource_path": "references/guide.md",
            "resource_kind": "reference",
            "resource_content_hash": "sha256:guide",
        },
    )

    write_event_log(tmp_path, event)

    log_path = tmp_path / "sess_1" / "logs" / "engine.log"
    payload = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["message"] == (
        "skill_resource_loaded skill=alpha resource=references/guide.md kind=reference"
    )
    assert payload["metadata"]["payload"]["resource_content_hash"] == "sha256:guide"


def test_phase1_engine_log_helpers_render_required_fact_messages() -> None:
    assert (
        skill_log_message(
            "skill_activated",
            {"skill_name": "alpha", "content_hash": "sha256:alpha"},
        )
        == "skill_activated skill=alpha hash=sha256:alpha"
    )
    assert approval_log_message(
        "approval_mode_changed",
        {"old_mode": "normal", "new_mode": "semi-auto"},
    ) == "approval_mode_changed normal->semi-auto"
    assert approval_log_message(
        "approval_decision_recorded",
        {
            "tool_name": "read_file",
            "decision": "approved_for_session",
            "grant_scope": "session",
        },
    ) == "approval_decision_recorded tool=read_file decision=approved_for_session scope=session"
    assert policy_log_message(
        "tool_call_denied",
        {
            "tool_name": "shell_exec",
            "result": {
                "error": {
                    "error_class": "policy_denied",
                    "message": "Shell command denied by policy.",
                }
            },
        },
    ) == (
        "tool_call_denied tool=shell_exec error_class=policy_denied "
        "message=Shell command denied by policy."
    )
    assert context_log_message(
        "context_optimized",
        {
            "trigger": "compression",
            "context_snapshot_id": "ctx_1",
            "reduced_from_tokens": 100,
            "reduced_to_tokens": 40,
        },
    ) == "context_optimized trigger=compression snapshot=ctx_1 tokens=100->40"
    assert artifact_log_message(
        "artifact_registered",
        {
            "artifact_id": "art_1",
            "artifact_type": "text",
            "relative_path": "sess_1/artifacts/out.txt",
        },
    ) == "artifact_registered artifact=art_1 type=text path=sess_1/artifacts/out.txt"

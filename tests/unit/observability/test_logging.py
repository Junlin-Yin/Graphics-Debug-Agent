from __future__ import annotations

import json

from debug_agent.observability.logging import (
    EngineLogWriter,
    _level_for_event,
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
    assert _level_for_event("skill_reference_loaded") == "INFO"


def test_skill_events_write_engine_log_fact_messages(tmp_path) -> None:
    event = RunEvent(
        event_id="evt_skill_ref",
        timestamp="2026-05-12T00:00:00Z",
        session_id="sess_1",
        run_id="run_1",
        step_id=None,
        kind="skill_reference_loaded",
        payload={
            "skill_name": "alpha",
            "skill_content_hash": "sha256:alpha",
            "reference_path": "references/guide.md",
            "reference_content_hash": "sha256:guide",
        },
    )

    write_event_log(tmp_path, event)

    log_path = tmp_path / "sess_1" / "logs" / "engine.log"
    payload = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["message"] == (
        "skill_reference_loaded skill=alpha reference=references/guide.md"
    )
    assert payload["metadata"]["payload"]["reference_content_hash"] == "sha256:guide"

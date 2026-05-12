from __future__ import annotations

import json

from debug_agent.observability.logging import EngineLogWriter


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

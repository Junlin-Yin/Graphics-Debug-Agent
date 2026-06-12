from __future__ import annotations

import json

from debug_agent.observability.trace_writer import TraceWriter
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.contracts import RunEvent, utc_now_iso
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


def _workspace_with_todo_plan(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "model": "fake-model"},
        session_id="sess_todo_status",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_todo_status")
    sessions.set_active_run(session.session_id, run.run_id)
    TodoPlanStore(db.connection).replace_plan(
        session.session_id,
        run.run_id,
        [
            {"content": "Review docs", "status": "completed"},
            {
                "content": "Patch runtime injection",
                "status": "in_progress",
                "activeForm": "Patching runtime injection",
            },
            {"content": "Run verification", "status": "pending"},
        ],
        events,
    )
    db.close()
    return workspace, session.session_id


def test_status_includes_current_todo_plan_counts_from_store(tmp_path) -> None:
    workspace, session_id = _workspace_with_todo_plan(tmp_path)

    status = RuntimeOrchestrator(workspace_root=workspace).status(session_id)

    assert status.exit_code == 0
    assert status.fields["todo_plan"] == {
        "plan_version": 1,
        "counts": {
            "pending": 1,
            "in_progress": 1,
            "completed": 1,
        },
    }


def test_trace_excludes_todo_events_and_current_plan_summary(tmp_path) -> None:
    workspace, session_id = _workspace_with_todo_plan(tmp_path)
    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    try:
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(session_id)
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert "todo_updated" not in content
    assert "plan_version=1" not in content
    assert "## Todo Plans" not in content
    assert "Patch runtime injection" not in content

    check_db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    try:
        row = check_db.connection.execute(
            """
            SELECT payload_json FROM run_events
            WHERE session_id = ? AND kind = 'todo_updated'
            """,
            (session_id,),
        ).fetchone()
    finally:
        check_db.close()
    assert row is not None
    assert json.loads(row[0])["plan_version"] == 1


def test_trace_excludes_view_image_event_facts_without_query_or_base64(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "model": "fake-model"},
        session_id="sess_view_image_trace",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_view_image_trace")
    sessions.set_active_run(session.session_id, run.run_id)
    events.append(
        RunEvent(
            event_id="evt_view_image_trace",
            timestamp=utc_now_iso(),
            session_id=session.session_id,
            run_id=run.run_id,
            step_id=None,
            kind="tool_call_completed",
            payload={
                "tool_name": "view_image",
                "arguments": {
                    "paths": [str(workspace / "capture.png")],
                    "effective_query_source": "assistant",
                },
                "target": str(workspace / "capture.png"),
                "status": "ok",
                "duration": 0.25,
                "artifact_ids": [],
                "effective_query_source": "assistant",
                "vision_provider": "openai",
                "vision_model": "kimi-k2.5",
                "duration_ms": 12,
                "projected_request_bytes": 1234,
                "images": [
                    {
                        "path": "capture.png",
                        "mime_type": "image/png",
                        "sha256": "abc123",
                        "byte_size": 42,
                        "width": 4,
                        "height": 3,
                    }
                ],
                "result": {
                    "status": "ok",
                    "output": {
                        "analysis": "visible image",
                        "metadata": [
                            {
                                "path": "capture.png",
                                "mime_type": "image/png",
                                "width": 4,
                                "height": 3,
                            }
                        ],
                    },
                    "error": None,
                    "artifacts": [],
                    "metadata": {
                        "tool_name": "view_image",
                        "vision_provider": "openai",
                        "vision_model": "kimi-k2.5",
                        "duration_ms": 12,
                        "effective_query_source": "assistant",
                        "projected_request_bytes": 1234,
                        "images": [
                            {
                                "path": "capture.png",
                                "mime_type": "image/png",
                                "sha256": "abc123",
                                "byte_size": 42,
                                "width": 4,
                                "height": 3,
                            }
                        ],
                    },
                    "redacted_output": "visible image\nImages: capture.png",
                },
            },
        )
    )

    try:
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(
            session.session_id
        )
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert "tool=view_image" not in content
    assert "path=capture.png" not in content
    assert "provider=openai" not in content
    assert "effective_query_source=assistant" not in content
    assert "analysis=visible image" not in content
    assert "secret query focus" not in content
    assert "query_preview" not in content
    assert "query_length" not in content
    assert "base64" not in content
    assert "data:image/png" not in content

    events_path = workspace / ".sessions" / session.session_id / "logs" / "events.jsonl"
    log_text = events_path.read_text(encoding="utf-8")
    assert "view_image" in log_text
    assert "effective_query_source" in log_text
    assert "secret query focus" not in log_text
    assert "query_preview" not in log_text
    assert "query_length" not in log_text
    assert "base64" not in log_text


def test_trace_excludes_view_image_error_events_without_leaks(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "model": "fake-model"},
        session_id="sess_view_image_denied_trace",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_view_image_denied_trace")
    sessions.set_active_run(session.session_id, run.run_id)
    events.append(
        RunEvent(
            event_id="evt_view_image_denied_trace",
            timestamp=utc_now_iso(),
            session_id=session.session_id,
            run_id=run.run_id,
            step_id=None,
            kind="tool_call_denied",
            payload={
                "tool_name": "view_image",
                "arguments": {
                    "paths": [str(workspace / "capture.png")],
                    "effective_query_source": "assistant",
                },
                "target": str(workspace / "capture.png"),
                "status": "denied",
                "duration": 0.01,
                "approval_wait_duration_ms": 0,
                "artifact_ids": [],
                "error_class": "config_error",
                "message": "view_image is disabled: missing_api_key_env",
                "source": "toolbroker",
                "recoverable": True,
            },
        )
    )

    try:
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(
            session.session_id
        )
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert "tool=view_image" not in content
    assert "status=denied" not in content
    assert "error_class=config_error" not in content
    assert "effective_query_source=assistant" not in content
    assert "secret query focus" not in content
    assert "query_preview" not in content
    assert "query_length" not in content
    assert "base64" not in content
    assert "data:image/png" not in content

    events_path = workspace / ".sessions" / session.session_id / "logs" / "events.jsonl"
    log_text = events_path.read_text(encoding="utf-8")
    assert "view_image" in log_text
    assert "status" in log_text
    assert "config_error" in log_text
    assert "secret query focus" not in log_text
    assert "query_preview" not in log_text
    assert "query_length" not in log_text
    assert "base64" not in log_text

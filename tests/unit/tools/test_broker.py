from __future__ import annotations

import time
from pathlib import Path

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.contracts import ToolResult
from debug_agent.tools.broker import ToolBroker


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    broker = ToolBroker(
        event_writer=EventWriter(db.connection),
        artifact_store=ArtifactStore(db.connection),
    )
    return workspace, db, broker, session, run


def _invoke(broker, session, run, tool_name, arguments, workspace, **context):
    merged_context = {"workspace_root": str(workspace), **context}
    return broker.invoke(
        session_id=session.session_id,
        run_id=run.run_id,
        tool_name=tool_name,
        arguments=arguments,
        context=merged_context,
    )


def test_unknown_tool_returns_denied_and_writes_audit_event(tmp_path) -> None:
    workspace, db, broker, session, run = _runtime(tmp_path)

    result = _invoke(broker, session, run, "write_file", {"path": "x"}, workspace)

    events = EventWriter(db.connection).list_for_run(run.run_id)
    assert result.status == "denied"
    assert result.error["error_class"] == "policy_denied"
    assert [event.kind for event in events] == ["tool_call_denied"]
    assert events[0].payload["tool_name"] == "write_file"
    db.close()


def test_path_outside_workspace_and_write_intent_are_denied(tmp_path) -> None:
    workspace, db, broker, session, run = _runtime(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    outside_result = _invoke(
        broker, session, run, "read_file", {"path": str(outside)}, workspace
    )
    write_result = _invoke(
        broker,
        session,
        run,
        "read_file",
        {"path": "allowed.txt", "write": True},
        workspace,
    )

    assert outside_result.status == "denied"
    assert write_result.status == "denied"
    assert [event.kind for event in EventWriter(db.connection).list_for_run(run.run_id)] == [
        "tool_call_denied",
        "tool_call_denied",
    ]
    db.close()


def test_symlink_traversal_outside_workspace_is_denied(tmp_path) -> None:
    workspace, db, broker, session, run = _runtime(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)

    result = _invoke(broker, session, run, "read_file", {"path": "link.txt"}, workspace)

    assert result.status == "denied"
    assert result.error["error_class"] == "policy_denied"
    db.close()


def test_successful_read_file_returns_tool_result_and_audit_events(tmp_path) -> None:
    workspace, db, broker, session, run = _runtime(tmp_path)
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    result = _invoke(broker, session, run, "read_file", {"path": "notes.txt"}, workspace)

    events = EventWriter(db.connection).list_for_run(run.run_id)
    assert isinstance(result, ToolResult)
    assert result.status == "ok"
    assert result.output == "hello"
    assert result.error is None
    assert result.artifacts == []
    assert [event.kind for event in events] == [
        "tool_call_started",
        "tool_call_completed",
    ]
    assert events[-1].payload["status"] == "ok"
    db.close()


def test_large_output_is_written_to_text_artifact(tmp_path) -> None:
    workspace, db, broker, session, run = _runtime(tmp_path)
    (workspace / "large.txt").write_text("x" * (16 * 1024 + 1), encoding="utf-8")

    result = _invoke(broker, session, run, "read_file", {"path": "large.txt"}, workspace)

    assert result.status == "ok"
    assert result.output is None
    assert result.redacted_output.startswith("[output stored as artifact:")
    assert len(result.artifacts) == 1
    artifact_path = ArtifactStore(db.connection).resolve_path(result.artifacts[0])
    assert artifact_path.read_text(encoding="utf-8") == "x" * (16 * 1024 + 1)
    db.close()


def test_timeout_returns_timeout_result_and_failed_audit_event(tmp_path) -> None:
    workspace, db, broker, session, run = _runtime(tmp_path)

    def slow_handler(_workspace: Path, _arguments: dict):
        time.sleep(0.2)
        return "late"

    broker._tool_handlers["read_file"] = slow_handler

    result = _invoke(
        broker,
        session,
        run,
        "read_file",
        {"path": "missing.txt"},
        workspace,
        timeout_seconds=0.01,
    )

    events = EventWriter(db.connection).list_for_run(run.run_id)
    assert result.status == "timeout"
    assert result.error["error_class"] == "timeout"
    assert [event.kind for event in events] == [
        "tool_call_started",
        "tool_call_failed",
    ]
    db.close()

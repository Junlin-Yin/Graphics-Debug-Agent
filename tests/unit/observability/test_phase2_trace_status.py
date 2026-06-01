from __future__ import annotations

from debug_agent.observability.trace_writer import TraceWriter
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


def _workspace_with_todo_plan(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
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


def test_trace_renders_todo_updated_events_and_current_plan_summary(tmp_path) -> None:
    workspace, session_id = _workspace_with_todo_plan(tmp_path)
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(session_id)
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert "todo_updated" in content
    assert "plan_version=1" in content
    assert "counts={'pending': 1, 'in_progress': 1, 'completed': 1}" in content
    assert "## Todo Plans" in content
    assert "run_todo_status: v1 1 pending, 1 in_progress, 1 completed" in content
    assert "Patch runtime injection" in content

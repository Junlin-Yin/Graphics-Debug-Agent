from __future__ import annotations

from debug_agent.observability.trace_writer import TraceWriter
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.contracts import Checkpoint, RunEvent, utc_now_iso


def _persist_session_with_events(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "model": "fake-model"},
        session_id="sess_trace",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_trace")
    session = sessions.set_active_run(session.session_id, run.run_id)
    for kind, payload in [
        ("session_started", {}),
        ("run_started", {}),
        ("user_message", {"content": "hello"}),
        ("model_call_started", {"provider": "fake", "model": "fake-model"}),
        ("assistant_message", {"content": "answer"}),
    ]:
        events.append(
            RunEvent(
                event_id=f"evt_{kind}",
                timestamp=utc_now_iso(),
                session_id=session.session_id,
                run_id=run.run_id,
                step_id=None,
                kind=kind,
                payload=payload,
            )
        )
    checkpoint = checkpoints.save(
        Checkpoint(
            checkpoint_id="chk_trace",
            session_id=session.session_id,
            run_id=run.run_id,
            kind="turn",
            state={"session_status": "running", "run_status": "running"},
            summary="answer",
            created_at=utc_now_iso(),
        )
    )
    events.append(
        RunEvent(
            event_id="evt_checkpoint",
            timestamp=utc_now_iso(),
            session_id=session.session_id,
            run_id=run.run_id,
            step_id=None,
            kind="checkpoint_written",
            payload={"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
        )
    )
    return db, session


def test_trace_writer_renders_required_sections_and_metadata(tmp_path) -> None:
    db, session = _persist_session_with_events(tmp_path)
    try:
        result = TraceWriter(db.connection).refresh_if_stale(session.session_id)
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert result.refreshed is True
    assert "<!-- event_count: 6 -->" in content
    assert "<!-- latest_event_id: evt_checkpoint -->" in content
    assert "## Session Summary" in content
    assert "## Runs" in content
    assert "## Timeline" in content
    assert "## Checkpoints" in content
    assert "## Artifacts" in content
    assert "## Errors" in content
    assert "model_call_started" in content
    assert "checkpoint_written" in content


def test_trace_writer_skips_fresh_trace_and_refreshes_stale_trace(tmp_path) -> None:
    db, session = _persist_session_with_events(tmp_path)
    try:
        writer = TraceWriter(db.connection)
        first = writer.refresh_if_stale(session.session_id)
        second = writer.refresh_if_stale(session.session_id)
        EventWriter(db.connection).append(
            RunEvent(
                event_id="evt_completed",
                timestamp=utc_now_iso(),
                session_id=session.session_id,
                run_id="run_trace",
                step_id=None,
                kind="session_completed",
                payload={},
            )
        )
        third = writer.refresh_if_stale(session.session_id)
    finally:
        db.close()

    assert first.refreshed is True
    assert second.refreshed is False
    assert third.refreshed is True

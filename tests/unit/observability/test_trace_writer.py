from __future__ import annotations

from debug_agent.observability.trace_writer import TraceWriter
from debug_agent.persistence.artifacts import ArtifactStore
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
    events = EventWriter(db.connection, db.path.parent)
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
        (
            "model_call_completed",
            {
                "usage": {},
                "metadata": {},
                "duration": 0.025,
                "content": "model answer",
                "tool_calls": [{"id": "call_1", "name": "read_file", "args": {}}],
                "artifact_ids": [],
                "redacted_output": None,
            },
        ),
        (
            "tool_call_completed",
            {
                "tool_name": "read_file",
                "status": "ok",
                "duration": 0.013,
                "artifact_ids": ["art_trace"],
                "result": {
                    "status": "ok",
                    "output": "tool answer",
                    "error": None,
                    "artifacts": ["art_trace"],
                    "metadata": {},
                    "redacted_output": None,
                },
            },
        ),
        (
            "artifact_registered",
            {
                "artifact_id": "art_trace",
                "artifact_type": "text",
                "relative_path": "sess_trace/artifacts/read_file_output.txt",
                "metadata": {"tool_name": "read_file", "bytes": 17000},
            },
        ),
        (
            "skill_snapshot_created",
            {
                "skill_name": "alpha",
                "execution_mode": "prompt",
                "source_scope": "project",
                "content_hash": "sha256:alpha",
                "reference_count": 1,
            },
        ),
        (
            "skill_activated",
            {
                "skill_name": "alpha",
                "content_hash": "sha256:alpha",
                "activation_reason": "model_requested",
                "scope": "run",
            },
        ),
        (
            "skill_reference_loaded",
            {
                "skill_name": "alpha",
                "skill_content_hash": "sha256:alpha",
                "reference_path": "references/guide.md",
                "reference_content_hash": "sha256:guide",
                "media_kind": "text",
                "size_bytes": 42,
                "artifact_id": None,
            },
        ),
        (
            "model_call_failed",
            {
                "error_class": "model_error",
                "message": "provider failed",
                "source": "model",
                "recoverable": True,
                "duration": 0.005,
            },
        ),
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
    ArtifactStore(db.connection, db.path.parent).write_text(
        session_id=session.session_id,
        run_id=run.run_id,
        artifact_id="art_trace",
        filename="read_file_output.txt",
        content="artifact text",
        metadata={"tool_name": "read_file", "bytes": 17000},
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
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(session.session_id)
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert result.refreshed is True
    assert "<!-- event_count: 13 -->" in content
    assert "<!-- latest_event_id: evt_checkpoint -->" in content
    assert "## Session Summary" in content
    assert "## Runs" in content
    assert "## Timeline" in content
    assert "## Checkpoints" in content
    assert "## Artifacts" in content
    assert "## Errors" in content
    assert "model_call_started" in content
    assert "model_call_completed" in content
    assert "'duration': 0.025" in content
    assert "response=model answer" in content
    assert "tool_calls=read_file" in content
    assert "tool_call_completed" in content
    assert "'duration': 0.013" in content
    assert "result=tool answer" in content
    assert "artifact_registered" in content
    assert "art_trace" in content
    assert "skill_snapshot_created" in content
    assert "skill=alpha" in content
    assert "mode=prompt" in content
    assert "scope=project" in content
    assert "hash=sha256:alpha" in content
    assert "references=1" in content
    assert "skill_activated" in content
    assert "reason=model_requested" in content
    assert "skill_reference_loaded" in content
    assert "reference=references/guide.md" in content
    assert "reference_hash=sha256:guide" in content
    assert "model_call_failed" in content
    assert "provider failed" in content
    assert "checkpoint_written" in content


def test_trace_writer_skips_fresh_trace_and_refreshes_stale_trace(tmp_path) -> None:
    db, session = _persist_session_with_events(tmp_path)
    try:
        writer = TraceWriter(db.connection, db.path.parent)
        first = writer.refresh_if_stale(session.session_id)
        second = writer.refresh_if_stale(session.session_id)
        EventWriter(db.connection, db.path.parent).append(
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

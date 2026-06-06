import re
import sqlite3

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.contracts import Checkpoint, RunEvent


def _stores(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    return (
        workspace,
        db,
        SessionStore(db.connection),
        RunStore(db.connection),
        EventWriter(db.connection, db.path.parent),
        CheckpointStore(db.connection),
        ArtifactStore(db.connection, db.path.parent),
    )


def _create_session_and_run(tmp_path):
    workspace, db, sessions, runs, events, checkpoints, artifacts = _stores(tmp_path)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "anthropic"},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    return workspace, db, sessions, runs, events, checkpoints, artifacts, session, run


def test_session_store_creates_and_rejects_active_workspace_conflict(tmp_path) -> None:
    workspace, db, sessions, *_ = _stores(tmp_path)

    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "anthropic"},
        session_id="sess_1",
    )

    assert session.status == "running"
    assert session.workspace_root == str(workspace.resolve())
    assert session.artifact_root == str(
        workspace.resolve() / ".sessions" / "sess_1" / "artifacts"
    )
    assert sessions.get("sess_1") == session
    assert sessions.find_active_for_workspace(workspace).session_id == "sess_1"

    try:
        sessions.create(
            workspace_root=workspace,
            approval_mode="normal",
            config_snapshot={},
            session_id="sess_2",
        )
    except StoreError as exc:
        assert exc.error_class == "user_error"
    else:
        raise AssertionError("second running session must be rejected")
    finally:
        db.close()


def test_session_store_default_session_id_uses_creation_timestamp_and_short_hash(
    tmp_path,
) -> None:
    workspace, db, sessions, *_ = _stores(tmp_path)

    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "anthropic"},
    )

    assert re.fullmatch(
        r"sess_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-[0-9a-f]{4}",
        session.session_id,
    )
    assert session.artifact_root == str(
        workspace.resolve() / ".sessions" / session.session_id / "artifacts"
    )
    db.close()


def test_session_store_releases_ownership_after_terminal_status(tmp_path) -> None:
    workspace, db, sessions, *_ = _stores(tmp_path)
    sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )

    completed = sessions.mark_completed("sess_1", latest_checkpoint_id="chk_1")
    replacement = sessions.create(
        workspace_root=workspace,
        approval_mode="normal",
        config_snapshot={},
        session_id="sess_2",
    )
    failed = sessions.mark_failed(
        "sess_2", error_summary="cancelled", latest_checkpoint_id="chk_2"
    )
    third = sessions.create(
        workspace_root=workspace,
        approval_mode="normal",
        config_snapshot={},
        session_id="sess_3",
    )

    assert completed.status == "completed"
    assert completed.latest_checkpoint_id == "chk_1"
    assert failed.status == "failed"
    assert failed.error_summary == "cancelled"
    assert third.session_id == "sess_3"
    db.close()


def test_run_store_creates_prompt_run_and_allows_phase_0_transitions(tmp_path) -> None:
    workspace, db, sessions, runs, *_ = _stores(tmp_path)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )

    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    completed = runs.mark_completed(run.run_id, latest_checkpoint_id="chk_1")

    second = runs.create_prompt_run(session.session_id, run_id="run_2")
    failed = runs.mark_failed(second.run_id, "model failed")

    assert run.run_type == "prompt"
    assert run.context_snapshot_id is None
    assert run.active_skills == []
    assert sessions.get(session.session_id).active_run_id == "run_1"
    assert completed.status == "completed"
    assert completed.latest_checkpoint_id == "chk_1"
    assert failed.status == "failed"
    assert failed.error_summary == "model failed"
    assert runs.latest_for_session(session.session_id).run_id == "run_2"
    db.close()


def test_run_store_rejects_invalid_status_transition(tmp_path) -> None:
    workspace, db, sessions, runs, *_ = _stores(tmp_path)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    runs.mark_completed(run.run_id)

    try:
        runs.mark_failed(run.run_id, "too late")
    except StoreError as exc:
        assert exc.error_class == "internal_error"
    else:
        raise AssertionError("terminal runs must not transition again")
    finally:
        db.close()


def test_session_store_rejects_invalid_status_transition(tmp_path) -> None:
    workspace, db, sessions, *_ = _stores(tmp_path)
    sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    sessions.mark_completed("sess_1")

    try:
        sessions.mark_failed("sess_1", "too late")
    except StoreError as exc:
        assert exc.error_class == "internal_error"
        assert "Invalid session transition" in exc.message
    else:
        raise AssertionError("terminal sessions must not transition again")
    finally:
        db.close()


def test_event_writer_appends_events_and_exposes_no_update_or_delete(tmp_path) -> None:
    *_, db, _sessions, _runs, events, _checkpoints, _artifacts, session, run = (
        _create_session_and_run(tmp_path)
    )
    first = RunEvent(
        event_id="evt_1",
        timestamp="2026-05-11T00:00:00Z",
        session_id=session.session_id,
        run_id=run.run_id,
        step_id=None,
        kind="run_started",
        payload={"n": 1},
    )
    second = RunEvent(
        event_id="evt_2",
        timestamp="2026-05-11T00:00:01Z",
        session_id=session.session_id,
        run_id=run.run_id,
        step_id=None,
        kind="checkpoint_written",
        payload={"checkpoint_id": "chk_1"},
    )

    events.append(first)
    events.append(second)

    assert events.list_for_run(run.run_id) == [first, second]
    assert not hasattr(events, "update")
    assert not hasattr(events, "delete")
    db.close()


def test_checkpoint_store_saves_loads_and_updates_latest_checkpoint(tmp_path) -> None:
    *_, db, sessions, runs, _events, checkpoints, _artifacts, session, run = (
        _create_session_and_run(tmp_path)
    )
    checkpoint = Checkpoint(
        checkpoint_id="chk_1",
        session_id=session.session_id,
        run_id=run.run_id,
        kind="turn",
        state={
            "session_status": "running",
            "run_status": "running",
            "prompt_turn_counter": 1,
            "latest_model_response_metadata": {"tokens": 3},
            "latest_artifact_ids": [],
            "latest_error_summary": None,
        },
        summary="first turn",
        created_at="2026-05-11T00:00:02Z",
    )

    saved = checkpoints.save(checkpoint)

    assert saved == checkpoint
    assert checkpoints.get("chk_1") == checkpoint
    assert checkpoints.latest_for_run(run.run_id) == checkpoint
    assert sessions.get(session.session_id).latest_checkpoint_id == "chk_1"
    assert runs.get(run.run_id).latest_checkpoint_id == "chk_1"
    db.close()


def test_checkpoint_store_accepts_phase_0_checkpoint_kinds(tmp_path) -> None:
    *_, db, _sessions, _runs, _events, checkpoints, _artifacts, session, run = (
        _create_session_and_run(tmp_path)
    )

    for kind in ("turn", "terminal", "error"):
        checkpoint = Checkpoint(
            checkpoint_id=f"chk_{kind}",
            session_id=session.session_id,
            run_id=run.run_id,
            kind=kind,
            state={"kind": kind},
            summary=None,
            created_at="2026-05-11T00:00:00Z",
        )
        checkpoints.save(checkpoint)

    assert checkpoints.get("chk_turn").kind == "turn"
    assert checkpoints.get("chk_terminal").kind == "terminal"
    assert checkpoints.get("chk_error").kind == "error"
    db.close()


def test_artifact_store_writes_text_and_resolves_artifact_paths(tmp_path) -> None:
    workspace, db, _sessions, _runs, _events, _checkpoints, artifacts, session, run = (
        _create_session_and_run(tmp_path)
    )

    artifact = artifacts.write_text(
        session_id=session.session_id,
        run_id=run.run_id,
        artifact_id="art_1",
        filename="output.txt",
        content="large output",
        metadata={"source": "test"},
    )

    assert artifact.artifact_type == "text"
    assert artifact.relative_path == "sess_1/artifacts/output.txt"
    assert artifacts.get("art_1") == artifact
    assert artifacts.resolve_path("art_1") == (
        workspace.resolve() / ".sessions" / "sess_1" / "artifacts" / "output.txt"
    )
    assert artifacts.resolve_path("art_1").read_text(encoding="utf-8") == "large output"
    db.close()


def test_artifact_store_registers_existing_session_file(tmp_path) -> None:
    workspace, db, _sessions, _runs, _events, _checkpoints, artifacts, session, run = (
        _create_session_and_run(tmp_path)
    )
    existing = workspace / ".sessions" / session.session_id / "artifacts" / "existing.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("already here", encoding="utf-8")

    artifact = artifacts.register_existing_file(
        session_id=session.session_id,
        run_id=run.run_id,
        artifact_id="art_2",
        path=existing,
        artifact_type="text",
        metadata={"registered": True},
    )

    assert artifact.relative_path == "sess_1/artifacts/existing.txt"
    assert artifact.metadata["registered"] is True
    assert artifact.metadata["payload_sha256"].startswith("sha256:")
    assert artifacts.resolve_path("art_2") == existing
    db.close()


def test_fake_session_run_lifecycle_persists_without_model_calls(tmp_path) -> None:
    *_, db, sessions, runs, events, checkpoints, _artifacts, session, run = (
        _create_session_and_run(tmp_path)
    )
    events.append(
        RunEvent(
            event_id="evt_1",
            timestamp="2026-05-11T00:00:00Z",
            session_id=session.session_id,
            run_id=run.run_id,
            step_id=None,
            kind="run_started",
            payload={},
        )
    )
    checkpoint = checkpoints.save(
        Checkpoint(
            checkpoint_id="chk_1",
            session_id=session.session_id,
            run_id=run.run_id,
            kind="terminal",
            state={"session_status": "completed", "run_status": "completed"},
            summary="done",
            created_at="2026-05-11T00:00:01Z",
        )
    )
    run = runs.mark_completed(run.run_id, latest_checkpoint_id=checkpoint.checkpoint_id)
    session = sessions.mark_completed(
        session.session_id, latest_checkpoint_id=checkpoint.checkpoint_id
    )

    assert session.status == "completed"
    assert run.status == "completed"
    assert len(events.list_for_run(run.run_id)) == 1
    assert checkpoints.latest_for_run(run.run_id).checkpoint_id == "chk_1"
    assert db.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
    db.close()

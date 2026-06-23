import json
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
        ArtifactStore(db.connection, db.path.parent),
    )


def _create_session_and_run(tmp_path):
    workspace, db, sessions, runs, events, artifacts = _stores(tmp_path)
    checkpoints = CheckpointStore(db.connection, artifact_store=artifacts)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={
            "provider": "anthropic",
            "execution": {
                "default_shell_timeout_seconds": 300,
                "max_shell_timeout_seconds": 300,
                "cancellation_timeout_seconds": 10,
            },
            "multimodal": {
                "view_image_enabled": False,
                "view_image_disabled_reason": "missing_multimodal_config",
                "timeout_seconds": 60,
                "max_tokens": 4096,
                "max_query_chars": 8192,
                "max_analysis_chars": 8192,
            },
            "policy": {},
        },
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


def test_session_store_release_ownership_requires_matching_owner_token(tmp_path) -> None:
    workspace, db, sessions, *_ = _stores(tmp_path)
    sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    sessions.record_owner(
        session_id="sess_1",
        owner_pid=123,
        owner_host_id="host-v1:sha256(test)",
        owner_token="owner_original",
    )
    sessions.mark_completed("sess_1", latest_checkpoint_id="chk_1")

    assert (
        sessions.release_ownership(
            session_id="sess_1",
            owner_token="owner_changed",
        )
        is False
    )
    assert (
        sessions.release_ownership(
            session_id="sess_1",
            owner_token="owner_original",
        )
        is True
    )
    row = db.connection.execute(
        "SELECT owner_pid, owner_host_id, owner_token FROM sessions WHERE session_id = 'sess_1'"
    ).fetchone()

    assert row == (None, None, None)
    db.close()


def test_stale_fail_close_event_kind_is_valid() -> None:
    event = RunEvent(
        event_id="evt_stale",
        timestamp="2026-06-06T00:00:00Z",
        session_id="sess_1",
        run_id="run_1",
        step_id=None,
        kind="stale_fail_closed",
        payload={
            "stale_proof_summary": {
                "host_match": True,
                "pid_absent": True,
                "token_fenced": True,
            }
        },
    )

    assert event.kind == "stale_fail_closed"


def test_session_store_stale_fail_close_non_resumable_is_token_fenced_and_redacted(
    tmp_path,
) -> None:
    workspace, db, sessions, runs, *_ = _stores(tmp_path)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    sessions.record_owner(
        session_id=session.session_id,
        owner_pid=12345,
        owner_host_id="host-v1:sha256(test-host)",
        owner_token="owner_original",
    )

    closed = sessions.fail_close_stale_owner(
        workspace_root=workspace,
        session_id=session.session_id,
        run_id=run.run_id,
        owner_pid=12345,
        owner_host_id="host-v1:sha256(test-host)",
        owner_token="owner_original",
        checkpoint_id=None,
    )

    assert closed is True
    row = db.connection.execute(
        """
        SELECT status, active_run_id, latest_checkpoint_id, terminal_reason,
               terminal_error_json, owner_pid, owner_host_id, owner_token
        FROM sessions
        WHERE session_id = ?
        """,
        (session.session_id,),
    ).fetchone()
    run_row = db.connection.execute(
        """
        SELECT status, latest_checkpoint_id, terminal_reason, terminal_error_json
        FROM runs
        WHERE run_id = ?
        """,
        (run.run_id,),
    ).fetchone()
    event_row = db.connection.execute(
        "SELECT kind, payload_json FROM run_events WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()

    assert row == ("failed", None, None, "terminal_stale", None, None, None, None)
    assert run_row == ("failed", None, "terminal_stale", None)
    assert event_row[0] == "stale_fail_closed"
    assert json.loads(event_row[1]) == {
        "stale_proof_summary": {
            "host_match": True,
            "pid_absent": True,
            "token_fenced": True,
        }
    }
    db.close()


def test_session_store_stale_fail_close_token_mismatch_rolls_back(tmp_path) -> None:
    workspace, db, sessions, runs, *_ = _stores(tmp_path)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    sessions.record_owner(
        session_id=session.session_id,
        owner_pid=12345,
        owner_host_id="host-v1:sha256(test-host)",
        owner_token="owner_original",
    )

    closed = sessions.fail_close_stale_owner(
        workspace_root=workspace,
        session_id=session.session_id,
        run_id=run.run_id,
        owner_pid=12345,
        owner_host_id="host-v1:sha256(test-host)",
        owner_token="owner_changed",
        checkpoint_id=None,
    )

    assert closed is False
    row = db.connection.execute(
        """
        SELECT status, active_run_id, terminal_reason,
               owner_pid, owner_host_id, owner_token
        FROM sessions
        WHERE session_id = ?
        """,
        (session.session_id,),
    ).fetchone()
    run_row = db.connection.execute(
        "SELECT status, terminal_reason FROM runs WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()
    event_count = db.connection.execute(
        "SELECT COUNT(*) FROM run_events WHERE kind = 'stale_fail_closed'"
    ).fetchone()[0]

    assert row == (
        "running",
        run.run_id,
        None,
        12345,
        "host-v1:sha256(test-host)",
        "owner_original",
    )
    assert run_row == ("running", None)
    assert event_count == 0
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
        assert exc.error_class == "persistence_error"
        assert exc.reason == "persistence_transition_failed"
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
        assert exc.error_class == "persistence_error"
        assert exc.reason == "persistence_transition_failed"
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


def test_checkpoint_store_rejects_non_terminal_checkpoint_without_latest_update(tmp_path) -> None:
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

    try:
        checkpoints.save(checkpoint)
    except StoreError as exc:
        assert "terminal_recovery" in exc.message
    else:
        raise AssertionError("Phase 3 must reject non-terminal checkpoint writes")

    assert checkpoints.latest_for_run(run.run_id) is None
    assert sessions.get(session.session_id).latest_checkpoint_id is None
    assert runs.get(run.run_id).latest_checkpoint_id is None
    db.close()


def test_checkpoint_store_rejects_phase_0_checkpoint_kinds(tmp_path) -> None:
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
        try:
            checkpoints.save(checkpoint)
        except StoreError as exc:
            assert "terminal_recovery" in exc.message
        else:
            raise AssertionError(f"Phase 3 must reject {kind} checkpoints")
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
    checkpoint = checkpoints.create_terminal_recovery(
        checkpoint_id="chk_1",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at="2026-05-11T00:00:01Z",
    )
    run = runs.get(run.run_id)
    session = sessions.get(session.session_id)

    assert session.status == "completed"
    assert run.status == "completed"
    assert len(events.list_for_run(run.run_id)) == 1
    assert checkpoints.latest_for_run(run.run_id).checkpoint_id == "chk_1"
    assert db.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
    db.close()

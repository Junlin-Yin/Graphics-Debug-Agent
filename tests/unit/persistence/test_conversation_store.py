import hashlib
import sqlite3

import pytest

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence import conversation as conversation_module
from debug_agent.persistence.conversation import (
    ConversationAppend,
    ConversationStore,
    canonical_json_bytes,
    sha256_hex,
)
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase


def _conversation_store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake"},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    return db, ConversationStore(db.connection, artifact_store=artifacts), artifacts, session, run


def _append(
    *,
    role="user",
    kind="user_input",
    content=None,
    message_group_id="grp_1",
    model_call_id="model_1",
    group_position=0,
    group_row_count=1,
    metadata=None,
    tool_call_id=None,
    artifact_id=None,
):
    return ConversationAppend(
        turn_id="turn-1",
        message_group_id=message_group_id,
        model_call_id=model_call_id,
        group_position=group_position,
        group_row_count=group_row_count,
        role=role,
        kind=kind,
        content={"text": "hello"} if content is None and artifact_id is None else content,
        artifact_id=artifact_id,
        metadata={} if metadata is None else metadata,
        source_event_id=None,
        tool_call_id=tool_call_id,
    )


def test_closed_group_append_updates_projection_and_fact_cut(tmp_path) -> None:
    db, store, _artifacts, session, run = _conversation_store(tmp_path)

    rows = store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            _append(content={"text": "你好"}, message_group_id="grp_user", model_call_id=None)
        ],
    )
    projection = store.get_projection(run.run_id)
    fact_cut = store.validate_fact_cut(run_id=run.run_id, highest_message_index=1)

    assert rows[0].message_index == 1
    assert rows[0].group_status == "closed"
    assert rows[0].content_sha256 == sha256_hex(canonical_json_bytes({"text": "你好"}))
    assert projection.source_high_watermark == 1
    assert projection.message_refs == [{"index": 1}]
    assert fact_cut.highest_message_index == 1
    assert fact_cut.message_count == 1
    db.close()


def test_closed_group_append_retries_sqlite_busy_before_partial_commit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(conversation_module, "sleep", lambda _seconds: None)
    db, _store, _artifacts, session, run = _conversation_store(tmp_path)
    db_path = db.path
    db.close()

    class BusyThenSuccessConnection(sqlite3.Connection):
        busy_failures = 0

        def execute(self, sql, parameters=(), /):
            if (
                "SELECT COALESCE(MAX(message_index), 0)" in sql
                and type(self).busy_failures < 2
            ):
                type(self).busy_failures += 1
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, parameters)

    connection = sqlite3.connect(db_path, factory=BusyThenSuccessConnection)
    try:
        store = ConversationStore(connection)
        rows = store.append_closed_group(
            session_id=session.session_id,
            run_id=run.run_id,
            messages=[
                _append(
                    content={"text": "after busy"},
                    message_group_id="grp_user",
                    model_call_id=None,
                )
            ],
        )

        assert BusyThenSuccessConnection.busy_failures == 2
        assert rows[0].message_index == 1
        assert [row.content for row in store.list_messages(run.run_id)] == [
            {"text": "after busy"}
        ]
    finally:
        connection.close()


def test_closed_group_append_does_not_retry_sqlite_busy_after_partial_commit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(conversation_module, "sleep", lambda _seconds: None)
    db, store, _artifacts, session, run = _conversation_store(tmp_path)

    original_upsert = store._upsert_projection_locked
    calls = 0

    def fail_after_insert_once(projection):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_upsert(projection)

    object.__setattr__(store, "_upsert_projection_locked", fail_after_insert_once)

    with pytest.raises(StoreError):
        store.append_closed_group(
            session_id=session.session_id,
            run_id=run.run_id,
            messages=[_append(content={"text": "partial risk"}, model_call_id=None)],
        )

    assert calls == 1
    assert store.list_messages(run.run_id) == []
    db.close()


def test_open_group_rows_are_rejected_and_fail_fact_cut_validation(tmp_path) -> None:
    db, store, _artifacts, session, run = _conversation_store(tmp_path)

    with pytest.raises(StoreError):
        store.append_closed_group(
            session_id=session.session_id,
            run_id=run.run_id,
            messages=[_append(metadata={"group_status": "open"})],
        )
    db.connection.execute(
        """
        INSERT INTO conversation_messages (
            session_id, run_id, message_index, message_group_id, model_call_id,
            group_position, group_status, group_row_count, role, kind,
            content_json, artifact_id, content_sha256, metadata_json,
            accepted_at, version
        )
        VALUES (
            'sess_1', 'run_1', 1, 'grp_bad', NULL, 0, 'open', 1, 'user',
            'user_input', '{}', NULL, 'sha256:bad', '{}',
            '2026-06-06T00:00:00Z', 1
        )
        """
    )
    db.connection.commit()

    with pytest.raises(StoreError):
        store.validate_fact_cut(run_id=run.run_id, highest_message_index=1)
    db.close()


def test_duplicate_and_truncated_group_validation_rejects_fact_cut(tmp_path) -> None:
    db, store, _artifacts, _session, run = _conversation_store(tmp_path)
    store.append_closed_group(
        session_id="sess_1",
        run_id=run.run_id,
        messages=[
            _append(message_group_id="grp_multi", group_position=0, group_row_count=2),
            _append(message_group_id="grp_multi", group_position=1, group_row_count=2),
        ],
    )

    with pytest.raises(StoreError):
        store.validate_fact_cut(run_id=run.run_id, highest_message_index=1)

    db.connection.execute(
        "UPDATE conversation_messages SET group_position = 0 WHERE message_index = 2"
    )
    db.connection.commit()
    with pytest.raises(StoreError):
        store.validate_fact_cut(run_id=run.run_id, highest_message_index=2)
    db.close()


def test_tool_call_pairing_requires_result_for_accepted_tool_call(tmp_path) -> None:
    db, store, _artifacts, _session, run = _conversation_store(tmp_path)
    store.append_closed_group(
        session_id="sess_1",
        run_id=run.run_id,
        messages=[
            _append(
                role="assistant",
                kind="assistant_tool_call",
                content={"tool_call_id": "call_1", "name": "shell_exec", "arguments": {}},
                message_group_id="grp_call",
                model_call_id="model_1",
                tool_call_id="call_1",
            )
        ],
    )

    with pytest.raises(StoreError):
        store.validate_fact_cut(run_id=run.run_id, highest_message_index=1)

    store.append_closed_group(
        session_id="sess_1",
        run_id=run.run_id,
        messages=[
            _append(
                role="tool",
                kind="tool_result",
                content={"tool_call_id": "call_1", "content": "ok"},
                message_group_id="grp_result",
                model_call_id="model_1",
                tool_call_id="call_1",
            )
        ],
    )
    assert store.validate_fact_cut(run_id=run.run_id, highest_message_index=2).message_count == 2
    db.close()


def test_artifact_backed_content_checksum_validation(tmp_path) -> None:
    db, store, artifacts, session, run = _conversation_store(tmp_path)
    artifact = artifacts.write_text(
        session_id=session.session_id,
        run_id=run.run_id,
        filename="tool-output.txt",
        content="large output",
        metadata={},
    )
    expected = "sha256:" + hashlib.sha256(b"large output").hexdigest()

    rows = store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            _append(
                role="tool",
                kind="tool_result",
                content=None,
                artifact_id=artifact.artifact_id,
                message_group_id="grp_artifact",
                model_call_id=None,
            )
        ],
    )

    assert rows[0].content_sha256 == expected
    store.validate_fact_cut(run_id=run.run_id, highest_message_index=1)
    (db.path.parent / artifact.relative_path).write_text("tampered", encoding="utf-8")
    assert store.validate_fact_cut(run_id=run.run_id, highest_message_index=1).message_count == 1
    db.close()


def test_append_atomicity_leaves_no_index_gap_or_projection_ref(tmp_path) -> None:
    db, store, _artifacts, session, run = _conversation_store(tmp_path)
    store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[_append(message_group_id="grp_ok")],
    )

    with pytest.raises(StoreError):
        store.append_closed_group(
            session_id=session.session_id,
            run_id=run.run_id,
            messages=[
                _append(message_group_id="grp_bad", content={"bad": 1.5}),
            ],
        )

    rows = store.list_messages(run.run_id)
    assert [row.message_index for row in rows] == [1]
    assert store.get_projection(run.run_id).message_refs == [{"index": 1}]
    db.close()


def test_empty_fact_cut_and_projection_checksums(tmp_path) -> None:
    db, store, _artifacts, _session, run = _conversation_store(tmp_path)

    fact_cut = store.validate_fact_cut(run_id=run.run_id, highest_message_index=0)
    projection = store.empty_projection_snapshot(run_id=run.run_id)

    assert fact_cut.message_count == 0
    assert fact_cut.checksum == sha256_hex(canonical_json_bytes({"run_id": run.run_id, "rows": []}))
    assert projection["source_high_watermark"] == 0
    assert projection["message_refs"] == []
    db.close()


def test_projection_validation_detects_tampered_message_checksum(tmp_path) -> None:
    db, store, _artifacts, session, run = _conversation_store(tmp_path)
    store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[_append(message_group_id="grp_user")],
    )
    projection = store.get_projection(run.run_id)
    store.validate_projection_snapshot(
        run_id=run.run_id,
        source_high_watermark=projection.source_high_watermark,
        message_refs=projection.message_refs,
        checksum=projection.projection_sha256,
    )

    db.connection.execute(
        "UPDATE conversation_messages SET content_sha256 = 'sha256:tampered' WHERE message_index = 1"
    )
    db.connection.commit()
    with pytest.raises(StoreError):
        store.validate_projection_snapshot(
            run_id=run.run_id,
            source_high_watermark=projection.source_high_watermark,
            message_refs=projection.message_refs,
            checksum=projection.projection_sha256,
        )
    db.close()


def test_runtime_projection_drift_fails_closed_outside_explicit_resume(tmp_path) -> None:
    db, store, _artifacts, session, run = _conversation_store(tmp_path)
    store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[_append(message_group_id="grp_user")],
    )

    with pytest.raises(StoreError):
        store.validate_runtime_projection_alignment(
            run_id=run.run_id,
            process_message_indexes=[],
            explicit_resume=False,
        )

    store.validate_runtime_projection_alignment(
        run_id=run.run_id,
        process_message_indexes=[],
        explicit_resume=True,
    )
    db.close()


def test_projection_can_be_overwritten_for_omission_or_compression(tmp_path) -> None:
    db, store, _artifacts, session, run = _conversation_store(tmp_path)
    store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[_append(message_group_id="grp_1", content={"content": "old"})],
    )
    store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[_append(message_group_id="grp_2", content={"content": "kept"})],
    )

    projection = store.overwrite_projection(
        session_id=session.session_id,
        run_id=run.run_id,
        message_refs=[{"index": 2}],
        update_reason="omission",
    )

    assert projection.source_high_watermark == 2
    assert projection.message_refs == [{"index": 2}]
    store.validate_projection_snapshot(
        run_id=run.run_id,
        source_high_watermark=projection.source_high_watermark,
        message_refs=projection.message_refs,
        checksum=projection.projection_sha256,
    )
    db.close()


def test_schema_has_conversation_tables(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    db.close()

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert "conversation_messages" in tables
    assert "conversation_projection_state" in tables

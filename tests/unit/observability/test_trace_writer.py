from __future__ import annotations

import json

from debug_agent.observability.trace_writer import TraceWriter
from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.conversation import ConversationAppend, ConversationStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.contracts import RunEvent, utc_now_iso


def _phase3_config_snapshot() -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "execution": {
            "default_shell_timeout_seconds": 300,
            "max_shell_timeout_seconds": 300,
            "cancellation_timeout_seconds": 10,
        },
        "multimodal": {
            "view_image_enabled": False,
            "view_image_disabled_reason": "not_configured",
            "timeout_seconds": 60,
            "max_tokens": 4096,
            "max_query_chars": 2000,
            "max_analysis_chars": 4000,
        },
    }


def _persist_session_with_conversation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="normal",
        config_snapshot=_phase3_config_snapshot(),
        session_id="sess_conversation_trace",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_conversation_trace")
    sessions.set_active_run(session.session_id, run.run_id)
    conversation = ConversationStore(db.connection)
    conversation.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            ConversationAppend(
                turn_id="turn-1",
                message_group_id="group_user",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="user",
                kind="user_input",
                content={"kind": "user_input", "content": "# user markdown\nhello"},
            )
        ],
    )
    conversation.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        update_reason="compression",
        messages=[
            ConversationAppend(
                turn_id="turn-1",
                message_group_id="group_summary",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="runtime",
                kind="context_summary",
                content={"kind": "context_summary", "content": "summary omitted"},
            )
        ],
    )
    conversation.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            ConversationAppend(
                turn_id="turn-1",
                message_group_id="group_tool",
                model_call_id="model-call-1",
                group_position=0,
                group_row_count=2,
                role="assistant",
                kind="assistant_tool_call",
                content={
                    "kind": "assistant_tool_call",
                    "content": "I will write the file before explaining the result.",
                    "tool_calls": [
                        {
                            "id": "tool-call-1",
                            "name": "write_file",
                            "args": {
                                "path": str(workspace / "secret.txt"),
                                "content": "secret payload",
                            },
                        }
                    ],
                },
            ),
            ConversationAppend(
                turn_id="turn-1",
                message_group_id="group_tool",
                model_call_id="model-call-1",
                group_position=1,
                group_row_count=2,
                role="tool",
                kind="tool_result",
                tool_call_id="tool-call-1",
                content={
                    "status": "error",
                    "content": None,
                    "error": {
                        "error_class": "tool_error",
                        "reason": "tool_execution_failed",
                        "message": "write failed",
                    },
                    "artifact_ids": [],
                },
            ),
        ],
    )
    conversation.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            ConversationAppend(
                turn_id="turn-1",
                message_group_id="group_assistant",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="assistant",
                kind="assistant_output",
                content={"kind": "assistant_output", "content": "Done.\n```kept"},
            )
        ],
    )
    conversation.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            ConversationAppend(
                turn_id="turn-1",
                message_group_id="group_fact",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="runtime",
                kind="failure_fact",
                content={
                    "error_class": "tool_error",
                    "reason": "tool_execution_failed",
                    "message": "Tool failed.",
                },
            )
        ],
    )
    EventWriter(db.connection, db.path.parent).append(
        RunEvent(
            event_id="evt_checkpoint",
            timestamp=utc_now_iso(),
            session_id=session.session_id,
            run_id=run.run_id,
            step_id=None,
            kind="checkpoint_written",
            payload={"checkpoint_id": "chk_ignored"},
        )
    )
    runs.mark_completed(run.run_id)
    sessions.mark_completed(session.session_id)
    db.connection.execute(
        "UPDATE sessions SET terminal_reason = ? WHERE session_id = ?",
        ("terminal_completion", session.session_id),
    )
    db.connection.commit()
    return db, session


def test_trace_writer_renders_phase35_conversation_transcript(tmp_path) -> None:
    db, session = _persist_session_with_conversation(tmp_path)
    try:
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(
            session.session_id
        )
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert result.trace_path == (
        tmp_path
        / "workspace"
        / ".sessions"
        / session.session_id
        / "logs"
        / "trace.md"
    )
    assert not (
        tmp_path / "workspace" / ".sessions" / session.session_id / "trace.md"
    ).exists()
    assert content.startswith("# debug-agent conversation trace\n\n*Exported on ")
    assert "**📊 Session Information**" in content
    assert f"- **Session ID**: `{session.session_id}`" in content
    assert "- **Run ID**: `run_conversation_trace`" in content
    assert "- **Status**: `completed`" in content
    assert "- **Terminal Reason**: `terminal_completion`" in content
    assert "- **Total Messages**: 5" in content
    assert "- **Total User Messages**: 1" in content
    assert "- **Total Assistant Messages**: 2" in content
    assert "- **Total Tool Calls**: 1" in content
    assert "## 👤 User" in content
    assert "# user markdown\nhello" in content
    assert "## 🤖 Assistant" in content
    assert "### 🔧 Tool Calls" in content
    assert "I will write the file before explaining the result." in content
    assert "**❌ write_file** (`write_file`)" in content
    assert "- **Tool Result Index**: 4" in content
    assert "- **Arguments**:\n    {" in content
    assert '"redacted": true' in content
    assert '"sha256": "1d2b0d590597f55a716a4f4e60e91827ee71c3ab6ab5b0e6ab1245305b1f6dbc"' in content
    assert '"bytes": 14' in content
    assert "- **Error**:\n    {" in content
    assert '"error_class": "tool_error"' in content
    assert '"reason": "tool_execution_failed"' in content
    assert '"message": "write failed"' in content
    assert "- **Result**:\n    null" not in content
    assert "Done.\n```kept" in content
    assert "## ⚠️ Runtime Fact" in content
    assert "`tool_error/tool_execution_failed`: Tool failed." in content
    assert "summary omitted" not in content
    assert "checkpoint_written" not in content
    assert "event_count" not in content
    assert "```json" not in content


def test_trace_writer_validates_tool_call_result_pairing_without_overwrite(
    tmp_path,
) -> None:
    db, session = _persist_session_with_conversation(tmp_path)
    writer = TraceWriter(db.connection, db.path.parent)
    first = writer.refresh_if_stale(session.session_id)
    first.trace_path.write_text("existing trace", encoding="utf-8")
    db.connection.execute(
        """
        UPDATE conversation_messages
        SET tool_call_id = ?
        WHERE run_id = ? AND kind = 'tool_result'
        """,
        ("different-call", "run_conversation_trace"),
    )
    db.connection.commit()
    try:
        try:
            writer.refresh_if_stale(session.session_id)
        except Exception as exc:
            assert "conversation_cut_invalid" in str(exc)
        else:
            raise AssertionError("Trace render should fail for unpaired tool result.")
        assert first.trace_path.read_text(encoding="utf-8") == "existing trace"
    finally:
        db.close()


def test_trace_writer_validates_inline_artifact_refs_match_artifact_ids_without_overwrite(
    tmp_path,
) -> None:
    db, session = _persist_session_with_conversation(tmp_path)
    artifact_store = ArtifactStore(db.connection, db.path.parent)
    artifact = artifact_store.write_text(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        artifact_id="art_inline",
        filename="read_file_output.txt",
        content="artifact body not rendered",
        metadata={"tool_name": "read_file", "bytes": 24},
    )
    artifact_store.write_text(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        artifact_id="art_unrelated",
        filename="unrelated.txt",
        content="unrelated artifact body",
        metadata={"tool_name": "read_file", "bytes": 23},
    )
    writer = TraceWriter(db.connection, db.path.parent)
    first = writer.refresh_if_stale(session.session_id)
    first.trace_path.write_text("existing trace", encoding="utf-8")
    content = {
        "status": "ok",
        "content": {
            "artifact_id": artifact.artifact_id,
            "relative_path": artifact.relative_path,
            "preview": "already redacted preview",
        },
        "error": None,
        "artifact_ids": [],
    }
    db.connection.execute(
        """
        UPDATE conversation_messages
        SET content_json = ?
        WHERE run_id = ? AND kind = 'tool_result'
        """,
        (
            json.dumps(content, ensure_ascii=False, sort_keys=True),
            "run_conversation_trace",
        ),
    )
    db.connection.commit()
    try:
        try:
            writer.refresh_if_stale(session.session_id)
        except Exception as exc:
            assert "conversation_cut_invalid" in str(exc)
        else:
            raise AssertionError("Trace render should fail for mismatched artifact refs.")
        assert first.trace_path.read_text(encoding="utf-8") == "existing trace"

        content["artifact_ids"] = [artifact.artifact_id, "art_unrelated"]
        db.connection.execute(
            """
            UPDATE conversation_messages
            SET content_json = ?
            WHERE run_id = ? AND kind = 'tool_result'
            """,
            (
                json.dumps(content, ensure_ascii=False, sort_keys=True),
                "run_conversation_trace",
            ),
        )
        db.connection.commit()
        try:
            writer.refresh_if_stale(session.session_id)
        except Exception as exc:
            assert "conversation_cut_invalid" in str(exc)
        else:
            raise AssertionError("Trace render should fail for unrelated artifact ids.")
        assert first.trace_path.read_text(encoding="utf-8") == "existing trace"
    finally:
        db.close()


def test_trace_writer_renders_artifact_backed_user_and_assistant_references(
    tmp_path,
) -> None:
    db, session = _persist_session_with_conversation(tmp_path)
    artifact_store = ArtifactStore(db.connection, db.path.parent)
    artifact = artifact_store.write_text(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        artifact_id="art_message",
        filename="message.md",
        content="full artifact body must not be read into trace",
        metadata={"source": "conversation"},
    )
    user_artifact = artifact_store.write_text(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        artifact_id="art_user_message",
        filename="user-message.md",
        content="full user artifact body must not be read into trace",
        metadata={"source": "conversation"},
    )
    ConversationStore(db.connection, artifact_store=artifact_store).append_closed_group(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        messages=[
            ConversationAppend(
                turn_id="turn-artifact",
                message_group_id="group_artifact_user",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="user",
                kind="user_input",
                content=None,
                artifact_id=user_artifact.artifact_id,
                metadata={
                    "preview": "already-inline user artifact preview",
                    "reference": {
                        "artifact_id": user_artifact.artifact_id,
                        "relative_path": user_artifact.relative_path,
                        "media_type": "text/markdown",
                    },
                },
            )
        ],
    )
    ConversationStore(db.connection, artifact_store=artifact_store).append_closed_group(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        messages=[
            ConversationAppend(
                turn_id="turn-artifact",
                message_group_id="group_artifact_assistant",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="assistant",
                kind="assistant_output",
                content=None,
                artifact_id=artifact.artifact_id,
                metadata={
                    "preview": "already-inline artifact preview",
                    "reference": {
                        "artifact_id": artifact.artifact_id,
                        "relative_path": artifact.relative_path,
                        "media_type": "text/markdown",
                    },
                },
            )
        ],
    )
    try:
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(
            session.session_id
        )
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert '"artifact_id": "art_user_message"' in content
    assert f'"relative_path": "{user_artifact.relative_path}"' in content
    assert '"preview": "already-inline user artifact preview"' in content
    assert '"artifact_id": "art_message"' in content
    assert f'"relative_path": "{artifact.relative_path}"' in content
    assert '"preview": "already-inline artifact preview"' in content
    assert '"media_type": "text/markdown"' in content
    assert "full user artifact body must not be read into trace" not in content
    assert "full artifact body must not be read into trace" not in content


def test_trace_writer_fails_closed_for_missing_artifact_backed_row_reference_metadata(
    tmp_path,
) -> None:
    db, session = _persist_session_with_conversation(tmp_path)
    writer = TraceWriter(db.connection, db.path.parent)
    first = writer.refresh_if_stale(session.session_id)
    first.trace_path.write_text("existing trace", encoding="utf-8")
    artifact_store = ArtifactStore(db.connection, db.path.parent)
    artifact = artifact_store.write_text(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        artifact_id="art_missing_reference",
        filename="missing-reference.md",
        content="artifact body must not be read",
        metadata={"source": "conversation"},
    )
    ConversationStore(db.connection, artifact_store=artifact_store).append_closed_group(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        messages=[
            ConversationAppend(
                turn_id="turn-artifact",
                message_group_id="group_missing_artifact_reference",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="user",
                kind="user_input",
                content=None,
                artifact_id=artifact.artifact_id,
                metadata={"preview": "preview without reference"},
            )
        ],
    )
    try:
        try:
            writer.refresh_if_stale(session.session_id)
        except Exception as exc:
            assert "artifact_missing" in str(exc)
        else:
            raise AssertionError("Trace render should fail for missing artifact metadata.")
        assert first.trace_path.read_text(encoding="utf-8") == "existing trace"
    finally:
        db.close()


def test_trace_writer_fails_closed_for_conflicting_artifact_backed_row_reference_metadata(
    tmp_path,
) -> None:
    db, session = _persist_session_with_conversation(tmp_path)
    writer = TraceWriter(db.connection, db.path.parent)
    first = writer.refresh_if_stale(session.session_id)
    first.trace_path.write_text("existing trace", encoding="utf-8")
    artifact_store = ArtifactStore(db.connection, db.path.parent)
    artifact = artifact_store.write_text(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        artifact_id="art_conflicting_reference",
        filename="conflicting-reference.md",
        content="artifact body must not be read",
        metadata={"source": "conversation"},
    )
    ConversationStore(db.connection, artifact_store=artifact_store).append_closed_group(
        session_id=session.session_id,
        run_id="run_conversation_trace",
        messages=[
            ConversationAppend(
                turn_id="turn-artifact",
                message_group_id="group_conflicting_artifact_reference",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="assistant",
                kind="assistant_output",
                content=None,
                artifact_id=artifact.artifact_id,
                metadata={
                    "preview": "preview with conflicting reference",
                    "reference": {
                        "artifact_id": artifact.artifact_id,
                        "relative_path": "other-session/artifacts/other.md",
                    },
                },
            )
        ],
    )
    try:
        try:
            writer.refresh_if_stale(session.session_id)
        except Exception as exc:
            assert "conversation_cut_invalid" in str(exc)
        else:
            raise AssertionError("Trace render should fail for conflicting artifact metadata.")
        assert first.trace_path.read_text(encoding="utf-8") == "existing trace"
    finally:
        db.close()


def _persist_session_with_events(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot=_phase3_config_snapshot(),
        session_id="sess_trace",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_trace")
    run = runs.activate_skill(
        run.run_id,
        name="alpha",
        content_hash="sha256:alpha",
        activation_reason="model_requested",
        scope="run",
    )
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
            "approval_requested",
            {
                "tool_name": "read_file",
                "risk_level": "read",
                "scope_signature": "read:/outside.txt",
                "target": "/outside.txt",
                "grant_scope": "once or session",
                "approval_request": "=== Approval Request ===",
            },
        ),
        (
            "approval_decision_recorded",
            {
                "tool_name": "read_file",
                "risk_level": "read",
                "scope_signature": "read:/outside.txt",
                "target": "/outside.txt",
                "decision": "approved_for_session",
                "grant_scope": "session",
                "message": "approved",
            },
        ),
        (
            "approval_mode_changed",
            {"old_mode": "normal", "new_mode": "semi-auto"},
        ),
        (
            "tool_call_denied",
            {
                "tool_name": "shell_exec",
                "arguments": {
                    "argv": ["git", "status"],
                    "denial_reason": "shell_policy",
                },
                "status": "denied",
                "duration": 0.002,
                "result": {
                    "status": "denied",
                    "output": None,
                    "error": {
                        "error_class": "policy_denied",
                        "message": "Shell command denied by policy.",
                    },
                    "artifacts": [],
                    "metadata": {},
                    "redacted_output": None,
                },
            },
        ),
        (
            "tool_call_failed",
            {
                "tool_name": "shell_exec",
                "arguments": {"argv": ["python", "slow.py"]},
                "status": "timeout",
                "duration": 300.0,
                "result": {
                    "status": "timeout",
                    "output": None,
                    "error": {
                        "error_class": "tool_timeout",
                        "message": "Tool timed out.",
                    },
                    "artifacts": [],
                    "metadata": {"effective_timeout_seconds": 300.0},
                    "redacted_output": None,
                },
            },
        ),
        (
            "skill_snapshot_created",
            {
                "skill_name": "alpha",
                "execution_mode": "prompt",
                "source_scope": "project",
                "content_hash": "sha256:alpha",
                "resource_count": 1,
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
            "skill_resource_loaded",
            {
                "skill_name": "alpha",
                "skill_content_hash": "sha256:alpha",
                "resource_path": "references/guide.md",
                "resource_kind": "reference",
                "resource_content_hash": "sha256:guide",
                "media_kind": "text",
                "size_bytes": 42,
                "artifact_id": None,
            },
        ),
        (
            "context_optimized",
            {
                "trigger": "omission | compression",
                "context_snapshot_id": "ctx_trace",
                "checkpoint_id": "chk_context",
                "omitted_tool_result_count": 1,
                "evicted_message_count": 2,
                "evicted_model_call_group_count": 1,
                "artifact_refs": ["art_trace"],
                "reduced_from_tokens": 900,
                "reduced_to_tokens": 300,
                "token_estimate": {"before": {"total_tokens": 900}},
            },
        ),
        (
            "compression_failed",
            {
                "error_class": "compression_failed",
                "reason": "oldest_group_too_large",
                "message": "Context compression could not fit.",
                "token_estimate": {"total_tokens": 900},
            },
        ),
        (
            "context_limit_exceeded",
            {
                "error_class": "context_limit_exceeded",
                "estimated_tokens": 212000,
                "window_tokens": 200000,
                "optimization_applied": ["omission", "compression"],
                "message": "Context window still exceeds the limit after compression. The current turn was aborted.",
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
    db.connection.execute(
        """
        INSERT INTO context_snapshots (
            context_snapshot_id, session_id, run_id, trigger,
            source_checkpoint_id, active_skill_records_json, summary,
            retained_messages_json, omitted_tool_result_count,
            evicted_message_count, evicted_model_call_group_count,
            artifact_refs_json, token_estimate_json, payload_artifact_id,
            created_at, version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ctx_trace",
            session.session_id,
            run.run_id,
            "omission | compression",
            None,
            '[{"name":"alpha","content_hash":"sha256:alpha"}]',
            '{"task_goal":"trace"}',
            "[]",
            1,
            2,
            1,
            '["art_trace"]',
            '{"after":{"total_tokens":300},"before":{"total_tokens":900}}',
            None,
            utc_now_iso(),
            1,
        ),
    )
    db.connection.commit()
    ApprovalGrantStore(db.connection).record(
        grant_id="grant_trace",
        session_id=session.session_id,
        run_id=run.run_id,
        tool_name="read_file",
        risk_level="read",
        scope_signature="read:/outside.txt",
        decision="approved_for_session",
        grant_scope="session",
        approval_request="=== Approval Request ===",
    )
    checkpoint = CheckpointStore(
        db.connection,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    ).create_terminal_recovery(
        checkpoint_id="chk_trace",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at=utc_now_iso(),
        artifact_ids=["art_trace"],
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


def test_trace_writer_excludes_event_dump_sections_and_legacy_metadata(tmp_path) -> None:
    db, session = _persist_session_with_events(tmp_path)
    try:
        result = TraceWriter(db.connection, db.path.parent).refresh_if_stale(session.session_id)
    finally:
        db.close()

    content = result.trace_path.read_text(encoding="utf-8")
    assert result.refreshed is True
    assert result.trace_path.name == "trace.md"
    assert result.trace_path.parent.name == "logs"
    assert "<!-- event_count:" not in content
    assert "<!-- latest_event_id:" not in content
    assert "## Timeline" not in content
    assert "## Checkpoints" not in content
    assert "## Context Snapshots" not in content
    assert "## Approval Grants" not in content
    assert "## Artifacts" not in content
    assert "model_call_started" not in content
    assert "model_call_completed" not in content
    assert "tool_call_completed" not in content
    assert "artifact_registered" not in content
    assert "approval_requested" not in content
    assert "checkpoint_written" not in content
    assert "- **Total Messages**: 0" in content


def test_trace_writer_always_full_rebuilds_without_legacy_stale_metadata(
    tmp_path,
) -> None:
    db, session = _persist_session_with_events(tmp_path)
    try:
        writer = TraceWriter(db.connection, db.path.parent)
        first = writer.refresh_if_stale(session.session_id)
        first.trace_path.write_text("partial old trace", encoding="utf-8")
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
        second = writer.refresh_if_stale(session.session_id)
    finally:
        db.close()

    assert first.refreshed is True
    assert second.refreshed is True
    content = second.trace_path.read_text(encoding="utf-8")
    assert content.startswith("# debug-agent conversation trace")
    assert "partial old trace" not in content
    assert "evt_completed" not in content

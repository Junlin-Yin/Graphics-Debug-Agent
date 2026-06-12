from __future__ import annotations

import json

from debug_agent.adapters.langchain_adapter import (
    _provider_message_from_segment,
    _tool_message_content,
)
from debug_agent.observability.logging import write_runtime_log
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.context_snapshots import ContextSnapshotStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.context_manager import ContextManager
from debug_agent.runtime.contracts import RunEvent, utc_now_iso
from debug_agent.runtime.model_context import ConversationMessage
from debug_agent.skills.registry import SkillSnapshot


UNICODE_TEXT = "中文调试"


def _phase3_config_snapshot() -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "note": UNICODE_TEXT,
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


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot=_phase3_config_snapshot(),
        session_id="sess_utf8",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_utf8")
    return workspace, db, session, run


def _assert_utf8_json(raw: str) -> None:
    assert UNICODE_TEXT in raw
    assert "\\u" not in raw
    assert json.loads(raw)


def test_runtime_json_columns_preserve_non_ascii_text(tmp_path) -> None:
    workspace, db, session, run = _runtime(tmp_path)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    skills = SkillSnapshotStore(db.connection)
    context_snapshots = ContextSnapshotStore(db.connection, artifacts)
    todo_plans = TodoPlanStore(db.connection)

    runs = RunStore(db.connection)
    runs.activate_skill(
        run.run_id,
        name="unicode_skill",
        content_hash="hash",
        activation_reason=UNICODE_TEXT,
    )
    events.append(
        RunEvent(
            event_id="evt_utf8",
            timestamp=utc_now_iso(),
            session_id=session.session_id,
            run_id=run.run_id,
            step_id=None,
            kind="user_message",
            payload={"content": UNICODE_TEXT},
        )
    )
    checkpoints.create_terminal_recovery(
        checkpoint_id="chk_utf8",
        session_id=session.session_id,
        run_id=run.run_id,
        terminal_status="completed",
        terminal_reason="user_exit",
        terminal_error=None,
        created_at=utc_now_iso(),
    )
    artifacts.write_text(
        session_id=session.session_id,
        run_id=run.run_id,
        filename="utf8.txt",
        content=UNICODE_TEXT,
        metadata={"label": UNICODE_TEXT},
        artifact_id="art_utf8",
    )
    skills.save_many(
        [
            SkillSnapshot(
                skill_snapshot_id="skill_snap_utf8",
                session_id=session.session_id,
                run_id=run.run_id,
                name="unicode_skill",
                execution_mode="prompt",
                source_scope="workspace",
                source_path=".debug-agent/skills/unicode",
                manifest={
                    "name": "unicode_skill",
                    "description": UNICODE_TEXT,
                    "execution_mode": "prompt",
                    "triggers": [],
                    "metadata": {"label": UNICODE_TEXT},
                },
                skill_md_content=UNICODE_TEXT,
                skill_md_content_hash="skill-md-hash",
                overall_content_hash="overall-hash",
                payload_artifact_id=None,
                resources=[],
            )
        ]
    )
    context_snapshots.save_omission_snapshot(
        session_id=session.session_id,
        run_id=run.run_id,
        source_checkpoint_id=None,
        active_skill_records=[{"name": "unicode_skill", "reason": UNICODE_TEXT}],
        retained_messages=[
            {
                "seq": 1,
                "role": "user",
                "kind": "current_user_input",
                "content": UNICODE_TEXT,
            }
        ],
        omitted_tool_result_count=0,
        artifact_refs=["art_utf8"],
        token_estimate={"note": UNICODE_TEXT},
    )
    todo_plans.replace_plan(
        session.session_id,
        run.run_id,
        [{"content": UNICODE_TEXT, "status": "pending"}],
        events,
    )

    rows = {
        "sessions": db.connection.execute(
            "SELECT config_snapshot_json FROM sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "runs": db.connection.execute(
            "SELECT active_skills_json FROM runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0],
        "run_events": db.connection.execute(
            "SELECT payload_json FROM run_events WHERE event_id = 'evt_utf8'",
        ).fetchone()[0],
        "checkpoints": db.connection.execute(
            "SELECT state_json FROM checkpoints WHERE checkpoint_id = 'chk_utf8'",
        ).fetchone()[0],
        "artifacts": db.connection.execute(
            "SELECT metadata_json FROM artifacts WHERE artifact_id = 'art_utf8'",
        ).fetchone()[0],
        "skill_snapshots": db.connection.execute(
            "SELECT manifest_json FROM skill_snapshots WHERE skill_snapshot_id = 'skill_snap_utf8'",
        ).fetchone()[0],
        "context_active_skills": db.connection.execute(
            "SELECT active_skill_records_json FROM context_snapshots WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0],
        "context_retained": db.connection.execute(
            "SELECT retained_messages_json FROM context_snapshots WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0],
        "context_token_estimate": db.connection.execute(
            "SELECT token_estimate_json FROM context_snapshots WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0],
        "todo_plans": db.connection.execute(
            "SELECT items_json FROM todo_plans WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0],
    }

    for raw in rows.values():
        _assert_utf8_json(raw)
    db.close()


def test_engine_log_and_context_payload_artifacts_preserve_non_ascii_json(
    tmp_path,
) -> None:
    workspace, db, session, run = _runtime(tmp_path)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    context_snapshots = ContextSnapshotStore(db.connection, artifacts)

    write_runtime_log(
        db.path.parent,
        session_id=session.session_id,
        run_id=run.run_id,
        level="info",
        event="unicode_event",
        message=UNICODE_TEXT,
        metadata={"note": UNICODE_TEXT},
    )
    context_snapshot = context_snapshots.save_omission_snapshot(
        session_id=session.session_id,
        run_id=run.run_id,
        source_checkpoint_id=None,
        active_skill_records=[{"name": "unicode_skill", "reason": UNICODE_TEXT}],
        retained_messages=[
            {
                "seq": 1,
                "role": "user",
                "kind": "current_user_input",
                "content": UNICODE_TEXT + ("x" * 17_000),
            }
        ],
        omitted_tool_result_count=0,
        artifact_refs=[],
        token_estimate={"note": UNICODE_TEXT},
    )

    log_text = (
        db.path.parent / session.session_id / "logs" / "events.jsonl"
    ).read_text(encoding="utf-8")
    assert UNICODE_TEXT in log_text
    assert "\\u" not in log_text

    assert context_snapshot.payload_artifact_id is not None
    artifact = artifacts.get(context_snapshot.payload_artifact_id)
    payload_text = (db.path.parent / artifact.relative_path).read_text(encoding="utf-8")
    assert UNICODE_TEXT in payload_text
    assert "\\u" not in payload_text
    db.close()


def test_model_visible_runtime_json_preserves_non_ascii_text() -> None:
    summary = ContextManager().canonical_summary_json(
        {
            "task_goal": UNICODE_TEXT,
            "completed_work": [UNICODE_TEXT],
            "inspected_or_modified_files": [],
            "remaining_work": [],
            "next_plan": [],
            "key_decisions": [],
            "constraints": [],
        }
    )
    tool_observation = _tool_message_content(
        {
            "status": "ok",
            "output": {"message": UNICODE_TEXT},
            "error": None,
            "artifacts": [],
            "metadata": {},
            "redacted_output": None,
        }
    )
    failure_observation = _provider_message_from_segment(
        ConversationMessage(
            seq=1,
            role="user",
            kind="failure_fact",
            turn_id="turn-1",
            model_call_id=None,
            tool_call_id=None,
            content={
                "error_class": "policy_denied",
                "reason": "approval_denied",
                "message": UNICODE_TEXT,
                "artifact_ids": [],
            },
        )
    )

    for content in [summary, tool_observation, failure_observation["content"]]:
        assert UNICODE_TEXT in content
        assert "\\u" not in content

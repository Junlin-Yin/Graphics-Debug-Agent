from __future__ import annotations

import json

from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.context_manager import ContextManager
from debug_agent.runtime.model_context import ConversationMessage, TokenEstimator
from debug_agent.runtime.prompt_composer import PromptComposer, PromptCompositionRequest


def _stores(tmp_path):
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
        session_id="sess_todo",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_todo")
    sessions.set_active_run(session.session_id, run.run_id)
    return db, session, run, events


def test_prompt_composer_always_injects_empty_todo_plan_after_active_skill_context(
    tmp_path,
) -> None:
    db, session, run, _events = _stores(tmp_path)
    composer = PromptComposer(
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            active_skills=[],
            context_summary="Compressed summary.",
            retained_messages=[
                ConversationMessage(
                    seq=1,
                    role="assistant",
                    kind="retained_raw",
                    turn_id="turn-1",
                    model_call_id="call-1",
                    tool_call_id=None,
                    content="Retained answer.",
                )
            ],
            live_messages=[],
            current_messages=[
                ConversationMessage(
                    seq=2,
                    role="user",
                    kind="current_user_input",
                    turn_id="turn-2",
                    model_call_id=None,
                    tool_call_id=None,
                    content="Current request.",
                )
            ],
        )
    )

    ordered = result.frame.ordered_message_segments()
    kinds = [segment.kind for segment in ordered]
    assert kinds == [
        "runtime_safety_prefix",
        "main_agent_system_prompt",
        "stable_skill_formatter_header",
        "available_skill_headers",
        "runtime_todo_plan",
        "context_summary",
        "retained_raw",
        "current_user_input",
    ]
    todo_segment = ordered[kinds.index("runtime_todo_plan")]
    assert todo_segment.role == "system"
    assert todo_segment.metadata == {
        "source": "runtime",
        "persistent": False,
        "compressible": False,
    }
    content = json.loads(str(todo_segment.content))
    assert content == {
        "plan_version": 0,
        "items": [],
        "summary": "Current Todo Plan is empty.",
        "instruction": (
            "Use the todo tool to rewrite this plan whenever task status changes "
            "or the plan no longer matches the work."
        ),
    }
    assert "runtime_todo_plan" in result.estimate.input_shape["message_kinds"]
    assert result.estimate == TokenEstimator().estimate_model_context_frame(result.frame)
    db.close()


def test_prompt_composer_injects_persisted_plan_as_structured_data(tmp_path) -> None:
    db, session, run, events = _stores(tmp_path)
    store = TodoPlanStore(db.connection)
    store.replace_plan(
        session.session_id,
        run.run_id,
        [
            {"content": "Review docs", "status": "completed"},
            {
                "content": "Patch injection",
                "status": "in_progress",
                "activeForm": "Patching injection",
            },
            {"content": "Run tests", "status": "pending"},
        ],
        events,
    )
    composer = PromptComposer(
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=store,
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
        )
    )

    todo_segment = next(
        segment
        for segment in result.frame.ordered_message_segments()
        if segment.kind == "runtime_todo_plan"
    )
    content = json.loads(str(todo_segment.content))
    assert content["plan_version"] == 1
    assert content["summary"] == (
        "Todo Plan has 1 pending, 1 in_progress, and 1 completed items."
    )
    assert content["items"] == [
        {"index": 1, "status": "completed", "content": "Review docs"},
        {
            "index": 2,
            "status": "in_progress",
            "content": "Patch injection",
            "activeForm": "Patching injection",
        },
        {"index": 3, "status": "pending", "content": "Run tests"},
    ]
    assert "Runtime enforces tool authorization" not in content["items"][1]["content"]
    db.close()


def test_prompt_composer_uses_persisted_empty_plan_after_clear(tmp_path) -> None:
    db, session, run, events = _stores(tmp_path)
    store = TodoPlanStore(db.connection)
    store.replace_plan(
        session.session_id,
        run.run_id,
        [{"content": "Initial item", "status": "pending"}],
        events,
    )
    store.replace_plan(session.session_id, run.run_id, [], events)
    composer = PromptComposer(
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=store,
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
        )
    )

    todo_segment = next(
        segment
        for segment in result.frame.ordered_message_segments()
        if segment.kind == "runtime_todo_plan"
    )
    content = json.loads(str(todo_segment.content))
    assert content["plan_version"] == 2
    assert content["items"] == []
    assert content["summary"] == "Current Todo Plan is empty."
    db.close()


def test_compression_frame_does_not_include_runtime_todo_plan(tmp_path) -> None:
    db, session, run, events = _stores(tmp_path)
    store = TodoPlanStore(db.connection)
    store.replace_plan(
        session.session_id,
        run.run_id,
        [{"content": "Do not compress this plan", "status": "in_progress"}],
        events,
    )
    manager = ContextManager()
    retained_messages = [
        ConversationMessage(
            seq=1,
            role="assistant",
            kind="assistant_output",
            turn_id="turn-old",
            model_call_id="call-old",
            tool_call_id=None,
            content="old model output",
            estimated_tokens=20,
        ),
        ConversationMessage(
            seq=2,
            role="assistant",
            kind="assistant_output",
            turn_id="turn-consumed",
            model_call_id="call-consumed",
            tool_call_id=None,
            content="Consumed old output.",
            estimated_tokens=10,
            metadata={"consumed_model_call_ids": ["call-old"]},
        ),
    ]

    plan = manager.prepare_compression(
        retained_messages=retained_messages,
        current_messages=[],
        retain_recent_model_calls=0,
        window_tokens=1000,
        compression_reserved_output_tokens=40,
    )

    assert [message.kind for message in plan.frame.evicted_messages] == [
        "assistant_output"
    ]
    assert all(
        message.kind != "runtime_todo_plan"
        for message in [*plan.frame.evicted_messages, plan.frame.instruction_segment]
    )
    assert store.get_current(run.run_id).items[0]["content"] == (
        "Do not compress this plan"
    )
    db.close()


def test_prompt_composer_requires_todo_plan_store_for_ordinary_frames(tmp_path) -> None:
    db, _session, _run, _events = _stores(tmp_path)
    try:
        PromptComposer(skill_snapshot_store=SkillSnapshotStore(db.connection))
    except TypeError as exc:
        assert "todo_plan_store" in str(exc)
    else:
        raise AssertionError("PromptComposer must require TodoPlanStore")
    finally:
        db.close()

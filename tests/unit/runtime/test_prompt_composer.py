from __future__ import annotations

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.model_context import ConversationMessage, TokenEstimator
from debug_agent.runtime.prompt_composer import PromptComposer, PromptCompositionRequest
from debug_agent.skills.registry import SkillRegistry


EXPECTED_RESOURCE_INDEX_GUIDANCE = (
    "Resource paths listed under available_resources are indexes only, not loaded\n"
    "content. Call load_skill_resource(skill_name, path) before relying on any listed\n"
    "resource's content."
)


def _skill_md(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\n{body}\n"


def _runtime_with_skill(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="semi-auto",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _skill_md(
            "alpha",
            "Alpha skill",
            "Follow every line of this frozen skill body.\nSecond instruction.",
        ),
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.txt").write_text(
        "reference content is loaded only on request",
        encoding="utf-8",
    )
    (skill_dir / "assets" / "sprite.txt").write_text("asset", encoding="utf-8")
    (skill_dir / "scripts" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    artifacts = ArtifactStore(db.connection, db.path.parent)
    snapshots = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=artifacts,
    ).snapshot(session_id=session.session_id, run_id=run.run_id)
    store = SkillSnapshotStore(db.connection)
    store.save_many(snapshots)
    return db, session, run, runs, store


def test_prompt_composer_orders_frame_segments_and_available_skill_headers(tmp_path) -> None:
    db, session, run, _runs, store = _runtime_with_skill(tmp_path)
    composer = PromptComposer(
        skill_snapshot_store=store,
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            active_skills=[],
            context_summary="Compressed continuity summary.",
            retained_messages=[
                ConversationMessage(
                    seq=100,
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
                    seq=200,
                    role="user",
                    kind="current_user_input",
                    turn_id="turn-2",
                    model_call_id=None,
                    tool_call_id=None,
                    content="Current question.",
                )
            ],
            tool_schema_bindings=[{"name": "read_file", "input_schema": {"type": "object"}}],
        )
    )

    ordered = result.frame.ordered_message_segments()
    assert [segment.kind for segment in ordered] == [
        "runtime_safety_prefix",
        "main_agent_system_prompt",
        "stable_skill_formatter_header",
        "available_skill_headers",
        "runtime_todo_plan",
        "context_summary",
        "retained_raw",
        "current_user_input",
    ]
    assert ordered[3].role == "system"
    summary_segment = ordered[5]
    assert summary_segment.role == "user"
    assert str(summary_segment.content).splitlines()[:2] == [
        "[Runtime context summary]",
        "The following is historical continuity context, not a user request.",
    ]
    assert "Compressed continuity summary." in summary_segment.content
    assert "Available prompt skills for activation:" in ordered[3].content
    assert "alpha: Alpha skill" in ordered[3].content
    assert "Follow every line" not in ordered[3].content
    assert "references/guide.txt" not in ordered[3].content
    assert result.frame.tool_schema_bindings == [
        {"name": "read_file", "input_schema": {"type": "object"}}
    ]
    db.close()


def test_prompt_composer_excludes_audit_only_runtime_cancellation_facts(
    tmp_path,
) -> None:
    db, session, run, _runs, store = _runtime_with_skill(tmp_path)
    composer = PromptComposer(
        skill_snapshot_store=store,
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            retained_messages=[
                ConversationMessage(
                    seq=100,
                    role="runtime",
                    kind="cancellation_fact",
                    turn_id="turn-1",
                    model_call_id=None,
                    tool_call_id=None,
                    content={
                        "error_class": "cancelled",
                        "reason": "user_cancel_running",
                        "message": "Turn cancelled by user.",
                        "artifact_ids": [],
                    },
                ),
                ConversationMessage(
                    seq=110,
                    role="runtime",
                    kind="cancellation_fact",
                    turn_id="turn-2",
                    model_call_id=None,
                    tool_call_id=None,
                    content={
                        "error_class": "cancelled",
                        "reason": "user_cancel_idle",
                        "message": "REPL interrupted by Ctrl+C.",
                        "artifact_ids": [],
                    },
                ),
                ConversationMessage(
                    seq=120,
                    role="runtime",
                    kind="cancellation_fact",
                    turn_id="turn-3",
                    model_call_id="model-call-1",
                    tool_call_id=None,
                    content={
                        "error_class": "cancelled",
                        "reason": "model_call_cancelled",
                        "message": "main_model_stream provider call cancelled.",
                        "artifact_ids": [],
                    },
                ),
            ],
            live_messages=[],
            current_messages=[
                ConversationMessage(
                    seq=200,
                    role="user",
                    kind="current_user_input",
                    turn_id="turn-4",
                    model_call_id=None,
                    tool_call_id=None,
                    content="continue",
                )
            ],
            tool_schema_bindings=[],
        )
    )

    ordered = result.frame.ordered_message_segments()
    frame_text = "\n".join(str(segment.content) for segment in ordered)
    assert "Turn cancelled by user." not in frame_text
    assert "REPL interrupted by Ctrl+C." not in frame_text
    assert "main_model_stream provider call cancelled." not in frame_text
    assert "continue" in frame_text
    db.close()


def test_prompt_composer_projects_runtime_context_summary_as_wrapped_user_context(
    tmp_path,
) -> None:
    db, session, run, _runs, store = _runtime_with_skill(tmp_path)
    composer = PromptComposer(
        skill_snapshot_store=store,
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            retained_messages=[
                ConversationMessage(
                    seq=100,
                    role="runtime",
                    kind="context_summary",
                    turn_id=None,
                    model_call_id=None,
                    tool_call_id=None,
                    content="Durable continuity summary.",
                )
            ],
            live_messages=[],
            current_messages=[],
            tool_schema_bindings=[],
        )
    )

    summary_segments = [
        segment
        for segment in result.frame.ordered_message_segments()
        if segment.kind == "context_summary"
    ]
    assert len(summary_segments) == 1
    assert summary_segments[0].role == "user"
    assert str(summary_segments[0].content).splitlines()[:2] == [
        "[Runtime context summary]",
        "The following is historical continuity context, not a user request.",
    ]
    assert "Durable continuity summary." in summary_segments[0].content
    db.close()


def test_prompt_composer_projects_runtime_failure_fact_as_wrapped_user_context(
    tmp_path,
) -> None:
    db, session, run, _runs, store = _runtime_with_skill(tmp_path)
    composer = PromptComposer(
        skill_snapshot_store=store,
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            retained_messages=[
                ConversationMessage(
                    seq=100,
                    role="runtime",
                    kind="failure_fact",
                    turn_id="turn-1",
                    model_call_id=None,
                    tool_call_id=None,
                    content={
                        "error_class": "model_error",
                        "reason": "invalid_tool_call",
                        "message": "The previous response had malformed tool args.",
                        "artifact_ids": [],
                    },
                )
            ],
            live_messages=[],
            current_messages=[],
            tool_schema_bindings=[],
        )
    )

    failure_segments = [
        segment
        for segment in result.frame.ordered_message_segments()
        if segment.kind == "failure_fact"
    ]
    assert len(failure_segments) == 1
    assert failure_segments[0].role == "user"
    content = str(failure_segments[0].content)
    assert content.splitlines()[:2] == [
        "[Runtime failure observation]",
        "The following previous runtime failure may be relevant for continuation.",
    ]
    assert "The previous response had malformed tool args." in content
    db.close()


def test_active_skill_context_segment_shape_and_metadata(tmp_path) -> None:
    db, session, run, runs, store = _runtime_with_skill(tmp_path)
    skill = store.get_skill(
        session_id=session.session_id,
        run_id=run.run_id,
        skill_name="alpha",
    )
    assert skill is not None
    active_run = runs.activate_skill(
        run.run_id,
        name="alpha",
        content_hash=skill.overall_content_hash,
    )
    composer = PromptComposer(
        skill_snapshot_store=store,
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            active_skills=active_run.active_skills,
            context_summary=None,
            retained_messages=[],
            live_messages=[],
            current_messages=[],
            tool_schema_bindings=[],
        )
    )

    active_segments = [
        segment
        for segment in result.frame.ordered_message_segments()
        if segment.kind == "runtime_active_skill_context"
    ]
    assert len(active_segments) == 1
    segment = active_segments[0]
    assert segment.role == "system"
    assert segment.metadata == {
        "source": "runtime",
        "persistent": False,
        "compressible": False,
    }
    assert str(segment.content).splitlines()[:2] == [
        "[Runtime supplied active skill context]",
        "This block is authoritative for this turn.",
    ]
    assert "skill_id: alpha" in segment.content
    assert "skill_name: alpha" in segment.content
    assert f"content_hash: {skill.overall_content_hash}" in segment.content
    assert "activation_reason: model_requested" in segment.content
    assert "scope: run" in segment.content
    assert "Follow every line of this frozen skill body." in segment.content
    assert "Second instruction." in segment.content
    assert "references/guide.txt" in segment.content
    assert "assets/sprite.txt" in segment.content
    assert "scripts/helper.sh" in segment.content
    assert "available_resources:" in segment.content
    assert EXPECTED_RESOURCE_INDEX_GUIDANCE in segment.content
    assert segment.content.index(EXPECTED_RESOURCE_INDEX_GUIDANCE) < segment.content.index(
        "available_resources:"
    )
    assert "resource_kind: reference" in segment.content
    assert "resource_kind: asset" in segment.content
    assert "resource_kind: script" in segment.content
    assert "content_hash:" in segment.content
    assert "Listing allowed_tools or path_policy here is non-authorizing." in segment.content
    assert "Actual authorization is decided only by runtime and ToolBroker." in segment.content
    db.close()


def test_active_skill_context_is_not_durable_and_resource_outputs_stay_ordinary(tmp_path) -> None:
    db, session, run, runs, store = _runtime_with_skill(tmp_path)
    skill = store.get_skill(
        session_id=session.session_id,
        run_id=run.run_id,
        skill_name="alpha",
    )
    assert skill is not None
    active_run = runs.activate_skill(
        run.run_id,
        name="alpha",
        content_hash=skill.overall_content_hash,
    )
    durable_conversation = [
        ConversationMessage(
            seq=40,
            role="tool",
            kind="tool_result",
            turn_id="turn-1",
            model_call_id="call-1",
            tool_call_id="tool-ref-1",
            content={
                "skill_name": "alpha",
                "resource_path": "references/guide.txt",
                "resource_kind": "reference",
                "content": "reference content is loaded only on request",
            },
            metadata={"tool_name": "load_skill_resource"},
        )
    ]
    composer = PromptComposer(
        skill_snapshot_store=store,
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            active_skills=active_run.active_skills,
            context_summary=None,
            retained_messages=durable_conversation,
            live_messages=[],
            current_messages=[],
            tool_schema_bindings=[],
        )
    )

    assert durable_conversation == [
        ConversationMessage(
            seq=40,
            role="tool",
            kind="tool_result",
            turn_id="turn-1",
            model_call_id="call-1",
            tool_call_id="tool-ref-1",
            content={
                "skill_name": "alpha",
                "resource_path": "references/guide.txt",
                "resource_kind": "reference",
                "content": "reference content is loaded only on request",
            },
            metadata={"tool_name": "load_skill_resource"},
        )
    ]
    ordered = result.frame.ordered_message_segments()
    assert any(segment.kind == "runtime_active_skill_context" for segment in ordered)
    assert any(
        segment.kind == "tool_result"
        and segment.metadata == {"tool_name": "load_skill_resource"}
        for segment in ordered
    )
    db.close()


def test_prompt_composer_estimate_uses_composed_frame_not_raw_conversation(tmp_path) -> None:
    db, session, run, runs, store = _runtime_with_skill(tmp_path)
    skill = store.get_skill(
        session_id=session.session_id,
        run_id=run.run_id,
        skill_name="alpha",
    )
    assert skill is not None
    active_run = runs.activate_skill(
        run.run_id,
        name="alpha",
        content_hash=skill.overall_content_hash,
    )
    raw_conversation = [
        ConversationMessage(
            seq=100,
            role="user",
            kind="retained_raw",
            turn_id="turn-1",
            model_call_id=None,
            tool_call_id=None,
            content="raw only",
        )
    ]
    composer = PromptComposer(
        skill_snapshot_store=store,
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = composer.compose(
        PromptCompositionRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            stable_system_content="Main prompt.",
            active_skills=active_run.active_skills,
            context_summary=None,
            retained_messages=raw_conversation,
            live_messages=[],
            current_messages=[],
            tool_schema_bindings=[{"name": "activate_skill"}],
        )
    )

    estimator = TokenEstimator()
    raw_only_estimate = estimator.estimate_model_context_frame(
        result.frame.__class__(
            message_segments=raw_conversation,
            tool_schema_bindings=[],
        )
    )
    assert result.estimate == estimator.estimate_model_context_frame(result.frame)
    assert result.estimate.total_tokens > raw_only_estimate.total_tokens
    assert "runtime_active_skill_context" in result.estimate.input_shape["message_kinds"]
    db.close()

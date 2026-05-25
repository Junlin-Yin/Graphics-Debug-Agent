from __future__ import annotations

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.model_context import ConversationMessage, TokenEstimator
from debug_agent.runtime.prompt_composer import PromptComposer, PromptCompositionRequest
from debug_agent.skills.registry import SkillRegistry


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
    composer = PromptComposer(skill_snapshot_store=store)

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
        "context_summary",
        "retained_raw",
        "current_user_input",
    ]
    assert ordered[3].role == "system"
    assert "Available prompt skills for activation:" in ordered[3].content
    assert "alpha: Alpha skill" in ordered[3].content
    assert "Follow every line" not in ordered[3].content
    assert "references/guide.txt" not in ordered[3].content
    assert result.frame.tool_schema_bindings == [
        {"name": "read_file", "input_schema": {"type": "object"}}
    ]
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
    composer = PromptComposer(skill_snapshot_store=store)

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
    assert "available_references:" in segment.content
    assert "content_hash:" in segment.content
    assert "Listing allowed_tools or path_policy here is non-authorizing." in segment.content
    assert "Actual authorization is decided only by runtime and ToolBroker." in segment.content
    db.close()


def test_active_skill_context_is_not_durable_and_reference_outputs_stay_ordinary(tmp_path) -> None:
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
                "reference_path": "references/guide.txt",
                "content": "reference content is loaded only on request",
            },
            metadata={"tool_name": "load_skill_ref_file"},
        )
    ]
    composer = PromptComposer(skill_snapshot_store=store)

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
                "reference_path": "references/guide.txt",
                "content": "reference content is loaded only on request",
            },
            metadata={"tool_name": "load_skill_ref_file"},
        )
    ]
    ordered = result.frame.ordered_message_segments()
    assert any(segment.kind == "runtime_active_skill_context" for segment in ordered)
    assert any(
        segment.kind == "tool_result"
        and segment.metadata == {"tool_name": "load_skill_ref_file"}
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
    composer = PromptComposer(skill_snapshot_store=store)

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

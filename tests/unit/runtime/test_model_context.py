from debug_agent.runtime.model_context import (
    CompressionContextFrame,
    ConversationMessage,
    ModelContextFrame,
    TokenEstimator,
)
from debug_agent.runtime.context_manager import ContextManager, CompressionError


def test_conversation_message_round_trips_through_json_safe_dict() -> None:
    message = ConversationMessage(
        seq=2,
        role="tool",
        kind="tool_result",
        turn_id="turn-1",
        model_call_id="call-1",
        tool_call_id="tool-1",
        content="tool output",
        artifact_refs=["artifact-1"],
        estimated_tokens=17,
        metadata={"model_call_group": "call-1", "consumed": True},
    )

    payload = message.to_dict()

    assert payload == {
        "seq": 2,
        "role": "tool",
        "kind": "tool_result",
        "turn_id": "turn-1",
        "model_call_id": "call-1",
        "tool_call_id": "tool-1",
        "content": "tool output",
        "artifact_refs": ["artifact-1"],
        "estimated_tokens": 17,
        "metadata": {"model_call_group": "call-1", "consumed": True},
    }
    assert ConversationMessage.from_dict(payload) == message


def test_conversation_message_preserves_dict_content_and_estimates_it() -> None:
    dict_content = {
        "tool_call": {"name": "read_file", "arguments": {"path": "src/app.py"}},
        "status": "requested",
    }
    message = ConversationMessage(
        seq=3,
        role="assistant",
        kind="tool_call",
        turn_id="turn-1",
        model_call_id="call-1",
        tool_call_id="tool-1",
        content=dict_content,
    )
    frame = ModelContextFrame(message_segments=[message], tool_schema_bindings=[])

    payload = message.to_dict()
    estimate = TokenEstimator().estimate_model_context_frame(frame)

    assert payload["content"] == dict_content
    assert isinstance(payload["content"], dict)
    assert ConversationMessage.from_dict(payload) == message
    assert estimate.total_tokens > TokenEstimator.MESSAGE_STRUCTURAL_TOKENS


def test_model_context_frame_preserves_explicit_segment_ordering() -> None:
    later = ConversationMessage(
        seq=20,
        role="user",
        kind="current_user_input",
        turn_id="turn-2",
        model_call_id=None,
        tool_call_id=None,
        content="now",
    )
    earlier = ConversationMessage(
        seq=10,
        role="system",
        kind="stable_system",
        turn_id=None,
        model_call_id=None,
        tool_call_id=None,
        content="system",
    )
    frame = ModelContextFrame(
        message_segments=[later, earlier],
        tool_schema_bindings=[],
    )

    assert [segment.seq for segment in frame.ordered_message_segments()] == [10, 20]
    assert [segment.seq for segment in frame.message_segments] == [20, 10]
    assert ModelContextFrame.from_dict(frame.to_dict()) == frame


def test_tool_schema_bindings_contribute_to_estimate() -> None:
    message = ConversationMessage(
        seq=1,
        role="user",
        kind="current_user_input",
        turn_id="turn-1",
        model_call_id=None,
        tool_call_id=None,
        content="inspect file",
    )
    without_tools = ModelContextFrame(message_segments=[message], tool_schema_bindings=[])
    with_tools = ModelContextFrame(
        message_segments=[message],
        tool_schema_bindings=[
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
    )

    estimator = TokenEstimator()

    assert estimator.estimate_model_context_frame(with_tools).total_tokens > (
        estimator.estimate_model_context_frame(without_tools).total_tokens
    )


def test_estimates_are_deterministic_and_record_metadata() -> None:
    frame = ModelContextFrame(
        message_segments=[
            ConversationMessage(
                seq=1,
                role="system",
                kind="stable_system",
                turn_id=None,
                model_call_id=None,
                tool_call_id=None,
                content="You are debug-agent.",
                metadata={"source": "config"},
            ),
            ConversationMessage(
                seq=2,
                role="user",
                kind="current_user_input",
                turn_id="turn-1",
                model_call_id=None,
                tool_call_id=None,
                content="Run tests.",
            ),
        ],
        tool_schema_bindings=[{"name": "shell_exec", "input_schema": {"type": "object"}}],
    )
    estimator = TokenEstimator()

    first = estimator.estimate_model_context_frame(frame)
    second = estimator.estimate_model_context_frame(frame)

    assert first == second
    assert first.estimator_version == TokenEstimator.VERSION
    assert first.input_shape == {
        "frame_type": "model_context",
        "message_segment_count": 2,
        "tool_schema_binding_count": 1,
        "message_kinds": ["stable_system", "current_user_input"],
        "message_roles": ["system", "user"],
    }


def test_estimator_accepts_frame_not_raw_conversation_list() -> None:
    estimator = TokenEstimator()
    raw_conversation = [
        ConversationMessage(
            seq=1,
            role="user",
            kind="durable_raw",
            turn_id="turn-1",
            model_call_id=None,
            tool_call_id=None,
            content="raw only",
        )
    ]

    try:
        estimator.estimate_model_context_frame(raw_conversation)  # type: ignore[arg-type]
    except TypeError as exc:
        assert "ModelContextFrame" in str(exc)
    else:
        raise AssertionError("raw conversation list was accepted for budget estimate")


def test_compression_context_frame_builds_only_previous_summary_evicted_history_and_instruction() -> None:
    previous = ConversationMessage(
        seq=1,
        role="system",
        kind="context_summary",
        turn_id=None,
        model_call_id=None,
        tool_call_id=None,
        content='{"task_goal":"debug"}',
    )
    old = ConversationMessage(
        seq=2,
        role="assistant",
        kind="assistant_output",
        turn_id="turn-1",
        model_call_id="call-1",
        tool_call_id=None,
        content="old inspected file.py",
        estimated_tokens=8,
    )
    consumed = ConversationMessage(
        seq=3,
        role="assistant",
        kind="assistant_output",
        turn_id="turn-2",
        model_call_id="call-2",
        tool_call_id=None,
        content="consumed call-1",
        estimated_tokens=5,
        metadata={"consumed_model_call_ids": ["call-1"]},
    )
    retained_recent = ConversationMessage(
        seq=4,
        role="assistant",
        kind="assistant_output",
        turn_id="turn-3",
        model_call_id="call-3",
        tool_call_id=None,
        content="recent raw",
        estimated_tokens=6,
        metadata={"consumed_model_call_ids": ["call-2"]},
    )
    current = ConversationMessage(
        seq=5,
        role="user",
        kind="current_user_input",
        turn_id="turn-4",
        model_call_id=None,
        tool_call_id=None,
        content="current request",
    )

    plan = ContextManager().prepare_compression(
        retained_messages=[previous, old, consumed, retained_recent],
        current_messages=[current],
        retain_recent_model_calls=1,
        window_tokens=1200,
        compression_reserved_output_tokens=100,
    )
    frame = plan.frame

    assert frame.previous_summary == '{"task_goal":"debug"}'
    assert [message.seq for message in frame.evicted_messages] == [2, 3]
    assert "current request" not in str(frame.to_dict())
    assert "recent raw" not in str(frame.to_dict())
    assert frame.instruction_segment.kind == "compression_instruction"
    assert CompressionContextFrame.from_dict(frame.to_dict()) == frame


def test_compression_budget_failure_is_reported_before_model_call() -> None:
    old = ConversationMessage(
        seq=1,
        role="assistant",
        kind="assistant_output",
        turn_id="turn-1",
        model_call_id="call-1",
        tool_call_id=None,
        content="old",
        estimated_tokens=200,
    )
    consumed = ConversationMessage(
        seq=2,
        role="assistant",
        kind="assistant_output",
        turn_id="turn-2",
        model_call_id="call-2",
        tool_call_id=None,
        content="consumed",
        estimated_tokens=10,
        metadata={"consumed_model_call_ids": ["call-1"]},
    )

    try:
        ContextManager().prepare_compression(
            retained_messages=[old, consumed],
            current_messages=[],
            retain_recent_model_calls=0,
            window_tokens=260,
            compression_reserved_output_tokens=10,
        )
    except CompressionError as exc:
        assert exc.reason == "oldest_group_too_large"
    else:
        raise AssertionError("oldest group fit failure was not reported")


def test_parse_continuity_summary_defaults_visible_fields_and_canonicalizes_json() -> None:
    summary = ContextManager().parse_continuity_summary(
        """
        {
          "task_goal": "fix compression",
          "completed_work": ["read docs"],
          "inspected_or_modified_files": ["src/debug_agent/runtime/context_manager.py"],
          "remaining_work": [],
          "next_plan": ["run tests"],
          "key_decisions": ["snapshots are not recovery truth"],
          "constraints": ["no manual command"],
          "extra": "ignored"
        }
        """
    )

    assert summary == {
        "task_goal": "fix compression",
        "completed_work": ["read docs"],
        "inspected_or_modified_files": ["src/debug_agent/runtime/context_manager.py"],
        "remaining_work": [],
        "next_plan": ["run tests"],
        "key_decisions": ["snapshots are not recovery truth"],
        "constraints": ["no manual command"],
        "visible_artifact_refs": [],
        "visible_active_skills": [],
        "visible_loaded_skill_resources": [],
        "visible_policy_or_approval_facts": [],
    }
    assert ContextManager().canonical_summary_json(summary).startswith(
        '{"completed_work":["read docs"]'
    )


def test_parse_continuity_summary_rejects_empty_non_object_missing_and_wrong_types() -> None:
    manager = ContextManager()

    for output in [
        "",
        "[]",
        '{"task_goal": "x"}',
        '{"task_goal": [], "completed_work": [], "inspected_or_modified_files": [], "remaining_work": [], "next_plan": [], "key_decisions": [], "constraints": []}',
        '{"task_goal": "x", "completed_work": ["ok", 1], "inspected_or_modified_files": [], "remaining_work": [], "next_plan": [], "key_decisions": [], "constraints": []}',
    ]:
        try:
            manager.parse_continuity_summary(output)
        except CompressionError as exc:
            assert exc.reason == "invalid_output"
        else:
            raise AssertionError(f"invalid summary parsed: {output}")


def test_compression_context_frame_round_trips() -> None:
    frame = CompressionContextFrame(
        previous_summary="summary",
        evicted_messages=[
            ConversationMessage(
                seq=1,
                role="assistant",
                kind="retained_raw",
                turn_id="turn-1",
                model_call_id="call-1",
                tool_call_id=None,
                content="done",
            )
        ],
        instruction_segment=ConversationMessage(
            seq=2,
            role="system",
            kind="compression_instruction",
            turn_id=None,
            model_call_id=None,
            tool_call_id=None,
            content="summarize",
        ),
    )

    assert CompressionContextFrame.from_dict(frame.to_dict()) == frame

from debug_agent.runtime.model_context import (
    CompressionContextFrame,
    ConversationMessage,
    ModelContextFrame,
    TokenEstimator,
)


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


def test_compression_context_frame_is_serializable_shape_only() -> None:
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

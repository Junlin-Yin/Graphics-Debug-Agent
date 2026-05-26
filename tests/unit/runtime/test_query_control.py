from __future__ import annotations

import pytest

from debug_agent.runtime.model_context import ConversationMessage, ModelContextFrame
from debug_agent.runtime.query_control import (
    CONTINUATION_REASONS,
    QueryControlPlane,
)


def test_query_control_plane_records_required_state() -> None:
    plane = QueryControlPlane()
    frame = ModelContextFrame(
        message_segments=[
            ConversationMessage(
                seq=1,
                role="user",
                kind="current_user_input",
                turn_id="turn-1",
                model_call_id=None,
                tool_call_id=None,
                content="hello",
            )
        ],
        tool_schema_bindings=[],
    )

    state = plane.start_query(
        session_id="sess_1",
        run_id="run_1",
        turn_id="turn-1",
        approval_mode="semi-auto",
        active_skill_records=[
            {
                "name": "alpha",
                "content_hash": "sha256:abc",
                "activation_reason": "model_requested",
                "scope": "run",
            }
        ],
    )
    updated = plane.record_context_estimate(
        state.query_id,
        frame=frame,
        estimate_total_tokens=42,
        estimator_version="deterministic-char-v1",
    )

    assert state.query_id.startswith("qry_")
    assert updated.turn_id == "turn-1"
    assert updated.continuation_reason == "initial_model_call"
    assert updated.current_approval_mode == "semi-auto"
    assert updated.active_skill_records == [
        {
            "name": "alpha",
            "content_hash": "sha256:abc",
            "activation_reason": "model_requested",
            "scope": "run",
        }
    ]
    assert updated.latest_context_estimate == {
        "total_tokens": 42,
        "estimator_version": "deterministic-char-v1",
    }
    assert updated.latest_model_context_frame is frame


def test_query_control_plane_accepts_only_documented_continuation_reasons() -> None:
    assert CONTINUATION_REASONS == frozenset(
        {
            "initial_model_call",
            "tool_result_continuation",
            "post_compression_continuation",
            "approval_denied_abort",
            "compression_failed_abort",
            "context_limit_abort",
            "final_assistant_response",
        }
    )
    plane = QueryControlPlane()
    state = plane.start_query(
        session_id="sess_1",
        run_id="run_1",
        turn_id="turn-1",
        approval_mode="normal",
        active_skill_records=[],
    )

    for reason in CONTINUATION_REASONS:
        assert plane.record_continuation(state.query_id, reason).continuation_reason == reason

    with pytest.raises(ValueError, match="Unsupported continuation reason"):
        plane.record_continuation(state.query_id, "unsupported")


def test_derives_model_call_groups_and_non_evictable_suffix() -> None:
    plane = QueryControlPlane()
    messages = [
        _msg(1, "assistant", "assistant_output", "turn-1", "call-1", None, 3),
        _msg(2, "assistant", "tool_call", "turn-1", "call-1", "tool-1", 2),
        _msg(3, "tool", "tool_result", "turn-1", "call-1", "tool-1", 11),
        _msg(
            4,
            "assistant",
            "assistant_output",
            "turn-2",
            "call-2",
            None,
            5,
            metadata={"consumed_model_call_ids": ["call-1"]},
        ),
        _msg(5, "assistant", "tool_call", "turn-3", "call-3", "tool-3", 2),
    ]

    groups = plane.derive_model_call_groups(messages)
    suffix = plane.compute_non_evictable_raw_suffix(
        messages,
        retain_recent_model_calls=1,
        current_messages=[_msg(99, "user", "current_user_input", "turn-4", None, None, 1)],
    )

    assert [(group.model_call_id, group.status) for group in groups] == [
        ("call-1", "closed"),
        ("call-2", "closed"),
        ("call-3", "open"),
    ]
    assert groups[0].consumed_by_later_model_call is True
    assert groups[1].consumed_by_later_model_call is False
    assert groups[2].consumed_by_later_model_call is False
    assert groups[0].estimated_tokens == 16
    assert groups[0].message_ids == [1, 2, 3]
    assert suffix.message_ids == {4, 5, 99}
    assert suffix.model_call_group_ids == {"call-2", "call-3"}


def _msg(
    seq: int,
    role: str,
    kind: str,
    turn_id: str,
    model_call_id: str | None,
    tool_call_id: str | None,
    estimated_tokens: int,
    *,
    metadata: dict | None = None,
) -> ConversationMessage:
    return ConversationMessage(
        seq=seq,
        role=role,
        kind=kind,
        turn_id=turn_id,
        model_call_id=model_call_id,
        tool_call_id=tool_call_id,
        content=f"message {seq}",
        estimated_tokens=estimated_tokens,
        metadata=metadata or {},
    )

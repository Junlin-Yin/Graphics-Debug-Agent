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

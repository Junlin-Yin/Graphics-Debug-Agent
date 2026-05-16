from __future__ import annotations

import pytest

from debug_agent.runtime.stream_events import AgentStreamEvent


def test_agent_stream_event_accepts_documented_stream_kinds() -> None:
    for kind in (
        "stream_model_call_started",
        "stream_text_delta",
        "stream_model_call_completed",
        "stream_tool_call_started",
        "stream_tool_call_completed",
        "stream_tool_result",
    ):
        event = AgentStreamEvent(kind=kind, payload={})

        assert event.kind == kind
        assert event.payload == {}


def test_agent_stream_event_rejects_unsupported_kind() -> None:
    with pytest.raises(ValueError, match="AgentStreamEvent.kind"):
        AgentStreamEvent(kind="model_call_started", payload={})


def test_agent_stream_event_requires_dict_payload() -> None:
    with pytest.raises(TypeError, match="payload"):
        AgentStreamEvent(kind="stream_text_delta", payload="text")  # type: ignore[arg-type]

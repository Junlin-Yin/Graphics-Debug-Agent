from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


AgentStreamEventKind = Literal[
    "stream_model_call_started",
    "stream_context_estimate_updated",
    "stream_text_delta",
    "stream_model_call_completed",
    "stream_tool_call_started",
    "stream_tool_call_completed",
    "stream_tool_result",
]

AGENT_STREAM_EVENT_KINDS = {
    "stream_model_call_started",
    "stream_context_estimate_updated",
    "stream_text_delta",
    "stream_model_call_completed",
    "stream_tool_call_started",
    "stream_tool_call_completed",
    "stream_tool_result",
}


@dataclass(frozen=True)
class AgentStreamEvent:
    kind: AgentStreamEventKind
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in AGENT_STREAM_EVENT_KINDS:
            raise ValueError(f"Unsupported AgentStreamEvent.kind: {self.kind}")
        if not isinstance(self.payload, dict):
            raise TypeError("AgentStreamEvent payload must be a dict.")

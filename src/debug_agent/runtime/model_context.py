from __future__ import annotations

from dataclasses import dataclass, field
import json
from math import ceil
from typing import Any


JsonDict = dict[str, Any]
MessageContent = str | JsonDict


@dataclass(frozen=True)
class ConversationMessage:
    seq: int
    role: str
    kind: str
    turn_id: str | None
    model_call_id: str | None
    tool_call_id: str | None
    content: MessageContent
    artifact_refs: list[str] = field(default_factory=list)
    estimated_tokens: int | None = None
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "seq": self.seq,
            "role": self.role,
            "kind": self.kind,
            "turn_id": self.turn_id,
            "model_call_id": self.model_call_id,
            "tool_call_id": self.tool_call_id,
            "content": _json_safe_copy(self.content),
            "artifact_refs": list(self.artifact_refs),
            "estimated_tokens": self.estimated_tokens,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: JsonDict) -> ConversationMessage:
        return cls(
            seq=payload["seq"],
            role=payload["role"],
            kind=payload["kind"],
            turn_id=payload.get("turn_id"),
            model_call_id=payload.get("model_call_id"),
            tool_call_id=payload.get("tool_call_id"),
            content=_json_safe_copy(payload["content"]),
            artifact_refs=list(payload.get("artifact_refs", [])),
            estimated_tokens=payload.get("estimated_tokens"),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class ModelContextFrame:
    message_segments: list[ConversationMessage]
    tool_schema_bindings: list[JsonDict] = field(default_factory=list)

    def ordered_message_segments(self) -> list[ConversationMessage]:
        return sorted(self.message_segments, key=lambda segment: segment.seq)

    def to_dict(self) -> JsonDict:
        return {
            "message_segments": [
                segment.to_dict() for segment in self.message_segments
            ],
            "tool_schema_bindings": [
                _json_safe_copy(binding) for binding in self.tool_schema_bindings
            ],
        }

    @classmethod
    def from_dict(cls, payload: JsonDict) -> ModelContextFrame:
        return cls(
            message_segments=[
                ConversationMessage.from_dict(segment)
                for segment in payload.get("message_segments", [])
            ],
            tool_schema_bindings=[
                _json_safe_copy(binding)
                for binding in payload.get("tool_schema_bindings", [])
            ],
        )


@dataclass(frozen=True)
class CompressionContextFrame:
    previous_summary: str | None
    evicted_messages: list[ConversationMessage]
    instruction_segment: ConversationMessage

    def to_dict(self) -> JsonDict:
        return {
            "previous_summary": self.previous_summary,
            "evicted_messages": [
                message.to_dict() for message in self.evicted_messages
            ],
            "instruction_segment": self.instruction_segment.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: JsonDict) -> CompressionContextFrame:
        return cls(
            previous_summary=payload.get("previous_summary"),
            evicted_messages=[
                ConversationMessage.from_dict(message)
                for message in payload.get("evicted_messages", [])
            ],
            instruction_segment=ConversationMessage.from_dict(
                payload["instruction_segment"]
            ),
        )


@dataclass(frozen=True)
class TokenEstimate:
    total_tokens: int
    estimator_version: str
    input_shape: JsonDict

    def to_dict(self) -> JsonDict:
        return {
            "total_tokens": self.total_tokens,
            "estimator_version": self.estimator_version,
            "input_shape": dict(self.input_shape),
        }

    @classmethod
    def from_dict(cls, payload: JsonDict) -> TokenEstimate:
        return cls(
            total_tokens=payload["total_tokens"],
            estimator_version=payload["estimator_version"],
            input_shape=dict(payload["input_shape"]),
        )


class TokenEstimator:
    VERSION = "deterministic-char-v1"
    MESSAGE_STRUCTURAL_TOKENS = 4
    TOOL_SCHEMA_STRUCTURAL_TOKENS = 8
    FRAME_STRUCTURAL_TOKENS = 2

    def estimate_model_context_frame(self, frame: ModelContextFrame) -> TokenEstimate:
        if not isinstance(frame, ModelContextFrame):
            raise TypeError("TokenEstimator requires a ModelContextFrame input")

        message_tokens = sum(
            self._estimate_message(segment)
            for segment in frame.ordered_message_segments()
        )
        tool_tokens = sum(
            self._estimate_tool_schema_binding(binding)
            for binding in frame.tool_schema_bindings
        )
        return TokenEstimate(
            total_tokens=self.FRAME_STRUCTURAL_TOKENS + message_tokens + tool_tokens,
            estimator_version=self.VERSION,
            input_shape={
                "frame_type": "model_context",
                "message_segment_count": len(frame.message_segments),
                "tool_schema_binding_count": len(frame.tool_schema_bindings),
                "message_kinds": [
                    segment.kind for segment in frame.ordered_message_segments()
                ],
                "message_roles": [
                    segment.role for segment in frame.ordered_message_segments()
                ],
            },
        )

    def _estimate_message(self, message: ConversationMessage) -> int:
        metadata_text = _stable_json(message.metadata)
        artifact_text = _stable_json(message.artifact_refs)
        visible_text = "\n".join(
            [
                message.role,
                message.kind,
                _content_to_estimate_text(message.content),
                metadata_text,
                artifact_text,
            ]
        )
        return self.MESSAGE_STRUCTURAL_TOKENS + _estimate_text_tokens(visible_text)

    def _estimate_tool_schema_binding(self, binding: JsonDict) -> int:
        return self.TOOL_SCHEMA_STRUCTURAL_TOKENS + _estimate_text_tokens(
            _stable_json(binding)
        )


def _json_safe_copy(value: Any) -> Any:
    return json.loads(_stable_json(value))


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_to_estimate_text(content: MessageContent) -> str:
    if isinstance(content, str):
        return content
    return _stable_json(content)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, ceil(len(text) / 4))

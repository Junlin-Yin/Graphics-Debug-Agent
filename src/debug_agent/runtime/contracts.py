from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Self


CONTRACT_VERSION = 1

SESSION_STATUSES = frozenset({"running", "completed", "failed"})
APPROVAL_MODES = frozenset({"normal", "yolo"})
RUN_TYPES = frozenset({"prompt"})
RUN_STATUSES = frozenset({"running", "completed", "failed"})
RUN_EVENT_KINDS = frozenset(
    {
        "session_started",
        "session_completed",
        "session_failed",
        "run_started",
        "run_completed",
        "run_failed",
        "user_message",
        "assistant_message",
        "model_call_started",
        "model_call_completed",
        "model_call_failed",
        "tool_call_started",
        "tool_call_completed",
        "tool_call_denied",
        "tool_call_failed",
        "checkpoint_written",
        "artifact_registered",
    }
)
CHECKPOINT_KINDS = frozenset({"turn", "terminal", "error"})
ARTIFACT_TYPES = frozenset({"image", "rdc", "text"})
TOOL_RESULT_STATUSES = frozenset({"ok", "error", "denied", "timeout", "cancelled"})
ERROR_CLASSES = frozenset(
    {
        "user_error",
        "config_error",
        "policy_denied",
        "tool_error",
        "model_error",
        "workflow_error",
        "internal_error",
        "timeout",
        "cancelled",
    }
)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate(value: str, allowed: frozenset[str], field_name: str) -> None:
    if value not in allowed:
        raise ValueError(f"Unsupported {field_name} for Phase 0: {value}")


@dataclass(frozen=True)
class Session:
    session_id: str
    workspace_root: str
    status: str
    approval_mode: str
    active_run_id: str | None
    artifact_root: str
    config_snapshot: dict[str, Any]
    latest_checkpoint_id: str | None
    created_at: str
    updated_at: str
    error_summary: str | None
    version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate(self.status, SESSION_STATUSES, "session status")
        _validate(self.approval_mode, APPROVAL_MODES, "approval_mode")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)


@dataclass(frozen=True)
class Run:
    run_id: str
    session_id: str
    parent_run_id: str | None
    run_type: str
    status: str
    active_skills: list[str]
    latest_checkpoint_id: str | None
    context_snapshot_id: str | None
    created_at: str
    updated_at: str
    error_summary: str | None
    version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate(self.run_type, RUN_TYPES, "run_type")
        _validate(self.status, RUN_STATUSES, "run status")
        if self.context_snapshot_id is not None:
            raise ValueError("context_snapshot_id must be None in Phase 0")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    timestamp: str
    session_id: str
    run_id: str
    step_id: str | None
    kind: str
    payload: dict[str, Any]
    version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate(self.kind, RUN_EVENT_KINDS, "event kind")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    session_id: str
    run_id: str
    kind: str
    state: dict[str, Any]
    summary: str | None
    created_at: str
    version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate(self.kind, CHECKPOINT_KINDS, "checkpoint kind")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    session_id: str
    run_id: str | None
    relative_path: str
    artifact_type: str
    metadata: dict[str, Any]
    created_at: str
    version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate(self.artifact_type, ARTIFACT_TYPES, "artifact_type")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)


@dataclass(frozen=True)
class ToolResult:
    status: str
    output: str | dict[str, Any] | None
    error: dict[str, Any] | None
    artifacts: list[str]
    metadata: dict[str, Any]
    redacted_output: str | None

    def __post_init__(self) -> None:
        _validate(self.status, TOOL_RESULT_STATUSES, "tool result status")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**data)

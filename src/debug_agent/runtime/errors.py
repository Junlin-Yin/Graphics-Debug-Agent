from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_SCHEMA_VERSION = 1

ERROR_REASON_REGISTRY: dict[str, frozenset[str]] = {
    "user_error": frozenset(
        {
            "invalid_cli_args",
            "invalid_command",
            "lookup_not_found",
            "invalid_runtime_control_target",
            "approval_input_unavailable",
        }
    ),
    "config_error": frozenset(
        {
            "legacy_schema_version",
            "unknown_schema_version",
            "schema_version_missing",
            "invalid_runtime_config",
            "invalid_policy_config",
            "provider_config_missing",
            "provider_config_invalid",
            "provider_auth_missing",
            "tool_unavailable",
            "startup_model_unavailable",
            "startup_schema_validation_failed",
        }
    ),
    "policy_error": frozenset(
        {
            "path_policy_denied",
            "shell_policy_denied",
            "approval_denied",
            "approval_required_non_interactive",
            "approval_provider_failed",
            "workspace_owner_active",
            "workspace_owner_not_proven_stale",
            "workspace_owner_confirmation_unavailable",
        }
    ),
    "model_error": frozenset(
        {
            "model_call_failed",
            "model_call_timeout",
            "provider_timeout",
            "provider_rate_limited",
            "provider_exception",
            "output_token_limit_reached",
            "model_output_invalid",
            "compression_model_failed",
            "compression_failed",
            "context_limit_exceeded",
        }
    ),
    "tool_error": frozenset(
        {
            "tool_schema_invalid",
            "unknown_tool",
            "tool_execution_failed",
            "tool_execution_timeout",
            "tool_result_invalid",
            "shell_nonzero_exit",
        }
    ),
    "skill_error": frozenset(
        {
            "skill_missing",
            "skill_manifest_invalid",
            "skill_duplicate",
            "skill_resource_invalid",
            "skill_snapshot_failed",
        }
    ),
    "persistence_error": frozenset(
        {
            "persistence_read_failed",
            "persistence_write_failed",
            "persistence_transition_failed",
            "sqlite_busy_timeout",
            "checkpoint_missing",
            "checkpoint_invalid",
            "conversation_cut_invalid",
            "artifact_missing",
            "event_write_failed",
        }
    ),
    "runtime_error": frozenset(
        {
            "internal_invariant_failed",
            "adapter_contract_violation",
            "resume_not_eligible",
            "resume_checkpoint_required",
            "terminal_transition_invalid",
            "ownership_release_failed",
            "retry_rule_invalid",
        }
    ),
    "ui_error": frozenset(
        {
            "tui_init_failed",
            "stream_render_failed",
            "trace_render_failed",
            "prompt_input_failed",
        }
    ),
    "cancelled": frozenset(
        {
            "user_cancel_running",
            "user_cancel_idle",
            "user_cancel_process",
            "model_call_cancelled",
            "tool_call_cancelled",
        }
    ),
}

SCOPES = frozenset(
    {"startup", "session", "run", "turn", "tool", "provider", "persistence", "ui"}
)
RECOVERABILITY_VALUES = frozenset(
    {
        "retryable",
        "terminal_recoverable",
        "terminal_non_resumable",
        "turn_recoverable",
        "non_recoverable",
    }
)


@dataclass(frozen=True)
class NormalizedError:
    error_class: str
    reason: str
    message: str
    scope: str
    recoverability: str
    metadata: dict[str, Any] = field(default_factory=dict)
    artifact_ids: list[str] = field(default_factory=list)
    schema_version: int = ERROR_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        error_class: str,
        reason: str,
        *,
        message: str,
        scope: str,
        recoverability: str | None = None,
        metadata: dict[str, Any] | None = None,
        artifact_ids: list[str] | None = None,
    ) -> "NormalizedError":
        if error_class not in ERROR_REASON_REGISTRY:
            raise ValueError(f"Unknown error_class: {error_class}")
        if reason not in ERROR_REASON_REGISTRY[error_class]:
            raise ValueError(f"Unknown reason for {error_class}: {reason}")
        if scope not in SCOPES:
            raise ValueError(f"Unknown error scope: {scope}")
        selected_recoverability = recoverability or default_recoverability(
            error_class, reason
        )
        if selected_recoverability not in RECOVERABILITY_VALUES:
            raise ValueError(f"Unknown recoverability: {selected_recoverability}")
        return cls(
            error_class=error_class,
            reason=reason,
            message=message,
            scope=scope,
            recoverability=selected_recoverability,
            metadata=dict(metadata or {}),
            artifact_ids=list(artifact_ids or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "error_class": self.error_class,
            "reason": self.reason,
            "message": self.message,
            "scope": self.scope,
            "recoverability": self.recoverability,
            "metadata": self.metadata,
            "artifact_ids": self.artifact_ids,
        }

    def to_model_visible(self) -> dict[str, Any]:
        return {
            "error_class": self.error_class,
            "reason": self.reason,
            "message": self.message,
            "artifact_ids": self.artifact_ids,
        }


def default_recoverability(error_class: str, reason: str) -> str:
    if error_class == "config_error":
        return "terminal_non_resumable"
    if error_class == "policy_error" and reason.startswith("workspace_owner_"):
        return "non_recoverable"
    if error_class == "tool_error":
        return "turn_recoverable"
    if error_class == "cancelled" and reason in {
        "user_cancel_running",
        "model_call_cancelled",
        "tool_call_cancelled",
    }:
        return "turn_recoverable"
    if error_class == "cancelled" and reason == "user_cancel_idle":
        return "terminal_recoverable"
    if error_class == "cancelled" and reason == "user_cancel_process":
        return "non_recoverable"
    if error_class == "runtime_error" and (
        reason.startswith("resume_") or reason == "terminal_transition_invalid"
    ):
        return "non_recoverable"
    if error_class == "persistence_error" and reason == "sqlite_busy_timeout":
        return "retryable"
    if error_class == "persistence_error":
        return "non_recoverable"
    if error_class == "model_error" and reason in {
        "provider_timeout",
        "model_call_timeout",
        "provider_rate_limited",
        "provider_exception",
        "compression_model_failed",
        "output_token_limit_reached",
    }:
        return "retryable"
    return "non_recoverable"

from __future__ import annotations

from debug_agent.runtime.errors import NormalizedError


OK = 0
ERROR_EXECUTION_FAILED = 1
ERROR_USAGE = 2
ERROR_ACTIVE_SESSION_CONFLICT = 3
ERROR_STARTUP_CONFIG = 4
ERROR_STARTUP_POLICY = 5
ERROR_STARTUP_PERSISTENCE = 6
ERROR_STARTUP_SKILL_SNAPSHOT = 7
ERROR_STARTUP_MODEL = 8
ERROR_LOOKUP_NOT_FOUND = 10
ERROR_TRACE_RENDER = 11
ERROR_MODEL_CALL = 20
ERROR_TOOL_CALL = 21
ERROR_POLICY_DENIED = 22
ERROR_CONTEXT = 23
ERROR_APPROVAL = 24
ERROR_PERSISTENCE_READ = 30
ERROR_PERSISTENCE_WRITE = 31
ERROR_PERSISTENCE_TRANSITION = 32
ERROR_INTERNAL_INVARIANT = 40
INTERRUPTED = 130


def map_error_to_exit_code(
    error: NormalizedError | dict[str, object],
    *,
    boundary: str | None = None,
) -> int:
    error_class = error.error_class if isinstance(error, NormalizedError) else str(error.get("error_class"))
    reason = error.reason if isinstance(error, NormalizedError) else str(error.get("reason"))

    if error_class == "user_error" and reason in {"invalid_cli_args", "invalid_command"}:
        return ERROR_USAGE
    if error_class == "user_error" and reason == "lookup_not_found":
        return ERROR_LOOKUP_NOT_FOUND
    if error_class == "config_error" and reason == "invalid_policy_config":
        return ERROR_STARTUP_POLICY
    if error_class == "config_error" and reason in {
        "legacy_schema_version",
        "unknown_schema_version",
        "schema_version_missing",
    }:
        return ERROR_STARTUP_PERSISTENCE
    if error_class == "config_error" and (
        reason.startswith("provider_") or reason == "startup_model_unavailable"
    ):
        return ERROR_STARTUP_MODEL
    if error_class == "config_error":
        return ERROR_STARTUP_CONFIG
    if error_class == "skill_error":
        return ERROR_STARTUP_SKILL_SNAPSHOT
    if error_class == "policy_error" and reason.startswith("workspace_owner_"):
        return ERROR_ACTIVE_SESSION_CONFLICT
    if error_class == "policy_error":
        return ERROR_POLICY_DENIED
    if error_class == "model_error" and (
        reason.startswith("compression_") or reason == "context_limit_exceeded"
    ):
        return ERROR_CONTEXT
    if error_class == "model_error":
        return ERROR_MODEL_CALL
    if error_class == "tool_error":
        return ERROR_TOOL_CALL
    if error_class == "persistence_error" and reason in {
        "persistence_read_failed",
        "sqlite_busy_timeout",
        "checkpoint_missing",
        "checkpoint_invalid",
        "conversation_cut_invalid",
        "artifact_missing",
    }:
        return ERROR_PERSISTENCE_READ
    if error_class == "persistence_error" and reason in {
        "persistence_write_failed",
        "event_write_failed",
    }:
        return ERROR_PERSISTENCE_WRITE
    if error_class == "persistence_error" and reason == "persistence_transition_failed":
        return ERROR_PERSISTENCE_TRANSITION
    if error_class == "runtime_error" and reason in {
        "internal_invariant_failed",
        "adapter_contract_violation",
        "terminal_transition_invalid",
        "retry_rule_invalid",
    }:
        return ERROR_INTERNAL_INVARIANT
    if error_class == "runtime_error" and reason in {
        "resume_not_eligible",
        "resume_checkpoint_required",
    }:
        return ERROR_EXECUTION_FAILED
    if error_class == "ui_error" and reason in {
        "stream_render_failed",
        "trace_render_failed",
    } and boundary == "trace":
        return ERROR_TRACE_RENDER
    if error_class == "cancelled" and reason == "user_cancel_process":
        return INTERRUPTED
    return ERROR_EXECUTION_FAILED

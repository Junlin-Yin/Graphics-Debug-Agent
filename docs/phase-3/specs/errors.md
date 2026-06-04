# Phase 3 Error Spec

## Purpose

This spec defines the Phase 3 normalized error taxonomy, fixed reason registry,
failure event payload shape, model-visible projection, and CLI exit-code
mapping.

Phase 3 error symbols are runtime truth. Call sites must not invent ad hoc
`error_class` or `reason` strings.

## Normalized Error Object

Internal error payloads use this shape:

```json
{
  "schema_version": 1,
  "error_class": "model_error",
  "reason": "output_token_limit_reached",
  "message": "The model stopped because the output token limit was reached.",
  "scope": "turn",
  "recoverability": "retryable",
  "source": "agent_loop_adapter",
  "metadata": {},
  "artifact_ids": []
}
```

Required fields:

- `schema_version`: integer, currently `1`.
- `error_class`: fixed symbol from `Error Classes`.
- `reason`: fixed symbol registered for the class.
- `message`: human-readable summary safe for trace/status and optional
  model-visible projection.
- `scope`: one of `startup`, `session`, `run`, `turn`, `tool`, `provider`,
  `persistence`, or `ui`.
- `recoverability`: one of `retryable`, `terminal_recoverable`,
  `terminal_non_resumable`, `turn_recoverable`, or `non_recoverable`.
- `source`: fixed runtime module/source symbol for audit.
- `metadata`: structured diagnostics allowlisted by the constructing module.
- `artifact_ids`: optional artifact references for large diagnostics.

`metadata` may include concrete paths, provider names, durations, retry counts,
exception summaries, policy facts, or store ids. It must not contain secrets,
raw provider request bodies, image bytes/base64, approval prompt drafts, or TUI
state.

## Error Classes

Phase 3 error classes are:

| Class | Meaning |
|---|---|
| `user_error` | Invalid user input, command shape, lookup target, or recoverable user action issue. |
| `config_error` | Startup/runtime configuration, policy configuration, provider configuration, or schema compatibility failure. |
| `policy_error` | Runtime policy, path policy, shell policy, or approval denial. |
| `model_error` | Main model, compression model, vision provider, or provider output failure. |
| `tool_error` | Model-visible tool schema, execution, timeout, cancellation, or result normalization failure. |
| `skill_error` | Skill discovery, freeze, manifest, resource, or snapshot failure. |
| `persistence_error` | SQLite, checkpoint, event, conversation, artifact, or store transition failure. |
| `runtime_error` | Internal invariant, adapter contract, lifecycle, or orchestration failure. |
| `ui_error` | REPL/TUI/presentation failure that does not redefine runtime truth. |
| `cancelled` | User or runtime cancellation fact. |

The previous coarse symbols `timeout`, `compression_failed`,
`context_limit_exceeded`, `policy_denied`, and `internal_error` become reasons
or mapped classes under this registry. Phase 3 code may keep legacy rendering
only for databases allowed by the schema policy; Phase 3 runtime truth must use
the normalized form.

## Reason Registry

### `user_error`

- `invalid_cli_args`
- `invalid_command`
- `lookup_not_found`
- `active_session_conflict`
- `invalid_tool_arguments`
- `invalid_runtime_control_target`
- `invalid_todo_plan`
- `approval_input_unavailable`

### `config_error`

- `legacy_schema_version`
- `unknown_schema_version`
- `schema_version_missing`
- `invalid_runtime_config`
- `invalid_policy_config`
- `invalid_shell_timeout_config`
- `provider_config_missing`
- `provider_config_invalid`
- `provider_auth_missing`
- `startup_model_unavailable`
- `startup_schema_validation_failed`

### `policy_error`

- `path_policy_denied`
- `shell_policy_denied`
- `approval_denied`
- `approval_required_non_interactive`
- `approval_provider_failed`
- `workspace_owner_active`
- `workspace_owner_not_proven_stale`
- `workspace_owner_confirmation_unavailable`

### `model_error`

- `model_call_failed`
- `model_call_timeout`
- `model_cancelled`
- `output_token_limit_reached`
- `model_output_invalid`
- `compression_failed`
- `context_limit_exceeded`
- `vision_provider_failed`
- `vision_provider_timeout`
- `vision_output_invalid`

### `tool_error`

- `tool_schema_invalid`
- `unknown_tool`
- `tool_execution_failed`
- `tool_execution_timeout`
- `tool_execution_cancelled`
- `tool_result_invalid`
- `shell_timeout`
- `shell_nonzero_exit`
- `shell_cancelled`
- `view_image_input_invalid`

### `skill_error`

- `skill_missing`
- `skill_manifest_invalid`
- `skill_duplicate`
- `skill_resource_invalid`
- `skill_snapshot_failed`

### `persistence_error`

- `persistence_read_failed`
- `persistence_write_failed`
- `persistence_transition_failed`
- `checkpoint_missing`
- `checkpoint_invalid`
- `conversation_cut_invalid`
- `artifact_missing`
- `event_write_failed`

### `runtime_error`

- `internal_invariant_failed`
- `adapter_contract_violation`
- `resume_not_eligible`
- `resume_checkpoint_required`
- `terminal_transition_invalid`
- `ownership_release_failed`
- `retry_rule_invalid`

### `ui_error`

- `tui_init_failed`
- `stream_render_failed`
- `prompt_input_failed`

### `cancelled`

- `user_cancel_running`
- `user_cancel_idle`
- `user_cancel_process`
- `provider_cancel_requested`
- `provider_cancel_uncertain`
- `shell_cancel_requested`

## Failure Events

Failure-class events must include:

```json
{
  "error": {
    "schema_version": 1,
    "error_class": "tool_error",
    "reason": "shell_timeout",
    "message": "shell_exec exceeded timeout_seconds.",
    "scope": "tool",
    "recoverability": "turn_recoverable",
    "source": "toolbroker",
    "metadata": {
      "tool_name": "shell_exec",
      "timeout_seconds": 30
    },
    "artifact_ids": []
  }
}
```

Event kind remains audit taxonomy. Existing event kinds such as
`model_call_failed`, `tool_call_failed`, `tool_call_denied`,
`compression_failed`, and `context_limit_exceeded` may remain, but their Phase 3
payloads must use `payload.error`.

## Model-Visible Projection

The model-visible projection is narrower than the internal error object:

```json
{
  "error_class": "tool_error",
  "reason": "shell_timeout",
  "message": "shell_exec exceeded timeout_seconds.",
  "artifact_ids": []
}
```

The projection must not include:

- `source`.
- `scope`.
- `recoverability`.
- retry policy.
- policy internals.
- provider internals.
- arbitrary `metadata`.
- stack traces.
- process ids unless summarized in `message` for user-visible debugging.

ToolBroker may include the projection in tool observations. Prompt Agent
Runtime may append failure/cancellation projections to durable conversation only
after the runtime has accepted the failure fact at a recovery boundary.

## CLI Exit Codes

Phase 3 CLI code must use semantic exit codes, not scattered magic numbers.

| Name | Code | Meaning |
|---|---:|---|
| `OK` | 0 | Command completed successfully. |
| `ERROR_EXECUTION_FAILED` | 1 | Runtime execution failed after entering a session/run path. |
| `ERROR_USAGE` | 2 | CLI arguments or command shape are invalid. |
| `ERROR_ACTIVE_SESSION_CONFLICT` | 3 | Workspace already has a running active session. |
| `ERROR_STARTUP_CONFIG` | 4 | Startup runtime config validation failed. |
| `ERROR_STARTUP_POLICY` | 5 | Startup policy config validation failed. |
| `ERROR_STARTUP_PERSISTENCE` | 6 | Database bootstrap or schema validation failed. |
| `ERROR_STARTUP_SKILL_SNAPSHOT` | 7 | Skill snapshot freeze failed during startup. |
| `ERROR_STARTUP_MODEL` | 8 | Model provider/client construction failed during startup. |
| `ERROR_LOOKUP_NOT_FOUND` | 10 | Requested session/run/checkpoint/artifact id was not found. |
| `ERROR_TRACE_RENDER` | 11 | Trace rendering failed after lookup succeeded. |
| `ERROR_MODEL_CALL` | 20 | Model call or model-owned context operation failed. |
| `ERROR_TOOL_CALL` | 21 | Model-visible tool call failed at the tool boundary. |
| `ERROR_POLICY_DENIED` | 22 | Runtime policy or approval denied the requested operation. |
| `ERROR_CONTEXT` | 23 | Context construction, compression, or context-window handling failed. |
| `ERROR_APPROVAL` | 24 | Approval control flow failed outside normal user-denied semantics. |
| `ERROR_PERSISTENCE_READ` | 30 | Runtime persistence read failed. |
| `ERROR_PERSISTENCE_WRITE` | 31 | Runtime persistence write failed. |
| `ERROR_PERSISTENCE_TRANSITION` | 32 | Runtime persistence state transition failed. |
| `ERROR_INTERNAL_INVARIANT` | 40 | Runtime invariant or adapter-contract violation. |
| `INTERRUPTED` | 130 | Process interrupted by user cancellation such as `Ctrl+C`. |

Mapping a specific error to an exit code is a CLI boundary decision. It must not
change persisted error class, reason, session status, run status, checkpoint
eligibility, or retry policy.

## Startup Failure Rules

Startup/config/schema failures are non-resumable.

If the failure happens before session/run creation, runtime writes no
session/run truth.

If the failure happens after session/run creation:

- write normalized audit failure facts/events when persistence is available.
- terminalize session/run.
- release active ownership if it was acquired.
- do not write any terminal recovery checkpoint.
- mark resume eligibility as false by construction.

`debug-agent resume <session_id>` must fail closed for such sessions with
`runtime_error/resume_not_eligible` or `runtime_error/resume_checkpoint_required`
as appropriate.

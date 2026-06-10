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
- `metadata`: structured diagnostics allowlisted by the constructing module.
- `artifact_ids`: required field whose value is a list. Use an empty list when
  there are no artifact references; include artifact ids only for large
  diagnostics that were actually stored through `ArtifactStore`.

`metadata` may include concrete paths, provider names, durations, retry counts,
exception summaries, minimal policy diagnostics, or store ids. It must not
contain secrets, raw provider request bodies, image bytes/base64, approval
prompt drafts, raw policy config blocks, approval prompt text, reusable approval
grant secrets or tokens, or TUI state.

Policy diagnostics in internal metadata are limited to the smallest facts needed
to explain the decision, such as policy component, normalized tool name, access
type, shell identity, redacted path classification, or denied rule category.
They must not include raw policy files, full allow/deny lists, approval prompt
contents, or other policy internals that are not needed for audit. The
model-visible projection never includes policy metadata.

`view_image` normalized error metadata, audit payloads, status output, and
events/log diagnostics inherit the Phase 2 query redaction rule.
Runtime-authored fields must not include the concrete effective query text, raw
`query` argument, query preview, or query length. They may record only the
redacted query source, such as `effective_query_source = "assistant"` or
`"default"`.
Phase 3.5 conversation trace is the explicit exception for assistant-authored
transcript content: it may render `query` when the query appears in accepted raw
tool-call arguments. This exception does not allow runtime-authored metadata,
events JSONL, status, error metadata, audit payloads, approval scope, or
`ToolResult.metadata` to copy query text, preview, or length.

## Error Classes

Phase 3 error classes are:

| Class | Meaning |
| --- | --- |
| `user_error` | Invalid user input, command shape, lookup target, or recoverable user action issue. |
| `config_error` | Startup/runtime configuration, policy configuration, provider configuration, or schema compatibility failure. |
| `policy_error` | Runtime policy, path policy, shell policy, or approval denial. |
| `model_error` | Main model, compression model, or provider output failure. |
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
- `invalid_runtime_control_target`
- `approval_input_unavailable`

### `config_error`

- `legacy_schema_version`
- `unknown_schema_version`
- `schema_version_missing`
- `invalid_runtime_config`
- `invalid_policy_config`
- `provider_config_missing`
- `provider_config_invalid`
- `provider_auth_missing`
- `tool_unavailable`
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
- `provider_timeout`
- `provider_rate_limited`
- `provider_exception`
- `output_token_limit_reached`
- `model_output_invalid`
- `compression_model_failed`
- `compression_failed`
- `context_limit_exceeded`

`provider_timeout` means the provider SDK, provider client, or provider service
reported that its own request timed out before returning a complete response.
`model_call_timeout` means the runtime-owned model-call worker or call budget
expired while waiting for the provider boundary to close. Both may be retryable
only when the central retry registry permits it, but they are distinct audit
facts and tests must not use them interchangeably.

### `tool_error`

- `tool_schema_invalid`
- `unknown_tool`
- `tool_execution_failed`
- `tool_execution_timeout`
- `tool_result_invalid`
- `shell_nonzero_exit`

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
- `sqlite_busy_timeout`
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
- `trace_render_failed`
- `prompt_input_failed`

### `cancelled`

- `user_cancel_running`
- `user_cancel_idle`
- `user_cancel_process`
- `model_call_cancelled`
- `tool_call_cancelled`

Phase 3 must not use shell/provider-specific cancellation reasons such as
`tool_error/shell_cancelled`, `cancelled/shell_cancel_requested`,
`cancelled/provider_cancel_requested`, or `cancelled/provider_cancel_uncertain`.
Shell and other brokered tool cancellations use `cancelled/tool_call_cancelled`
with `tool_name` and termination details in metadata. Model/provider boundary
cancellations use `cancelled/model_call_cancelled` as internal/audit
provider-boundary facts; during running `Ctrl+C` or `Esc`, they do not append a
separate durable conversation cancellation message because the turn-scoped
model-visible fact remains `cancelled/user_cancel_running`. Remote-stop and billing
uncertainty are metadata, not reason symbols. `view_image` is a brokered tool:
its model-visible cancelled observation uses `cancelled/tool_call_cancelled`
even though the internal vision provider cancellation may be recorded in
metadata.

Main prompt model provider configuration failures may use
`config_error/provider_config_missing`,
`config_error/provider_config_invalid`, `config_error/provider_auth_missing`,
or `config_error/startup_model_unavailable` as startup failures. Missing or
invalid multimodal `view_image` configuration discovered at session startup
retains the Phase 2 behavior: runtime freezes `view_image` as disabled for the
session and omits it from model-visible tool bindings. It must not upgrade that
disabled tool availability state into a session startup failure.

If a stale or direct valid `view_image` call reaches `ToolBroker` while the
frozen session tool-availability snapshot has `view_image` disabled, runtime
must return `config_error/tool_unavailable` with the no-secret disabled reason in
the message or internal metadata. Runtime must not route that call to
`ViewImageTool` or the vision provider. Malformed disabled `view_image` calls
still fail schema/local validation first with `tool_error/tool_schema_invalid`;
unknown tool names remain `tool_error/unknown_tool`.

Active workspace ownership blockage is a policy/control-plane failure, not a
human CLI argument failure. Startup and resume paths blocked by an active owner
must use `policy_error/workspace_owner_active` unless stale proof specifically
fails with `policy_error/workspace_owner_not_proven_stale` or confirmation is
unavailable with `policy_error/workspace_owner_confirmation_unavailable`.

## Failure Events

Failure-class events must include:

```json
{
  "error": {
    "schema_version": 1,
    "error_class": "tool_error",
    "reason": "tool_execution_timeout",
    "message": "shell_exec exceeded timeout_seconds.",
    "scope": "tool",
    "recoverability": "turn_recoverable",
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

Brokered cancelled tool observations use the existing `tool_call_failed` event
kind with `payload.error.error_class = "cancelled"` and
`payload.error.reason = "tool_call_cancelled"`. Main provider cancellation may
use a model/provider failure-class audit event with
`payload.error.reason = "model_call_cancelled"`, but it must not be appended as
a separate durable conversation fact during running `Ctrl+C` or `Esc`.

## Model-Visible Projection

The model-visible projection is narrower than the internal error object:

```json
{
  "error_class": "tool_error",
  "reason": "tool_execution_timeout",
  "message": "shell_exec exceeded timeout_seconds.",
  "artifact_ids": []
}
```

`artifact_ids` is a list in the model-visible projection as well. Use an empty
list when no artifact-backed diagnostic should be shown to the model.

The projection must not include:

- `scope`.
- `recoverability`.
- retry policy.
- policy internals.
- provider internals.
- arbitrary `metadata`.
- stack traces.
- process ids unless summarized in `message` for user-visible debugging.

Policy configuration failures use `config_error/invalid_policy_config` and map
to `ERROR_STARTUP_POLICY` at the CLI boundary. Runtime policy, path policy,
shell policy, and approval denials use `policy_error/*` and map to
`ERROR_POLICY_DENIED`.

Brokered tool timeouts use `tool_error/tool_execution_timeout` for every tool,
including `shell_exec` and `view_image`. The concrete tool name and component,
such as `tool_name = "shell_exec"` or `component = "vision_provider"`, live in
metadata and must not become reason symbols.

Tool-specific input and local validation failures must use generic tool reasons
such as `tool_schema_invalid` or `tool_execution_failed`. Current tool names,
including `view_image` and `todo`, live in metadata and must not become reason
symbols.

This is a Phase 3 taxonomy and tool-contract breaking change from earlier phase
tool contracts. In particular, it replaces the Phase 2 `view_image` and `todo`
schema/semantic validation expectation of `user_error` with ToolBroker-boundary
`tool_error/tool_schema_invalid`. Malformed or locally invalid model-visible
`view_image` and `todo` calls, including missing fields, unknown fields,
invalid field values, and semantic validation failures before provider or
side-effect execution starts, use `tool_error/tool_schema_invalid`, not
`user_error`.

Model-visible tool argument failures are ToolBroker boundary failures, not
human CLI input failures. Invalid tool JSON shape, missing required fields,
unknown fields, invalid field values, Todo Plan semantic validation failures,
and view-image local input validation failures must use
`tool_error/tool_schema_invalid` unless the tool has already started execution
and the failure belongs to execution or result normalization. `user_error` is
reserved for human-facing CLI, slash-command, lookup, and equivalent direct user
input boundaries.

ToolBroker may include the projection in tool observations. Prompt Agent
Runtime may append failure/cancellation projections to durable conversation only
after the runtime has accepted the failure fact at a recovery boundary.

## CLI Exit Codes

Phase 3 CLI code must use semantic exit codes, not scattered magic numbers.

| Name | Code | Meaning |
| --- | ---: | --- |
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
| `INTERRUPTED` | 130 | Process interrupted by user cancellation such as `Ctrl+C`, `Esc`, or an abnormal process-level interrupt. |

Mapping a specific error to an exit code is a CLI boundary decision. It must not
change persisted error class, reason, session status, run status, checkpoint
eligibility, or retry policy.

Phase 3 command boundaries must use this dispatch order when no more specific
mapping is defined below:

| Internal error | Default CLI exit code |
| --- | --- |
| `user_error/invalid_cli_args` or `user_error/invalid_command` | `ERROR_USAGE` |
| `user_error/lookup_not_found` | `ERROR_LOOKUP_NOT_FOUND` |
| `config_error/invalid_policy_config` | `ERROR_STARTUP_POLICY` |
| `config_error/legacy_schema_version`, `config_error/unknown_schema_version`, or `config_error/schema_version_missing` outside the startup legacy reset path | `ERROR_STARTUP_PERSISTENCE` |
| Other startup `config_error/*` for runtime config | `ERROR_STARTUP_CONFIG` |
| Startup `config_error/provider_*` or `config_error/startup_model_unavailable` | `ERROR_STARTUP_MODEL` |
| Startup `skill_error/*` | `ERROR_STARTUP_SKILL_SNAPSHOT` |
| `policy_error/workspace_owner_*` on startup or resume | `ERROR_ACTIVE_SESSION_CONFLICT` |
| Other `policy_error/*` | `ERROR_POLICY_DENIED` |
| `model_error/compression_*` or `model_error/context_limit_exceeded` | `ERROR_CONTEXT` |
| Other `model_error/*` | `ERROR_MODEL_CALL` |
| `tool_error/*` | `ERROR_TOOL_CALL` |
| `persistence_error/persistence_read_failed`, `persistence_error/checkpoint_missing`, `persistence_error/checkpoint_invalid`, `persistence_error/conversation_cut_invalid`, or `persistence_error/artifact_missing` | `ERROR_PERSISTENCE_READ` |
| `persistence_error/persistence_write_failed`, `persistence_error/event_write_failed`, or persistence failures while writing checkpoints, conversation rows, artifacts, or audit facts | `ERROR_PERSISTENCE_WRITE` |
| `persistence_error/persistence_transition_failed` or failed lifecycle/ownership store transition | `ERROR_PERSISTENCE_TRANSITION` |
| `runtime_error/internal_invariant_failed`, `runtime_error/adapter_contract_violation`, `runtime_error/terminal_transition_invalid`, or `runtime_error/retry_rule_invalid` | `ERROR_INTERNAL_INVARIANT` |
| `runtime_error/resume_not_eligible` or `runtime_error/resume_checkpoint_required` | `ERROR_EXECUTION_FAILED` |
| `ui_error/stream_render_failed` after trace lookup succeeds | `ERROR_TRACE_RENDER` |
| `ui_error/trace_render_failed` after trace lookup succeeds | `ERROR_TRACE_RENDER` |
| Other `ui_error/*` | `ERROR_EXECUTION_FAILED` |
| `cancelled/user_cancel_process` or process-level interrupt | `INTERRUPTED` |
| Other accepted runtime failures after entering a session/run execution path | `ERROR_EXECUTION_FAILED` |

One-shot, REPL, `resume`, `status`, and `trace` may override only where this
spec or another Phase 3 spec names a more specific command-boundary mapping.

Fail-closed schema compatibility failures are classified as `config_error`
because schema version is part of compatibility configuration, but their CLI
boundary exit code is fixed to `ERROR_STARTUP_PERSISTENCE`. This applies to
`config_error/legacy_schema_version`, `config_error/unknown_schema_version`, and
`config_error/schema_version_missing` when the active command is not the Phase 3
startup legacy reset path. The startup legacy reset path is only a REPL or
one-shot command path that will create a new session/run. Startup legacy reset
deletes the legacy database before interpreting rows and does not create a
schema-failure session.

Active ownership blockage uses `policy_error/workspace_owner_active` in
persisted and internal error payloads. The CLI boundary maps startup and resume
ownership blockage to `ERROR_ACTIVE_SESSION_CONFLICT` without changing the
persisted error symbol.

When startup or resume can prove an active owner stale but cannot obtain the
required user confirmation, runtime uses
`policy_error/workspace_owner_confirmation_unavailable`. At the CLI boundary,
startup and resume map this reason to `ERROR_ACTIVE_SESSION_CONFLICT` because
the command remains blocked by active workspace ownership.

## Default Recoverability Registry

Every normalized error constructor must set `recoverability` deliberately. The
default mapping is:

| Error class / reason family | Default recoverability |
| --- | --- |
| `config_error/*` during startup, schema validation, or provider construction | `terminal_non_resumable` |
| `policy_error/workspace_owner_*` before session creation or resume revival | `non_recoverable` |
| `policy_error/approval_denied` and `policy_error/approval_required_non_interactive` in long-lived REPL turns | `turn_recoverable` |
| `policy_error/approval_denied` and `policy_error/approval_required_non_interactive` in one-shot prompt runs | `terminal_recoverable` |
| `model_error/provider_timeout`, `model_error/model_call_timeout`, `model_error/provider_rate_limited`, transient `model_error/provider_exception`, and transient `model_error/compression_model_failed` when retry registry permits repeat | `retryable` |
| `model_error/output_token_limit_reached` when retry registry permits continuation and the partial output is text-only with no complete or partial tool-use fragment | `retryable` |
| `model_error/compression_failed` and `model_error/context_limit_exceeded` in long-lived REPL turns | `turn_recoverable` |
| `model_error/compression_failed` and `model_error/context_limit_exceeded` in one-shot prompt runs after terminal checkpoint eligibility holds | `terminal_recoverable` |
| `model_error/compression_failed` and `model_error/context_limit_exceeded` in one-shot prompt runs before terminal checkpoint eligibility holds | `terminal_non_resumable` |
| ordinary `tool_error/*` returned as model-visible tool observations | `turn_recoverable` |
| `persistence_error/sqlite_busy_timeout` inside a retry-eligible persistence transaction boundary | `retryable` |
| `persistence_error/*` that prevents checkpoint, conversation, or lifecycle consistency | `non_recoverable` |
| `runtime_error/resume_*` and `runtime_error/terminal_transition_invalid` | `non_recoverable` |
| `cancelled/user_cancel_running`, `cancelled/model_call_cancelled`, and `cancelled/tool_call_cancelled` | `turn_recoverable` |
| `cancelled/user_cancel_idle` after terminal recovery checkpoint creation succeeds | `terminal_recoverable` |
| `cancelled/user_cancel_process` before a recovery boundary | `non_recoverable` |

`recoverability` describes ordinary error handling after the normalized failure
is accepted. It does not decide whether a user-triggered startup or resume
workflow may attempt stale proof and user-confirmed fail-close for an active
ownership blockage. Stale fail-close eligibility is governed by
`stale-fail-close.md` and `resume.md`.

If a call site needs a different recoverability value, the active phase spec
must define that path explicitly and tests must cover the fixed combination of
`error_class`, `reason`, `scope`, and `recoverability`.

## Startup Failure Rules

Startup/config/schema failures are non-resumable.

If the failure happens before session/run creation, runtime writes no
session/run truth.

If the failure happens after session/run creation:

- write normalized audit failure facts/events when persistence is available.
- terminalize session/run.
- release active ownership if it was acquired.
- do not write any terminal recovery checkpoint.
- mark resume eligibility as false by construction through the same structured
  non-resumable startup-failure marker required by `resume.md`.

`debug-agent resume <session_id>` must fail closed for such sessions with
`runtime_error/resume_not_eligible` or `runtime_error/resume_checkpoint_required`
as appropriate.

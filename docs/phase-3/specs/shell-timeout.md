# Phase 3 Shell Timeout Spec

## Purpose

Phase 3 cleans up shell timeout configuration and error semantics.

The goal is to make shell default timeout and shell maximum timeout explicit,
and to prevent explicit tool input from being silently capped by the default.

## Config Contract

Shell execution config must distinguish:

- default shell timeout.
- maximum shell timeout.

The default applies when `shell_exec.timeout_seconds` is omitted.

The maximum is the upper bound for any explicit `shell_exec.timeout_seconds`.

Invalid shell timeout configuration is a startup config/policy failure and must
use normalized error reason `config_error/invalid_shell_timeout_config`.

## Tool Input Contract

`shell_exec.timeout_seconds` means the requested timeout.

Runtime behavior:

- omitted value uses the configured default.
- explicit value must be positive.
- explicit value must be less than or equal to configured maximum.
- explicit value greater than maximum is denied/failed with normalized
  user/config error according to ToolBroker schema boundary.
- runtime must not silently cap explicit value to the default.

## Timeout Result

When a shell command exceeds the effective timeout:

- ToolResult status is timeout according to ToolBroker conventions.
- normalized error uses `tool_error/shell_timeout`.
- event payload includes `payload.error`.
- shell process receives best-effort termination.
- partial output may be returned only as normalized failed/timeout tool
  observation or artifact after command-runner boundary closes.

Shell timeout does not make shell mid-flight state resumable.

## Cancellation Interaction

Running `Ctrl+C` while shell is active uses cancellation behavior from
`cancellation.md`.

If cancellation and timeout race, runtime must record one primary normalized
reason and may include the other as metadata. It must not emit contradictory
accepted tool results.

## Retry

Shell timeout is not retryable by default in Phase 3.

Skills may ask the model to run a new shell command after seeing a timeout
observation, but runtime must not automatically replay the timed-out command.

# Phase 3 Shell Timeout Spec

## Purpose

Phase 3 cleans up shell timeout configuration and error semantics.

The goal is to make shell default timeout and shell maximum timeout explicit,
and to prevent explicit tool input from being silently capped by the default.

Phase 3 replaces the Phase 1 shell timeout behavior where an explicit
`shell_exec.timeout_seconds` was silently capped by
`default_shell_timeout_seconds`. In Phase 3, the default is used only when the
tool input omits `timeout_seconds`; explicit values are honored exactly after
validation against `max_shell_timeout_seconds`.

The Phase 0/1 `[defaults].timeout_seconds` setting remains the main model or
provider call timeout setting when that setting is still supported by the
runtime. It is not the shell execution default and must not be reused as
`default_shell_timeout_seconds`.

## Phase 3 Execution Config Keys

Phase 3 execution settings under `[execution]` in
`~/.debug-agent/config.toml` are frozen into the session config snapshot and
restored from that frozen snapshot during explicit resume. Resume must not
hot-reload current `config.toml` values.

| Key | Default | Validation | Meaning |
|---|---:|---|---|
| `default_shell_timeout_seconds` | `300` | positive integer | Effective timeout when `shell_exec.timeout_seconds` is omitted. |
| `max_shell_timeout_seconds` | `3600` | positive integer and `>= default_shell_timeout_seconds` | Upper bound for explicit `shell_exec.timeout_seconds`. |
| `cancellation_timeout_seconds` | `10` | positive integer | Local cleanup envelope after runtime accepts a running interruption. |

Invalid Phase 3 execution config is a startup config failure using
`config_error/invalid_runtime_config`.

`cancellation_timeout_seconds` is defined normatively in
`cancellation.md`; it is not a shell execution timeout, provider call timeout,
remote cancellation guarantee, or model-visible tool input.

## Shell Timeout Config Contract

Shell execution config under `[execution]` in `~/.debug-agent/config.toml` must
distinguish:

- `default_shell_timeout_seconds`, default `300`.
- `max_shell_timeout_seconds`, default `3600`.

The default applies when `shell_exec.timeout_seconds` is omitted.

The maximum is the upper bound for any explicit `shell_exec.timeout_seconds`.
Both values must be positive integers, and
`max_shell_timeout_seconds >= default_shell_timeout_seconds`.

Invalid shell timeout configuration is a startup config failure and must use
normalized error reason `config_error/invalid_runtime_config`.
Even if the invalid value is discovered while parsing files that also contain
policy declarations, shell timeout is an execution runtime setting and must not
use `config_error/invalid_policy_config`.

Resolved shell timeout settings are frozen into the session config snapshot.

## Tool Input Contract

`shell_exec.timeout_seconds` means the requested timeout. The model-visible
`shell_exec` schema must describe the configured maximum from the frozen session
config snapshot so the model can see the valid range for `timeout_seconds`.
Resume must restore this schema limit from the original frozen snapshot, not
from the current `config.toml`. Terminal recovery checkpoints therefore must
validate the frozen config/policy/tool-availability references needed to rebuild
the same `shell_exec` schema after resume.

Runtime behavior:

- omitted value uses the configured default.
- explicit value must be positive.
- explicit value must be less than or equal to configured maximum.
- effective timeout equals the requested timeout: the explicit value when
  provided, otherwise `default_shell_timeout_seconds`.
- explicit value greater than maximum is denied/failed with normalized
  `tool_error/tool_schema_invalid`.
- runtime must not silently cap explicit value to the default.
- runtime must not silently cap explicit value to the configured maximum.

Approval grant scope signatures that include shell timeout must use this Phase
3 effective-timeout calculation. A grant created under the old Phase 1
silent-cap behavior is legacy runtime truth and is not interpreted after the
Phase 3 schema reset.

## Timeout Result

When a shell command exceeds the effective timeout:

- ToolResult status is timeout according to ToolBroker conventions.
- normalized error uses `tool_error/tool_execution_timeout`.
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

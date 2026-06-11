# Phase 3.5 Configuration And Constants Specification

## Boundary

Phase 3.5 centralizes runtime constants by module and adds two runtime
configuration fields. It does not introduce project-local config, config/model
hot reload, provider expansion, policy migration into `config.toml`, or a
general configuration control plane.

`~/.debug-agent/config.toml` remains operational runtime configuration.
`~/.debug-agent/agent.toml` remains the path and shell policy declaration file.
Path policy and shell policy must not move into `config.toml`.

Project-local `config.toml` files are not read in Phase 3.5.

## Settings Modules

Phase 3.5 uses four directory-level settings modules as centralized constant
sources:

| Module | Content |
| --- | --- |
| `src/debug_agent/runtime/settings.py` | main model defaults, context defaults, execution defaults, development defaults, agent loop defaults, retry registry data, token estimator constants, policy safety baselines, provider execution constants, platform constants, prompt defaults, and runtime ordering constants. |
| `src/debug_agent/tools/settings.py` | native tool pagination defaults and hard maximums, ToolBroker internal limits, `view_image` defaults and fixed image/request limits. |
| `src/debug_agent/cli/settings.py` | REPL/TUI presentation, flush, scroll, and preview constants. |
| `src/debug_agent/persistence/settings.py` | SQLite schema version, legacy schema versions, checkpoint manifest version, and inline/artifact thresholds. |

Settings modules use uppercase module constants. Constants that are easy to
confuse across domains must use domain prefixes, such as
`DEFAULT_AGENT_LOOP_MAX_TOOL_CALL_ITERATIONS` and
`DEFAULT_GENERIC_TOOL_TIMEOUT_SECONDS`.

Each moved or newly centralized constant must have a short adjacent code comment
that explains what the constant controls, its boundary, or its contract source.
Comments must not merely repeat the constant name.

Centralizing a constant does not make it configurable. Only fields explicitly
listed in this spec or earlier phase config specs may be overridden through
`config.toml`.

## New Runtime Config Fields

Phase 3.5 adds:

```toml
[agent_loop]
max_tool_call_iterations = 1000

[execution]
default_tool_timeout_seconds = 30
```

| Key | Default | Validation | Meaning |
| --- | ---: | --- | --- |
| `agent_loop.max_tool_call_iterations` | `1000` | positive integer | Maximum model/tool-call loop iterations for one agent turn. |
| `execution.default_tool_timeout_seconds` | `30` | positive integer, no Phase 3.5 hard maximum | Default ToolBroker timeout for brokered tool calls that do not have a tool-specific timeout source. |

Boolean values must not be accepted as integers for these fields.

Phase 3.5 does not set an additional hard cap for either field. `0` does not
mean infinite and must be rejected. Extremely large values can make a local
agent turn run for a very long time or consume substantial local resources; this
is an explicit local-user configuration risk accepted by the person setting the
value. This absence of a Phase 3.5 hard cap is an intentional contract decision,
not an implementation gap.

Invalid values use `config_error/invalid_runtime_config`.

## Startup Ordering

Phase 3.5 does not redefine startup/config/schema ordering. It inherits the
Phase 3 startup ordering: runtime config is resolved before policy freeze,
runtime database bootstrap, startup legacy schema reset, session/run creation,
active ownership checks, stale fail-close, model calls, or tool calls.

Phase 3.5 configuration additions must be parsed by the existing
`load_config_snapshot()` path. Invalid `[agent_loop]` or `[execution]`
configuration is therefore the same startup config failure class as other
runtime config errors and must return before `.sessions/runtime.db` is opened,
deleted, reset, created, or interpreted.

Phase 3.5 must not introduce a second config parsing point later in startup,
resume, ToolBroker construction, adapter execution, or tool execution.

## Frozen Snapshot Rules

The new fields are resolved at session startup and frozen into
`sessions.config_snapshot_json`.

Resume uses the original session frozen snapshot. It must not hot-reload current
`config.toml` values.

Fresh sessions, fake-provider sessions, and real-provider sessions must use the
same snapshot shape for these fields.

Direct adapter or tool unit tests that construct lower-level objects without a
complete session snapshot may use settings defaults as non-session fallback.
Fresh session and resume runtime paths must use the frozen snapshot.

Secret values must not be written into snapshots. This spec adds no new secret
fields.

## Agent Loop Iteration Limit

`agent_loop.max_tool_call_iterations` replaces the adapter-local tool-call loop
constant as the authoritative per-session loop bound.

The adapter must read this value from the frozen session config supplied through
`AgentRunRequest.model_config`. Resume therefore preserves the original session's
loop limit.

This value is not model-visible tool availability, not a checkpoint manifest
field, and not a retry policy setting. It must not be written into terminal
recovery checkpoint tool-availability facts.

## Generic ToolBroker Default Timeout

`execution.default_tool_timeout_seconds` extends the Phase 3 `[execution]`
runtime config group.

This field follows the same Phase 3 frozen execution-config discipline as
`default_shell_timeout_seconds`, `max_shell_timeout_seconds`, and
`cancellation_timeout_seconds`: it is resolved at session startup, validated as
runtime config, frozen into `sessions.config_snapshot_json`, restored from the
frozen snapshot on resume, and never hot-reloaded from current `config.toml`.

This field is not a shell timeout. It must not be used as
`shell_exec.timeout_seconds`, must not cap explicit shell timeouts, and must not
change `shell_exec.timeout_seconds.maximum`, which continues to come from frozen
`execution.max_shell_timeout_seconds`. When a model omits
`shell_exec.timeout_seconds`, `shell_exec` uses frozen
`execution.default_shell_timeout_seconds`, not
`execution.default_tool_timeout_seconds`.

This field is not a provider/model timeout and must not replace
`[defaults].timeout_seconds`.

This field is not the `view_image` provider timeout. `view_image` continues to
use frozen `multimodal.timeout_seconds`.

This field does not add a model-visible timeout input to native tools or
runtime-control tools. It configures the existing ToolBroker execution envelope
for brokered calls without a more specific timeout source.

The default ToolBroker timeout starts after interactive approval has finished and
immediately before handler, traversal, provider, or command work begins. It
includes handler work, traversal, provider/command work, and ArtifactStore
registration or artifact writes caused by large tool output. It does not include
interactive approval wait time, audit emission, or final result envelope
formatting. Schema validation, policy evaluation, approval, audit, and final
result envelope formatting failures keep their own existing error handling.

It is part of the session frozen config snapshot, but not part of terminal
recovery checkpoint tool-availability facts.

## Multimodal Boundary

Phase 3.5 does not change the Phase 2/3 disabled-`view_image` startup behavior.
Missing, incomplete, invalid, or unsupported multimodal config still freezes
`view_image` as disabled with a no-secret disabled reason and does not fail
session startup by itself.

Phase 3.5 must not add these fields to `[multimodal.defaults]`:

- `max_images`
- `max_image_edge`
- `max_image_pixels`
- `max_request_bytes`

Image count, image edge, image pixel, and request body limits remain fixed
runtime constants in `tools/settings.py`.

## Unknown Config Keys

Phase 3.5 does not add a global unknown-key fail-closed rule for
`config.toml`. The loader reads and validates the fields defined by the current
phase contracts.

Unknown model-visible tool input fields remain governed by the ToolBroker schema
validation contract, not by runtime config parsing.

## Non-Configurable Constants

The following must be centralized where appropriate but must not become
`config.toml` fields in Phase 3.5:

- SQLite schema version, legacy schema versions, and checkpoint manifest version.
- CLI exit code map.
- normalized error class and reason sets.
- Phase 3.5 native tool pagination defaults and hard maximums.
- builtin path deny, skill source deny, raw shell trampoline deny, privilege
  escalation deny, and recursive rm deny baselines.
- token estimator version and estimation parameters.
- provider execution poll interval and platform API constants.
- retry rule keys, preconditions, strategies, and reason sets.
- system prompt and prompt profile.
- provider support lists and multimodal support lists.
- `view_image` image count, image edge, image pixel, and request body limits.

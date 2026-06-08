# Phase 3 Cancellation Spec

## Purpose

This spec defines running turn cancellation, provider cancellation, shell
best-effort termination, `cancelling` presentation, and process interrupt
behavior.

## Running Cancellation Boundary

Running cancellation starts when user sends `Ctrl+C` or `Esc` during an active
turn. `Esc` is a user-facing equivalent for the same state-dependent
interruption semantics; it does not introduce a separate cancellation reason,
exit code, or persistence shape.

Runtime must reduce the interruption to a durable cancellation/failure fact
before returning the session to ordinary input. Until then, local control state
is `cancelling`.

Durable cancellation fact:

- `error_class = "cancelled"`
- `reason = "user_cancel_running"`
- `scope = "turn"`
- model-visible projection may be appended to durable conversation only after
  the cancellation fact is accepted.

The cancellation cleanup envelope is configured by frozen execution setting
`cancellation_timeout_seconds` under `[execution]` in
`~/.debug-agent/config.toml`, defaulting to `10`. It must be a positive integer.
Invalid configuration is a startup config failure using
`config_error/invalid_runtime_config`. This setting controls local cleanup after
runtime accepts an interrupt; it is not a provider call timeout, shell execution
timeout, remote cancellation guarantee, or model-visible tool input.

## Provider Cancellation

Phase 3 includes main model provider calls and `view_image` provider calls in
best-effort provider cancellation.

Phase 3 preserves the public main-model adapter contract:

- `AgentLoopAdapter.run()` remains authoritative result path.
- `AgentLoopAdapter.stream()` remains UI observation path.
- no public async adapter method is required for Phase 3.

Older phase documents and code may contain an `AgentLoopAdapter.cancel(run_id)`
placeholder reserved for future control paths. Current code may implement this
as adapter-local cancelled-run state checked before a later `run()` or
`stream()` call. That is not a real in-flight provider cancellation boundary.
Phase 3 must remove this placeholder public API from the adapter protocol and
concrete adapter implementation. Provider cancellation must be driven by
runtime-owned cancellation handles attached to the async provider task used
inside a `run()` or `stream()` adapter call, and must not build Phase 3
cancellation around adapter-owned `cancel(run_id)` state.

Main model adapters must internally:

- execute authoritative `run()` provider calls through a runtime-owned async
  provider path, using the configured provider's async API such as `ainvoke`
  when available.
- execute observational `stream()` provider calls through a runtime-owned async
  provider path, using the configured provider's async streaming API such as
  `astream` when available.
- register runtime-owned cancellation handles.
- observe local cancellation and return normalized cancelled results.
- surface provider stop/finish metadata.

The public adapter API remains synchronous for Phase 3: `run()` and `stream()`
may block their caller while internally driving and collecting async provider
tasks. Phase 3 does not add a public async adapter method.

`view_image` provider calls must use an async vision provider execution path
and register a runtime-owned cancellation handle with ToolBroker/runtime
control. Main model and `view_image` provider calls should share a common
runtime-owned async provider cancellation primitive where practical so local
boundary collection, cancellation metadata, late-result ignoring, and cleanup
timeout behavior stay consistent.

For concrete provider integrations with available async provider APIs, wrapping
sync `invoke()` or sync `stream()` in a worker is not an accepted Phase 3
provider fallback for either ordinary main-model invocation or streaming output.
Phase 3 does not accept a sync-only provider execution path as an
implementation fallback once the configured provider exposes a usable async
call/stream API.

Implementation planning must audit the concrete main-model adapter and
`view_image` provider path before coding cancellation. If either concrete path
can only execute as a sync-only uncancellable provider call, Phase 3
implementation must stop for a contract or provider-path decision instead of
silently weakening the cancellation contract.

The Phase 0.5 streaming observation fallback remains allowed: `stream()` may
fall back to non-streaming provider invocation for UI observation. That fallback
must still execute the underlying provider invocation inside a runtime-owned
async provider task when the configured provider exposes an async invocation
API. It must not become an uncancellable sync-only execution path.

If a provider call is cancelled:

- runtime enters/presents `cancelling`.
- runtime must not claim remote execution stopped.
- runtime must not claim provider billing stopped.
- runtime must not accept partial stream output as final assistant output.
- runtime must ignore any late provider result after the local cancellation
  boundary has been accepted.
- runtime must not append a late result to durable conversation.
- runtime must not write a late result as an accepted assistant or tool-call
  result.
- runtime must not write a late `view_image` provider result as an accepted
  `ToolResult`, vision analysis, raw provider text, or provider response.
- main model provider cancellation uses `cancelled/model_call_cancelled` as an
  internal failure-class audit fact and provider-boundary detail for the
  turn-scoped running cancellation.
- brokered `view_image` cancellation is returned to the main agent as a
  cancelled tool-call observation with `cancelled/tool_call_cancelled`;
  provider-layer cancellation details may appear only in metadata.
- uncertainty about remote stop or billing may be recorded only as metadata.

The Phase 0.5 stream-delta equality rule applies only to completed, uncancelled,
accepted stream results. A cancelled provider call does not produce an accepted
`AgentRunResult.assistant_output`, and stream tokens shown before cancellation
must remain presentation-only.

## Shell Best-Effort Termination

If `shell_exec` is active during running cancellation:

1. runtime asks CommandRunner/ToolBroker for the active subprocess handle.
2. runtime sends best-effort local termination.
3. runtime may escalate to stronger local termination according to platform
   support and existing command-runner safety rules.
4. runtime waits only within the cancellation envelope.
5. runtime records shell cancellation, timeout, or failure facts.

Shell cancellation does not resume shell mid-flight state. Partial shell output
may be included only as a normalized failed/cancelled tool observation or
artifact when the command-runner boundary has closed.

The cancellation envelope starts when the running interruption is accepted and
covers provider worker cancellation, shell termination, output collection, and
result normalization. Runtime may return REPL/TUI to input only after the
relevant local provider/tool/shell boundary has closed and a durable
cancellation/failure fact has been accepted. Phase 3 must not leave a shell
process or provider worker running as a hidden background runtime task after
returning to input.

For provider execution, local boundary closure means the runtime-owned worker or
async provider task has finished, been cancelled, or otherwise reached a
runtime-observed terminal state and has been awaited/collected by runtime
control. It does not mean the remote provider necessarily stopped execution or
billing; remote-stop and billing uncertainty remain metadata only.

If local cleanup cannot close within the cancellation envelope, runtime must
fail closed for that command path: do not accept partial state, do not write a
terminal recovery checkpoint from incomplete state, and do not return the
REPL/TUI to prompt input. The process must exit or abort the command path with
`INTERRUPTED` or another non-zero abnormal-exit code, while active ownership
remains blocked by the last durable owner facts. Later recovery may proceed only
through already durable facts and user-confirmed stale fail-close.

If running cancellation occurs after an assistant tool-call message has already
been accepted and the brokered tool has started, runtime must close that accepted
tool call before appending the turn-scoped runtime cancellation observation.
The durable conversation order is:

1. accepted `assistant_tool_call`.
2. cancelled or failed `tool_result` with the original `tool_call_id`, using the
   normalized tool cancellation/failure observation returned by ToolBroker.
3. runtime-authored `cancellation_fact` for the running interruption, using
   `cancelled/user_cancel_running` with `scope = "turn"`.

If cancellation occurs while a provider has emitted only partial tool-use stream
fragments that have not been accepted as a complete assistant tool-call message,
runtime must not append an `assistant_tool_call` or a matching `tool_result`.
Only the accepted runtime cancellation/failure fact may become durable
conversation.

If the turn's `user_input` was already accepted before cancellation and no
assistant output or complete assistant tool-call message was accepted, runtime
appends the turn-scoped runtime `cancellation_fact` after that `user_input`.
`cancelled/model_call_cancelled` remains an internal/audit provider-boundary
fact and must not be appended as a separate durable conversation message unless
a later phase explicitly changes this contract.

This ordering is the normative running-cancellation conversation order for
`session-control.md`: a brokered tool cancellation closes an already accepted
tool-call boundary first, then the runtime appends the turn-scoped
`cancelled/user_cancel_running` fact. If no complete assistant tool-call message
was accepted before cancellation, runtime must not invent a tool result.

A clean cancelled tool-call observation uses:

- `error_class = "cancelled"`
- `reason = "tool_call_cancelled"`
- `scope = "tool"`

Brokered cancelled tool observations use the existing `tool_call_failed` audit
event kind with `payload.error` set to the normalized
`cancelled/tool_call_cancelled` object. Runtime must not invent a separate
tool-specific cancellation event kind or reason for `shell_exec`, `view_image`,
or other brokered tools in Phase 3.

For `shell_exec`, metadata may include `tool_name = "shell_exec"`, termination
outcome, return code or signal when available, and partial output artifact ids.

For `view_image`, cancellation metadata inherits the Phase 2 redaction rules:
it must not contain the concrete query text, image bytes, base64 image content,
or provider image content parts.

## Tool Cancellation

Tool cancellation support in Phase 3 is limited to active shell process
best-effort termination and `view_image` provider calls running through the
runtime-owned async provider cancellation path.

Phase 3 does not add generic tool cancellation, tool-mid-flight resume, or
automatic retry of cancelled tools.

## Idle Cancellation

Idle `Ctrl+C` or `Esc` is session terminalization, not running turn
cancellation.

It writes a session-scoped cancellation fact:

- `error_class = "cancelled"`
- `reason = "user_cancel_idle"`
- `scope = "session"`

It then writes a terminal recovery checkpoint when eligible, terminalizes
session/run, releases ownership, and exits.

## Input Lockout While Cancelling

After runtime has accepted a running interruption and entered `cancelling`,
the controller must block all user input until runtime leaves `cancelling` by
reaching a recovery boundary or by failing closed. This includes ordinary
prompt text, slash commands, approval input, `Ctrl+C`, and `Esc`.

Input received during `cancelling` must not create a double-interrupt
escalation, a special process-level abort path, a second durable cancellation
fact, a queued prompt/command/approval response, or a new resume source.

Runtime continues waiting for the same cancellation cleanup envelope. If local
provider/tool/shell boundaries do not close within
`cancellation_timeout_seconds`, runtime uses the documented timeout
fail-closed behavior: do not accept partial state, do not write a terminal
recovery checkpoint from incomplete state, do not return to input, and exit or
abort the command path with `INTERRUPTED` or another non-zero abnormal-exit
code while ownership remains governed by the last durable facts.

## Process-Level Interrupt

If process-level interrupt prevents normal turn recovery:

- command exit code should be `INTERRUPTED`.
- runtime must not promote partial state to durable truth.
- later stale fail-close may use only facts already durably persisted.

## Events And Trace

Runtime must record audit facts for:

- interrupt requested.
- model/provider call cancellation requested/observed and any remote-stop
  uncertainty metadata.
- shell termination requested/result.
- turn cancellation fact.
- whether REPL returned to input or process exited interrupted.

Presentation-only cancellation details may remain UI/trace rendering state and do
not need separate audit facts.

Trace/status rendering is observational and not recovery truth.

## Cancellation Reason Mapping

| Trigger | Internal failure/cancellation fact | Model-visible durable conversation append | Notes |
|---|---|---|---|
| Running `Ctrl+C` or `Esc` for an active prompt turn | `cancelled/user_cancel_running`, `scope = "turn"` | Append one runtime `cancellation_fact` only after the turn reaches a recovery boundary. | Session/run remain `running`; ownership remains held. |
| Local main-model provider cancellation boundary closes | `cancelled/model_call_cancelled`, `scope = "provider"` | Do not append a separate assistant message. It may be metadata/source detail for the turn cancellation fact. The turn-scoped durable conversation append remains `cancelled/user_cancel_running`, `scope = "turn"`. | Late provider output is ignored. |
| Brokered `view_image` provider cancellation boundary closes after an accepted assistant tool call | `cancelled/tool_call_cancelled`, `scope = "tool"` | Append the cancelled `tool` observation with the original `tool_call_id`, then append the turn-scoped runtime `cancellation_fact`. | Provider-layer details are metadata only. |
| Active `shell_exec` is terminated by running cancellation after an accepted assistant tool call | `cancelled/tool_call_cancelled`, `scope = "tool"` | Append the cancelled `tool` observation after the command-runner boundary closes, then append the turn-scoped runtime `cancellation_fact`. | Partial shell output may be artifacted only after closure. |
| Idle `Ctrl+C` or `Esc` | `cancelled/user_cancel_idle`, `scope = "session"` | Append a runtime `cancellation_fact` when terminalization accepts it as model-visible history. | Writes terminal recovery checkpoint when eligible and releases ownership. |
| Process-level interrupt before recovery boundary | `cancelled/user_cancel_process` when persistence is possible | No append unless a recovery boundary is reached. | Must not promote partial state to durable truth. |

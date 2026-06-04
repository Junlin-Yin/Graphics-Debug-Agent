# Phase 3 Cancellation Spec

## Purpose

This spec defines running turn cancellation, provider cancellation, shell
best-effort termination, `cancelling` presentation, and process interrupt
behavior.

## Running Cancellation Boundary

Running cancellation starts when user sends `Ctrl+C` during an active turn.

Runtime must reduce the interruption to a durable cancellation/failure fact
before returning the session to ordinary input. Until then, local control state
is `cancelling`.

Durable cancellation fact:

- `error_class = "cancelled"`
- `reason = "user_cancel_running"`
- `scope = "turn"`
- model-visible projection may be appended to durable conversation only after
  the cancellation fact is accepted.

## Provider Cancellation

Phase 3 preserves the public adapter contract:

- `AgentLoopAdapter.run()` remains authoritative result path.
- `AgentLoopAdapter.stream()` remains UI observation path.
- no public async adapter method is required for Phase 3.

Adapters may internally:

- run provider calls in async tasks.
- register runtime-owned cancellation handles.
- observe local cancellation and return normalized cancelled results.
- surface provider stop/finish metadata.

If provider cancellation is unsupported, sync-only, or uncertain:

- runtime enters/presents `cancelling`.
- runtime must not claim remote execution stopped.
- runtime must not claim provider billing stopped.
- runtime must not accept partial stream output as final assistant output.
- runtime records `cancelled/provider_cancel_uncertain` or equivalent
  diagnostic metadata.

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

## Tool Cancellation

Tool cancellation support in Phase 3 is limited to active shell process
best-effort termination and provider-like calls already controlled by runtime.

Phase 3 does not add generic tool cancellation, tool-mid-flight resume, or
automatic retry of cancelled tools.

## Idle Cancellation

Idle `Ctrl+C` is session terminalization, not running turn cancellation.

It writes a session-scoped cancellation fact:

- `error_class = "cancelled"`
- `reason = "user_cancel_idle"`
- `scope = "session"`

It then writes a terminal recovery checkpoint when eligible, terminalizes
session/run, releases ownership, and exits.

## Process-Level Interrupt

If process-level interrupt prevents normal turn recovery:

- command exit code should be `INTERRUPTED`.
- runtime must not promote partial state to durable truth.
- later stale fail-close may use only facts already durably persisted.

## Double Ctrl+C

A second `Ctrl+C` while `cancelling` may request process exit or stronger local
cleanup. It must not:

- mark pending provider/tool/shell state accepted.
- write a terminal recovery checkpoint from incomplete state.
- release ownership without terminalization or later user-confirmed stale
  fail-close.

## Events And Trace

Runtime should record:

- interrupt requested.
- provider cancellation requested/observed/uncertain.
- shell termination requested/result.
- turn cancellation fact.
- whether REPL returned to input or process exited interrupted.

Trace/status rendering is observational and not recovery truth.

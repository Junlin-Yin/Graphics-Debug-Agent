# Phase 3 Session Control Spec

## Purpose

This spec defines running interruption, idle terminalization, explicit resume
revival, active ownership release, and one-shot/REPL lifecycle unification.

## Lifecycle And Control State

Phase 3 keeps lifecycle status separate from transient control state.

Lifecycle status is persisted session/run state such as `running`, `completed`,
or `failed` according to existing store conventions. Terminal lifecycle statuses
remain terminal except for the explicit `debug-agent resume <session_id>` path.

Control state is runtime/controller state such as:

- `idle`
- `running_turn`
- `cancelling`
- `terminalizing`
- `resuming`

Control state may be emitted in events, trace, or UI/status presentation, but
it is not a replacement for lifecycle status.

## Running Turn Interruption

Running turn interruption is turn-scoped.

When user sends `Ctrl+C` while a prompt turn is running:

1. controller sends interrupt to runtime control path.
2. runtime enters `cancelling` control state for the turn.
3. runtime requests provider cancellation if a provider call is active.
4. runtime requests shell best-effort termination if `shell_exec` is active.
5. runtime rejects partial model output and pending tool/shell result as
   durable truth.
6. runtime writes a normalized cancellation/failure fact after reaching a
   recovery boundary.
7. REPL/TUI returns to input.
8. session/run lifecycle remains `running`.
9. active workspace ownership remains held.

Running interruption must not:

- write a terminal recovery checkpoint.
- terminalize session/run.
- release active ownership.
- accept partial assistant output.
- execute incomplete tool calls.
- mark shell or tool mid-flight state resumable.

The durable cancellation fact uses:

- `error_class = "cancelled"`
- `reason = "user_cancel_running"`
- `scope = "turn"`

If provider cancellation is uncertain, runtime records
`cancelled/provider_cancel_uncertain` or equivalent metadata and presents
`cancelling` until the local control path reaches a safe boundary.

## Idle Terminalization

Idle terminalization applies when the prompt session is not executing an active
turn.

Triggers:

- idle `Ctrl+C`.
- `/exit`.
- normal graceful REPL shutdown.

Required behavior:

1. verify no active turn/tool/shell state is being treated as resumable truth.
2. write terminal cancellation/completion fact.
3. write terminal recovery checkpoint if session/run are eligible.
4. terminalize session/run lifecycle.
5. release active workspace ownership.
6. exit the controller.

Idle `Ctrl+C` uses:

- `error_class = "cancelled"`
- `reason = "user_cancel_idle"`
- `scope = "session"`

Idle terminalization may produce a resumable session only when terminal
checkpoint creation succeeds and eligibility holds.

## Terminal Failures

A terminal failure after accepted prompt runtime facts exist may write a
terminal recovery checkpoint when:

- session/run are prompt lineage.
- failure is not startup/config/schema failure.
- durable conversation cut validates.
- active runtime-owned state can be captured or referenced.
- checkpoint write and terminal transition can be made consistent.

Terminal failure does not make mid-flight provider/tool/shell state resumable.

## Startup Failure Sessions

Startup/config/schema failure sessions are non-resumable.

If session/run were created before the failure:

- terminalize them.
- write normalized audit events/facts where possible.
- release ownership if acquired.
- do not write a terminal recovery checkpoint.

## Explicit Resume Revival

Only `debug-agent resume <session_id>` may revive terminalized session/run
lineage.

Resume success:

- preserves `session_id`.
- preserves `run_id`.
- preserves previous terminal facts and terminal checkpoint.
- writes resume audit events.
- reacquires active ownership.
- transitions lifecycle status to `running`.
- starts REPL with restored runtime context.

Resume must not:

- create a successor session.
- create a new prompt run.
- append a model-visible resume observation.
- attach to a running or idle non-terminal session.
- resume startup/config/schema failure.
- resume without a valid terminal recovery checkpoint.

## One-Shot And REPL Lifecycle Unification

One-shot prompt execution and REPL prompt execution must share the same runtime
turn lifecycle.

Shared behavior:

- session/run creation.
- durable conversation append.
- ToolBroker path.
- approval policy and grants.
- Todo Plan persistence.
- skill/context injection.
- normalized errors.
- terminal recovery checkpoint creation.
- resume eligibility.

Controller-specific behavior:

- one-shot owns input/output binding and process exit-code mapping.
- one-shot non-interactive approval unavailable behavior fails closed according
  to the same approval policy.
- REPL owns interactive loop, TUI/plain input, slash command handling, and idle
  control actions.

One-shot terminal prompt sessions can resume into REPL only after they produce
the same terminal recovery checkpoint shape as REPL prompt sessions.

## Active Ownership Release

Runtime releases active workspace ownership only after:

- eligible terminalization completes.
- terminal checkpoint requirements have succeeded when resumability is expected.
- cancellation/failure facts needed for audit have been persisted where
  persistence is available.

Running turn cancellation does not release ownership.

Abnormal process death may leave ownership stale. A later process may handle it
only through user-confirmed stale fail-close.

## Double Interrupt

If the user sends a second interrupt while runtime is already `cancelling`:

- runtime should escalate local shutdown behavior only enough to return control
  or exit safely.
- runtime must not write partial provider/tool/shell state as durable truth.
- if process-level interruption exits the command, use `INTERRUPTED` exit code.
- later stale fail-close can only use already durable facts.

Double interrupt does not create a special resume source.

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
- print a session close/cancelled terminal summary.
- accept partial assistant output.
- execute incomplete tool calls.
- mark shell or tool mid-flight state resumable.

The durable cancellation fact uses:

- `error_class = "cancelled"`
- `reason = "user_cancel_running"`
- `scope = "turn"`

Provider calls must run inside runtime-owned cancellable workers. Main model
provider cancellation records `cancelled/model_call_cancelled` as an internal
failure-class audit fact when the local cancellation boundary closes. It is not
appended as a separate durable conversation `cancellation_fact` during running
`Ctrl+C`; the model-visible durable conversation fact for the turn remains
`cancelled/user_cancel_running`. Brokered `view_image` cancellation returns
`cancelled/tool_call_cancelled` as the model-visible tool observation, with
provider-layer cancellation details only in metadata. Late provider results are
ignored and must not become durable conversation or accepted assistant/tool-call
output. Runtime may record remote-stop or billing uncertainty only as metadata
and must not claim the remote provider stopped.

These provider/tool cancellation reasons are boundary facts for the active
model or tool operation. They do not replace the turn-scoped running-interrupt
fact. When running `Ctrl+C` cancels an active turn, runtime records
`cancelled/user_cancel_running` at the turn recovery boundary; if an already
accepted tool call was in flight, the cancelled tool observation is appended
first with its original `tool_call_id`, followed by the turn-scoped runtime
cancellation fact. If no complete assistant tool-call message had been accepted,
runtime must not invent a tool result. When the current turn's user input has
already been accepted but no assistant output or complete assistant tool-call
message has been accepted, the durable conversation order is the accepted
`user_input` followed by the runtime-authored `cancellation_fact` for
`cancelled/user_cancel_running`.

## Idle Terminalization

Idle terminalization applies when the prompt session is not executing an active
turn.

Triggers:

- idle `Ctrl+C`.
- `/exit`.
- normal graceful REPL shutdown.

Required behavior:

1. verify no active turn/tool/shell state is being treated as resumable truth.
2. prepare the terminal fact, durable conversation cut, projection snapshot, and
   runtime-owned state references.
3. write terminal recovery checkpoint if session/run are eligible.
4. terminalize session/run lifecycle and update `latest_checkpoint_id` in the
   same resume-eligibility consistency boundary as checkpoint creation.
5. release active workspace ownership after terminal state is consistent.
6. exit the controller.

Idle `Ctrl+C` uses:

- `error_class = "cancelled"`
- `reason = "user_cancel_idle"`
- `scope = "session"`

Idle terminalization may produce a resumable session only when terminal
checkpoint creation succeeds and eligibility holds.

Idle `Ctrl+C` terminalization must carry the session-scoped
`cancelled/user_cancel_idle` fact into the terminal recovery manifest as the
terminal cancellation fact.

Other idle terminalization reasons:

- `/exit` and normal graceful REPL shutdown use terminal reason `user_exit`.
- one-shot prompt normal completion uses terminal reason
  `terminal_completion`, terminal status `completed`, and no terminal error.
- terminal prompt failure uses terminal reason `terminal_failure`.
- user-confirmed stale fail-close uses terminal reason `terminal_stale`.

## Terminal Failures

A terminal failure after a closed accepted durable conversation cut exists may
write a terminal recovery checkpoint when:

- session/run are prompt lineage.
- failure is not startup/config/schema failure.
- durable conversation fact cut and current projection state validate.
- active runtime-owned state can be captured or referenced.
- checkpoint write and terminal transition can be made consistent.

Terminal failure does not make mid-flight provider/tool/shell state resumable.

A closed accepted durable conversation cut means at least one accepted
`conversation_messages` group for the prompt run has reached a closed
acceptance boundary and the current projection state validates against that
cut. A startup/config/schema failure or any prompt failure before the first
accepted durable conversation group is non-resumable and must not write a
terminal recovery checkpoint.

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
- transitions lifecycle status to `running` after ownership is reacquired.
- records current owner `pid`, `host_id`, and fresh `owner_token`.
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
- the release is fenced by the current owner's `owner_token`.

Running turn cancellation does not release ownership.

If ownership release fails after terminalization is durable, runtime must not
roll back terminal session/run facts or terminal recovery checkpoints. It must
record `runtime_error/ownership_release_failed` where persistence is available,
leave active ownership blocked, and surface guidance that a later process may
proceed only through user-confirmed stale fail-close or manual cleanup. An
`owner_token` mismatch is an ownership release failure and must not release the
new current owner.

Abnormal process death may leave ownership stale. A later process may handle it
only through user-confirmed stale fail-close.

Active shell process handles used for cancellation are runtime-internal state
created only after a `shell_exec` call has passed ToolBroker policy, approval,
timeout, and audit setup and the command has started. The shell start audit
fact and active process-handle registration must occur in the same command-start
boundary so a started command is visible to cancellation control. They are not
model-visible tool inputs or reusable execution handles.

## Double Interrupt

If the user sends a second interrupt while runtime is already `cancelling`:

- runtime treats it as a process-level interruption request.
- runtime must stop waiting for ordinary REPL recovery, attempt only the
  minimum local cleanup needed to avoid accepting partial state, and exit or
  abort the command path with `INTERRUPTED`.
- runtime must not return REPL/TUI to prompt input from the same cancelling
  state.
- runtime must not write partial provider/tool/shell state as durable truth.
- later stale fail-close can only use already durable facts.

Double interrupt does not create a special resume source.

## TTY Terminal Summary

Phase 0.5 TTY summary behavior is narrowed by Phase 3 session control.
Running-turn `Ctrl+C` returns the REPL/TUI to input after the cancellation
boundary closes and must not print the post-TUI session close or cancelled
summary. Idle `Ctrl+C`, `/exit`, normal graceful shutdown, and process-level
interruption that exits the command may print the appropriate terminal summary
after the TUI leaves the alternate screen.

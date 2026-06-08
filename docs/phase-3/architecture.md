# Phase 3 Architecture

## Module List

### CLI Entrypoint

The CLI adds `debug-agent resume <session_id>` as the only explicit terminal
session revival path.

Responsibilities:

- validate command shape and map usage errors to semantic exit codes.
- initialize persistence with Phase 3 schema-version fail-closed behavior before
  interpreting runtime truth.
- route `resume` through the Runtime Orchestrator, not directly through stores.
- preserve existing `status` and `trace` as observation commands.
- present active ownership conflicts and stale fail-close prompts.
- fail closed when stale evidence is insufficient or user confirmation is not
  available.

`status` and `trace` remain read-only. They must not repair, migrate, delete,
revive, or terminalize sessions.

### REPL And TUI Controllers

REPL/TUI own human interaction and control signal presentation. They do not own
runtime truth.

Responsibilities:

- distinguish running turn `Ctrl+C`/`Esc` from idle `Ctrl+C`/`Esc`.
- send running interruption to the runtime control path.
- terminalize eligible idle sessions through the orchestrator.
- present `cancelling` when provider cancellation is best-effort or uncertain.
- return to prompt input after turn-scoped cancellation reaches a recovery
  boundary.
- exit after idle terminalization has written durable terminal facts and
  released active ownership.

TUI stream blocks, token deltas, scroll position, input drafts, and transient
error displays are not durable truth and are not resume inputs.

### Runtime Orchestrator

The Runtime Orchestrator owns session/run lifecycle, active ownership,
terminalization, resume, and stale fail-close coordination.

Responsibilities:

- create prompt sessions/runs with Phase 3 schema compatibility checks.
- drive one-shot and REPL prompt execution through the same turn lifecycle.
- terminalize sessions/runs using durable facts and terminal checkpoints.
- enforce that only `resume` can revive terminalized prompt sessions/runs.
- keep same `session_id` and `run_id` during eligible resume.
- reacquire active workspace ownership before revival.
- generate and persist a fresh `owner_token` whenever active ownership is
  claimed or reclaimed.
- reject resume when eligibility, schema, checkpoint, checksum, durable
  conversation fact cut, checkpoint-frozen projection snapshot, or ownership
  validation fails.
- write `session_resumed` and `run_resumed` audit events after successful
  revival.
- coordinate user-confirmed stale fail-close only before creating or resuming
  the current session.

The orchestrator must not auto-attach to a stale session or silently release
active ownership.

### Prompt Agent Runtime

Prompt Agent Runtime owns prompt turn execution, durable conversation append,
model/tool loop boundaries, cancellation facts, and model-visible context
projection.

Responsibilities:

- append accepted model-visible user, assistant, tool, failure, and cancellation
  message groups to `conversation_messages`.
- maintain process-local conversation and persisted projection state together
  during ordinary execution.
- rebuild process-local conversation from durable messages only during explicit
  resume.
- reject pending provider/tool/shell state as durable conversation truth.
- construct `ModelContextFrame` from the current conversation projection, active
  skills, compression state, Todo Plan, approval state, and current input.
- emit failure/cancellation facts at recovery boundaries.
- produce terminal checkpoint input from durable facts during terminalization.

Resume does not append a model-visible observation by itself. After resume, the
next ordinary model call sees restored runtime context, not a synthetic
assistant or tool message saying that resume occurred.

### Provider Execution

Main model calls and `view_image` provider calls must execute through a shared
runtime-owned async provider primitive where practical.

Responsibilities:

- preserve the public `AgentLoopAdapter.run()` / `stream()` contract for main
  model calls.
- keep `run()` as the authoritative main model result path.
- keep `stream()` as UI observation path.
- execute main model calls through runtime-owned async provider tasks without
  adding a public async adapter method: `run()` drives the provider async
  invocation path such as `ainvoke`, and `stream()` drives the provider async
  streaming path such as `astream`, when those APIs are available for the
  configured provider.
- remove the older placeholder `AgentLoopAdapter.cancel(run_id)` public API from
  the adapter protocol and concrete adapter implementation. That API was a
  Phase 0/0.5 future-control placeholder, not a real provider-cancellation
  boundary.
- keep cancellation handles owned by the runtime async provider task wrapping
  the internal provider boundary for `run()` or `stream()`.
- execute `view_image` provider calls through the same class of runtime-owned
  async provider boundary using the async vision provider path.
- return normalized cancellation/failure results when local cancellation is
  observed.
- expose enough provider stop/finish metadata for
  `output_token_limit_reached` detection.

Implementation planning must begin this area with a concrete adapter/provider
capability audit. If the main-model adapter or `view_image` provider path can
only run through a sync-only uncancellable call, implementation must stop for a
contract or provider-path decision instead of weakening the Phase 3
cancellation contract.

Adapters must not make stream observations runtime truth. The existing
streaming observation fallback to non-streaming provider invocation may remain,
but the underlying provider invocation must still run through a runtime-owned
async provider task when the configured provider exposes a usable async
invocation API. Sync-only `invoke()` / `stream()` wrapped in a worker is not an
accepted Phase 3 fallback for concrete main-model providers once async provider
APIs are available. After local cancellation is accepted, late provider results
are ignored and must not become durable conversation, accepted assistant output,
accepted tool-call output, or accepted `view_image` tool result. Runtime must
not claim the remote provider stopped execution or billing.

The one-shot/non-stream REPL path and streaming REPL/TUI path share the same
adapter/executor provider boundary. Both paths therefore inherit the same async
provider cancellation behavior even though the public adapter methods remain
synchronous.

### ToolBroker

`ToolBroker` remains the only execution boundary for model-visible tools.

Phase 3 responsibilities:

- normalize tool schema, policy, approval, timeout, cancellation, and handler
  failures through the Phase 3 error taxonomy.
- pass active shell process handles to runtime cancellation control when a
  `shell_exec` call is in flight.
- classify shell timeout and shell cancellation with fixed reasons.
- avoid default runtime-level retry for ordinary tool calls.
- keep tool mid-flight state out of resume truth.
- write failure-class events with `payload.error`.

Tool handlers must not implement local ad hoc retry policy unless registered as
an explicit runtime-owned retry-safe path.

### RetryController

`RetryController` is a runtime-owned policy component, not a workflow engine.

Responsibilities:

- maintain the central retry rule registry.
- decide whether a normalized error is retryable.
- apply only Phase 3 strategies: `repeat_call` and `continue_generation`.
- record retry attempts and exhaustion metadata as audit facts.
- return final failures to ordinary error handling after retry is disabled or
  exhausted.

RetryController does not own terminalization, ownership release, checkpoint
writing, tool replay, or accepted result replay.

### SessionStore And RunStore

Session/run stores persist lifecycle state, active ownership facts, terminal
facts, and explicit resume transitions.

Responsibilities:

- keep terminal facts and terminal checkpoint references immutable.
- allow terminal-to-running lifecycle transition only when called by the
  explicit resume orchestration path.
- reject all other terminal-to-running transitions.
- record terminal reason and normalized terminal error/cancellation facts, except
  for administrative stale fail-close where the terminal reason is
  `terminal_stale` and no normalized terminal error is written.
- support active ownership release on eligible terminalization.
- persist active ownership `pid`, `host_id`, and `owner_token`.
- release or stale fail-close ownership only through owner-token fenced
  conditional transitions.

Phase 3 does not require a new lifecycle status for `idle` or `cancelling`.
Those are runtime control/presentation states. Durable lifecycle status remains
separate from transient control state.

### EventStore

EventStore owns audit facts.

Phase 3 failure-class events must place the normalized internal error object at
`payload.error`. Event kinds remain audit taxonomy. Error taxonomy lives inside
the error object.

EventStore is not event sourcing. Resume must not reconstruct runtime state by
replaying arbitrary events.

### ConversationStore

ConversationStore is new in Phase 3 and persists append-only
`conversation_messages` plus current mutable conversation projection state.

Responsibilities:

- append accepted message groups in deterministic order.
- store durable message role, kind, content/reference, metadata allowlist, and
  acceptance boundary facts.
- expose verified fact cuts by high-watermark, message count, and checksum.
- maintain one mutable current projection state per prompt run, updated by
  message append, omission, and compression.
- provide checkpoint creation with a compact frozen projection snapshot copied
  from the current projection state.
- reject pending, speculative, partial, or stream-only messages.
- provide projection reads for Prompt Agent Runtime and terminal checkpoint
  validation.

Large message content may be stored through ArtifactStore references according
to existing artifact rules, but the durable conversation row must carry the
reference and checksum facts needed to validate the cut.

### CheckpointStore

CheckpointStore remains recovery snapshot storage, but Phase 3 narrows resume
eligibility to terminal recovery checkpoints.

Responsibilities:

- write terminal recovery checkpoints during eligible terminalization only.
- reject attempts to write ordinary turn, context, error, streaming, UI, trace,
  or other non-terminal checkpoint/provenance records for Phase 3 prompt
  sessions/runs.
- store compact recovery manifests, not unbounded full conversation history.
- verify manifest schema, terminal kind, conversation fact cut,
  checkpoint-frozen projection snapshot, checksums, Todo Plan
  reference/snapshot, approval state, active skill runtime records and snapshot
  references, frozen config/policy references, artifact references, and
  terminal facts.
- ensure `latest_checkpoint_id` points only to terminal recovery checkpoints.

Non-terminal provenance records are not written for Phase 3 prompt sessions/runs
and must not be accepted by resume if encountered in a corrupt or manually
modified database.

### ArtifactStore

ArtifactStore keeps its existing role for large outputs and external artifacts.

Phase 3 uses artifact ids in normalized errors and durable conversation rows
only when content exceeds inline limits or already belongs in artifact storage.
Artifacts may be referenced by terminal recovery checkpoint manifests, but
artifact metadata remains a reference source, not an event replay source.

### TraceWriter And Status Queries

Trace and status render Phase 3 facts for humans.

They should show:

- normalized error class/reason and concise message.
- terminal checkpoint id and checkpoint eligibility.
- resume attempts and outcomes.
- cancellation facts.
- retry attempts and exhaustion.
- durable conversation high-watermark summaries.
- stale fail-close terminalization outcome.

They must not:

- repair schema or runtime state.
- revive terminal sessions.
- infer resumability from events alone.
- expose internal error metadata that is excluded from model-visible
  projection.

## Data Flow

### Accepted Turn

1. Controller receives user input.
2. Prompt Agent Runtime accepts the user message, appends it to
   `conversation_messages`, and updates the persisted projection state in the
   same append consistency boundary.
3. Runtime builds `ModelContextFrame` from durable conversation projection and
   runtime-owned state.
4. Adapter returns authoritative assistant/tool-call result through `run()`.
5. Tool calls execute through ToolBroker.
6. Accepted assistant/tool/failure messages append to durable conversation at
   recovery boundaries.
7. Events and trace facts are written for audit.

### Running Turn Interruption

1. User sends `Ctrl+C` or `Esc` while a turn is running.
2. Controller signals runtime interruption.
3. Runtime marks the current turn as cancelling.
4. Runtime requests model/provider call cancellation through the runtime-owned
   cancellable worker and active shell process termination where applicable.
5. Runtime rejects partial provider output and pending tool/shell results as
   durable truth.
6. Runtime writes a turn-scoped cancellation/failure fact when it reaches a
   recovery boundary.
7. REPL/TUI returns to input. Session/run lifecycle remains running.

### Idle Terminalization

1. User sends idle `Ctrl+C`, idle `Esc`, `/exit`, or normal shutdown.
2. Orchestrator verifies there is no active turn/tool/shell mid-flight state
   being treated as resumable truth.
3. Prompt Agent Runtime supplies durable conversation fact cut, current
   projection state, and runtime-owned state references.
4. Runtime commits terminal recovery checkpoint creation, terminal session/run
   facts, and `latest_checkpoint_id` update in one resume-eligibility
   consistency boundary.
5. Active ownership is released only after that terminal state is consistent.
6. Controller exits.

### Resume

1. User runs `debug-agent resume <session_id>`.
2. CLI validates Phase 3 schema before reading runtime truth.
3. Orchestrator checks that session/run are prompt lineage and not
   startup/config/schema failure. The ordinary path requires terminalized
   session/run rows; the only non-terminal exception is when the explicit resume
   target is itself the current proven-stale active owner and the user confirms
   fail-close before ordinary resume validation continues.
4. Orchestrator verifies terminal checkpoint kind, schema version, durable
   conversation fact cut, checkpoint-frozen projection snapshot, checksums, and
   referenced runtime-owned state.
5. Orchestrator checks active ownership and runs user-confirmed stale
   fail-close when a proven-stale owner blocks resume.
6. Orchestrator reacquires active workspace ownership.
7. Store transition revives the same session/run lineage to `running` and
   records current owner `pid`, `host_id`, and fresh `owner_token`.
8. Resume events are written.
9. REPL starts with runtime context restored from durable truth.

### Stale Fail-Close

1. Startup or resume finds active ownership blockage.
2. Runtime evaluates proven-stale evidence for the owner using recorded
   `host_id`, `pid`, and captured `owner_token`.
3. If evidence is insufficient or the owner appears alive, startup/resume fails
   closed with active ownership conflict.
4. If evidence proves stale and an interactive confirmation is available,
   runtime asks the user.
5. On confirmation, runtime writes a terminal checkpoint only when durable facts
   are sufficient and the stale session/run is checkpoint-eligible, then marks
   the stale session/run `failed` with terminal reason `terminal_stale`, writes
   one minimal administrative `stale_fail_closed` run event, and releases
   ownership in one owner-token fenced SQLite transaction over the
   authoritative ownership row.
6. The original startup or resume then proceeds. Startup may only continue by
   creating the new startup session. Explicit `debug-agent resume <session_id>`
   may continue ordinary resume validation for the same stale target only when
   that target was the explicit resume argument and fail-close produced a valid
   terminal recovery checkpoint.

Stale fail-close cannot attach to, auto-resume, or continue the stale session
outside the explicit resume-target exception above.
It must not write a normalized error fact or durable conversation
failure/cancellation fact for the stale session.

## Schema Impact

Phase 3 requires a SQLite schema-version bump because it changes:

- durable conversation truth.
- checkpoint kind and `latest_checkpoint_id` semantics.
- normalized error payload shape.
- retry metadata.
- terminal/resume audit events.
- explicit same-lineage terminal-to-running resume transition.
- shell timeout contract.
- fresh Phase 3 databases omit legacy ordinary checkpoint and context snapshot
  schema used by earlier phases.

Detailed table and payload fields are specified in `specs/`.

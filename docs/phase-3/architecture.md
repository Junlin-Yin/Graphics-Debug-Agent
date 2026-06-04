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

`status` and `trace` remain read-only. They must not repair, migrate, revive, or
terminalize sessions.

### REPL And TUI Controllers

REPL/TUI own human interaction and control signal presentation. They do not own
runtime truth.

Responsibilities:

- distinguish running turn `Ctrl+C` from idle `Ctrl+C`.
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
- reject resume when eligibility, ownership, schema, checkpoint, checksum, or
  durable conversation cut validation fails.
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
- build process-local conversation from durable messages plus runtime-owned
  injections.
- reject pending provider/tool/shell state as durable conversation truth.
- construct `ModelContextFrame` from durable conversation, active skills,
  compression state, Todo Plan, approval state, and current input.
- emit failure/cancellation facts at recovery boundaries.
- produce terminal checkpoint input from durable facts during terminalization.

Resume does not append a model-visible observation by itself. After resume, the
next ordinary model call sees restored runtime context, not a synthetic
assistant or tool message saying that resume occurred.

### AgentLoopAdapter

The public `AgentLoopAdapter.run()` and `AgentLoopAdapter.stream()` contract is
preserved.

Responsibilities:

- keep `run()` as the authoritative model result path.
- keep `stream()` as UI observation path.
- optionally use internal async provider tasks and runtime-owned cancellation
  handles for best-effort cancellation.
- return normalized cancellation/failure results when local cancellation is
  observed.
- expose enough provider stop/finish metadata for
  `output_token_limit_reached` detection.

Adapters must not make stream observations runtime truth. Sync fallback or
uncertain provider cancellation reports `cancelling` control state and must not
claim the remote provider stopped execution or billing.

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
- record terminal reason and normalized terminal error/cancellation facts.
- support active ownership release on eligible terminalization.

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
`conversation_messages`.

Responsibilities:

- append accepted message groups in deterministic order.
- store durable message role, kind, content/reference, metadata allowlist, and
  acceptance boundary facts.
- expose verified cuts by high-watermark, message count, and checksum.
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
- store compact recovery manifests, not unbounded full conversation history.
- verify manifest schema, terminal kind, conversation cut, checksums, Todo Plan
  reference/snapshot, approval state, active skill snapshot references, frozen
  config/policy references, artifact references, and terminal facts.
- ensure `latest_checkpoint_id` points only to terminal recovery checkpoints.

Non-terminal provenance records must not update `latest_checkpoint_id` and must
not be accepted by resume.

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
- stale fail-close decisions and confirmation outcomes.

They must not:

- repair schema or runtime state.
- revive terminal sessions.
- infer resumability from events alone.
- expose internal error metadata that is excluded from model-visible
  projection.

## Data Flow

### Accepted Turn

1. Controller receives user input.
2. Prompt Agent Runtime accepts the user message and appends it to
   `conversation_messages`.
3. Runtime builds `ModelContextFrame` from durable conversation projection and
   runtime-owned state.
4. Adapter returns authoritative assistant/tool-call result through `run()`.
5. Tool calls execute through ToolBroker.
6. Accepted assistant/tool/failure messages append to durable conversation at
   recovery boundaries.
7. Events and trace facts are written for audit.

### Running Turn Interruption

1. User sends `Ctrl+C` while a turn is running.
2. Controller signals runtime interruption.
3. Runtime marks the current turn as cancelling.
4. Runtime requests best-effort provider cancellation and active shell process
   termination where applicable.
5. Runtime rejects partial provider output and pending tool/shell results as
   durable truth.
6. Runtime writes a turn-scoped cancellation/failure fact when it reaches a
   recovery boundary.
7. REPL/TUI returns to input. Session/run lifecycle remains running.

### Idle Terminalization

1. User sends idle `Ctrl+C`, `/exit`, or normal shutdown.
2. Orchestrator verifies there is no active turn/tool/shell mid-flight state
   being treated as resumable truth.
3. Prompt Agent Runtime supplies durable conversation cut and runtime-owned
   state references.
4. CheckpointStore writes a terminal recovery checkpoint manifest.
5. SessionStore/RunStore write terminal facts and update
   `latest_checkpoint_id`.
6. Active ownership is released.
7. Controller exits.

### Resume

1. User runs `debug-agent resume <session_id>`.
2. CLI validates Phase 3 schema before reading runtime truth.
3. Orchestrator checks that session/run are terminalized prompt lineage and not
   startup/config/schema failure.
4. Orchestrator verifies terminal checkpoint kind, schema version, durable
   conversation cut, checksums, and referenced runtime-owned state.
5. Orchestrator reacquires active workspace ownership.
6. Store transition revives the same session/run lineage to `running`.
7. Resume events are written.
8. REPL starts with runtime context restored from durable truth.

### Stale Fail-Close

1. Startup or resume finds active ownership blockage.
2. Runtime evaluates proven-stale evidence for the owner.
3. If evidence is insufficient or the owner appears alive, startup/resume fails
   closed with active ownership conflict.
4. If evidence proves stale and an interactive confirmation is available,
   runtime asks the user.
5. On confirmation, runtime best-effort constructs a terminal checkpoint from
   durable facts, terminalizes the stale session/run, and releases ownership.
6. The original startup or resume then proceeds.

Stale fail-close cannot attach to, resume, or continue the stale session.

## Schema Impact

Phase 3 requires a SQLite schema-version bump because it changes:

- durable conversation truth.
- checkpoint kind and `latest_checkpoint_id` semantics.
- normalized error payload shape.
- retry metadata.
- terminal/resume audit events.
- explicit same-lineage terminal-to-running resume transition.
- shell timeout contract.

Detailed table and payload fields are specified in `specs/`.

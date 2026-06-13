# ADR 0014: Terminal Recovery Checkpoints And Durable Conversation

## Status

Accepted for Phase 3 planning.

## Context

Phase 0 introduced SQLite-backed events and checkpoints so sessions can be
audited and later recovered from structured state. Phase 1 and Phase 2 then
added context compression, skill snapshots, `view_image`, and Todo Plan
continuity. Those additions made the difference between audit facts,
model-visible working history, and recovery truth more important.

Phase 3 needs explicit session recovery behavior for long-lived prompt sessions
and terminal one-shot prompt sessions. It must support interruption,
terminalization, stale-owner fail-close, and resume without allowing runtime to
recover from natural language summaries, UI state, stream observations, event
replay, or provider/tool/shell mid-flight state.

The existing in-memory `ReplRuntime.conversation` is not sufficient as recovery
truth. A terminal checkpoint that inlines full conversation payloads would also
grow unbounded and duplicate large state.

## Decision

Use terminal recovery checkpoints as the only Phase 3 prompt-session
checkpoint kind.

Phase 3 adds append-only `conversation_messages` as the durable conversation
truth for accepted model-visible message groups. Runtime in-memory conversation
is only a projection of this durable store. A terminal recovery checkpoint stores
a compact recovery manifest that freezes a verified conversation cut, such as a
high-watermark, message count, checksum, and references to related runtime-owned
state.

Explicit resume is the only Phase 3 path that rebuilds process-local
conversation from durable conversation rows. Ordinary runtime drift between the
process-local projection and durable conversation facts is a fail-closed
invariant violation, not a silent repair path.

Resume must preserve provider-visible equivalence for accepted durable
conversation messages included in the checkpoint-frozen projection. For every
accepted model-visible message group in that projection, the round trip from
ordinary process-local projection to durable `conversation_messages`, then from
durable rows through explicit resume back to provider messages, must preserve
the provider-visible role, content, tool-call ids, tool names, arguments,
tool-result pairing, artifact references, and documented runtime wrappers. This
equivalence applies only to accepted durable conversation truth. It does not
apply to stream deltas, partial provider output, incomplete tool calls, pending
tool results, approval drafts, or provider/tool/shell mid-flight state.

Resume is allowed only through explicit `debug-agent resume <session_id>` and
only for eligible terminalized prompt sessions/runs. The explicit resume path may
revive the same session/run lineage by changing terminalized `session` and `run`
rows back to `running`. No other store, CLI, API, or runtime path may revive a
terminalized session or run.

When `debug-agent resume <session_id>` targets the current active owner itself
and that owner is proven stale, Phase 3 may first run the user-confirmed
stale-target fail-close workflow for that same target. This is precondition
handling inside the explicit resume command, not non-terminal attach or
non-terminal resume. Ordinary resume validation may continue only after the
stale-target fail-close has terminalized the target, released ownership through
owner-token fencing, and produced a valid `terminal_recovery` checkpoint.

Terminal recovery checkpoints must not represent ordinary turn, context, error,
streaming, trace, UI, provider, tool, or shell mid-flight state. Running
cancellation, turn-scoped failure, ordinary tool failures, context failures, and
compression failures persist durable facts and events; they do not create
independent non-terminal checkpoints or provenance snapshots. Later eligible
terminalization may write a terminal checkpoint that references the already
durable facts.

Startup/config/schema failures are not resumable. If such a failure happens
after session/run creation, runtime may terminalize the session/run and write
normalized audit failure facts/events, but it must not write a terminal recovery
checkpoint.

Todo Plan remains run-scoped runtime truth. Because resume restores the same run
lineage, a terminal recovery checkpoint snapshots Todo Plan state and restores
the current row for that same run during resume. Todo Plan is not recovered from
conversation history, compression summary, trace, UI state, or the mutable
current TodoPlanStore row alone.

Phase 3 stops writing context snapshots as runtime provenance for prompt
sessions/runs. If a context summary must be model-visible after resume, it must
already be present in `conversation_messages` as durable conversation truth.

## Supersedes / Refines

- Refines [ADR 0003: SQLite Event Log Plus Checkpoint Snapshot](0003-sqlite-event-log-checkpoint.md)
  by restricting resume recovery to terminal recovery checkpoints and by adding
  durable conversation rows as the checkpoint-referenced conversation source.
- Refines [ADR 0010: ModelContextFrame As The LLM-Visible Context Boundary](0010-modelcontextframe-llm-visible-context-boundary.md)
  by replacing in-memory `ReplRuntime.conversation` as durable working history
  with append-only `conversation_messages`; in-memory conversation becomes a
  projection used to build model context.
- Refines [ADR 0011: Layered Context Compression For Runtime Continuity](0011-layered-context-compression-continuity.md)
  by stopping Phase 3 prompt-session context snapshot writes, relying on
  durable conversation projection state for current projection continuity, and
  requiring resumable context summaries to be durable conversation messages.
- Refines [ADR 0013: Runtime-Owned Todo Plan Continuity](0013-runtime-owned-todo-plan-continuity.md)
  by defining same-run Todo Plan restore semantics for terminalized prompt
  session resume.

## Alternatives Considered

### Resume from the latest checkpoint of any kind

This preserves Phase 0/1 checkpoint behavior, but it makes ordinary turn,
context, and error checkpoints look like recovery truth even when they cannot
restore a complete prompt runtime.

### Inline full conversation in every terminal checkpoint

This makes each terminal checkpoint self-contained, but duplicates unbounded
conversation payloads and encourages checkpoint JSON to become a large history
store.

### Recover by replaying event log rows

Event replay can reconstruct useful audit narratives, but it requires event
sourcing semantics that the runtime deliberately avoided. Events remain facts
for audit and trace, not the executable recovery source.

### Recover from context snapshots or compression summaries

This is compact, but it turns continuity summaries into recovery truth and
reintroduces ambiguity, omission, and model-generated interpretation into
runtime state recovery.

### Create successor sessions on resume

Successor sessions avoid terminal status revival, but they split one debugging
lineage across multiple session ids and complicate active ownership, Todo Plan,
approval grants, trace, and artifact relationships.

## Consequences

- Phase 3 changes runtime truth schema and checkpoint payload semantics, so it
  requires a SQLite `PRAGMA user_version` bump and legacy database fail-closed
  behavior.
- Phase 3 prompt sessions/runs must not write non-terminal checkpoint or
  context-snapshot provenance. `latest_checkpoint_id` must point only to a
  terminal recovery checkpoint that can serve as a resume entrypoint.
- Resume validation must fail closed when the terminal checkpoint is missing,
  not terminal, schema-incompatible, checksum-invalid, or references an invalid
  durable conversation cut.
- Resume restore and subsequent provider prompt projection must be equivalent
  to the ordinary non-resume projection for every accepted durable message in
  the checkpoint-frozen projection. Durable serialization must not drop or
  reshape provider-visible content in a way that changes what the model sees
  after resume.
- Startup/config/schema failures remain auditable but are non-resumable by
  construction.
- Running cancellation and tool/provider/shell interruption do not imply
  mid-flight recovery. Only already accepted durable facts can be included in a
  later terminal recovery checkpoint.

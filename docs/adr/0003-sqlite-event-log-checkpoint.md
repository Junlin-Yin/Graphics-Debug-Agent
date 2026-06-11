# ADR 0003: SQLite Event Log Plus Checkpoint Snapshot

## Status

Accepted for Phase 0.

## Context

`debug-agent` must support long local debugging sessions that can be inspected after failure and later resumed from structured state. It needs both auditability and a compact recovery source.

Large outputs such as logs, captures, diffs, and model/tool details should not live in model context or checkpoint JSON.

## Decision

Use SQLite for metadata and audit records, with local filesystem artifacts.

- `run_events` records append-only audit facts.
- `checkpoints` records authoritative state snapshots.
- `artifacts` records filesystem-backed large outputs.
- Phase 0 `trace.md` is derived from events and artifacts; later phase
  refinements may supersede this trace derivation model without making
  Markdown trace output runtime truth.

## Alternatives Considered

### Checkpoint-only

Simpler, but weak for diagnosing failures and reconstructing what happened.

### Full event sourcing

More theoretically complete, but requires replay semantics and migration discipline that are too heavy for v1.

### JSONL files only

Easy to start, but brittle for querying status, enforcing active workspace ownership, migrations, and consistency checks.

### Postgres

Operationally heavier than needed for local-first Phase 0.

## Consequences

- Runtime can answer `status` and `trace` without scanning arbitrary files.
- Recovery uses checkpoint snapshots rather than replaying every event.
- Migration strategy must exist from Phase 0, even if minimal.
- Artifact ids, not raw paths, become long-term references.

## Trace And Log Derivation

`trace.md` and `engine.log` are observability outputs, not runtime truth.

Runtime truth remains in SQLite rows and artifact records. `trace.md` is rendered
from sessions, runs, run events, checkpoints, and artifacts. It may be refreshed
when a session reaches a terminal state, and it may be refreshed on demand by
`debug-agent trace <session_id>` when missing or stale.

Phase 0 trace freshness is intentionally cheap: compare rendered event metadata
with current persisted event metadata, such as event count and latest event id.
Phase 0 does not checksum event payloads or artifact contents for trace
freshness.

This keeps event writing simple and avoids turning trace generation into a
synchronous dependency for every runtime event. If richer trace integrity is
needed later, it should extend the renderer metadata without making `trace.md`
the authoritative state store.

## Phase 3 Refinement

[ADR 0014](0014-terminal-recovery-checkpoints-durable-conversation.md) refines
the checkpoint recovery semantics introduced here.

For Phase 3 prompt sessions, resume recovery is restricted to terminal recovery
checkpoints. Ordinary `turn`, `context`, and `error` checkpoints are not resume
entrypoints. Phase 3 also adds append-only `conversation_messages` as durable
conversation truth; terminal recovery checkpoints reference a validated
conversation cut instead of relying on event replay or full inline conversation
payloads.

[ADR 0015](0015-normalized-error-taxonomy-narrow-runtime-retry.md) refines
failure event payloads by requiring normalized failure facts under
`payload.error` for Phase 3 failure-class events.

## Phase 3.5 Refinement

Phase 3.5 supersedes the Phase 0 trace derivation model for prompt sessions.
`trace.md` remains non-authoritative observability output, but it is no longer
rendered as a runtime event/checkpoint timeline and it no longer uses rendered
event metadata such as event count or latest event id for freshness.

For Phase 3.5 prompt sessions:

- `trace.md` is rendered from accepted closed `conversation_messages` rows,
  ordered by `message_index`.
- ordinary run events, checkpoint internals, approval internals, context
  compression internals, and admin timeline facts remain outside the trace body.
- `debug-agent trace <session_id>` fully rebuilds the trace from SQLite runtime
  truth; it must not read or trust old Markdown trace metadata.
- trace output moves to `.sessions/<session_id>/logs/trace.md`.
- `.sessions/<session_id>/logs/events.jsonl` replaces
  `.sessions/<session_id>/logs/engine.log` as the non-authoritative JSONL
  observation stream for run events, audit facts, and runtime diagnostics.

This refinement does not make Markdown or JSONL output runtime truth. SQLite
rows, checkpoint payloads, and artifact records remain authoritative for status,
resume, checkpoint validation, audit, and recovery.

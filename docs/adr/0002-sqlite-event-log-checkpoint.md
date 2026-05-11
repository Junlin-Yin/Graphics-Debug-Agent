# ADR 0002: SQLite Event Log Plus Checkpoint Snapshot

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
- `trace.md` is derived from events and artifacts.

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


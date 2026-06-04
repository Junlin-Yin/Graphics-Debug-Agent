# Phase 3 Terminal Checkpoint Spec

## Purpose

Phase 3 makes terminal recovery checkpoints the only resume entrypoint.

Events, ordinary checkpoints, context snapshots, trace output, stream
observations, UI state, and in-memory conversation are not resume truth.

## Checkpoint Kinds

Phase 3 checkpoint records must distinguish terminal recovery checkpoints from
non-resume provenance records.

Required logical fields:

- `checkpoint_id`.
- `session_id`.
- `run_id`.
- `kind`.
- `schema_version`.
- `created_at`.
- `payload_json`.
- `payload_sha256`.

Allowed Phase 3 resume checkpoint kind:

- `terminal_recovery`

If implementation keeps other checkpoint/provenance kinds for audit or
inspection, they must not be accepted by resume and must not update
`latest_checkpoint_id`.

## `latest_checkpoint_id`

For Phase 3 prompt sessions/runs, `latest_checkpoint_id` means:

```text
the latest terminal_recovery checkpoint that can serve as a resume entrypoint
```

No ordinary turn, context, error, streaming, trace, UI, or non-terminal
provenance record may write this field.

Startup/config/schema failure sessions must not set `latest_checkpoint_id`,
because they do not have a terminal recovery checkpoint.

## Terminal Recovery Manifest

A `terminal_recovery` checkpoint payload stores a compact manifest:

```json
{
  "schema_version": 1,
  "checkpoint_kind": "terminal_recovery",
  "session_id": "session_...",
  "run_id": "run_...",
  "run_type": "prompt",
  "terminal_status": "failed",
  "terminal_reason": "user_cancel_idle",
  "terminal_error": {
    "error_class": "cancelled",
    "reason": "user_cancel_idle"
  },
  "conversation_cut": {
    "highest_message_index": 42,
    "message_count": 42,
    "checksum": "sha256:..."
  },
  "todo_plan": {
    "plan_version": 7,
    "checksum": "sha256:..."
  },
  "approval_state": {
    "approval_mode": "on-request",
    "grant_cut": "..."
  },
  "active_skills": {
    "snapshot_ids": []
  },
  "frozen_snapshots": {
    "config_snapshot_id": "...",
    "policy_snapshot_id": "..."
  },
  "artifacts": {
    "artifact_ids": []
  }
}
```

Exact field names may follow existing store conventions, but the manifest must
contain enough structured information to verify and restore:

- session/run identity.
- prompt run type.
- terminal status and reason.
- normalized terminal error/cancellation fact when applicable.
- durable conversation cut.
- Todo Plan current state for the same run.
- approval mode and session-scoped approval grants.
- active skill snapshots and frozen resource references.
- frozen config/policy/tool availability references.
- artifact references needed by conversation or runtime state.

The manifest must not inline unbounded conversation history.

## Eligible Writers

Runtime may write terminal recovery checkpoints for:

- idle `Ctrl+C` terminalization.
- graceful `/exit`.
- normal shutdown of an eligible idle prompt session.
- terminal prompt failure after accepted durable facts exist.
- eligible one-shot prompt terminalization.
- user-confirmed stale fail-close when durable facts are sufficient.

Runtime must not write terminal recovery checkpoints for:

- startup/config/schema failure.
- legacy schema fail-closed startup.
- ordinary turn-scoped failure.
- running turn `Ctrl+C` by itself.
- compression/context failure by itself.
- tool failure by itself.
- stream observation.
- TUI error.
- provider/tool/shell mid-flight state.

Running cancellation or turn-scoped failure can later appear in a terminal
recovery checkpoint only as already persisted durable facts referenced by the
terminal checkpoint.

## Startup Failure Rule

Startup/config/schema failures are non-resumable.

If such a failure happens after session/run creation:

- write normalized audit failure facts/events if persistence is available.
- terminalize session/run.
- release active ownership if acquired.
- do not write a terminal recovery checkpoint.
- leave `latest_checkpoint_id` unset.

Resume must fail closed for these sessions even if events contain detailed
failure facts.

## Resume Validation

`debug-agent resume <session_id>` must validate:

- Phase 3 schema version.
- session exists.
- session is terminalized.
- run exists and is terminalized.
- run type is `prompt`.
- session/run are not startup/config/schema failure.
- `latest_checkpoint_id` exists.
- checkpoint exists.
- checkpoint kind is `terminal_recovery`.
- checkpoint schema version is supported.
- checkpoint session/run identity matches.
- payload checksum validates.
- conversation cut validates.
- Todo Plan reference/snapshot validates.
- approval grants and active skill snapshot references validate.
- frozen config/policy references validate.
- required artifacts exist and validate.
- active workspace ownership can be reacquired.

Any failure must fail closed. Resume must not best-effort skip invalid recovery
state.

## Same-Lineage Revival

When resume succeeds:

- keep the same `session_id`.
- keep the same `run_id`.
- preserve terminalization events.
- preserve the terminal recovery checkpoint.
- write `session_resumed` and `run_resumed` events.
- transition session/run lifecycle back to `running`.
- reacquire active workspace ownership.
- start REPL using restored runtime context.

No other path may transition a terminalized session/run to `running`.

## Checkpoint Integrity

Terminal checkpoint creation and terminal session/run status transition must be
atomic from the perspective of resume eligibility.

If checkpoint writing fails, runtime must not mark the session/run as resumable.
If terminal status transition fails after checkpoint writing, runtime must
record a persistence transition failure and avoid presenting the session as
cleanly resumable until store state is consistent.

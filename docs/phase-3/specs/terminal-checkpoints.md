# Phase 3 Terminal Checkpoint Spec

## Purpose

Phase 3 makes terminal recovery checkpoints the only resume entrypoint.

Events, trace output, stream observations, UI state, and in-memory conversation
are not resume truth. Phase 3 prompt sessions/runs do not write ordinary
turn/context/error checkpoints or context snapshots as non-terminal provenance.

## Checkpoint Kinds

Phase 3 prompt sessions/runs write only terminal recovery checkpoints.

Required logical fields:

- `checkpoint_id`.
- `session_id`.
- `run_id`.
- `kind`.
- `checkpoint_schema_version`.
- `created_at`.
- `payload_json`.
- `payload_sha256`.

Allowed Phase 3 checkpoint kind for prompt sessions/runs:

- `terminal_recovery`

Runtime must reject attempts to write ordinary turn, context, error, streaming,
trace, UI, or other non-terminal checkpoint/provenance records for Phase 3
prompt sessions/runs. If a corrupt, manually modified, or test fixture database
contains another checkpoint kind, resume must fail closed instead of accepting
it.

## `latest_checkpoint_id`

For Phase 3 prompt sessions/runs, `latest_checkpoint_id` means:

```text
the latest terminal_recovery checkpoint that can serve as a resume entrypoint
```

No ordinary turn, context, error, streaming, trace, UI, or non-terminal
provenance record may be written for Phase 3 prompt sessions/runs.

Startup/config/schema failure sessions must not set `latest_checkpoint_id`,
because they do not have a terminal recovery checkpoint.

## Terminal Recovery Manifest

A `terminal_recovery` checkpoint payload stores a compact manifest. This is a
normal one-shot completion example:

```json
{
  "manifest_schema_version": 1,
  "checkpoint_kind": "terminal_recovery",
  "session_id": "session_...",
  "run_id": "run_...",
  "run_type": "prompt",
  "terminal_status": "completed",
  "terminal_reason": "terminal_completion",
  "terminal_error": null,
  "conversation": {
    "fact_cut": {
      "highest_message_index": 42,
      "message_count": 42,
      "checksum": "sha256:..."
    },
    "projection_snapshot": {
      "projection_state_id": "projection_...",
      "source_high_watermark": 42,
      "message_refs": [
        {"start": 1, "end": 8},
        {"index": 15},
        {"start": 30, "end": 42}
      ],
      "checksum": "sha256:..."
    }
  },
  "todo_plan": {
    "plan_version": 7,
    "items": [],
    "checksum": "sha256:..."
  },
  "approval_state": {
    "approval_mode": "normal",
    "grant_high_watermark": 12,
    "grant_count": 3,
    "grant_checksum": "sha256:..."
  },
  "active_skills": {
    "records": []
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

An eligible idle prompt session may terminalize before any durable conversation
message has been accepted. For `/exit`, normal graceful shutdown, or idle
prompt termination paths that do not create a model-visible failure or
cancellation fact, the manifest uses the same shape with
`conversation.fact_cut.highest_message_index = 0`,
`conversation.fact_cut.message_count = 0`, and an empty
`conversation.projection_snapshot.message_refs` list. The fact-cut and
projection checksums are computed from the canonical empty cut/projection inputs
defined in `durable-conversation.md`. This zero-message checkpoint shape is not
allowed for idle `Ctrl+C` or terminal prompt failure.

A failed terminalization example uses the same manifest shape but carries failed
terminal facts:

```json
{
  "manifest_schema_version": 1,
  "checkpoint_kind": "terminal_recovery",
  "session_id": "session_...",
  "run_id": "run_...",
  "run_type": "prompt",
  "terminal_status": "failed",
  "terminal_reason": "terminal_failure",
  "terminal_error": {
    "schema_version": 1,
    "error_class": "model_error",
    "reason": "model_call_failed",
    "message": "The model call failed after accepted durable conversation facts.",
    "scope": "turn",
    "recoverability": "terminal_recoverable",
    "metadata": {},
    "artifact_ids": []
  },
  "conversation": {
    "fact_cut": {
      "highest_message_index": 3,
      "message_count": 3,
      "checksum": "sha256:..."
    },
    "projection_snapshot": {
      "projection_state_id": "projection_...",
      "source_high_watermark": 3,
      "message_refs": [
        {"start": 1, "end": 3}
      ],
      "checksum": "sha256:..."
    }
  },
  "todo_plan": {
    "plan_version": 1,
    "items": [],
    "checksum": "sha256:..."
  },
  "approval_state": {
    "approval_mode": "normal",
    "grant_high_watermark": 0,
    "grant_count": 0,
    "grant_checksum": "sha256:..."
  },
  "active_skills": {
    "records": []
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
- durable conversation fact cut.
- checkpoint-frozen conversation projection snapshot.
- Todo Plan snapshot for the same run, including plan version, item order,
  content, status, and active form.
- approval mode and a verifiable session-scoped approval grant cut.
- active skill runtime records, including skill id, snapshot/content hash
  reference, activation reason, scope, and frozen resource references.
- frozen config/policy/tool availability references.
- artifact references needed by conversation or runtime state.

The manifest must not inline unbounded conversation history.

Frozen tool availability is a logical recovery input, not a value recomputed
from current config, environment variables, or current provider availability.
The manifest must include a verifiable reference to the original session's
tool-availability snapshot, either as a dedicated
`tool_availability_snapshot_id` plus checksum or as an explicitly checksummed
field inside the frozen config/policy snapshot. The referenced availability
facts must cover every model-visible tool whose availability can vary by
session startup state, including `view_image` enabled/disabled state,
disabled reason, multimodal limits, and the `shell_exec` schema limit derived
from frozen `max_shell_timeout_seconds`. Resume must fail closed if the
referenced tool-availability facts are missing, checksum-invalid,
session-mismatched, or would require re-reading current config or environment
state to reconstruct.

`manifest_schema_version` is the schema version for this checkpoint payload
shape only. It must not be merged with SQLite `PRAGMA user_version`: the SQLite
user version gates whether runtime may interpret database tables at all, while
the manifest schema version gates whether a specific checkpoint payload can be
validated. Resume must validate both.

All terminal checkpoint checksums, including `payload_sha256`, conversation
fact-cut checksum, projection snapshot checksum, Todo Plan checksum, and
approval grant checksum, use the canonicalization rules in
`durable-conversation.md`.

Allowed terminal reasons:

- `terminal_completion`: one-shot prompt execution completed normally and
  terminalized with `terminal_status = "completed"` so the prompt session can
  later resume into REPL under the ordinary eligibility rules.
- `user_exit`: explicit `/exit` or normal graceful REPL shutdown.
- `user_cancel_idle`: idle `Ctrl+C`.
- `terminal_failure`: terminal prompt failure that is not
  startup/config/schema failure and is not stale fail-close.
- `terminal_stale`: later user-confirmed stale fail-close terminalized this
  session/run.

Terminal reason/status/error matrix:

| Terminal reason | Required terminal status | Required terminal error |
| --- | --- | --- |
| `terminal_completion` | `completed` | absent or `null` |
| `user_exit` | `completed` | absent or `null` |
| `user_cancel_idle` | `failed` | normalized session-scoped cancellation fact `cancelled/user_cancel_idle` |
| `terminal_failure` | `failed` | normalized terminal failure fact |
| `terminal_stale` | `failed` | absent or `null` |

`terminal_error` is required only when the terminalization is caused by a
normalized failure or cancellation fact. For `terminal_completion`,
`terminal_error` must be absent or `null` because normal one-shot completion is
not an error. For `user_exit`, `terminal_error` must be absent or `null` because
graceful idle exit is not an error. For `terminal_stale`, `terminal_error` must
be absent or `null` because stale fail-close is an administrative closure
performed by a later process, not a normalized execution failure reported by the
old session/run. For `user_cancel_idle`, `terminal_error` must reference the
normalized session-scoped cancellation fact `cancelled/user_cancel_idle`. For
`terminal_failure`, `terminal_error` must reference the normalized terminal
failure fact that caused terminalization.

If terminal checkpoint creation fails for a path that expected resumability,
runtime must not present the session/run as terminal-recoverable or set
`latest_checkpoint_id`; failure behavior follows the checkpoint integrity rules
below.

## Eligible Writers

Runtime may write terminal recovery checkpoints for:

- idle `Ctrl+C` terminalization.
- graceful `/exit`.
- normal graceful shutdown of an eligible idle prompt session.
- terminal prompt failure after a closed accepted durable conversation cut
  exists.
- eligible one-shot prompt terminalization, including normal completion with
  terminal reason `terminal_completion`.
- user-confirmed stale fail-close when durable facts are sufficient.

A session terminalized by user-confirmed stale fail-close remains eligible for
later explicit `debug-agent resume <session_id>` when it has a valid
`terminal_recovery` checkpoint and all other resume validation succeeds. Stale
fail-close itself must not auto-attach or auto-resume the stale session; it only
administratively terminalizes and releases ownership so the user-triggered
startup or resume flow that discovered the stale owner can proceed.

Runtime must not write terminal recovery checkpoints for:

- startup/config/schema failure.
- unknown-schema fail-closed startup.
- legacy startup reset before Phase 3 session/run creation.
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

Idle terminalization does not require an accepted durable conversation group.
If no message has been accepted, runtime may still write a terminal recovery
checkpoint using the canonical zero-message conversation cut and empty
projection snapshot. This exception is limited to non-failure idle
terminalization paths such as `/exit` and normal graceful shutdown. Idle
`Ctrl+C` writes a session-scoped cancellation fact before checkpoint creation
and therefore does not use the zero-message cut. Terminal prompt failure remains
checkpoint-eligible only after a closed accepted durable conversation cut
exists.

For terminal prompt failure, a closed accepted durable conversation cut means
there is at least one closed accepted `conversation_messages` group for the
prompt run, such as accepted user input, an accepted assistant output, an
accepted tool observation, an accepted runtime failure/cancellation fact, or an
accepted context summary, and the current projection state validates against
that cut. A failure after session/run creation but before any accepted durable
conversation group is not terminal-checkpoint eligible.

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
- conversation fact cut validates.
- checkpoint-frozen conversation projection snapshot validates.
- zero-message fact cuts and empty projection snapshots, when present, are used
  only for allowed non-failure idle terminalization reasons.
- checkpoint-embedded Todo Plan snapshot validates.
- approval grants and active skill runtime records plus snapshot references
  validate.
- frozen config/policy references validate.
- required artifacts exist and validate.
- active workspace ownership can be reacquired.

Any failure must fail closed. Resume must not best-effort skip invalid recovery
state.

## Approval Grant Cut

The terminal manifest must not use an opaque `grant_cut` field. Approval grants
already live in the session-local `approval_grants` table, so the checkpoint
stores a verifiable cut over that table instead of duplicating full grant rows.

The approval grant cut contains:

- `grant_high_watermark`: the highest grant row id or monotonic grant sequence
  included for the session at terminalization time.
- `grant_count`: the number of included grant rows.
- `grant_checksum`: checksum over canonical grant rows up to the high-watermark.

Resume validates that the persisted `approval_grants` rows for the same session
match this cut before restoring approval mode and reusable session grants. If
the grant cut is missing, references another session, or checksum validation
fails, resume fails closed with `persistence_error/checkpoint_invalid` or a more
specific persistence reason when available.

## Same-Lineage Revival

When resume succeeds:

- keep the same `session_id`.
- keep the same `run_id`.
- preserve terminalization events.
- preserve the terminal recovery checkpoint.
- reacquire active workspace ownership.
- record current owner `pid`, `host_id`, and fresh `owner_token`.
- transition session/run lifecycle back to `running`.
- write `session_resumed` and `run_resumed` events.
- start REPL using restored runtime context.

No other path may transition a terminalized session/run to `running`.

## Checkpoint Integrity

Terminal checkpoint creation, terminal session/run status transition, and
`latest_checkpoint_id` update must be committed in one consistency boundary for
resume eligibility when they are part of the same terminalization workflow.

Ownership release happens only after terminal state is consistent. If ownership
release fails after durable terminalization, runtime must preserve terminal
facts and the terminal recovery checkpoint, record a normalized ownership
release failure where persistence is available, and leave active ownership
blocked so later startup or resume fails closed unless user-confirmed stale
fail-close or manual cleanup resolves the blockage.

If checkpoint writing fails, runtime must not mark the session/run as resumable.
If terminal status transition fails after checkpoint writing, runtime must
record a persistence transition failure and avoid presenting the session as
cleanly resumable until store state is consistent.

For resumable user-confirmed stale fail-close, the terminal checkpoint,
terminal session/run status, `latest_checkpoint_id`, administrative
`stale_fail_closed` event, and active ownership release must be committed in one
owner-token-fenced SQLite transaction over the authoritative ownership row. If
checkpoint payload bytes or artifact-backed checkpoint content are prepared
outside SQLite before that transaction, they are not runtime truth unless the
transaction commits a checkpoint row/reference to them.

For non-resumable user-confirmed stale fail-close when durable facts are
insufficient for a terminal recovery checkpoint, runtime must commit terminal
session/run status, administrative `stale_fail_closed` event, and active
ownership release in one owner-token-fenced SQLite transaction, without writing
a fake or partial terminal checkpoint and with `latest_checkpoint_id`
unset/cleared so it cannot point to an older checkpoint. Older checkpoint rows,
if any, remain auditable historical records, but after this non-resumable stale
closure they must not be exposed through `latest_checkpoint_id` or accepted as
the current resume entrypoint. Local ownership anchor files, if present, are
diagnostic only and may be cleaned up best-effort after the SQLite truth is
consistent; they are not part of the ownership truth or resume eligibility
boundary. If the owner-token compare-and-swap fails, runtime must not leave
behind a checkpoint row/reference, `latest_checkpoint_id`, terminal status,
administrative event, ownership release, or other committed DB state that makes
the stale owner appear resumable. The fenced SQLite transaction must roll back,
and runtime must not terminalize or release whichever owner record is now
current. Any checkpoint payload prepared outside SQLite remains an unreferenced
orphan diagnostic only and must not be accepted by resume.

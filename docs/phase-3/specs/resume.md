# Phase 3 Resume Spec

## Purpose

This spec defines `debug-agent resume <session_id>` eligibility, validation,
restoration, and failure behavior.

Resume restores runtime context from a terminal recovery checkpoint. It does
not resume provider, tool, shell, token, or workflow mid-flight state.

Phase 3 chooses same-lineage restore. It restores the original prompt
`session_id` and `run_id`; it does not copy Todo Plan or conversation state into
a successor run.

## Command Contract

`debug-agent resume <session_id>`:

- is the only CLI/API path that may revive terminalized session/run lineage.
- resumes eligible prompt sessions into REPL.
- preserves the same `session_id` and `run_id`.
- reacquires active workspace ownership before transitioning lifecycle state.
- fails closed for ineligible or inconsistent state.

The command must not append a model-visible observation by itself.

## Resume Order

Resume must execute in this order:

1. validate Phase 3 schema before reading runtime truth.
2. perform minimal target preflight: session/run exist, are prompt lineage, and
   are not startup/config/schema failure.
3. if the target is non-terminal because it is the current active owner, run
   the explicit stale-target branch:
   - prove the active owner stale using the captured owner facts and
     `owner_token`.
   - obtain explicit user confirmation.
   - fail-close that target through the owner-token fenced stale fail-close
     workflow.
   - continue only when fail-close releases ownership and produces a valid
     `terminal_recovery` checkpoint for the same target.
   - fail closed after the confirmed administrative fail-close if no valid
     checkpoint exists or any later ordinary validation fails.
4. perform ordinary terminal target validation: session/run are terminal and
   `latest_checkpoint_id` exists.
5. perform full recovery validation: terminal checkpoint, durable conversation
   fact cut, checkpoint-frozen conversation projection snapshot, Todo Plan,
   approval grants, active skill runtime records and snapshot references,
   frozen config/policy/tool-availability snapshots, and required artifacts.
6. check active workspace ownership.
7. if active ownership is blocked, run stale proof; on proven stale evidence and
   explicit user confirmation, terminalize the stale owner and release
   ownership. This branch handles stale owners other than the already
   fail-closed resume target.
8. reacquire active workspace ownership by writing a current owner record with
   this process's `pid`, `host_id`, and a fresh `owner_token`.
9. transition the same session/run lifecycle rows back to `running` and
   associate the revived lineage with the owner facts from step 8.
10. write `session_resumed` and `run_resumed` audit events.
11. start REPL with restored runtime context.

If any step fails, subsequent steps must not run.

Steps 8 and 9 are one resume-revival consistency boundary. Implementations may
commit the active owner claim and lifecycle revival in a single SQLite
transaction, or use an equivalent fenced sequence that rolls back or releases
the newly claimed owner if lifecycle revival fails. Runtime must not leave a
durable state where resume has claimed active ownership for the workspace but
the target session/run was not revived to `running`, except for a recorded
abnormal persistence failure that keeps ownership blocked and prevents later
startup/resume until user-confirmed stale fail-close or manual cleanup.

The startup/config/schema failure marker used by minimal preflight must come
from persisted session/run lifecycle or terminal metadata fields written by the
startup failure path, such as a fixed terminal/source reason or non-resumable
startup-failure flag. Resume must not infer this marker from event replay,
trace output, terminal checkpoint payloads, or natural-language messages.

Phase 3 session/run lifecycle schema must therefore include a structured
non-resumable startup-failure marker. The exact SQL name may follow store
conventions, but the logical field must distinguish startup/config/schema
failure terminalization from ordinary terminal prompt failure before resume reads
events or checkpoints. Acceptable shapes include a boolean
`non_resumable_startup_failure` field or a fixed terminal source/reason field
whose allowed value set includes `startup_failure`. Startup/config/schema failure
paths that create session/run rows must set this marker before releasing
ownership or returning an error.

## Eligibility

A session/run is resumable when all conditions hold:

- Phase 3 schema version matches.
- session exists.
- primary prompt run exists.
- session lifecycle is terminal.
- run lifecycle is terminal.
- run type is `prompt`.
- session/run are not startup/config/schema failure.
- `latest_checkpoint_id` references a terminal recovery checkpoint.
- terminal recovery checkpoint validates.
- durable conversation fact cut validates.
- checkpoint-frozen conversation projection snapshot validates.
- Todo Plan, approval grants, active skill runtime records and snapshot
  references, frozen config/policy/tool-availability snapshots, and required
  artifacts validate.
- active workspace ownership can be reacquired.

Long-lived REPL prompt sessions and one-shot terminal prompt sessions use the
same eligibility rules.

## Ineligible Sessions

Resume must reject:

- running sessions.
- idle non-terminal sessions.
- stale non-terminal sessions, except when the stale non-terminal session is
  the explicit resume target, the active owner is proven stale, the user
  confirms fail-close, and the fail-close produces a valid terminal recovery
  checkpoint before ordinary resume validation continues.
- startup/config/schema failure sessions.
- sessions without `latest_checkpoint_id`.
- sessions whose latest checkpoint is not `terminal_recovery`.
- sessions whose checkpoint, conversation fact cut, or projection snapshot fails
  validation.
- sessions whose schema version is legacy or unknown.
- non-prompt runs.
- sessions blocked by active ownership that cannot be cleared through
  user-confirmed stale fail-close.

A session whose terminal reason is `terminal_stale` is not rejected solely
because it was stale fail-closed. It is resumable when it has a valid
`terminal_recovery` checkpoint and all ordinary eligibility checks pass.

If `debug-agent resume <session_id>` targets the current stale active owner
itself, runtime may use the same user-triggered resume workflow to prove stale
ownership, obtain confirmation, fail-close the target, release ownership through
owner-token fencing, and then continue ordinary resume validation for the same
target. If fail-close cannot produce a valid terminal recovery checkpoint, or
any ordinary validation fails after fail-close, the command fails closed after
the confirmed administrative fail-close. Runtime must not create a successor
session, must not attach to the stale process, and must not best-effort recover
from partial state.

## Restore Inputs

Resume reads:

- terminal recovery checkpoint manifest.
- durable `conversation_messages` fact cut.
- checkpoint-frozen conversation projection snapshot.
- checkpoint-embedded Todo Plan snapshot for the same run.
- approval mode and session-scoped approval grants.
- active skill runtime records, including skill id, snapshot/content hash
  reference, activation reason, scope, and frozen resource references.
- frozen config, policy, and tool-availability snapshots.
- artifact metadata referenced by conversation/checkpoint/runtime state.
- terminal facts and normalized terminal error/cancellation.

Resume restores from the original frozen session/run snapshots referenced by
the terminal recovery checkpoint. It must not re-read current config files,
current policy files, current skill source files, or current `view_image`
availability as recovery truth. Disk changes after the original session startup
do not hot-reload the resumed lineage.

Resume must not read recovery truth from:

- event replay.
- trace rendering.
- legacy or corrupt-database context snapshots.
- natural-language summary unless it is a durable conversation message.
- TUI state.
- stream observations.
- provider/tool/shell internal state.

## Restore Output

On success, runtime:

- reacquires active workspace ownership.
- transitions session/run lifecycle back to `running`.
- records current owner `pid`, `host_id`, and fresh `owner_token`.
- rebuilds process-local conversation projection from checkpoint-frozen
  projection snapshot and durable conversation rows.
- restores the same run's current Todo Plan row from the checkpoint-embedded
  Todo Plan snapshot, overwriting any drifted current row after validation.
  Resume may overwrite only the mutable current Todo Plan row for the same run.
  If durable Todo Plan history, the checkpoint-embedded Todo Plan snapshot,
  checksum facts, run ownership, plan version, item order, content, status, or
  active form fail validation, resume must fail closed instead of repairing or
  rewriting durable Todo Plan history.
  This is part of the `run_resumed` restore transition, not a `todo` mutation:
  runtime must not increment the Todo Plan version and must not emit
  `todo_updated` or a separate Todo restore event.
- restores runtime-owned state needed for the next ordinary `ModelContextFrame`.
- writes `session_resumed` event.
- writes `run_resumed` event.
- starts REPL.

The previous terminal checkpoint remains present and remains auditable.

## Failure Behavior

Resume failures are fail-closed:

- do not change session/run lifecycle.
- do not release active ownership unless the failure happened after a
  user-confirmed stale fail-close of another proven-stale owner. In that case,
  runtime has released only the confirmed stale blocking owner; it must not
  release, terminalize, revive, or otherwise mutate the resume target unless the
  target is the explicitly documented stale-target branch.
- do not create a new session/run.
- do not write a recovery checkpoint.
- write audit failure event only when it can be done without mutating the target
  into a partially resumed state.

Error classification:

- missing target: `user_error/lookup_not_found`.
- non-terminal target: `runtime_error/resume_not_eligible`.
- startup/config/schema failure target: `runtime_error/resume_not_eligible`.
- `latest_checkpoint_id` unset: `runtime_error/resume_checkpoint_required`.
- checkpoint row missing: `persistence_error/checkpoint_missing`.
- invalid checkpoint: `persistence_error/checkpoint_invalid`.
- invalid conversation fact cut or projection snapshot:
  `persistence_error/conversation_cut_invalid`.
- ownership blocked by live owner: `policy_error/workspace_owner_active`.
- stale evidence insufficient: `policy_error/workspace_owner_not_proven_stale`.
  If ownership facts or `owner_token` change after stale proof and before the
  fenced fail-close transaction commits, resume fails closed as an active
  ownership blockage and must not continue with stale proof from the old owner
  record.

If the explicit stale-target branch confirmed and completed administrative
fail-close for the target but no valid terminal recovery checkpoint exists, the
resume command must fail closed with the appropriate checkpoint or eligibility
error and tell the user that the target session cannot be recovered. Runtime
must not promise resumability in the stale fail-close confirmation prompt.

## Resume And Startup Failure

Startup/config/schema failure sessions never have terminal recovery checkpoints.
If they contain audit events or terminal facts, those facts do not make them
resumable.

Resume must reject these sessions even when session/run ids exist and lifecycle
status is terminal.

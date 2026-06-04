# Phase 3 Resume Spec

## Purpose

This spec defines `debug-agent resume <session_id>` eligibility, validation,
restoration, and failure behavior.

Resume restores runtime context from a terminal recovery checkpoint. It does
not resume provider, tool, shell, token, or workflow mid-flight state.

## Command Contract

`debug-agent resume <session_id>`:

- is the only CLI/API path that may revive terminalized session/run lineage.
- resumes eligible prompt sessions into REPL.
- preserves the same `session_id` and `run_id`.
- reacquires active workspace ownership before transitioning lifecycle state.
- fails closed for ineligible or inconsistent state.

The command must not append a model-visible observation by itself.

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
- durable conversation cut validates.
- Todo Plan, approval grants, active skills, frozen config/policy snapshots, and
  required artifacts validate.
- active workspace ownership can be reacquired.

Long-lived REPL prompt sessions and one-shot terminal prompt sessions use the
same eligibility rules.

## Ineligible Sessions

Resume must reject:

- running sessions.
- idle non-terminal sessions.
- stale non-terminal sessions.
- startup/config/schema failure sessions.
- sessions without `latest_checkpoint_id`.
- sessions whose latest checkpoint is not `terminal_recovery`.
- sessions whose checkpoint or conversation cut fails validation.
- sessions whose schema version is legacy or unknown.
- non-prompt runs.
- sessions blocked by active ownership that cannot be cleared through
  user-confirmed stale fail-close.

## Restore Inputs

Resume reads:

- terminal recovery checkpoint manifest.
- durable `conversation_messages` cut.
- current or checkpoint-referenced Todo Plan for the same run.
- approval mode and session-scoped approval grants.
- active skill snapshot references.
- frozen config and policy snapshots.
- artifact metadata referenced by conversation/checkpoint/runtime state.
- terminal facts and normalized terminal error/cancellation.

Resume must not read recovery truth from:

- event replay.
- trace rendering.
- context snapshots.
- natural-language summary unless it is a durable conversation message.
- TUI state.
- stream observations.
- provider/tool/shell internal state.

## Restore Output

On success, runtime:

- transitions session/run lifecycle back to `running`.
- reacquires active workspace ownership.
- rebuilds process-local conversation projection from durable conversation cut.
- restores runtime-owned state needed for the next ordinary `ModelContextFrame`.
- writes `session_resumed` event.
- writes `run_resumed` event.
- starts REPL.

The previous terminal checkpoint remains present and remains auditable.

## Failure Behavior

Resume failures are fail-closed:

- do not change session/run lifecycle.
- do not release active ownership unless the failure happened after a
  user-confirmed stale fail-close of another proven-stale owner.
- do not create a new session/run.
- do not write a recovery checkpoint.
- write audit failure event only when it can be done without mutating the target
  into a partially resumed state.

Error classification:

- missing target: `user_error/lookup_not_found`.
- non-terminal target: `runtime_error/resume_not_eligible`.
- startup/config/schema failure target: `runtime_error/resume_not_eligible`.
- missing checkpoint: `runtime_error/resume_checkpoint_required`.
- invalid checkpoint: `persistence_error/checkpoint_invalid`.
- invalid conversation cut: `persistence_error/conversation_cut_invalid`.
- ownership blocked by live owner: `policy_error/workspace_owner_active`.
- stale evidence insufficient: `policy_error/workspace_owner_not_proven_stale`.

## Resume And Startup Failure

Startup/config/schema failure sessions never have terminal recovery checkpoints.
If they contain audit events or terminal facts, those facts do not make them
resumable.

Resume must reject these sessions even when session/run ids exist and lifecycle
status is terminal.

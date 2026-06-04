# Phase 3 Stale Fail-Close Spec

## Purpose

Phase 3 permits user-confirmed stale running session fail-close when startup or
resume is blocked by active workspace ownership and the current owner is proven
stale.

This is a fail-close recovery workflow. It is not attach, auto-resume, or
automatic ownership cleanup.

## Trigger

Stale fail-close may be considered only when:

- user starts a new session and active ownership blocks startup.
- user runs `debug-agent resume <session_id>` and active ownership blocks
  resume.

Runtime must not run stale fail-close as a background cleanup task.

## Required Conditions

Runtime may terminalize the old session/run and release ownership only when:

- active ownership blockage exists.
- owner is proven stale.
- user explicitly confirms the fail-close action.
- runtime can write the required durable facts for the old session/run or can
  fail closed without corrupting ownership state.

If any condition fails, startup/resume fails closed with active ownership
conflict.

## Proven-Stale Evidence

Phase 3 implementation must define a concrete proven-stale evidence set before
implementing fail-close.

Spec TODO, accepted as non-blocking for current docs stage:

- define durable owner process identity fields.
- define host identity requirements.
- define pid and process start-time validation or an equivalent stale-proof
  mechanism.
- define heartbeat or owner lease facts if used.
- define how to distinguish a live owner from pid reuse.
- define what evidence is available cross-platform.
- define trace/status rendering for stale proof without exposing unnecessary
  host internals.

Until this TODO is resolved in implementation planning/spec refinement,
insufficient evidence must fail closed.

## User Confirmation

Interactive startup/resume:

- present the blocked owner session/run id.
- present concise stale evidence summary.
- ask for explicit confirmation.
- proceed only on affirmative confirmation.

Non-interactive commands:

- may reuse confirmation obtained before entering non-interactive execution.
- must fail closed when confirmation cannot be obtained.

No config flag may silently bypass confirmation in Phase 3.

## Fail-Close Action

On confirmed proven-stale fail-close:

1. write audit event for stale fail-close requested.
2. build a terminal recovery checkpoint for the old session/run only if durable
   facts are sufficient and the old session is otherwise checkpoint-eligible.
3. terminalize the old session/run with normalized stale fail-close terminal
   fact.
4. release active ownership.
5. write audit event for stale fail-close completed.
6. continue the original startup or resume flow.

If durable facts are insufficient for a terminal recovery checkpoint, runtime
may terminalize the old session/run as non-resumable according to normalized
failure policy, but it must not write a fake or partial terminal checkpoint.

## Disallowed Behavior

Runtime must not:

- auto attach to stale session.
- auto resume stale session.
- silently release ownership.
- terminalize a live owner.
- terminalize when stale evidence is insufficient.
- create a successor session for the stale owner.
- infer recovery truth from event replay, trace, or UI state.

## Error Reasons

Relevant normalized reasons:

- `policy_error/workspace_owner_active`
- `policy_error/workspace_owner_not_proven_stale`
- `policy_error/workspace_owner_confirmation_unavailable`
- `runtime_error/ownership_release_failed`
- `persistence_error/persistence_transition_failed`

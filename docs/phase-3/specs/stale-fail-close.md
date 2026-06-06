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
- runtime can terminalize the old session/run durably before releasing
  ownership, or can fail closed without corrupting ownership state.

If any condition fails, startup/resume fails closed with active ownership
conflict.

## Proven-Stale Evidence

Phase 3 uses a deliberately minimal same-host stale proof.

Session/ownership truth must record:

- `pid`: local runtime owner process id.
- `host_id`: hashed local host identity for comparing current process host with
  the recorded owner host.
- `owner_token`: an opaque runtime-generated ownership fencing token for the
  current active ownership claim.

Runtime generates a fresh `owner_token` every time it claims or reclaims active
workspace ownership, including new session startup and successful explicit
resume. The token is persisted with the active owner facts and is not exposed to
the model. It may appear in internal diagnostics only when needed to explain a
store invariant. While an active ownership conflict is still unresolved, normal
trace/status or CLI diagnostics may show the blocking owner session/run, raw
`host_id`, and raw `pid`, but must not show the raw token.
After a user-confirmed stale fail-close terminalizes an old owner, trace/status
for that administrative closure must use the redacted `stale_fail_closed` proof
summary and terminal facts. It must not require or reconstruct raw owner
`host_id`, raw `pid`, or raw `owner_token` from the administrative event or
from historical active-owner rows when rendering the terminalized old session.

`host_id` is computed by a runtime-owned host identity provider. Phase 3 uses this
fixed algorithm:

```text
host-v1:sha256(<platform-stable-machine-id>)
```

The platform-stable machine id source is selected in this order:

- Linux: read `/etc/machine-id`, falling back to
  `/var/lib/dbus/machine-id`.
- macOS: run `/usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice` and parse
  `IOPlatformUUID`.
- Windows: read registry value
  `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`.

Runtime stores and compares only the `host-v1:sha256(...)` value. It must not
persist the raw machine id, raw IOPlatformUUID, or raw MachineGuid. Tests may
inject a fake host identity provider, but ordinary runtime must use the
documented provider. If the current platform's machine id cannot be read,
parsed, or hashed, the current `host_id` is unavailable and stale proof must fail
closed.

Runtime may prove stale only when:

- recorded `host_id` matches the current `host_id`; and
- recorded `pid` no longer exists on the current host; and
- recorded `owner_token` is present and is captured with the stale proof.

Runtime must fail closed when:

- recorded `host_id` is missing or differs from the current `host_id`.
- current `host_id` cannot be computed.
- recorded `pid` is missing.
- recorded `pid` exists.
- recorded `owner_token` is missing.
- process liveness cannot be checked reliably.

Phase 3 does not use process name or command line as stale proof. Those details
may appear only as diagnostics in trace/status when available.

This minimal rule intentionally accepts false negatives. PID reuse, a still
existing unrelated process with the same pid, missing host identity, or a
different host may keep active ownership blocked even when the original
`debug-agent` process is gone. This is acceptable because the fail-close safety
boundary prefers refusing cleanup over terminalizing a live owner.

## Ownership Fencing

Stale proof is only valid for the exact active ownership record observed during
the blocked startup or resume workflow. Confirmed stale fail-close must use
compare-and-swap ownership fencing.

The runtime must perform the terminalize/release transition in a durable
transaction that verifies the active owner row still matches the captured:

- blocked workspace root.
- old `session_id`.
- old `run_id`.
- old `host_id`.
- old `pid`.
- old `owner_token`.

If the conditional update affects no row, or the owner facts changed before the
transaction commits, runtime must fail closed with active ownership still
blocked. It must not release ownership, must not terminalize whatever owner is
now current, and must not reuse stale proof from the previous owner record.

Normal ownership release by the current runtime process must also use the
current process's `owner_token` so a late shutdown cannot release ownership that
was already reacquired by another process.

## User Confirmation

Interactive startup/resume:

- present the blocked owner session/run id.
- present concise stale evidence summary.
- ask for explicit confirmation.
- proceed only on affirmative confirmation.
- do not promise that the stale owner will be resumable after fail-close.

Non-interactive commands:

- may reuse confirmation obtained before entering non-interactive execution.
- must fail closed when confirmation cannot be obtained.

No config flag may silently bypass confirmation in Phase 3.

## Fail-Close Action

On confirmed proven-stale fail-close:

1. prepare a terminal recovery checkpoint payload for the old session/run only
   if durable facts are sufficient and the old session is otherwise
   checkpoint-eligible.
2. commit the checkpoint row/reference, terminal session/run lifecycle status
   `failed`, terminal reason `terminal_stale`, `latest_checkpoint_id`, the
   minimal administrative `stale_fail_closed` run event, and active ownership
   release in one owner-token-fenced SQLite transaction over the authoritative
   ownership row.
3. continue the original startup or resume flow only if that flow's own
   preconditions still hold. For the explicit resume-target self branch, this
   requires the fail-close to have produced a valid `terminal_recovery`
   checkpoint for the same target before ordinary resume validation may
   continue.

For resumable stale fail-close, the terminal checkpoint, terminal session/run
status, `latest_checkpoint_id`, administrative event, and ownership release must
be committed in one owner-token-fenced SQLite transaction over the authoritative
ownership row. If checkpoint payload bytes or artifact-backed checkpoint content
must be written outside SQLite before that transaction, those writes are only
prepared payloads. They are not runtime truth unless the fenced transaction
commits a checkpoint row/reference to them. If the transaction rolls back or the
fenced compare-and-swap fails, any such unreferenced payload is an orphaned
diagnostic artifact that may be cleaned up best-effort and must never be
accepted as a resume entrypoint.

If durable facts are insufficient for a terminal recovery checkpoint, runtime
may terminalize the old session/run as non-resumable with lifecycle status
`failed` and terminal reason `terminal_stale`, but it must not write a fake or
partial terminal checkpoint. For non-resumable stale fail-close, terminal
session/run status, administrative event, and ownership release must be
committed in one owner-token-fenced SQLite transaction over the authoritative
ownership row; `latest_checkpoint_id` must be unset/cleared and must not point
to an older checkpoint, a new fake checkpoint, or a partial checkpoint. Older
checkpoint rows, if any, remain auditable historical records, but this
non-resumable administrative closure must not expose any older checkpoint as the
current resume entrypoint.

Local ownership anchor files, if present, are diagnostic only and may be cleaned
up best-effort after the SQLite truth is consistent; they are not part of the
ownership truth or resume eligibility boundary. If the fenced compare-and-swap
fails, the entire owner-token-fenced SQLite transaction must roll back. Runtime
must not commit a terminal checkpoint row/reference, `latest_checkpoint_id`,
terminal status, administrative event, or ownership release for the stale proof,
and must not terminalize or release whichever owner record is now current.

If durable facts are sufficient and a valid terminal recovery checkpoint is
written, the old stale fail-closed session may later be explicitly resumed with
`debug-agent resume <old_session_id>` under the ordinary resume eligibility
rules. Stale fail-close itself must never attach to, continue, or auto-resume
that old session.

When stale fail-close is triggered by ordinary startup, the workflow may only
administratively fail-close the old owner, release ownership, and continue
creating the new startup session. It must not resume the old stale session,
because startup is not an explicit resume command.

When stale fail-close is triggered by `debug-agent resume <session_id>` and the
target is the stale owner itself, the same explicit resume workflow may
fail-close the target first and then continue ordinary resume validation for
that same target only if the fail-close produced a valid terminal recovery
checkpoint and ownership was released. If validation fails, the command fails
closed after the confirmed administrative fail-close.
When that failure is caused by a missing or invalid terminal recovery
checkpoint, the user-facing resume error must say that the target session cannot
be recovered. The prior fail-close confirmation prompt must not have promised
that the target would be resumable.

Stale fail-close is an administrative control-plane closure, not an execution
failure reported by the old session. Runtime must not write a normalized error
fact, must not append a durable conversation failure/cancellation fact, and
must not claim what model, provider, tool, shell, or runtime operation the old
session was performing when its owner process died.

The `stale_fail_closed` event is only an audit marker for the administrative
closure. It records the old session/run identity, terminal reason
`terminal_stale`, and this exact redacted proof summary:

```json
{
  "stale_proof_summary": {
    "host_match": true,
    "pid_absent": true,
    "token_fenced": true
  }
}
```

It must not persist any other stale proof details, process diagnostics, raw
`host_id`, raw `pid`, raw `owner_token`, process name, command line, user
confirmation text, or user input details.

Status and trace rendering must present `terminal_stale` as an administrative
closure. The old session/run lifecycle status is `failed`, but
`stale_fail_closed` is not a failure-class event and does not require
`payload.error`. Status and trace should show the terminal reason
`terminal_stale`, the administrative event, and the absence of a normalized
terminal error without treating that absence as corrupt runtime truth. For the
terminalized old session, stale proof rendering must come from the redacted
summary in `stale_fail_closed`; renderers must not expect raw stale owner facts
in that administrative event and must not recover those raw facts from previous
active-owner records for display.

Active ownership may be released only after the old session/run terminal
transition has been durably written and the fenced owner-token comparison still
matches. If the terminal transition cannot be written or the fenced comparison
fails, startup/resume must fail closed and ownership must remain blocked.

## Disallowed Behavior

Runtime must not:

- auto attach to stale session.
- auto resume stale session during fail-close.
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

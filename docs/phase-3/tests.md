# Phase 3 Test Plan

## Acceptance Criteria

Phase 3 acceptance requires:

- Phase 2 runtime databases fail closed with the Phase 3 compatibility error.
- startup, `status`, `trace`, active ownership checks, and `resume` validate
  schema version before interpreting runtime truth.
- normalized error payloads use fixed `error_class` and `reason` symbols.
- failure-class events carry normalized error objects at `payload.error`.
- model-visible errors expose only the narrow projection.
- semantic CLI exit codes are used at command boundaries.
- `conversation_messages` is append-only durable conversation truth.
- process-local conversation is rebuilt as a projection.
- pending stream/model/tool/shell state is never appended as durable
  conversation.
- terminal recovery checkpoints are the only resume entrypoints.
- `latest_checkpoint_id` points only to terminal recovery checkpoints.
- startup/config/schema failure after session/run creation writes audit facts
  but no terminal recovery checkpoint.
- resume rejects startup/config/schema failure sessions.
- running `Ctrl+C` cancels only the active turn and returns REPL/TUI to input.
- running `Ctrl+C` does not terminalize session/run or release ownership.
- idle `Ctrl+C` terminalizes session/run, writes terminal recovery checkpoint,
  releases ownership, and exits.
- graceful `/exit` writes terminal recovery checkpoint when eligible and releases
  ownership.
- explicit `debug-agent resume <session_id>` restores eligible terminalized
  REPL prompt session using the same session/run lineage.
- explicit `debug-agent resume <session_id>` restores eligible one-shot terminal
  prompt session into REPL using the same eligibility rules.
- no path other than explicit resume revives terminalized session/run lifecycle
  to `running`.
- provider cancellation preserves public adapter contract and reports uncertain
  cancellation as `cancelling`.
- active `shell_exec` receives best-effort termination on running cancellation.
- shell mid-flight state is never resumable.
- stale running fail-close requires proven-stale evidence and user
  confirmation.
- stale fail-close fails closed for live owner, insufficient evidence, or
  missing confirmation.
- retry decisions come from a central registry.
- `repeat_call` applies only to registered retry-safe runtime-owned transient
  failures.
- `output_token_limit_reached` continuation does not accept partial assistant
  output or execute incomplete tool calls.
- shell timeout config distinguishes default and maximum timeout.
- explicit `shell_exec.timeout_seconds` is validated against maximum and is not
  silently capped by default.

## Unit Tests

### Schema Compatibility

- fresh workspace initializes Phase 3 schema version.
- missing schema version fails closed before session truth reads.
- Phase 0/0.5/1/2 schema versions fail closed.
- unknown future schema version fails closed.
- `status` fails closed before reading legacy rows.
- `trace` fails closed before reading legacy rows.
- `resume` fails closed before reading legacy rows.
- active ownership check fails closed before interpreting legacy owner rows.
- legacy failure message instructs user to move/remove `.sessions/` or use a
  fresh workspace.

### Normalized Errors

- every constructed error validates against the central class/reason registry.
- unknown error class is rejected in tests/helpers.
- unknown reason for a class is rejected.
- failure events write `payload.error`.
- model-visible projection omits `source`, `scope`, `recoverability`,
  `metadata`, retry policy, and provider internals.
- startup config failure maps to semantic exit code.
- startup policy failure maps to semantic exit code.
- startup persistence/schema failure maps to semantic exit code.
- lookup missing maps to `ERROR_LOOKUP_NOT_FOUND`.
- process-level interrupt maps to `INTERRUPTED`.

### Durable Conversation

- accepted user input appends one durable conversation row.
- accepted assistant output appends durable conversation row only after complete
  authoritative result.
- accepted tool result appends durable conversation row only after ToolBroker
  normalization.
- failure fact appends only after recovery boundary.
- cancellation fact appends only after recovery boundary.
- stream delta does not append durable conversation.
- partial output from token-limit response does not append final assistant row.
- incomplete tool call does not append accepted assistant tool-call row.
- process-local conversation can be rebuilt from durable rows.
- durable conversation cut checksum validates.
- missing row invalidates cut.
- artifact-backed row validates artifact reference and checksum.

### Terminal Checkpoints

- idle terminalization writes `terminal_recovery` checkpoint.
- terminal failure after accepted facts writes `terminal_recovery` checkpoint
  when eligible.
- running cancellation does not write terminal recovery checkpoint.
- turn-scoped failure does not write terminal recovery checkpoint by itself.
- context/compression failure does not write terminal recovery checkpoint by
  itself.
- startup/config/schema failure writes no terminal recovery checkpoint.
- non-terminal checkpoint/provenance does not update `latest_checkpoint_id`.
- `latest_checkpoint_id` rejects non-terminal checkpoint kind.
- terminal manifest includes conversation cut, Todo Plan, approval state, active
  skills, frozen config/policy references, and artifact references.
- invalid checkpoint checksum fails resume.
- invalid conversation cut fails resume.

### Session Control

- running `Ctrl+C` enters `cancelling`.
- running `Ctrl+C` writes `cancelled/user_cancel_running` fact.
- running `Ctrl+C` returns REPL/TUI to input.
- running `Ctrl+C` leaves session/run lifecycle `running`.
- running `Ctrl+C` keeps active ownership.
- idle `Ctrl+C` writes `cancelled/user_cancel_idle` fact.
- idle `Ctrl+C` terminalizes session/run.
- idle `Ctrl+C` releases active ownership.
- `/exit` terminalizes eligible idle session and releases ownership.
- double interrupt while `cancelling` does not accept partial state.

### Resume

- eligible terminalized REPL prompt session resumes into REPL.
- eligible one-shot terminal prompt session resumes into REPL.
- resume preserves `session_id`.
- resume preserves `run_id`.
- resume writes `session_resumed` and `run_resumed`.
- resume reacquires active ownership before lifecycle revival.
- resume restores durable conversation projection.
- resume restores Todo Plan for same run.
- resume restores approval mode and session-scoped grants.
- resume restores active skill snapshot references.
- resume rejects running session.
- resume rejects idle non-terminal session.
- resume rejects startup/config/schema failure session.
- resume rejects missing checkpoint.
- resume rejects non-terminal checkpoint kind.
- resume rejects invalid checkpoint checksum.
- resume rejects invalid durable conversation cut.
- store/API paths other than explicit resume reject terminal-to-running
  transition.

### Provider And Shell Cancellation

- adapter public contract remains `run()` / `stream()`.
- fake async adapter observes cancellation handle and returns normalized
  cancelled result.
- sync fallback reports `cancelling` without claiming remote stop.
- stream tokens shown before cancellation are not accepted as final output.
- active shell process receives best-effort terminate request.
- shell cancellation returns normalized `tool_error/shell_cancelled` or
  `cancelled/shell_cancel_requested` according to boundary.
- partial shell output is included only after command-runner boundary closes.
- shell cancellation writes no terminal recovery checkpoint by itself.

### Stale Fail-Close

- active owner that appears alive blocks startup/resume.
- insufficient stale evidence blocks startup/resume.
- non-interactive missing confirmation blocks startup/resume.
- interactive confirmed proven-stale owner can be terminalized.
- confirmed stale fail-close releases ownership.
- stale fail-close writes audit events.
- stale fail-close writes terminal checkpoint only when durable facts are
  sufficient and session is checkpoint-eligible.
- stale fail-close never attaches to stale session.
- stale fail-close never resumes stale session.

### Retry And Output Token Continuation

- retry rule registry rejects unknown rule reason.
- unregistered ordinary tool failure is not retried.
- shell timeout is not retried by default.
- registered retry-safe transient runtime failure uses bounded `repeat_call`.
- retry exhaustion returns to ordinary error handling.
- output token limit maps to `model_error/output_token_limit_reached`.
- partial assistant output is not accepted before continuation succeeds.
- incomplete tool call from partial output is not executed.
- successful continuation appends one final accepted assistant output.
- continuation attempts are bounded and audited.

### Shell Timeout

- omitted `shell_exec.timeout_seconds` uses configured default.
- explicit timeout below maximum is honored.
- explicit timeout above maximum is rejected.
- explicit timeout is not silently capped by default.
- invalid default timeout fails startup config.
- invalid maximum timeout fails startup config.
- shell timeout writes normalized `tool_error/shell_timeout`.
- shell timeout best-effort terminates process.

## Integration Tests

- one-shot session completes enough accepted conversation, terminalizes, and
  resumes into REPL with same session/run lineage.
- REPL session with Todo Plan and approval grant terminalizes and resumes with
  both restored.
- running cancellation during fake model call returns to REPL input and later
  idle terminalization produces a resumable terminal checkpoint.
- running cancellation during fake shell command terminates the subprocess,
  records cancellation fact, and keeps session active.
- startup skill snapshot failure after session creation terminalizes without
  checkpoint and resume rejects it.
- legacy Phase 2 database fails closed for startup, status, trace, ownership,
  and resume.
- stale ownership conflict with insufficient proof blocks; with fixture-proven
  stale proof and confirmation, startup/resume proceeds after fail-close.

## Manual Verification

Manual verification is required for TTY behaviors that are not reliably covered
by automated tests:

- running `Ctrl+C` while model call is visibly active.
- running `Ctrl+C` while `shell_exec` is visibly active.
- idle `Ctrl+C` terminalization and ownership release.
- `debug-agent resume <session_id>` into interactive REPL.
- stale fail-close confirmation prompt.
- double `Ctrl+C` presentation while cancelling.

Manual verification must record:

- terminal application used.
- command sequence.
- expected result.
- observed result.
- session id and run id.
- relevant trace/status excerpts.
- any known limitation.

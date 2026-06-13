# Phase 3 Scope

## Goal

Phase 3 delivers lightweight session control, failure control, and recovery for
prompt sessions.

The phase closes the control-plane gaps that prevent long debugging sessions
from being safely interrupted, terminalized, audited, and resumed from durable
runtime truth:

- running turn interruption and best-effort provider/shell cancellation.
- idle prompt session terminalization and active ownership release.
- terminal-checkpoint-backed runtime context restore for eligible prompt
  sessions, including long-lived REPL prompt sessions and one-shot terminal
  prompt sessions resumed into REPL.
- normalized runtime error taxonomy and fixed reason registry.
- durable failure/cancellation facts and terminal recovery checkpoint policy.
- narrow runtime retry and `output_token_limit_reached` continuation.
- shell timeout config cleanup.
- user-confirmed stale running session fail-close when ownership is blocked by
  a proven-stale owner.

Phase 3 extends the Phase 2 runtime, persistence, CLI, REPL/TUI, ToolBroker,
Prompt Agent Runtime, and `ModelContextFrame` contracts. It remains a light
control plane. It does not add workflow runtime, background tasks, subagents,
MCP, plugins, PTY shell, long-running shell runtime, or generic step-level
retry.

## Must Implement

- Compatibility:
  - set `PHASE_3_SCHEMA_USER_VERSION = 3` and write SQLite
    `PRAGMA user_version = 3` for fresh Phase 3 databases.
  - before startup, active ownership checks, `status`, `trace`, or `resume`
    interpret runtime truth, read SQLite `PRAGMA user_version` when
    `.sessions/runtime.db` exists.
  - for startup only, where startup means a REPL or one-shot command path that
    will create a new session/run, delete an existing `.sessions/runtime.db`
    whose `PRAGMA user_version` is missing/`0` or legacy Phase 0/0.5/1/2 before
    interpreting any rows, then create a fresh Phase 3 database.
  - startup legacy reset deletes only the legacy `.sessions/runtime.db`.
    Orphaned legacy artifact, log, trace, checkpoint-payload, or session
    subdirectories under `.sessions/` may remain on disk for manual cleanup, but
    Phase 3 runtime must not interpret them as runtime truth and the fresh
    Phase 3 database must not reference them.
  - for `status`, `trace`, `resume`, an existing runtime database with missing
    schema version/`0`, unknown future schema versions, or any non-startup schema
    mismatch, fail closed before interpreting runtime truth.
  - if `.sessions/runtime.db` does not exist, `status`, `trace`, and `resume`
    must not create it. `status` returns a read-only no-session observation;
    `trace <session_id>` and `resume <session_id>` return lookup-not-found.
  - do not migrate, reinterpret, preserve, or rewrite legacy rows into Phase 3
    shape.
  - classify fail-closed schema-version failures internally as
    `config_error/{legacy_schema_version,unknown_schema_version,schema_version_missing}`
    while mapping the CLI boundary to `ERROR_STARTUP_PERSISTENCE`, with
    user-facing guidance. Startup legacy reset is not a schema-version failure
    session because runtime deletes the legacy DB before interpreting rows or
    creating Phase 3 runtime truth.
  - treat Phase 3 normalized tool argument validation as a tool-contract
    breaking change from Phase 2: malformed or locally invalid model-visible
    `view_image` and `todo` calls use `tool_error/tool_schema_invalid`, not the
    earlier Phase 2 `user_error` expectation.
- Normalized errors:
  - define centralized `error_class` and `reason` symbols.
  - persist normalized failure objects at `payload.error` for failure-class
    events.
  - use a narrow model-visible error projection.
  - introduce semantic CLI exit codes for Phase 3 runtime boundaries.
- Durable conversation:
  - add append-only `conversation_messages` as durable accepted conversation
    fact truth.
  - add mutable current conversation projection state for each prompt run.
  - demote process-local in-memory conversation to a projection of durable facts
    plus runtime-owned injections.
  - persist accepted user, assistant, tool, failure, and cancellation
    model-visible message groups at recovery boundaries.
  - forbid pending or speculative model/tool/provider state from becoming
    durable conversation truth.
- Terminal recovery checkpoints:
  - make terminal recovery checkpoints the only resume entrypoints.
  - make `terminal_recovery` the only checkpoint kind written for Phase 3
    prompt sessions/runs.
  - narrow `latest_checkpoint_id` to the latest terminal recovery checkpoint id.
  - remove Phase 3 write paths and fresh-schema storage for ordinary turn,
    context, error, trace, streaming, UI, or other non-terminal provenance
    checkpoints/snapshots for Phase 3 prompt sessions/runs.
  - write terminal recovery checkpoints for eligible terminalization paths only
    after durable facts are self-consistent.
  - include a compact recovery manifest referencing a verified durable
    conversation fact cut, checkpoint-frozen projection snapshot, and
    runtime-owned state.
- Startup/config/schema failure handling:
  - startup/config/schema failures are non-resumable.
  - if such a failure occurs after session/run creation, write only normalized
    audit failure facts/events, terminalize session/run, and do not write a
    terminal recovery checkpoint.
  - mark startup/config/schema failure sessions with a structured
    non-resumable startup-failure lifecycle or terminal metadata marker before
    releasing ownership or returning the startup error.
  - `debug-agent resume <session_id>` must fail closed for these sessions.
- Session control:
  - distinguish running turn interruption from idle session terminalization.
  - running `Ctrl+C` or `Esc` cancels the current turn, persists a
    cancellation/failure fact when it reaches a recovery boundary, and returns
    REPL/TUI to input.
  - define frozen `[execution].cancellation_timeout_seconds` as the local cleanup
    envelope for accepted running interruptions; invalid values are startup
    config failures.
  - idle `Ctrl+C` or `Esc` terminalizes session/run, writes a terminal recovery
    checkpoint, releases active ownership, and exits the interactive loop.
  - while cancellation cleanup is already in progress, controllers must block
    all user input, including `Ctrl+C` and `Esc`; runtime waits for the cleanup
    envelope and uses the timeout fail-closed behavior if local boundaries do
    not close.
  - graceful `/exit` and normal shutdown terminalize eligible idle prompt
    sessions with terminal recovery checkpoints and release ownership.
  - explicit `debug-agent resume <session_id>` may revive the same eligible
    terminalized session/run lineage to `running`; no other path may revive a
    terminalized session or run.
- Provider cancellation:
  - preserve the public `AgentLoopAdapter.run()` / `stream()` contract.
  - run main model calls and `view_image` provider calls through runtime-owned
    cancellable workers.
  - ignore late provider results after local cancellation is accepted.
  - do not append late results to durable conversation or accepted assistant /
    tool-call output, and do not accept late `view_image` provider results as
    tool results.
  - do not promise remote execution stopped or provider billing stopped.
- Shell cancellation:
  - terminate active `shell_exec` subprocesses on running interruption using
    best-effort local process termination.
  - never treat shell mid-flight state as resumable truth.
- One-shot and REPL lifecycle:
  - run one-shot prompt execution through the same runtime turn lifecycle shape
    as REPL prompt execution.
  - use the same durable conversation, terminal checkpoint, failure, approval,
    Todo Plan, and resume eligibility rules for one-shot and REPL prompt runs.
  - allow eligible one-shot terminal prompt sessions to resume into REPL.
  - keep successful one-shot stdout stable as the final accepted assistant
    output only; do not add default streaming, intermediate model text, tool
    progress, or runtime event output to one-shot stdout or stderr.
  - when one-shot exits with a terminal failure after a session/run exists,
    print this stable terminal failure summary to stderr:
    `One-shot session <session_id> failed.`,
    `<error_class>/<reason>: <message>`,
    `trace: debug-agent trace <session_id>`, and
    `resume: debug-agent resume <session_id>`.
- Stale running session fail-close:
  - only a user-triggered startup or resume workflow may attempt stale
    fail-close when active ownership blocks progress.
  - require proven-stale evidence and explicit user confirmation before
    terminalizing the old session/run and releasing ownership.
  - use `owner_token` fencing so fail-close and ownership release can affect only
    the exact active owner record that was proven stale.
  - write a terminal recovery checkpoint only when durable facts are sufficient
    and the stale prompt session/run is checkpoint-eligible; otherwise
    terminalize the old session/run as non-resumable without a checkpoint.
  - terminalize the old session/run as `failed` with terminal reason
    `terminal_stale`, and write one minimal administrative
    `stale_fail_closed` run event.
  - do not write a normalized error fact or durable conversation
    failure/cancellation fact for stale fail-close.
  - allow `debug-agent resume <session_id>` targeting the current stale active
    owner to run a stale-target fail-close pre-step, then continue ordinary
    terminal-checkpoint-backed resume only if that pre-step produced a valid
    terminal recovery checkpoint and released ownership.
  - fail closed if the owner is still alive, stale evidence is insufficient, or
    confirmation cannot be obtained.
- Retry and output-token continuation:
  - define a central opt-in retry rule registry.
  - support only `repeat_call` for explicitly retry-safe runtime-owned
    transient failures and `continue_generation` for
    `output_token_limit_reached`.
  - restrict Phase 3 `continue_generation` to text-only partial output.
  - route partial output containing any complete or partial tool-call fragment
    to ordinary failure handling instead of continuation.
  - do not accept partial output as final assistant output.
  - do not execute incomplete tool calls or incomplete tool arguments.
- Shell timeout cleanup:
  - split default shell timeout from maximum shell timeout.
  - freeze `default_shell_timeout_seconds`, `max_shell_timeout_seconds`, and
    `cancellation_timeout_seconds` from `[execution]` into the session config
    snapshot.
  - make explicit `shell_exec.timeout_seconds` mean the requested timeout,
    validated against the configured maximum, not a value silently capped by the
    default.

## Must Not Implement

- `/cancel`.
- non-terminal session attach.
- startup/config/schema failure resume.
- automatic stale ownership cleanup without user confirmation.
- auto attach, auto resume, or unconfirmed release of active ownership.
- generic step-level retry.
- default runtime-level automatic tool retry.
- accepted or completed model-call result replay.
- token-level resume.
- tool-mid-flight resume.
- shell-mid-flight resume.
- provider mid-flight resume.
- subagent cancellation.
- workflow runtime, workflow handoff, or workflow resume.
- background task system.
- PTY shell, interactive terminal, or long-running shell runtime.
- MCP server lifecycle, MCP tool discovery, or MCP tool invocation.
- plugin packaging.
- shader-specific runtime validators, RenderDoc command allowlists, Ralph Loop
  state machines, or business report schemas.

## Phase 3 Runtime Contract Additions

Phase 3 adds normalized error payloads and fixed reason symbols as runtime
truth. Error classes and reasons are control-plane symbols, not presentation
strings.

Phase 3 adds append-only durable conversation rows. During ordinary execution,
process-local conversation is the active projection maintained alongside
durable appends and projection-state updates. Explicit resume is the only path
that rebuilds process-local conversation from durable rows and the
checkpoint-frozen projection snapshot. Process-local conversation, stream
observations, TUI state, and trace rendering are not recovery truth. Phase 3
prompt sessions/runs do not write context snapshots; if legacy or corrupt
databases contain them, they must not be used as recovery truth.
For accepted durable model-visible messages included in the checkpoint-frozen
projection, resume must preserve provider-visible equivalence with ordinary
non-resume projection. This equivalence applies to accepted durable truth only,
not to partial provider output, stream deltas, pending tool results, approval
drafts, or provider/tool/shell mid-flight state.

Phase 3 changes checkpoint semantics. A prompt session/run can be resumed only
from a terminal recovery checkpoint, and Phase 3 prompt sessions/runs do not
write non-terminal checkpoint/provenance records. Ordinary turn, context, error,
stream, UI, or trace facts remain audit/event or projection facts only; they are
not checkpoint records.

Phase 3 changes terminal state semantics for one explicit path. A terminalized
eligible prompt session/run may return to `running` only through
`debug-agent resume <session_id>`. The resume path preserves the same
`session_id` and `run_id`, writes resume audit events, reacquires active
ownership, and leaves prior terminal facts and checkpoints intact.

Phase 3 does not make terminal state generally reversible. Store helpers,
startup flows, status/trace reads, stale fail-close, and non-resume runtime
paths must still treat terminal session/run rows as terminal.

Phase 3 adds administrative audit event kind `stale_fail_closed` for
user-confirmed stale fail-close of an old owner run. This event is not a
failure-class event and must not carry `payload.error`.

## Compatibility

Phase 3 is a schema, checkpoint, conversation, event payload, retry metadata,
and status/control semantics breaking change from Phase 2.

Runtime initialization, `debug-agent status`, `debug-agent trace`,
`debug-agent resume`, and active workspace ownership checks must read SQLite
`PRAGMA user_version` before interpreting runtime truth rows.

Phase 3 identifies its SQLite schema with:

```text
PHASE_3_SCHEMA_USER_VERSION = 3
```

Fresh Phase 3 databases must write:

```sql
PRAGMA user_version = 3;
```

During startup only, where startup means a REPL or one-shot command path that
will create a new session/run, a missing (`0`) or legacy Phase 0, Phase 0.5,
Phase 1, or Phase 2 schema version is handled by deleting
`.sessions/runtime.db` before interpreting any legacy rows, then creating a
fresh Phase 3 database. This is a destructive Phase 3 schema reset, not
migration. Runtime must not copy, reinterpret, or rewrite legacy rows into the
fresh database.

`debug-agent status`, `debug-agent trace`, and `debug-agent resume` remain
non-destructive observation/recovery commands. When `.sessions/runtime.db`
exists, they must fail closed for missing (`0`), legacy, unknown, or otherwise
mismatched schema versions before interpreting runtime truth rows.

Unknown future schema versions always fail closed; Phase 3 must not delete
databases whose version is newer or not recognized as a legacy Phase 0/0.5/1/2
schema.

If `.sessions/runtime.db` does not exist, Phase 3 startup paths that create a new
REPL or one-shot session create it with the Phase 3 schema and write the Phase 3
schema user version before interpreting runtime rows. Read-only or recovery
commands, including `status`, `trace`, and `resume`, must not create a missing
database.

The startup user-facing legacy-schema reset message must say that older runtime
databases are unsupported by Phase 3, the old runtime database was deleted, a
fresh Phase 3 database was created, and legacy artifact/log/trace files may
remain on disk but are not interpreted by the fresh Phase 3 runtime. The
`status`, `trace`, and `resume` legacy-schema error must say that older runtime
databases are unsupported by Phase 3 and instruct the user to start a new
session or use a fresh workspace.

## ADR Impact

Phase 3 adds:

- ADR 0014 for terminal recovery checkpoints and durable conversation.
- ADR 0015 for normalized error taxonomy and narrow runtime retry.

Phase 3 refines:

- ADR 0003 by restricting resume recovery to terminal recovery checkpoints.
- ADR 0010 by making durable conversation rows the authoritative conversation
  source used to build model-visible context.
- ADR 0011 by stopping Phase 3 prompt-session context snapshot writes and by
  requiring resumable context summaries to be persisted as durable conversation
  messages.
- ADR 0013 by restoring Todo Plan for the same resumed run lineage, not from
  conversation, summary, trace, or UI state.

## Minimum Runnable Slice

1. User starts a REPL or one-shot session in a fresh workspace.
2. Runtime initializes the Phase 3 database, validates schema version, freezes
   config/policy/skills/tool availability, and creates prompt session/run state.
3. Runtime appends accepted conversation messages to `conversation_messages`.
4. A running turn interrupted by `Ctrl+C` or `Esc` records a turn-scoped
   cancellation fact and returns REPL/TUI to input without terminalizing the
   session.
5. Idle `Ctrl+C`, idle `Esc`, or `/exit` terminalizes the prompt session/run,
   writes a terminal recovery checkpoint, and releases active workspace
   ownership.
6. `debug-agent resume <session_id>` validates eligibility, verifies the
   terminal checkpoint, durable conversation fact cut, and checkpoint-frozen
   projection snapshot, reacquires ownership, revives the same session/run
   lineage to `running`, and starts REPL with restored runtime context.
7. Startup/config/schema failure after session/run creation writes normalized
   audit facts and terminalizes without a terminal recovery checkpoint; resume
   rejects it.
8. Active ownership blockage caused by a proven-stale owner can be fail-closed
   only after user confirmation.
9. `output_token_limit_reached` continuation completes final assistant output
   without accepting partial output or executing incomplete tool calls.

## Completion Definition

Phase 3 spec work is ready for implementation planning when:

- all Phase 3 docs except `implementation-plan.md` have been reviewed and
  accepted.
- open spec TODOs are either resolved or explicitly accepted as non-blocking
  implementation notes.
- `implementation-plan.md` is then created and approved before implementation
  work starts.

Phase 3 implementation is complete only when:

- all Phase 3 acceptance criteria in `tests.md` pass.
- `operations.md` canonical verification commands have been run as applicable.
- legacy Phase 2 databases are deleted and replaced with a fresh Phase 3
  database on startup, while `status`, `trace`, and `resume` fail closed before
  interpreting legacy rows.
- startup/config/schema failure sessions are proven non-resumable.
- terminal-checkpoint-backed resume restores eligible REPL and one-shot prompt
  sessions into REPL using the same session/run lineage.

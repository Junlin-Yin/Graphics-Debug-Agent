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
  - bump SQLite `PRAGMA user_version` for Phase 3.
  - fail closed for missing, legacy, unknown, or non-Phase-3 schema versions
    before startup, active ownership checks, `status`, `trace`, or `resume`
    interpret runtime truth.
  - do not migrate, delete, or rewrite old `.sessions/runtime.db`.
  - classify schema-version failure as startup persistence/config failure with
    normalized error payload and user-facing cleanup guidance.
- Normalized errors:
  - define centralized `error_class` and `reason` symbols.
  - persist normalized failure objects at `payload.error` for failure-class
    events.
  - use a narrow model-visible error projection.
  - introduce semantic CLI exit codes for Phase 3 runtime boundaries.
- Durable conversation:
  - add append-only `conversation_messages` as durable conversation truth.
  - demote process-local in-memory conversation to a projection.
  - persist accepted user, assistant, tool, failure, and cancellation
    model-visible message groups at recovery boundaries.
  - forbid pending or speculative model/tool/provider state from becoming
    durable conversation truth.
- Terminal recovery checkpoints:
  - make terminal recovery checkpoints the only resume entrypoints.
  - narrow `latest_checkpoint_id` to the latest terminal recovery checkpoint id.
  - forbid ordinary turn, context, error, trace, streaming, or UI provenance
    from writing resume checkpoints.
  - write terminal recovery checkpoints for eligible terminalization paths only
    after durable facts are self-consistent.
  - include a compact recovery manifest referencing a verified durable
    conversation cut and runtime-owned state.
- Startup/config/schema failure handling:
  - startup/config/schema failures are non-resumable.
  - if such a failure occurs after session/run creation, write only normalized
    audit failure facts/events, terminalize session/run, and do not write a
    terminal recovery checkpoint.
  - `debug-agent resume <session_id>` must fail closed for these sessions.
- Session control:
  - distinguish running turn interruption from idle session terminalization.
  - running `Ctrl+C` cancels the current turn, persists a cancellation/failure
    fact when it reaches a recovery boundary, and returns REPL/TUI to input.
  - idle `Ctrl+C` terminalizes session/run, writes a terminal recovery
    checkpoint, releases active ownership, and exits the interactive loop.
  - graceful `/exit` and normal shutdown terminalize eligible idle prompt
    sessions with terminal recovery checkpoints and release ownership.
  - explicit `debug-agent resume <session_id>` may revive the same eligible
    terminalized session/run lineage to `running`; no other path may revive a
    terminalized session or run.
- Provider cancellation:
  - preserve the public `AgentLoopAdapter.run()` / `stream()` contract.
  - allow adapters to use internal async provider tasks and runtime-owned
    cancellation handles.
  - represent sync fallback or uncertain provider cancellation as
    `cancelling`; do not promise remote execution stopped or provider billing
    stopped.
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
- Stale running session fail-close:
  - only a user-triggered startup or resume workflow may attempt stale
    fail-close when active ownership blocks progress.
  - require proven-stale evidence and explicit user confirmation before
    terminalizing the old session/run and releasing ownership.
  - fail closed if the owner is still alive, stale evidence is insufficient, or
    confirmation cannot be obtained.
- Retry and output-token continuation:
  - define a central opt-in retry rule registry.
  - support only `repeat_call` for explicitly retry-safe runtime-owned
    transient failures and `continue_generation` for
    `output_token_limit_reached`.
  - do not accept partial output as final assistant output.
  - do not execute incomplete tool calls or incomplete tool arguments.
- Shell timeout cleanup:
  - split default shell timeout from maximum shell timeout.
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

Phase 3 adds append-only durable conversation rows. The runtime builds
process-local conversation from durable rows and runtime-owned injections.
Process-local conversation, stream observations, TUI state, trace rendering, and
context snapshots are not recovery truth.

Phase 3 changes checkpoint semantics. A prompt session/run can be resumed only
from a terminal recovery checkpoint. Ordinary turn, context, error, stream, UI,
or trace records may remain useful for audit and inspection, but they are not
resume checkpoints.

Phase 3 changes terminal state semantics for one explicit path. A terminalized
eligible prompt session/run may return to `running` only through
`debug-agent resume <session_id>`. The resume path preserves the same
`session_id` and `run_id`, writes resume audit events, reacquires active
ownership, and leaves prior terminal facts and checkpoints intact.

Phase 3 does not make terminal state generally reversible. Store helpers,
startup flows, status/trace reads, stale fail-close, and non-resume runtime
paths must still treat terminal session/run rows as terminal.

## Compatibility

Phase 3 is a schema, checkpoint, conversation, event payload, retry metadata,
and status/control semantics breaking change from Phase 2.

Runtime initialization, `debug-agent status`, `debug-agent trace`,
`debug-agent resume`, and active workspace ownership checks must read SQLite
`PRAGMA user_version` before interpreting runtime truth rows. A missing (`0`),
Phase 0, Phase 0.5, Phase 1, Phase 2, unknown, or otherwise mismatched version
fails closed with a normalized startup persistence/config error.

If `.sessions/runtime.db` does not exist, Phase 3 creates it with the Phase 3
schema and writes the Phase 3 schema user version before interpreting runtime
rows.

The user-facing legacy-schema error must say that older runtime databases are
unsupported by Phase 3 and instruct the user to move or remove `.sessions/` or
use a fresh workspace.

Runtime must not automatically migrate, delete, or rewrite legacy databases.

## ADR Impact

Phase 3 adds:

- ADR 0014 for terminal recovery checkpoints and durable conversation.
- ADR 0015 for normalized error taxonomy and narrow runtime retry.

Phase 3 refines:

- ADR 0003 by restricting resume recovery to terminal recovery checkpoints.
- ADR 0010 by making durable conversation rows the authoritative conversation
  source used to build model-visible context.
- ADR 0011 by keeping context snapshots and compression summaries out of
  recovery truth unless the model-visible summary is already persisted as a
  durable conversation message.
- ADR 0013 by restoring Todo Plan for the same resumed run lineage, not from
  conversation, summary, trace, or UI state.

## Minimum Runnable Slice

1. User starts a REPL or one-shot session in a fresh workspace.
2. Runtime initializes the Phase 3 database, validates schema version, freezes
   config/policy/skills/tool availability, and creates prompt session/run state.
3. Runtime appends accepted conversation messages to `conversation_messages`.
4. A running turn interrupted by `Ctrl+C` records a turn-scoped cancellation
   fact and returns REPL/TUI to input without terminalizing the session.
5. Idle `Ctrl+C` or `/exit` terminalizes the prompt session/run, writes a
   terminal recovery checkpoint, and releases active workspace ownership.
6. `debug-agent resume <session_id>` validates eligibility, verifies the
   terminal checkpoint and durable conversation cut, reacquires ownership,
   revives the same session/run lineage to `running`, and starts REPL with
   restored runtime context.
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
- legacy Phase 2 databases fail closed with the Phase 3 compatibility error.
- startup/config/schema failure sessions are proven non-resumable.
- terminal-checkpoint-backed resume restores eligible REPL and one-shot prompt
  sessions into REPL using the same session/run lineage.

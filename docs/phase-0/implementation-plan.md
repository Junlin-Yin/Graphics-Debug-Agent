# Phase 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This is an implementation-process instruction only. Phase 0 runtime itself does not implement subagents.

**Goal:** Build the minimal runtime slice that can run CLI one-shot and REPL sessions while persisting session/run/event/checkpoint/artifact state.

**Architecture:** Runtime owns all session truth. LangChain is only called through `AgentLoopAdapter`. SQLite stores metadata and audit facts; filesystem stores logs, artifacts, temp files, and trace output.

**Tech Stack:** Python, SQLite, local filesystem, LangChain-compatible chat model, pytest.

---

## File Structure To Create

- `src/debug_agent/runtime/contracts.py`: dataclasses/enums for Session, Run, RunEvent, Checkpoint, Artifact, ToolResult.
- `src/debug_agent/persistence/sqlite.py`: SQLite connection and migration bootstrap.
- `src/debug_agent/persistence/sessions.py`: session CRUD and active workspace ownership.
- `src/debug_agent/persistence/runs.py`: run lifecycle and status transitions.
- `src/debug_agent/persistence/events.py`: append-only run event writer.
- `src/debug_agent/persistence/checkpoints.py`: checkpoint save/load.
- `src/debug_agent/persistence/artifacts.py`: per-session artifact root and artifact registry.
- `src/debug_agent/tools/broker.py`: ToolBroker policy, invocation, audit event writing.
- `src/debug_agent/tools/native_readonly.py`: `read_file`, `list_dir`, `search_text`, `git_status`.
- `src/debug_agent/adapters/model_factory.py`: config snapshot to model instance.
- `src/debug_agent/adapters/langchain_adapter.py`: LangChain AgentLoopAdapter.
- `src/debug_agent/runtime/prompt_executor.py`: prompt turn execution.
- `src/debug_agent/runtime/orchestrator.py`: session/run orchestration.
- `src/debug_agent/runtime/workspace.py`: workspace root resolution.
- `src/debug_agent/runtime/config.py`: global config loading, built-in defaults, and config snapshot creation.
- `src/debug_agent/observability/trace_writer.py`: trace rendering.
- `src/debug_agent/cli/main.py`: command entrypoint.
- `src/debug_agent/cli/repl.py`: REPL loop and slash command handling.

## Milestone 1: Runtime Contracts And SQLite Bootstrap

- [x] Define contract dataclasses and enums.
- [x] Implement workspace root resolution: git worktree root when available, otherwise current working directory.
- [x] Implement config loading from `~/.debug-agent/config.toml` when present.
- [x] Implement built-in Phase 0 defaults for timeout, temperature, token limits, and system prompt when `config.toml` is absent.
- [x] Return `config_error` if provider/model cannot be resolved from config or environment; Phase 0 does not guess a default provider/model.
- [x] Create SQLite migration for `sessions`, `runs`, `run_events`, `checkpoints`, `artifacts`.
- [x] Add unit tests for schema creation, contract serialization, workspace resolution, and config loading/defaults.
- [x] Verify with `uv run pytest tests/unit/persistence -v`.

Runnable state: database can be initialized and empty stores can be constructed.

## Milestone 2: Session, Run, Event, Checkpoint, Artifact Stores

- [x] Implement session creation and active workspace ownership check.
- [x] Implement run creation and Phase 0 status transitions: `running -> completed` and `running -> failed`.
- [x] Implement append-only event writer.
- [x] Implement checkpoint save/load.
- [x] Implement artifact registration and path resolution.
- [x] Add unit tests for ownership rejection, ownership release after completed/failed, append-only events, checkpoint restore, artifact registry.

Runnable state: a fake session/run lifecycle can be persisted without model calls.

## Milestone 3: ToolBroker And Read-Only Native Tools

- [x] Implement ToolBroker invocation boundary.
- [x] Implement read-only native tools: `read_file`, `list_dir`, `search_text`, `git_status`.
- [x] Ensure each invocation returns `ToolResult`.
- [x] Ensure each invocation writes a tool audit event.
- [x] Add tests for allow, deny, path validation, timeout, and audit event writes.

Runnable state: runtime can execute read-only tools only through ToolBroker.

## Milestone 4: Agent Adapter And Prompt Executor

- [x] Implement `ModelFactory` with one stable LangChain-compatible provider path.
- [x] Use the hardcoded Phase 0 default system prompt: `You are debug-agent, a local debugging assistant. Answer concisely and use only tools exposed by the runtime.`
- [x] Implement `LangChainAgentLoopAdapter`.
- [x] Implement `PromptAgentExecutor` with fake model tests first.
- [x] Add tests proving LangChain does not own session/run/checkpoint state.

Runnable state: fake model one-shot run can produce an assistant response and checkpoint.

## Milestone 5: CLI One-Shot

- [x] Implement `debug-agent -p "..."`.
- [x] Create session and prompt run for one-shot.
- [x] Persist lifecycle events and final checkpoint.
- [x] Print assistant output.
- [x] Add integration smoke test using fake model.

Runnable state: `debug-agent -p "hello"` completes with exit code `0`.

## Milestone 6: REPL And Slash Commands

- [x] Implement `debug-agent` REPL.
- [x] Implement local `/status`.
- [x] Implement local `/exit`.
- [x] Reject non-slash input while a run is actively executing.
- [x] Add REPL smoke test with two turns and `/exit`.

Runnable state: REPL can hold a long-lived prompt run and shut down cleanly.

## Milestone 7: Status, Trace, And Logs

- [x] Implement `debug-agent status <session_id>`.
- [x] Implement `debug-agent trace <session_id>`.
- [x] Write `engine.log` for lifecycle, model, tool, checkpoint, error events.
- [x] Render `trace.md` from run events and artifact metadata on terminal session state and on explicit trace command when missing or stale.
- [x] Add integration tests for status and trace output fields.

Runnable state: a completed one-shot or REPL session is inspectable after process exit.

## Milestone 8: Phase 0 Acceptance Pass

- [x] Run unit tests.
- [x] Run integration tests.
- [x] Run one-shot smoke test.
- [x] Run REPL smoke test.
- [x] Verify same-workspace active session rejection.
- [x] Verify no skill/subagent/workflow/MCP/plugin command is required or exposed for Phase 0.

Runnable state: Phase 0 meets `docs/phase-0/scope.md` completion definition.

## Milestone 9: Cancellation, Timeout, And Terminal Failure Closure

- [x] Add tests proving REPL Ctrl+C after session creation records session and run as `failed`.
- [x] Ensure Ctrl+C failure uses error class `cancelled`.
- [x] Ensure Ctrl+C writes an `error` checkpoint when session/run state exists.
- [x] Ensure Ctrl+C releases workspace active ownership.
- [x] Add tests proving mid-call model cancellation records terminal `failed/cancelled` state and releases ownership.
- [x] Add tests proving model timeout records terminal `failed/timeout` state and releases ownership.
- [x] Implement the minimal runtime cancellation/failure handling required by those tests.
- [x] Verify with `uv run pytest tests/unit/cli tests/unit/runtime tests/integration -v`.

Runnable state: observed cancellation or timeout never leaves a Phase 0 session or run stuck in `running`.

## Milestone 10: ToolBroker Timeout And Runtime Config Closure

- [x] Add a regression test proving ToolBroker returns a timeout result without waiting for the full tool handler duration.
- [x] Fix ToolBroker timeout execution so `ToolResult(status="timeout")` returns promptly and writes `tool_call_failed`.
- [x] Add tests proving runtime config `timeout_seconds` is passed into ToolBroker invocation context.
- [x] Ensure one-shot and REPL tool calls both honor the frozen session config timeout.
- [x] Verify with `uv run pytest tests/unit/tools tests/unit/adapters tests/unit/runtime -v`.

Runnable state: ToolBroker timeout behavior matches `docs/phase-0/specs/toolbroker.md`, including runtime config override.

## Milestone 11: LangChain Tool Binding Closure

- [x] Add adapter tests proving runtime `ToolDefinition` schemas are converted into LangChain-compatible tool callables.
- [x] Ensure generated LangChain tool callables delegate execution only to `ToolBroker.invoke(...)`.
- [x] Ensure generated tool callables do not call native tools directly and do not write audit events directly.
- [x] Keep fake-model tests network-free and deterministic.
- [x] Verify with `uv run pytest tests/unit/adapters tests/unit/tools -v`.

Runnable state: real LangChain-compatible model paths can receive Phase 0 read-only tool definitions through the adapter boundary.

## Milestone 12: Observability And Error Payload Closure

- [x] Add tests proving artifact creation writes an `artifact_registered` run event.
- [x] Ensure large ToolBroker output produces artifact metadata, a ToolResult artifact reference, a tool audit event, and an `artifact_registered` event.
- [x] Add tests proving model call completed events include duration metadata required by trace rendering.
- [x] Add tests proving every error event payload exposes `error_class`, `message`, `source`, and `recoverable` at the event payload level.
- [x] Update trace tests to cover `artifact_registered`, model duration, tool audit duration, terminal failure, and error summary rendering.
- [x] Verify with `uv run pytest tests/unit/observability tests/unit/runtime tests/unit/tools tests/integration -v`.

Runnable state: `engine.log` and `trace.md` expose the Phase 0 observability facts required by `docs/phase-0/specs/observability.md`.

## Milestone 13: Failure Scenario Acceptance Closure

- [x] Add integration coverage for all remaining `docs/phase-0/tests.md` failure scenarios not covered by Milestones 9-12.
- [x] Cover invalid config and config failure behavior, including exit code `4`.
- [x] Cover artifact path missing during trace rendering.
- [x] Cover SQLite migration/bootstrap failure as an explicit surfaced failure, without silent success.
- [x] Re-run reserved command checks to confirm no Phase 1+ command is exposed or required.
- [x] Verify with `uv run pytest tests/integration -v`.

Runnable state: every Phase 0 documented failure scenario has either a passing test or an explicitly documented reason it is not executable in Phase 0.

## Milestone 14: Strict Phase 0 Acceptance Pass

- [x] Run `uv run pytest tests/unit -v`.
- [x] Run `uv run pytest tests/integration -v`.
- [x] Run `uv run pytest -v`.
- [x] Run one-shot smoke with fake model config: `debug-agent -p "hello"`.
- [x] Run status smoke against the created session: `debug-agent status <session_id>`.
- [x] Run trace smoke against the created session: `debug-agent trace <session_id>`.
- [x] Run REPL smoke with fake model config: `hello`, `/status`, `tell me one more thing`, `/exit`.
- [x] Confirm `.sessions/runtime.db` contains session, run, event, checkpoint rows for baseline executions.
- [x] Confirm artifact rows exist only for executions that produce artifacts.
- [x] Confirm no session remains `running` after successful exit, failed execution, timeout, or cancellation.
- [x] Confirm no Phase 1+ feature is required for Phase 0 acceptance.

Runnable state: Phase 0 satisfies `docs/phase-0/scope.md`, all Phase 0 specs, and `docs/phase-0/tests.md`.

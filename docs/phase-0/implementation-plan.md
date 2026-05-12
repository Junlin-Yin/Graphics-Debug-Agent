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

- [ ] Define contract dataclasses and enums.
- [ ] Implement workspace root resolution: git worktree root when available, otherwise current working directory.
- [ ] Implement config loading from `~/.debug-agent/config.toml` when present.
- [ ] Implement built-in Phase 0 defaults for timeout, temperature, token limits, and system prompt when `config.toml` is absent.
- [ ] Return `config_error` if provider/model cannot be resolved from config or environment; Phase 0 does not guess a default provider/model.
- [ ] Create SQLite migration for `sessions`, `runs`, `run_events`, `checkpoints`, `artifacts`.
- [ ] Add unit tests for schema creation, contract serialization, workspace resolution, and config loading/defaults.
- [ ] Verify with `uv run pytest tests/unit/persistence -v`.

Runnable state: database can be initialized and empty stores can be constructed.

## Milestone 2: Session, Run, Event, Checkpoint, Artifact Stores

- [ ] Implement session creation and active workspace ownership check.
- [ ] Implement run creation and Phase 0 status transitions: `running -> completed` and `running -> failed`.
- [ ] Implement append-only event writer.
- [ ] Implement checkpoint save/load.
- [ ] Implement artifact registration and path resolution.
- [ ] Add unit tests for ownership rejection, ownership release after completed/failed, append-only events, checkpoint restore, artifact registry.

Runnable state: a fake session/run lifecycle can be persisted without model calls.

## Milestone 3: ToolBroker And Read-Only Native Tools

- [ ] Implement ToolBroker invocation boundary.
- [ ] Implement read-only native tools: `read_file`, `list_dir`, `search_text`, `git_status`.
- [ ] Ensure each invocation returns `ToolResult`.
- [ ] Ensure each invocation writes a tool audit event.
- [ ] Add tests for allow, deny, path validation, timeout, and audit event writes.

Runnable state: runtime can execute read-only tools only through ToolBroker.

## Milestone 4: Agent Adapter And Prompt Executor

- [ ] Implement `ModelFactory` with one stable LangChain-compatible provider path.
- [ ] Use the hardcoded Phase 0 default system prompt: `You are debug-agent, a local debugging assistant. Answer concisely and use only tools exposed by the runtime.`
- [ ] Implement `LangChainAgentLoopAdapter`.
- [ ] Implement `PromptAgentExecutor` with fake model tests first.
- [ ] Add tests proving LangChain does not own session/run/checkpoint state.

Runnable state: fake model one-shot run can produce an assistant response and checkpoint.

## Milestone 5: CLI One-Shot

- [ ] Implement `debug-agent -p "..."`.
- [ ] Create session and prompt run for one-shot.
- [ ] Persist lifecycle events and final checkpoint.
- [ ] Print assistant output.
- [ ] Add integration smoke test using fake model.

Runnable state: `debug-agent -p "hello"` completes with exit code `0`.

## Milestone 6: REPL And Slash Commands

- [ ] Implement `debug-agent` REPL.
- [ ] Implement local `/status`.
- [ ] Implement local `/exit`.
- [ ] Reject non-slash input while a run is actively executing.
- [ ] Add REPL smoke test with two turns and `/exit`.

Runnable state: REPL can hold a long-lived prompt run and shut down cleanly.

## Milestone 7: Status, Trace, And Logs

- [ ] Implement `debug-agent status <session_id>`.
- [ ] Implement `debug-agent trace <session_id>`.
- [ ] Write `engine.log` for lifecycle, model, tool, checkpoint, error events.
- [ ] Render `trace.md` from run events and artifact metadata on terminal session state and on explicit trace command when missing or stale.
- [ ] Add integration tests for status and trace output fields.

Runnable state: a completed one-shot or REPL session is inspectable after process exit.

## Milestone 8: Phase 0 Acceptance Pass

- [ ] Run unit tests.
- [ ] Run integration tests.
- [ ] Run one-shot smoke test.
- [ ] Run REPL smoke test.
- [ ] Verify same-workspace active session rejection.
- [ ] Verify no skill/subagent/workflow/MCP/plugin command is required or exposed for Phase 0.

Runnable state: Phase 0 meets `docs/phase-0/scope.md` completion definition.

## Milestone 9: Cancellation, Timeout, And Terminal Failure Closure

- [ ] Add tests proving REPL Ctrl+C after session creation records session and run as `failed`.
- [ ] Ensure Ctrl+C failure uses error class `cancelled`.
- [ ] Ensure Ctrl+C writes an `error` checkpoint when session/run state exists.
- [ ] Ensure Ctrl+C releases workspace active ownership.
- [ ] Add tests proving mid-call model cancellation records terminal `failed/cancelled` state and releases ownership.
- [ ] Add tests proving model timeout records terminal `failed/timeout` state and releases ownership.
- [ ] Implement the minimal runtime cancellation/failure handling required by those tests.
- [ ] Verify with `uv run pytest tests/unit/cli tests/unit/runtime tests/integration -v`.

Runnable state: observed cancellation or timeout never leaves a Phase 0 session or run stuck in `running`.

## Milestone 10: ToolBroker Timeout And Runtime Config Closure

- [ ] Add a regression test proving ToolBroker returns a timeout result without waiting for the full tool handler duration.
- [ ] Fix ToolBroker timeout execution so `ToolResult(status="timeout")` returns promptly and writes `tool_call_failed`.
- [ ] Add tests proving runtime config `timeout_seconds` is passed into ToolBroker invocation context.
- [ ] Ensure one-shot and REPL tool calls both honor the frozen session config timeout.
- [ ] Verify with `uv run pytest tests/unit/tools tests/unit/adapters tests/unit/runtime -v`.

Runnable state: ToolBroker timeout behavior matches `docs/phase-0/specs/toolbroker.md`, including runtime config override.

## Milestone 11: LangChain Tool Binding Closure

- [ ] Add adapter tests proving runtime `ToolDefinition` schemas are converted into LangChain-compatible tool callables.
- [ ] Ensure generated LangChain tool callables delegate execution only to `ToolBroker.invoke(...)`.
- [ ] Ensure generated tool callables do not call native tools directly and do not write audit events directly.
- [ ] Keep fake-model tests network-free and deterministic.
- [ ] Verify with `uv run pytest tests/unit/adapters tests/unit/tools -v`.

Runnable state: real LangChain-compatible model paths can receive Phase 0 read-only tool definitions through the adapter boundary.

## Milestone 12: Observability And Error Payload Closure

- [ ] Add tests proving artifact creation writes an `artifact_registered` run event.
- [ ] Ensure large ToolBroker output produces artifact metadata, a ToolResult artifact reference, a tool audit event, and an `artifact_registered` event.
- [ ] Add tests proving model call completed events include duration metadata required by trace rendering.
- [ ] Add tests proving every error event payload exposes `error_class`, `message`, `source`, and `recoverable` at the event payload level.
- [ ] Update trace tests to cover `artifact_registered`, model duration, tool audit duration, terminal failure, and error summary rendering.
- [ ] Verify with `uv run pytest tests/unit/observability tests/unit/runtime tests/unit/tools tests/integration -v`.

Runnable state: `engine.log` and `trace.md` expose the Phase 0 observability facts required by `docs/phase-0/specs/observability.md`.

## Milestone 13: Failure Scenario Acceptance Closure

- [ ] Add integration coverage for all remaining `docs/phase-0/tests.md` failure scenarios not covered by Milestones 9-12.
- [ ] Cover invalid config and config failure behavior, including exit code `4`.
- [ ] Cover artifact path missing during trace rendering.
- [ ] Cover SQLite migration/bootstrap failure as an explicit surfaced failure, without silent success.
- [ ] Re-run reserved command checks to confirm no Phase 1+ command is exposed or required.
- [ ] Verify with `uv run pytest tests/integration -v`.

Runnable state: every Phase 0 documented failure scenario has either a passing test or an explicitly documented reason it is not executable in Phase 0.

## Milestone 14: Strict Phase 0 Acceptance Pass

- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run `uv run pytest -v`.
- [ ] Run one-shot smoke with fake model config: `debug-agent -p "hello"`.
- [ ] Run status smoke against the created session: `debug-agent status <session_id>`.
- [ ] Run trace smoke against the created session: `debug-agent trace <session_id>`.
- [ ] Run REPL smoke with fake model config: `hello`, `/status`, `tell me one more thing`, `/exit`.
- [ ] Confirm `.sessions/runtime.db` contains session, run, event, checkpoint rows for baseline executions.
- [ ] Confirm artifact rows exist only for executions that produce artifacts.
- [ ] Confirm no session remains `running` after successful exit, failed execution, timeout, or cancellation.
- [ ] Confirm no Phase 1+ feature is required for Phase 0 acceptance.

Runnable state: Phase 0 satisfies `docs/phase-0/scope.md`, all Phase 0 specs, and `docs/phase-0/tests.md`.

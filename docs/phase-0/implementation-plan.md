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

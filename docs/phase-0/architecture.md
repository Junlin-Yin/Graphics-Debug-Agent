# Phase 0 Architecture

## Module List

### CLI Entrypoint

Parses commands, starts REPL or one-shot execution, and handles local slash commands. Slash commands are handled locally and never sent to the model.

### Runtime Orchestrator

Creates sessions and runs, enforces workspace ownership, calls executors, records lifecycle events, writes checkpoints, and exposes status/trace query paths.

### PromptAgentExecutor

Runs a prompt turn through `AgentLoopAdapter`, writes model-call events, and returns text output to CLI. It does not own session state.

### AgentLoopAdapter

Framework boundary around LangChain. Phase 0 default implementation is `LangChainAgentLoopAdapter`.

### ModelFactory

Reads the frozen config snapshot and constructs the LangChain-compatible chat model. Phase 0 does not implement a full provider abstraction.

### ToolBroker

The only entry point for native tools. Phase 0 supports read-only tools only: `read_file`, `list_dir`, `search_text`, `git_status`.

### Persistence Services

SQLite stores metadata and audit facts. Filesystem stores artifacts, logs, temp files, and trace output.

## Dependency Direction

```text
CLI
-> RuntimeOrchestrator
-> PromptAgentExecutor
-> AgentLoopAdapter
-> LangChain

RuntimeOrchestrator
-> SessionStore / RunStore / EventWriter / CheckpointStore / ArtifactStore
-> ToolBroker
-> TraceWriter
```

LangChain must not call persistence services directly. Tools must not write events directly; ToolBroker writes tool audit events through runtime services.

## Initialization Order

1. Resolve `workspace_root`: use git worktree root when inside a git worktree; otherwise use current working directory.
2. Load global config from `~/.debug-agent/config.toml` if present.
3. Create immutable Phase 0 config snapshot.
4. Open `.sessions/runtime.db`.
5. Check active session ownership for `workspace_root`.
6. Create session row and session artifact root.
7. Create prompt run.
8. Initialize ToolBroker with read-only native tools.
9. Initialize ModelFactory and LangChainAgentLoopAdapter.
10. Execute REPL or one-shot.

## One-Shot Flow

```text
debug-agent -p "..."
-> CLI parse
-> create session
-> create prompt run
-> write run_started event
-> PromptAgentExecutor.run_turn
-> AgentLoopAdapter.run
-> write assistant_message event
-> write checkpoint
-> mark run completed
-> mark session completed
-> print answer
```

## REPL Flow

```text
debug-agent
-> CLI parse
-> create session
-> create long-lived prompt run
-> wait for input
-> if slash command: handle locally
-> otherwise run one prompt turn through PromptAgentExecutor
-> write event and checkpoint per turn
-> /exit interrupts or completes active run, releases ownership
```

## Status Flow

```text
debug-agent status <session_id>
-> open runtime.db
-> load session
-> load active/latest run
-> load latest checkpoint metadata
-> print status view
```

## Trace Flow

```text
debug-agent trace <session_id>
-> open runtime.db
-> load session, runs, run_events, checkpoints, artifacts
-> render trace.md if stale
-> print trace path plus a short summary
```

`trace.md` is not refreshed after every event. Runtime refreshes it once when a session reaches a terminal state, and `debug-agent trace <session_id>` refreshes it on demand if the file is missing or stale.

Stale detection compares rendered `event_count` and `latest_event_id` metadata with the current persisted event set. Phase 0 does not checksum event payloads or artifact contents for trace freshness.

## Recommended Code Directory Structure

```text
src/debug_agent/
  cli/
    main.py
    repl.py
    output.py
  runtime/
    orchestrator.py
    contracts.py
    prompt_executor.py
    ownership.py
    workspace.py
    config.py
  adapters/
    langchain_adapter.py
    model_factory.py
  tools/
    broker.py
    native_readonly.py
  persistence/
    sqlite.py
    sessions.py
    runs.py
    events.py
    checkpoints.py
    artifacts.py
  observability/
    trace_writer.py
    logging.py
tests/
  unit/
  integration/
```

This structure is a Phase 0 recommendation. It can be adjusted during implementation if the actual package scaffold has an established convention, but module responsibilities must stay separated.

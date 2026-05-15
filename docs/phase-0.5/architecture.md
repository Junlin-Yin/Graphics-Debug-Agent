# Phase 0.5 Architecture

## Module List

### CLI Entrypoint

Selects TUI REPL, plain REPL, or one-shot execution. Slash commands remain local and are never sent to the model.

### ReplController

Coordinates user input, local slash commands, runtime turn execution, stream-event queue draining, timer updates, and view updates.

The controller maps `AgentStreamEvent` into `ReplViewEvent` or view render state before calling the view.

### ReplView

Protocol boundary for REPL presentation. View implementations render controller-provided snapshots and view events. They must not read runtime state directly and must not consume `AgentStreamEvent`.

### PromptToolkitReplView

TUI implementation for interactive TTY sessions. It owns the prompt_toolkit event loop, key bindings, current-session history, input buffer, layout, and redraw scheduling.

### PlainReplView

Fallback implementation for non-TTY, injected I/O, tests, and prompt_toolkit initialization failure. It preserves the Phase 0 plain REPL behavior.

### Optional ReplRuntime

Optional UI-facing facade around existing runtime services. If implemented, it must only adapt existing `RuntimeOrchestrator` and prompt-turn calls for the controller. It must not own Session, Run, Event, Checkpoint, Artifact, ToolBroker, Approval, or Path Policy truth.

### PromptAgentExecutor

Runs prompt turns through `AgentLoopAdapter`. Phase 0.5 adds an optional `agent_stream_callback` parameter. The executor must remain unaware of TUI view types.

### AgentLoopAdapter

Framework boundary around LangChain. Phase 0.5 adds `stream(...)` beside the existing authoritative `run(...)` path.

### Persistence Services

Remain authoritative for runtime audit facts and recovery state. `AgentStreamEvent` is not persisted.

## Dependency Direction

```text
CLI
-> ReplController
-> ReplView

ReplController
-> RuntimeOrchestrator or optional ReplRuntime facade
-> PromptAgentExecutor
-> AgentLoopAdapter
-> LangChain

RuntimeOrchestrator
-> SessionStore / RunStore / EventWriter / CheckpointStore / ArtifactStore
-> ToolBroker
-> TraceWriter
```

Runtime services must not depend on prompt_toolkit, rich, or TUI view classes.

Runtime background work must not call view methods directly. Runtime emits stream observations through a callback; the controller owns queue consumption and view updates.

## Initialization Order

1. Resolve `workspace_root` using the Phase 0 rule.
2. Load global config and create the frozen config snapshot.
3. Open `.sessions/runtime.db`.
4. Check active session ownership for `workspace_root`.
5. Create session row and session artifact root.
6. Create the long-lived prompt run.
7. Initialize ToolBroker with Phase 0 available tools.
8. Initialize ModelFactory and LangChainAgentLoopAdapter.
9. Select REPL view:
   - choose `PromptToolkitReplView` only when stdin and stdout are TTY and no input/output streams are injected.
   - choose `PlainReplView` for non-TTY or injected I/O.
   - if prompt_toolkit initialization fails, choose `PlainReplView` and emit one concise warning.
10. Create `ReplController`.
11. Call `view.run(controller)`.

one-shot mode bypasses this TUI selection path and prints plain stdout.

## TUI REPL Flow

```text
debug-agent
-> CLI parse
-> create session and long-lived prompt run
-> select PromptToolkitReplView
-> ReplController.show_welcome
-> PromptToolkitReplView.run(controller)
-> user submits input
-> controller appends user message and disables input
-> controller starts runtime turn in background thread
-> runtime emits AgentStreamEvent through callback
-> controller drains queue in UI event loop
-> controller maps AgentStreamEvent to ReplViewEvent or render state
-> view renders model text, tool blocks, status, and status bar
-> runtime returns AgentRunResult
-> controller finalizes turn status and reenables input
-> /exit closes the session at the runtime safe boundary
```

## Plain REPL Fallback Flow

```text
debug-agent
-> CLI parse
-> create session and long-lived prompt run
-> select PlainReplView
-> read input from injected or standard input stream
-> handle local slash commands
-> run prompt turn through existing runtime path
-> write plain output to injected or standard output stream
-> /exit closes the session
```

Plain fallback must preserve testability and automation behavior. It must not require prompt_toolkit or terminal control sequences.

## Streaming Turn Flow

```text
UI thread
  prompt_toolkit Application.run()
    - keyboard input
    - layout rendering
    - timer callback
    - queue drain
    - AgentStreamEvent -> ReplViewEvent / render state
    - view redraw

Runtime background thread
  PromptAgentExecutor.run_turn(..., agent_stream_callback=queue_callback)
    - AgentLoopAdapter.stream(...)
    - model-call lifecycle events
    - text delta events
    - tool call events
    - returns AgentRunResult
```

The background thread may call the controller-provided thread-safe wakeup hook after queueing an event. The hook only invalidates the application or schedules a drain. It must not inspect queue contents or mutate view state.

## Recommended Code Directory Structure

```text
src/debug_agent/
  cli/
    repl_controller.py
    repl_view.py
    prompt_toolkit_view.py
    plain_repl_view.py
  runtime/
    prompt_executor.py
    stream_events.py
  adapters/
    langchain_adapter.py
tests/
  unit/
  integration/
```

This structure is a Phase 0.5 recommendation. It can be adjusted during implementation if the actual package scaffold has an established convention, but module responsibilities must stay separated.

# Phase 0.5 Architecture

## Module List

### CLI Entrypoint

Selects TUI REPL, plain REPL, or one-shot execution. Slash commands remain local and are never sent to the model.

### ReplController

Coordinates user input, local slash commands, runtime turn execution, stream-event queue draining, timer updates, and view updates.

The controller maps `AgentStreamEvent` into `ReplViewEvent`, snapshots, or direct view method calls before calling the view.

The controller owns the active-turn command boundary. During active execution,
slash-command entry points must not execute `/status`, `/exit`, unsupported
command output, or runtime state transitions. The TTY view also disables the
prompt buffer, but that presentation guard does not replace the controller
boundary.

### ReplView

Protocol boundary for REPL presentation. View implementations render controller-provided snapshots and view events. They must not read runtime state directly and must not consume `AgentStreamEvent`.

### PromptToolkitReplView

TUI implementation for interactive TTY sessions. It owns the prompt_toolkit event loop, key bindings, current-session history, input buffer, layout, and redraw scheduling.

`PromptToolkitReplView` must use a prompt_toolkit `Application` layout with
separate regions for the message list, turn/status display, prompt input
buffer, and bottom status bar. It must not use a `PromptSession.prompt()` loop
as the primary TTY architecture once streaming is enabled, because that model
does not provide strict region ownership for concurrent message-list updates.

`PromptToolkitReplView` runs as a full-screen alternate-screen terminal
application. While active, terminal-native scrollback is not the message history
surface; the message list region owns in-session scrolling and history
visibility.

The production REPL selection path must let prompt_toolkit use its normal
terminal input and output only after the CLI has selected TTY TUI mode. Direct
unit tests of `PromptToolkitReplView` may construct the view without going
through CLI view selection; those tests must be able to inject prompt_toolkit
test-safe input/output objects such as `DummyOutput` so they do not require a
real terminal or Windows console screen buffer. This injection is a testing and
construction boundary only; it must not change TTY selection, plain fallback, or
terminal summary output behavior.

The TTY view owns an in-memory render model for visible messages. Controller
view calls update that model and invalidate the application. Streaming deltas
update only the active assistant block in the message list region; they must not
write directly to stdout/stderr, prompt_toolkit `write_raw`, or ANSI-cleared
terminal transcript output while the application is active.

When `/exit` or TTY `Ctrl+C` terminates the session, the TTY application exits
back to the terminal's normal screen before the CLI prints the terminal summary
to stdout. `/exit` prints `session <session-name> exit.` followed by
`trace: debug-agent trace <session-name>`. TTY `Ctrl+C` prints
`session <session-name> cancelled.` followed by
`trace: debug-agent trace <session-name>`. These summaries are presentation
output and do not alter runtime persistence contracts.

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
-> controller maps AgentStreamEvent to ReplViewEvent, snapshots, or direct view method calls
-> view renders model text, tool blocks, status, and status bar
-> runtime returns AgentRunResult
-> controller finalizes turn status and reenables input
-> /exit closes the session at the runtime safe boundary
-> PromptToolkitReplView exits the alternate screen
-> CLI prints the post-TUI terminal summary
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
    - up/down history buffer replacement
    - layout rendering
    - timer callback
    - queue drain
    - AgentStreamEvent -> ReplViewEvent / snapshots / view method calls
    - message-list view model update
    - application redraw

Runtime background thread
  PromptAgentExecutor.run_turn(..., agent_stream_callback=queue_callback)
    - AgentLoopAdapter.stream(...)
    - model-call lifecycle events
    - text delta events
    - tool call events
    - returns AgentRunResult
```

The background thread may call the controller-provided thread-safe wakeup hook after queueing an event. The hook only invalidates the application or schedules a drain. It must not inspect queue contents or mutate view state.

TTY streaming rendering is layout-driven. The UI thread must update the active
assistant message block and request an application redraw. It must not maintain
the streamed assistant text by directly appending visible bytes to the terminal
transcript and repairing the prompt/status regions afterward.

## Milestone Execution Model

Milestone A uses the existing `AgentLoopAdapter.run(...)` path. It does not require `AgentLoopAdapter.stream(...)` to exist. The controller adapts the final `AgentRunResult` into view updates for the TUI shell.

Milestone B adds `AgentLoopAdapter.stream(...)`, `agent_stream_callback`, and queue-driven stream event delivery. After Milestone B, TUI model and tool progress comes from stream observations when streaming is available, or from the non-streaming fallback result when `AgentRunResult.metadata["streaming_fallback"] = True`.

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

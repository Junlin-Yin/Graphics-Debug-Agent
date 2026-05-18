# ADR 0008: Lightweight REPL TUI Architecture And Terminal UI Stack

## Status

Accepted for Phase 0.5.

## Context

Phase 0 proves the minimal REPL and one-shot runtime path. The plain REPL is
enough for basic interaction, but it is weak for observing streaming output,
tool calls, long tool results, turn status, token usage, and session summaries.

Phase 1 and later phases add skills, tools, subagents, and workflows. Those
features need a human-readable interactive surface for evaluation. The UI must
not become a new runtime truth layer, and it must not break one-shot,
non-interactive, or injected-I/O test paths.

The project needs a terminal UI stack that is lightweight enough for Phase 0.5
but keeps a migration path open if a richer UI is needed later.

## Decision

Implement the Phase 0.5 REPL TUI as a CLI/UI layer using an MVC-style boundary:

- `ReplController` coordinates input, local slash commands, runtime turn
  execution, stream queue draining, timers, and view updates.
- `ReplView` is a protocol boundary for UI implementations.
- `PromptToolkitReplView` is the default TTY implementation.
- `PlainReplView` is the fallback for non-TTY, injected I/O, tests, and
  prompt_toolkit initialization failure.

Runtime remains headless. Runtime services must not depend on prompt_toolkit,
rich, or concrete TUI view classes. Runtime background work must not call view
methods directly.

The view consumes rendering-layer data such as `ReplViewEvent` and snapshots.
It must not consume `AgentStreamEvent` directly. The controller maps stream
observations into view events or render state.

Use:

- `prompt_toolkit` for input, history, key bindings, and the terminal event
  loop.
- `rich` for Markdown rendering, panels, colors, and status display.

`PromptToolkitReplView` uses a prompt_toolkit `Application` layout, not a
linear `PromptSession.prompt()` transcript as the primary TTY architecture once
streaming is enabled. The application owns separate regions for the message
list, current turn/status display, prompt input buffer, and bottom status bar.
Streaming observations update the view's in-memory message model and invalidate
the application. They do not write visible streamed text directly to stdout,
stderr, `write_raw`, or ANSI-cleared terminal transcript output while the
application is active.

`PromptToolkitReplView` is selected only when stdin and stdout are TTYs and no
input/output streams are injected. one-shot mode always keeps plain stdout
behavior.

## Alternatives Considered

### Keep only the plain REPL

This avoids UI dependencies, but it makes streaming output, tool progress,
status updates, and long output previews difficult to inspect.

### Build directly on Textual

Textual offers a richer terminal application framework, but it is heavier than
Phase 0.5 needs and would move the first UI milestone toward a larger
application architecture.

### Use curses or a lower-level terminal UI library

This gives low-level control, but it increases portability and input-handling
cost. Phase 0.5 needs reliable prompt editing, history, and multiline input more
than custom terminal primitives.

### Let runtime call prompt_toolkit or view objects directly

This is simpler initially, but it would make Runtime Core depend on UI details
and would violate the headless runtime boundary.

### Make the TUI mandatory for all REPL paths

This improves feature consistency, but it would break non-TTY automation and
injected-I/O tests. Plain fallback is required to preserve Phase 0 behavior.

## Consequences

- TTY REPL gets a richer interactive surface without changing Runtime Core
  ownership.
- one-shot, non-TTY, and injected-I/O paths remain automation-friendly.
- `prompt_toolkit` and `rich` become Phase 0.5 runtime dependencies.
- The controller/view boundary makes a future Textual migration possible
  without rewriting Runtime Core.
- TUI behavior needs dedicated tests for view selection, fallback, stream event
  mapping, input history, multiline input, and status rendering.
- The UI layer must keep formatting and preview thresholds out of runtime and
  adapter contracts.
- The TTY implementation must test prompt input, history, status, and streaming
  redraw behavior as layout state, not as direct terminal transcript writes.

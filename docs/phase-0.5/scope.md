# Phase 0.5 Scope

## Goal

Phase 0.5 delivers a lightweight, stable, observable command-line TUI for `debug-agent` REPL use.

Phase 0.5 is a `CLI Entrypoint / REPL UI` enhancement. It must not introduce a new runtime semantics layer, change runtime truth, weaken ToolBroker policy, or expand Phase 1+ agent capabilities.

## Must Implement

- TUI shell:
  - TTY REPL uses the lightweight TUI by default.
  - one-shot mode keeps plain stdout behavior.
  - non-TTY and injected I/O environments fall back to `PlainReplView`.
  - prompt_toolkit initialization failure falls back to `PlainReplView` with one concise warning.
- REPL view/controller:
  - `ReplView` protocol.
  - `ReplController`.
  - `PromptToolkitReplView`.
  - `PlainReplView`.
  - optional `ReplRuntime` facade only when needed as a UI-facing adapter.
- Input behavior:
  - shell-style input prompt.
  - current-session prompt history.
  - `Ctrl+J` multiline input.
  - best-effort `Shift+Enter` multiline input.
  - input disablement while a turn is running.
- Output behavior:
  - welcome panel.
  - user message blocks.
  - streaming model text blocks.
  - basic Markdown final rendering for completed model text.
  - tool call and tool result blocks.
  - slash command result messages.
  - error and system messages.
- Turn and session status:
  - per-turn status and elapsed seconds.
  - status bar with token usage, approval mode, and model.
  - session close summary.
  - display status mapping for `cancelled` and `timeout`.
- Streaming:
  - `AgentLoopAdapter.stream(...)`.
  - `LangChainAgentLoopAdapter` native `model.stream()` path.
  - non-streaming `invoke()` fallback when the provider/model does not support streaming.
  - `PromptAgentExecutor.run_turn(..., agent_stream_callback=...)`.
  - `AgentStreamEvent` to queue to `ReplViewEvent` conversion.

## Must Not Implement

- skill registry or `activate_skill`.
- prompt skill injection.
- subagent.
- workflow.
- MCP.
- plugin.
- approval UI popups.
- trace viewer, diff viewer, or workflow viewer.
- session list or session browser.
- cross-session prompt history persistence.
- full theme system.
- full provider/token abstraction.
- complete Chinese IME support.
- mid-call cancel propagation.
- block-level incremental Markdown rendering.
- mouse interaction, multi-pane layout, or message folding.
- any change to Session, Run, RunEvent, Checkpoint, Artifact, ToolBroker, Approval, or Path Policy semantics.

## Minimum Runnable Slice

1. User runs `debug-agent` in a TTY.
2. Runtime creates the session and long-lived prompt run using the Phase 0 runtime path.
3. CLI selects `PromptToolkitReplView`.
4. TUI displays the welcome panel from session and config snapshots.
5. User enters one prompt.
6. TUI appends the submitted user message and disables input while the turn runs.
7. Runtime executes the prompt turn through `PromptAgentExecutor`.
8. Controller receives stream events or a non-streaming fallback result.
9. TUI displays model output, tool blocks when present, and final turn status.
10. User enters `/status`, and TUI appends the local status result as a system message.
11. User enters `/exit`, and TUI displays the session close summary.

one-shot mode, non-TTY mode, and injected I/O tests must continue to use plain stdout/stdin and must not start the TUI.

## Completion Definition

Phase 0.5 is complete when all of these pass:

- REPL startup shows the welcome panel.
- user input and model output do not visually mix.
- prompt history works with up/down navigation.
- `Ctrl+J` multiline input works.
- `Shift+Enter` multiline input is best-effort.
- submitted user prompts remain fixed in the message list.
- input is disabled while a turn is running.
- model output supports streaming display.
- completed model text supports basic Markdown rendering with plain-text fallback.
- tool calls and tool results render as separate blocks.
- long tool results are truncated for preview without changing persisted data.
- each prompt turn shows status and elapsed seconds.
- status bar shows token usage, approval mode, and model.
- `/status` appends a system message in TUI mode.
- `/exit` displays `session <session_id> closed.` and token usage or `unavailable`.
- non-TTY and injected I/O environments use `PlainReplView`.
- prompt_toolkit initialization failure uses `PlainReplView`.
- one-shot mode keeps plain stdout output.
- `AgentLoopAdapter.stream()` final assistant model-call deltas concatenate to `AgentRunResult.assistant_output`.
- `AgentStreamEvent` is never persisted to `run_events`.
- TUI does not change Session/Run/Event/Checkpoint/Artifact runtime contracts.

## Cancellation And Interruption In Phase 0.5

Phase 0.5 does not implement mid-call cancel propagation.

During active execution, `/exit` follows the existing runtime safe-boundary behavior and must not add a new cancellation path. Full cancellation token propagation remains Phase 2 scope.

The TUI may display:

- `cancelled`
- `timeout`

Persisted session and run status remains limited to the runtime contract. `cancelled` maps to `failed` with `error_class=cancelled`. `timeout` maps to `failed` with `error_class=timeout`.

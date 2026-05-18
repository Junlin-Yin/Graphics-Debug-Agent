# Phase 0.5 Test Plan

## Unit Tests

- `PromptHistory` navigates previous and next entries.
- `PromptHistory` stores multiline prompts as one history item.
- empty prompts are not submitted and are not stored in history.
- slash commands are stored in current-session history.
- multiline input submission preserves newlines.
- `ReplView.run(controller)` returns CLI-style exit code `0` for normal close.
- `ToolResultPreviewFormatter` truncates by line limit.
- `ToolResultPreviewFormatter` truncates by character limit.
- `ToolResultPreviewFormatter` includes artifact ids when present.
- `ToolResultPreviewFormatter` converts dictionary output with `json.dumps(..., ensure_ascii=False, sort_keys=True)`.
- `ToolResultPreviewFormatter` prefers `redacted_output` when present.
- tool result preview truncation does not create artifacts.
- `WelcomeSnapshot` uses `unknown` when version lookup fails.
- `WelcomeSnapshot` uses `unknown` when model is missing from the config snapshot.
- `WelcomeSnapshot.session_id_short` formats as `sess-<short-id>` from runtime contract `Session.session_id`; for `sess_<timestamp>-0abc`, it displays `sess-0abc`, not `session: sess`.
- `StatusBarSnapshot` includes model for every render.
- `SessionCloseSummary` uses the full session id.
- Markdown rendering falls back to plain text when rendering fails.
- Markdown rendering is attempted for completed model text below the render threshold.
- model text above `max_markdown_render_chars` remains plain text.
- streaming model deltas update one assistant block for the same `model_call_id`.
- reused turn-local `model_call_id` values in later turns create a new assistant block after the later user message.
- TTY model text deltas update the active assistant block in the prompt_toolkit application layout.
- TTY model text deltas do not write visible streamed text through stdout, stderr, `write_raw`, or other linear terminal transcript paths while the prompt application is active.
- TTY mode uses the terminal alternate screen and does not depend on terminal-native scrollback for in-session message history.
- TTY message-list scrolling controls operate on the application message list region while the prompt application is active.
- TTY message-list mouse wheel and macOS trackpad scroll events operate on the application message list region while the prompt application is active.
- TTY message-list mouse wheel and macOS trackpad scroll events use `message_scroll_step_lines = 2`.
- TTY PageUp/PageDown message-list scrolling uses `message_scroll_step_page = 10`.
- TTY model text deltas may coalesce application redraws without changing accumulated assistant text.
- final Markdown rendering replaces the streamed assistant block for that `model_call_id`.
- final Markdown rendering updates the user-visible TTY output, not only the view's internal message cache.
- final Markdown replacement in TTY mode updates only the active assistant block and does not emit terminal clearing sequences against the active prompt display.
- TTY streaming model deltas update only the `🔮 Assistant` block body and do not duplicate or rewrite the assistant block header.
- `AgentStreamEvent` maps to `ReplViewEvent`, snapshots, or direct view method calls.
- `ReplView` does not directly consume `AgentStreamEvent`.
- `AgentStreamEvent.kind` values use the `stream_` prefix.
- `AgentStreamEvent` is not written to persisted `run_events`.
- model calls without text deltas do not create model output blocks.
- intermediate model-call text renders but does not participate in final assistant output equality.
- function-call-only chunks do not render as model text.
- partial tool args do not render as model text.
- LangChain streaming tool-call chunks are reconstructed into complete tool arguments before ToolBroker invocation.
- tool args produce only redacted or short preview output when displayed.
- duplicate tool names correlate by `tool_call_id`.
- streamed tool call start does not append a visible message block.
- streamed tool call completion appends one visible tool call block.
- streamed tool result appends a separate preview-only result block without repeating the completed tool block's `tool` and `status` lines.
- streamed tool results reuse the correlated tool name and do not render as `tool: unknown`.
- streamed tool completion may use the correlated start name for display when the name is already known.
- TTY successful tool completion renders as `🟢 <tool_name> (<duration>)` on one line.
- TTY non-success tool completion renders as `🔴 <tool_name> (<duration>)` on one line.
- TTY tool result preview lines are indented by four spaces.
- final Markdown replacement does not print terminal control sequences as visible message text.
- terminal replacement clears the full previous streamed block before printing the replacement block.
- provider tool calls with missing or empty names are not emitted as stream tool events and are not invoked through ToolBroker.
- status bar snapshot formatting handles known usage.
- status bar snapshot formatting handles unavailable usage.
- status bar token formatting uses raw integers below `1000`.
- status bar token formatting uses one decimal `k` for values of `1000` or greater.
- TTY turn status updates do not append message-list entries.
- repeated TTY turn status updates replace the same bottom status region in place.
- TTY bottom input/status region remains visible while input submission is disabled during a running turn.
- TTY prompt input buffer is non-editable while input submission is disabled during a running turn.
- TTY up/down keys replace the active prompt input buffer from current-session history and place the cursor at the end.
- TTY up/down keys navigate current-session prompt history only when the input cursor is at the end of the buffer.
- TTY up/down keys move the cursor within the prompt input buffer when the cursor is not at the end of the buffer.
- TTY down navigation past the newest history entry clears the active prompt input buffer.
- TTY `Ctrl+J` inserts a newline in the prompt input buffer and expands the visible input region up to 5 lines.
- TTY prompt input region starts at 1 visible line for a new editable prompt.
- TTY prompt submission resets the prompt input region to 1 visible line.
- TTY backspacing over newline characters recalculates prompt input height and shrinks the visible input region when fewer lines are needed.
- TTY prompt input top and bottom borders render as separate `-` rows and do not count toward the prompt input buffer's 1-to-5 visible line limit.
- TTY prompt input height changes refresh the message list so the newest message remains visible when following the newest message.
- TTY turn/status region has one blank spacer row above it and does not append that spacer to the message list.
- TTY welcome panel renders inside a lightweight rectangular ASCII border.
- TTY submitted user prompt blocks render with top and bottom `-` borders and a shell-style `> ` marker.
- TTY submitted user prompt block borders use the smallest terminal cell width that fully covers the rendered prompt text in that block, including Chinese text.
- TTY submitted multiline user prompts render `> ` only on the first line and indent continuation lines by two spaces.
- TTY system message blocks render a top `-` border, a `🤖 System` header, one blank line, and the message body.
- TTY error message blocks render a top `-` border, a `❌ Error` header, one blank line, and the message body.
- TTY assistant message blocks render a top `-` border, a `🔮 Assistant` header, one blank line, and the assistant body.
- TTY follow-newest message-list scrolling remains clamped to actual rendered content and never renders an empty message viewport while welcome or message content exists.
- TTY message-list visibility tests cover prompt_toolkit application rendering, not only the view's internal message cache or helper text serialization.
- TTY message-list growth does not shrink the prompt input region below its current visible height.
- TTY multiline prompt input keeps the bottom status bar visible below the input region.
- TTY `Ctrl+C` invokes the existing interrupt path and records terminal `failed/cancelled` state.
- TTY `/exit` closes the application idempotently and does not raise a prompt_toolkit return-value error.
- TTY `/exit` exits the alternate screen before printing `session <session-name> exit.` and `trace: debug-agent trace <session-name>` to stdout.
- TTY `Ctrl+C` exits the alternate screen before printing `session <session-name> cancelled.` and `trace: debug-agent trace <session-name>` to stdout.
- TTY message list content remains reachable after it exceeds the visible terminal height.
- TTY message list keeps the newest message visible after message append, streaming delta, and prompt input height change when following the newest message.
- TTY streaming assistant text redraws do not mutate prompt input buffer text or cursor position.
- TTY streaming assistant text redraws do not change bottom status text unless the status snapshot or turn status changed.
- TTY streaming assistant text redraws do not rewrite prior user, assistant, tool, system, or error blocks.
- truncated tool previews always include the `[truncated: ...]` detail line, not only a bare `> ...` marker.
- session cumulative token usage aggregates best-effort provider usage.
- session close summary formats known token usage.
- session close summary formats unavailable token usage.
- `agent_stream_callback` sends events to the controller queue.
- controller queue draining maps events in order.
- recoverable one-turn adapter failures display turn `failed`, keep the REPL session database open, and allow a later prompt in the same session.
- final assistant model-call deltas concatenate to `AgentRunResult.assistant_output`.
- non-streaming provider fallback uses `invoke()`.
- non-streaming provider fallback sets `AgentRunResult.metadata["streaming_fallback"] = True`.
- non-streaming provider fallback emits the warning at most once.
- `/status` appends a TUI system message.
- prompt_toolkit initialization failure falls back to `PlainReplView`.
- prompt_toolkit initialization failure emits at most one warning.
- welcome version lookup failure displays `unknown`.

## Integration Tests

- `debug-agent` in a TTY starts TUI and shows the welcome panel.
- TTY REPL uses a full-screen alternate-screen application with application-owned message scrolling.
- submitted prompt appears as a fixed user message.
- input is disabled while a prompt turn is running.
- multiline input grows visibly from 1 to at most 5 prompt input lines.
- submitted multiline input resets visibly to 1 prompt input line.
- macOS Terminal or iTerm2 trackpad scrolling scrolls the TUI message list in full-screen alternate-screen mode.
- mouse wheel or trackpad scrolling moves the message list in smaller increments than PageUp/PageDown.
- long message-list growth does not resize the prompt input region and keeps newest content visible when following the newest message.
- prompt input top and bottom borders remain visible during multiline editing and terminal resize.
- backspacing multiline prompt input shrinks the input area when lines are removed.
- turn/status text is separated from the message list by one blank row.
- welcome panel appears inside a rectangular border.
- submitted prompt appears with shell-style message-list formatting and multiline continuation alignment.
- submitted prompt message-list borders cover the submitted text without spanning unrelated terminal width.
- assistant output appears under a `🔮 Assistant` header without an `assistant:` prefix.
- system output appears under a `🤖 System` header without a `system:` prefix.
- error output appears under a `❌ Error` header without an `error:` prefix.
- tool completion appears as one emoji-prefixed line, with result preview indented by four spaces.
- welcome, submitted prompts, and assistant output remain visible through the actual prompt_toolkit message-list viewport when the message list is following newest content.
- mock streaming model deltas render incrementally.
- mock streaming model final text switches to Markdown rendering when allowed.
- mock tool call start and completion render as tool blocks.
- mock tool result renders as a preview block.
- long tool output is truncated for display.
- turn status updates while running and becomes final at completion.
- timeout displays as `timeout` while persisted state remains `failed` with `error_class=timeout`.
- cancellation displays as `cancelled` while persisted state remains `failed` with `error_class=cancelled`.
- `/status` during active execution appends a system message.
- ordinary prompt during active execution is rejected and shown clearly.
- TTY REPL `Ctrl+C` records terminal `failed/cancelled` state and releases workspace ownership.
- `/exit` displays session closed and token summary.
- TTY `/exit` exits without duplicate `Application.exit(...)` errors.
- TTY `/exit` prints the post-TUI terminal summary to stdout after returning to the terminal's normal screen.
- TTY `Ctrl+C` prints the post-TUI cancellation summary to stdout after returning to the terminal's normal screen.
- active execution `/exit` does not introduce mid-call cancel propagation.
- one-shot mode does not start TUI and keeps plain stdout.
- non-TTY REPL uses `PlainReplView`.
- injected input/output streams use `PlainReplView`.
- prompt_toolkit initialization failure uses `PlainReplView`.
- Milestone A TUI shell works through `AgentLoopAdapter.run(...)` without requiring `AgentLoopAdapter.stream(...)`.
- Milestone B TUI streaming works through `AgentLoopAdapter.stream(...)`.

## Failure Scenarios

- prompt_toolkit initialization failure.
- provider does not support streaming.
- model stream raises provider error.
- model timeout.
- model cancellation observed by runtime.
- tool result output exceeds preview line limit.
- tool result output exceeds preview character limit.
- tool call has no provider tool call id.
- duplicate tool names in one turn.
- malformed stream event payload from adapter.
- Markdown rendering failure.
- token usage missing from provider response.
- version lookup failure.

## Fake Model Testing

Fake model must support:

- deterministic non-streaming assistant text.
- deterministic streaming text deltas.
- multiple model-call lifecycles in one turn.
- model calls with no displayable text.
- provider-visible text before a tool call.
- function-call-only chunks.
- partial tool argument chunks.
- forced provider error.
- forced timeout.
- forced lack of streaming support.

Tests should not require network access.

## Fake Tool Testing

Fake tool or fixture workspace must cover:

- tool call start.
- tool call success.
- tool call error.
- output large enough for preview truncation.
- artifact ids present in `ToolResult`.
- dictionary output requiring controller formatting.
- repeated use of the same tool name in one turn.

## Manual Tests

- macOS Terminal.
- iTerm2.
- Chinese input, best effort.
- Chinese backspace behavior, best effort.
- multiline prompt entry.
- backspacing multiline prompt input until it shrinks.
- fast history navigation.
- long Markdown output.
- long tool result output.
- narrow terminal layout with wrapping and readable tool blocks.

## Smoke Commands

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
```

TUI smoke test:

```text
debug-agent
> hello
> /status
> tell me one more thing
> /exit
```

Fallback smoke test:

```text
debug-agent < input.txt
```

one-shot smoke test:

```bash
debug-agent -p "hello"
```

## Phase 0.5 Acceptance

Phase 0.5 is accepted only if:

- the Phase 0.5 TUI smoke path works with fake model configuration.
- one-shot behavior remains plain stdout.
- non-TTY and injected I/O paths use `PlainReplView`.
- streaming tests pass with a fake or mock streaming provider.
- non-streaming fallback tests pass.
- no Phase 1+ feature is required.

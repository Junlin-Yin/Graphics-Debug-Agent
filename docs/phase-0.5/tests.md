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
- TTY model text deltas may coalesce application redraws without changing accumulated assistant text.
- final Markdown rendering replaces the streamed assistant block for that `model_call_id`.
- final Markdown rendering updates the user-visible TTY output, not only the view's internal message cache.
- final Markdown replacement in TTY mode updates only the active assistant block and does not emit terminal clearing sequences against the active prompt display.
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
- TTY down navigation past the newest history entry clears the active prompt input buffer.
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
- submitted prompt appears as a fixed user message.
- input is disabled while a prompt turn is running.
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

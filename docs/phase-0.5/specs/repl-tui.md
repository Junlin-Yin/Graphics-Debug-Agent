# Phase 0.5 REPL TUI Specification

## Boundary

The TUI is a presentation and input layer for REPL sessions. It must not own runtime truth.

The TUI may display runtime state snapshots, view events, and stream observations after controller mapping. It must not mutate Session, Run, RunEvent, Checkpoint, Artifact, ToolBroker, Approval, or Path Policy semantics.

## Interfaces

```python
class ReplView:
    def run(self, controller: ReplController) -> int: ...
    def show_welcome(self, snapshot: WelcomeSnapshot) -> None: ...
    def set_input_enabled(self, enabled: bool) -> None: ...
    def append_user_message(self, message: str) -> None: ...
    def append_view_event(self, event: ReplViewEvent) -> None: ...
    def set_turn_status(self, turn_id: int, status: str, elapsed_seconds: int) -> None: ...
    def update_status_bar(self, snapshot: StatusBarSnapshot) -> None: ...
    def show_session_closed(self, summary: SessionCloseSummary) -> None: ...
    def show_error(self, message: str) -> None: ...
```

`run(controller)` replaces a blocking `read_prompt()` model. The view owns the UI event loop.

`run(controller)` returns a CLI-style exit code. `0` means normal REPL close. Non-zero values follow the runtime or CLI failure exit code for the terminal condition.

`append_view_event()` receives rendering-layer events only. The view must not depend on `AgentStreamEvent`.

In TTY TUI mode, `PromptToolkitReplView` must be implemented as a
prompt_toolkit `Application` with explicit, independently rendered UI regions:

- message list region
- current turn/status region
- prompt input buffer region
- bottom status bar region

The TTY application must use the terminal alternate screen. While it is active,
the terminal viewport is owned by the TUI application rather than by the
terminal's normal linear transcript. The TUI must not rely on terminal scrollback
for message history visibility.

The TTY view must keep an in-memory view model for visible messages. Rendering
updates mutate that view model and request an application redraw. Streaming
model text, final Markdown replacement, tool blocks, system messages, and error
messages must not be written directly to stdout, stderr, `write_raw`, or other
linear terminal transcript paths while the prompt application is active.

The message list region must support scrolling or an equivalent layout mechanism
that keeps older visible messages reachable after the message list exceeds the
available terminal height. Long message history must not be silently discarded or
made unreachable by viewport clipping.

Mouse wheel, macOS trackpad scrolling, PageUp/PageDown, or equivalent
application-level scrolling controls must operate on the TUI message list
region, not on terminal-native scrollback, while the TTY application is active.
Phase 0.5 supports these pointer events only for message-list scrolling; it does
not add general mouse interaction such as clickable panes, selection behavior,
or message folding.

Mouse wheel and trackpad scroll events use a small line-oriented step so
fine-grained scrolling remains controllable. PageUp/PageDown use a larger
page-oriented step. The default steps are:

```text
message_scroll_step_lines = 2
message_scroll_step_page = 10
```

```python
class ReplController:
    def on_submit(self, text: str) -> None: ...
    def on_slash_command(self, cmd: str) -> None: ...
    def on_interrupt(self) -> None: ...
    def on_agent_stream_event(self, event: AgentStreamEvent) -> None: ...
    def notify_event_ready(self) -> None: ...
    def on_turn_finished(self, result: AgentRunResult) -> None: ...
```

`notify_event_ready()` is a thread-safe wakeup hook. It must not update view state directly.

## Snapshot Types

```python
class WelcomeSnapshot:
    tool_name: str
    version: str
    model: str
    workspace_root: str
    approval_mode: str
    session_id_short: str
```

```python
class StatusBarSnapshot:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    approval_mode: str
    model: str
```

```python
class SessionCloseSummary:
    session_id: str
    status: Literal["closed", "cancelled", "failed"]
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_type: str | None
```

`session_id` in `SessionCloseSummary` is the full session id.

When the model is missing from the config snapshot, snapshot builders use `unknown`.

## Welcome Panel

The REPL shows a welcome panel at startup.

Minimum fields:

- tool name: `debug-agent`
- version from `importlib.metadata.version("debug-agent")`
- current model from the config snapshot
- workspace root from the session
- approval mode from the session
- display session name `sess-<short-id>`, where `<short-id>` is the first four characters of the unique id segment in the runtime contract `Session.session_id`; for Phase 0 ids shaped like `sess_<timestamp>-<id>`, this uses the trailing `<id>` segment, not `sess`
- the display session name must not be derived from the `.sessions` directory name or artifact path

If version lookup fails in editable or development environments, the view displays `unknown` and continues startup.

The welcome panel must not affect one-shot output.

In TTY TUI mode, the welcome panel is rendered inside a lightweight rectangular
ASCII border so the startup title area is visually distinct from normal
messages. The border is presentation-only and must be derived from the rendered
welcome fields, not from runtime state beyond `WelcomeSnapshot`.

## Input Behavior

The input area uses a shell-style prompt beginning with `>`.

Minimum behavior:

- bounded multiline input in TTY TUI mode, with a minimum visible height of 1
  line and a maximum visible height of 5 lines.
- `Ctrl+J` inserts a newline.
- the prompt input region starts each editable prompt at 1 visible line.
- `Ctrl+J` grows the prompt input region upward by visible line count until it
  reaches the 5-line maximum.
- submitting a prompt resets the prompt input region to the initial 1 visible
  line state.
- deleting text, including backspacing over newline characters, must recalculate
  the prompt input region height so the region can shrink back toward 1 visible
  line when the buffer no longer needs additional lines.
- `Shift+Enter` inserts a newline on terminals where prompt_toolkit can detect it.
- `Enter` submits.
- up/down navigates current-session prompt history.
- submitted user input is appended to the message list.
- input submission is disabled while a prompt turn is running.
- the input area is visually distinct from normal output.
- in TTY TUI mode, the input area has a one-line top border and a one-line
  bottom border made from `-` characters. These border rows do not count toward
  the prompt input buffer's 1-to-5 visible line limit. The border rows must
  render to the current prompt region width and adapt when the terminal is
  resized; they must not be hard-coded to a stale terminal width.
- in TTY TUI mode, disabling input submission must make the prompt input buffer non-editable for ordinary typing while a turn is running.
- in TTY TUI mode, disabling input submission must not remove the bottom input/status region; the bottom status bar remains visible while the turn is running and continues to update in place.
- in TTY TUI mode, the prompt input region owns its current visible height.
  Message-list growth must not shrink or overwrite the current prompt input
  height.
- in TTY TUI mode, prompt input height changes caused by `Ctrl+J`, prompt
  submission reset, history replacement, or buffer clearing must trigger a
  layout redraw and keep the newest message visible unless the user has
  intentionally scrolled away from the newest message.
- in TTY TUI mode, up/down history navigation must be wired to the active prompt input buffer only when the input cursor is at the end of the buffer.
- in TTY TUI mode, when the cursor is not at the end of the prompt input buffer, up/down must perform normal in-buffer cursor movement instead of prompt history navigation.
- in TTY TUI mode, history navigation replaces the buffer text with the selected current-session history item and places the cursor at the end.
- in TTY TUI mode, `Ctrl+C` must be bound to the existing `ReplController.on_interrupt()` path. Phase 0.5 does not change the persisted interrupt contract.

History rules:

- record only successfully submitted user prompts.
- do not submit or record empty prompts.
- record slash commands.
- store a multiline prompt as one history item.
- keep history in memory for the current session only.

`PromptHistory` is a current-session in-memory component. The minimum interface is:

```python
class PromptHistory:
    def add(self, entry: str) -> None: ...
    def previous(self) -> str | None: ...
    def next(self) -> str | None: ...
    def reset_navigation(self) -> None: ...
```

`PlainReplView` does not need interactive history navigation.

Chinese IME input and Chinese backspace correctness are best-effort manual checks in Phase 0.5. Complete support is not required for acceptance.

## Message Rendering

The message list displays:

- user prompts.
- model output.
- completed tool call blocks.
- tool result preview blocks.
- slash command results.
- system, error, interrupt, and completion status messages.

TTY message blocks use lightweight visual separation. Each user, model, tool,
system, and error block starts with one blank line before its visible content.
The welcome panel keeps its own startup border format and is not part of this
per-message block format. Session close summaries remain post-TUI terminal
output, not message-list content.

Message list rules:

- TTY message history must remain reachable after it exceeds the terminal
  viewport height.
- Adding new message blocks or streaming deltas must not discard older visible
  message blocks from the in-memory view model.
- Adding new message blocks or streaming deltas must keep the newest message
  visible when the message list is already following the newest message.
- If the prompt input region changes height and reduces or increases the
  message-list viewport, the message list must refresh its scroll position so
  the newest message remains visible when following the newest message.
- The message-list scroll position must always remain within the real rendered
  content range. Follow-newest behavior must not use an unbounded or sentinel
  scroll offset that can render blank viewport rows while welcome or message
  content exists in the in-memory view model.
- In full-screen alternate-screen TTY mode, message history visibility is owned
  by the TUI message list. Terminal-native scrollback is not an acceptance path
  for viewing in-session message history.

User prompt blocks render with a top and bottom `-` border and a shell-style
prompt marker. The user prompt block border length is the smallest length that
fully covers the rendered prompt text in that block by terminal cell width,
including the `> ` prefix on the first line, two-space indentation on
continuation lines, and double-width characters such as Chinese text. For
multiline prompts, only the first line uses `> ` and following lines are
indented by two spaces so prompt text remains aligned:

```text

--------------
> line 1
  line 2
  line 3
--------------
```

Model output rules:

- during streaming, text deltas append to the existing plain-text model block for that `model_call_id`; the view must not render each delta as a separate assistant message.
- `model_call_id` is turn-local. A new submitted user message starts a new visible turn, so the view must not use a reused `model_call_id` from a previous turn to replace or append to that previous turn's assistant block.
- TTY views may coalesce text deltas into bounded-rate application redraws to keep rendering efficient; coalescing must not change the accumulated model text or final `model_call_id` block replacement.
- TTY streaming updates must be scoped to the active assistant block in the message list region. They must not rewrite previous user messages, previous assistant/tool/system blocks, the prompt input buffer, the current turn/status region, or the bottom status bar region.
- TTY assistant blocks render a top `-` border, a `🔮 Assistant` header, one
  blank line, and the current assistant text. Streaming deltas update only the
  assistant text body within the same assistant block; they do not duplicate or
  rewrite the header.
- when a model call completes and accumulated text is at or below `max_markdown_render_chars`, the view must attempt to replace that same user-visible model block with rich Markdown rendering. Updating only an internal cache is insufficient.
- final Markdown replacement in TTY mode must replace only the same assistant block in the message list region. It must not use terminal clearing sequences against the active prompt display and must not disturb prompt input or bottom status rendering.
- if Markdown rendering fails, keep plain text.
- tables are not an acceptance capability.
- if accumulated text for a model call exceeds `max_markdown_render_chars = 50_000`, keep plain text and do not run Markdown rendering.
- if a model call has no `stream_text_delta`, do not create an empty model output block.
- tool blocks must remain separate from model Markdown text.
- `stream_tool_call_started` is not rendered as a visible message block. The controller may store its `tool_call_id`, `model_call_id`, and `name` for later correlation only.
- `stream_tool_call_completed` appends the visible tool call block with tool name, final status, and duration.
- `stream_tool_result` appends a separate tool result preview block. Because the preceding completion block already displayed the tool name and status, the streamed result preview block must not repeat `tool: ...` or `status: ...`. It may keep `tool_call_id`, `model_call_id`, and correlated tool name in rendering metadata for tests or diagnostics. Tool rendering in Phase 0.5 does not perform in-place updates.
- TTY system message blocks render a top `-` border, a `🤖 System` header, one
  blank line, and the system message body. This includes `/status`, busy
  messages, unsupported slash command messages, and streaming fallback
  warnings.
- TTY error message blocks render a top `-` border, a `❌ Error` header, one
  blank line, and the error message body. This applies to both
  `error_message` view events and direct `show_error(...)` calls.

## Tool Result Preview

Tool calls render as independent blocks.

TTY tool completion blocks render the tool name, duration, and success/failure
state on one line. Successful `ok` or `completed` states use `🟢`; failed,
timeout, cancelled, error, or otherwise non-success states use `🔴`.

Example:

```text

🟢 read_file (1.2s)
```

Tool result preview renders as quoted text with truncation. In TTY mode, every
preview line is indented by four spaces and the preview block does not repeat
the already displayed tool name or status:

```text
    > line 1
    > line 2
    > ...
    > [truncated: showing 10 of 325 lines, full output saved as artifact art_xxx]
```

When preview truncation happens, the `[truncated: ...]` line is mandatory. A bare `> ...` marker without the following truncation detail is incomplete UI output.

Default preview limits:

```text
max_tool_result_preview_lines = 10
max_tool_result_preview_chars = 1000
```

The controller delegates preview formatting to `ToolResultPreviewFormatter`. Runtime and adapter code must not own UI preview thresholds.

```python
class ToolResultPreview:
    text: str
    truncated: bool
    shown_lines: int
    total_lines: int | None
    artifact_ids: list[str]
```

```python
class ToolResultPreviewFormatter:
    def format(
        self,
        *,
        output: str | dict | None,
        redacted_output: str | None,
        artifact_ids: list[str],
        max_lines: int = 10,
        max_chars: int = 1000,
    ) -> ToolResultPreview: ...
```

If `output` is a dictionary, the formatter converts it to a string with:

```python
json.dumps(output, ensure_ascii=False, sort_keys=True)
```

If `redacted_output` is present, it is used as the preview source instead of `output`.

Preview truncation affects display only. It must not create artifacts and must not alter persistence. Full content remains governed by existing `ArtifactStore` and `run_events` rules.

If a `ToolResult` includes artifacts, the view displays artifact ids. If not, the view may state that full output follows the existing persistence rules.

For streamed tools, the controller correlates tool display state by `tool_call_id`. The tool name comes from `stream_tool_call_started.name` or, as a defensive fallback, `stream_tool_call_completed.name`; `stream_tool_result` is not required to carry a name and must not render as `tool: unknown` when a prior lifecycle event supplied the name.

## Turn Status

Every submitted user prompt has a turn status.

Turn status is stable UI state, not message-list content. In TTY TUI mode the view displays the current turn status in the bottom status region below the message list and above the input prompt. The bottom status region must have one blank spacer row above it so turn/status text is visually separated from the message list. The bottom status region remains visible before submission, while a submitted turn is running, and after the turn completes. Repeated updates for the same turn replace the displayed status in place and must not append new message blocks.

The bottom status region must remain visually stable while model text streams. Stream text deltas must not change bottom status state or bottom status text. A full application redraw may occur for message-list changes, but it must preserve the bottom status region content unless the rendered bottom status text changed.

The prompt input buffer and bottom status toolbar are separate prompt_toolkit-controlled UI regions. Streaming model text and message-list updates must not render through those regions, and bottom status updates must not append to or rewrite the message list.

`/exit` and TTY `Ctrl+C` must close the TTY application through a single
idempotent application shutdown path. Rendering the session terminal summary
must not trigger duplicate `Application.exit(...)` calls or surface
prompt_toolkit return-value errors to the user.

In full-screen alternate-screen TTY mode, `/exit` and TTY `Ctrl+C` terminal
summaries are printed only after the TUI application exits back to the terminal's
normal screen. They must not be rendered solely inside the alternate-screen
message list, because that content is not retained in terminal-native
scrollback after application exit.

TTY `/exit` prints this terminal summary to stdout after leaving the alternate
screen:

```text
session <session-name> exit.
trace: debug-agent trace <session-name>
```

TTY `Ctrl+C` prints this terminal summary to stdout after leaving the alternate
screen:

```text
session <session-name> cancelled.
trace: debug-agent trace <session-name>
```

`<session-name>` is the full runtime `Session.session_id`, matching the existing
trace command input. This terminal summary is presentation output only; it does
not change runtime persistence semantics.

While the prompt_toolkit application is active, the TTY view must not maintain
streaming output by writing visible text directly to the terminal transcript,
then repairing the prompt with bottom-toolbar redraws. Streaming and final
replacement rendering must be ordinary application layout updates against the
message list view model. Terminal transcript writes remain allowed only for
plain fallback paths or for output emitted before the TTY application starts or
after it exits.

The controller updates running turns once per second:

```python
view.set_turn_status(turn_id, "running", elapsed_seconds)
```

At turn completion, the controller sets the final display status.

Display statuses:

- `running`
- `completed`
- `failed`
- `cancelled`
- `timeout`

Persisted run/session statuses remain governed by runtime contracts. `cancelled` maps to `failed` with `error_class=cancelled`. `timeout` maps to `failed` with `error_class=timeout`.

During active execution, ordinary user prompts are rejected and shown as system or error messages. `/status` appends a system message and must not open a modal or replace the status bar.

Recoverable adapter failures scoped to one prompt turn, such as the Phase 0 tool-call loop iteration limit, display the turn as `failed` and append an error message, but they must not terminalize the REPL session or prompt run. The controller must leave the session database open, keep workspace ownership active, re-enable input, and allow the next user prompt in the same session.

`Ctrl+C` in a TTY REPL exits the current session as a terminal cancellation using the Phase 0 runtime rule: persist `failed` with `error_class=cancelled`, write an error checkpoint when session/run state exists, release workspace ownership, return a non-zero exit code, and print the post-TUI terminal summary defined above. Phase 0.5 does not add mid-call cancellation propagation; if the interrupt is observed while a turn is active, terminal marking happens through the existing safe-boundary runtime path.

## Status Bar

The status bar displays:

- current turn status and elapsed seconds when a turn is active or has just completed.
- token usage, best effort.
- current approval mode from persisted session state.
- current model.

`StatusBarSnapshot` carries raw token counts. The controller owns best-effort usage aggregation. The view owns display formatting using the token formatting rules in this section.

Example:

```text
tokens: 12.4k used | mode: normal | model: claude-sonnet-4
```

If the model is missing from the config snapshot, display `unknown`.

Token usage formatting:

- if usage is unavailable, display `unavailable`.
- if the value is below `1000`, display the raw integer.
- if the value is `1000` or greater, display one decimal place with a `k` suffix.

Phase 0.5 does not display context remaining percentage.

Update timing:

- initialize the status bar after REPL startup.
- update token usage after each completed model response.
- redraw the TTY bottom status bar only when its rendered text changes; model text deltas alone do not count as a status bar change.
- for turns with multiple model calls, the controller maintains cumulative best-effort usage.
- if a model response omits usage, keep the last known cumulative value; if no usage has ever been observed, display `unavailable`.

## Session Close Summary

Normal exit:

```text
session <session_id> closed.
tokens used: <input_tokens> input, <output_tokens> output, <total_tokens> total
```

If usage is unavailable:

```text
session <session_id> closed.
tokens used: unavailable
```

Cancellation:

```text
session <session_id> cancelled.
trace: debug-agent trace <session_id>
```

Failure:

```text
session <session_id> failed.
error: <error_type>
trace: debug-agent trace <session_id>
```

Timeout:

```text
session <session_id> failed.
error: timeout
trace: debug-agent trace <session_id>
```

## Fallback Rules

`PromptToolkitReplView` is selected only when:

- stdin is a TTY.
- stdout is a TTY.
- no `input_stream` is injected.
- no `output_stream` is injected.

`PlainReplView` is selected when stdin or stdout is not a TTY, or when injected I/O is present.

If prompt_toolkit initialization fails in a TTY environment, the controller selects `PlainReplView` and writes one concise warning.

one-shot mode never starts TUI.

Direct unit tests of `PromptToolkitReplView` are allowed to construct the TTY
view class outside this CLI selection path. Such tests must not depend on the
test process having a real terminal. The view constructor must support injecting
prompt_toolkit input/output objects for tests, and tests should use
prompt_toolkit's test-safe output, for example `DummyOutput`, when they only
need to inspect layout state, rendered text, key handling, scrolling, or terminal
summary behavior. Production TTY execution keeps prompt_toolkit's normal
terminal input/output selection.

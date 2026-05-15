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
- first eight characters of the session id

If version lookup fails in editable or development environments, the view displays `unknown` and continues startup.

The welcome panel must not affect one-shot output.

## Input Behavior

The input area uses a shell-style prompt beginning with `>`.

Minimum behavior:

- single-line input.
- `Ctrl+J` inserts a newline.
- `Shift+Enter` inserts a newline on terminals where prompt_toolkit can detect it.
- `Enter` submits.
- up/down navigates current-session prompt history.
- submitted user input is appended to the message list.
- input is disabled while a prompt turn is running.
- the input area is visually distinct from normal output.

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
- tool call start and completion blocks.
- tool result preview blocks.
- slash command results.
- system, error, interrupt, and completion status messages.

Model output rules:

- during streaming, text deltas append to a plain-text model block.
- when a model call completes and accumulated text is at or below `max_markdown_render_chars`, the view must attempt to replace that block with rich Markdown rendering.
- if Markdown rendering fails, keep plain text.
- tables are not an acceptance capability.
- if accumulated text for a model call exceeds `max_markdown_render_chars = 50_000`, keep plain text and do not run Markdown rendering.
- if a model call has no `stream_text_delta`, do not create an empty model output block.
- tool blocks must remain separate from model Markdown text.

## Tool Result Preview

Tool calls render as independent blocks.

Example:

```text
tool: read_file
status: ok
duration: 1.2s
```

Tool result preview renders as quoted text with truncation:

```text
> line 1
> line 2
> ...
> [truncated: showing 10 of 325 lines, full output saved as artifact art_xxx]
```

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

## Turn Status

Every submitted user prompt has a turn status.

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

## Status Bar

The status bar displays:

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

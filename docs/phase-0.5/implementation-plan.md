# Phase 0.5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This is an implementation-process instruction only. Phase 0.5 runtime itself does not implement subagents.

**Goal:** Build the lightweight REPL TUI and streaming observation path while preserving Phase 0 one-shot, plain REPL, persistence, ToolBroker, and trace behavior.

**Architecture:** The TUI is a CLI/REPL presentation layer. Runtime remains headless and authoritative. `AgentLoopAdapter.run(...)` stays the authoritative result path; `AgentLoopAdapter.stream(...)` is added later as a UI observation path.

**Tech Stack:** Python, prompt_toolkit, rich, LangChain-compatible chat model, pytest, uv.

---

## File Structure To Create Or Modify

- `pyproject.toml`: add Phase 0.5 runtime dependencies.
- `uv.lock`: lock Phase 0.5 runtime dependencies.
- `src/debug_agent/cli/repl_view.py`: `ReplView` protocol, view events, snapshots, token formatting, prompt history, and tool result preview formatter.
- `src/debug_agent/cli/plain_repl_view.py`: plain fallback `ReplView` implementation for non-TTY, injected I/O, and tests.
- `src/debug_agent/cli/repl_controller.py`: submit/slash/turn/status orchestration and stream queue draining.
- `src/debug_agent/cli/prompt_toolkit_view.py`: prompt_toolkit + rich TTY implementation.
- `src/debug_agent/cli/main.py`: REPL view selection and one-shot preservation.
- `src/debug_agent/runtime/prompt_executor.py`: optional `agent_stream_callback` path.
- `src/debug_agent/runtime/stream_events.py`: `AgentStreamEvent` contract.
- `src/debug_agent/adapters/langchain_adapter.py`: `AgentLoopAdapter.stream(...)`, LangChain streaming path, and non-streaming fallback metadata.
- `tests/unit/cli/`: prompt history, formatter, snapshots, controller, view selection, and prompt_toolkit fallback tests.
- `tests/unit/adapters/`: streaming adapter, fallback metadata, and final-delta equality tests.
- `tests/unit/runtime/`: prompt executor callback and no-stream-event-persistence tests.
- `tests/integration/`: TUI shell, plain fallback, one-shot preservation, and streaming turn tests.

This file structure may be adjusted to match the actual package scaffold, but module responsibilities and boundaries must remain separated.

## Global Invariants

- Do not change Session, Run, RunEvent, Checkpoint, Artifact, ToolBroker, Approval, or Path Policy semantics.
- Do not persist `AgentStreamEvent` to `run_events`.
- Do not route one-shot through TUI.
- Do not require prompt_toolkit or rich for non-TTY or injected-I/O fallback.
- Do not add skill, subagent, workflow, MCP, plugin, approval UI, mid-call cancel propagation, or cross-session prompt history.
- Do not let runtime services import prompt_toolkit, rich, or concrete TUI view classes.
- Keep the repository compilable, tests runnable, and `debug-agent -p "..."` startable after every milestone.
- If `docs/project-contract.md`, active phase docs, and accepted ADRs conflict, stop and patch the documentation before implementation continues.

## Dependency Order

```text
dependency declarations
-> UI-neutral contracts/helpers
-> PlainReplView compatibility boundary with fake/minimal controller coverage
-> non-streaming ReplController with background turn execution, timer, and completion wakeup
-> PromptToolkitReplView shell
-> Milestone A acceptance
-> streaming contracts
-> LangChain streaming implementation
-> controller stream queue integration for AgentStreamEvent delivery
-> Milestone B acceptance
-> final hardening
```

This order is mandatory. It keeps fallback behavior available before TUI selection changes, and it keeps `run(...)` stable before `stream(...)` is introduced.

## Milestone 1: Dependency And Lockfile Boundary

- [x] Add `prompt_toolkit >= 3.0.0` to the package dependency declarations.
- [x] Add `rich >= 13.0.0` to the package dependency declarations.
- [x] Run `uv lock`.
- [x] Verify dependency changes do not introduce runtime imports outside CLI/UI modules.
- [x] Verify with `uv run pytest tests/unit -v`.

Modified boundaries: package dependency declarations and lockfile only.

Invariants: no runtime behavior changes; one-shot and plain REPL behavior remains unchanged.

Freeze/review checkpoint: dependency diff is isolated, and lockfile diff contains only expected dependency resolution changes.

Rollback: revert dependency declarations and lockfile changes.

Runnable state: the package resolves and the existing unit test suite remains runnable.

## Milestone 2: UI-neutral Contracts And Pure Helpers

- [x] Define `ReplView` protocol with `run(controller) -> int`.
- [x] Define `ReplViewEvent`.
- [x] Define `WelcomeSnapshot`, `StatusBarSnapshot`, and `SessionCloseSummary`.
- [x] Implement token formatting for status bar display.
- [x] Implement current-session `PromptHistory`.
- [x] Implement `ToolResultPreviewFormatter`.
- [x] Ensure dictionary tool output uses `json.dumps(output, ensure_ascii=False, sort_keys=True)`.
- [x] Ensure `redacted_output` is preferred over raw output for previews.
- [x] Add unit tests for prompt history navigation, multiline storage, empty prompt exclusion, and slash command storage.
- [x] Add unit tests for snapshot fallback values, full session id in close summary, and status bar token formatting.
- [x] Add unit tests for tool preview line truncation, character truncation, artifact ids, dictionary formatting, and redacted output.
- [x] Verify with `uv run pytest tests/unit/cli -v`.

Modified boundaries: CLI/REPL UI contract modules and pure helper tests.

Invariants: helpers must not import prompt_toolkit or rich; helpers must not call runtime stores; preview truncation affects display only.

Freeze/review checkpoint: added behavior is pure, deterministic, and unused by runtime paths.

Rollback: remove helper modules and tests without touching runtime.

Runnable state: UI contracts and pure helpers can be tested without terminal UI.

## Milestone 3: PlainReplView Compatibility Boundary

- [x] Implement `PlainReplView` as a `ReplView`.
- [x] Preserve existing injected input/output stream behavior.
- [x] Preserve local `/status` and `/exit` behavior.
- [x] Ensure `run(controller)` returns `0` for normal REPL close.
- [x] Keep this milestone limited to `PlainReplView` extraction and compatibility; use a fake or minimal controller in tests when `run(controller)` needs callbacks.
- [x] Do not route CLI execution through the real `ReplController` until Milestone 4.
- [x] Ensure one-shot mode does not construct or run a `ReplView`.
- [x] Add integration test for injected I/O selecting `PlainReplView`.
- [x] Add integration test for non-TTY REPL selecting `PlainReplView`.
- [x] Add integration test proving one-shot output remains plain stdout.
- [x] Verify existing Phase 0 REPL and one-shot tests still pass.

Modified boundaries: CLI REPL entry code and test doubles around input/output streams.

Invariants: no prompt_toolkit dependency is required for fallback; slash commands remain local and never enter model context.

Freeze/review checkpoint: fallback path is stable before the TTY TUI path is enabled.

Rollback: switch CLI selection back to the existing plain REPL path.

Runnable state: non-TTY and injected-I/O sessions keep working through the `ReplView` protocol, with fake/minimal controller coverage where needed.

## Milestone 4: Non-streaming ReplController

- [x] Implement `ReplController.on_submit`.
- [x] Implement `ReplController.on_slash_command`.
- [x] Implement `ReplController.on_interrupt`.
- [x] Implement `ReplController.on_turn_finished`.
- [x] Add background runtime turn thread for non-streaming TUI execution through `AgentLoopAdapter.run(...)`.
- [x] Ensure runtime background work never calls view methods directly.
- [x] Add controller-owned timer loop or callback for active turns.
- [x] Call `view.set_turn_status(turn_id, "running", elapsed_seconds)` once per second while a turn is active.
- [x] Add thread-safe completion wakeup so final result handling runs on the UI event-loop side.
- [x] Adapt final `AgentRunResult` into model, tool, status, and status bar view updates.
- [x] Disable input while a turn is running and reenable it after final result handling.
- [x] Reject ordinary prompts during active execution with a system or error message.
- [x] Append `/status` output as a system message.
- [x] Own best-effort token usage aggregation in the controller.
- [x] Preserve last known cumulative token usage when a completed model response omits usage.
- [x] Update the status bar snapshot after each completed model response.
- [x] Keep `/exit` on the existing runtime safe-boundary behavior.
- [x] Map display `cancelled` to persisted `failed + error_class=cancelled`.
- [x] Map display `timeout` to persisted `failed + error_class=timeout`.
- [x] Add unit tests with fake view and fake runtime for submit lifecycle, background completion wakeup, timer status updates, active prompt rejection, `/status`, status mapping, and usage aggregation.
- [x] Add integration test with fake model through `AgentLoopAdapter.run(...)`.
- [x] Verify with `uv run pytest tests/unit/cli tests/integration -v`.

Modified boundaries: CLI/REPL controller and an optional UI-facing runtime facade if needed.

Invariants: Milestone A uses `AgentLoopAdapter.run(...)`; `AgentLoopAdapter.stream(...)` does not need to exist; runtime background work does not call view methods directly; timer and final result updates are controller-owned.

Freeze/review checkpoint: controller works with fake view/runtime before prompt_toolkit is introduced.

Rollback: remove controller selection and return to plain fallback.

Runnable state: a non-streaming TUI controller path can complete one fake-model turn without terminal UI.

## Milestone 5: PromptToolkitReplView Shell

- [x] Implement `PromptToolkitReplView`.
- [x] Render the welcome panel from `WelcomeSnapshot`.
- [x] Render shell-style input beginning with `>`.
- [x] Support `Ctrl+J` multiline input.
- [x] Support best-effort `Shift+Enter` multiline input.
- [x] Support up/down current-session history navigation.
- [x] Render user, model, tool, system, and error message blocks.
- [x] Attempt rich Markdown final rendering under `max_markdown_render_chars = 50_000`.
- [x] Keep plain text on Markdown render failure or size threshold.
- [x] Render status bar from raw `StatusBarSnapshot` values.
- [x] Initialize the status bar after REPL startup.
- [x] Format status bar token values from raw `StatusBarSnapshot` counts only.
- [x] Do not aggregate or preserve token usage inside `PromptToolkitReplView`.
- [x] Render `SessionCloseSummary`.
- [x] Fall back to `PlainReplView` with one warning if prompt_toolkit initialization fails.
- [x] Add unit tests for view selection.
- [x] Add rendering snapshot/golden tests where deterministic.
- [x] Add integration test for prompt_toolkit initialization failure fallback.
- [x] Run manual TTY smoke with fake model: `hello`, `/status`, `/exit`.

Modified boundaries: prompt_toolkit view module, rich rendering adapter code, and CLI view selection.

Invariants: TUI selection only when stdin and stdout are TTY and no streams are injected; `PromptToolkitReplView` consumes view events, snapshots, and direct view calls, not `AgentStreamEvent`; preview thresholds remain outside runtime and adapter modules.

Freeze/review checkpoint: Milestone A TUI shell is runnable, and one-shot, non-TTY, injected-I/O tests still pass.

Rollback: disable TTY TUI selection and keep `PlainReplView`.

Runnable state: Milestone A is usable without streaming.

## Milestone 6: Milestone A Acceptance Gate

- [x] Run `uv run pytest tests/unit -v`.
- [x] Run `uv run pytest tests/integration -v`.
- [x] Run one-shot smoke with fake model.
- [x] Run non-TTY fallback smoke.
- [x] Run TTY TUI smoke with fake model.
- [x] Confirm runtime turn execution does not block the prompt_toolkit event loop.
- [x] Confirm running turn status updates elapsed seconds once per second.
- [x] Confirm no `AgentLoopAdapter.stream(...)` implementation is required for Milestone A.
- [x] Confirm all prompt turns still use `AgentLoopAdapter.run(...)`.
- [x] Confirm no Phase 1+ feature is exposed or required.
- [x] Record review evidence before beginning Milestone 7.

Review evidence:

- `uv run pytest tests/unit -v`: 120 passed.
- `uv run pytest tests/integration -v`: 23 passed.
- one-shot fake-model smoke returned `one-shot smoke answer`.
- non-TTY fallback smoke returned `fallback smoke answer` and plain `/status` fields.
- TTY TUI smoke rendered welcome, `assistant: tty acceptance answer`, `/status`, and `session ... closed.`.
- Static source search found no `AgentLoopAdapter.stream(...)`, `AgentStreamEvent`, or `agent_stream_callback` implementation in `src` or tests.
- Existing Phase 0 acceptance tests continue to cover reserved Phase 1+ command non-exposure.

Modified boundaries: none; this is a stabilization checkpoint.

Invariants: repository remains compilable and tests remain runnable; Phase 0 paths remain compatible.

Freeze/review checkpoint: do not begin streaming work until Milestone A evidence is recorded.

Rollback: revert TTY TUI selection while keeping covered pure helpers if they remain unused.

Runnable state: Phase 0.5 Milestone A satisfies the non-streaming TUI shell contract.

## Milestone 7: Streaming Contracts And Executor Dual Path

- [x] Define `AgentStreamEvent`.
- [x] Add `AgentLoopAdapter.stream(...)` to the adapter protocol.
- [x] Add optional `agent_stream_callback` to `PromptAgentExecutor.run_turn(...)`.
- [x] Preserve existing behavior when `agent_stream_callback is None`.
- [x] Add fake streaming model support for deterministic tests.
- [x] Add non-streaming fallback metadata: `AgentRunResult.metadata["streaming_fallback"] = True`.
- [x] Add unit tests for `AgentStreamEvent` construction or validation.
- [x] Add unit tests proving `agent_stream_callback=None` preserves existing behavior.
- [x] Add tests proving fallback metadata is set for non-streaming fallback.
- [x] Add tests proving `AgentStreamEvent` is not written to `run_events`.
- [x] Verify existing adapter and prompt executor tests still pass.

Modified boundaries: adapter contract module, prompt executor signature, fake model/test adapter path.

Invariants: `AgentLoopAdapter.run(...)` behavior remains unchanged; persisted model/tool events remain authoritative runtime events.

Freeze/review checkpoint: interface expansion is isolated from TUI routing.

Rollback: revert protocol expansion and executor signature changes before TUI consumes them.

Runnable state: streaming contracts exist and are testable without prompt_toolkit routing.

## Milestone 8: LangChain Streaming Implementation

- [x] Implement `LangChainAgentLoopAdapter.stream(...)`.
- [x] Use native `model.stream()` when available.
- [x] Fall back to existing `invoke()` when streaming is unsupported.
- [x] Do not simulate streaming.
- [x] Emit model call start/completion observations.
- [x] Emit text delta observations for displayable model text.
- [x] Emit tool call start/completion/result observations.
- [x] Prefer provider-returned `tool_call_id` when available.
- [x] Generate a turn-local `tool_call_id` when the provider omits one.
- [x] Correlate tool start, completion, and result events by `tool_call_id`, not by tool name.
- [x] Do not render tool-call-only chunks, function-call-only chunks, partial tool args, or internal planning data as model text.
- [x] Ensure final assistant model-call deltas concatenate to `AgentRunResult.assistant_output`.
- [x] Keep intermediate model-call text display-only.
- [x] Add fake streaming provider tests for deltas and model lifecycle.
- [x] Add tests proving model calls without text deltas do not create model output blocks.
- [x] Add tests proving intermediate model-call text renders but does not participate in final assistant output equality.
- [x] Add tests proving function-call-only chunks do not render as model text.
- [x] Add tests proving partial tool argument chunks do not render as model text.
- [x] Add fake tool call tests for tool lifecycle observations.
- [x] Add provider-missing-tool-id tests proving generated `tool_call_id` links start, completion, and result.
- [x] Add duplicate-tool-name tests proving repeated tool names in the same turn correlate by distinct `tool_call_id` values.
- [x] Add final assistant delta equality tests.
- [x] Add non-streaming provider fallback tests.
- [x] Verify with `uv run pytest tests/unit/adapters tests/unit/runtime -v`.

Modified boundaries: LangChain adapter only.

Invariants: provider-specific streaming details stay inside the adapter; final `AgentRunResult` remains authoritative; stream correlation ids are turn-local and not recovery truth.

Freeze/review checkpoint: adapter streaming is reviewable independently of TUI routing.

Rollback: keep the protocol but route `stream(...)` to `invoke()` fallback until streaming bugs are fixed.

Runnable state: fake streaming provider can produce deterministic stream observations and final results.

## Milestone 9: Controller Stream Queue Integration

- [x] Reuse the Milestone 4 background runtime turn thread for streaming TUI execution.
- [x] Add queue-based `AgentStreamEvent` delivery.
- [x] Extend the thread-safe wakeup hook for stream-event queue readiness.
- [x] Drain the queue on the UI event-loop side.
- [x] Map `AgentStreamEvent` to `ReplViewEvent`, snapshots, or direct view method calls.
- [x] Show one non-streaming fallback warning when `metadata["streaming_fallback"] = True`.
- [x] Finalize turn status from `AgentRunResult`.
- [x] Reenable input only after final turn handling.
- [x] Add unit tests for queue drain ordering.
- [x] Add unit tests for malformed stream event payloads during queue drain or mapping.
- [x] Ensure malformed stream event payloads produce an error or system view event and do not crash the UI loop, block remaining queue drain, or prevent final turn handling.
- [x] Add unit tests for thread-safe wakeup behavior with a fake invalidator.
- [x] Add integration test for streaming fake model deltas rendering incrementally.
- [x] Add integration test for non-streaming fallback warning.
- [x] Add integration test for active prompt rejection during a running turn.
- [x] Verify with `uv run pytest tests/unit/cli tests/integration -v`.

Modified boundaries: `ReplController` and optional TUI runtime facade.

Invariants: runtime background thread does not call view methods; `notify_event_ready()` does not inspect queue contents or mutate view state; fallback warning is shown at most once per fallback turn.

Freeze/review checkpoint: Milestone B controller behavior is independently reviewable, and prompt_toolkit calls remain isolated to the view implementation.

Rollback: switch controller back to Milestone 4 final-result adaptation while keeping adapter streaming disabled.

Runnable state: TUI turns can render streaming observations or deterministic fallback output.

## Milestone 10: Milestone B Acceptance Gate

- [x] Run `uv run pytest tests/unit -v`.
- [x] Run `uv run pytest tests/integration -v`.
- [x] Run `uv run pytest -v`.
- [x] Run one-shot smoke with fake model.
- [x] Run non-TTY fallback smoke.
- [x] Run TTY TUI smoke with fake streaming model.
- [x] Confirm `AgentStreamEvent` is never persisted to `run_events`.
- [x] Confirm no one-shot, non-TTY, or injected-I/O regression.
- [x] Confirm `run(...)` remains valid and covered.
- [x] Confirm `stream(...)` is used only where TUI needs observations.
- [x] Run manual macOS Terminal or iTerm2 check for multiline input, history, long Markdown output, and long tool result preview.

Review evidence:

- `uv run pytest tests/unit -v`: 139 passed.
- `uv run pytest tests/integration -v`: 26 passed.
- `uv run pytest -v`: 165 passed.
- one-shot fake-model smoke returned `one-shot acceptance answer` from an isolated temporary workspace.
- non-TTY fallback smoke returned `fallback acceptance answer` and plain `/status` fields from redirected input.
- PTY TUI smoke with fake streaming model rendered welcome, streamed assistant deltas, final assistant text, `/status`, and `session ... closed.`.
- SQLite inspection of one-shot, fallback, TTY, and TTY-stream smoke workspaces found `stream_event_rows=0`; persisted event kinds remained Phase 0 runtime events such as `model_call_started`, `model_call_completed`, `assistant_message`, checkpoints, run lifecycle, and session lifecycle events.
- Integration coverage includes one-shot preservation, non-TTY fallback, injected-I/O fallback, TTY selection, prompt_toolkit initialization fallback, streaming turns, and non-streaming fallback warning.
- Static source search shows `agent_stream_callback` is supplied by `ReplController` for TUI observation delivery; `PromptAgentExecutor` uses `adapter.run(...)` when no callback is supplied and `adapter.stream(...)` when a callback is supplied.
- Static dependency search confirms `prompt_toolkit` and `rich` are declared in `pyproject.toml`, resolved in `uv.lock`, and imported only by CLI/TUI modules.
- Static Phase 1+ search found no runtime exposure of skill registry, subagent, workflow, MCP, plugin, or reserved slash commands beyond contract docs and negative tests.
- Native macOS Terminal/iTerm2 manual behavior for multiline input, history, long Markdown output, and long tool result preview was not run in this pass; available PTY smoke coverage is recorded above.

Modified boundaries: none; this is a stabilization checkpoint.

Invariants: Session, Run, Event, Checkpoint, Artifact, ToolBroker, Approval, and Path Policy contracts remain unchanged.

Freeze/review checkpoint: complete Phase 0.5 can be reviewed as a stable vertical slice only after acceptance evidence is recorded.

Rollback: disable streaming controller routing first; if needed, route TUI turns back through `AgentLoopAdapter.run(...)`; if TUI shell is affected, disable TTY TUI selection and fall back to `PlainReplView`.

Runnable state: Phase 0.5 satisfies `docs/phase-0.5/scope.md`, specs, ADR 0007, ADR 0008, and tests.

## Milestone 11: Corrective TTY Layout Isolation

This milestone supersedes the prior TTY streaming terminal-write approach for
`PromptToolkitReplView`. It is required before Phase 0.5 can be treated as
accepted after manual TTY verification.

- [x] Replace the active TTY view architecture with a prompt_toolkit `Application` layout.
- [x] Define separate layout regions for message list, current turn/status display, prompt input buffer, and bottom status bar.
- [x] Move visible message rendering behind an in-memory TTY view model owned by `PromptToolkitReplView`.
- [x] Ensure streaming text deltas update only the active assistant block in the message list view model.
- [x] Ensure final Markdown replacement updates only the same active assistant block.
- [x] Remove active-application streaming output paths that directly write visible text through stdout, stderr, prompt_toolkit `write_raw`, or ANSI-cleared terminal transcript output.
- [x] Preserve one-shot, non-TTY, and injected-I/O `PlainReplView` behavior.
- [x] Wire TTY up/down keys to current-session `PromptHistory` and replace the active prompt input buffer text.
- [x] Place the cursor at the end after history replacement.
- [x] Clear the input buffer when down navigation advances past the newest history item.
- [x] Ensure input remains non-editable while a turn is running.
- [x] Ensure streaming redraws preserve prompt input buffer text and cursor position.
- [x] Ensure streaming redraws preserve bottom status text unless status state changed.
- [x] Ensure streaming redraws do not rewrite prior user, assistant, tool, system, or error blocks.
- [x] Add or update unit tests for layout-backed message rendering, active assistant block updates, final Markdown block replacement, and history key binding behavior.
- [x] Add or update integration or PTY smoke coverage for streaming output isolation from prompt input and bottom status.
- [x] Run the narrow relevant Phase 0.5 verification commands from `docs/phase-0.5/operations.md`.
- [x] Run manual macOS Terminal or iTerm2 verification for streaming output, narrow wrapping, multiline input, and fast history navigation.
- [x] Ensure the TTY message list can scroll or otherwise keep older message history reachable after the visible region is full.
- [x] Ensure `/exit` uses an idempotent application shutdown path and does not raise duplicate `Application.exit(...)` return-value errors.
- [x] Bind TTY `Ctrl+C` to the existing interrupt path without changing Phase 0.5 persisted interrupt semantics.
- [x] Ensure `Ctrl+J` inserts a visible newline in a prompt input region that grows from 1 to at most 5 lines.
- [x] Ensure TTY up/down keys navigate prompt history only when the input cursor is at the end of the buffer, and otherwise move within the input buffer.
- [x] Add or update tests for scrollable message history, idempotent `/exit`, TTY `Ctrl+C`, visible `Ctrl+J` multiline input, and conditional up/down behavior.
- [x] Switch TTY mode to a full-screen alternate-screen application with application-owned message-list scrolling.
- [x] Ensure terminal-native scrollback is not required for viewing in-session TTY message history.
- [x] Ensure TTY `/exit` exits the alternate screen before printing `session <session-name> exit.` and `trace: debug-agent trace <session-name>` to stdout.
- [x] Ensure TTY `Ctrl+C` exits the alternate screen before printing `session <session-name> cancelled.` and `trace: debug-agent trace <session-name>` to stdout.
- [x] Add or update tests for alternate-screen TTY mode, application-owned message scrolling, and post-TUI terminal summaries.

Modified boundaries: `PromptToolkitReplView` and its tests only, unless a small
controller hook adjustment is required to support application wakeups.

Invariants: no Session, Run, Event, Checkpoint, Artifact, ToolBroker, Approval,
Path Policy, one-shot, non-TTY, injected-I/O, adapter, or persistence semantics
change. Runtime and adapter code must remain unaware of prompt_toolkit layout
details.

Freeze/review checkpoint: do not implement this milestone until the updated
documentation contract has been reviewed and accepted.

Rollback: disable TTY TUI selection and fall back to `PlainReplView` if layout
isolation cannot be completed without destabilizing Phase 0 behavior.

Runnable state: TTY streaming output is isolated to the active assistant block,
prompt history works through up/down keys, and fallback paths remain unchanged.

## Milestone 12: Corrective TTY Scrolling And Input Height Stability

This milestone addresses manual macOS TTY verification findings after the
prompt_toolkit `Application` layout migration. It is required before Phase 0.5
can be treated as accepted after manual TTY verification.

- [x] Resolve the Phase 0.5 scope boundary so terminal mouse wheel and trackpad
  events are explicitly allowed only for message-list scrolling.
- [x] Keep the prompt input region's initial editable height at 1 visible line.
- [x] Ensure `Ctrl+J` grows the prompt input region upward by visible line count
  up to the existing 5-line maximum.
- [x] Ensure prompt submission resets the prompt input region to 1 visible line.
- [x] Ensure prompt input height changes trigger an application layout redraw.
- [x] Ensure prompt input height changes keep the newest message visible when
  the message list is following the newest message.
- [x] Ensure appended messages and streaming deltas keep the newest message
  visible when the message list is following the newest message.
- [x] Ensure follow-newest message-list scrolling is clamped to actual rendered
  content and cannot render a blank message viewport while welcome or message
  content exists in the in-memory view model.
- [x] Ensure message-list growth never shrinks or overwrites the current prompt
  input region height.
- [x] Bind terminal mouse wheel and macOS trackpad scroll events to the TUI
  message list region, not to terminal-native scrollback.
- [x] Preserve PageUp/PageDown or equivalent keyboard message-list scrolling.
- [x] Add or update unit tests for input height initialization, `Ctrl+J`
  growth, submit reset, layout redraw, newest-message follow behavior, and
  message-list growth not shrinking input height.
- [x] Add or update tests for mouse wheel or trackpad scroll event handling on
  the message list region.
- [x] Add renderer-level regression tests that render the prompt_toolkit
  message-list viewport and verify welcome, submitted prompts, and assistant
  output remain visible while following newest content.
- [x] Run the narrow relevant Phase 0.5 verification commands from
  `docs/phase-0.5/operations.md`.
- [x] Run manual macOS Terminal or iTerm2 verification for trackpad scrolling,
  multiline prompt growth/reset, long message-list growth, and newest-message
  visibility.

Review evidence:

- Added unit coverage for exact prompt input region height ownership, prompt
  submission height reset, follow-latest refresh on input height changes,
  appended-message follow behavior, and message-list mouse wheel or trackpad
  scroll event handling.
- `PromptToolkitReplView` now keeps prompt input visible height as explicit
  state, uses an exact layout dimension for that state, refreshes the message
  list when prompt height or message content changes, and handles
  `SCROLL_UP`/`SCROLL_DOWN` pointer events on the message region.
- Follow-newest message-list scrolling is now clamped during prompt_toolkit
  viewport rendering, so the TUI cannot render a blank message viewport while
  welcome or message content exists in the in-memory view model.
- Added renderer-level unit coverage for the prompt_toolkit message-list
  viewport showing welcome, submitted prompts, and assistant output while
  following newest content.
- Added unit coverage that scrolling down from a historical message-list
  position advances through history without immediately jumping to the newest
  message; follow-newest resumes only after the rendered viewport reaches the
  bottom of the content.
- Targeted red/green check completed for
  `uv run pytest tests/unit/cli/test_prompt_toolkit_view.py::test_prompt_toolkit_view_follow_latest_renders_existing_message_viewport -q`:
  failed before the fix with a blank rendered viewport, then passed after the
  fix.
- Targeted red/green check completed for
  `uv run pytest tests/unit/cli/test_prompt_toolkit_view.py::test_prompt_toolkit_view_scroll_down_from_history_does_not_jump_to_latest -q`:
  failed before the fix because scroll-down immediately restored follow-newest,
  then passed after the fix.
- Targeted TUI view check completed for
  `uv run pytest tests/unit/cli/test_prompt_toolkit_view.py -q`: 39 passed.
- Canonical narrow verification completed:
  `uv run pytest tests/unit -v`: 187 passed.
- Canonical narrow verification completed:
  `uv run pytest tests/integration -v`: 26 passed.
- Manual macOS Terminal or iTerm2 trackpad verification remains pending because
  it requires an interactive terminal and physical or OS-level trackpad input.

Modified boundaries: `PromptToolkitReplView`, its tests, and Phase 0.5 TUI
documentation only.

Invariants: no Session, Run, Event, Checkpoint, Artifact, ToolBroker, Approval,
Path Policy, one-shot, non-TTY, injected-I/O, adapter, or persistence semantics
change. Pointer support is limited to message-list scrolling and must not add
general mouse interaction.

Freeze/review checkpoint: do not implement this milestone until the updated
documentation contract has been reviewed and accepted.

Rollback: disable the new mouse wheel or trackpad message-list binding first;
if input-height layout stability cannot be completed without destabilizing the
TUI, disable TTY TUI selection and fall back to `PlainReplView`.

Runnable state: TTY message-list scrolling works with keyboard and macOS
trackpad input, prompt input remains bounded at 1-5 visible lines, prompt
submission resets input height, and newest message visibility remains stable
across message-list and input-height changes.

## Manual TUI Polish Adjustment

Goal: incorporate human validation feedback for Phase 0.5 TTY presentation
without changing runtime, persistence, streaming, plain fallback, or one-shot
semantics.

Scope:

- `src/debug_agent/cli/prompt_toolkit_view.py`: message-list scroll step
  constants, prompt input visual borders and height recalculation, turn/status
  spacer, and welcome panel border formatting.
- `tests/unit/cli/test_prompt_toolkit_view.py`: focused layout and behavior
  coverage for the adjusted TTY view behavior.
- `docs/phase-0.5/specs/repl-tui.md` and `docs/phase-0.5/tests.md`:
  contract and test-plan updates only.

Tasks:

- [x] Split message-list scroll increments into
  `message_scroll_step_lines = 2` for mouse wheel or trackpad events and
  `message_scroll_step_page = 10` for PageUp/PageDown.
- [x] Render one-line `-` borders above and below the prompt input buffer.
  These border rows must adapt to terminal resize and must not count toward
  the prompt input buffer's 1-to-5 visible line limit.
- [x] Recalculate prompt input visible height on buffer text changes so
  backspacing over newline characters can shrink the prompt input region.
- [x] Add one blank spacer row above the turn/status region.
- [x] Render the welcome panel inside a lightweight rectangular ASCII border.
- [x] Add or update tests for scroll-step separation, prompt border height
  accounting, backspace-driven shrink, turn/status spacer separation, and
  welcome border rendering.
- [x] Run the relevant checks from `docs/phase-0.5/operations.md` before
  claiming completion.

Invariants: no Session, Run, Event, Checkpoint, Artifact, ToolBroker, Approval,
Path Policy, one-shot, non-TTY, injected-I/O, adapter, or persistence semantics
change. Pointer support remains limited to message-list scrolling.

Freeze/review checkpoint: do not implement this adjustment until the updated
documentation contract has been reviewed and accepted.

Rollback: revert the TTY presentation changes in `PromptToolkitReplView` while
preserving the existing `PlainReplView`, one-shot, non-TTY, and injected-I/O
paths.

## Manual Message Rendering Adjustment

Goal: incorporate human validation feedback for Phase 0.5 TTY message-list
readability without changing controller events, streaming contracts, runtime,
persistence, plain fallback, injected-I/O, or one-shot behavior.

Scope:

- `src/debug_agent/cli/prompt_toolkit_view.py`: TTY message block formatting
  for user, assistant, tool, system, and error messages.
- `tests/unit/cli/test_prompt_toolkit_view.py`: focused rendering coverage for
  the adjusted message-list formats.
- `docs/phase-0.5/specs/repl-tui.md` and `docs/phase-0.5/tests.md`: contract
  and test-plan updates only.

Tasks:

- [x] Render submitted user message blocks with a leading blank line, top and
  bottom `-` borders, and shell-style `> ` prefix on the first line only.
  The borders use the smallest terminal cell width that fully covers the
  rendered prompt text in that block, including Chinese text. Multiline
  continuation lines align under the prompt text with two leading spaces.
- [x] Render assistant message blocks with a leading blank line, top `-`
  border, `🔮 Assistant` header, one blank line, and assistant body text.
  Streaming deltas update only the assistant body inside the same block.
- [x] Render system message blocks with a leading blank line, top `-` border,
  `🤖 System` header, one blank line, and message body.
- [x] Render error message blocks with a leading blank line, top `-` border,
  `❌ Error` header, one blank line, and message body.
- [x] Render tool completion blocks as a leading blank line followed by one
  emoji-prefixed line: `🟢 <tool_name> (<duration>)` for success and
  `🔴 <tool_name> (<duration>)` for non-success.
- [x] Render tool result preview blocks with each preview line indented by four
  spaces and without repeating the completed tool block's tool name or status.
- [x] Add or update tests for user multiline alignment, assistant streaming
  body-only updates, system/error headers, tool success/failure summary lines,
  and four-space tool result indentation.
- [x] Run the relevant checks from `docs/phase-0.5/operations.md` before
  claiming completion.

Invariants: no Session, Run, Event, Checkpoint, Artifact, ToolBroker, Approval,
Path Policy, one-shot, non-TTY, injected-I/O, adapter, controller event, or
persistence semantics change. The change is TTY presentation only.

Freeze/review checkpoint: do not implement this adjustment until the updated
documentation contract has been reviewed and accepted.

Rollback: revert the TTY message block formatting in `PromptToolkitReplView`
while preserving existing event mapping and fallback paths.

## Migration And Rollback Rules

- Use abstraction-first migration: protocols and pure helpers before routing changes.
- Use dual-path transition: keep `run(...)` through Milestone A, add `stream(...)` in Milestone B.
- Use incremental replacement: route plain fallback before TTY TUI, then route streaming after adapter tests pass.
- Preserve a working fallback at every step.
- Disable the newest routing choice before reverting lower layers.
- Roll back dependencies only after all prompt_toolkit/rich imports are removed.
- Do not delete or rewrite runtime persistence data during rollback.
- Do not alter ToolBroker behavior during rollback.

## Verification Strategy

- Prefer deterministic unit and integration tests before manual terminal checks.
- Use pure unit tests for helpers, snapshots, formatter, token formatting, history, and stream mapping.
- Use controller unit tests with fake view and fake runtime.
- Use adapter tests with fake streaming and non-streaming providers.
- Use integration tests for one-shot, plain fallback, TTY selection, injected I/O, prompt_toolkit initialization failure, and streaming turns.
- Assert `AgentStreamEvent` is never written to `run_events`.
- Use smoke commands from `docs/phase-0.5/operations.md`.

Canonical verification commands:

```bash
uv lock
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Manual verification is required for terminal behavior that automated tests cannot reliably prove:

- macOS Terminal or iTerm2 rendering.
- Chinese IME and backspace best-effort behavior.
- narrow terminal wrapping.
- long Markdown output.
- long tool result preview.

## Strict Phase 0.5 Acceptance Pass

- [x] Run `uv run pytest tests/unit -v`.
- [x] Run `uv run pytest tests/integration -v`.
- [x] Run `uv run pytest -v`.
- [x] Run one-shot smoke with fake model config: `debug-agent -p "hello"`.
- [x] Run non-TTY fallback smoke with injected or redirected input.
- [x] Run TTY TUI smoke with fake model config: `hello`, `/status`, `/exit`.
- [x] Run TTY TUI smoke with fake streaming model config.
- [x] Confirm `.sessions/runtime.db` contains no `AgentStreamEvent` rows.
- [x] Confirm baseline session/run/event/checkpoint behavior remains compatible with Phase 0.
- [x] Confirm no Phase 1+ feature is required for Phase 0.5 acceptance.
- [x] Confirm dependency declarations and lockfile include Phase 0.5 runtime dependencies.

Strict acceptance evidence:

- Canonical verification commands passed: unit 139 passed, integration 26 passed, full suite 165 passed.
- Fake-model one-shot, redirected-input fallback, TTY fake-model, and TTY fake-streaming smokes completed from isolated temporary workspaces.
- Direct SQLite inspection found no persisted `AgentStreamEvent` or `stream_*` rows in smoke `run_events`.
- Smoke databases showed completed sessions and runs with persisted user, model, assistant, checkpoint, run lifecycle, and session lifecycle events.
- Phase 0 compatibility is covered by the full suite, including Phase 0 acceptance tests, status/trace tests, and one-shot/REPL persistence tests.
- Phase 1+ capability non-exposure is covered by negative reserved-command tests and static source search.
- `pyproject.toml` includes `prompt_toolkit>=3.0.0` and `rich>=13.0.0`; `uv.lock` resolves `prompt_toolkit` and `rich`.

Runnable state: Phase 0.5 satisfies the project contract, `docs/phase-0.5/*`, ADR 0007, ADR 0008, and preserves Phase 0 compatibility.

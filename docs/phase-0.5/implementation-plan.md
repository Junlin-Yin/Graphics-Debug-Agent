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

- [ ] Define `ReplView` protocol with `run(controller) -> int`.
- [ ] Define `ReplViewEvent`.
- [ ] Define `WelcomeSnapshot`, `StatusBarSnapshot`, and `SessionCloseSummary`.
- [ ] Implement token formatting for status bar display.
- [ ] Implement current-session `PromptHistory`.
- [ ] Implement `ToolResultPreviewFormatter`.
- [ ] Ensure dictionary tool output uses `json.dumps(output, ensure_ascii=False, sort_keys=True)`.
- [ ] Ensure `redacted_output` is preferred over raw output for previews.
- [ ] Add unit tests for prompt history navigation, multiline storage, empty prompt exclusion, and slash command storage.
- [ ] Add unit tests for snapshot fallback values, full session id in close summary, and status bar token formatting.
- [ ] Add unit tests for tool preview line truncation, character truncation, artifact ids, dictionary formatting, and redacted output.
- [ ] Verify with `uv run pytest tests/unit/cli -v`.

Modified boundaries: CLI/REPL UI contract modules and pure helper tests.

Invariants: helpers must not import prompt_toolkit or rich; helpers must not call runtime stores; preview truncation affects display only.

Freeze/review checkpoint: added behavior is pure, deterministic, and unused by runtime paths.

Rollback: remove helper modules and tests without touching runtime.

Runnable state: UI contracts and pure helpers can be tested without terminal UI.

## Milestone 3: PlainReplView Compatibility Boundary

- [ ] Implement `PlainReplView` as a `ReplView`.
- [ ] Preserve existing injected input/output stream behavior.
- [ ] Preserve local `/status` and `/exit` behavior.
- [ ] Ensure `run(controller)` returns `0` for normal REPL close.
- [ ] Keep this milestone limited to `PlainReplView` extraction and compatibility; use a fake or minimal controller in tests when `run(controller)` needs callbacks.
- [ ] Do not route CLI execution through the real `ReplController` until Milestone 4.
- [ ] Ensure one-shot mode does not construct or run a `ReplView`.
- [ ] Add integration test for injected I/O selecting `PlainReplView`.
- [ ] Add integration test for non-TTY REPL selecting `PlainReplView`.
- [ ] Add integration test proving one-shot output remains plain stdout.
- [ ] Verify existing Phase 0 REPL and one-shot tests still pass.

Modified boundaries: CLI REPL entry code and test doubles around input/output streams.

Invariants: no prompt_toolkit dependency is required for fallback; slash commands remain local and never enter model context.

Freeze/review checkpoint: fallback path is stable before the TTY TUI path is enabled.

Rollback: switch CLI selection back to the existing plain REPL path.

Runnable state: non-TTY and injected-I/O sessions keep working through the `ReplView` protocol, with fake/minimal controller coverage where needed.

## Milestone 4: Non-streaming ReplController

- [ ] Implement `ReplController.on_submit`.
- [ ] Implement `ReplController.on_slash_command`.
- [ ] Implement `ReplController.on_interrupt`.
- [ ] Implement `ReplController.on_turn_finished`.
- [ ] Add background runtime turn thread for non-streaming TUI execution through `AgentLoopAdapter.run(...)`.
- [ ] Ensure runtime background work never calls view methods directly.
- [ ] Add controller-owned timer loop or callback for active turns.
- [ ] Call `view.set_turn_status(turn_id, "running", elapsed_seconds)` once per second while a turn is active.
- [ ] Add thread-safe completion wakeup so final result handling runs on the UI event-loop side.
- [ ] Adapt final `AgentRunResult` into model, tool, status, and status bar view updates.
- [ ] Disable input while a turn is running and reenable it after final result handling.
- [ ] Reject ordinary prompts during active execution with a system or error message.
- [ ] Append `/status` output as a system message.
- [ ] Own best-effort token usage aggregation in the controller.
- [ ] Preserve last known cumulative token usage when a completed model response omits usage.
- [ ] Update the status bar snapshot after each completed model response.
- [ ] Keep `/exit` on the existing runtime safe-boundary behavior.
- [ ] Map display `cancelled` to persisted `failed + error_class=cancelled`.
- [ ] Map display `timeout` to persisted `failed + error_class=timeout`.
- [ ] Add unit tests with fake view and fake runtime for submit lifecycle, background completion wakeup, timer status updates, active prompt rejection, `/status`, status mapping, and usage aggregation.
- [ ] Add integration test with fake model through `AgentLoopAdapter.run(...)`.
- [ ] Verify with `uv run pytest tests/unit/cli tests/integration -v`.

Modified boundaries: CLI/REPL controller and an optional UI-facing runtime facade if needed.

Invariants: Milestone A uses `AgentLoopAdapter.run(...)`; `AgentLoopAdapter.stream(...)` does not need to exist; runtime background work does not call view methods directly; timer and final result updates are controller-owned.

Freeze/review checkpoint: controller works with fake view/runtime before prompt_toolkit is introduced.

Rollback: remove controller selection and return to plain fallback.

Runnable state: a non-streaming TUI controller path can complete one fake-model turn without terminal UI.

## Milestone 5: PromptToolkitReplView Shell

- [ ] Implement `PromptToolkitReplView`.
- [ ] Render the welcome panel from `WelcomeSnapshot`.
- [ ] Render shell-style input beginning with `>`.
- [ ] Support `Ctrl+J` multiline input.
- [ ] Support best-effort `Shift+Enter` multiline input.
- [ ] Support up/down current-session history navigation.
- [ ] Render user, model, tool, system, and error message blocks.
- [ ] Attempt rich Markdown final rendering under `max_markdown_render_chars = 50_000`.
- [ ] Keep plain text on Markdown render failure or size threshold.
- [ ] Render status bar from raw `StatusBarSnapshot` values.
- [ ] Initialize the status bar after REPL startup.
- [ ] Format status bar token values from raw `StatusBarSnapshot` counts only.
- [ ] Do not aggregate or preserve token usage inside `PromptToolkitReplView`.
- [ ] Render `SessionCloseSummary`.
- [ ] Fall back to `PlainReplView` with one warning if prompt_toolkit initialization fails.
- [ ] Add unit tests for view selection.
- [ ] Add rendering snapshot/golden tests where deterministic.
- [ ] Add integration test for prompt_toolkit initialization failure fallback.
- [ ] Run manual TTY smoke with fake model: `hello`, `/status`, `/exit`.

Modified boundaries: prompt_toolkit view module, rich rendering adapter code, and CLI view selection.

Invariants: TUI selection only when stdin and stdout are TTY and no streams are injected; `PromptToolkitReplView` consumes view events, snapshots, and direct view calls, not `AgentStreamEvent`; preview thresholds remain outside runtime and adapter modules.

Freeze/review checkpoint: Milestone A TUI shell is runnable, and one-shot, non-TTY, injected-I/O tests still pass.

Rollback: disable TTY TUI selection and keep `PlainReplView`.

Runnable state: Milestone A is usable without streaming.

## Milestone 6: Milestone A Acceptance Gate

- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run one-shot smoke with fake model.
- [ ] Run non-TTY fallback smoke.
- [ ] Run TTY TUI smoke with fake model.
- [ ] Confirm runtime turn execution does not block the prompt_toolkit event loop.
- [ ] Confirm running turn status updates elapsed seconds once per second.
- [ ] Confirm no `AgentLoopAdapter.stream(...)` implementation is required for Milestone A.
- [ ] Confirm all prompt turns still use `AgentLoopAdapter.run(...)`.
- [ ] Confirm no Phase 1+ feature is exposed or required.
- [ ] Record review evidence before beginning Milestone 7.

Modified boundaries: none; this is a stabilization checkpoint.

Invariants: repository remains compilable and tests remain runnable; Phase 0 paths remain compatible.

Freeze/review checkpoint: do not begin streaming work until Milestone A evidence is recorded.

Rollback: revert TTY TUI selection while keeping covered pure helpers if they remain unused.

Runnable state: Phase 0.5 Milestone A satisfies the non-streaming TUI shell contract.

## Milestone 7: Streaming Contracts And Executor Dual Path

- [ ] Define `AgentStreamEvent`.
- [ ] Add `AgentLoopAdapter.stream(...)` to the adapter protocol.
- [ ] Add optional `agent_stream_callback` to `PromptAgentExecutor.run_turn(...)`.
- [ ] Preserve existing behavior when `agent_stream_callback is None`.
- [ ] Add fake streaming model support for deterministic tests.
- [ ] Add non-streaming fallback metadata: `AgentRunResult.metadata["streaming_fallback"] = True`.
- [ ] Add unit tests for `AgentStreamEvent` construction or validation.
- [ ] Add unit tests proving `agent_stream_callback=None` preserves existing behavior.
- [ ] Add tests proving fallback metadata is set for non-streaming fallback.
- [ ] Add tests proving `AgentStreamEvent` is not written to `run_events`.
- [ ] Verify existing adapter and prompt executor tests still pass.

Modified boundaries: adapter contract module, prompt executor signature, fake model/test adapter path.

Invariants: `AgentLoopAdapter.run(...)` behavior remains unchanged; persisted model/tool events remain authoritative runtime events.

Freeze/review checkpoint: interface expansion is isolated from TUI routing.

Rollback: revert protocol expansion and executor signature changes before TUI consumes them.

Runnable state: streaming contracts exist and are testable without prompt_toolkit routing.

## Milestone 8: LangChain Streaming Implementation

- [ ] Implement `LangChainAgentLoopAdapter.stream(...)`.
- [ ] Use native `model.stream()` when available.
- [ ] Fall back to existing `invoke()` when streaming is unsupported.
- [ ] Do not simulate streaming.
- [ ] Emit model call start/completion observations.
- [ ] Emit text delta observations for displayable model text.
- [ ] Emit tool call start/completion/result observations.
- [ ] Prefer provider-returned `tool_call_id` when available.
- [ ] Generate a turn-local `tool_call_id` when the provider omits one.
- [ ] Correlate tool start, completion, and result events by `tool_call_id`, not by tool name.
- [ ] Do not render tool-call-only chunks, function-call-only chunks, partial tool args, or internal planning data as model text.
- [ ] Ensure final assistant model-call deltas concatenate to `AgentRunResult.assistant_output`.
- [ ] Keep intermediate model-call text display-only.
- [ ] Add fake streaming provider tests for deltas and model lifecycle.
- [ ] Add tests proving model calls without text deltas do not create model output blocks.
- [ ] Add tests proving intermediate model-call text renders but does not participate in final assistant output equality.
- [ ] Add tests proving function-call-only chunks do not render as model text.
- [ ] Add tests proving partial tool argument chunks do not render as model text.
- [ ] Add fake tool call tests for tool lifecycle observations.
- [ ] Add provider-missing-tool-id tests proving generated `tool_call_id` links start, completion, and result.
- [ ] Add duplicate-tool-name tests proving repeated tool names in the same turn correlate by distinct `tool_call_id` values.
- [ ] Add final assistant delta equality tests.
- [ ] Add non-streaming provider fallback tests.
- [ ] Verify with `uv run pytest tests/unit/adapters tests/unit/runtime -v`.

Modified boundaries: LangChain adapter only.

Invariants: provider-specific streaming details stay inside the adapter; final `AgentRunResult` remains authoritative; stream correlation ids are turn-local and not recovery truth.

Freeze/review checkpoint: adapter streaming is reviewable independently of TUI routing.

Rollback: keep the protocol but route `stream(...)` to `invoke()` fallback until streaming bugs are fixed.

Runnable state: fake streaming provider can produce deterministic stream observations and final results.

## Milestone 9: Controller Stream Queue Integration

- [ ] Reuse the Milestone 4 background runtime turn thread for streaming TUI execution.
- [ ] Add queue-based `AgentStreamEvent` delivery.
- [ ] Extend the thread-safe wakeup hook for stream-event queue readiness.
- [ ] Drain the queue on the UI event-loop side.
- [ ] Map `AgentStreamEvent` to `ReplViewEvent`, snapshots, or direct view method calls.
- [ ] Show one non-streaming fallback warning when `metadata["streaming_fallback"] = True`.
- [ ] Finalize turn status from `AgentRunResult`.
- [ ] Reenable input only after final turn handling.
- [ ] Add unit tests for queue drain ordering.
- [ ] Add unit tests for malformed stream event payloads during queue drain or mapping.
- [ ] Ensure malformed stream event payloads produce an error or system view event and do not crash the UI loop, block remaining queue drain, or prevent final turn handling.
- [ ] Add unit tests for thread-safe wakeup behavior with a fake invalidator.
- [ ] Add integration test for streaming fake model deltas rendering incrementally.
- [ ] Add integration test for non-streaming fallback warning.
- [ ] Add integration test for active prompt rejection during a running turn.
- [ ] Verify with `uv run pytest tests/unit/cli tests/integration -v`.

Modified boundaries: `ReplController` and optional TUI runtime facade.

Invariants: runtime background thread does not call view methods; `notify_event_ready()` does not inspect queue contents or mutate view state; fallback warning is shown at most once per fallback turn.

Freeze/review checkpoint: Milestone B controller behavior is independently reviewable, and prompt_toolkit calls remain isolated to the view implementation.

Rollback: switch controller back to Milestone 4 final-result adaptation while keeping adapter streaming disabled.

Runnable state: TUI turns can render streaming observations or deterministic fallback output.

## Milestone 10: Milestone B Acceptance Gate

- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run `uv run pytest -v`.
- [ ] Run one-shot smoke with fake model.
- [ ] Run non-TTY fallback smoke.
- [ ] Run TTY TUI smoke with fake streaming model.
- [ ] Confirm `AgentStreamEvent` is never persisted to `run_events`.
- [ ] Confirm no one-shot, non-TTY, or injected-I/O regression.
- [ ] Confirm `run(...)` remains valid and covered.
- [ ] Confirm `stream(...)` is used only where TUI needs observations.
- [ ] Run manual macOS Terminal or iTerm2 check for multiline input, history, long Markdown output, and long tool result preview.

Modified boundaries: none; this is a stabilization checkpoint.

Invariants: Session, Run, Event, Checkpoint, Artifact, ToolBroker, Approval, and Path Policy contracts remain unchanged.

Freeze/review checkpoint: complete Phase 0.5 can be reviewed as a stable vertical slice only after acceptance evidence is recorded.

Rollback: disable streaming controller routing first; if needed, route TUI turns back through `AgentLoopAdapter.run(...)`; if TUI shell is affected, disable TTY TUI selection and fall back to `PlainReplView`.

Runnable state: Phase 0.5 satisfies `docs/phase-0.5/scope.md`, specs, ADR 0007, ADR 0008, and tests.

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

- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run `uv run pytest -v`.
- [ ] Run one-shot smoke with fake model config: `debug-agent -p "hello"`.
- [ ] Run non-TTY fallback smoke with injected or redirected input.
- [ ] Run TTY TUI smoke with fake model config: `hello`, `/status`, `/exit`.
- [ ] Run TTY TUI smoke with fake streaming model config.
- [ ] Confirm `.sessions/runtime.db` contains no `AgentStreamEvent` rows.
- [ ] Confirm baseline session/run/event/checkpoint behavior remains compatible with Phase 0.
- [ ] Confirm no Phase 1+ feature is required for Phase 0.5 acceptance.
- [ ] Confirm dependency declarations and lockfile include Phase 0.5 runtime dependencies.

Runnable state: Phase 0.5 satisfies the project contract, `docs/phase-0.5/*`, ADR 0007, ADR 0008, and preserves Phase 0 compatibility.

# ADR 0007: AgentLoopAdapter Streaming Observation Path

## Status

Accepted for Phase 0.5.

## Context

Phase 0 uses `AgentLoopAdapter.run(...)` as the boundary between Runtime Core
and the model framework. That path returns a complete `AgentRunResult` and lets
Runtime Core persist authoritative events, checkpoints, artifacts, and trace
data.

Phase 0.5 adds a REPL TUI. The TUI needs incremental observations for model
text, model-call lifecycle, and tool-call progress. Those observations improve
human feedback, but they are not recovery truth. Persisting token or delta level
UI events as runtime truth would couple TUI behavior to checkpoint and audit
semantics.

Provider streaming support is also uneven. Some model paths support native
streaming, while others only support a non-streaming invoke call.

## Decision

Add `AgentLoopAdapter.stream(...)` as a UI observation path beside the existing
authoritative `AgentLoopAdapter.run(...)` path.

`run(...)` remains the authoritative result path for one-shot, plain REPL,
tests, and future workflow reuse.

`stream(...)`:

- accepts the same request and context shape as `run(...)`.
- accepts `on_event: Callable[[AgentStreamEvent], None]`.
- returns the final `AgentRunResult`.
- emits model lifecycle, text delta, tool lifecycle, and tool result
  observations as `AgentStreamEvent`.

`AgentStreamEvent` is runtime-neutral observation data. It must never be written
as a persisted `run_events` row. Runtime events may copy stream correlation ids
into persisted payloads for trace correlation, but those ids are not recovery
truth and do not need to be stable across sessions.

The final assistant model call's text deltas must concatenate exactly to
`AgentRunResult.assistant_output`. Intermediate model-call text is display-only
and is not part of that equality requirement.

`LangChainAgentLoopAdapter.stream(...)` uses LangChain's native `model.stream()`
path when available. If the provider or model does not support streaming, the
adapter falls back to the existing non-streaming `invoke()` path. It must not
simulate streaming.

`PromptAgentExecutor.run_turn(...)` may accept an optional
`agent_stream_callback`. When absent, callers keep the existing non-streaming
behavior.

## Alternatives Considered

### Persist stream observations as runtime events

This would make every text delta auditable, but it would mix UI observation with
runtime truth. It would also create replay and recovery semantics for events
that only exist to improve terminal rendering.

### Replace `run(...)` with `stream(...)`

This would reduce adapter surface area, but it would force one-shot, plain REPL,
tests, and future workflow paths to depend on a UI-oriented event stream.

### Simulate streaming for non-streaming providers

This would make the UI look consistent, but it would create false delta
semantics and make provider behavior harder to debug.

### Poll persisted runtime events from the TUI

This keeps adapter interfaces smaller, but it couples rendering to persistence
latency and encourages the TUI to treat audit events as its presentation event
stream.

## Consequences

- TUI can render incremental model text and tool progress without owning runtime
  truth.
- Runtime persistence remains based on complete authoritative runtime events,
  checkpoints, and artifacts.
- Adapters have two public execution paths and must keep their final results
  consistent.
- Tests must cover final-delta equality, non-streaming fallback, and the rule
  that `AgentStreamEvent` is never persisted to `run_events`.
- Future providers can add true streaming support without changing Runtime Core
  persistence contracts.

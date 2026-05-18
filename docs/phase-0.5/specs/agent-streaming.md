# Phase 0.5 Agent Streaming Specification

## Boundary

Runtime Core calls model frameworks only through `AgentLoopAdapter`.

Phase 0.5 adds a streaming observation path for REPL TUI use. The existing `run(...)` path remains the authoritative result path for one-shot, plain REPL, tests, and future workflow reuse.

Streaming observations are not recovery truth and must not be persisted as `run_events`.

## Interfaces

```python
class AgentLoopAdapter:
    def run(self, request: AgentRunRequest, context: RunContext) -> AgentRunResult: ...

    def stream(
        self,
        request: AgentRunRequest,
        context: RunContext,
        on_event: Callable[[AgentStreamEvent], None],
    ) -> AgentRunResult: ...

    def cancel(self, run_id: str) -> None: ...
```

`stream(...)` returns the same final `AgentRunResult` shape as `run(...)`.

If `stream(...)` falls back to a non-streaming provider path, the returned result sets:

```python
AgentRunResult.metadata["streaming_fallback"] = True
```

Otherwise, this metadata key is absent or false.

```python
def run_turn(
    self,
    *,
    session: Session,
    run: Run,
    user_input: str,
    workspace_root: str,
    conversation: list[dict[str, Any]] | None = None,
    prompt_turn_counter: int = 1,
    agent_stream_callback: Callable[[AgentStreamEvent], None] | None = None,
) -> AgentRunResult: ...
```

`agent_stream_callback` defaults to `None`. When it is absent, callers may use the existing non-streaming behavior.

## AgentStreamEvent

`AgentStreamEvent` is a runtime-neutral stream observation event sent from the adapter or runtime turn path to the controller.

It is not a UI rendering instruction. It is not recovery truth.

```python
class AgentStreamEvent:
    kind: Literal[
        "stream_model_call_started",
        "stream_text_delta",
        "stream_model_call_completed",
        "stream_tool_call_started",
        "stream_tool_call_completed",
        "stream_tool_result",
    ]
    payload: dict
```

Payloads:

- `stream_model_call_started`: `{"model_call_id": str}`.
- `stream_text_delta`: `{"model_call_id": str, "text": str}`.
- `stream_model_call_completed`: `{"model_call_id": str, "is_final": bool, "usage": dict, "duration_ms": int}`.
- `stream_tool_call_started`: `{"tool_call_id": str, "model_call_id": str, "name": str, "args": dict}`.
- `stream_tool_call_completed`: `{"tool_call_id": str, "model_call_id": str, "name": str, "status": str, "duration_ms": int}`.
- `stream_tool_result`: `{"tool_call_id": str, "model_call_id": str, "output": str | dict | None, "redacted_output": str | None, "artifact_ids": list[str]}`.

Phase 0.5 supports only these event kinds.

`stream_tool_call_started.args` is an observation payload, not default UI output. The TUI displays tool name, status, and duration by default. Any argument display must use a redacted or short preview produced outside the view.

`stream_tool_result` is based on the current tool invocation result, not on a lookup from persisted `run_events`.

The controller must correlate `stream_tool_call_started`, `stream_tool_call_completed`, and `stream_tool_result` by `tool_call_id`, but `stream_tool_call_started` is presentation-silent. The TUI appends a visible tool call block only when `stream_tool_call_completed` arrives, then appends a separate preview-only result block when `stream_tool_result` arrives. The result preview block must not repeat the already displayed tool name or status. `stream_tool_result` intentionally does not include `name`; UI mapping must use the name already observed for that `tool_call_id` and must not append `tool: unknown`.

Provider tool-call observations with a missing or empty tool name are incomplete observations. The adapter must not emit tool lifecycle stream events for them and must not invoke ToolBroker with an empty tool name.

## ReplViewEvent

The controller maps `AgentStreamEvent` into rendering-layer events, snapshots, or direct view method calls.

```python
class ReplViewEvent:
    kind: Literal[
        "model_text_delta",
        "model_markdown_final",
        "tool_block",
        "system_message",
        "error_message",
    ]
    payload: dict
```

The view consumes `ReplViewEvent`, not `AgentStreamEvent`.

## Streaming Consistency Contract

- `stream(...)` calls `on_event` for each model-call lifecycle.
- Each model call has its own start and completion events.
- A model call that produces user-visible text emits `stream_text_delta`.
- Provider-visible content before a tool call may also emit `stream_text_delta`.
- Tool-call chunks, function-call-only chunks, partial tool args, and internal planning data must not render as model text.
- LangChain streaming tool-call chunks must be accumulated into complete tool call observations before ToolBroker invocation. If a provider streams tool arguments across `tool_call_chunks`, the adapter must reconstruct the final `args` dictionary instead of invoking ToolBroker with `{}`.
- If an intermediate model call has no displayable text, no empty model output block is rendered.
- The authoritative assistant message is the final `AgentRunResult` persisted by `PromptAgentExecutor`.
- The final assistant model call's `stream_text_delta` sequence must concatenate exactly to `AgentRunResult.assistant_output`.
- Intermediate model-call text is display-only and is not part of the `assistant_output` equality requirement.

## LangChain Streaming Path

`LangChainAgentLoopAdapter.stream(...)` uses LangChain's native `model.stream()` path when available.

If the current provider or model does not support streaming:

- fall back to the existing non-streaming `invoke()` path.
- do not simulate streaming.
- set `AgentRunResult.metadata["streaming_fallback"] = True`.
- the controller shows one system message after the result is returned:

```text
streaming unavailable for this model; using non-streaming response.
```

Fake or mock streaming providers are sufficient for Phase 0.5 streaming acceptance when a real provider does not support streaming.

## Correlation IDs

`model_call_id` and `tool_call_id` are turn-local correlation ids.

Rules:

- `model_call_id` links model lifecycle events, text deltas, and tool events for one model call.
- `tool_call_id` links tool start, completion, and result events.
- `tool_call_id` prefers the provider-returned tool call id when available.
- when the provider does not provide a tool call id, adapter or runtime code generates one for the current turn.
- ids are not recovery truth.
- ids do not need to be stable across sessions.

Runtime events such as `model_call_completed` and `tool_call_completed` may copy these ids into persisted payloads for trace correlation. `AgentStreamEvent` itself must never be written to `run_events`.

# Phase 0 Agent Adapter Specification

## Boundary

Runtime Core calls model frameworks only through `AgentLoopAdapter`.

LangChain may own the immediate model/tool-calling loop inside a single adapter call. It must not own:

- session lifecycle
- run lifecycle
- checkpoints
- workspace ownership
- artifact registry
- ToolBroker policy
- trace generation

## Interfaces

```python
class AgentRunRequest:
    session_id: str
    run_id: str
    user_input: str
    system_prompt: str
    conversation: list[dict]
    tools: list[dict]
    model_config: dict
    timeout_seconds: int | None
```

`tools` uses a runtime-owned schema, not a LangChain-specific object. The adapter converts this schema to the framework-specific representation.

LangChain tool callables created by the adapter must delegate tool execution to `ToolBroker.invoke(...)`. They must not call native tools directly and must not write tool audit events directly.

When a model returns tool calls, the adapter runs a bounded tool-calling loop
inside the single `AgentLoopAdapter.run(...)` call:

1. invoke the model with the composed prompt and runtime tool definitions
2. record `model_call_completed` for that model invocation
3. delegate each returned tool call to `ToolBroker.invoke(...)`
4. append the assistant tool-call message and corresponding tool result messages
5. invoke the model again until it returns a final assistant response

The adapter returns the final assistant response as `assistant_output` and returns
the standardized `ToolResult` payloads in `tool_results`. The bounded loop is an
internal Phase 0 safety guard against infinite tool-calling and is not a
configuration setting.

For a tool-using turn, the expected event order is:

```text
model_call_started
model_call_completed
tool_call_started
tool_call_completed
model_call_started
model_call_completed
```

Additional tool calls repeat the same completed-model-call then tool-call event
pattern before the next model invocation.

```python
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
```

`input_schema` is a JSON Schema subset:

- `type`
- `properties`
- `required`
- `description`
- `enum`
- scalar types: `string`, `integer`, `number`, `boolean`
- containers: `object`, `array`

```python
class RunContext:
    workspace_root: str
    artifact_root: str
    approval_mode: str
    cancellation_token: object | None
    metadata: dict
    model_event_recorder: callable | None
```

`model_event_recorder` is provided by Runtime Core. The adapter uses it to write
`model_call_started`, `model_call_completed`, and `model_call_failed` at the
boundary of each actual model invocation.

```python
class AgentRunResult:
    status: str
    assistant_output: str | None
    tool_results: list[dict]
    usage: dict
    error: dict | None
    metadata: dict
```

```python
class AgentLoopAdapter:
    def run(self, request: AgentRunRequest, context: RunContext) -> AgentRunResult: ...
    def cancel(self, run_id: str) -> None: ...
```

Allowed result statuses:

- `completed`
- `failed`
- `timeout`
- `cancelled`

## ModelFactory

`ModelFactory` reads the frozen config snapshot and creates a LangChain-compatible chat model.

Phase 0 does not implement full provider abstraction. It must isolate provider-specific setup inside ModelFactory so later adapters or provider paths do not change runtime contracts.

Phase 0 has built-in defaults for non-provider runtime settings such as timeout, temperature, token limits, and the default system prompt. It does not guess a provider or model. If provider/model cannot be resolved from `~/.debug-agent/config.toml` or environment-backed configuration, ModelFactory returns `config_error`. See `docs/phase-0/specs/config.md` for the Phase 0 config schema.

## Prompt Composition

Phase 0 prompt order:

1. runtime safety prefix
2. hardcoded default agent system prompt
3. current user input

Phase 0 does not inject prompt skills.

Phase 0 default system prompt:

```text
You are debug-agent, a local debugging assistant. Answer concisely and use only tools exposed by the runtime.
```

Phase 0 does not read `agent.toml`; agent registry begins in Phase 1.

## Timeout And Cancellation

Phase 0 boundaries:

- Adapter receives `timeout_seconds`.
- Adapter returns `timeout` if model call exceeds timeout.
- Adapter exposes `cancel(run_id)` for future control paths.
- Token-level resume is not supported.
- Mid-model-call resume is not supported.

Full cancellation propagation is Phase 2. Phase 0 only needs clean terminal state recording when timeout or cancellation is observed.

## Error Mapping

- Provider configuration failure: `config_error`
- Model API failure: `model_error`
- Adapter contract violation: `internal_error`
- Timeout: `timeout`
- Cancellation: `cancelled`

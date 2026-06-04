# ADR 0010: ModelContextFrame As The LLM-Visible Context Boundary

## Status

Accepted for Phase 1.

## Context

Phase 0 and Phase 0.5 keep REPL conversation history in memory and pass it to
the adapter along with a system prompt and current user input. Phase 1 adds
skills, compression, large-output omission, and token budget display. At that
point, durable conversation history is no longer the same object as the actual
model input.

The runtime needs a single boundary for what the model sees on each call so
token estimates, context compression, status display, and skill injection all
operate on the same data.

## Decision

Introduce `ModelContextFrame` as the ordinary task model-call LLM-visible
request boundary.

`ReplRuntime.conversation` remains durable LLM-visible working history. It is
not audit truth, may be modified by context omission or compression, and is not
the exact model-call input. It must not be used directly for token estimates or
context budget decisions.

Before every adapter model invocation, runtime builds a `ModelContextFrame`.
This includes follow-up model calls inside a tool-calling loop, not only the
first model call of a user turn.

Phase 1 `AgentRunRequest` is the adapter-call envelope. It carries
`model_context_frame` plus execution metadata such as session id, run id, model
config, timeout, and broker execution metadata. The adapter materializes
provider-legal messages from `ModelContextFrame.message_segments` and
provider-native tool bindings from `ModelContextFrame.tool_schema_bindings`; it
does not pass the frame verbatim to providers. It must not own prompt injection
policy or reconstruct model-visible context from separate `system_prompt`,
`conversation`, `user_input`, or `tools` fields.

Ordinary task `ModelContextFrame` includes:

- message segments for stable system message content.
- available skill headers.
- runtime-supplied active skill context as non-persistent frame segments with
  `role="system"` and `kind="runtime_active_skill_context"`.
- context summary, when present.
- retained raw conversation messages and live or unconsumed suffix messages.
- tool-loop messages when applicable.
- current user input when applicable.
- `tool_schema_bindings` describing the frozen model-visible tool set.

Tool schema bindings are not conversation messages and must not be serialized
into the stable system prompt. They remain provider-native tool bindings at
adapter call time, such as LangChain `bind_tools(...)`, while still being part
of the runtime-owned frame used for estimates and tests.

Token estimation, context window percentage, omission, compression decisions,
and status bar context display are based on `ModelContextFrame`.

Phase 1 uses a deterministic runtime-owned `TokenEstimator` for pre-call
context estimates. The estimator is local and conservative; it does not call the
provider before the real model call. Provider usage, when available after a
model call, is used for cumulative token usage rather than for pre-call context
budget decisions.

Runtime-owned compression calls use a separate compression frame. Compression
does not include the main agent system prompt, available skill headers,
model-visible tool schema bindings, active `SKILL.md` bodies, retained recent raw
messages, live or unconsumed suffix messages, or runtime-owned active skill
records, artifact refs, policy facts, or approval facts. Ordinary task
`ModelContextFrame` estimates still count those ordinary task inputs because
they are sent to the provider for ordinary task model calls.

## Alternatives Considered

### Estimate from raw conversation history

This is simpler, but it ignores system prompts, active skill context, omitted
tool results, summaries, and tool-loop messages. The status bar would not match
what the model actually receives.

### Let the adapter own final context construction

This keeps runtime smaller, but it lets framework-specific code own context
semantics and weakens runtime control over compression and skill injection.

## Consequences

- Prompt composition becomes explicit and testable.
- Token budget display can reflect the actual model-call input.
- Compression and active skill injection share one context boundary.
- Adapter implementations remain replaceable because model-visible context is a
  runtime object before framework conversion.

## Phase 3 Refinement

[ADR 0014](0014-terminal-recovery-checkpoints-durable-conversation.md) refines
the conversation truth referenced in this ADR.

For Phase 3, append-only `conversation_messages` becomes the durable
conversation truth. In-memory `ReplRuntime.conversation` is a projection used to
construct `ModelContextFrame`, not the authoritative durable working history.
`ModelContextFrame` remains the ordinary task model-call visibility boundary.

[ADR 0015](0015-normalized-error-taxonomy-narrow-runtime-retry.md) refines
model-visible failure observations. Tool and turn failures shown to the model use
a narrow projection of normalized runtime errors rather than the complete
internal error payload.

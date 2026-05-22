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
context boundary.

`ReplRuntime.conversation` remains durable LLM-visible working history. It is
not audit truth, may be modified by context omission or compression, and is not
the exact model-call input. It must not be used directly for token estimates or
context budget decisions.

Before every adapter model invocation, runtime builds a `ModelContextFrame`.
This includes follow-up model calls inside a tool-calling loop, not only the
first model call of a user turn.

Ordinary task `ModelContextFrame` includes:

- stable system message content.
- available skill headers and model-visible tool schemas.
- context summary, when present.
- retained conversation messages.
- runtime-supplied active skill context.
- tool-loop messages when applicable.
- current user input when applicable.

Token estimation, context window percentage, omission, compression decisions,
and status bar context display are based on `ModelContextFrame`.

Phase 1 uses a deterministic runtime-owned `TokenEstimator` for pre-call
context estimates. The estimator is local and conservative; it does not call the
provider before the real model call. Provider usage, when available after a
model call, is used for cumulative token usage rather than for pre-call context
budget decisions.

Runtime-owned compression calls use a separate compression frame. Compression
does not include the main agent system prompt, available skill headers,
model-visible tool schemas, or active `SKILL.md` bodies, because those are not
compressible durable conversation. Ordinary task `ModelContextFrame` estimates
still count those inputs because they are sent to the provider for ordinary task
model calls.

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

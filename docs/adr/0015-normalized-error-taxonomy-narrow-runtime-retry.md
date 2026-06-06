# ADR 0015: Normalized Error Taxonomy And Narrow Runtime Retry

## Status

Accepted for Phase 3 planning.

## Context

Earlier phases used a small set of error classes directly in tool results,
agent results, events, checkpoints, traces, and CLI exit paths. As more runtime
surfaces were added, those classes began mixing module boundaries, detailed
causes, presentation strings, and recovery behavior.

Phase 3 needs failure handling that can drive terminalization, retry decisions,
model-visible observations, trace rendering, and resume eligibility without
letting call sites invent ad hoc error reasons.

The runtime also needs a narrow retry mechanism for runtime-owned transient
failures and `output_token_limit_reached` continuation. This must not become a
generic step retry system, tool replay, token-level resume, or model-call result
replay.

## Decision

Use normalized error payloads with centrally defined `error_class` and `reason`
symbols.

`error_class` names the major runtime responsibility boundary, such as user
input, configuration, policy, model, tool, skill, persistence, runtime, UI, or
cancellation. `reason` names the fixed detailed cause within that class. Message
text and structured metadata carry concrete paths, provider names, durations,
exception summaries, retry attempts, or other diagnostic details.

Failure-class run events must carry the normalized error object at
`payload.error`. Event kind remains audit taxonomy; error taxonomy lives inside
the normalized error object. Existing event kinds such as `model_call_failed`,
`tool_call_failed`, `tool_call_denied`, `compression_failed`, and
`context_limit_exceeded` may remain even when their payloads use the new
taxonomy.

Model-visible error output is a narrow projection of the internal normalized
error. It includes only `error_class`, `reason`, `message`, and an
`artifact_ids` list that is empty when no artifact-backed diagnostic is exposed.
It must not expose internal scope, recoverability, arbitrary metadata, retry
policy, provider internals, or policy facts unless those details are summarized
in the message.

Retry is controlled by a central opt-in retry rule registry. The retry system
answers only whether a normalized error may be retried, which strategy applies,
and how many attempts are allowed. It does not own final failure handling.
After retry is disabled or exhausted, ordinary error handling decides whether to
continue the model/tool loop, abort the turn, terminalize session/run, write a
terminal recovery checkpoint when eligible, release ownership, or show user
guidance.

The only retry strategies in Phase 3 are:

- `repeat_call` for explicitly retry-safe runtime-owned transient failures.
- `continue_generation` for `output_token_limit_reached`.

`output_token_limit_reached` continuation is a new model call based on a
completed partial response. It is not token-level resume, accepted result
replay, or tool-mid-flight resume. Runtime must not accept partial output as the
final assistant message and must not execute incomplete tool calls or incomplete
tool arguments.

## Supersedes / Refines

- Refines [ADR 0003: SQLite Event Log Plus Checkpoint Snapshot](0003-sqlite-event-log-checkpoint.md)
  by standardizing failure facts stored in events and terminal checkpoint
  manifests.
- Refines [ADR 0010: ModelContextFrame As The LLM-Visible Context Boundary](0010-modelcontextframe-llm-visible-context-boundary.md)
  by defining the model-visible projection for tool and turn failure
  observations.
- Refines [ADR 0011: Layered Context Compression For Runtime Continuity](0011-layered-context-compression-continuity.md)
  by replacing coarse top-level compression/context error classes with
  normalized model-error reasons while preserving context failure as
  turn-scoped behavior where specified.
- Refines [ADR 0013: Runtime-Owned Todo Plan Continuity](0013-runtime-owned-todo-plan-continuity.md)
  by aligning Todo validation and persistence failures with the centralized
  tool/persistence error taxonomy.

## Alternatives Considered

### Keep top-level error classes as detailed causes

This keeps changes small, but classes such as `timeout`,
`compression_failed`, and `context_limit_exceeded` mix responsibility boundary
with cause. Recovery and retry policy become harder to define consistently.

### Let each module define free-form reason strings

This keeps modules locally flexible, but it prevents reliable tests, stable
trace rendering, and centralized retry policy. It also lets user-facing
messages drift into control-plane semantics.

### Expose full internal error payloads to the model

This gives the model more details, but it leaks policy facts, module metadata,
provider internals, retry state, and other diagnostics that are better kept in
events, trace, checkpoint manifests, or artifact records.

### Implement generic step-level retry

This would be more broadly powerful, but it crosses Phase 3 boundaries. Generic
step retry requires replay semantics for model/tool outcomes and risks
re-executing side effects.

### Automatically retry ordinary tool calls

This is unsafe by default because tools can have filesystem, shell, or external
side effects. Tool retry needs a future explicit retry-safe registry before it
can become a runtime behavior.

## Consequences

- Error classes and reasons become runtime truth and require schema/version,
  spec, and test updates when changed.
- Tests should assert normalized `error_class`, `reason`, scope, and relevant
  metadata instead of matching presentation strings.
- ToolBroker, model adapter, runtime-control handlers, persistence paths, and
  CLI boundaries must normalize errors through shared constructors or
  validation helpers.
- Trace/status/UI rendering can prefer `payload.error` while retaining limited
  legacy fallback only for databases allowed by the current schema policy.
- Retry remains deliberately narrow and does not weaken ToolBroker, approval,
  path policy, shell policy, checkpoint, or ownership boundaries.

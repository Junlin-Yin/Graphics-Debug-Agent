# Phase 3 Durable Conversation Spec

## Purpose

Phase 3 adds append-only `conversation_messages` as durable conversation truth.
Process-local in-memory conversation becomes a projection.

Durable conversation stores accepted model-visible message groups. It does not
store pending provider output, stream deltas, TUI state, approval drafts, tool
mid-flight state, or shell mid-flight state.

## Table Contract

The Phase 3 schema must include a `conversation_messages` table or equivalent
append-only store with these logical fields:

| Field | Meaning |
|---|---|
| `id` | Monotonic durable row id. |
| `session_id` | Owning session id. |
| `run_id` | Owning run id. |
| `turn_id` | Runtime turn id when applicable. |
| `message_index` | Monotonic per-run accepted message index. |
| `role` | `system`, `user`, `assistant`, `tool`, or `runtime`. |
| `kind` | Fixed message kind such as `user_input`, `assistant_output`, `tool_call`, `tool_result`, `failure_fact`, `cancellation_fact`, or `compression_summary`. |
| `content_json` | Inline structured content when below inline limits. |
| `artifact_id` | Artifact reference when content is stored externally. |
| `content_sha256` | Checksum of canonical content payload or artifact bytes/record content. |
| `metadata_json` | Allowlisted metadata. |
| `accepted_at` | Runtime acceptance timestamp. |
| `source_event_id` | Optional event id for audit correlation. |

Implementations may choose exact SQL names, but the Phase 3 contract requires
the logical fields needed to validate durable cuts and rebuild model-visible
conversation projection.

## Roles And Kinds

Allowed `role` values:

- `system`: runtime-authored durable model-visible system messages, including
  compression summaries when they are intentionally durable conversation.
- `user`: accepted user input.
- `assistant`: accepted final assistant output or complete accepted assistant
  tool-call message.
- `tool`: accepted tool observation returned to the model.
- `runtime`: accepted model-visible failure/cancellation facts authored by the
  runtime.

Allowed `kind` values:

- `user_input`
- `assistant_output`
- `assistant_tool_call`
- `tool_result`
- `failure_fact`
- `cancellation_fact`
- `compression_summary`

`runtime` messages are model-visible facts, not hidden control state. Hidden
state such as active skills, Todo Plan, approval mode, and config snapshots must
remain in their dedicated runtime stores and may be referenced by terminal
checkpoint manifests.

## Append Boundaries

Runtime may append a conversation message only after reaching an acceptance or
recovery boundary:

- user input has been accepted as the current turn input.
- assistant output is complete and accepted.
- assistant tool-call message is complete and valid enough to route through the
  provider/tool-call protocol.
- tool result has completed, been denied, timed out, or failed and has been
  normalized into a model-visible observation.
- model output token limit continuation has succeeded and final assistant output
  is accepted.
- running cancellation has been reduced to a durable cancellation fact.
- turn-scoped failure has been reduced to a durable failure fact.
- compression summary has been accepted as model-visible context.

Runtime must not append:

- stream deltas.
- partial assistant output.
- incomplete tool call names or arguments.
- provider request bodies.
- provider internal state.
- pending tool results.
- shell output before the shell command completes, times out, fails, or is
  cancelled and normalized.
- approval prompt drafts or unsubmitted approval input.
- TUI presentation blocks.

## Metadata Allowlist

`metadata_json` may include:

- `turn_id`.
- `tool_call_id`.
- `tool_name`.
- normalized `error_class` and `reason` for failure/cancellation facts.
- `artifact_ids`.
- `retry_attempt`.
- `continuation_attempt`.
- token count estimates.
- provider stop/finish reason after it has been normalized.
- source event id.
- compression generation id or summary source range.

`metadata_json` must not include:

- secrets.
- raw provider request or response objects when they exceed the model-visible
  accepted content.
- image bytes/base64.
- policy internals not meant for model-visible projection.
- process ids, owner process details, or stale proof details unless summarized
  by a model-visible failure/cancellation message.
- TUI state.

## Projection

Prompt Agent Runtime rebuilds process-local conversation from:

1. durable `conversation_messages` rows for the run up to the selected cut.
2. runtime-owned non-persistent injections such as active skill context, Todo
   Plan segment, approval mode, and current user input.
3. compression/retention logic defined by `ModelContextFrame` and context
   compression specs.

In-memory conversation is not authoritative. If it differs from
`conversation_messages`, durable rows win and the in-memory projection must be
rebuilt.

## Conversation Cut

A terminal recovery checkpoint references a durable conversation cut instead of
inlining full history.

The cut must include:

- `run_id`.
- highest included `message_index`.
- included message count.
- checksum over the canonical ordered rows in the cut.
- optional artifact checksums for artifact-backed content.

Resume must fail closed if:

- any referenced row is missing.
- rows are not contiguous for the cut.
- row session/run ownership does not match.
- checksum validation fails.
- an artifact-backed content reference is missing or checksum-invalid.
- the cut includes unsupported role/kind values for the current schema.

## Interaction With Context Compression

Compression summaries are not recovery truth by themselves. A compression
summary can be visible after resume only if it has been accepted and appended as
a `conversation_messages` row with `kind = "compression_summary"`.

Context snapshots remain provenance and inspection records. Resume must not
restore durable conversation from context snapshots.

## Interaction With Tool Results

Tool results are durable conversation only after ToolBroker returns a normalized
model-visible observation. Tool mid-flight state is never durable conversation.

Large tool output may be artifacted by existing artifact rules. The durable
conversation row must preserve the artifact reference and checksum facts needed
for cut validation.

## Interaction With Output Token Continuation

When a provider stops because of `output_token_limit_reached`, partial output is
not accepted as final assistant output.

Runtime may store an audit failure/continuation fact, but it must not append the
partial output as an accepted `assistant_output` unless continuation succeeds
and runtime constructs the final accepted assistant message.

Incomplete tool calls or incomplete tool arguments from partial output must not
be executed and must not be appended as accepted assistant tool-call messages.

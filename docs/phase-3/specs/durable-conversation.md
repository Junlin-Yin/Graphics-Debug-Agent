# Phase 3 Durable Conversation Spec

## Purpose

Phase 3 adds append-only `conversation_messages` as durable accepted
conversation fact truth. Process-local in-memory conversation becomes a
projection of those facts plus runtime-owned non-persistent injections.

Durable conversation stores accepted model-visible message groups. It does not
store pending provider output, stream deltas, TUI state, approval drafts, tool
mid-flight state, or shell mid-flight state.

## Table Contract

The Phase 3 schema must include a `conversation_messages` table or equivalent
append-only store with these logical fields:

| Field | Meaning |
| --- | --- |
| `id` | Monotonic durable row id. |
| `session_id` | Owning session id. |
| `run_id` | Owning run id. |
| `turn_id` | Runtime turn id when applicable. |
| `message_index` | Monotonic per-run accepted message index. |
| `message_group_id` | Stable id for the accepted model-visible message group. |
| `model_call_id` | Ordinary task model-call id when the row belongs to a model-call group. |
| `group_position` | Zero-based position of this row within its message group. |
| `group_status` | Group lifecycle marker. Accepted durable rows must use `closed`; `open` is reserved only for implementation-internal staging state that is not visible as accepted durable conversation truth. |
| `group_row_count` or equivalent completeness record | Deterministic expected row count for the closed message group, either stored on rows or in a companion accepted closed-group record. |
| `role` | `user`, `assistant`, `tool`, or `runtime`. |
| `kind` | Fixed message kind such as `user_input`, `assistant_output`, `assistant_tool_call`, `tool_result`, `failure_fact`, `cancellation_fact`, or `context_summary`. |
| `content_json` | Inline structured model-visible content when below inline limits. |
| `artifact_id` | Artifact reference when content is stored externally. |
| `content_sha256` | Checksum of canonical content payload or artifact bytes/record content. |
| `metadata_json` | Allowlisted metadata. |
| `accepted_at` | Runtime acceptance timestamp. |
| `source_event_id` | Optional event id for audit correlation. |

Implementations may choose exact SQL names, but the Phase 3 contract requires
the logical fields needed to validate durable fact cuts and rebuild
model-visible conversation projection.

Each accepted conversation row must have exactly one canonical content source:
either inline `content_json` or artifact-backed `artifact_id`. Inline rows must
have `content_json` present and `artifact_id = null`. Artifact-backed rows must
have `artifact_id` present and `content_json = null`.
Conversation rebuild, checksum validation, and projection rendering must use
the declared canonical content source consistently; runtime must fail closed if
both sources are missing or if both sources are populated.

`message_group_id`, `model_call_id`, `group_position`, and `group_status` are
explicit logical fields. Runtime must not rely on ad hoc reconstruction from
`turn_id`, event ids, contiguous row ids, or metadata alone to recover
model-call groups.
Recovery validation, compression grouping, non-evictable suffix selection, and
terminal checkpoint validation must use these logical fields as the only
authoritative group identifiers. Runtime must not duplicate
`message_group_id` or `model_call_id` in `metadata_json` as an alternate source
of truth.

`message_group_id` represents one accepted model-visible group, such as a user
input, a final assistant message, an assistant tool-call message, the tool
observations that close accepted tool calls for a model call, a runtime
failure/cancellation fact, or an accepted context summary. `model_call_id`
groups ordinary task model-call outputs for context compression and
non-evictable suffix rules. Rows outside an ordinary task model call, such as
runtime-authored cancellation facts, may have `model_call_id = null` while still
having a `message_group_id`.

An open group is a model-visible protocol group whose acceptance boundary has
not closed. Examples include partial provider text, partial tool-use fragments,
a complete assistant tool-call message whose required tool observation has not
yet been accepted, a multi-row group that has not been fully inserted, or a
runtime failure/cancellation fact that has not reached its recovery boundary.
Open groups are mid-flight state.

For a group with multiple rows, `group_position` must be contiguous starting at
`0`. Every accepted durable row in `conversation_messages` must carry
`group_status = "closed"`. Group completeness is validated from the closed
rows, their contiguous positions, and either the implementation's deterministic
group row count metadata or an accepted closed-group marker, if the
implementation uses one. A closed-group marker, if present, is accepted
durable truth and must not use `group_status = "open"`.

Every accepted multi-row group must have one deterministic completeness source:
either a logical group row-count field/record, or an accepted closed-group marker
that records the expected row count. The exact SQL shape may follow the
implementation's store conventions, but the completeness source is part of the
durable conversation contract and must be validated during fact-cut,
projection, checkpoint, resume, compression grouping, status, and trace reads.
Runtime must not infer multi-row group completeness only from contiguous row ids,
event ids, timestamps, or ad hoc metadata.

Terminal checkpoint fact-cut validation must fail closed if the cut truncates a
group, includes duplicate group positions, includes any open group, or includes
a tool result whose `tool_call_id` cannot be paired with an accepted assistant
tool call in the same closed model-call/tool-loop sequence. Terminal recovery
checkpoints must not include open groups. Open model, tool, provider, or shell
state is mid-flight state and is not resumable in Phase 3.

Phase 3 accepted durable conversation truth must commit only closed groups to
`conversation_messages`. If an implementation uses an internal staging state for
open groups, that state must not be visible to projection reads, fact-cut
validation, terminal checkpoint creation, resume validation, compression
grouping, or status/trace as accepted durable conversation truth. An accepted
`conversation_messages` row with `group_status = "open"` is invalid Phase 3
runtime truth and must make fact-cut validation fail closed.

Appending accepted conversation messages must be atomic with per-run
`message_index` allocation and the corresponding current projection-state
update. If any part of the append fails, runtime must not leave a
`message_index` gap, a half-inserted message group, or a projection state that
references messages that were not committed.

`message_index` is 1-based within a run. The first accepted durable
conversation row for a run has `message_index = 1`. Projection references,
including terminal checkpoint `message_refs` indexes and ranges, use the same
1-based index values as `message_index`. `group_position` remains 0-based within
its message group.

## Projection State Contract

Phase 3 must persist a mutable current conversation projection state for each
prompt run. This state exists to rebuild process-local conversation after
resume and to explain current projection in status/trace.

Logical projection state fields:

| Field | Meaning |
| --- | --- |
| `projection_state_id` | Stable id for the current projection state row. |
| `session_id` | Owning session id. |
| `run_id` | Owning run id. |
| `source_high_watermark` | Highest `conversation_messages.message_index` covered by this projection. |
| `message_refs_json` | Ordered message indexes or ranges used to rebuild in-memory conversation. |
| `projection_sha256` | Checksum over ordered refs plus referenced message content hashes. |
| `updated_at` | Last update timestamp. |
| `update_reason` | Audit-only reason: `message_append`, `omission`, or `compression`. |
| `source_event_id` | Optional event id for audit correlation. |

`conversation_projection_state` is mutable. Message append, omission, and
compression update the current projection state by overwriting the previous
state for the same run. This mutable row is current runtime state, not immutable
historical recovery truth.

Allowed `update_reason` values:

- `message_append`: accepted message facts were appended and the projection was
  extended without omission or compression.
- `omission`: old tool result content was omitted from the model-visible
  projection according to the context compression contract.
- `compression`: a rolling summary message was accepted and the model-visible
  projection was rebuilt around that summary and retained raw suffix.

`update_reason` is for audit and trace only. Resume eligibility and recovery do
not depend on it.

Normal runtime execution uses the process-local in-memory message list after it
has been built. It does not need to re-read projection state before every model
call. The persisted projection state exists for resume, terminal checkpoint
creation, user-confirmed stale fail-close, and status/trace inspection.

## Roles And Kinds

Allowed `role` values:

- `user`: accepted user input.
- `assistant`: accepted final assistant output or complete accepted assistant
  tool-call message.
- `tool`: accepted tool observation returned to the model.
- `runtime`: accepted model-visible failure/cancellation facts authored by the
  runtime, and runtime-authored context summaries that follow Phase 1 rolling
  summary semantics.

Allowed `kind` values:

- `user_input`
- `assistant_output`
- `assistant_tool_call`
- `tool_result`
- `failure_fact`
- `cancellation_fact`
- `context_summary`

`runtime` messages are model-visible facts, not hidden control state. Hidden
state such as active skills, Todo Plan, approval mode, and config snapshots must
remain in their dedicated runtime stores and may be referenced by terminal
checkpoint manifests.

For `failure_fact` and `cancellation_fact` rows, `content_json` stores the
model-visible normalized error projection:

```json
{
  "error_class": "tool_error",
  "reason": "tool_execution_timeout",
  "message": "shell_exec exceeded timeout_seconds.",
  "artifact_ids": []
}
```

`metadata_json` may repeat `error_class` and `reason` for query convenience and
may include correlation fields such as `turn_id`, `tool_call_id`,
`retry_attempt`, or `continuation_attempt`, but it is not the model-visible
error source. Resume rebuilds model-visible failure and cancellation messages
from `content_json`, not from `metadata_json`.

## Append Boundaries

Runtime may append a conversation message only after reaching an acceptance or
recovery boundary:

- user input has been accepted as the current turn input.
- assistant output is complete and accepted.
- assistant tool-call message has a complete provider/tool-call protocol shape,
  including complete tool-call id, name, and arguments. Malformed or partial
  tool-call fragments must not be accepted as `assistant_tool_call`; they must
  either be rejected before append or reduced to an accepted `failure_fact` at a
  recovery boundary.
- tool result has completed, been denied, timed out, or failed and has been
  normalized into a model-visible observation.
- model output token limit continuation has succeeded and final assistant output
  is accepted.
- running cancellation has been reduced to a durable cancellation fact.
- turn-scoped failure has been reduced to a durable failure fact.
- context summary has been accepted as model-visible continuity context.

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

Phase 3 conversation JSON surfaces inherit the Phase 2 UTF-8 JSON
serialization rule. Runtime-authored `content_json`, `metadata_json`,
projection-state JSON, and terminal-checkpoint conversation payload JSON must
preserve non-ASCII text as UTF-8 rather than ASCII `\uXXXX` escapes. Stable
checksum inputs may use a separate documented canonical byte representation.

## Checksum Canonicalization

Phase 3 uses the same canonical byte rules for durable conversation checksums,
projection checksums, terminal checkpoint manifest checksums, Todo Plan
checkpoint checksums, approval grant cuts, and recovery validation inputs.

Canonical JSON inputs must use:

- UTF-8 bytes.
- object keys sorted by Unicode codepoint.
- no insignificant whitespace.
- non-ASCII strings preserved as UTF-8, not ASCII `\uXXXX` escapes.
- deterministic JSON scalar forms: strings as JSON strings, booleans as
  `true`/`false`, null as `null`, and integers as base-10 JSON numbers.
- no floating point values, NaN, or Infinity in checksum inputs.

If a checksum input contains a type that cannot be represented by these rules,
or if a required checksum input is missing, runtime must fail closed. Runtime
must not best-effort skip fields, reserialize with implementation defaults, or
repair invalid checksum inputs during resume.

Before appending accepted durable conversation rows or writing terminal
checkpoint payloads, runtime-owned normalization must either convert content into
the supported canonical JSON scalar set or reject the append/checkpoint creation
before it becomes accepted durable truth. Unsupported numeric values, including
floating point values, NaN, and Infinity, must not be accepted and left for a
later resume-time checksum failure.

Conversation row `content_sha256` is computed as:

- inline `content_json`: SHA-256 over the canonical JSON bytes of
  `content_json`.
- artifact-backed content: the already verified artifact payload checksum from
  `ArtifactStore`. Conversation row validation uses the `artifact_id` plus that
  artifact checksum; it must not read an arbitrary filesystem path as a
  substitute for artifact validation.

Conversation fact-cut checksum is computed over accepted rows ordered by
`message_index`. Each row contributes a canonical JSON object containing only:

- `session_id`.
- `run_id`.
- `message_index`.
- `message_group_id`.
- `model_call_id`.
- `group_position`.
- `group_status`.
- `role`.
- `kind`.
- `content_sha256`.
- `artifact_id`.
- `metadata_json`, canonicalized with the same JSON rules.
- `source_event_id`.

The fact-cut checksum must not include physical row `id`, `accepted_at`, SQLite
insertion order beyond `message_index`, or presentation-only trace/status
fields.

Projection checksum is computed from a canonical JSON object containing:

- `run_id`.
- `source_high_watermark`.
- ordered `message_refs`.
- the `content_sha256` for every referenced `message_index` in projection
  order.

Projection checksum must not include `projection_state_id`, `updated_at`, or
`update_reason`; those are diagnostics or mutable projection-state facts, not
historical projection identity.

Terminal checkpoint `payload_sha256` is computed over the canonical JSON bytes
of the complete `payload_json`. Checksum fields already present inside the
payload are included as ordinary string fields in that payload checksum.

Todo Plan checkpoint checksum is computed from a canonical JSON object
containing:

- `run_id`.
- `plan_version`.
- ordered `items`, including each item's `index`, `content`, `status`,
  `activeForm`, and canonical `metadata`.

Todo Plan checkpoint checksum must not include `updated_at`.

Approval grant checksum is computed over approval grants for the same session
ordered by grant row id or monotonic grant sequence up to
`grant_high_watermark`. Each grant contributes its grant id/sequence, session
id, tool name, risk/access facts, deterministic approval scope signature,
approval decision, and source event id. It must not include wall-clock
timestamps, approval prompt text, unsubmitted approval input, reusable grant
secrets or tokens, or UI presentation fields.

Active skill and frozen snapshot references in terminal recovery checkpoints
are validated by stored snapshot ids and stored content hashes. Resume must not
re-read current skill source files, current config files, or current policy
files to recompute these hashes.

Wall-clock timestamps are audit facts and must not participate in recovery
checksums unless a future phase explicitly defines a timestamp as part of a
runtime identity contract.

Resume and terminal checkpoint validation must fail closed when any referenced
row, artifact, Todo Plan snapshot, approval grant cut, active skill snapshot,
frozen config/policy reference, or checksum input is missing,
ownership-mismatched, or checksum-invalid.

## Projection

Explicit resume is the only Phase 3 path that rebuilds process-local
conversation from durable rows.

Resume rebuilds process-local conversation from:

1. durable `conversation_messages` rows selected by the terminal checkpoint's
   checkpoint-frozen projection snapshot.
2. runtime-owned non-persistent injections such as active skill context, Todo
   Plan segment, approval mode, and the next current user input when a later
   turn begins.

Resume must not rerun compression selection, retention selection, omission, or
other context optimization to choose a different historical message projection.
Those operations may run only later, before an ordinary model call, after the
session has been revived and normal runtime execution resumes.

Ordinary runtime execution maintains process-local conversation and the
persisted current projection state together as messages append, omission runs,
or compression runs. Ordinary pre-call execution builds `ModelContextFrame`
from that current projection plus runtime-owned non-persistent injections such
as active skill context, Todo Plan segment, approval mode, and current user
input.

Ordinary execution must not silently repair process-local conversation by
rebuilding it from durable rows. If runtime detects that process-local
conversation, current projection state, or durable `conversation_messages`
disagree outside the explicit resume path, it must fail closed with a
normalized runtime or persistence error such as
`runtime_error/internal_invariant_failed` or
`persistence_error/conversation_cut_invalid`. That drift is not a recovery
entrypoint.

In-memory conversation is not durable truth, but it is the active execution
projection while the process is running. Durable rows win only during explicit
resume validation and restore.

## Conversation Fact Cut And Projection Snapshot

A terminal recovery checkpoint freezes both accepted conversation facts and the
current model-visible projection.

The fact cut must include:

- `run_id`.
- highest included `message_index`.
- included message count.
- checksum over the canonical ordered rows in the fact cut.
- optional artifact checksums for artifact-backed content.

An empty accepted conversation cut is valid for eligible idle terminalization
paths such as starting a REPL and immediately using `/exit`, normal graceful
shutdown, or an equivalent non-failure idle close. The empty cut uses
`highest_message_index = 0`, `message_count = 0`, no message rows, and the
documented checksum over the canonical empty ordered row list for the same
`run_id`. It is not valid for idle `Ctrl+C` or `Esc`, which writes a
session-scoped cancellation fact, or for terminal prompt failure, which requires
at least one closed accepted durable conversation group.

The projection snapshot must be copied into the terminal checkpoint payload from
the mutable current projection state at terminalization time. It must include:

- source `projection_state_id` for diagnostics.
- `source_high_watermark`.
- ordered `message_refs` indexes or ranges.
- checksum over ordered refs plus referenced message content hashes.

For an empty accepted conversation cut, the projection snapshot uses
`source_high_watermark = 0`, an empty `message_refs` list, and the documented
checksum over the canonical empty projection input for the same `run_id`.

Resume must use the checkpoint-frozen projection snapshot, not the mutable
`conversation_projection_state` row. The mutable row may be overwritten after a
later resume and therefore cannot be historical recovery truth.

Resume must fail closed if:

- any referenced row is missing.
- rows are not contiguous for the fact cut.
- the fact cut truncates a message group or includes an invalid group lifecycle.
- row session/run ownership does not match.
- fact cut checksum validation fails.
- projection snapshot checksum validation fails.
- projection snapshot references rows outside the fact cut.
- an artifact-backed content reference is missing or checksum-invalid.
- the fact cut or projection includes unsupported role/kind values for the
  current schema.

## Interaction With Context Compression

Context summaries are not recovery truth by themselves. A context summary can
be visible after resume only if it has been accepted and appended as a
`conversation_messages` row with `role = "runtime"` and
`kind = "context_summary"`.

The `context_summary` row is a runtime-authored model-visible continuity fact
derived from the validated compression result. It is not the compression model's
assistant response appended verbatim, and it does not make the compression call
an ordinary assistant answer.

Phase 3 prompt sessions/runs do not write Phase 1 `context` checkpoints or
`context_snapshots` as non-terminal provenance. Omission and compression must
persist their model-visible continuity effects through durable conversation
messages and the mutable conversation projection state. Resume must not restore
durable conversation from legacy context snapshots if they are encountered in a
corrupt, manually modified, or legacy database.

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

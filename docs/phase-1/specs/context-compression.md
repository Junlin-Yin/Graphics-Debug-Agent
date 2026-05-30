# Phase 1 Context Compression Specification

## Boundary

`ContextManager` manages LLM-visible context only.

It does not own authoritative business state such as session status, run status,
workflow state, artifacts, approval records, or skill snapshots. It may produce
context snapshots and request persistence updates through runtime stores.

Phase 1 context management runs under the runtime-owned query control plane.
The query control plane coordinates query composition, active skill injection,
token estimation, context-window display, automatic omission/compression checks,
and turn-scoped abort decisions before each adapter model invocation.

## Core Strategy

Phase 1 uses layered context reduction:

1. artifact large outputs.
2. omit earlier tool results after the configured omission threshold.
3. roll older evictable `model_call_group` history into a continuity summary
   after the configured compression threshold or proactive compression-input
   budget threshold.

Manual `/compress` uses the same rolling summary compression machinery as
automatic compression and is allowed only while idle. It does not run
old-tool-result omission.

Automatic omission and compression preserve a non-evictable raw suffix. The
suffix includes the configured recent raw `model_call_group` window and live or
unconsumed messages required by the next ordinary model call, such as current
user input, open model-call output, pending tool calls, fresh tool results that
no later ordinary model call has consumed, and current query/tool-loop buffers.
These messages are excluded from omission and compression and are sent unchanged
to the real model call.

## Large Output Artifacting

Large model responses and tool outputs remain governed by Phase 0 rules:

- content larger than the inline threshold is written to `ArtifactStore`.
- events and model-visible context use summary text and artifact ids.
- raw large output is not stored inline in SQLite event payloads when it can be
  stored as an artifact.

Model-visible replacement text should include the artifact id.

Example:

```text
[Tool output stored as artifact: art_123. Summary: 420 lines of pytest output with 3 failures.]
```

Large output artifacting happens when a tool result or model response is
recorded and prepared for model-visible conversation. It is not a later pre-call
optimization step. Pre-call omission and compression operate only on the
existing model-visible representation, such as artifact markers, summaries,
artifact ids, and omission markers.

## Context Settings

Context and execution settings live in `~/.debug-agent/config.toml`:

```toml
[context]
window_tokens = 200000
omit_old_tool_results_at_ratio = 0.60
compress_history_at_ratio = 0.80
retain_recent_model_calls = 4
compression_reserved_output_tokens = 10000

[execution]
default_shell_timeout_seconds = 300
```

`omit_old_tool_results_at_ratio` is the old-tool-result omission threshold.
`compress_history_at_ratio` is the history compression threshold.
`retain_recent_model_calls` is the number of most recent raw completed
`model_call_group` values retained in durable `ReplRuntime.conversation`, not in
the generated `ModelContextFrame`.
`compression_reserved_output_tokens` is the estimated output margin reserved for
the compression model call. It protects the compression call from filling the
entire context window with input and defaults to `10000`.
`window_tokens` is the hard context limit for Phase 1 context-limit failure.
`default_shell_timeout_seconds` is the default timeout for `shell_exec` when a
tool call does not provide `timeout_seconds`. It defaults to `300`.
If `[context]` is absent, these example values are the built-in Phase 1
defaults. If `[execution]` is absent, `default_shell_timeout_seconds = 300` is
the built-in Phase 1 default. Resolved context and execution settings are frozen
into `sessions.config_snapshot_json`.

Validation rules:

- `window_tokens` must be a positive integer.
- `omit_old_tool_results_at_ratio` must be greater than `0` and at most `1`.
- `compress_history_at_ratio` must be greater than `0` and at most `1`.
- `omit_old_tool_results_at_ratio` must be less than or equal to
  `compress_history_at_ratio`.
- `retain_recent_model_calls` must be a non-negative integer.
- `compression_reserved_output_tokens` must be a non-negative integer less than
  `window_tokens`.
- `default_shell_timeout_seconds` must be a positive integer.

Invalid context or execution settings fail with `config_error` before
session/run creation and do not write runtime rows.

## ModelContextFrame

Phase 1 estimates context usage from the `ModelContextFrame` generated for the
next model call. `ModelContextFrame` is a runtime-owned LLM-visible request
frame, not a provider message list. It contains message segments and
`tool_schema_bindings`.

`ReplRuntime.conversation` is durable LLM-visible working history, not audit
truth and not the exact model-call input. It may be modified by omission and
compression. Complete tool-call facts, full tool outputs, model outputs, and
artifacts remain available from `run_events` and `ArtifactStore`.

`ReplRuntime.conversation` must not be used directly for context budget display
or threshold decisions.

Context checks run before every adapter model invocation. This includes
follow-up model calls inside a tool-calling loop, not only the first model call
of a user turn.

Active `SKILL.md` content is included in `ModelContextFrame` estimates as
non-persistent segments with `role="system"` and
`kind="runtime_active_skill_context"`, but is not part of the compressible
conversation history. Loaded skill resource outputs are ordinary
conversation tool observations and may be omitted or compressed.

Ordinary task `ModelContextFrame` estimates include the complete model-call
input: stable system block, available skill headers, active `SKILL.md` context
as non-persistent `runtime_active_skill_context` frame segments, rolling
summary, retained raw conversation, live or unconsumed messages, current user
input, and model-visible tool schema bindings. Tool schema bindings are not
conversation messages and must not be serialized into the stable system prompt;
adapters materialize them through provider-native tool binding APIs. The stable
system block is not compressed, but it is still counted for ordinary task
context-window estimates because it is sent to the provider on every ordinary
task model call.

Durable `ReplRuntime.conversation` entries use message metadata to make
grouping and non-evictable suffix behavior deterministic. Minimum metadata
includes `seq`, `turn_id`, `model_call_id`, optional `tool_call_id`, `kind`,
`artifact_refs`, and `estimated_tokens`.

## Model Call Groups

The query control plane derives a `model_call_group` view from durable
conversation message metadata. The view is used for context selection, token
budget decisions, omission, and compression. It is not authoritative business
state and does not replace events, checkpoints, artifacts, approval records, or
skill snapshots.

Minimum derived group facts:

```text
model_call_id
turn_id
start_seq
end_seq
status: open | closed
consumed_by_later_model_call: bool
estimated_tokens
message_ids
```

A group is `closed` only after the assistant output for that `model_call_id` has
finished and every tool call emitted by that model call has a terminal tool
result. A group with streaming output, pending tool execution, or missing
terminal tool results is `open`.

Runtime marks a closed group as `consumed_by_later_model_call` only after at
least one later ordinary task model call has included that group's raw messages
in its input. Fresh tool results and other messages that have not yet been
consumed by a later ordinary task model call remain raw and non-evictable.

The non-evictable raw suffix is the union of:

- the newest `retain_recent_model_calls` raw completed groups.
- all open groups.
- all closed groups that have not been consumed by a later ordinary task model
  call.
- current user input and current query/tool-loop buffers that are not yet part
  of a closed consumed group.

A `model_call_group` is eligible for eviction only if:

- `status == closed`.
- `consumed_by_later_model_call == true`.
- it is outside the live or unconsumed raw suffix.
- it is outside the newest `retain_recent_model_calls` raw completed groups.

## Token Estimation

Phase 1 uses a deterministic runtime-owned `TokenEstimator` for pre-call context
estimates. It does not call the provider before the real model call.

The estimator must:

- accept the complete `ModelContextFrame`, including stable system content,
  active `SKILL.md` context, rolling summary, retained raw conversation, live or
  unconsumed messages, current user input, and model-visible tool schema
  bindings.
- return a conservative integer estimate.
- use deterministic local rules so tests do not require network access or
  provider-specific token APIs.
- include fixed structural overhead for messages and tool schema bindings.
- record enough metadata in context snapshots and events to explain which
  estimator version produced an estimate.

Phase 1 does not require a model-specific tokenizer dependency. A later phase
may replace or calibrate the estimator, but Phase 1 context-limit behavior is
defined in terms of this deterministic estimate.

## Tool Result Omission

Before a model call, if estimated `ModelContextFrame` size is strictly greater
than
`omit_old_tool_results_at_ratio * window_tokens`, `ContextManager` replaces
older eligible tool result messages outside the non-evictable raw suffix with
omission markers.

Marker:

```text
[Earlier tool result omitted for brevity. See artifact references or trace for full details.]
```

Rules:

- recent `retain_recent_model_calls` raw completed `model_call_group` values
  remain intact.
- live and unconsumed messages remain intact.
- tool call metadata may remain visible.
- artifact ids remain visible when available.
- omission mutates `ReplRuntime.conversation` by replacing older tool result
  message bodies with omission markers.
- persisted run events and artifacts are unchanged.
- omission writes a context snapshot and updates `runs.context_snapshot_id`.
- omission is not required to restore the prior in-memory conversation. The
  omitted facts remain recoverable for trace, audit, and future reconstruction
  from persisted events, artifacts, and context snapshots.

Purpose:

- reduce token pressure cheaply.
- avoid model calls solely for summarization while tool output is the main
  source of growth.

After omission, the REPL displays a system message with the optimization effect:

```text
Context optimized: reduced from 42.1K to 31.4K tokens by omitting earlier tool results.
```

After omission, runtime rebuilds and re-estimates the candidate
`ModelContextFrame`. Compression is decided from this re-estimated candidate,
not from the pre-omission estimate. If omission does not run, runtime reuses the
initial candidate estimate for compression decisions.

## Conversation Compression

Before a model call, `ContextManager` performs conversation compression when at
least one of these conditions is true:

- estimated `ModelContextFrame` size is strictly greater than
  `compress_history_at_ratio * window_tokens`.
- total estimated tokens for eligible evictable `model_call_group` values
  exceeds `compression_evicted_history_budget`.

If omission ran in the same optimization pass, this estimate is the rebuilt
post-omission `ModelContextFrame` estimate.

`compression_evicted_history_budget` is derived for each compression decision:

```text
compression_evicted_history_budget =
  window_tokens
  - estimated(previous_summary)
  - estimated(compression_prompt)
  - fixed structural overhead
  - compression_reserved_output_tokens
```

If this derived budget is less than or equal to zero and compression would need
to run, runtime fails the current turn with `compression_failed` before calling
the compression model.

Compression is a process:

1. select the oldest eligible evictable `model_call_group` values that fit
   within `compression_evicted_history_budget`, preserving chronological order.
2. call the model with previous summary, if any; the selected bounded evicted
   history; and the compression instruction to produce a replacement continuity
   summary.
3. replace the previous summary and selected evicted groups in
   `ReplRuntime.conversation` with the new summary, retaining recent raw groups
   and live or unconsumed suffix messages unchanged.
4. rebuild the `ModelContextFrame` from the replaced conversation state.
5. perform the real model call if the rebuilt frame fits within `window_tokens`.

Compression batch selection must start from the oldest eligible group and stop
before adding the next group would exceed `compression_evicted_history_budget`.
Runtime must not skip an older eligible group to compress a newer group. If the
oldest eligible group cannot fit in the compression frame after artifacting and
omission, the current turn fails with `compression_failed`. Phase 1 does not
perform map-reduce or repeated compression calls within one optimization pass.
One pre-call optimization pass may apply omission once and may run at most one
compression model call.

The non-evictable raw suffix must not be included in the compression model call.
Compression must not answer, reinterpret, summarize, or plan from current user
input, open model-call output, pending tool calls, fresh tool results, or other
messages that have not yet been consumed by a later ordinary task model call.

The compression model call is runtime-owned and tool-less. Runtime must not
expose model-visible tools to the compression call, must not enter the ordinary
tool loop, and must not append a compression assistant answer to durable
conversation.

Compression uses a separate `CompressionContextFrame`, not the ordinary task
`ModelContextFrame`. The compression frame includes these inputs in order:

- previous continuity summary from `ReplRuntime.conversation`, if present.
- bounded evicted history messages selected from eligible `model_call_group`
  values.
- compression instruction and schema prompt.

The compression frame excludes:

- main agent system prompt.
- available skill headers.
- model-visible tool schema bindings.
- active `SKILL.md` bodies.
- retained recent raw messages.
- live and unconsumed raw suffix messages.
- runtime-owned active skill records.
- runtime-owned artifact refs.
- runtime-owned policy or approval facts.

Those excluded inputs are stable system-block content, runtime-owned structured
state, retained raw context, or live messages that compression must not rewrite.
They still count toward ordinary task `ModelContextFrame` estimates after
compression. The compression summary may preserve visible artifact, active
skill, loaded skill resource, approval, or policy references only when those
references already appear in the previous summary or selected evicted history.
The runtime must not inject those facts into the compression frame as an
independent source of truth.

The compression model call is still an actual model call and must be audited
through the normal model-call event path. Runtime writes
`model_call_started` before the compression call and then writes either
`model_call_completed` or `model_call_failed`. These model-call events include
`purpose="compression"` in their payloads and use an empty model-visible tool
set. On successful compression, runtime then writes the context/compression
event, context snapshot, and checkpoint facts. This means a successful
compression call produces at least one model-call event pair plus the
compression-specific run events.

The compression model call is itself subject to context estimation. Runtime must
construct a compression input that fits within `window_tokens` before calling the
model. If runtime cannot build a valid compression input within `window_tokens`
while respecting `compression_reserved_output_tokens`, the current turn fails
with `compression_failed`. Runtime must not call the compression model and must
not fall through to the real model call.

The compression instruction must preserve:

- current task goal.
- key completed tasks or milestones.
- files inspected or modified.
- remaining work and next plan.
- key decisions and constraints.
- human-readable references to relevant artifacts when visible in history.
- human-readable references to active skills when visible in history.
- human-readable references to loaded skill resources when visible in
  history.
- approval or path-policy facts visible in history.

Compression must not ask the model to summarize authoritative runtime state as
the recovery source. Runtime truth remains structured.

The continuity summary is a rolling replacement state, not a delta. The
compression prompt must instruct the model to merge the previous summary and the
new bounded evicted history into a complete replacement summary.

The compression model response must be parseable as a continuity summary.
Minimum Phase 1 schema:

```json
{
  "task_goal": "string",
  "completed_work": ["string"],
  "inspected_or_modified_files": ["string"],
  "remaining_work": ["string"],
  "next_plan": ["string"],
  "key_decisions": ["string"],
  "constraints": ["string"],
  "visible_artifact_refs": ["string"],
  "visible_active_skills": ["string"],
  "visible_loaded_skill_resources": ["string"],
  "visible_policy_or_approval_facts": ["string"]
}
```

Required core fields: `task_goal` (string), `completed_work`,
`inspected_or_modified_files`, `remaining_work`, `next_plan`, `key_decisions`,
and `constraints` (all string arrays). These must be present with the correct
type; list fields may be empty arrays but must not be missing.

Optional continuity fields: `visible_artifact_refs`, `visible_active_skills`,
`visible_loaded_skill_resources`, and `visible_policy_or_approval_facts`
(string arrays). When missing, they default to empty arrays.

The parser extracts only the fields above from the model output. Extra fields
are ignored. The output is invalid and causes `compression_failed` when the
output is not a JSON object, is empty, a required core field is missing, or any
known field has the wrong type (for example, `task_goal` is not a string, or
`completed_work` is not an array of strings).
`visible_artifact_refs`, `visible_active_skills`,
`visible_loaded_skill_resources`, and `visible_policy_or_approval_facts`
are continuity fields only. They are populated only from previous summary or
evicted history that was already LLM-visible. They do not authorize, restore,
validate, or mutate runtime state.

Runtime, not the model, preserves:

- active skill records.
- frozen skill and resource snapshots.
- artifact ids.
- approval records.
- path policy and shell policy.
- context snapshot ids.

After compression, the REPL displays a system message with the optimization
effect:

```text
Context compressed: reduced from 88.0K to 24.5K tokens; retained 4 recent model calls.
```

## Context Limit Failure

After omission and compression, runtime must rebuild and re-estimate the next
`ModelContextFrame`.

If the rebuilt frame still exceeds the hard context limit (`window_tokens`),
runtime must:

- not call the model adapter for that turn.
- mark the UI turn as failed without terminalizing the session, runtime, or
  long-lived prompt run.
- keep the REPL available for the next user query.
- write a run event and checkpoint fact with
  `error_class="context_limit_exceeded"`.
- display this English UI message:

```text
Context window still exceeds the limit after compression. The current turn was aborted.
```

This failure is turn-scoped. It does not change the long-lived prompt run or
session into a terminal state.

Phase 1 adds `context_limit_exceeded` to the shared runtime error classes for
this turn-scoped failure. This error class must not cause `runs.status` or
`sessions.status` to become `failed` in a long-lived REPL prompt run. The prompt
run remains `running`.

Minimum run event:

```json
{
  "kind": "context_limit_exceeded",
  "payload": {
    "error_class": "context_limit_exceeded",
    "estimated_tokens": 212000,
    "window_tokens": 200000,
    "optimization_applied": ["omission", "compression"],
    "message": "Context window still exceeds the limit after compression. The current turn was aborted."
  }
}
```

Runtime also writes a `context` checkpoint fact after the event. The checkpoint
records the latest context snapshot id when one exists, the same
`error_class="context_limit_exceeded"`, and the token estimate facts. Its
authoritative statuses remain `session_status="running"` and
`run_status="running"` for a long-lived REPL prompt run.

For one-shot prompt runs, this same condition is terminal. Runtime still writes
the `context_limit_exceeded` run event and `context` checkpoint fact before
terminalization. It then marks the one-shot run and session as `failed`, records
`error_class="context_limit_exceeded"` in terminal error metadata, and exits
non-zero.

## Compression Failure

If the compression model call fails, returns empty output, or returns output
that cannot be parsed into a valid continuity summary, the current UI turn is
aborted. Runtime writes a `compression_failed` run event and a `context`
checkpoint fact with `error_class="compression_failed"`, displays an English
UI message, and returns the REPL to prompt input without terminalizing the
session or long-lived prompt run.

`compression_failed` also covers the case where runtime cannot construct a
compression model input that fits within `window_tokens` while respecting
`compression_reserved_output_tokens`, including the case where the oldest
eligible evictable `model_call_group` cannot fit.

When compression cannot proceed because the selected history cannot fit within
the compression input budget, the English UI message must make the recovery
boundary explicit:

```text
Context compression could not fit the oldest eligible history group. The current turn was aborted. Start a new session to continue with a fresh context window.
```

Runtime must not add Phase 1 recovery commands, forced history deletion,
map-reduce compression, or repeated compression calls to work around this
condition.

For one-shot prompt runs, compression failure is terminal after recording the
same event and checkpoint fact.

This behavior is consistent with `context_limit_exceeded`: the turn is scoped
as failed, but the REPL session remains usable.

## Manual `/compress`

`/compress`:

- is accepted only while the REPL is idle.
- uses the same rolling summary compression machinery as automatic compression,
  but manual triggering ignores the compression threshold when evictable history
  exists.
- skips old-tool-result omission and directly constructs a
  `CompressionContextFrame` from the previous summary, selected eligible
  evictable history, and compression instruction/schema prompt.
- writes the same context snapshot shape when compression actually runs.
- replaces the previous summary and selected evicted groups in
  `ReplRuntime.conversation` when compression actually runs.
- rebuilds the current or next `ModelContextFrame` from the replaced
  conversation state when compression actually runs.
- does not call skill activation or tool execution.
- does not alter active skills except by preserving their structured records.

If `ReplRuntime.conversation` is empty, `/compress` is a no-op and displays an
English system message such as:

```text
No compressible history.
```

If durable conversation exists but the `model_call_group` eligibility rules and
`retain_recent_model_calls` leave no evictable group, `/compress` is also a
no-op with the same message. Runtime must not call the compression model, write a
context snapshot, or mutate conversation for these no-op cases.

If manual `/compress` runs the compression model and the compression model call
fails, returns empty output, or returns output that cannot be parsed into a valid
continuity summary, runtime uses the same `compression_failed` event and
`context` checkpoint behavior as automatic compression failure. Because no
optimization succeeded, runtime must not write a context snapshot for that
manual `/compress`, must not mutate `ReplRuntime.conversation`, and must keep
the long-lived REPL prompt run and session non-terminal.

If the user types `/compact`, Phase 1 must treat it as unsupported unless a
separate contract change adds it as an alias. The project-contract command name
is `/compress`.

## Context Snapshot

Phase 1 enables `runs.context_snapshot_id`.

`context_snapshots` stores the post-optimization `ReplRuntime.conversation`
continuity state produced by omission and/or compression. It is not a copy of
the pre-compression full context, and it is not the final `ModelContextFrame`.

Raw pre-compression facts remain in `run_events` and artifacts. The final
`ModelContextFrame` is reconstructed by `PromptComposer` from stable system
content, the current `ReplRuntime.conversation`, runtime-supplied active skill
context, and the current user input or tool-loop messages. The snapshot shape
records enough continuity facts for trace, audit, continuity inspection, and
future design, but it is not an executable recovery source in Phase 1. Phase 1
does not implement restart or resume recovery from context snapshots.
Persistent context snapshots exist only for trace, audit, and continuity
inspection. No runtime code may resume or reconstruct working state from them.

Automatic omission and compression snapshots exclude the live and unconsumed raw
suffix. The snapshot records the prepared durable conversation continuity state;
live and unconsumed messages are sent only to the real model call.

Minimum required shape:

```python
class ContextSnapshot:
    context_snapshot_id: str
    session_id: str
    run_id: str
    created_at: str
    trigger: str  # see allowed values below
    source_checkpoint_id: str | None
    active_skill_records: list[dict]
    summary: str  # canonical JSON summary string; empty for omission-only snapshots
    retained_messages: list[dict]
    omitted_tool_result_count: int
    evicted_message_count: int
    evicted_model_call_group_count: int
    artifact_refs: list[str]
    token_estimate: dict
    payload_artifact_id: str | None
    version: int
```

`active_skill_records` entries:

```json
{
  "name": "systematic-debugging",
  "content_hash": "sha256:..."
}
```

Phase 1 stores context snapshots in SQLite by default. Snapshot payloads that
exceed 16 KiB when serialized are stored as text artifacts and referenced
through `payload_artifact_id`, consistent with the Phase 0 large-content
threshold.

Minimum table shape:

```sql
CREATE TABLE context_snapshots (
  context_snapshot_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  trigger TEXT NOT NULL,
  source_checkpoint_id TEXT,
  active_skill_records_json TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  retained_messages_json TEXT NOT NULL,
  omitted_tool_result_count INTEGER NOT NULL,
  evicted_message_count INTEGER NOT NULL DEFAULT 0,
  evicted_model_call_group_count INTEGER NOT NULL DEFAULT 0,
  artifact_refs_json TEXT NOT NULL,
  token_estimate_json TEXT NOT NULL,
  payload_artifact_id TEXT,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL
);
```

The SQLite row stores the post-optimization continuity summary, retained
messages from the mutated `ReplRuntime.conversation`, structured active skill
records, artifact ids, omission count, evicted message and model-call-group counts,
token estimates, and source checkpoint reference. It must not store raw large
tool/model outputs, skill bodies, the pre-compression full context, or the final
composed `ModelContextFrame`. Large payloads remain artifact-backed; if a
snapshot payload would exceed the normal inline persistence threshold, the large
JSON payload is stored as a text artifact and referenced through
`payload_artifact_id`.

Allowed `trigger` values:

- `manual`: user-triggered `/compress`.
- `omission`: automatic old-tool-result omission only.
- `compression`: automatic conversation compression only.
- `omission | compression`: one optimization pass applied omission and then
  compression.

One pre-call optimization pass writes at most one context snapshot. If omission
and compression both succeed in the same pass, runtime writes only the final
post-compression snapshot with trigger `omission | compression`; it must not
write a separate omission-only snapshot for the intermediate state.

For compression snapshots, `summary` stores the canonical JSON serialization of
the parsed continuity summary. For omission-only snapshots, `summary` is the
empty string. `retained_messages` and omission markers carry the prepared
continuity context for that snapshot.

## Checkpoint Shape

Compression writes a checkpoint after the context snapshot is saved.

Required checkpoint kind:

```text
context
```

State:

```json
{
  "session_status": "running",
  "run_status": "running",
  "prompt_turn_counter": 12,
  "context_snapshot_id": "ctx_abc",
  "active_skill_records": [
    {
      "name": "systematic-debugging",
      "content_hash": "sha256:..."
    }
  ],
  "latest_artifact_ids": ["art_123"],
  "latest_error_summary": null
}
```

The checkpoint records that compression happened and points at the structured
context snapshot. It does not store raw large outputs or skill bodies.

For context-limit failure after compression, the `context` checkpoint records
the failure fact and keeps `session_status` and `run_status` as `running` for a
long-lived REPL prompt run. It is not a terminal `error` checkpoint.

For one-shot context-limit failure, runtime writes the `context` checkpoint fact
first, then writes the normal terminal failure metadata for the run/session using
`error_class="context_limit_exceeded"`.

## Checkpoint Kinds

Phase 1 extends the Phase 0 checkpoint kinds with `context`:

- `turn`: written after a successful one-shot response or REPL turn.
- `terminal`: written when a run/session exits successfully.
- `error`: written before a failed terminal state.
- `context`: written after context optimization (omission, compression) or
  context-limit/compression-failure events.

## Prompt Composition After Compression

After compression, the next model call is composed from:

1. runtime safety prefix.
2. main agent system prompt.
3. stable active-skill formatter header.
4. available skill headers from the frozen skill registry snapshot.
5. active `SKILL.md` content reconstructed from frozen skill snapshots as
   non-persistent `ModelContextFrame` segments with `role="system"` and
   `kind="runtime_active_skill_context"`.
6. latest context summary from `ReplRuntime.conversation`, if present.
7. retained recent raw messages and live or unconsumed suffix messages from
   `ReplRuntime.conversation` or the current query state.
8. current user input or tool-loop messages when applicable.

Active `SKILL.md` content is reconstructed from structured active skill records,
not from the summary. It is injected before rolling summary and retained raw
conversation so retained raw groups and live messages remain contiguous. Loaded
skill resource outputs are reconstructed only if they are still present in
retained raw conversation; otherwise the model may call `load_skill_resource`
again.

Manual and automatic compression replace only the previous summary and selected
evicted `model_call_group` messages. They do not mutate or summarize the stable
system block, available skill headers, model-visible tool schema bindings, active
`SKILL.md` instructions, retained recent raw messages, or live/unconsumed
messages.

## Token Usage And Status Bar

Phase 1 tracks two token values:

- token usage: model provider usage observed after model calls, with fallback to
  deterministic estimates when provider usage is unavailable.
- context window usage: model-call input estimate computed by `TokenEstimator`
  from `ModelContextFrame` before model calls.

The displayed context window value is the frozen `window_tokens` setting from
the session config snapshot. If the user configures a non-default
`window_tokens`, both status-bar display and context-limit decisions use that
configured frozen value.

The bottom status bar order is:

```text
model: <model> | approval: <approval> | context: <used> / <window> (<pct>) | tokens: <used> used
```

Before the first context estimate or token-usage accounting event in a REPL
session, the status bar renders zero-valued fields instead of unavailable
placeholders:

```text
model: <model> | approval: <approval> | context: 0 | tokens: 0
```

Example:

```text
model: kimi-k2.5 | approval: semi-auto | context: 38.2k / 200k (19%) | tokens: 12.4k used
```

Update timing:

- before each model call, estimate `ModelContextFrame`, update context display,
  and decide whether omission or compression is needed.
- after omission or compression, update context display immediately and show a
  system message with reduced-from and reduced-to token estimates.
- after each model call, update cumulative token usage from provider usage when
  available, otherwise from deterministic estimates.
- do not perform timer-based context estimation in Phase 1.

## Comparison With Earlier Plan

Earlier planning treated `/compress` mainly as an idle slash command that writes
a conversation summary checkpoint.

The Phase 1 design uses a stronger layered strategy:

- artifacting handles large raw output first.
- old-tool-result omission handles older tool-result bulk without another model
  call.
- history compression handles broad conversation growth.
- manual `/compress` reuses the same summary machinery.

This is preferred because it reduces token pressure before it becomes an error,
keeps expensive summarization calls rarer, and gives both automatic and manual
compression the same recovery shape.

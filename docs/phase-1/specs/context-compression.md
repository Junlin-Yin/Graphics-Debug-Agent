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
3. summarize older conversation after the configured compression threshold.

Manual `/compress` uses the same summary path as threshold-B automatic
compression and is allowed only while idle.

Automatic compression preserves a current-turn protected suffix. The protected
suffix includes the current user input, current-turn assistant tool-call
messages, fresh tool results, and follow-up tool-loop messages. These messages
are excluded from the compression model call and appended unchanged to the real
model call after compression.

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

## Context Settings

Context settings live in `~/.debug-agent/config.toml`:

```toml
[context]
window_tokens = 200000
omit_old_tool_results_at_ratio = 0.60
compress_history_at_ratio = 0.80
retain_recent_turns = 4
```

`omit_old_tool_results_at_ratio` is the old-tool-result omission threshold.
`compress_history_at_ratio` is the history compression threshold.
`retain_recent_turns` applies to the durable `ReplRuntime.conversation` list,
not to the generated `ModelContextFrame`.
`window_tokens` is the hard context limit for Phase 1 context-limit failure.
If `[context]` is absent, these example values are the built-in Phase 1
defaults. Resolved context settings are frozen into `sessions.config_snapshot_json`.

Validation rules:

- `window_tokens` must be a positive integer.
- `omit_old_tool_results_at_ratio` must be greater than `0` and at most `1`.
- `compress_history_at_ratio` must be greater than `0` and at most `1`.
- `omit_old_tool_results_at_ratio` must be less than or equal to
  `compress_history_at_ratio`.
- `retain_recent_turns` must be a non-negative integer.

Invalid context settings fail session startup with `config_error`.

## ModelContextFrame

Phase 1 estimates context usage from the `ModelContextFrame` generated for the
next model call.

`ReplRuntime.conversation` is durable LLM-visible working history, not audit
truth and not the exact model-call input. It may be modified by omission and
compression. Complete tool-call facts, full tool outputs, model outputs, and
artifacts remain available from `run_events` and `ArtifactStore`.

`ReplRuntime.conversation` must not be used directly for context budget display
or threshold decisions.

Context checks run before every adapter model invocation. This includes
follow-up model calls inside a tool-calling loop, not only the first model call
of a user turn.

Active `SKILL.md` content is included in `ModelContextFrame` estimates but is
not part of the compressible conversation history. Loaded skill reference file
outputs are ordinary conversation tool observations and may be omitted or
compressed.

Ordinary task `ModelContextFrame` estimates include the complete model-call
input: stable system block, available skill headers, active `SKILL.md` context,
retained conversation, tool-loop messages, current user input, and
model-visible tool schemas. The stable system block is not compressed, but it is
still counted for ordinary task context-window estimates because it is sent to
the provider on every ordinary task model call.

Durable `ReplRuntime.conversation` entries use message metadata to make
safe-boundary behavior deterministic. Minimum metadata includes `turn_id`,
`model_call_id`, `tool_call_id`, `kind`, and `artifact_refs`. The current-turn
protected suffix is identified from that metadata and is excluded from
automatic compression.

## Token Estimation

Phase 1 uses a deterministic runtime-owned `TokenEstimator` for pre-call context
estimates. It does not call the provider before the real model call.

The estimator must:

- accept the complete `ModelContextFrame`, including stable system content,
  retained conversation, active `SKILL.md` context, tool-loop messages, current
  user input, and model-visible tool schemas.
- return a conservative integer estimate.
- use deterministic local rules so tests do not require network access or
  provider-specific token APIs.
- include fixed structural overhead for messages and tool schemas.
- record enough metadata in context snapshots and events to explain which
  estimator version produced an estimate.

Phase 1 does not require a model-specific tokenizer dependency. A later phase
may replace or calibrate the estimator, but Phase 1 context-limit behavior is
defined in terms of this deterministic estimate.

## Tool Result Omission

Before a model call, if estimated `ModelContextFrame` size exceeds
`omit_old_tool_results_at_ratio * window_tokens`, `ContextManager` replaces
older tool result messages outside the most recent `retain_recent_turns` turns
in `ReplRuntime.conversation` with omission markers.

Marker:

```text
[Earlier tool result omitted for brevity. See artifact references or trace for full details.]
```

Rules:

- recent `retain_recent_turns` turns from `ReplRuntime.conversation` remain
  intact.
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
not from the pre-omission estimate.

## Conversation Compression

Before a model call, if estimated `ModelContextFrame` size exceeds
`compress_history_at_ratio * window_tokens`, `ContextManager` performs
conversation compression.

If omission ran in the same optimization pass, this estimate is the rebuilt
post-omission `ModelContextFrame` estimate.

Compression is a two-step process:

1. call the model with a compression instruction and history before the previous
   safe boundary to produce a continuity summary.
2. replace the compressible portion of `ReplRuntime.conversation` with that
   summary plus retained recent messages.
3. rebuild the `ModelContextFrame` from the replaced conversation state.
4. append the protected suffix unchanged and perform the real model call.

The current-turn protected suffix must not be included in the compression model
call. Compression must not answer, reinterpret, summarize, or plan from the
current user input, current-turn assistant tool-call messages, fresh tool
results, or follow-up tool-loop messages.

The compression model call is runtime-owned and tool-less. Runtime must not
expose model-visible tools to the compression call, must not enter the ordinary
tool loop, and must not append a compression assistant answer to durable
conversation.

Compression uses a separate `CompressionContextFrame`, not the ordinary task
`ModelContextFrame`. The compression frame includes:

- compression instruction.
- compressible durable conversation history before the current-turn protected
  suffix.
- structured active skill refs, such as skill name and content hash.
- visible artifact refs.
- visible policy or approval facts when they appear in history.

The compression frame excludes:

- main agent system prompt.
- available skill headers.
- model-visible tool schemas.
- active `SKILL.md` bodies.
- current-turn protected suffix.

Those excluded inputs are either stable system-block content, runtime-owned
structured state, or current-turn messages that compression must not rewrite.
They still count toward ordinary task `ModelContextFrame` estimates after
compression.

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
model. If, after excluding the protected suffix and applying available
omission/retention rules, runtime still cannot build a valid compression input
within `window_tokens`, the current turn fails with `compression_failed`.
Runtime must not call the compression model and must not fall through to the
real model call.

The compression instruction must preserve:

- current task goal.
- key completed tasks or milestones.
- files inspected or modified.
- remaining work and next plan.
- key decisions and constraints.
- human-readable references to relevant artifacts when visible in history.
- human-readable references to active skills when visible in history.
- human-readable references to loaded skill reference files when visible in
  history.
- approval or path-policy facts visible in history.

Compression must not ask the model to summarize authoritative runtime state as
the recovery source. Runtime truth remains structured.

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
  "visible_active_skill_refs": ["string"],
  "visible_policy_or_approval_facts": ["string"]
}
```

The parser accepts only a JSON object with these keys and string-array values
for every list field. Empty strings, non-object output, missing required keys,
or non-string list entries are invalid and cause `compression_failed`.

Runtime, not the model, preserves:

- active skill refs.
- frozen skill and reference snapshots.
- artifact ids.
- approval records.
- path policy and shell policy.
- context snapshot ids.

After compression, the REPL displays a system message with the optimization
effect:

```text
Context compressed: reduced from 88.0K to 24.5K tokens; retained 4 recent turns.
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
compression model input that fits within `window_tokens`.

For one-shot prompt runs, compression failure is terminal after recording the
same event and checkpoint fact.

This behavior is consistent with `context_limit_exceeded`: the turn is scoped
as failed, but the REPL session remains usable.

## Manual `/compress`

`/compress`:

- is accepted only while the REPL is idle.
- uses the same implementation path as threshold-B compression, but manual
  triggering ignores the compression threshold when compressible history exists.
- writes the same context snapshot shape when compression actually runs.
- replaces the compressible portion of `ReplRuntime.conversation` when
  compression actually runs.
- rebuilds the current or next `ModelContextFrame` from the replaced
  conversation state when compression actually runs.
- does not call skill activation or tool execution.
- does not alter active skills except by preserving their structured refs.

If `ReplRuntime.conversation` is empty, `/compress` is a no-op and displays an
English system message such as:

```text
No compressible history.
```

If durable conversation exists but the safe-boundary and `retain_recent_turns`
rules leave no compressible prefix, `/compress` is also a no-op with the same
message. Runtime must not call the compression model, write a context snapshot,
or mutate conversation for these no-op cases.

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
must be sufficient for future recovery, but Phase 1 writes context snapshots
only for trace, audit, continuity inspection, and future recovery support. Phase
1 does not implement restart or resume recovery from context snapshots.

Automatic omission and compression snapshots exclude the current-turn protected
suffix. The snapshot records the prepared durable conversation context up to the
previous safe boundary; the protected suffix is appended only to the real model
call.

Recommended shape:

```python
class ContextSnapshot:
    context_snapshot_id: str
    session_id: str
    run_id: str
    created_at: str
    trigger: str  # see allowed values below
    source_checkpoint_id: str | None
    active_skill_refs: list[dict]
    summary: str  # canonical JSON summary string; empty for omission-only snapshots
    retained_messages: list[dict]
    omitted_tool_result_count: int
    artifact_refs: list[str]
    token_estimate: dict
    payload_artifact_id: str | None
    version: int
```

`active_skill_refs` entries:

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
  active_skill_refs_json TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  retained_messages_json TEXT NOT NULL,
  omitted_tool_result_count INTEGER NOT NULL,
  artifact_refs_json TEXT NOT NULL,
  token_estimate_json TEXT NOT NULL,
  payload_artifact_id TEXT,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL
);
```

The SQLite row stores the post-optimization continuity summary, retained
messages from the mutated `ReplRuntime.conversation`, structured active skill
refs, artifact ids, omission count, token estimates, and source checkpoint
reference. It must not store raw large tool/model outputs, skill bodies, the
pre-compression full context, or the final composed `ModelContextFrame`. Large
payloads remain artifact-backed; if a snapshot payload would exceed the normal
inline persistence threshold, the large JSON payload is stored as a text
artifact and referenced through `payload_artifact_id`.

Allowed `trigger` values:

- `manual`: user-triggered `/compress`.
- `omission`: automatic old-tool-result omission only.
- `compression`: automatic conversation compression only.
- `omission | compression`: one optimization pass applied omission and then
  compression.

For compression snapshots, `summary` stores the canonical JSON serialization of
the parsed continuity summary. For omission-only snapshots, `summary` is the
empty string. `retained_messages` and omission markers carry the prepared
continuity context for that snapshot.

## Checkpoint Shape

Compression writes a checkpoint after the context snapshot is saved.

Recommended checkpoint kind:

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
  "active_skill_refs": [
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
5. latest context summary from `ReplRuntime.conversation`, if present.
6. retained recent messages from `ReplRuntime.conversation`.
7. active `SKILL.md` content reconstructed from frozen skill snapshots.
8. current user input or tool-loop messages from the protected suffix.

Active `SKILL.md` content is reconstructed from structured refs, not from the
summary. Loaded skill reference file outputs are reconstructed only if they are
still present in retained conversation; otherwise the model may call
`load_skill_ref_file` again.

Manual and automatic compression replace only the compressible durable
conversation prefix. They do not mutate or summarize the stable system block,
available skill headers, model-visible tool schemas, or active `SKILL.md`
instructions.

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

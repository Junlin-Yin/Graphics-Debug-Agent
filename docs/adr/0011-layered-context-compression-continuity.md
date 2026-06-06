# ADR 0011: Layered Context Compression For Runtime Continuity

## Status

Accepted for Phase 1.

## Context

Long-running debug sessions accumulate large tool outputs, model responses,
file snippets, decisions, and task history. The runtime must keep model context
within the configured window without losing continuity.

Compression cannot be treated as a simple content-shortening operation. It must
preserve enough working memory for the agent to continue the same task
consistently after many turns.

At the same time, authoritative runtime state must stay structured. The model's
summary is LLM-visible memory, not recovery truth.

## Decision

Use layered context reduction:

1. store large model/tool outputs as artifacts and keep model-visible summaries
   plus artifact ids.
2. when `ModelContextFrame` exceeds `omit_old_tool_results_at_ratio`, mutate
   `ReplRuntime.conversation` by replacing old tool result content with omission
   markers while retaining recent raw `model_call_group` values, live or
   unconsumed messages, and artifact references.
3. when `ModelContextFrame` exceeds `compress_history_at_ratio` or eligible
   evictable history exceeds the derived compression input budget, run rolling
   conversation compression over bounded evicted `model_call_group` history.

Conversation compression is a continuity-preserving operation. The compression
prompt must preserve:

- current task goal.
- completed key tasks or milestones.
- files inspected or modified.
- remaining work and next plan.
- key decisions and constraints.
- visible references to artifacts, active skills, loaded skill resources,
  approvals, and policies when they appear in history.

Runtime, not the model summary, preserves authoritative state:

- active skill records.
- frozen skill and resource snapshots.
- artifact ids.
- approval records.
- path policy and shell policy.
- context snapshot ids.

Summary fields that mention visible artifacts, active skills, approvals, or
policies are continuity notes only. They may be populated only from previous
summary or evicted history that was already LLM-visible. Runtime must not inject
active skill records, artifact refs, approval facts, or policy facts into the
compression frame as independent sources of truth, and runtime must not restore
or trust authoritative state from the summary.

`context_snapshots` store the post-optimization `ReplRuntime.conversation`
continuity state produced by omission and/or compression. They do not store the
pre-compression full context and do not store the final composed
`ModelContextFrame`.

Raw pre-compression facts remain in `run_events` and artifacts. The final
`ModelContextFrame` is reconstructed by `PromptComposer` from stable system
content, the current `ReplRuntime.conversation`, runtime-supplied active skill
context as non-persistent frame segments with `role="system"` and
`kind="runtime_active_skill_context"`, the current user input or tool-loop
messages, and `tool_schema_bindings` that adapters materialize through
provider-native tool binding APIs. Context snapshots record enough continuity
facts for trace, audit, continuity inspection, and future design, but they are
not an executable recovery source in Phase 1. Phase 1 does not implement restart
or resume recovery from context snapshots.

For automatic omission and compression, the non-evictable raw suffix is excluded
from the compression model call and context snapshot. The suffix includes the
newest retained raw `model_call_group` values and live or unconsumed messages
such as current user input, open model-call output, pending tool calls, fresh
tool results that no later ordinary model call has consumed, and current
query/tool-loop buffers. The snapshot records prepared durable conversation
continuity state; the non-evictable raw suffix is sent only to the real model
call.

Snapshot triggers identify the optimization that produced the stored continuity
state: `manual`, `omission`, `compression`, or `omission | compression` when one
optimization pass first omitted old tool results and then compressed history.
One pre-call optimization pass writes at most one context snapshot. If omission
and compression both succeed in the same pass, runtime writes only the final
post-compression snapshot with trigger `omission | compression`; it does not
write an intermediate omission-only snapshot.
Omission-only snapshots store an empty string as `summary`; retained messages
and omission markers carry the prepared continuity context.

Automatic compression runs before adapter model invocations, including
follow-up calls inside tool loops. The non-evictable raw suffix is not included
in the compression call. The query control plane derives a `model_call_group`
view from durable conversation metadata. A group is eligible for eviction only
when it is closed, has been consumed by at least one later ordinary task model
call, is outside the live or unconsumed suffix, and is outside the configured
raw retention suffix.

Runtime selects eligible groups from oldest to newest until adding the next
group would exceed `compression_evicted_history_budget`. It must not skip older
eligible groups. If the oldest eligible group cannot fit, compression fails
before the model call with `compression_failed`. Phase 1 performs at most one
compression model call in a single pre-call optimization pass.

If compression fails because the oldest eligible group cannot fit, the UI error
message tells the user to start a new session to continue with a fresh context
window. Phase 1 does not add recovery commands, forced history deletion,
map-reduce compression, or repeated compression calls for this condition.

The compression model call is runtime-owned and tool-less. It does not expose
model-visible tools, does not enter the ordinary tool loop, and does not append
an assistant answer to durable conversation.

Compression uses a separate compression frame instead of the ordinary task
`ModelContextFrame`. The compression frame contains, in order, previous
continuity summary if present, bounded evicted history messages, and the
compression instruction/schema prompt. It excludes the main agent system prompt,
available skill headers, model-visible tool schema bindings, active `SKILL.md`
bodies, retained recent raw messages, live or unconsumed suffix messages, and
runtime-owned active skill records, artifact refs, policy facts, or approval
facts.

Manual `/compress` uses the same rolling summary compression machinery while
idle, but it skips old-tool-result omission and directly constructs a
`CompressionContextFrame` from previous summary, selected eligible evictable
history, and the compression instruction/schema prompt. It replaces the previous
summary and selected evicted groups in `ReplRuntime.conversation` and rebuilds
the current or next `ModelContextFrame` when compression actually runs.

Every actual optimization displays a REPL system message with reduced-from and
reduced-to context estimates.

If omission/compression still leaves the rebuilt `ModelContextFrame` over the
hard context limit (`window_tokens`), runtime must not call the model adapter.
The failure is scoped to the current turn: runtime writes a
`context_limit_exceeded` run event and a `context` checkpoint fact with
`error_class="context_limit_exceeded"`, displays an English UI message, and
returns the REPL to prompt input without terminalizing the session or long-lived
prompt run. In a long-lived REPL prompt run, `sessions.status` and `runs.status`
remain `running`.

For one-shot prompt runs, the same condition is terminal because there is no
later REPL query boundary. Runtime writes the same run event and `context`
checkpoint fact, then marks the one-shot run and session as `failed` with
`error_class="context_limit_exceeded"` and returns a non-zero CLI exit code.

## Alternatives Considered

### Only provide manual `/compress`

Manual compression gives users control, but long tool-heavy sessions can hit
context pressure before the user notices.

### Summarize everything into one model-generated state

This is compact, but it lets natural language become the recovery source and
risks losing structured runtime facts.

### Include live or unconsumed messages in the compression call

This can reduce implementation steps, but it lets the compression model
reinterpret or answer the latest user request, pending tool call, or fresh tool
result before an ordinary task model call has consumed it.

## Consequences

- Context pressure is handled before it becomes a model-call failure.
- Summaries are optimized for long-runtime continuity, not only brevity.
- Runtime state remains authoritative and structured.
- UI users can see when and how much context was optimized.
- Irreducible context pressure fails the current turn explicitly instead of
  attempting a model call that is expected to exceed the provider context limit.
- Tests must cover artifacting, omission, automatic compression, manual
  `/compress`, conversation replacement, and status bar updates.

## Phase 3 Refinement

[ADR 0014](0014-terminal-recovery-checkpoints-durable-conversation.md) refines
the Phase 3 role of context snapshots and compressed conversation state.

For Phase 3 prompt sessions/runs, runtime stops writing `context_snapshots` as
provenance. A context summary that must be model-visible after resume must be
persisted as a durable `conversation_messages` row and referenced by a terminal
recovery checkpoint's conversation cut. Runtime must not recover model context
from natural-language compression summaries, trace text, UI state, or legacy
context snapshots.

[ADR 0015](0015-normalized-error-taxonomy-narrow-runtime-retry.md) refines
compression and context-limit failure taxonomy. Phase 3 uses normalized
model-related error classes and fixed reasons for compression and context
failures while preserving the turn/session terminalization behavior specified by
the active phase spec.

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
   markers while retaining recent turns and artifact references.
3. when `ModelContextFrame` exceeds `compress_history_at_ratio`, run
   conversation compression.

Conversation compression is a continuity-preserving operation. The compression
prompt must preserve:

- current task goal.
- completed key tasks or milestones.
- files inspected or modified.
- remaining work and next plan.
- key decisions and constraints.
- visible references to artifacts, active skills, loaded skill reference files,
  approvals, and policies when they appear in history.

Runtime, not the model summary, preserves authoritative state:

- active skill refs.
- frozen skill and reference snapshots.
- artifact ids.
- approval records.
- path policy and shell policy.
- context snapshot ids.

`context_snapshots` store the post-optimization `ReplRuntime.conversation`
continuity state produced by omission and/or compression. They do not store the
pre-compression full context and do not store the final composed
`ModelContextFrame`.

Raw pre-compression facts remain in `run_events` and artifacts. The final
`ModelContextFrame` is reconstructed by `PromptComposer` from stable system
content, the current `ReplRuntime.conversation`, runtime-supplied active skill
context, and the current user input or tool-loop messages. The snapshot shape
must be sufficient for future recovery, but Phase 1 writes context snapshots
only for trace, audit, continuity inspection, and future recovery support. Phase
1 does not implement restart or resume recovery from context snapshots.

For automatic omission and compression, the current-turn protected suffix is
excluded from the compression model call and context snapshot. The protected
suffix includes the current user input, current-turn assistant tool-call
messages, fresh tool results, and follow-up tool-loop messages. The snapshot
records prepared durable conversation context up to the previous safe boundary;
the protected suffix is appended only to the real model call.

Snapshot triggers identify the optimization that produced the stored continuity
state: `manual`, `omission`, `compression`, or `omission | compression` when one
optimization pass first omitted old tool results and then compressed history.
Omission-only snapshots store an empty string as `summary`; retained messages
and omission markers carry the prepared continuity context.

Automatic compression runs before adapter model invocations, including
follow-up calls inside tool loops. The current-turn protected suffix is not
included in the compression call. Runtime compresses history before the previous
safe boundary, replaces the compressible portion of `ReplRuntime.conversation`,
rebuilds `ModelContextFrame`, then appends the protected suffix unchanged for
the real model call.

The compression model call is runtime-owned and tool-less. It does not expose
model-visible tools, does not enter the ordinary tool loop, and does not append
an assistant answer to durable conversation.

Compression uses a separate compression frame instead of the ordinary task
`ModelContextFrame`. The compression frame contains the compression instruction,
compressible durable history, structured active skill refs, visible artifact
refs, and visible policy or approval facts. It excludes the main agent system
prompt, available skill headers, model-visible tool schemas, active
`SKILL.md` bodies, and the current-turn protected suffix.

Manual `/compress` uses the same compression path while idle. It also replaces
the compressible portion of `ReplRuntime.conversation` and rebuilds the current
or next `ModelContextFrame`.

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

### Include current-turn messages in the compression call

This can reduce implementation steps, but it lets the compression model
reinterpret or answer the latest user request, current tool call, or fresh tool
result before the real task call.

## Consequences

- Context pressure is handled before it becomes a model-call failure.
- Summaries are optimized for long-runtime continuity, not only brevity.
- Runtime state remains authoritative and structured.
- UI users can see when and how much context was optimized.
- Irreducible context pressure fails the current turn explicitly instead of
  attempting a model call that is expected to exceed the provider context limit.
- Tests must cover artifacting, omission, automatic compression, manual
  `/compress`, conversation replacement, and status bar updates.

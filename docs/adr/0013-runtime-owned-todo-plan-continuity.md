# ADR 0013: Runtime-Owned Todo Plan Continuity

## Status

Accepted for Phase 2.

## Context

Long-running prompt-agent debugging sessions drift when the current plan exists
only in natural-language conversation. Context omission and compression can
remove, summarize, or reinterpret older plan facts. A model-generated summary is
useful continuity memory, but it is not authoritative runtime state.

Phase 2 needs a narrow runtime-owned continuity mechanism before RenderDoc
readiness work. The mechanism must help the main prompt agent maintain a current
plan across ordinary turns and context compression, without turning runtime core
into a workflow engine, task database, business state machine, or RenderDoc /
shader-specific controller.

ADR 0010 already defines `ModelContextFrame` as the ordinary task model-call
visibility boundary. ADR 0011 already defines compression summaries as
LLM-visible continuity, not recovery truth. Todo Plan builds on those decisions
by adding a structured, run-scoped plan truth that runtime owns and injects into
ordinary task frames.

## Decision

Add Todo Plan as runtime-owned authoritative continuity state.

Todo Plan is bound to `run_id`. Phase 2 only has main prompt runs, but the model
must not assume a singleton plan per session because later foreground subagent
runs may need independent plans.

Runtime exposes one brokered model-visible tool, `todo`, for whole-plan
replacement. The model provides the complete current plan on every mutation.
Runtime validates the full plan, assigns deterministic display indexes,
increments a run-local plan version, persists the new current plan, and writes a
`todo_updated` event in the same SQLite transaction.

`todo` is a narrow runtime-owned approval-policy exception. Although it is a
`runtime_control` tool, valid `todo` calls are audit-only in every approval mode,
including `normal`. They do not request interactive approval, do not create
approval grants, and do not emit interactive approval events. This exception is
limited to `todo` because it performs no filesystem access, shell execution,
network call, model/provider call, runtime permission change, or execution
authorization. Schema validation failures, semantic validation failures, config
errors, and any runtime-control policy denial remain final and cannot be
overridden by approval mode.

Todo Plan is execution continuity, not execution authorization. It does not
grant filesystem, shell, provider, runtime-control, subagent, or workflow
permission. ToolBroker, path policy, shell policy, approval mode, timeout,
artifact rules, and audit remain the only execution boundaries.

Before every ordinary task model call, runtime injects the current Todo Plan
into `ModelContextFrame` as a non-persistent runtime segment with
`kind="runtime_todo_plan"`. The injected segment is counted by token estimation
because it is sent to the ordinary task model call.

Todo Plan injection is intended to guide the model's next actions. The
runtime-authored Todo Plan wrapper and update instruction are authoritative
runtime context. Individual plan item `content` and `activeForm` values are
structured plan data supplied through the `todo` tool; they are not independent
system instructions. Provider materialization must preserve that distinction by
rendering plan items as delimited data, so item text cannot override runtime
safety policy, active skill instructions, user instructions, or ToolBroker
decisions.

Todo Plan is not appended to durable conversation history, is not compression
input, and is not restored from compression summaries. Manual `/compress`,
automatic omission, and automatic compression must leave `TodoPlanStore`
unchanged. After compression, the next ordinary `ModelContextFrame` must inject
the current plan from `TodoPlanStore`, not from summary text.

Phase 2 does not define a Todo Plan status transition state machine. Any rewrite
from one valid plan to another valid plan is allowed. Phase 2 also does not
define Todo Plan resume semantics; later phases that copy or transform Todo
Plan across terminalized runs must define that behavior explicitly.

## Alternatives Considered

### Rely On Conversation History

The model could keep a natural-language checklist in ordinary conversation
messages.

This is insufficient because omission and compression can remove or rewrite the
only plan copy. It also makes runtime continuity depend on the model remembering
to preserve plan facts in prose.

### Rely On Compression Summary

The compression prompt could be instructed to preserve the current plan in the
rolling summary.

This preserves useful working memory, but it still makes the summary the
authoritative plan source. ADR 0011 explicitly keeps summaries as LLM-visible
continuity, not runtime truth. Restoring plan state from summary text would
reintroduce drift and parsing ambiguity.

### Implement Workflow Or Task Graph Runtime

Runtime could model the plan as executable workflow steps, task graph nodes,
state transitions, retries, and step-level recovery.

This is too broad for Phase 2 and risks pulling business process semantics into
runtime core. Workflow remains a deferred architecture module. Todo Plan is a
continuity aid for prompt execution, not an execution engine.

### Store Todo Plan In UI State

The TUI could display and preserve the current plan.

UI state is observational and non-authoritative. It cannot be the source of
recovery truth, and it would not help one-shot, plain REPL, non-TTY, trace, or
future resume paths.

### CRUD-Style Plan Tools

Runtime could expose multiple model-visible tools such as `plan_list`,
`plan_set`, `plan_update`, or item-level add/update/delete operations.

This increases the tool surface and creates partial-update semantics, stable item
id requirements, conflict behavior, and ambiguity about whether omitted items
should be preserved or deleted. It also makes it easier for the model to drift by
updating one item while forgetting the full current plan. Whole-plan replacement
keeps the contract deterministic: every successful mutation produces one
complete authoritative plan, one version increment, one atomic persistence
transaction, and one `todo_updated` event.

### Store Stable Item Ids In Phase 2

Runtime could require model-supplied or runtime-assigned stable item ids.

Stable ids are useful for future item-level mutation or cross-run plan
transforms, but Phase 2 does not need those semantics. Display indexes derived
from current order are enough for status, trace, TUI rendering, and prompt
continuity.

## Consequences

- Long-running prompt sessions get a structured current-plan truth independent
  from natural-language history.
- Todo Plan survives ordinary context omission, automatic compression, and
  manual `/compress`.
- `ModelContextFrame` remains the single ordinary task visibility boundary for
  injected runtime context and token estimation.
- Todo Plan persistence and `todo_updated` events become Phase 2 runtime truth
  and require the Phase 2 SQLite `PRAGMA user_version` bump.
- Runtime avoids introducing workflow, task graph, business state machine, or
  RenderDoc/shader-specific semantics.
- `todo` becomes the first explicit `runtime_control` approval-policy exception:
  audit-only in every approval mode because it changes only run-scoped plan
  continuity truth and has no execution-authorization effect.
- Later phases must explicitly define any Todo Plan behavior across resume,
  foreground subagent runs, or future item-level mutation.

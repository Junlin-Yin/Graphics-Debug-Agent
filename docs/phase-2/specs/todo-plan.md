# Phase 2 Todo Plan Specification

## Boundary

Todo Plan is runtime-owned structured continuity for the current run.

It is not a workflow engine, task graph, background task system, subagent
handoff protocol, project management database, or business state machine.
Runtime stores the agent's current plan so ordinary prompt execution can remain
coherent across long conversations and context compression.

Todo Plan does not authorize tools. Path policy, shell policy, approval, and
ToolBroker decisions remain authoritative for execution.

## Data Model

Todo Plan is bound to `run_id`.

Current plan shape:

```json
{
  "run_id": "run_...",
  "version": 3,
  "items": [
    {
      "index": 1,
      "content": "Inspect exported render target",
      "status": "pending",
      "activeForm": null,
      "metadata": {}
    }
  ],
  "updated_at": "2026-05-30T00:00:00Z"
}
```

Item fields:

- `index`: runtime-assigned 1-based display index derived from item order.
- `content`: non-empty human-readable task text.
- `status`: one of `pending`, `in_progress`, or `completed`.
- `activeForm`: optional present-continuous label. Runtime preserves and
  displays it only for an `in_progress` item.
- `metadata`: object reserved for runtime-owned metadata. Phase 2 stores `{}`.

Runtime assigns `version` monotonically per run whenever a mutation succeeds.
The initial version before any successful mutation is `0`; the first successful
mutation writes `plan_version = 1` and reports `previous_plan_version = 0`.
Runtime owns `updated_at`.

The model does not supply stable item ids in Phase 2. The model rewrites the
whole current plan with the `todo` tool. Runtime preserves item ordering from
the tool input and assigns display indexes from that order.

A Todo Plan may contain zero to twenty items. `items=[]` is a valid rewrite that
clears the current plan. At most one item may be `in_progress`.

Phase 2 does not define a Todo Plan status transition state machine. Any rewrite
from one valid plan to another valid plan is allowed. Execution authorization
remains controlled only by ToolBroker and runtime policy, not by Todo Plan
status.

## Persistence

Phase 2 persists current Todo Plan state in SQLite separately from durable
conversation history and context snapshots.

Minimum persistence facts:

- `session_id`
- `run_id`
- `plan_version`
- ordered item list
- item indexes, content, status, activeForm, and metadata
- `created_at`
- `updated_at`

Implementations may store items as normalized rows or as a JSON payload as long
as `todo`, prompt injection, `status`, and trace behavior satisfy this contract.

Todo Plan is not restored from compression summary. It is read from
`TodoPlanStore`.

## Tool Definition

Phase 2 exposes one model-visible Todo Plan tool through `ToolBroker`:

- `todo`

Minimum metadata:

```json
{
  "name": "todo",
  "category": "runtime_control",
  "risk_level": "runtime_control",
  "access": []
}
```

`todo` is a runtime-control tool. It does not perform filesystem access, shell
execution, network calls, provider calls, or execution authorization.

`todo` is a runtime-owned tool-policy exception. Like valid
`load_skill_resource` calls for active frozen resources, valid `todo` calls are
audit-only in every approval mode:

- they do not request interactive approval in `normal`, `semi-auto`, or `yolo`.
- they do not write `approval_grants` rows.
- they do not emit `approval_requested` or `approval_decision_recorded` events.
- they still write normal ToolBroker audit facts and a dedicated mutation event
  for successful rewrites.

This is a Phase 2 narrow exception to the generic ADR 0012 `runtime_control`
approval matrix. The exception is limited to the `todo` tool because it performs
no filesystem access, shell execution, network call, provider call, or execution
authorization. Other `runtime_control` tools continue to follow ADR 0012 unless
a phase document explicitly defines an equally narrow exception.

Schema validation failures and plan semantic validation failures return
`ToolResult.status = "denied"` with `error_class = "user_error"` and a
`tool_call_denied` event, matching Phase 1 ToolBroker error boundaries. They
cannot be overridden by approval mode. Runtime-control policy denials, if any,
remain `policy_denied`.

### `todo`

Rewrites the current run's whole Todo Plan. The model must provide the full
current list each time.

Input schema:

```json
{
  "type": "object",
  "properties": {
    "items": {
      "type": "array",
      "minItems": 0,
      "maxItems": 20,
      "items": {
        "type": "object",
        "properties": {
          "content": {
            "type": "string"
          },
          "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "completed"]
          },
          "activeForm": {
            "type": "string",
            "description": "Optional present-continuous label."
          }
        },
        "required": ["content", "status"],
        "additionalProperties": false
      }
    }
  },
  "required": ["items"],
  "additionalProperties": false
}
```

Todo Plan validation is two-stage. `ToolBroker` performs structural schema
validation: object shape, required fields, field types, unknown fields, item
count, and `status` enum. `TodoToolHandler` then trims `content` and
`activeForm` and performs semantic validation. After trimming, empty
`content`, `content` longer than 240 characters, empty `activeForm`, and
`activeForm` longer than 120 characters are invalid.

At most one item may be `in_progress`. If an item whose status is not
`in_progress` includes `activeForm`, the input is still valid, but runtime
normalizes that item's `activeForm` away before persistence, `ToolResult`
output, events, trace, prompt injection, and TUI display.

Successful output:

```json
{
  "plan_version": 4,
  "item_count": 4,
  "counts": {
    "pending": 1,
    "in_progress": 1,
    "completed": 2
  },
  "items": [
    {
      "index": 1,
      "content": "Review Phase 2 docs",
      "status": "completed"
    },
    {
      "index": 2,
      "content": "Implementation-plan.md ready",
      "status": "completed"
    },
    {
      "index": 3,
      "content": "Patching Todo Plan spec",
      "status": "in_progress",
      "activeForm": "Patching Todo Plan spec"
    },
    {
      "index": 4,
      "content": "Update tests",
      "status": "pending"
    }
  ]
}
```

`ToolResult.metadata` must include:

```json
{
  "tool_name": "todo",
  "previous_plan_version": 3,
  "plan_version": 4,
  "mutation": "replace",
  "item_count": 4,
  "counts": {
    "pending": 1,
    "in_progress": 1,
    "completed": 2
  }
}
```

`ToolResult.output` must keep the complete structured Todo Plan result. For
`todo`, the provider-visible tool-loop message must be derived from
`ToolResult.output`, not from `ToolResult.redacted_output`, so the model can
observe the complete current plan after the tool call.

`ToolResult.redacted_output` must be either `null` or a compact text rendering
that is safe for TUI display. When there is at most one completed item and at
most four pending items, the compact rendering shows every item:

```text
Todo Plan v4: 1 pending, 1 in_progress, 2 completed
[o] 1. Review Phase 2 docs
[o] 2. Implementation-plan.md ready
[>] 3. Patching Todo Plan spec
[ ] 4. Update tests
```

When a successful rewrite produces an empty Todo Plan, `ToolResult.output` still
contains the normal structured fields with `item_count = 0`, zero status counts,
and `items = []`. Its compact rendering must explicitly show the empty state:

```text
Todo Plan v4: empty
```

TUI presentation may use the structured output directly, but it must preserve
the same status markers:

- `[o]` for `completed`.
- `[>]` for `in_progress`.
- `[ ]` for `pending`.

When the completed item count is greater than one, compact rendering collapses
all completed items into one line using the actual display indexes:

```text
[o] (steps 1-3, 6 done)
```

When the pending item count is greater than four, compact rendering shows the
first three pending items individually and collapses the fourth and later
pending items into one line using the actual display indexes:

```text
[ ] (steps 8-9, 12 pending)
```

`in_progress` items remain individually rendered. Because Phase 2 allows whole
plan rewrites without a status transition state machine, compact rendering must
not assume items of the same status are contiguous. Range text may use `x-y`
only for contiguous display indexes; non-contiguous indexes must be listed
explicitly.

## Events

Successful `todo` calls write a dedicated runtime event.

`todo_updated` event payload:

```json
{
  "previous_plan_version": 3,
  "plan_version": 4,
  "item_count": 4,
  "counts": {
    "pending": 1,
    "in_progress": 1,
    "completed": 2
  },
  "items": [
    {"index": 1, "content": "Review Phase 2 docs", "status": "completed"},
    {"index": 2, "content": "Implementation-plan.md ready", "status": "completed"},
    {
      "index": 3,
      "content": "Patching Todo Plan spec",
      "status": "in_progress",
      "activeForm": "Patching Todo Plan spec"
    },
    {"index": 4, "content": "Update tests", "status": "pending"}
  ]
}
```

Events must not be the only source of current plan state during normal
execution. Runtime reads current state from `TodoPlanStore`.

The TodoPlanStore replacement and the `todo_updated` event for the same
successful `todo` call must commit in the same SQLite transaction. If either
write fails, the whole mutation fails, the current plan remains unchanged, and
no `todo_updated` event is committed.

Failed `todo` calls write normal ToolBroker audit facts but do not write
`todo_updated`.

## Checkpoint And Continuity

Todo Plan is continuity truth for the current run.

Whenever a `todo` mutation succeeds, runtime must persist the new plan before
the next ordinary model call can observe it. Runtime may also include current
plan metadata in existing checkpoint payloads, but current plan truth must
remain queryable from `TodoPlanStore`.

`/compress` must not modify Todo Plan. Automatic omission and compression must
not modify Todo Plan.

The next `ModelContextFrame` after compression must inject the current plan from
`TodoPlanStore`, not from summary text.

Phase 2 does not implement `resume`. Phase 3 defines any copying of Todo Plan
from terminalized runs into new runs.

## Prompt Injection

Before each ordinary task model call, `PromptComposer` injects the current Todo
Plan as a non-persistent `ModelContextFrame` segment:

```json
{
  "role": "system",
  "kind": "runtime_todo_plan",
  "content": {
    "plan_version": 4,
    "items": [
      {"index": 1, "status": "completed", "content": "Review Phase 2 docs"},
      {"index": 2, "status": "completed", "content": "Implementation-plan.md ready"},
      {
        "index": 3,
        "status": "in_progress",
        "content": "Patching Todo Plan spec",
        "activeForm": "Patching Todo Plan spec"
      },
      {"index": 4, "status": "pending", "content": "Update tests"}
    ],
    "summary": "Todo Plan has 1 pending, 1 in_progress, and 2 completed items.",
    "instruction": "Use the todo tool to rewrite this plan whenever task status changes or the plan no longer matches the work."
  }
}
```

Runtime always injects this segment, including when the current Todo Plan is
empty. If no successful `todo` mutation has happened for the run, the injected
segment uses `plan_version = 0`, `items = []`, and the summary
`Current Todo Plan is empty.` If the plan was cleared by a successful
`todo(items=[])` rewrite, the injected segment uses that persisted
`plan_version`, `items = []`, and the same empty summary.

The Todo Plan segment is intentionally model-guiding runtime context: it tells
the model what work is pending, in progress, and completed. The runtime-owned
segment instruction is authoritative. Individual item `content` and
`activeForm` values are plan data supplied through the `todo` tool, not
independent system instructions. Adapter materialization must preserve that
distinction by rendering plan items as delimited structured data, not as
free-form instructions. Text inside plan items cannot override runtime safety,
tool policy, active skills, user instructions, or the segment's own instruction
to keep the plan current.

The Todo Plan segment:

- is not appended to durable conversation history.
- is not compression input.
- is counted by `TokenEstimator`.
- appears after active `SKILL.md` context and before rolling summary, retained
  raw conversation, live/unconsumed messages, tool-loop messages, and current
  user input.
- is materialized by the adapter according to its `role`, using provider-legal
  system context when the provider supports system messages or an equivalent
  provider-specific system instruction channel.
- includes an explicit empty summary when `items = []`, so the model can tell
  the runtime injected an authoritative empty Todo Plan rather than omitting the
  plan state.

Runtime does not inject a separate fixed-interval reminder based on model-call
counts in Phase 2. The stable `runtime_todo_plan` segment itself carries the
plan-update instruction on every ordinary task model call.

## Error Handling

Invalid schema, too many items, empty item content, invalid status, invalid
`activeForm`, or multiple `in_progress` items:

- `ToolResult.status = "denied"`
- `error.error_class = "user_error"`
- a `tool_call_denied` event

Persistence failure:

- `ToolResult.status = "error"`
- `error.error_class = "internal_error"`

Policy denial:

- `ToolResult.status = "denied"`
- `error.error_class = "policy_denied"`

A failed `todo` mutation must be atomic: no partial state update and no
`todo_updated` event.

## Status And Trace

`debug-agent status <session_id>` must include a compact current Todo Plan
summary when a plan exists:

```json
{
  "todo_plan": {
    "plan_version": 4,
    "counts": {
      "pending": 1,
      "in_progress": 1,
      "completed": 2
    }
  }
}
```

Trace output must show `todo` calls, `todo_updated` events, and current plan
summaries in chronological context.

Trace output is observational and must not be used as a recovery source.

# Phase 2 Architecture

## Module List

### ToolBroker

`ToolBroker` remains the only execution boundary for model-visible tools.

Phase 2 extends the broker-visible tool set with:

- `view_image`, only when enabled by frozen multimodal tool availability
- `todo`

The broker continues to own tool schema validation, normalized execution facts,
permission evaluation, approval, timeout, routing, artifact handling, result
normalization, and audit emission.

Tool handlers must not bypass path policy, approval policy, timeout, artifact
rules, or audit. Tool handlers return raw handler results to the broker; the
broker normalizes them into `ToolResult`.

### ViewImageTool

`ViewImageTool` is a native tool handler routed by `ToolBroker`.

Responsibilities:

- validate one to four normalized local image paths per call.
- verify each image is PNG or JPEG from image bytes, not extension alone, using
  Pillow for image metadata parsing.
- read bytes only after `ToolBroker` has allowed every path.
- reject remote URLs, artifact ids, directories, symlink escapes, missing files,
  unsupported image types, and corrupt images.
- compute image metadata for every image before the multimodal call.
- enforce Phase 2 image dimension limits and projected provider request body
  size limits before the multimodal call.
- call `VisionModelClient` with temporary in-memory image bytes.
- convert provider output into the Phase 2 structured observation object.
- return error facts without leaking image bytes or base64.

`ViewImageTool` does not copy local path inputs into `ArtifactStore` as a
contract requirement. Implementations may create internal temporary files only
if they are not exposed as user artifacts and are removed after the call.

### VisionModelClient

`VisionModelClient` owns the OpenAI-compatible multimodal provider call used by
`view_image`.

It is separate from the main prompt agent provider path. The main
`AgentLoopAdapter` does not own vision configuration, image encoding policy, or
vision response normalization.

Responsibilities:

- load frozen multimodal config from the session config snapshot.
- expose a frozen enabled/disabled availability decision for `view_image` based
  on startup config and required environment-variable presence.
- use configured provider, model, base URL env var, API key env var, timeout,
  max completion token limit, query character limit, and analysis character
  limit.
- build an OpenAI-compatible Chat Completions request for `kimi-k2.5` with one
  user message whose content parts contain image `data:` URLs followed by the
  runtime-owned text instruction.
- send transient image data URLs and the effective query only to the configured
  multimodal provider call.
- use non-streaming Chat Completions; internal vision provider deltas are not
  streamed to REPL/TUI and do not produce ordinary model stream events.
- perform at most one provider request per `view_image` tool call and disable
  SDK/client implicit retry for this provider call path.
- use the effective timeout supplied by `ToolBroker`; the same timeout is passed
  to the underlying Chat Completions call.
- request JSON-object response format through the OpenAI-compatible provider
  path.
- return raw provider text plus provider metadata to `ViewImageTool`.
- classify provider outcomes as `model_error`, `timeout`, or `config_error`
  without changing the Phase 2 `ToolResult.status` contract for each class.

Phase 2 supports only the OpenAI-compatible multimodal path. It does not add a
provider abstraction that covers the main agent provider, Anthropic vision, or
fallback vision models. Real multimodal execution is limited to `kimi-k2.5` in
Phase 2.

If multimodal configuration is missing, invalid, unsupported, or missing required
environment variables at session startup, runtime does not fail startup solely
for that reason. It freezes `view_image` as disabled for the session, records a
no-secret disabled reason in `sessions.config_snapshot_json`, omits `view_image`
from `ModelContextFrame.tool_schema_bindings`, and keeps other session features
available. Because v1/v2 do not support config or model hot reload, later config
or environment changes require a new session to change `view_image` availability.

Disabled `view_image` remains a known broker-side tool availability state, not
an unknown tool name. If a stale or direct valid `view_image` call reaches
`ToolBroker` while disabled, the broker returns a denied `config_error` without
routing to `ViewImageTool` or `VisionModelClient`. Unknown tool names keep the
existing Phase 1 unknown-tool denial behavior.

### TodoPlanStore

`TodoPlanStore` persists the current Todo Plan for each run.

Responsibilities:

- replace a run's current plan for `todo`.
- return the current plan for prompt injection, `status`, and trace rendering.
- maintain deterministic item ordering.
- persist enough update metadata to audit current state and reconstruct changes
  from events.

Todo Plan is bound to `run_id`. Phase 2 has only main prompt runs, but the data
model must not assume a singleton plan per session because later subagent runs
will need independent plans.

### TodoToolHandler

`todo` is a runtime-owned runtime-control handler routed by `ToolBroker`.

Responsibilities:

- validate model-visible input schemas.
- enforce Todo Plan semantic limits, including at most twenty items and at most
  one `in_progress` item.
- write Todo Plan state through `TodoPlanStore`.
- emit dedicated plan events.
- return compact `ToolResult` objects suitable for ordinary conversation.

`todo` does not read or write files and does not call external providers.

`todo` is an audit-only runtime-owned exception to the generic
`runtime_control` approval matrix, similar to valid `load_skill_resource` calls
for active frozen resources. It does not request interactive approval in any
approval mode and does not create reusable approval grants. This exception is
limited to `todo` because it has no filesystem, shell, network, provider, or
execution-authorization effect. It still passes through `ToolBroker` schema
validation, runtime-control target validation, timeout, result normalization,
and audit emission.

### PromptComposer

`PromptComposer` extends ordinary task `ModelContextFrame` construction.

Before every ordinary task model call, it injects the current run's Todo Plan as
a non-persistent runtime segment:

```text
role="system"
kind="runtime_todo_plan"
```

The Todo Plan segment is not appended to durable `ReplRuntime.conversation`.
It is not compression input. It is counted by `TokenEstimator` because it is
sent to the provider on ordinary task model calls.

The Todo Plan segment appears after stable system content and active skill
context, and before rolling summary, retained raw history, live/unconsumed
messages, tool-loop messages, and current user input. The segment carries a
short runtime-owned instruction telling the model to call `todo` whenever task
status changes or the plan no longer matches the work.

The segment is model-guiding context by design, but only the runtime-authored
segment wrapper and instruction are authoritative instructions. Plan item text
is structured plan data. Provider materialization must render item `content` and
`activeForm` as delimited data so they guide task continuity without becoming
independent system instructions that can override runtime policy, active skills,
or user instructions.

PromptComposer always injects the segment. When the current Todo Plan is empty,
the segment contains `items = []` plus an explicit empty summary so the model can
distinguish authoritative empty state from missing state.

### ContextManager

`ContextManager` continues to manage compressible LLM-visible conversation
history.

Todo Plan is outside omission and compression. Compression summaries may mention
plan facts only if those facts appeared in evicted conversation messages, but
runtime must not restore or validate Todo Plan from summaries.

Manual `/compress` and automatic compression must leave `TodoPlanStore`
unchanged. After compression, the next `ModelContextFrame` is rebuilt with the
same authoritative current Todo Plan.

`view_image` tool observations are ordinary durable tool observations after
normalization. They may be omitted or compressed like other ordinary tool
results. Raw image bytes and base64 are never part of those observations.

### Persistence Services

Phase 2 extends SQLite persistence with Todo Plan and schema-version changes.

Persistence services own:

- Phase 2 `PRAGMA user_version` initialization and validation.
- current Todo Plan state.
- plan change events.
- ToolBroker audit events and metadata for `view_image`.

Runtime-owned `.sessions/` writes do not go through model-visible path policy,
but model-visible tools cannot access `.sessions/` paths.

### TraceWriter And Status Queries

Trace rendering must include:

- `todo` tool calls and resulting plan state summaries.
- `view_image` calls with source paths, image metadata, provider/model, latency,
  effective query source, status, error class if any, and analysis summary.
  Runtime-authored persisted audit metadata, trace output, engine log entries,
  context snapshot metadata, and ToolResult metadata record only whether the
  effective query source was `default` or `assistant`, not the concrete query
  text, raw query argument, query preview, or query length. Assistant-authored
  raw tool-call arguments and the immediate tool-loop transcript may contain
  `query` because they are the model's tool invocation, not runtime-authored
  audit facts.

Trace rendering must not include:

- image base64.
- raw image bytes.
- provider request image content parts.

Status queries should include a compact current Todo Plan summary for active
or terminal runs when a plan exists. Status output is observational only and is
not a recovery source.

### REPL And TUI

Phase 2 does not add new slash commands.

The existing `/tools` output must include `todo` and must include `view_image`
only when it is enabled by the frozen multimodal config. When `view_image` is
disabled, `/tools` or status output should expose a concise disabled reason for
human diagnosis without exposing secrets. The existing `/compress` command must
preserve Todo Plan visibility.

TUI may display a compact Todo Plan summary, but UI state is not authoritative
and must not be used to reconstruct plan truth. The default compact rendering is:

```text
Todo Plan v4: 1 pending, 1 in_progress, 2 completed
[o] 1. Review Phase 2 docs
[o] 2. Implementation-plan.md ready
[>] 3. Patching Todo Plan spec
[ ] 4. Update tests
```

## Data Flow

### `view_image`

1. Model calls `view_image`.
2. `ToolBroker` validates schema and normalizes path facts.
3. `PermissionEvaluator` applies path policy, approval mode, grants, and hard
   denies for every path. The reusable approval scope signature is based on the
   tool name, read access, and the ordered canonical image path list; it excludes
   `query`, image metadata, hashes, and provider configuration. Provider egress is
   governed by frozen multimodal tool availability, not by a separate approval
   scope.
4. `ToolBroker` computes the effective `view_image` timeout from frozen
   multimodal config and routes the allowed call to `ViewImageTool` through the
   normal timeout envelope.
5. `ViewImageTool` reads authorized image bytes for every path.
6. `ViewImageTool` computes metadata, enforces image/request limits, resolves the
   effective query, and calls `VisionModelClient`.
7. `VisionModelClient` builds image data URLs in memory, calls Chat
   Completions once with the same effective timeout and JSON-object response
   format, then returns raw provider text and provider metadata.
8. `ViewImageTool` parses the provider text as JSON, validates the required
   semantic fields and frozen analysis length limit, ignores provider-returned
   source metadata, and normalizes semantic observations with runtime-computed
   image metadata.
9. `ToolBroker` writes audit events, artifacts large textual output when
   needed, normalizes `ToolResult`, and appends only the structured result to
   ordinary conversation.

### Todo Plan

1. Model calls `todo`.
2. `ToolBroker` validates schema and applies the audit-only runtime-owned Plan
   tool policy.
3. `TodoToolHandler` validates plan semantics.
4. `TodoPlanStore` persists the new current plan and runtime writes the
   `todo_updated` event in the same SQLite transaction.
5. `ToolBroker` returns a compact `ToolResult`.
6. Before the next ordinary model call, `PromptComposer` reads current Todo Plan
   and injects it into `ModelContextFrame`.

## Dependency Direction

```text
CLI / REPL
-> RuntimeOrchestrator
-> PromptAgentExecutor
-> QueryControlPlane
-> PromptComposer
-> TodoPlanStore

PromptAgentExecutor
-> ToolBroker
-> ViewImageTool / TodoToolHandler
-> VisionModelClient / TodoPlanStore / ArtifactStore / EventWriter

TraceWriter / Status Queries
-> RunStore / EventStore / TodoPlanStore / ArtifactStore
```

`VisionModelClient` does not depend on `AgentLoopAdapter`.
`TodoPlanStore` does not depend on `ContextManager`.
`ContextManager` does not own Todo Plan truth.

## Failure Boundaries

- Broker pre-route and schema failures follow Phase 1 ToolBroker semantics:
  `ToolResult(status="denied")`, `error_class="user_error"`, and a
  `tool_call_denied` event.
- Policy denial returns `ToolResult(status="denied")` with
  `error_class="policy_denied"`.
- Missing or invalid multimodal config at startup disables `view_image` for the
  session and omits it from model-visible tool bindings; it does not fail session
  initialization by itself.
- A valid stale or direct `view_image` call while disabled returns
  `ToolResult(status="denied")` with `error_class="config_error"` and a
  `tool_call_denied` event.
- An unknown tool name keeps the existing Phase 1 unknown-tool denial behavior
  and is not treated as disabled `view_image`.
- If frozen multimodal config was valid at startup but a required execution-time
  environment variable is missing when `view_image` runs, the tool returns
  `ToolResult(status="error")` with `error_class="config_error"`.
- Unsupported image type, missing image, corrupt PNG/JPEG, remote URL, or
  artifact id input returns `ToolResult(status="error")` with
  `error_class="tool_error"` unless path policy denied access first.
- Vision provider HTTP/SDK failures and invalid provider responses return
  `ToolResult(status="error")` with `error_class="model_error"`.
- `view_image` provider timeout returns `ToolResult(status="timeout")` with
  `error_class="timeout"` through the same ToolBroker timeout result and audit
  path as other brokered tools.
- Plan semantic validation failures return `ToolResult(status="denied")` with
  `error_class="user_error"` and a `tool_call_denied` event.

Tool failures do not terminalize a long-lived prompt run. One-shot behavior
follows existing one-shot tool-loop semantics.

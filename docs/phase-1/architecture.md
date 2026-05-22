# Phase 1 Architecture

## Module List

### SkillRegistry

Discovers prompt skills, parses `SKILL.md` front matter, snapshots full skill
content, snapshots file-level references under `references/**`, artifacts large
reference payloads, computes content hashes, and exposes only Phase 1-supported
prompt skills.

Phase 1 does not build Markdown section trees or implement section-level
progressive disclosure.

Workflow skills may be parsed into an unsupported/deferred result for future
compatibility, but they are not available to `activate_skill`.

### Main Agent Config Loader

Loads `~/.debug-agent/agent.toml` for the main prompt agent.

Phase 1 uses this file for main-agent policy declarations, especially path
policy and shell policy. It does not implement `AgentRegistry`, named agent
discovery, or subagent-specific `agent.toml` loading.

Agent config declares policy. Runtime and `ToolBroker` enforce policy.

### Prompt Composer

Builds model-call frames under the Phase 1 query control plane.

The composer owns `ModelContextFrame` construction, active `SKILL.md` context
injection, optimized conversation inclusion, and token estimation input. Active
`SKILL.md` content is not appended to the durable conversation message list and
is not part of `/compress` input. Loaded skill reference files are ordinary tool
observations in durable conversation.

`ContextManager` prepares the compressible conversation history (omission +
compression). `PromptComposer` takes the prepared history and assembles the
final `ModelContextFrame`, including skill injection and token estimation.
`ModelContextFrame` is the runtime-owned LLM-visible context boundary for
estimation, omission, compression, status display, and tests. In Phase 1,
`PromptComposer` then materializes that frame into the existing
`AgentRunRequest.system_prompt`, `AgentRunRequest.conversation`,
`AgentRunRequest.user_input`, and `AgentRunRequest.tools` fields before calling
the adapter. The adapter does not own prompt injection policy. Tests must prove
that the materialized request is semantically equivalent to the frame used for
token estimation and context decisions.

`ReplRuntime.conversation` stores durable LLM-visible working history. Omission
and compression may mutate it, while audit truth remains in events and
artifacts. It is not the same object as the messages sent to the model.
`ModelContextFrame` is generated for each model call and is the only object used
for context window estimation.

### QueryControlPlane

Owns per-query model-call preparation and continuation control for Phase 1.

A query is one REPL user submission or one one-shot prompt from initial model
call until final answer, turn abort, timeout, cancellation, or terminal
one-shot failure. The query control plane is runtime-owned; it is not an agent
framework loop and does not own durable runtime truth.

Phase 1 query state includes:

- `query_id`.
- `session_id` and `run_id`.
- `turn_id`.
- current approval mode.
- durable conversation cursor.
- current-turn protected suffix.
- active skill refs.
- latest context estimate.
- latest optimization result.
- continuation reason.

Phase 1 continuation reasons include:

- `initial_model_call`.
- `tool_result_continuation`.
- `post_compression_continuation`.
- `approval_denied_abort`.
- `compression_failed_abort`.
- `context_limit_abort`.
- `final_assistant_response`.

The query control plane coordinates:

- query composition from durable conversation, current user input, current-turn
  tool-loop messages, active skill refs, and frozen skill headers.
- `ModelContextFrame` construction for ordinary task model calls.
- deterministic token estimation and context-window status updates.
- old tool-result omission and automatic compression checks before every adapter
  model invocation.
- `CompressionContextFrame` construction for runtime-owned compression calls.
- tool-loop continuation after brokered tool results.
- turn-scoped aborts for approval denial, compression failure, and context-limit
  failure.

`ConversationMessage` entries in durable working history are the message-level
data model used by the query control plane. Minimum fields are:

```python
class ConversationMessage:
    role: str
    kind: str
    turn_id: str
    model_call_id: str | None
    tool_call_id: str | None
    content: str | dict
    artifact_refs: list[str]
    metadata: dict
```

`turn_id`, `model_call_id`, and `tool_call_id` define safe-boundary and
protected-suffix behavior. The previous safe boundary is the end of the last
completed user turn or the start of the current user turn before the first model
call, whichever is later for the current query. Current user input, current-turn
assistant tool-call messages, fresh tool results, and follow-up tool-loop
messages form the current-turn protected suffix and are not compressed.

### ContextManager

Owns LLM-visible context shaping. It applies:

- large output artifact references.
- old tool-result omission at `omit_old_tool_results_at_ratio`.
- history compression at `compress_history_at_ratio`.
- manual `/compress` through the same compression path.

It does not own authoritative business state. It may produce context snapshots
and request persistence updates through runtime stores.

Phase 1 stores post-optimization context snapshots in SQLite by default and may
artifact large snapshot payloads when the inline payload exceeds persistence
thresholds. A context snapshot is not the pre-compression full context and is
not the final composed `ModelContextFrame`. Its shape must be sufficient for
future recovery, but Phase 1 writes it only for trace, audit, continuity
inspection, and future recovery support. Phase 1 does not implement restart or
resume recovery from context snapshots.

### ToolBroker

Extends the Phase 0 broker into the Phase 1 tool control plane.

`ToolBroker` remains the only model-visible tool execution boundary, but it
must stay thin. It coordinates normalization, permission evaluation, routing,
artifact handling, standardized results, and audit. Tool handlers do not own
permission policy and must not write audit events directly.

Phase 1 tool control plane components:

- `ToolDefinition`: model-visible schema plus runtime metadata such as
  category, risk level, and access.
- `ToolUseContext`: frozen execution context for one tool call, including
  session/run ids, workspace root, artifact root, approval mode, frozen config,
  permission rules, approval grant store, approval provider, event writer,
  artifact store, and skill snapshot store.
- `PermissionRule`: normalized rule representation produced from builtin
  policy, main-agent path policy, main-agent shell policy, and session-local
  approval grants.
- `PermissionEvaluator`: evaluates normalized tool-call facts against
  `PermissionRule` values and approval mode.
- `ToolRouter`: dispatches allowed tool calls to Phase 1 handler categories:
  `native`, `shell`, and `runtime_control`.
- tool handlers: execute only after broker permission approval and return raw
  handler results for broker normalization.

The broker execution order is:

1. validate tool name and input schema.
2. resolve runtime targets and normalize tool facts:
   - canonical paths.
   - shell argv identity, cwd, timeout, and classified argv paths.
   - runtime-control targets such as skill name and frozen content hash.
   - risk level, access, category, and approval scope signature.
3. evaluate deny rules.
4. evaluate approval mode.
5. evaluate allow, trust, and session-grant rules.
6. ask the user through `ApprovalProvider` when the decision remains `ask`.
7. route the allowed call through `ToolRouter`.
8. normalize to `ToolResult`, artifact large outputs, and write audit events.

Phase 1 extends the Phase 0 tool surface with:

- tool risk metadata.
- shell policy enforcement.
- path policy enforcement.
- approval mode enforcement.
- session-local approval grant lookup.
- approval request dispatch.
- writable native tools and `shell_exec`.

`activate_skill` and `load_skill_ref_file` are also brokered tools.

The Phase 0 model-visible `git_status` native tool is removed from the model
tool list in Phase 1. Model-initiated git operations go through `shell_exec` and
shell policy. CLI commands such as `debug-agent status` and
`debug-agent trace` are unrelated to shell policy.

### ApprovalProvider

Runtime-facing interface used by `ToolBroker` when a tool call needs user
approval.

The REPL implementation asks through the existing prompt_toolkit application or
plain input stream. Non-interactive approval requests are denied.

### ReplController And ReplView

Phase 1 reuses the Phase 0.5 controller/view architecture. Approval is modeled
as a temporary controller state inside the same input lane, not as a popup or
second command lane.

## Dependency Direction

```text
CLI
-> RuntimeOrchestrator
-> SkillRegistry / MainAgentConfigLoader
-> ReplController / ReplView

PromptAgentExecutor
-> QueryControlPlane
-> PromptComposer / ContextManager
-> AgentLoopAdapter

AgentLoopAdapter tool callable
-> ToolBroker
-> PermissionEvaluator / ApprovalProvider / ApprovalGrantStore
-> ToolRouter
-> native/shell/runtime_control handlers
```

Skill handlers, native handlers, and shell handlers must not evaluate permission
rules or write audit events directly. `ToolBroker` remains the permission and
audit boundary.

## Runtime Store Boundary

Path policy applies only to model-visible tool invocations mediated by
`ToolBroker`.

Runtime-owned persistence and artifact store operations are not model-visible
tool invocations. `SessionStore`, `RunStore`, `EventStore`, `CheckpointStore`,
`ArtifactStore`, `TraceWriter`, skill snapshot artifact staging, and context
snapshot artifacting may write under `.sessions/` through runtime service APIs
without path-policy evaluation. They remain governed by the persistence,
artifact, checkpoint, and audit contracts.

Model-visible tools must not read, list, search, write, edit, or shell into
`.sessions/`. Runtime may expose artifact ids, summaries, trace commands, and
audited metadata to the model or UI, but those references do not grant
operational filesystem access to `.sessions/`.

## Initialization Order

1. Resolve `workspace_root` using the Phase 0 rule.
2. Load global runtime config from `~/.debug-agent/config.toml`.
3. Load main agent config from `~/.debug-agent/agent.toml` if present.
4. Create the session config snapshot including:
   - provider/model runtime settings.
   - main agent config facts.
   - path policy declaration facts.
   - shell policy declaration facts.
   - context window settings.
5. Initialize `.sessions/runtime.db`:
   - if the file does not exist, create it with the Phase 1 schema and write the
     Phase 1 SQLite `PRAGMA user_version = 1`
     (`PHASE_1_SCHEMA_USER_VERSION = 1`).
   - if the file exists, read `PRAGMA user_version` before interpreting runtime
     rows.
6. Fail closed if an existing `.sessions/runtime.db` has missing (`0`), unknown,
   Phase 0, or Phase 0.5 `user_version`, using `error_class="config_error"`.
7. Check workspace active session ownership using Phase 1 schema semantics.
   Phase 0 and Phase 0.5 schema/session compatibility is rejected in Phase 1.
8. Create session, session artifact root, and prompt run.
9. Discover skills, build the frozen skill snapshot, and persist it in
   skill-registry snapshot storage associated with the session and prompt run:
   - store manifest, `SKILL.md` content, file-level reference snapshots, and
     content hashes in Phase 1 snapshot storage.
   - artifact large reference payloads and record artifact ids and content
     hashes.
   - artifact any oversized snapshot payload according to normal artifact rules.
   Then initialize structured run-scoped `active_skills` state.
10. Build startup prompt metadata, including available skill headers, from the
    persisted frozen skill registry snapshot.
11. Initialize `ToolBroker` with:
   - tool definitions.
   - permission rules from builtin policy, main-agent path policy, and
     main-agent shell policy.
   - permission evaluator.
   - tool router.
   - approval grant store.
   - approval provider.
   - skill activation handler.
   - skill reference file load handler.
12. Initialize `ContextManager`.
13. Initialize `QueryControlPlane`.
14. Initialize model adapter and prompt executor.
15. Start one-shot, plain REPL, or TUI REPL.

Skill discovery, snapshotting, persistence, and available skill header
generation are startup-blocking. One-shot execution and REPL input must not
accept a user prompt until the frozen skill registry snapshot is persisted and
ready for prompt composition.

## Prompt Composition Flow

```text
model call requested
-> QueryControlPlane starts or resumes query state
-> load run active_skill refs
-> validate active refs against frozen skill snapshot
-> identify durable conversation and current-turn protected suffix
-> compose candidate ModelContextFrame for an ordinary task model call
-> estimate candidate ModelContextFrame tokens and update context status
-> if candidate context exceeds omit threshold, ContextManager mutates older tool results to omission markers
-> after omission, rebuild and re-estimate the candidate ModelContextFrame
-> if the re-estimated candidate context exceeds compression threshold:
     build CompressionContextFrame and run runtime-owned compression
     mutate compressible durable conversation prefix
-> rebuild ModelContextFrame from optimized ReplRuntime.conversation
-> estimate final ModelContextFrame tokens and update context status
-> if final context still exceeds the hard context limit (`window_tokens`):
     record context_limit_exceeded event and context checkpoint fact, mark UI turn failed, return to input
-> else call AgentLoopAdapter
```

Ordinary task `ModelContextFrame` composition order:

1. runtime safety prefix.
2. main agent system prompt.
3. stable skill formatter header.
4. available skill headers from the frozen skill registry snapshot.
5. context summary, if present.
6. retained conversation messages.
7. runtime-supplied active `SKILL.md` context for this turn.
8. current user input or tool-loop messages.

The system block is stable for the session. Dynamic active skill instructions
are supplied in the near context zone as runtime-authored context messages, not
by mutating the system prompt.

Compression does not use the ordinary task `ModelContextFrame`. It uses a
runtime-owned `CompressionContextFrame` that excludes the main agent system
prompt, available skill headers, model-visible tool schemas, and active
`SKILL.md` bodies. It includes the compression instruction, compressible
conversation history, structured active skill refs, visible artifact refs, and
visible policy or approval facts needed for continuity.

## Approval Flow

```text
model requests tool
-> adapter delegates to ToolBroker
-> ToolBroker validates schema and normalizes tool facts
-> PermissionEvaluator checks deny rules, approval mode, allow/trust rules, and grants
   (activate_skill and load_skill_ref_file resolve frozen targets before approval)
-> if approval needed, ToolBroker calls ApprovalProvider
-> REPL asks inline
-> y/a continues execution
-> n records the denial and returns TurnAborted to the executor
-> executor short-circuits the current turn without a same-turn follow-up model call
-> input is re-enabled
```

User denial is a turn-scoped outcome. It must not fail the session unless a
later runtime component independently encounters a terminal error.

Interactive user denial produces a `TurnAborted` outcome. It is not fed back
into the model as an ordinary tool result for another reasoning step in the same
turn. `PromptAgentExecutor` stops the current tool loop and does not make a
same-turn follow-up model call. Runtime records the approval decision and tool
denial audit facts, records the denied tool result as a terminal observation in
durable LLM-visible conversation for future turns, marks the UI turn as ended
normally, and returns the REPL to prompt input. Policy, schema, or config denials
that happen before an interactive approval request may still be represented as
ordinary denied `ToolResult` values unless a narrower spec requires turn
short-circuiting.

For one-shot prompt runs, approval denial is terminal because there is no later
REPL input boundary. Runtime records the approval decision and denied tool audit
facts, marks the one-shot run and session as `failed` with
`error_class="policy_denied"`, and exits non-zero.

Phase 0.5 disables prompt input while a turn is actively executing. Phase 1
approval is an exception to that rule: when a tool requires interactive
approval, the input lane temporarily switches to approval mode so the user can
enter `y`, `a`, or `n`. After the decision is recorded, the input lane returns
to normal prompt input or remains disabled if the turn is still executing.

## Compression Flow

`/compress` and automatic threshold-B compression use the same implementation
path. The only difference is the trigger.

Compression is allowed only at safe boundaries:

- before a model call.
- while REPL is idle for manual `/compress`.

It is not allowed in the middle of a model stream or active tool invocation.

Automatic compression must preserve the current-turn protected suffix. The
protected suffix includes the current user input, current-turn assistant
tool-call messages, fresh tool results, and follow-up tool-loop messages. These
messages are excluded from the compression model call and appended unchanged to
the real model call after compression. Compression may only replace history
before the previous safe boundary.

The compression model call is runtime-owned and tool-less. It does not expose
model-visible tools, does not enter the ordinary tool loop, and does not append
an assistant answer to durable conversation. Its parsed continuity summary and
context snapshot are the only durable outputs of the compression call.

Compression model calls still use the normal model-call audit event path.
Runtime writes `model_call_started` and `model_call_completed` or
`model_call_failed` with `purpose="compression"` and an empty model-visible tool
set. On success, runtime then writes the compression-specific run event,
context snapshot, and checkpoint facts.

If omission/compression still leaves the next `ModelContextFrame` over the hard
context limit (`window_tokens`), runtime must not call the adapter. It marks
only the UI turn as failed, records the `context_limit_exceeded` run event and
`context` checkpoint fact, shows an English UI message, and returns the REPL to
prompt input without terminalizing the session or long-lived prompt run. For
REPL prompt runs, persisted `sessions.status` and `runs.status` remain
`running`.

For one-shot prompt runs, the same pre-adapter context-limit branch records the
same run event and `context` checkpoint fact, then marks the one-shot run and
session as terminal `failed` with `error_class="context_limit_exceeded"` and
returns a non-zero CLI exit code.

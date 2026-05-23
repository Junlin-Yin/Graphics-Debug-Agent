# Phase 1 Scope

## Goal

Phase 1 delivers prompt skills, controlled native/shell tools, session-local
approval grants, path policy, and LLM-visible context compression.

Phase 1 extends the Phase 0 runtime truth model and the Phase 0.5 REPL
presentation layer without introducing subagents, workflow, MCP, plugin
packaging, or broad provider/model discovery.

## Must Implement

- Prompt skills:
  - `SkillRegistry`.
  - `SKILL.md` manifest parsing for prompt skills.
  - `references/**` file-level snapshots for prompt skills.
  - registration-time full `SKILL.md` and reference file snapshot.
  - frozen-snapshot hash verification.
  - `activate_skill` as a runtime tool executed through `ToolBroker`.
  - `load_skill_ref_file` as a runtime tool executed through `ToolBroker`.
  - run-scoped `active_skills` persistence and audit.
- Prompt composition:
  - active `SKILL.md` content is injected by the prompt composer before each
    model call, not stored as conversation history.
  - active `SKILL.md` content is outside `/compress` scope.
  - loaded skill reference files are ordinary durable conversation tool
    observations and may be omitted or compressed.
  - no `deactivate_skill` command in Phase 1.
- Context management:
  - runtime-owned query control plane for query composition, continuation
    reasons, active skill injection, context estimation, and context
    optimization decisions.
  - `ContextManager`.
  - `ModelContextFrame` as the runtime-owned LLM-visible request frame used for
    token estimates, including message segments and tool schema bindings.
  - `CompressionContextFrame` for runtime-owned compression model calls.
  - deterministic runtime-owned token estimator for pre-call context estimates.
  - context snapshot storage.
  - enable `runs.context_snapshot_id`.
  - context window estimation and status-bar budget display.
  - large model/tool output remains artifact-backed with summaries and artifact
    ids in model-visible context.
  - old tool-result omission at `omit_old_tool_results_at_ratio`.
  - `retain_recent_model_calls` as the raw recent-history retention setting.
  - `compression_reserved_output_tokens` as the compression-call output margin
    setting, defaulting to `10000`.
  - query-control-plane derived `model_call_group` view for omission and
    compression eligibility.
  - automatic rolling conversation compression at `compress_history_at_ratio`
    or when eligible evictable history exceeds the derived compression input
    budget.
  - `/compress` shares the same rolling summary compression machinery and is
    allowed only while idle, but does not run old tool-result omission.
  - if omission/compression still leaves the next `ModelContextFrame` over the
    hard context limit (`window_tokens`), the UI turn fails and runtime records
    a run event/checkpoint fact without terminating the session or long-lived
    prompt run.
- Tool safety:
  - Phase 1 `ToolBroker` acts as the runtime-owned tool control plane.
  - `ToolUseContext` for normalized per-call execution context.
  - fixed permission decision pipeline for builtin path, user path,
    builtin shell, user shell, runtime-control, approval-mode, and
    approval-grant decisions.
  - path policy declared by the main agent config at
    `~/.debug-agent/agent.toml`.
  - shell policy declared independently from path policy in
    `~/.debug-agent/agent.toml`.
  - builtin shell deny rules that cannot be overridden by user shell `allow`
    rules or approval.
  - runtime and `ToolBroker` enforce path policy; agent config only declares it.
  - runtime and `ToolBroker` enforce shell policy before shell execution.
  - session-local approval grants with persisted audit records.
  - approval modes: `normal`, `semi-auto`, `yolo`.
  - approval behavior is path-aware: `normal` automatically allows read access
    inside trusted workspace only; `semi-auto` automatically allows read access
    unless blacklisted and write/execute access inside trusted workspace; `yolo`
    skips interactive approval but still enforces schema validation, path policy
    including blacklist veto, shell policy, timeout, artifact handling, and
    audit.
  - fixed Phase 1 model-visible tool set:
    - `read_file`
    - `list_dir`
    - `search_text`
    - `write_file`
    - `edit_file`
    - `shell_exec`
    - `activate_skill`
    - `load_skill_ref_file`
  - remove model-visible `git_status` native tool; git access for the model goes
    through `shell_exec` and shell policy.
- REPL and CLI:
  - `/skills`.
  - `/tools`.
  - `/compress`.
  - idle-state `Ctrl+Y` approval mode cycling through
    `normal -> semi-auto -> yolo -> normal`, with persisted run-event and engine
    log audit.
  - REPL default approval mode remains `normal`.
  - one-shot default approval mode remains `normal`; users may explicitly select
    `semi-auto` or `yolo` through the CLI approval-mode option.
  - TUI approval prompt integrated into the existing prompt_toolkit
    `Application`.
  - plain REPL approval prompt where interactive input is available.
  - bottom status bar shows model, approval, context window usage, and
    cumulative token usage.

## Must Not Implement

- `AgentRegistry`.
- `/agents`.
- `/models`.
- subagents.
- workflow execution.
- workflow skill activation.
- workflow skill manifests in the Phase 1 skill registry.
- MCP server lifecycle, MCP tool discovery, or MCP tool invocation.
- plugin packaging.
- skill, agent, config, or model hot reload.
- persistent approval grants across sessions.
- `deactivate_skill`.
- section-level progressive disclosure for skills.
- semantic skill reference retrieval.
- token-level resume, tool-mid-flight resume, or subagent-mid-thought resume.
- arbitrary unrestricted shell execution.
- regex-based shell policy matching.

## Phase 1 Runtime Contract Additions

Phase 1 adds `context_limit_exceeded` and `compression_failed` to the shared
runtime error classes.

`context_limit_exceeded` is used for turn-scoped context-limit failure after
omission/compression. It is recorded as a run event and `context` checkpoint
fact. It must not mark `sessions.status` or `runs.status` as `failed` for a
long-lived REPL prompt run; those statuses remain `running` and the REPL accepts
the next user query.

`compression_failed` is used when the compression model call fails, returns
empty output, returns output that cannot be parsed into a valid continuity
summary, runtime cannot construct a compression input within `window_tokens`
while respecting `compression_reserved_output_tokens`, or the oldest eligible
evictable `model_call_group` cannot fit within the derived compression input
budget. It is recorded as a run event and `context` checkpoint fact, and the
current turn is aborted without terminalizing the session or long-lived prompt
run.

For one-shot prompt runs, both `context_limit_exceeded` and `compression_failed`
are terminal because there is no later REPL query boundary. Runtime records the
same run event and `context` checkpoint fact before terminalization, then marks
the one-shot run and session as `failed` with the corresponding error class in
terminal error metadata, and exits non-zero.

## Scope Adjustments From Roadmap

`AgentRegistry` and `/agents` move to Phase 2 because they are primarily needed
for subagent support.

`/models` is not implemented in Phase 1 and is not assigned to a later phase.
Phase 0.5 already displays the current frozen model in the status bar. A
standalone model listing command would require provider/model discovery that
conflicts with the narrow provider strategy.

Phase 1 keeps the project-contract command name `/compress`. The internal
operation may be called compacting or compression, but `/compact` is not a
Phase 1 public command unless the project contract is explicitly changed.

## Compatibility

Phase 1 is a schema and safety-policy breaking change from Phase 0 and Phase
0.5.

Phase 1 does not load, resume, attach to, query, trace, migrate, or apply active
ownership compatibility rules to sessions created by Phase 0 or Phase 0.5.
Runtime initialization, `debug-agent status`, and `debug-agent trace` must use the
Phase 1 schema and snapshot shapes.

If a workspace contains a Phase 0 or Phase 0.5 `.sessions/runtime.db`, Phase 1
must fail closed with a clear legacy-schema error. It must not silently
reinterpret legacy rows as Phase 1 runtime truth, and Phase 1 `status` and
`trace` do not support legacy sessions.

The user-facing legacy-schema error must say that Phase 0/0.5 runtime databases
are unsupported by Phase 1 and instruct the user to move or remove `.sessions/`
or use a fresh workspace. Runtime must not migrate, delete, or rewrite the
legacy database automatically.

This is the final Phase 1 compatibility contract, not an open implementation
question. Phase 1 identifies its schema with SQLite `PRAGMA user_version`. If
`.sessions/runtime.db` does not exist, Phase 1 creates it with the Phase 1 schema
and writes `PHASE_1_SCHEMA_USER_VERSION = 1` before interpreting runtime rows.
If `.sessions/runtime.db` already exists, Phase 1 reads `PRAGMA user_version`
before active ownership, status, trace, or session startup logic interprets any
rows. A missing (`0`), unknown, Phase 0, or Phase 0.5 `user_version` is treated
as legacy schema and fails closed with `error_class="config_error"`.
Runtime initialization, `debug-agent status`, and `debug-agent trace` must all
perform this schema-version check before reading any runtime truth tables.

Phase 1 intentionally tightens filesystem safety. The Phase 0 `search_text`
behavior allowed explicit searches inside directories that were skipped by
default. Phase 1 promotes those skipped directories to builtin path-policy deny
rules that apply uniformly to filesystem tools and classified shell paths. These
builtin deny rules are hard denies for Phase 1 and cannot be overridden by
approval or user path policy.

## Minimum Runnable Slice

1. User starts a REPL session.
2. Runtime loads global config and main agent `~/.debug-agent/agent.toml`.
3. Runtime resolves and validates config and main-agent policy facts, initializes
   the Phase 1 database, checks active session ownership, creates the session,
   prompt run, and artifact root, and persists the frozen session config
   snapshot.
4. Runtime snapshots prompt skills, persists the skill registry snapshot, and
   makes available skill headers ready before any user prompt is accepted.
5. Runtime initializes `ToolBroker` from frozen builtin policy, main-agent path
   policy, main-agent shell policy, and validated runtime-control targets.
6. User asks for a task that causes the model to call `activate_skill`.
7. `activate_skill` runs through `ToolBroker`, validates the skill hash, updates
   run-scoped `active_skills`, and records audit events.
8. The next model call includes runtime-supplied active skill context through
   prompt composition.
9. If needed, the model calls `load_skill_ref_file` to load a frozen reference
   file from the active skill as an ordinary tool observation.
10. The model calls a controlled tool.
11. `ToolBroker` normalizes the tool call, applies the fixed permission
   decision pipeline, applies timeout, artifact rules, and audit, then routes the
   allowed call to the native, shell, or runtime-control handler.
12. If approval is needed, the REPL asks the user inline.
13. Before each ordinary task model call, if context grows past configured
    thresholds, `ContextManager` applies omission or the query control plane runs
    at most one rolling compression call over bounded evictable
    `model_call_group` history.
14. `/compress` triggers the same rolling summary compression machinery while
    idle. Manual `/compress` does not run old tool-result omission.

## Completion Definition

Phase 1 is complete when:

- prompt skills can be discovered, snapshotted, activated, injected, and audited.
- non-prompt skill manifests fail startup with `config_error`.
- skill content is not compressed into conversation summaries and remains
  recoverable from the frozen skill snapshot.
- active skill records survive `/compress`.
- skill reference files are frozen at session startup and can be loaded through
  `load_skill_ref_file` only for active skills.
- loaded skill reference file outputs are ordinary conversation observations and
  may be omitted or compressed.
- skills are not automatically deactivated or disclosure-degraded.
- controlled tools cannot bypass `ToolBroker`.
- `ToolBroker` applies the fixed permission decision pipeline before routing
  tool calls to handlers.
- path policy, shell policy, runtime-control constraints, approval mode, and
  reusable session approval grants participate in one deterministic broker
  decision path.
- path policy denial is enforced by runtime, not by prompts.
- path policy applies only to model-visible tool invocations mediated by
  `ToolBroker`; runtime-owned persistence and artifact store operations are not
  tool invocations and are governed by persistence, artifact, and audit
  contracts.
- shell policy denial is enforced by runtime, not by prompts.
- shell commands must also pass path policy before execution, including `cwd`,
  path-qualified `argv[0]`, and runtime-classified path-like argv tokens.
- model-visible tools cannot read, list, search, write, edit, or shell into
  `.sessions/`, and cannot use artifact ids or runtime references to bypass this
  builtin deny rule.
- model-visible tools cannot read, list, search, write, edit, or shell into the
  configured skill source roots `~/.debug-agent/skills/` and
  `<workspace_root>/.debug-agent/skills/`.
  Prompt skill content is exposed only through frozen skill snapshots,
  `/skills`, active skill injection, and `load_skill_ref_file`.
- Phase 1 acknowledges that argv path classification cannot fully sandbox shell
  command filesystem side effects.
- approval decisions are persisted for audit and grants apply only to the
  current session.
- in one-shot mode, approval denial or unavailable interactive approval for an
  approval-required operation records `policy_denied`, terminalizes the
  one-shot run/session as `failed`, and exits non-zero.
- `semi-auto` behavior matches its documented boundary.
- user denial of a tool approval produces a `TurnAborted` outcome, ends the
  current turn normally without a same-turn follow-up model call, records the
  denied tool result as a terminal observation visible to future turns, and
  returns the REPL to prompt input without terminalizing the session.
- `/compress` and automatic compression share one rolling summary compression
  implementation path, while manual `/compress` skips old tool-result omission.
- compression evicts only closed, later-consumed `model_call_group` values
  outside the live/unconsumed suffix and outside the
  `retain_recent_model_calls` raw window.
- automatic compression runs when post-omission context exceeds
  `compress_history_at_ratio * window_tokens` or when eligible evictable history
  exceeds the derived compression input budget.
- each pre-call optimization pass runs at most one compression model call.
- manual `/compress` compression failure uses the same `compression_failed`
  event/checkpoint behavior as automatic compression, does not write a context
  snapshot, does not mutate conversation, and keeps the long-lived REPL prompt
  run/session non-terminal.
- when context remains over the hard limit (`window_tokens`) after omission and
  compression, the UI turn is marked failed, an English UI message is displayed,
  a run event/checkpoint fact is recorded with
  `error_class="context_limit_exceeded"`, persisted session/run statuses remain
  non-terminal, and the REPL remains usable for the next query.
- in one-shot mode, the same context-limit condition records the same event and
  checkpoint fact, then terminalizes the one-shot run/session as `failed` and
  exits non-zero.
- `/tools` lists current runtime-visible tools and disabled reasons.
- status bar context percentage is based on deterministic `ModelContextFrame`
  estimates.
- Phase 1 acceptance is evaluated against the Phase 1 schema and safety policy.
  Phase 0 and Phase 0.5 sessions and the Phase 0 explicit-search behavior for
  generated/dependency directories are not compatibility requirements.
- startup failures after session/run creation but before the first user prompt
  are recorded as `config_error`, terminalize the partially initialized
  run/session as `failed`, and release workspace active ownership.

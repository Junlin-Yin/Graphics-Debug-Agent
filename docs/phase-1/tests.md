# Phase 1 Test Plan

## Unit Tests

### Skill Registry And Snapshot

- `SkillRegistry` discovers prompt skills from global and project paths
  according to precedence.
- project skills override same-name global skills.
- duplicate skill names within the same discovery scope fail with
  `config_error`.
- CLI explicit skill paths and builtin skill roots are not discovered in
  Phase 1.
- same-name skills override as whole skills and do not merge files.
- `SKILL.md` must have valid front matter with `name` and `description`.
- unknown top-level manifest fields fail with `config_error`.
- manifest fields must match the documented Phase 1 types and skill name
  pattern.
- absent `execution_mode` is treated as `prompt`.
- `execution_mode: workflow` is recorded as unsupported/deferred and is not
  exposed to `activate_skill`.
- registry reads and snapshots `SKILL.md` plus files under `references/**`.
- registry ignores files outside `SKILL.md` and `references/**`.
- non-Markdown files outside `references/**` are not copied into session
  artifacts and do not affect the skill content hash.
- skill discovery, snapshot, persistence, and available-skill header generation
  complete before one-shot execution or REPL input accepts the first user prompt.
- skill registry snapshots are persisted separately from
  `sessions.config_snapshot_json` and associated with the session and prompt run.
- registry computes stable SHA-256 content hashes.
- activation validates against the frozen session snapshot without re-reading
  source files.
- frozen snapshot hash mismatch fails and writes an audit event.
- modifying or deleting a skill source file after session startup does not
  change first activation behavior for the active session.
- active skills are reconstructed from frozen snapshots on later model calls
  without re-reading source files.
- Markdown and non-Markdown references are represented as file-level frozen
  reference snapshots, not section trees.
- reference file content hashes participate in the skill content hash.
- modifying or deleting a reference source file after session startup does not
  change active session behavior.
- large reference payloads are artifact-backed.
- no section ids, section trees, semantic retrieval, or progressive disclosure
  state is produced in Phase 1.

### Skill Activation And Prompt Composition

- `activate_skill` is exposed as a runtime tool definition.
- `load_skill_ref_file` is exposed as a runtime tool definition.
- `activate_skill` invocation goes through `ToolBroker`.
- `load_skill_ref_file` invocation goes through `ToolBroker`.
- unknown skill activation returns `ToolResult(status="denied")` without
  prompting for approval.
- workflow skill activation returns `ToolResult(status="denied")` without
  prompting for approval.
- frozen snapshot corruption or hash mismatch returns
  `ToolResult(status="denied")` without prompting for approval.
- repeated activation is idempotent.
- successful activation updates run-scoped structured `active_skills` entries
  with name, content hash, activation reason, and scope.
- successful activation writes skill activation audit events.
- `activate_skill` returns a short activation result and never returns full
  skill body as ordinary tool output.
- active `SKILL.md` content becomes visible on the next model call.
- active `SKILL.md` content is not appended to `ReplRuntime.conversation`.
- active skill context lists available frozen reference file paths and hashes
  without injecting reference file content automatically.
- `load_skill_ref_file` succeeds only for already active skills.
- `load_skill_ref_file` resolves `path` only inside the frozen `references/**`
  snapshot for the requested skill.
- `load_skill_ref_file` denies path traversal, absolute paths, inactive skills,
  unknown references, and frozen reference hash mismatches without reading source
  files.
- text reference files below the inline threshold return text plus metadata.
- large text and non-text reference files return artifact/reference markers plus
  metadata without injecting raw large or binary content.
- loaded reference file outputs are ordinary durable conversation tool
  observations and may later be omitted or compressed.
- `PromptComposer` keeps the stable system block stable across skill
  activation.
- `PromptComposer` adds runtime-supplied active skill context to
  `ModelContextFrame`.
- active skill context is marked authoritative for the current turn.
- active skill context includes skill id, content hash or version, activation
  reason, scope, `SKILL.md` instructions, and available reference metadata.
- model-visible `allowed_tools` and `path_policy` fields in skill context do not
  authorize execution.
- active skill content is not disclosure-degraded under budget pressure.
- no `deactivate_skill` tool or slash command is exposed in Phase 1.

### ModelContextFrame And ContextManager

- query control plane creates query state for each REPL user submission and
  one-shot prompt.
- query control plane records continuation reasons for initial model calls,
  tool-result continuations, post-compression continuations, approval-denial
  aborts, compression-failure aborts, context-limit aborts, and final assistant
  responses.
- durable conversation messages carry enough metadata to identify turn ids,
  model-call ids, tool-call ids, artifact refs, safe boundaries, and the
  current-turn protected suffix.
- `ModelContextFrame` contains stable system content, context summary, retained
  messages, runtime-supplied active skill context, tool-loop messages, and
  current user input when applicable.
- `PromptComposer` materializes `ModelContextFrame` into the existing
  `AgentRunRequest.system_prompt`, `AgentRunRequest.conversation`,
  `AgentRunRequest.user_input`, and `AgentRunRequest.tools` fields before the
  adapter call.
- the materialized `AgentRunRequest` is semantically equivalent to the
  `ModelContextFrame` used for token estimation and context decisions.
- token estimates are based on `ModelContextFrame`, not raw
  `ReplRuntime.conversation`.
- context checks run before every adapter model invocation, including tool-loop
  follow-up model calls.
- `TokenEstimator` produces deterministic pre-call estimates from
  `ModelContextFrame`.
- context settings load from `~/.debug-agent/config.toml`.
- absent `[context]` uses Phase 1 built-in defaults for `window_tokens`,
  `omit_old_tool_results_at_ratio`, `compress_history_at_ratio`, and
  `retain_recent_turns`.
- invalid context settings fail startup with `config_error`.
- configured `window_tokens` is frozen into the session config snapshot.
- context window percentage uses `window_tokens`.
- status-bar context window display uses the frozen configured
  `window_tokens`, not a hardcoded default.
- `retain_recent_turns` applies to the durable `ReplRuntime.conversation` list,
  not to `ModelContextFrame`.
- `omit_old_tool_results_at_ratio` triggers old tool-result omission.
- old tool-result omission keeps recent `retain_recent_turns` turns from
  `ReplRuntime.conversation` intact.
- old tool-result omission keeps artifact ids visible when available.
- old tool-result omission mutates `ReplRuntime.conversation` by replacing
  older tool result bodies with omission markers.
- old tool-result omission does not mutate persisted run events or artifacts.
- old tool-result omission writes a context snapshot and updates
  `runs.context_snapshot_id`.
- old tool-result omission emits a REPL system message with reduced-from and
  reduced-to estimates.
- after old tool-result omission, runtime rebuilds and re-estimates the
  candidate `ModelContextFrame` before deciding whether compression is needed.
- `compress_history_at_ratio` triggers conversation compression.
- automatic compression excludes the current user input.
- automatic compression excludes the full current-turn protected suffix,
  including current user input, current-turn assistant tool-call messages, fresh
  tool results, and follow-up tool-loop messages.
- compression prompt preserves task goal, completed milestones, inspected or
  modified files, remaining work, next plan, key decisions, and constraints.
- compression output must parse as the required Phase 1 continuity summary JSON
  object.
- compression output with missing required keys, non-string list entries,
  non-object output, or empty output fails with `compression_failed`.
- inability to construct a compression model input within `window_tokens` fails
  with `compression_failed` before any compression model call is made.
- compression model calls are runtime-owned and tool-less.
- compression model calls do not expose model-visible tools, enter the ordinary
  tool loop, or append assistant answers to durable conversation.
- compression model calls use `CompressionContextFrame`, not the ordinary task
  `ModelContextFrame`.
- `CompressionContextFrame` excludes the main agent system prompt, available
  skill headers, model-visible tool schemas, active `SKILL.md` bodies, and the
  current-turn protected suffix.
- ordinary task `ModelContextFrame` estimates still include the stable system
  block, available skill headers, model-visible tool schemas, and active
  `SKILL.md` bodies.
- compression model calls write normal `model_call_started` and
  `model_call_completed` or `model_call_failed` events with
  `purpose="compression"` and an empty model-visible tool set.
- successful compression writes compression-specific run events after the
  compression model-call event pair.
- runtime, not the compression summary, preserves active skill refs, frozen skill
  and reference snapshots, artifact ids, approval records, path policy, shell
  policy, and context snapshot ids.
- compression replaces the compressible portion of `ReplRuntime.conversation`.
- compression rebuilds `ModelContextFrame` from the replaced conversation state.
- `/compress` uses the same compression path as automatic compression.
- `/compress` is accepted only while REPL is idle.
- `/compress` ignores the compression threshold when compressible history exists.
- `/compress` is a no-op with an English system message when durable
  conversation is empty.
- `/compress` is a no-op with the same English system message when safe-boundary
  and `retain_recent_turns` rules leave no compressible prefix.
- `/compress` does not call skill activation or tool execution.
- `/compress` preserves structured active skill refs.
- context snapshot stores trigger, source checkpoint id, active skill refs,
  summary, retained messages, omitted tool result count, artifact refs, token
  estimate, payload artifact id when present, and version.
- context snapshot trigger supports `manual`, `omission`, `compression`, and
  `omission | compression`.
- compression context snapshots store `summary` as canonical JSON serialization
  of the parsed continuity summary.
- omission-only context snapshots store an empty string as `summary`.
- context snapshot does not store raw large tool/model outputs or skill bodies
  inline.
- context snapshot stores post-optimization continuity state, not the
  pre-compression full context and not the final composed `ModelContextFrame`.
- automatic omission and compression context snapshots exclude the current user
  input and the rest of the current-turn protected suffix.
- `runs.context_snapshot_id` is updated when a context snapshot is written.
- `context` checkpoints reference the written context snapshot.
- when omission/compression still leaves the next `ModelContextFrame` over the
  hard context limit (`window_tokens`), the UI turn fails without calling the
  model adapter.
- context-limit failure records `error_class="context_limit_exceeded"`, displays
  `Context window still exceeds the limit after compression. The current turn was aborted.`, and keeps the REPL session usable.
- context-limit failure writes a `context_limit_exceeded` run event.
- context-limit failure writes a `context` checkpoint fact and keeps
  `session_status` and `run_status` as `running` for a long-lived REPL prompt
  run.
- one-shot context-limit failure writes the same run event and `context`
  checkpoint fact, then marks the one-shot run/session as terminal `failed` with
  `error_class="context_limit_exceeded"` and exits non-zero.

### Status Bar And Token Accounting

Phase 1 status bar supersedes the Phase 0.5 status bar format.

- status bar renders in the order:
  `model | approval | context | tokens`.
- status bar displays context as `<used> / <window> (<pct>)`.
- status bar displays cumulative token usage as `<used> used`.
- provider token usage updates cumulative usage after model calls.
- missing provider token usage falls back to deterministic estimates.
- before each model call, context estimate updates status-bar context fields.
- after omission or compression, status-bar context fields update immediately.
- Phase 1 does not perform timer-based context estimation.

### ToolBroker Policy

- `ToolBroker` acts as the Phase 1 tool control plane, not a direct handler
  lookup table.
- `ToolDefinition` includes category, risk level, and access metadata.
- `ToolUseContext` is assembled for each brokered call from frozen config,
  frozen policy facts, approval mode, runtime stores, and session approval
  records.
- `ToolRouter` routes allowed calls only after permission evaluation and
  approval are complete.
- tool handlers do not write audit events directly.
- tool handlers do not read mutable global policy directly.
- builtin path policy, user path policy, builtin shell policy, user shell policy,
  runtime-control decisions, and reusable session approval grants are normalized
  into `PermissionRule` values.
- `PermissionRule.effect` supports `deny`, `allow`, `trust`, and `ask`.
- path trust rules use `effect="trust"` and do not by themselves grant read,
  write, execute, or runtime-control permission.
- reusable `approved_for_session` grants are evaluated as session-local
  `PermissionRule` values with `effect="allow"` after schema validation and all
  deny rules.
- `PermissionEvaluator` checks deny rules before approval mode, allow/trust
  rules, approval grants, or user approval.
- non-empty shell allowlist miss is a policy denial and does not ask the user.
- path trust rules are not exclusive allowlist gates.
- `PermissionEvaluator` applies approval mode before asking the user.
- policy denial cannot be overridden by allow rules, session grants, or
  approval.
- Phase 1 does not define per-tool approval metadata such as
  `requires_approval`.
- model-visible tool definitions are exactly `read_file`, `list_dir`,
  `search_text`, `write_file`, `edit_file`, `shell_exec`, `activate_skill`, and
  `load_skill_ref_file`.
- all model-visible tool schemas reject unknown fields.
- `read_file` accepts `path` and optional positive integer `limit` interpreted
  as line count.
- `list_dir` accepts `path` and optional positive integer `limit` interpreted
  as entry count.
- `search_text` accepts `path`, `query`, and optional positive integer `limit`
  interpreted as match count.
- `write_file` accepts `path` and `content`, and may create missing parent
  directories under authorized write scope.
- `write_file` writes complete UTF-8 file content only under authorized write
  paths.
- `edit_file` accepts `path`, `old_text`, and `new_text`.
- `edit_file` performs structured exact-match text replacement only under
  authorized write paths.
- `edit_file` replaces only the first exact occurrence and returns
  `ToolResult(status="error")` when `old_text` is absent.
- `shell_exec` accepts non-empty `argv`, optional `cwd`, and optional positive
  integer `timeout_seconds`.
- `shell_exec` effective timeout is the lesser of requested timeout and frozen
  config timeout, or frozen config timeout when omitted.
- `activate_skill` uses `runtime_control` risk.
- `load_skill_ref_file` uses read risk and runtime-control category.
- `load_skill_ref_file` is audit-only in every approval mode when the target
  skill is active, the frozen reference path resolves, and the frozen reference
  hash validates.
- `load_skill_ref_file` denials for inactive skills, invalid paths, missing
  references, corrupt snapshots, or hash mismatch happen before approval and
  cannot be overridden.
- path policy is loaded from main agent `~/.debug-agent/agent.toml`.
- path policy accepts only `trust` and `deny` scopes.
- path policy does not classify read, write, or execute tool types.
- trusted workspace is session `workspace_root` plus path policy `trust` paths.
- path policy `trust` adds trusted roots and does not narrow default trust for
  `workspace_root`.
- absent path policy uses session `workspace_root` as trusted workspace.
- non-blacklisted paths outside trusted workspace are not denied by path policy
  solely for being outside trusted workspace.
- path policy blacklist denies before approval is requested.
- path policy blacklist applies in every approval mode, including `yolo`.
- path policy blacklist cannot be overridden by approval.
- path policy is enforced by runtime/ToolBroker, not by prompts.
- path policy applies only to model-visible tool invocations mediated by
  `ToolBroker`; runtime-owned persistence and artifact store operations are not
  path-policy tool access.
- relative paths are resolved against `workspace_root`.
- absolute path policy entries are accepted.
- path policy canonicalizes requested paths before matching.
- trailing `/` policy entries match subtrees.
- non-trailing-`/` file policy entries match exact canonical paths.
- path policy canonicalizes missing targets by resolving the deepest existing
  parent and appending non-existing path components lexically.
- builtin directory deny rules match any same-name directory component under any
  accessed root.
- path traversal into blacklisted paths is denied.
- symlink escape into blacklisted paths is denied.
- builtin path-policy deny rules block explicit access to `.git`,
  `node_modules`, `build`, `dist`, `.venv`, `__pycache__`, `.pytest_cache`, and
  `.sessions` as an intentional Phase 1 breaking change.
- builtin path-policy deny rules are hard denies and cannot be overridden by
  approval or user path policy in Phase 1.
- model-visible tools cannot read, list, search, write, edit, or shell into
  `.sessions/`.
- model-visible tools cannot use artifact ids or runtime references to bypass
  the builtin `.sessions/` deny rule.
- runtime-owned stores can write under `.sessions/` through runtime service APIs
  without path-policy evaluation.
- shell policy is loaded independently from path policy.
- absent shell policy defaults to empty user allow and empty user deny, plus
  builtin shell deny rules, subject to path policy, approval/risk policy,
  timeout, and audit.
- empty shell allow list means default allow as an accepted Phase 1 local
  automation risk, not a sandbox guarantee.
- builtin shell deny rules cannot be overridden by user allow rules or approval.
- builtin shell deny blocks privilege escalation, destructive recursive delete,
  and raw shell trampoline command-string forms.
- destructive recursive delete builtin denial blocks normalized `rm` invocations
  with recursive short-option clusters or `--recursive`, including `rm -r`,
  `rm -R`, `rm -rf`, `rm -fr`, and `rm --recursive`.
- shell commands must pass shell policy, path policy, and approval/risk policy.
- execute access requires shell policy in addition to path policy and approval
  mode checks.
- shell policy uses argv-prefix matching.
- shell policy matching uses normalized executable identities.
- shell policy normalizes path-qualified executables and common Windows
  executable suffixes before matching.
- shell policy unwraps runtime-defined transparent wrapper forms, including
  `env`-style wrappers whose nested command is structurally visible.
- shell policy does not semantically inspect opaque wrappers such as `npm run`,
  `make`, `uv run`, interpreter script execution, or arbitrary local scripts.
- generic `shell_exec` evaluates `cwd`, path-qualified `argv[0]`, and
  runtime-classified path-like argv tokens through path policy for blacklist and
  trusted/untrusted classification.
- generic `shell_exec` evaluates option/value path pairs only for the documented
  Phase 1 runtime-owned path option list.
- generic `shell_exec` documents the known issue that argv classification cannot
  fully sandbox command filesystem side effects.
- shell policy deny rules take precedence over allow rules.
- empty shell allow list means default allow subject to builtin deny, user deny,
  and other policy.
- non-empty shell allow list means only matching prefixes are allowed.
- regex shell policy is not accepted in Phase 1.
- raw shell strings are not accepted by the model-visible shell tool.
- shell execution uses structured argv and `shell=False`.
- command name normalization handles Windows `.exe`, `.cmd`, and `.bat`
  suffixes for policy matching.
- shell cwd is checked against path policy.
- shell stdout and stderr are captured and normalized into `ToolResult`.
- large shell stdout and stderr become text artifacts.
- shell timeout returns `ToolResult(status="timeout")`.
- model-visible `git_status` native tool is not exposed in Phase 1.
- denying `["git"]` denies direct and runtime-normalized git invocations,
  including supported transparent wrapper forms.
- CLI `debug-agent status` and `debug-agent trace` are unaffected by shell
  policy.

### Approval Grants

- approval modes include `normal`, `semi-auto`, and `yolo`.
- `normal` automatically allows read access inside trusted workspace.
- `normal` requires approval for read access outside trusted workspace.
- `normal` requires approval for write and execute access on any path.
- `semi-auto` automatically allows read access unless blacklisted.
- `semi-auto` automatically allows write and execute access inside trusted
  workspace.
- `semi-auto` requires approval for write and execute access outside trusted
  workspace.
- `yolo` skips interactive approval but still applies schema validation, path
  blacklist veto, path policy, shell policy, timeout, artifact handling, and
  audit.
- approval grant keys include session id, tool name, risk level, and scope
  signature.
- file-tool approval scope signatures include the exact canonical path and
  access type.
- shell approval scope signatures include normalized argv, canonical cwd,
  effective timeout seconds, and classified argv path tokens.
- `activate_skill` approval scope signatures include skill name and content
  hash.
- `load_skill_ref_file` approval scope signatures include skill name, skill
  content hash, reference path, and reference content hash.
- `approved_once` does not create a reusable session grant.
- `approved_for_session` creates a reusable grant only for the current session.
- `denied` decisions are persisted for audit.
- approval grants do not apply across sessions.
- approval cannot override policy denial.
- `normal` mode requires approval for `activate_skill`.
- `semi-auto` and `yolo` do not request interactive approval for
  `runtime_control` tools but still audit the runtime-control decision.
- non-interactive approval requests return `policy_denied` and do not hang.
- `n` denial returns a denied tool outcome with `turn_aborted=true`, ends the
  current turn normally, and re-enables input.
- interactive approval denial does not trigger a same-turn follow-up model call.
- interactive approval denial records the denied tool result as a terminal
  observation visible to future turns after the next user input.

### REPL Commands And Views

- `/skills` lists discoverable prompt skills and unsupported/deferred workflow
  skills with reasons.
- `/tools` lists runtime-visible tools, category, risk, access, approval
  behavior, enabled status, and disabled reason.
- `/compress` triggers manual compression only while idle.
- `/compress` during active execution is suppressed without runtime side
  effects.
- `/agents` remains unsupported in Phase 1.
- `/models` remains unsupported in Phase 1.
- `Ctrl+Y` cycles `normal -> semi-auto -> yolo -> normal`.
- TUI approval prompt uses the existing prompt_toolkit application input area.
- TUI approval `y` approves once and continues execution.
- TUI approval `a` approves for the current session and continues execution.
- TUI approval `n` denies and returns to normal prompt input.
- plain REPL approval prompt works when interactive input is available.
- `debug-agent < input.txt` uses `PlainReplView`; if approval is required, it
  denies non-interactively rather than hanging.
- REPL default approval mode is `normal`.
- one-shot default approval mode is `normal`.
- one-shot may explicitly select `semi-auto` or `yolo` through the CLI
  approval-mode option.

### Persistence And Observability

- `approval_grants` table is created.
- `approval_grants.approval_request` stores the rendered approval request text.
- `context_snapshots` SQLite table is created.
- oversized context snapshot payloads are artifact-backed through
  `payload_artifact_id`.
- run events support skill activation, approval request, approval decision,
  context omission, context compression, and shell policy denial events.
- trace renders skill activation, active skill refs, approval decisions, shell
  policy denials, path policy denials, context snapshots, and compression
  events.
- engine log records approval mode switches, approval decisions, policy denials,
  context optimizations, and artifact registrations.
- large stack traces, shell outputs, and details are artifact-backed rather than
  stored inline.

## Integration Tests

- `debug-agent` REPL can activate a prompt skill through `activate_skill`, then
  the next model call receives runtime-supplied active skill context.
- one-shot explicitly configured as `semi-auto` can activate a prompt skill
  through `activate_skill`.
- workflow skills cannot be activated in Phase 1.
- modifying a skill file after session start does not change the frozen skill
  content used by the session.
- frozen snapshot hash mismatch fails with a clear audit trail.
- frozen reference file loading through `load_skill_ref_file` returns text
  content for small text references and artifact/reference markers for large or
  non-text references.
- loaded reference file outputs may be omitted or compressed as ordinary
  conversation observations.
- `/skills` shows prompt skills and unsupported workflow skills.
- `/tools` shows available and disabled tools with reasons.
- `/compress` while idle writes a context snapshot and updates
  `runs.context_snapshot_id`.
- `/compress` while idle and with empty durable conversation displays a no-op
  message and does not write a context snapshot.
- `/compress` while idle and with durable conversation but no compressible
  prefix displays a no-op message and does not write a context snapshot.
- `/compress` while idle and with compressible history runs compression even when
  the current context estimate is below `compress_history_at_ratio`.
- automatic old tool-result omission triggers before a model call and shows a
  REPL system message.
- automatic old tool-result omission updates `ReplRuntime.conversation`,
  writes a context snapshot, and leaves full tool output recoverable through
  events or artifacts.
- automatic conversation compression triggers before a model call and shows a
  REPL system message.
- compression model calls are visible in trace as model-call events with
  `purpose="compression"` before the compression-specific events.
- automatic compression in a tool-calling loop runs before follow-up model
  invocation when the follow-up frame exceeds the threshold.
- the current-turn protected suffix is not included in the compression model
  call.
- after compression, the current-turn protected suffix is passed unchanged to the
  real model call.
- after compression inside a tool-calling loop, fresh tool results and
  follow-up tool-loop messages are passed unchanged to the real model call.
- if context remains over the hard context limit (`window_tokens`) after
  omission and compression, the UI turn fails and the next REPL input is
  accepted.
- status bar displays model, approval, context, and tokens in the required
  order.
- provider usage updates cumulative token usage after model calls.
- context window percentage updates before model calls and after compression.
- `semi-auto` allows read-only native tools without asking unless path policy
  blacklist veto applies.
- `semi-auto` allows `shell_exec` inside trusted workspace when shell policy
  allows it and all classified paths are trusted or non-blacklisted.
- `semi-auto` asks approval for `shell_exec` outside trusted workspace when
  shell policy allows it.
- TUI approval `y` continues the current turn.
- TUI approval `a` creates a session-local reusable grant and continues the
  current turn.
- TUI approval `n` denies the tool, ends the turn normally, and returns to
  prompt input.
- approval denial short-circuits the current turn without making a same-turn
  follow-up model call.
- approval denial remains visible to future model calls as a terminal
  observation from the denied turn.
- one-shot default `normal` denies approval-required operations when no
  interactive approval provider is available.
- one-shot approval denial or unavailable interactive approval terminalizes the
  one-shot run/session as `failed` with `error_class="policy_denied"` and exits
  non-zero.
- one-shot explicitly configured as `semi-auto` automatically permits
  trusted-workspace write and shell operations when shell/path policy allow them.
- one-shot configured as `normal` or `semi-auto` denies approval-required
  untrusted operations when no interactive approval provider is available.
- shell allow prefix permits matching command argv.
- shell deny prefix blocks matching command argv.
- builtin shell deny blocks matching command argv even when user shell allow
  would otherwise permit it.
- path policy blacklist denial blocks shell command even when shell policy
  allows it.
- path policy blacklist denial blocks path-qualified `argv[0]` and classified
  argv paths, not only `cwd`.
- shell policy denial blocks shell command even when path policy allows it.
- model-visible tool list does not include `git_status`.
- denying `["git"]` blocks direct model-initiated `git status`, path-qualified
  git executable invocations, Windows git executable suffix forms, and supported
  transparent wrapper forms such as `env FOO=1 git status`.
- denying `["git"]` does not claim to block git hidden inside opaque wrapper
  commands; those wrappers must be denied directly or excluded by allowlist.
- `debug-agent trace <session_id>` includes Phase 1 skill, approval, tool, and
  compression events.
- Phase 1 startup, `status`, and `trace` fail closed with a clear legacy-schema
  error for Phase 0/0.5 legacy sessions.
- Phase 1 startup creates a missing `.sessions/runtime.db` with the Phase 1
  schema and writes `PRAGMA user_version = 1`
  (`PHASE_1_SCHEMA_USER_VERSION = 1`).
- Phase 1 startup, `status`, and `trace` fail closed before reading runtime truth
  when an existing runtime database has `PRAGMA user_version = 0`, unknown,
  Phase 0, or Phase 0.5.
- legacy schema failures use `error_class="config_error"`.
- one-shot mode remains plain stdout.
- non-TTY and injected I/O paths still use `PlainReplView`.
- Phase 0.5 streaming output remains non-authoritative and `AgentStreamEvent`
  is not persisted.

## Failure Scenarios

- invalid `SKILL.md` front matter.
- duplicate skill names within one discovery scope.
- unsupported workflow skill activation.
- frozen skill snapshot corrupt or hash-mismatched.
- missing referenced file in a frozen skill snapshot.
- malformed reference paths passed to `load_skill_ref_file`.
- reference file hash mismatch.
- invalid `~/.debug-agent/agent.toml`.
- invalid path policy scope other than `trust` or `deny`.
- invalid shell policy shape.
- regex shell policy attempt.
- shell command denied by allowlist.
- shell command denied by denylist.
- shell command denied by builtin denylist.
- shell cwd under blacklisted path.
- shell classified argv path under blacklisted path.
- shell timeout.
- shell stdout/stderr large enough to artifact.
- approval provider unavailable.
- approval denied.
- one-shot approval denial terminalizes the one-shot run/session as `failed`
  with `error_class="policy_denied"` and exits non-zero.
- stale or missing context snapshot.
- compression model failure.
- compression model failure writes `model_call_failed` with
  `purpose="compression"` before `compression_failed`.
- compression output empty or invalid.
- compression output missing required continuity summary JSON fields.
- compression input cannot be constructed within `window_tokens`.
- compression failure aborts the current turn without terminalizing the REPL
  session or long-lived prompt run.
- provider usage unavailable.
- context remains over hard context limit after omission and compression.
- context-limit failure must not mark the long-lived REPL prompt run or session
  as terminal `failed`.
- one-shot context-limit failure must mark the one-shot prompt run and session
  as terminal `failed` after recording the context event/checkpoint fact.
- `/compress` submitted while a turn is running.
- `/agents` or `/models` submitted in Phase 1.
- existing runtime database with `PRAGMA user_version = 0`, unknown, Phase 0, or
  Phase 0.5, reported as `error_class="config_error"`.

## Fake Model Testing

Fake model must support:

- deterministic assistant text.
- deterministic tool calls for `activate_skill`.
- deterministic tool calls for `load_skill_ref_file`.
- deterministic tool calls for native and shell tools.
- deterministic compression summary output.
- deterministic malformed compression summary output.
- forced compression failure.
- forced model error.
- forced timeout.
- provider usage present.
- provider usage absent.
- multi-call tool loop where the second model invocation exceeds a context
  threshold.

Tests should not require network access.

## Fake Tool And Shell Testing

Fake tool or fixture workspace must cover:

- read-only native tool success.
- writable native tool success within authorized path.
- writable native tool denied under blacklisted path.
- native tool denied for explicit access to builtin denied directories.
- writable native tool with a missing target path under a symlinked parent is
  checked using deepest-existing-parent canonicalization.
- shell argv allow prefix success.
- shell argv deny prefix denial.
- builtin shell deny prefix denial.
- shell command with Windows executable suffix normalization.
- shell command with path-qualified executable normalization.
- shell command through supported `env` transparent wrapper normalization.
- opaque wrapper command behavior documents that nested commands are not
  semantically inspected.
- shell cwd blacklist denial.
- shell path-qualified `argv[0]` blacklist denial.
- shell classified argv path blacklist denial.
- shell timeout.
- shell stdout and stderr artifacting.
- denied `git` command after `git_status` removal.
- denied path-qualified git and supported transparent-wrapper git command after
  `git_status` removal.
- `activate_skill` success and denial.
- `load_skill_ref_file` success and denial.

## Manual Tests

- macOS Terminal TTY approval prompt.
- iTerm2 TTY approval prompt.
- TTY denial returns to prompt input without closing the application.
- TTY `/skills`, `/tools`, and `/compress`.
- TTY status bar updates after context estimation, compression, and model
  usage.
- non-TTY `debug-agent < input.txt` does not hang on approval-required tool.
- Windows shell wrapper behavior using a fake runner or real Windows smoke
  environment when available.

## Smoke Commands

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
```

REPL smoke test:

```text
debug-agent
> use the relevant debugging skill
> /skills
> /tools
> /compress
> /exit
```

Fallback smoke test:

```text
debug-agent < input.txt
```

one-shot smoke test:

```bash
debug-agent --approval-mode semi-auto -p "use the relevant debugging skill"
```

## Phase 1 Acceptance

Phase 1 is accepted only if:

- Phase 1 unit and integration tests pass with fake model configuration.
- Phase 1 acceptance uses the Phase 1 schema and intentionally tightened path
  policy; Phase 0 and Phase 0.5 sessions are not queryable and fail closed with
  a clear legacy-schema error.
- prompt skills are frozen, activatable, injected through `ModelContextFrame`,
  and survive compression.
- skill reference files are frozen and loadable through `load_skill_ref_file`
  without being automatically injected into every model call.
- `/compress` and automatic compression share one path and replace
  `ReplRuntime.conversation`.
- status bar context percentage is based on `ModelContextFrame`.
- ToolBroker enforces shell policy, path policy, approval, timeout, and audit.
- approval grants are session-local and persisted for audit.
- model-visible `git_status` is removed and git access is controlled by shell
  policy.
- `/skills`, `/tools`, and `/compress` are available.
- `/agents` and `/models` remain unavailable.
- no subagent, workflow, MCP, or plugin feature is required for Phase 1
  acceptance.

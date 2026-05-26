# Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This is an implementation-process instruction only. Phase 1 runtime itself does not implement subagents.

**Goal:** Build prompt skills, controlled native/shell tools, runtime-enforced policy, approval grants, `ModelContextFrame`, and context compression while preserving the runtime truth and TUI boundaries established by Phase 0/0.5.

**Architecture:** Phase 1 turns `ToolBroker` into the model-visible tool control plane and introduces a runtime-owned query control plane for prompt composition and context optimization. Policy, tools, skills, and compression are delivered as separately testable vertical slices; subagents, workflow, MCP, plugin packaging, and hot reload remain out of scope.

**Tech Stack:** Python, SQLite, local filesystem artifacts, prompt_toolkit/rich REPL from Phase 0.5, LangChain-compatible adapter, pytest, uv.

---

## Goals

- Deliver prompt skill discovery, frozen startup snapshots, run-scoped activation, active skill prompt injection, frozen reference loading, `/skills`, and trace/audit visibility.
- Deliver controlled model-visible native tools and structured `shell_exec` through `ToolBroker`, with deterministic schema validation, path policy, shell policy, approval, timeout, artifacting, and audit.
- Deliver session-local interactive approval grants, approval modes `normal`, `semi-auto`, and `yolo`, TTY/plain/non-interactive approval behavior, `/tools`, and idle-only `Ctrl+Y` approval-mode cycling.
- Deliver runtime-owned `ModelContextFrame`, prompt composition, deterministic context estimates, query-control model-call groups, old tool-result omission, rolling compression, manual `/compress`, context snapshots, and context-limit/compression-failure handling.
- Preserve the runtime truth boundaries established in Phase 0/0.5: runtime state remains authoritative, `AgentStreamEvent` remains UI-only, one-shot/non-TTY paths remain plain, and TUI does not become a runtime truth layer.

## Non-goals

- No subagents, `AgentRegistry`, `/agents`, child run lifecycle, cancellation token expansion, or subagent-mid-thought resume.
- No workflow execution, workflow skill activation, workflow manifests in the Phase 1 registry, YAML DSL, nested workflow, or parallel workflow.
- No MCP server lifecycle, MCP tool discovery, MCP tool invocation, or plugin packaging.
- No skill, agent, config, model, MCP, or plugin hot reload.
- No `/models`, `/compact`, `deactivate_skill`, section-level skill disclosure, semantic reference retrieval, or automatic active-skill disclosure degradation.
- No token-level resume, tool-mid-flight resume, restart/resume recovery from context snapshots, or recovery from natural-language compression summaries.
- No unrestricted shell execution, raw shell strings, `shell=True`, regex shell policy, or filesystem sandbox guarantee for generic shell argv side effects.
- No persistent approval grants across sessions and no approval override for policy denial, schema validation failure, config errors, or invalid frozen skill targets.
- No Phase 0/0.5 runtime database migration, deletion, automatic rewrite, or legacy runtime-truth compatibility reads. `debug-agent status` and `debug-agent trace` may read only `PRAGMA user_version` from an existing legacy database, then must fail closed without querying legacy session, run, event, checkpoint, artifact, or active-ownership rows.
- No new provider/model discovery surface beyond the existing narrow provider strategy and deterministic fake model test path.

## File Structure To Create Or Modify

- `docs/phase-1/implementation-plan.md`: this milestone schedule.
- `src/debug_agent/runtime/config.py`: Phase 1 config parsing, context/execution defaults, main-agent policy declaration loading, frozen config snapshot shape.
- `src/debug_agent/runtime/orchestrator.py`: Phase 1 startup ordering across config/policy validation, schema gate, active ownership, session/run/artifact creation, startup-blocking skill snapshotting, broker/context/query-control initialization, and first-prompt acceptance.
- `src/debug_agent/persistence/sqlite.py`: Phase 1 schema bootstrap, `PRAGMA user_version = 1`, legacy-schema fail-closed checks.
- `src/debug_agent/persistence/sessions.py`: Phase 1 active ownership checks after schema-version validation.
- `src/debug_agent/persistence/runs.py`: `runs.context_snapshot_id` support and Phase 1 run metadata updates.
- `src/debug_agent/persistence/events.py`: Phase 1 event kinds for approval, policy denial, skill activation, context optimization, and compression.
- `src/debug_agent/persistence/checkpoints.py`: `context` checkpoint kind support.
- `src/debug_agent/persistence/artifacts.py`: artifact registration/staging support for large tool/model outputs, oversized skill/reference snapshots, and oversized context snapshots.
- `src/debug_agent/persistence/approval_grants.py`: session-local interactive approval records and reusable grant lookup.
- `src/debug_agent/persistence/skills.py`: frozen skill and reference snapshot storage.
- `src/debug_agent/persistence/context_snapshots.py`: post-optimization context snapshot storage and payload artifacting.
- `src/debug_agent/runtime/policy.py`: frozen path policy, shell policy, runtime-control target facts, and `PermissionEvaluator`.
- `src/debug_agent/runtime/query_control.py`: query state, continuation reasons, model-call group derivation, non-evictable suffix handling.
- `src/debug_agent/runtime/model_context.py`: `ModelContextFrame`, `CompressionContextFrame`, message segments, tool schema bindings, deterministic token estimator inputs.
- `src/debug_agent/runtime/context_manager.py`: large-output context representation hooks, old tool-result omission, rolling compression, context-limit failure decisions.
- `src/debug_agent/runtime/prompt_composer.py`: stable system block composition, available skill headers, active skill context injection, final frame construction.
- `src/debug_agent/runtime/prompt_executor.py`: Phase 1 query-control integration, approval-denial turn abort, compression/context-limit turn aborts, `AgentRunRequest(model_context_frame=...)`.
- `src/debug_agent/adapters/langchain_adapter.py`: provider materialization from `ModelContextFrame` and provider-native tool bindings from `tool_schema_bindings`.
- `src/debug_agent/tools/broker.py`: Phase 1 broker envelope, `ToolUseContext`, permission evaluation, approval dispatch, routing, artifacting, audit.
- `src/debug_agent/tools/native.py`: Phase 1 native tools: `read_file`, `list_dir`, `search_text`, `write_file`, `edit_file`.
- `src/debug_agent/tools/shell.py`: structured `shell_exec` with fake-runner test seam, timeout, stdout/stderr artifacting.
- `src/debug_agent/tools/runtime_control.py`: `activate_skill` and `load_skill_ref_file` handlers.
- `src/debug_agent/skills/registry.py`: prompt skill discovery, manifest validation, startup snapshotting, hash normalization, available skill headers.
- `src/debug_agent/cli/repl_controller.py`: `/skills`, `/tools`, `/compress`, inline approval state, idle-only `Ctrl+Y`, Phase 1 status bar updates.
- `src/debug_agent/cli/main.py`: one-shot and REPL startup `--approval-mode` option parsing and validation.
- `src/debug_agent/cli/repl_view.py`: Phase 1 snapshots/events for approval prompts, `/skills`, `/tools`, and context status.
- `src/debug_agent/cli/plain_repl_view.py`: plain approval prompt and non-interactive approval denial behavior.
- `src/debug_agent/cli/prompt_toolkit_view.py`: TUI inline approval prompt and exact Phase 1 status bar format.
- `src/debug_agent/observability/trace_writer.py`: Phase 1 skill, approval, policy, tool, context, and compression trace rendering.
- `src/debug_agent/observability/logging.py`: Phase 1 engine log entries for approval mode switches, decisions, policy denials, optimizations, and artifact registrations.
- `tests/unit/runtime/`: config, policy, query control, model context, context manager, prompt composer, prompt executor tests.
- `tests/unit/tools/`: broker, native, shell, runtime-control tool tests.
- `tests/unit/skills/`: skill registry, snapshot, hash, activation, and reference loading tests.
- `tests/unit/persistence/`: Phase 1 schema, user_version, approval grants, skill snapshots, context snapshots tests.
- `tests/unit/cli/`: `/skills`, `/tools`, `/compress`, approval prompt, `Ctrl+Y`, and status bar tests.
- `tests/unit/adapters/`: `ModelContextFrame` provider materialization tests.
- `tests/unit/observability/`: trace and engine-log rendering tests for Phase 1 events.
- `tests/integration/`: Phase 1 REPL, one-shot, legacy-schema fail-closed, tools, skills, compression, and approval behavior tests.

Module paths may be adjusted only to match established repository naming or nearby ownership patterns. Such changes must be recorded at the milestone checkpoint, and they must not change module responsibilities, stage boundaries, forbidden-feature boundaries, or dependency direction.

## Global Invariants

- `docs/project-contract.md`, `docs/phase-1/*`, and accepted `docs/adr/*` are the implementation truth. If they conflict, stop and patch documentation before implementation continues.
- Do not implement subagents, workflow execution, workflow skill activation, MCP, plugin packaging, hot reload, `/agents`, `/models`, `/compact`, `deactivate_skill`, section-level skill disclosure, semantic reference retrieval, token-level resume, tool-mid-flight resume, or unrestricted shell execution.
- All model-visible tools must pass through `ToolBroker`.
- Path policy, shell policy, approval mode, and reusable grants are runtime-enforced facts, not prompt instructions.
- `yolo` skips interactive approval only; it does not bypass schema validation, path deny, shell policy, timeout, artifact handling, or audit.
- Runtime-owned persistence and artifact operations may write under `.sessions/`; model-visible tools must not read, list, search, write, edit, or shell into `.sessions/`.
- Model-visible tools must not access `~/.debug-agent/skills/` or `<workspace_root>/.debug-agent/skills/`; prompt skill content is exposed only through frozen snapshots, `/skills`, active skill context, and `load_skill_ref_file`.
- Phase 1 does not read or migrate Phase 0/0.5 runtime databases. Existing legacy `.sessions/runtime.db` files fail closed before runtime truth rows are interpreted.
- `AgentStreamEvent` remains a UI observation and must not be persisted as `run_events`.
- `ModelContextFrame` is the ordinary task model-call context boundary. Token estimates, prompt composition, and adapter materialization use the same frame.
- Active `SKILL.md` content is runtime-supplied per model call and is not durable conversation history or compression input.
- Context summaries and context snapshots are continuity artifacts, not executable recovery truth.
- Keep one-shot, non-TTY, and injected-I/O paths plain and automation-friendly.
- Keep the repository runnable and testable after every milestone.

## Dependency Order

```text
startup config/policy validation + schema gate foundation
-> ToolBroker envelope and approval-provider seam
   -> native tools through ToolBroker
   -> shell_exec through ToolBroker
-> skill snapshot registry
ToolBroker envelope + skill snapshot registry
   -> runtime-control skill tools
      -> skill listing and skill observability
      -> ModelContextFrame foundation
         -> prompt composition and active skill injection
            -> adapter materialization, query state, and status context source
               -> query-control model-call groups
                  -> context omission
                     -> compression frame/parser and automatic compression
                        -> compression failure and manual /compress integration
compression/manual command surfaces + skill/tool surfaces + CLI approval-mode option
   -> REPL local commands, status surfaces, and approval UI
-> trace and acceptance sweep
```

This dependency order is mandatory, but it is a DAG, not a single serial feature chain. Startup config/policy validation and schema gating are first because tools, skills, approval, and context all depend on the same frozen permission boundary and because runtime truth rows must never be interpreted before the Phase 1 schema version gate. Native tools, shell execution, and skill snapshotting are separately reviewable branches after the shared foundation exists. Runtime-control skill tools are the first join point because they require both the broker envelope and the frozen skill registry. `ModelContextFrame` is separate because both skill injection and compression need the same runtime-owned model-call frame.

## Dependency Graph

```text
Frozen config + frozen policy facts + Phase 1 schema gate
  -> PermissionEvaluator + ApprovalGrantStore
    -> ToolBroker envelope
      -> native tools
      -> shell_exec

Frozen config + frozen policy facts + Phase 1 schema gate
  -> frozen skill registry

ToolBroker envelope + frozen skill registry
  -> runtime-control skill tools + active skill records
    -> skill listing + skill observability
    -> ModelContextFrame + TokenEstimator
      -> PromptComposer + active skill injection
        -> adapter materialization + query state + status context source
          -> QueryControlPlane model-call groups
            -> old tool-result omission
              -> compression frame/parser + automatic compression
                -> compression failure boundaries + manual /compress

Frozen config + frozen policy facts + Phase 1 schema gate
  -> REPL local commands + CLI approval-mode option + status bar
    -> approval UI + denial semantics

All completed branches
  -> trace + acceptance
```

Edges are implementation dependencies. A later node may add tests that exercise earlier nodes, but it must not require unfinished later behavior for the repository to compile, run tests, start the main flow, or keep one-shot/plain/TUI paths usable.

## Execution Stages

Each milestone is an incrementally safe stage. After every stage:

- The repository must compile/import and the relevant canonical tests must run.
- The main CLI/REPL flow must still start or fail closed according to the current milestone's documented runnable state.
- Unfinished later-phase behavior must be disabled, hidden, or represented by explicit unsupported-command behavior rather than half-wired runtime paths.
- Review should stop at the stage checkpoint before depending on the next stage.

### Dual-Path Development Gate

Milestones 1 through 2B introduce the Phase 1 schema, frozen policy facts, broker envelope, native tools, and shell execution before the Phase 1 minimum runnable slice is complete. During those milestones, Phase 1-only startup and model-visible tool paths must remain behind an internal development/test gate or fake harness. The default user-facing one-shot, plain REPL, and TUI entrypoints must either keep the last completed runnable path intact or fail closed before accepting a user prompt with a clear milestone-gated error. They must not enter a partially wired Phase 1 prompt loop.

This is an implementation-transition rule only. It must not create a shipped user-facing compatibility mode, schema migration path, or Phase 0/0.5 legacy runtime-truth reader. Once Milestone 3A completes startup-blocking skill snapshot persistence and available skill headers, the gated Phase 1 startup path becomes the main path and the temporary development gate is removed.

After Milestone 3A, new Phase 1 user-facing behavior may still remain hidden behind narrower internal gates when exposing it would violate an unfinished downstream contract. In particular, brokered runtime-control skill tools must not be exposed to real provider/model loops until active skill prompt injection uses `ModelContextFrame` as the actual adapter boundary in Milestone 4C, and automatic compression must not run on the real pre-call path until Milestone 5B implements its failure transaction boundaries.

## Verification Strategy

- Use only Phase 1 canonical commands from `docs/phase-1/operations.md`: `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, `uv run pytest -v`, and `uv lock` when dependency declarations change.
- Prefer the narrowest canonical command that meaningfully validates the changed layer. Milestones that add only unit coverage use `uv run pytest tests/unit -v`; milestones that add or change integration behavior also run `uv run pytest tests/integration -v`.
- Each milestone's tests must keep previously completed milestone behavior passing. A stage is not complete if it breaks an earlier runnable state.
- If a milestone changes dependency declarations, run `uv lock` in that same milestone before its freeze/review checkpoint. Do not defer lockfile reconciliation to the final acceptance sweep.
- Fake model tests are the default for model-facing behavior. They must cover deterministic assistant text, deterministic tool calls, deterministic compression output, malformed compression output, forced compression/model failure, timeout behavior, provider usage present/absent, and multi-call tool loops where needed.
- Fake shell runners are the default for shell-policy, timeout, stdout/stderr artifacting, Windows suffix normalization, and transparent-wrapper behavior. Tests must not require network access.
- Manual TTY verification is reserved for Milestone 6B and 6C behavior that cannot be reliably asserted through automated tests. Manual records must include terminal application, command sequence, expected result, observed result, and known limitation.
- Phase 1 acceptance requires `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, and `uv run pytest -v`, plus the required manual checks.

## Migration / Rollback Strategy

- Phase 1 is intentionally incompatible with Phase 0 and Phase 0.5 runtime databases.
- If `.sessions/runtime.db` is absent, Phase 1 creates it with the Phase 1 schema and writes `PHASE_1_SCHEMA_USER_VERSION = 1` before interpreting runtime rows.
- If `.sessions/runtime.db` exists, startup, `debug-agent status`, and `debug-agent trace` must read `PRAGMA user_version` before active ownership, session, run, status, trace, or other runtime truth rows are interpreted.
- Missing (`0`), unknown, Phase 0, and Phase 0.5 schema versions fail closed with `error_class="config_error"`. Runtime must not migrate, delete, rewrite, or reinterpret the legacy database.
- The legacy-schema user guidance must state that Phase 0/0.5 runtime databases are unsupported by Phase 1 and instruct the user to move or remove `.sessions/` or use a fresh workspace.
- Rollback is feature-disable-first: each milestone must be reversible by disabling the newly exposed Phase 1 path while leaving earlier completed milestones runnable and testable.
- Context snapshots and compression summaries are rollback-safe continuity artifacts only. They are not executable recovery truth and must not become a migration source.

## Milestone 1: Phase 1 Schema, Config, And Policy Foundation

Objective: establish the Phase 1 persistence/config/policy base before any model-visible Phase 1 tool or skill can execute.

Deliverables: Phase 1 schema/user-version gate, frozen context/execution settings, frozen main-agent path/shell policy facts, deterministic permission evaluation, deterministic approval scope signatures, and approval grant storage.

- [x] Follow the Phase 1 initialization order from `docs/phase-1/architecture.md`: resolve workspace, load global config, load main-agent config, validate/freeze config and policy facts, then perform schema bootstrap/version gating before interpreting runtime truth rows.
- [x] Add Phase 1 SQLite schema bootstrap with `PHASE_1_SCHEMA_USER_VERSION = 1`.
- [x] Ensure startup, `debug-agent status`, and `debug-agent trace` read `PRAGMA user_version` before interpreting runtime truth tables.
- [x] Fail closed with `error_class="config_error"` for missing (`0`), unknown, Phase 0, or Phase 0.5 schema versions.
- [x] Add schema support for `approval_grants`, `skill_snapshots`, `skill_reference_snapshots`, `context_snapshots`, and `runs.context_snapshot_id`, preserving the minimum table shapes and constraints from the Phase 1 specs.
- [x] Ensure `context_snapshots` schema preserves the minimum Phase 1 fields, allowed trigger values `manual`, `omission`, `compression`, and `omission | compression`, and the `payload_artifact_id` reference used when serialized snapshot payloads exceed 16 KiB.
- [x] Ensure `skill_snapshots` enforces one frozen `SKILL.md` body per `(session_id, run_id, skill_name)` and `skill_reference_snapshots` enforces one reference row per `(skill_snapshot_id, reference_path)` with a foreign key to the owning skill snapshot.
- [x] Ensure `approval_grants` stores only allowed `decision` values `approved_once`, `approved_for_session`, and `denied`, only allowed `grant_scope` values `once`, `session`, and `none`, the rendered `approval_request` text, and uses only `approved_for_session` rows as reusable grants.
- [x] Parse `[context]` defaults from `~/.debug-agent/config.toml`: `window_tokens=200000`, `omit_old_tool_results_at_ratio=0.60`, `compress_history_at_ratio=0.80`, `retain_recent_model_calls=4`, and `compression_reserved_output_tokens=10000`.
- [x] Parse `[execution].default_shell_timeout_seconds` with default `300`.
- [x] Reject invalid context or execution settings with `config_error` before session/run creation.
- [x] Load main-agent policy declarations from `~/.debug-agent/agent.toml`.
- [x] Treat absent `~/.debug-agent/agent.toml`, absent path policy, and absent shell policy as documented Phase 1 defaults: workspace root remains trusted, user shell `allow` and `deny` are empty, and builtin path/shell denies still apply.
- [x] Parse and freeze path policy facts with scopes `trust` and `deny`.
- [x] Parse and freeze shell policy facts with argv-prefix `allow` and `deny`; reject regex policy shapes.
- [x] Add builtin path denies for `.git/`, `node_modules/`, `build/`, `dist/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.sessions/`, `~/.debug-agent/skills/`, and `<workspace_root>/.debug-agent/skills/`.
- [x] Add builtin shell denies for privilege escalation, destructive recursive `rm`, and raw shell trampoline forms.
- [x] Implement path canonicalization for existing paths, missing targets, symlink escape checks, exact file policy entries, and subtree policy entries.
- [x] Implement shell executable normalization, Windows executable suffix normalization, and transparent `env` wrapper unwrapping.
- [x] Implement argv path classification for documented path-like tokens and only the Phase 1 runtime-owned path option list in `docs/phase-1/specs/approval.md`.
- [x] Implement `PermissionEvaluator` with the fixed decision order from `docs/phase-1/specs/approval.md`.
- [x] Implement approval-mode matrix for `normal`, `semi-auto`, and `yolo`.
- [x] Implement deterministic approval scope signatures for file tools, `shell_exec`, `activate_skill`, and `load_skill_ref_file`.
- [x] Ensure file-tool approval scope signatures use the exact canonical path plus access type, and `write_file`/`edit_file` signatures do not widen from file path to directory path.
- [x] Ensure `load_skill_ref_file` signature facts are audit/scope facts only; valid active reference loads remain audit-only and invalid loads are denied before approval.
- [x] Implement `ApprovalGrantStore` over `approval_grants`, with reusable grants only for `approved_for_session`.
- [x] Add unit tests for schema creation, schema version gate, legacy fail-closed behavior, config defaults, invalid settings, absent policy defaults, policy parsing, path classification, shell matching, mode matrix, approval grant lookup, and `context_snapshots` table shape/trigger constraints.
- [x] Verify with canonical command `uv run pytest tests/unit -v`.

Modified boundaries: persistence bootstrap, runtime config, policy facts, permission evaluation, and approval grant storage.

Invariants: no model-visible tool may bypass this policy foundation; legacy runtime truth rows are never interpreted before schema-version validation; invalid context, execution, or main-agent policy settings fail before session/run creation.

Freeze/review checkpoint: a fake normalized tool-call fact can be authorized or denied deterministically without invoking any real tool handler.

Rollback: revert or disable this milestone's Phase 1 startup/schema changes on the development branch before later Phase 1 code depends on them. Runtime must still fail closed for legacy databases; rollback must not introduce Phase 0/0.5 compatibility reads, migration, deletion, rewrite, or active-ownership interpretation.

Runnable state: Phase 1 can initialize a fresh database, reject legacy databases, freeze config/policy facts, and evaluate permission decisions in unit tests. The default user-facing one-shot/plain REPL/TUI path must remain runnable through the last completed stable path or fail closed before accepting a prompt; Phase 1-only startup and prompt paths remain internal-gated until Milestone 3A.

## Milestone 2A: Native Tools Through The Phase 1 Broker

Objective: expose Phase 1 native filesystem tools only through the brokered tool-control plane.

Deliverables: Phase 1 `ToolDefinition`, `ToolUseContext`, `ToolRouter`, broker invocation envelope, native read/list/search/write/edit handlers, removal of model-visible `git_status`, native tool audit and artifact behavior.

- [x] Replace the Phase 0 direct read-only broker shape with the Phase 1 broker envelope: schema validation, target normalization, permission evaluation, approval dispatch, routing, artifact handling, `ToolResult` normalization, and audit event writing.
- [x] Keep the broker envelope migration reviewable as two internal checkpoints: first introduce `ToolDefinition`, `ToolUseContext`, `ToolRouter`, permission result types, approval-provider seam, and audit/result normalization without enabling new handlers; then enable the native handlers behind that envelope.
- [x] Define Phase 1 `ToolDefinition` with `name`, `description`, `input_schema`, `category`, `risk_level`, and `access`.
- [x] Define `ToolUseContext` with session/run ids, workspace root, artifact root, approval mode, frozen config, frozen policy, approval grants, approval provider, event writer, artifact store, and skill snapshot store.
- [x] Define only the abstract approval-provider interface and fake/non-interactive test provider needed by `ToolBroker`; concrete TUI and plain interactive approval providers are implemented later in Milestone 6B.
- [x] Add `ToolRouter` with Phase 1 categories `native`, `shell`, and `runtime_control`, while only native handlers are enabled in this sub-milestone.
- [x] Expose native tool definitions for `read_file`, `list_dir`, `search_text`, `write_file`, and `edit_file`.
- [x] Remove model-visible `git_status` from the Phase 1 tool list.
- [x] Implement `read_file` for UTF-8 text files with optional positive line limit and output artifacting for large content.
- [x] Implement `list_dir` for immediate directory entries with optional positive entry limit.
- [x] Implement literal, case-sensitive, line-oriented `search_text` with optional positive match limit and UTF-8-only file scanning.
- [x] Keep native tool limit defaults and caps as builtin frozen runtime facts unless a later approved contract adds new user-facing configuration for them.
- [x] Implement `write_file` for complete UTF-8 content and missing parent directory creation under authorized write scope.
- [x] Implement `edit_file` as first exact occurrence replacement on normalized LF view while preserving dominant line endings.
- [x] Ensure all native tool handlers execute only after `ToolBroker` permission approval and do not write audit events directly.
- [x] Ensure tool handlers do not read mutable global policy directly, ask users for approval directly, or widen model-visible schemas.
- [x] Add unit tests for tool schemas rejecting unknown fields.
- [x] Add unit tests for native tool required schema fields, positive integer `limit` validation, and `additionalProperties=false` on every native model-visible tool schema enabled in this milestone.
- [x] Add unit tests for `edit_file` absent `old_text` returning `ToolResult(status="error", error_class="tool_error")`, first-occurrence replacement only, normalized-LF matching, dominant line-ending preservation, and LF fallback when no dominant line ending exists.
- [x] Add unit tests for native read auto-allow in trusted workspace under `normal`.
- [x] Add unit tests for read outside trusted workspace requiring approval in `normal`.
- [x] Add unit tests for write requiring approval in `normal`, auto-allow in trusted workspace under `semi-auto`, and untrusted write requiring approval under `semi-auto`.
- [x] Add unit tests for builtin path denies, user path denies, symlink escape denies, missing target canonicalization, and skill-source deny rules.
- [x] Add unit tests proving `search_text` traversal deterministically skips or denies builtin-denied and user-denied directories instead of reading through them.
- [x] Add unit tests proving model-visible tools cannot access `.sessions/` or skill source roots.
- [x] Add unit tests proving artifact ids or runtime references cannot be used to bypass `.sessions/` builtin deny.
- [x] Add unit tests proving native tools do not write audit events directly; broker writes the audit events.
- [x] Verify with canonical command `uv run pytest tests/unit -v`.

Modified boundaries: `ToolBroker`, native tool implementations, native tool audit events, and model-visible tool list.

Invariants: `git_status` is gone for Phase 1; all native access is brokered and policy-checked; native handlers remain policy-free execution units behind `ToolBroker`; `ToolUseContext` is a per-call execution context and must not become persisted runtime truth.

Freeze/review checkpoint: native filesystem tools can be tested end-to-end with fake approval providers before shell execution exists.

Rollback: disable or revert this milestone's Phase 1 native tool definitions on the development branch while leaving the Phase 1 schema/policy foundation intact. Do not restore a legacy Phase 0/0.5 runtime compatibility path or weaken the Phase 1 schema fail-closed behavior.

Runnable state: model-visible native read/write/search/edit/list behavior is available through `ToolBroker` with path policy, approval mode, artifacting, and audit in unit/fake harnesses. The default user-facing one-shot/plain REPL/TUI path must remain runnable through the last completed stable path or fail closed before accepting a prompt; Phase 1-only tool exposure remains internal-gated until Milestone 3A persists frozen skill snapshots and available skill headers.

## Milestone 2B: Structured Shell Execution

Objective: add structured shell execution without weakening the broker, policy, timeout, artifact, or audit boundary.

Deliverables: `shell_exec` definition, structured argv execution path, shell policy matching, argv path classification, fake shell runner seam, timeout behavior, stdout/stderr artifacting, and git-denial integration coverage.

- [x] Add `shell_exec` tool definition with structured `argv`, optional `cwd`, and optional positive `timeout_seconds`.
- [x] Add unit tests for `shell_exec` required schema fields, non-empty `argv`, positive integer `timeout_seconds` validation, raw shell string rejection, and `additionalProperties=false`.
- [x] Ensure raw shell strings and unrestricted `shell=True` are not accepted.
- [x] Resolve default `cwd` to `workspace_root`; resolve provided `cwd` against `workspace_root`.
- [x] Apply shell policy allow/deny gates before approval.
- [x] Apply path policy to `cwd`, path-qualified `argv[0]`, bare syntactically path-like argv tokens, and runtime-classified argv option/value path tokens.
- [x] Limit argv path classification to the documented Phase 1 runtime-owned path option list and do not add ad-hoc implicit path options: `--output`, `--out`, `--config`, `--file`, `--path`, `--cwd`, `--directory`, `--root`, `--input`, `--src`, `--source`, `--dest`, `--destination`, `-o`, `-c`, `-f`, `-C`, and `-I`.
- [x] Aggregate path classification conservatively: the shell call is trusted only when all participating paths are trusted.
- [x] Compute effective timeout as `min(requested_timeout_seconds, frozen default_shell_timeout_seconds)` when requested, or the frozen default when omitted; include the effective timeout in approval scope signatures.
- [x] Add a fake shell runner test seam so unit and integration tests remain network-free and platform-independent.
- [x] Capture stdout and stderr separately, normalize them into `ToolResult`, and artifact large stdout/stderr.
- [x] Return `ToolResult(status="timeout")` on timeout and write the required audit facts.
- [x] Add unit tests for shell allow prefix, user deny prefix, builtin deny, non-empty allowlist miss, and empty allow default.
- [x] Add unit tests for path-qualified executable normalization, Windows executable suffix normalization, Windows shell wrapper behavior through a fake runner when real OS coverage is unavailable, and transparent `env` wrapper denial for nested denied commands.
- [x] Add unit tests documenting opaque wrapper behavior for `npm run`, `make`, `uv run`, interpreter script execution, and arbitrary local scripts.
- [x] Add unit tests proving bare syntactically path-like argv tokens such as `src/file.py`, `../repo/file.py`, `/tmp/file`, and Windows drive/UNC paths are classified and checked by path policy.
- [x] Add unit tests for shell cwd blacklist denial, path-qualified `argv[0]` blacklist denial, classified argv path blacklist denial, untrusted argv path approval behavior, timeout, and stdout/stderr artifacting.
- [x] Add integration tests proving denying `["git"]` blocks direct git, path-qualified git, Windows suffix git, and transparent-wrapper git.
- [x] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: shell tool handler, broker normalization, shell policy matching, fake shell runner tests, and shell audit events.

Invariants: shell execution is high risk and must satisfy shell policy, path policy, approval/risk policy, timeout, artifacting, and audit. Empty shell allowlists are an accepted local automation risk, not a sandbox guarantee; opaque wrappers are not semantically inspected; generic shell path policy remains best-effort until a real filesystem sandbox or specialized wrapper exists.

Freeze/review checkpoint: shell execution works through a fake runner and cannot bypass `ToolBroker` or policy gates.

Rollback: remove `shell_exec` from model-visible tool definitions while leaving native tools and policy foundation intact.

Runnable state: shell policy, path policy, approval behavior, and timeout are testable through fake runners and broker harnesses without enabling skills or compression. The default user-facing one-shot/plain REPL/TUI path must remain runnable through the last completed stable path or fail closed before accepting a prompt; Phase 1-only shell exposure remains internal-gated until Milestone 3A persists frozen skill snapshots and available skill headers.

## Milestone 3A: Prompt Skill Registry And Startup Snapshot Gate

Objective: freeze prompt skills at startup before any user prompt or runtime-control skill tool can depend on skill data.

Deliverables: deterministic skill discovery, manifest validation, frozen skill/reference snapshots, startup ordering, available skill headers, and startup-failure transaction behavior.

- [x] Implement `SkillRegistry` discovery from exactly `~/.debug-agent/skills` and `<workspace_root>/.debug-agent/skills`.
- [x] Ensure CLI explicit skill paths and builtin skill roots are not discovered in Phase 1.
- [x] Scan direct child directories only; do not treat the root itself as a skill directory; ignore nested `SKILL.md`; do not follow symlinked skill directories.
- [x] Process skill directories and reference paths in normalized path order.
- [x] Parse required UTF-8 `SKILL.md` with YAML front matter and Markdown body.
- [x] Validate manifest fields: `name`, `description`, optional `execution_mode`, `triggers`, and `metadata`; reject unknown fields and invalid field types.
- [x] Enforce skill names matching `[A-Za-z0-9_.-]+` with maximum length 128.
- [x] Treat absent `execution_mode` as `prompt`; reject non-`prompt` execution modes with startup `config_error`.
- [x] Implement project-over-global whole-skill override and duplicate-name rejection within the same scope.
- [x] Snapshot `SKILL.md` and file-level `references/**`; ignore files outside those paths, do not copy them to artifacts, and do not include them in skill hashes.
- [x] Classify references as UTF-8 text or non-text; artifact large text and always artifact all non-text reference payloads.
- [x] Artifact oversized serialized skill snapshot payloads through `skill_snapshots.payload_artifact_id` while preserving inline minimum snapshot facts required for lookup and trace.
- [x] Fail startup with `config_error` for unreadable reference files under `references/**`.
- [x] Compute deterministic SHA-256 hashes for manifest facts, normalized `SKILL.md`, reference paths, reference metadata, and reference content.
- [x] Persist `skill_snapshots` and `skill_reference_snapshots` after session/run/artifact root creation and before accepting any user prompt.
- [x] Wire the startup coordinator so Phase 1 validates config/policy, performs schema-version gating, checks active ownership, creates session/run/artifact state, persists frozen skill snapshots and available skill headers, initializes broker/context/query-control services, and only then accepts the first one-shot or REPL prompt.
- [x] Implement available skill header generation from the frozen registry snapshot.
- [x] Ensure available skill headers contain activation candidates only and do not include full skill bodies or reference file contents.
- [x] Implement startup `config_error` transaction behavior after session/run creation but before first prompt: write best available failure event and `error` checkpoint, mark partially initialized run/session `failed`, and release active ownership.
- [x] Add unit tests for discovery roots, precedence, duplicates, invalid manifests, hash stability, startup blocking, source file mutation after startup, unreadable reference startup failure, reference artifacting, oversized skill snapshot payload artifacting, and startup failure transactions.
- [x] Verify with canonical command `uv run pytest tests/unit -v`.

Modified boundaries: startup orchestration, skill discovery/snapshotting, skill snapshot persistence, and available skill header generation.

Invariants: skill source files are startup inputs only; frozen snapshots are execution truth for the active session; one-shot and REPL input must not accept the first prompt until the frozen skill snapshot is persisted.

Freeze/review checkpoint: a fresh session can persist a deterministic frozen skill registry snapshot and available skill headers before model execution begins.

Rollback: disable skill discovery/snapshotting startup work while leaving brokered native/shell tools intact.

Runnable state: users can start a session with prompt skills snapshotted or receive a startup `config_error` before first prompt acceptance.

## Milestone 3B: Runtime-Control Skill Tools And Active Skill State

Objective: implement skill activation/reference loading only through `ToolBroker` using the frozen registry snapshot from Milestone 3A, while keeping the real model-visible skill-tool path gated until active skill prompt injection is executable.

Deliverables: structured run-scoped active skill records, brokered `activate_skill`, brokered `load_skill_ref_file`, frozen-target validation, skill tool audit facts, and an explicit internal gate that prevents half-functional skill activation from reaching real provider/model loops before Milestone 4C.

- [x] Implement structured run-scoped active skill records with name, content hash, activation reason, and scope.
- [x] Add `activate_skill` runtime-control tool through `ToolBroker`.
- [x] Ensure `activate_skill` requires interactive approval in `normal`, is audit-only in `semi-auto` and `yolo`, and returns a short activation result without the skill body.
- [x] Ensure unknown skills and missing, corrupt, or hash-mismatched frozen skill snapshots return `ToolResult(status="denied", error_class="config_error")` without prompting for approval.
- [x] Ensure repeated activation is idempotent and does not duplicate active skill records.
- [x] Add `load_skill_ref_file` runtime-control tool through `ToolBroker`.
- [x] Ensure valid active reference loads are audit-only in every approval mode; invalid, inactive, missing, corrupt, or hash-mismatched targets are denied before approval.
- [x] Ensure `load_skill_ref_file.path` is a skill-local relative path and path traversal, absolute paths, and paths outside the frozen reference set are denied before approval.
- [x] Ensure missing, corrupt, or hash-mismatched frozen reference snapshots return `ToolResult(status="denied", error_class="config_error")`.
- [x] Ensure `load_skill_ref_file` reads only frozen reference snapshots, never live source files.
- [x] Ensure text references below the inline threshold return text plus metadata, while large text and all non-text references return controlled artifact/reference markers plus metadata.
- [x] Keep `activate_skill` and `load_skill_ref_file` exposed only in fake approval/model harnesses until Milestone 4C proves active `SKILL.md` content is visible on the next real model call through `ModelContextFrame`.
- [x] Ensure default user-facing provider/model loops cannot activate a skill without the next ordinary model call receiving runtime-supplied active skill context.
- [x] Add unit tests for `activate_skill` and `load_skill_ref_file` required schema fields and `additionalProperties=false`.
- [x] Add unit tests for activation, idempotent activation, source file mutation after startup, reference loading, relative-path validation, path traversal and absolute-path denial, reference artifacting, and reference load denial cases.
- [x] Add integration tests for brokered skill activation and brokered `semi-auto` runtime-control auto-allow through a frozen-config/fake-model harness, and assert that the real provider/model tool surface remains gated until Milestone 4C.
- [x] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: runtime-control tool handlers, run active skill state, broker runtime-control target validation, and skill tool audit facts.

Invariants: skill runtime-control tools never read live source files and never bypass `ToolBroker`, approval mode, policy denial, or audit.

Freeze/review checkpoint: skills can be activated and frozen reference files can be loaded through fake approval/model paths, while the real provider/model loop cannot expose these tools until active skill prompt injection is connected.

Rollback: remove skill runtime-control tool definitions while leaving frozen skill snapshots and brokered native/shell tools intact.

Runnable state: users can start a session and inspect frozen skills, while brokered skill activation/reference loading is testable only in fake approval/model paths. The real provider/model loop must not expose skill activation until Milestone 4C makes active skill instructions visible on the next model call.

## Milestone 3C: Skill Listing And Skill Observability

Objective: expose frozen skill state to users and traces without adding new runtime skill semantics.

Deliverables: local `/skills`, skill snapshot/activation/reference trace rendering, and skill engine-log facts.

- [x] Implement `/skills` as a local REPL command from the frozen session snapshot.
- [x] Ensure `/skills` lists skill name, description, source scope, and active status for the current run, never reads live skill source files after startup, and renders each skill as a blank-line-prefixed two-line entry:
  first line `- <skill-name> (<global|project>) [<inactive|active>]`,
  second line `<description>`.
- [x] Add trace and engine-log facts for skill snapshotting, activation, and reference loading.
- [x] Add unit tests for `/skills` rendering from frozen snapshots and active skill state.
- [x] Add trace/log tests for skill snapshotting, activation, and reference loading facts.
- [x] Verify with canonical command `uv run pytest tests/unit -v`.

Modified boundaries: REPL local command routing for `/skills`, trace writer, and engine log rendering for skill facts.

Invariants: `/skills` is local and never sent to the model; observability reads frozen runtime records, not live skill source files.

Freeze/review checkpoint: frozen skills and active skill records are inspectable without active skill prompt injection or compression.

Rollback: disable `/skills` and skill trace/log rendering while keeping frozen snapshots and runtime-control skill tools intact.

Runnable state: users can inspect frozen prompt skills and active status from the REPL before prompt injection exists.

## Milestone 4A: ModelContextFrame Foundation And Token Estimation

Objective: introduce the runtime-owned model-call frame and deterministic estimator before prompt composition or adapter behavior depends on them.

Deliverables: `ConversationMessage`, `ModelContextFrame`, `CompressionContextFrame` type shape, deterministic token estimator, estimator metadata, and frame-only unit coverage.

- [x] Define `ConversationMessage` with `seq`, `role`, `kind`, `turn_id`, `model_call_id`, `tool_call_id`, `content`, `artifact_refs`, `estimated_tokens`, and `metadata`.
- [x] Define `ModelContextFrame` with `message_segments` and `tool_schema_bindings`.
- [x] Define `CompressionContextFrame` for later compression calls, without enabling compression behavior yet.
- [x] Implement deterministic `TokenEstimator` for local pre-call estimates, including structural overhead for messages and tool schema bindings.
- [x] Record estimator version and input-shape metadata in context estimates so later events and context snapshots can explain which deterministic estimator produced each estimate.
- [x] Add unit tests for frame serialization, segment ordering primitives, tool schema binding estimate inclusion, deterministic estimate stability, estimator version metadata, and raw conversation not being used directly for budget decisions.
- [x] Verify with canonical command `uv run pytest tests/unit -v`.

Modified boundaries: model context types and deterministic token estimation.

Invariants: `ModelContextFrame` is an in-memory/request-frame boundary, not durable runtime truth; `CompressionContextFrame` exists only as a type shape and does not enable compression behavior.

Freeze/review checkpoint: frame construction and deterministic estimates can be tested without changing adapter calls or prompt composition.

Rollback: remove the frame/estimator foundation before downstream prompt composition depends on it.

Runnable state: current prompt execution remains on the previous working path while frame and estimator behavior is available in unit tests.

## Milestone 4B: Prompt Composition And Active Skill Injection

Objective: use the frame foundation to build ordinary task prompt frames, including active skill context, without changing provider adapter materialization yet.

Deliverables: `PromptComposer`, available skill header composition, active skill context frame segments, stable system block handling, and prompt-frame tests.

- [x] Implement `PromptComposer` for stable system content, available skill headers, active skill context, summary, retained raw messages, live/unconsumed messages, current user input/tool-loop messages, and tool schema bindings.
- [x] Inject active `SKILL.md` bodies as non-persistent `ModelContextFrame` segments with `role="system"` and `kind="runtime_active_skill_context"`.
- [x] Ensure active skill context segments are runtime-authored with `source="runtime"`, `persistent=false`, `compressible=false`, and start with `[Runtime supplied active skill context]` plus `This block is authoritative for this turn.`
- [x] Ensure each active skill context entry includes skill id, content hash or version, activation reason, scope, instructions, and available reference file paths and hashes.
- [x] Ensure any model-visible `allowed_tools` or `path_policy` guidance inside active skill context is non-authorizing; runtime and `ToolBroker` remain the only authorization source.
- [x] Ensure active skill context is not appended to `ReplRuntime.conversation`.
- [x] Ensure active skill content is not disclosure-degraded under budget pressure.
- [x] Ensure loaded skill reference outputs remain ordinary durable conversation tool observations.
- [x] Ensure prompt composition estimates use the composed `ModelContextFrame`, not raw `ReplRuntime.conversation`.
- [x] Add unit tests for frame ordering, active skill segment shape, available skill headers, active skill metadata, active skill context not being durable conversation, loaded reference outputs remaining ordinary conversation observations, and prompt-frame estimate consistency.
- [x] Verify with canonical command `uv run pytest tests/unit -v`.

Modified boundaries: prompt composition, active skill injection, available skill headers, and model-call frame construction.

Invariants: runtime owns prompt injection policy; active skill instructions are reconstructed from frozen snapshots and structured active skill records, never from compression summaries or live source files.

Freeze/review checkpoint: prompt frames include active skill instructions in the documented order without changing provider adapter calls.

Rollback: disable the Phase 1 prompt composer and keep active skill records/runtime-control tools available without injecting active skill instructions.

Runnable state: users can activate skills, and composed frames can include active skill instructions in tests; provider calls are not switched to the new envelope until Milestone 4C.

## Milestone 4C: Adapter Materialization, Query State, And Status Context Source

Objective: make `ModelContextFrame` the actual ordinary task model-call boundary used by the adapter and status display before enabling omission or compression.

Deliverables: Phase 1 `AgentRunRequest`, adapter materialization from `ModelContextFrame`, query state, continuation reasons, status-bar context data source, and active skill injection integration.

- [x] Update `AgentRunRequest` so `model_context_frame` is the complete context truth for Phase 1.
- [x] Ensure Phase 0/0.5 `system_prompt`, `conversation`, `user_input`, and `tools` fields are not independent prompt/context truth.
- [x] Update adapter materialization to generate provider-legal messages from `ModelContextFrame.message_segments`.
- [x] Update adapter materialization to bind provider-native tools from `ModelContextFrame.tool_schema_bindings`.
- [x] Ensure `AgentRunRequest.model_context_frame` is the same frame used for token estimation and context decisions.
- [x] Implement query state with query id, turn id, continuation reason, active skill records, latest context estimate, and current approval mode.
- [x] Add continuation reasons for `initial_model_call`, `tool_result_continuation`, `post_compression_continuation`, `approval_denied_abort`, `compression_failed_abort`, `context_limit_abort`, and `final_assistant_response`.
- [x] Update status bar data source so context window usage is based on `ModelContextFrame` estimates.
- [x] Add unit tests for tool schema bindings, adapter materialization, `AgentRunRequest.model_context_frame` identity, estimate consistency across prompt composer and adapter request, query state, continuation reasons, and status data source.
- [x] Add integration tests proving active `SKILL.md` content appears on the next model call after activation and is not appended to durable conversation.
- [x] Remove the runtime-control skill-tool gate introduced in Milestone 3B only after the same integration path proves skill activation, next-call active skill injection, and adapter materialization share one `ModelContextFrame`.
- [x] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: prompt composition, model-call request envelope, adapter materialization, token estimation, query state.

Invariants: runtime owns prompt injection policy; the adapter must not reconstruct prompt context from legacy fields or mutate frame ordering; `AgentStreamEvent` remains UI observation and must not become persisted runtime truth during adapter changes.

Freeze/review checkpoint: ordinary model calls, tool-loop follow-up calls, active skill injection, and status-bar context estimates share one `ModelContextFrame`.

Rollback: disable Phase 1 prompt composition path and revert to the previous adapter request envelope before enabling compression.

Runnable state: users can activate skills and observe active skill instructions in subsequent model calls, with context estimates based on the actual frame sent to the provider.

## Milestone 5A: Model Call Groups And Old Tool-Result Omission

Objective: derive deterministic model-call groups and reduce old tool-result bulk before any model-based compression exists.

Deliverables: model-call group derivation, non-evictable raw suffix, old tool-result omission markers, omission context snapshots, `runs.context_snapshot_id` updates, `context` checkpoints, and omission status display.

- [x] Implement `QueryControlPlane` model-call group derivation from durable conversation metadata.
- [x] Mark groups `open` while streaming output, pending tool calls, or missing terminal tool results exist.
- [x] Mark closed groups `consumed_by_later_model_call` only after a later ordinary task model call includes their raw messages.
- [x] Compute the non-evictable raw suffix from newest `retain_recent_model_calls` completed groups, open groups, unconsumed groups, current user input, and current tool-loop buffers.
- [x] Ensure large tool/model outputs continue to be artifact-backed at record time, not as a later pre-call optimization.
- [x] Implement old tool-result omission when the candidate `ModelContextFrame` is strictly greater than `omit_old_tool_results_at_ratio * window_tokens`.
- [x] Replace eligible older tool result bodies with the exact omission marker `[Earlier tool result omitted for brevity. See artifact references or trace for full details.]` while preserving metadata and artifact ids.
- [x] Persist omission context snapshots with trigger `omission`, empty summary, retained messages, omission count, artifact refs, token estimates, and active skill records.
- [x] Ensure omission context snapshots use the Phase 1 minimum `context_snapshots` shape, allowed trigger value `omission`, inline payload threshold, and `payload_artifact_id` artifacting rules.
- [x] Update `runs.context_snapshot_id` and write a `context` checkpoint after omission snapshots.
- [x] Display a REPL system message with reduced-from and reduced-to estimates after omission.
- [x] Rebuild and re-estimate the candidate `ModelContextFrame` after omission before any compression decision.
- [x] Add unit tests for group derivation, non-evictable suffix, live/unconsumed protection, omission eligibility, exact omission marker text, snapshot shape, checkpoint shape, and status bar update.
- [x] Add integration test proving full omitted tool output remains recoverable through events or artifacts.
- [x] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: query control plane, durable conversation metadata, context manager omission, context snapshots, context checkpoints, status display.

Invariants: omission mutates durable LLM-visible working history but not audit truth, run events, or artifacts; context snapshots are continuity/audit inspection records and not executable recovery sources.

Freeze/review checkpoint: old tool-result omission can be triggered and inspected before any model-based compression is enabled.

Rollback: disable omission threshold handling and keep `ModelContextFrame` hard-limit checks only.

Runnable state: long tool-heavy conversations can reduce old tool-result bulk without a compression model call.

## Milestone 5B: Compression Frame, Parser, Automatic Compression, And Failure Boundaries

Objective: add automatic rolling compression on top of `ModelContextFrame` and old tool-result omission with its minimum failure transaction boundaries before wiring the manual command.

Deliverables: compression frame construction, compression prompt/schema, compression parser, automatic compression trigger/batch selection, compression model-call audit, compression failure event/checkpoint handling for automatic pre-call compression, compression context snapshots, and context checkpoints.

- [x] Implement `CompressionContextFrame` construction from previous summary, bounded evicted model-call groups, and compression instruction/schema prompt.
- [x] Exclude main agent system prompt, available skill headers, tool schema bindings, active `SKILL.md` bodies, retained recent raw messages, live/unconsumed suffix, and runtime-owned active skill/artifact/policy/approval facts from compression calls.
- [x] Keep Milestone 5B reviewable through internal checkpoints: first land compression frame construction and parser tests without automatic replacement, then land automatic trigger/batch selection behind a fake pre-call harness, then land automatic failure boundaries, then land snapshot/checkpoint persistence and enable automatic compression on the real pre-call path.
- [x] Internal checkpoint 1 runnable state: compression frame construction and parser behavior are unit-test-only, with no ordinary runtime behavior change and no automatic replacement path enabled.
- [x] Internal checkpoint 2 runnable state: automatic trigger and batch selection are exercised only through a fake pre-call harness; the real pre-call path still does not run automatic compression.
- [x] Internal checkpoint 3 runnable state: automatic compression failure abort boundaries are implemented and testable, while automatic compression success replacement remains gated from the real pre-call path.
- [x] Internal checkpoint 4 runnable state: snapshot/checkpoint persistence is wired and automatic compression is enabled on the real pre-call path only after success and failure transaction tests pass.
- [x] Implement compression instructions that preserve current task goal, completed milestones, inspected or modified files, remaining work, next plan, key decisions, constraints, and visible artifact/skill/reference/approval/policy facts only when already visible in previous summary or evicted history.
- [x] Derive `compression_evicted_history_budget` from `window_tokens`, previous summary estimate, compression prompt estimate, fixed overhead, and `compression_reserved_output_tokens`.
- [x] Trigger automatic compression when post-omission candidate tokens are strictly greater than `compress_history_at_ratio * window_tokens` or eligible evictable history exceeds the derived budget.
- [x] Select oldest eligible groups in chronological order without skipping older eligible groups.
- [x] Ensure runtime constructs and estimates a compression input that fits within `window_tokens` while respecting `compression_reserved_output_tokens` before calling the compression model.
- [x] Run at most one compression model call per pre-call optimization pass.
- [x] Ensure compression model calls are runtime-owned, tool-less, audited with `purpose="compression"`, and do not append assistant answers to durable conversation.
- [x] Parse compression output using the Phase 1 continuity summary JSON rules: required core fields are present and correctly typed, optional `visible_*` fields default to `[]`, extra fields are ignored, and empty, non-object, missing-required, or wrong-type output fails with `compression_failed`.
- [x] Before enabling automatic compression on the real pre-call path, implement automatic `compression_failed` handling for compression model failure, empty output, invalid continuity summary output, inability to construct compression input within `window_tokens` while respecting `compression_reserved_output_tokens`, and oldest eligible group not fitting within the derived compression input budget.
- [x] Ensure automatic `compression_failed` writes the compression failure event and a `context` checkpoint, aborts the current REPL turn without terminalizing the long-lived session/run, and terminalizes one-shot only after recording the same event/checkpoint facts.
- [x] Ensure automatic compression model failure writes `model_call_failed` with `purpose="compression"` before writing `compression_failed`.
- [x] Replace previous summary and selected evicted groups with the new canonical JSON summary.
- [x] Persist compression context snapshots with trigger `compression` or `omission | compression`.
- [x] Ensure an omission-plus-compression pass writes only one final context snapshot.
- [x] Artifact context snapshot payloads larger than 16 KiB through `payload_artifact_id`.
- [x] Display a REPL system message with reduced-from and reduced-to estimates after successful automatic compression.
- [x] Ensure no runtime code resumes or reconstructs working state from context snapshots in Phase 1.
- [x] Add unit tests for automatic compression triggers, proactive budget triggers, compression input fit checks, continuity summary parser behavior, compression frame exclusions, automatic failure transactions, model-call audit events, snapshot triggers, automatic compression success message, context snapshot 16 KiB artifacting, and omission-plus-compression single-snapshot behavior.
- [x] Add integration tests for automatic compression before initial model calls and before tool-loop follow-up calls, including at least one automatic `compression_failed` branch that proves no half-mutated conversation state remains.
- [x] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: context manager compression success path, compression model-call path, context snapshots, and context checkpoints.

Invariants: compression summary is continuity memory only; runtime truth remains in structured active skill records, snapshots, events, checkpoints, artifacts, approval records, and policy facts.

Freeze/review checkpoint: automatic compression can run once per optimization pass, replace eligible consumed history, persist one final context snapshot on success, and abort deterministically on failure before manual `/compress` is exposed.

Rollback: disable automatic compression while leaving old tool-result omission, automatic compression failure guards, and `ModelContextFrame` in place.

Runnable state: automatic compression can protect ordinary model calls and tool-loop continuations from context pressure when compression succeeds, and automatic compression failure leaves the REPL/one-shot state exactly as documented.

## Milestone 5C: Compression Failure Boundaries, Manual /compress, And Context Limit

Objective: finish compression by adding manual `/compress` and hard context-limit behavior on top of the automatic failure boundaries from Milestone 5B.

Deliverables: manual `/compress`, no-op and success messages, manual compression failure transaction boundaries, context-limit failure, one-shot terminal behavior, and REPL turn-scoped abort behavior.

- [ ] For manual `/compress`, reuse the Milestone 5B `compression_failed` boundary before calling the model when the derived budget is invalid or the oldest eligible group cannot fit.
- [ ] For oldest-eligible-group compression failure, display the exact message `Context compression could not fit the oldest eligible history group. The current turn was aborted. Start a new session to continue with a fresh context window.`
- [ ] Do not add Phase 1 recovery commands, forced history deletion, map-reduce compression, or repeated compression calls to work around compression failure.
- [ ] Implement manual `/compress` while idle using the same rolling summary machinery, skipping old-tool-result omission and ignoring the threshold when evictable history exists.
- [ ] Ensure `/compress` is a no-op with the exact system message `No compressible history.` when durable conversation is empty or no group is evictable.
- [ ] Ensure `/compress` no-op branches do not call the compression model, write a context snapshot, or mutate `ReplRuntime.conversation`.
- [ ] Ensure successful manual and automatic compression display a REPL system message with reduced-from and reduced-to estimates.
- [ ] Ensure manual `/compress` failure writes `compression_failed` and a `context` checkpoint, writes no context snapshot, mutates no conversation, and keeps the long-lived REPL session/run running.
- [ ] Ensure one-shot `compression_failed` writes the same compression event and `context` checkpoint fact before terminalizing the one-shot run/session as `failed` with `error_class="compression_failed"` and exiting non-zero.
- [ ] Ensure all `compression_failed` branches display the documented English UI message for the failure class; oldest-eligible-group failure must display `Context compression could not fit the oldest eligible history group. The current turn was aborted. Start a new session to continue with a fresh context window.`
- [ ] Implement `context_limit_exceeded` when rebuilt `ModelContextFrame` still exceeds `window_tokens`; REPL turns abort with a `context` checkpoint and the exact UI/event message `Context window still exceeds the limit after compression. The current turn was aborted.` without terminalizing session/run, while one-shot writes the same event/checkpoint before terminal failure.
- [ ] Add unit tests for oldest-group failure, manual no-op without model call/snapshot/conversation mutation, manual success, manual failure transaction boundary, one-shot `compression_failed` terminal behavior, compression-failure UI messages, and context-limit failure including the exact UI/event message.
- [ ] Add integration tests for manual `/compress` success/no-op/failure where deterministic injected I/O can verify behavior without manual TTY interaction.
- [ ] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: context manager compression failure path, `/compress`, context checkpoints, one-shot/REPL context failure behavior, and local command routing needed for `/compress`.

Invariants: failed compression and context-limit branches do not mutate successful continuity state; manual `/compress` shares the automatic rolling summary machinery and skips old-tool-result omission.

Freeze/review checkpoint: manual and automatic compression are independently testable, and all failure cases leave session/run state exactly as documented.

Rollback: disable manual `/compress` and compression failure UI integration while leaving successful automatic compression, old tool-result omission, and `ModelContextFrame` in place.

Runnable state: users can manually compress idle REPL history, receive deterministic no-op/failure messages, and recover from compression/context-limit turn aborts without terminalizing long-lived REPL runs.

## Milestone 6A: Local Commands, CLI Approval Mode, And Status Bar

Objective: expose Phase 1 local command and status surfaces before adding interactive approval UI complexity.

Deliverables: `/tools`, consolidated local slash-command assertions, unsupported command handling, CLI approval-mode option parsing for one-shot and REPL initial mode, approval-mode status source, and exact Phase 1 status bar format.

- [ ] Add `/tools` as a local REPL command listing current runtime-visible tools, category, risk, access, approval behavior, enabled status, and disabled reason.
- [ ] Ensure `/tools` reflects current frozen session config, active approval mode, path policy, and shell policy.
- [ ] Ensure `/skills`, `/tools`, and `/compress` are local and never sent to the model.
- [ ] Ensure `/compress` during active execution is suppressed without runtime side effects.
- [ ] Keep `/agents`, `/models`, and `/compact` unsupported in Phase 1.
- [ ] Add CLI approval-mode option parsing and validation for exactly `normal`, `semi-auto`, and `yolo` on one-shot and REPL startup.
- [ ] Ensure REPL default approval mode remains `normal`, one-shot default approval mode remains `normal`, explicit REPL startup values become the initial session approval mode, and explicit one-shot values are frozen into query/tool context.
- [ ] Ensure REPL startup approval-mode selection does not implement or replace 6B idle-state `Ctrl+Y` cycling.
- [ ] Add the deferred one-shot `semi-auto` skill activation integration test now that the CLI approval-mode option exists.
- [ ] Add a REPL startup `semi-auto` skill activation integration test that proves runtime-control activation is audit-only and does not request interactive approval.
- [ ] Update status bar to the exact Phase 1 format `model: <model> | approval: <approval> | context: <used> / <window> (<pct>) | tokens: <used> used`.
- [ ] Update status bar context before model calls, after omission/compression, and after provider usage or deterministic estimate fallback.
- [ ] Add unit tests for `/tools`, unsupported commands, local `/skills`/`/tools`/`/compress` routing, one-shot and REPL approval-mode option parsing/defaults, and exact status bar format.
- [ ] Add integration tests for `/tools`, `/skills`, `/compress` local routing and one-shot/REPL approval-mode selection where deterministic injected I/O can verify behavior without manual TTY interaction.
- [ ] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: REPL local command routing, CLI approval-mode parsing, REPL initial approval-mode selection, status data source, and status bar rendering.

Invariants: local slash commands are never sent to the model; status display is presentation state and not runtime truth.

Freeze/review checkpoint: users can inspect tools/skills/compression command behavior and status bar state before interactive approval UI is wired.

Rollback: disable `/tools` and Phase 1 status additions while retaining broker policy decisions and compression/runtime-control behavior.

Runnable state: REPL local commands and one-shot/REPL startup approval-mode selection are deterministic and testable without TTY approval prompts.

## Milestone 6B: Approval Providers, Denial Semantics, And TTY Integration

Objective: wire Phase 1 approval behavior into TTY/plain/non-interactive UI without making UI runtime truth.

Deliverables: `ApprovalProvider` implementations, inline TUI approval prompt, plain approval prompt, non-interactive denial, denial turn abort, approval mode cycling, approval persistence, and manual TTY evidence.

- [ ] Implement `ApprovalProvider` for TUI, plain REPL, and non-interactive paths.
- [ ] Implement prompt_toolkit inline approval state in the existing input lane with `y`, `a`, and `n`.
- [ ] Keep Milestone 6B reviewable through internal checkpoints: first land concrete approval providers and approval-request persistence, then denial turn-abort semantics, then idle-only `Ctrl+Y` and manual TTY evidence.
- [ ] Render approval prompts with at least tool name, risk level, path or command preview, and grant scope.
- [ ] Implement plain REPL approval prompt when interactive input is available.
- [ ] Deny non-interactive approval requests with `policy_denied` without hanging.
- [ ] Persist interactive approval decisions to `approval_grants` only for real prompts, including the rendered `approval_request` text shown to the user.
- [ ] Ensure policy auto-allow outcomes do not write `approval_grants` rows or `approval_requested` / `approval_decision_recorded` events.
- [ ] Implement user denial as `TurnAborted`: record denial facts, record terminal observation visible to future turns, short-circuit same-turn follow-up model calls, and return REPL input without terminalizing long-lived session/run.
- [ ] Ensure one-shot approval denial or unavailable approval terminalizes run/session as `failed` with `error_class="policy_denied"` and exits non-zero.
- [ ] Implement idle-only `Ctrl+Y` cycling `normal -> semi-auto -> yolo -> normal`.
- [ ] Record each idle `Ctrl+Y` approval-mode switch as an `approval_mode_changed` run event and an `engine.log` entry.
- [ ] Ensure `Ctrl+Y` during active execution or inline approval prompt is a silent no-op and does not queue a later mode change.
- [ ] Add unit tests for approval provider behavior, non-interactive denial, approval grant persistence including rendered `approval_request` text, denial turn abort, same-turn follow-up suppression, and `Ctrl+Y`.
- [ ] Add integration tests for non-interactive approval denial, approval denial turn abort, one-shot approval denial/unavailable approval behavior, and `approval_mode_changed` observability where deterministic injected I/O can verify behavior without manual TTY interaction.
- [ ] Run manual TTY checks for inline approval prompt, denial returning to input, `Ctrl+Y`, and approval-mode status updates; record terminal application, command sequence, expected result, observed result, and known limitation.
- [ ] Verify with canonical commands `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

Modified boundaries: REPL controller/view approval state, approval provider, approval audit, approval grant persistence, and prompt executor denial handling.

Invariants: approval UI is a presentation/control-plane state, not runtime truth; runtime services do not import prompt_toolkit, rich, or concrete view classes.

Freeze/review checkpoint: users can approve, approve-for-session, deny, and cycle approval mode interactively before final trace hardening.

Rollback: disable TUI approval prompt and use plain/non-interactive provider behavior while retaining broker policy decisions and local commands.

Runnable state: TTY, plain REPL, one-shot, and non-TTY approval behavior match the documented Phase 1 contract.

## Milestone 6C: Observability, Legacy Fail-Closed, And Acceptance Sweep

Objective: harden trace/log rendering, legacy fail-closed behavior, compatibility-boundary clarity, and final Phase 1 acceptance evidence.

Deliverables: Phase 1 trace rendering, Phase 1 engine-log helper ownership, engine-log rendering, legacy schema integration tests, one-shot/plain/non-TTY compatibility checks, streaming non-authority regression checks, full canonical test evidence, manual TTY evidence, and lockfile verification.

- [ ] Render Phase 1 skill snapshot, activation, active skill records, and reference load facts in trace.
- [ ] Render approval requested/decision facts, approval mode switches, policy denials, reusable grants, and denied tool outcomes in trace.
- [ ] Render shell policy denial, path policy denial, tool timeout, artifact registration, context snapshots, omission, compression, compression failure, and context-limit failure in trace.
- [ ] Extend `src/debug_agent/observability/logging.py` with Phase 1 engine-log helpers for skill, approval, policy, context optimization, and artifact registration facts.
- [ ] Record approval mode switches, approval decisions, policy denials, context optimizations, and artifact registrations in `engine.log`.
- [ ] Ensure large stack traces, shell outputs, and details are artifact-backed rather than stored inline.
- [ ] Add integration tests for `debug-agent trace <session_id>` including Phase 1 skill, approval, tool, and compression events.
- [ ] Add integration tests for Phase 1 startup, status, and trace failing closed on legacy Phase 0/0.5 schemas with clear user-facing guidance.
- [ ] Confirm one-shot mode remains plain stdout.
- [ ] Confirm non-TTY and injected I/O paths still use `PlainReplView`.
- [ ] Confirm Phase 0.5 streaming output remains non-authoritative and `AgentStreamEvent` is not persisted.
- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run `uv run pytest -v`.
- [ ] Run manual macOS Terminal TTY approval prompt check.
- [ ] Run manual iTerm2 TTY approval prompt check.
- [ ] Run manual TTY denial return-to-input check.
- [ ] Run manual TTY `/skills`, `/tools`, `/compress` check.
- [ ] Run manual status bar context/token update check.
- [ ] Record each manual check with terminal application, command sequence, expected result, observed result, and known limitation.
- [ ] Run REPL smoke with fake model config: start `debug-agent`, exercise `/skills`, `/tools`, `/compress`, then `/exit`.
- [ ] Run non-TTY `debug-agent < input.txt` approval-required check.
- [ ] Run one-shot `--approval-mode semi-auto` smoke.
- [ ] Run REPL startup `--approval-mode semi-auto` smoke.
- [ ] Record Windows shell wrapper behavior evidence using a fake runner or a real Windows smoke environment when available.
- [ ] Confirm no subagent, workflow, MCP, plugin, `/agents`, `/models`, `/compact`, or `deactivate_skill` feature is exposed or required.
- [ ] Confirm project lockfile reflects any changed dependency declarations; run `uv lock` if dependencies changed.

Modified boundaries: trace writer, engine log, final integration behavior, manual verification evidence.

Invariants: Phase 1 acceptance uses Phase 1 schema and intentionally tightened path policy; Phase 0/0.5 sessions are not compatibility requirements.

Freeze/review checkpoint: all Phase 1 unit/integration/manual verification evidence is recorded before declaring Phase 1 complete.

Rollback: keep completed lower milestones and disable only final presentation/trace additions if acceptance failures are isolated there.

Runnable state: Phase 1 satisfies `docs/phase-1/scope.md`, all Phase 1 specs, `docs/phase-1/tests.md`, and `docs/phase-1/operations.md`.

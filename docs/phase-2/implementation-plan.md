# Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This is an implementation-process instruction only. Phase 2 runtime itself does not implement subagents.

**Goal:** Build brokered `view_image` vision analysis and runtime-owned Todo Plan continuity while preserving Phase 1 ToolBroker, path policy, approval, context compression, and frozen-config boundaries.

**Architecture:** Phase 2 adds two vertical platform capabilities on top of Phase 1: run-scoped Todo Plan runtime truth, and a conditionally available native `view_image` tool backed by a separately frozen OpenAI-compatible multimodal provider config. Todo Plan is implemented as SQLite runtime truth plus prompt-frame injection; `view_image` is implemented as a brokered native tool with image metadata parsing, provider request normalization, strict audit redaction, and no raw image persistence.

**Tech Stack:** Python, SQLite, local filesystem artifacts, ToolBroker, prompt_toolkit/rich REPL from earlier phases, OpenAI Python SDK for OpenAI-compatible multimodal calls, Pillow for PNG/JPEG metadata parsing, pytest, uv.

---

## Freeze Decision

Phase 2 documentation is frozen for implementation planning as of this file.

Authoritative sources:

1. `docs/project-contract.md`
2. `docs/phase-2/*`
3. accepted `docs/adr/*`

If implementation discovers a conflict or missing contract, stop and request a contract patch before continuing. Do not silently reinterpret the contract from existing code.

## Goals

- Deliver SQLite Phase 2 schema gating with `PRAGMA user_version = 2`, including legacy Phase 0/0.5/1 fail-closed behavior before interpreting runtime truth.
- Deliver run-scoped Todo Plan persistence, whole-plan `todo` replacement, `todo_updated` events, status/trace visibility, and atomic mutation semantics.
- Deliver `runtime_todo_plan` injection into every ordinary task `ModelContextFrame`, including empty-plan injection, token estimation, and survival across automatic compression and manual `/compress`.
- Deliver frozen multimodal configuration and session-scoped `view_image` availability, including no-secret disabled reasons and no hot reload.
- Deliver brokered `view_image` for one to four authorized local PNG/JPEG paths with Pillow metadata parsing, SHA-256, dimension and request-size limits, provider call normalization, structured JSON response validation, and no image/base64 persistence.
- Preserve Phase 1 ToolBroker, path policy, approval, timeout, artifact, audit, context compression, REPL/TUI, one-shot, and trace behavior.

## Non-Goals

- No `AgentRegistry`, `/agents`, `/models`, subagents, brokered `task` tool, child runs, or subagent policy profiles.
- No workflow runtime, workflow skills, workflow handoff, workflow resume, task graph, background tasks, or RenderDoc readiness e2e.
- No MCP server lifecycle, MCP tool discovery, MCP invocation, plugin packaging, hook system, memory system, or tool-call cache.
- No session interruption, `/cancel`, terminalization, `resume`, PTY shell, long-running shell runtime, token-level resume, or tool-mid-flight resume.
- No remote image URLs, data URLs, file URLs, provider-native image content inputs, artifact-id image inputs, arbitrary image formats, image cache, or automatic copying of source images into `ArtifactStore`.
- No Anthropic-compatible vision path, fallback vision path, full provider abstraction, provider/model discovery, or vision config hot reload.
- No shader-specific runtime validators, RenderDoc command allowlists, Ralph Loop state machines, shader business report schemas, or runtime-owned RenderDoc semantics.

## File Structure To Create Or Modify

- `docs/phase-2/implementation-plan.md`: this milestone schedule.
- `pyproject.toml`: add Phase 2 runtime dependencies `openai` and `Pillow`.
- `uv.lock`: update after dependency declaration changes with `uv lock`.
- `src/debug_agent/persistence/sqlite.py`: Phase 2 schema, `PHASE_2_SCHEMA_USER_VERSION = 2`, legacy-schema fail-closed message, Todo Plan tables.
- `src/debug_agent/persistence/todo_plans.py`: run-scoped current Todo Plan store, atomic replace + `todo_updated` transaction helper.
- `src/debug_agent/persistence/sessions.py`: ensure active ownership checks run only after Phase 2 schema validation.
- `src/debug_agent/persistence/events.py`: accept/render `todo_updated` and Phase 2 `view_image` audit metadata through existing event writer paths.
- `src/debug_agent/runtime/config.py`: multimodal config parsing, defaults, validation, no-secret frozen snapshot fields, disabled reason handling.
- `src/debug_agent/runtime/model_context.py`: `runtime_todo_plan` segment kind support and token-estimation coverage.
- `src/debug_agent/runtime/prompt_composer.py`: Todo Plan segment injection after active skill context and before summary/history.
- `src/debug_agent/runtime/orchestrator.py`: startup ordering for Phase 2 schema gate, config/policy freeze, TodoPlanStore, and broker tool availability.
- `src/debug_agent/runtime/prompt_executor.py`: pass TodoPlanStore and multimodal availability through ordinary task/tool-loop execution context.
- `src/debug_agent/tools/broker.py`: dynamic tool availability for disabled `view_image`, Phase 2 timeout override, audit redaction hooks for `view_image`, provider-egress approval-scope behavior.
- `src/debug_agent/tools/runtime_control.py`: `todo` tool definition and handler.
- `src/debug_agent/tools/view_image.py`: `view_image` definition, schema validation helpers, local image validation, Pillow metadata extraction, SHA-256, request-size projection, structured result normalization.
- `src/debug_agent/adapters/vision_client.py`: OpenAI-compatible non-streaming Chat Completions client for `kimi-k2.5`, fake client test seam, no retry, timeout propagation, JSON-object response request.
- `src/debug_agent/observability/trace_writer.py`: render Todo Plan and `view_image` facts without image bytes/base64/query text.
- `src/debug_agent/observability/logging.py`: no-secret Phase 2 engine log helpers for `view_image` and Todo Plan facts.
- `src/debug_agent/cli/repl_controller.py`: `/tools` visibility and disabled reason behavior, `/compress` Todo Plan preservation, optional Todo Plan summary wiring.
- `src/debug_agent/cli/repl_view.py`: view-facing snapshots for Phase 2 tool visibility and Todo Plan summary if implemented.
- `src/debug_agent/cli/plain_repl_view.py`: plain disabled reason and approval prompt target rendering.
- `src/debug_agent/cli/prompt_toolkit_view.py`: TUI disabled reason, approval prompt target rendering, optional compact Todo Plan summary.
- `tests/unit/persistence/test_phase2_schema.py`: schema version, legacy fail-closed, Todo Plan table bootstrap.
- `tests/unit/persistence/test_todo_plans.py`: TodoPlanStore persistence, atomicity, versioning, run isolation.
- `tests/unit/runtime/test_phase2_multimodal_config.py`: multimodal config freeze, disabled reasons, no-secret snapshot, no hot reload.
- `tests/unit/runtime/test_phase2_prompt_composer.py`: `runtime_todo_plan` injection, empty state, token estimation, compression exclusion.
- `tests/unit/tools/test_todo_tool.py`: `todo` schema, semantics, approval exception, events, result shape.
- `tests/unit/tools/test_view_image.py`: source validation, policy ordering, Pillow metadata, request limits, query rules, no bytes/base64.
- `tests/unit/adapters/test_vision_client.py`: provider request shape, timeout, no retry, JSON parsing boundary, fake injection.
- `tests/unit/observability/test_phase2_trace_status.py`: status/trace rendering and no query/image leakage.
- `tests/unit/cli/test_phase2_tools_display.py`: `/tools`, disabled reason, approval prompt target display.
- `tests/integration/test_phase2_todo_plan.py`: one-shot and REPL Todo Plan continuity including `/compress`.
- `tests/integration/test_phase2_view_image.py`: valid single/multi-image calls, disabled config, policy denial, provider timeout, trace no-base64.
- `tests/integration/test_phase2_compatibility.py`: legacy Phase 1 database fail-closed for startup/status/trace.

Module paths may be adjusted only to match established repository naming or nearby ownership patterns. Record any path adjustment in the milestone checkpoint and preserve the responsibilities, scope, and dependency direction above.

Files and modules not listed in this plan are out of modification scope by default. If implementation evidence shows an unlisted nearby owner module is required to satisfy a listed deliverable, keep the change minimal, record the path adjustment at the milestone checkpoint, and do not expand Phase 2 behavior beyond the documented boundaries.

## Global Invariants

- All model-visible tools pass through `ToolBroker`; neither `todo` nor `view_image` may bypass schema validation, permission evaluation, timeout, result normalization, or audit.
- `todo` is the only new Phase 2 `runtime_control` approval exception. Valid `todo` calls are audit-only in every approval mode and must not write `approval_grants`, `approval_requested`, or `approval_decision_recorded`.
- `view_image` is read-only for approval-mode purposes. Its reusable approval scope contains only `tool_name = "view_image"`, read access, and the ordered canonical image path list.
- Enabled frozen multimodal configuration is the provider-egress contract for `view_image`; Phase 2 does not add a separate provider-egress approval scope.
- `view_image` raw image bytes, base64 strings, provider image content parts, and concrete effective query text must not appear in runtime-authored persisted audit metadata, trace output, engine log, context snapshot metadata, or `ToolResult.metadata`.
- Assistant-authored raw `view_image` tool-call arguments and the immediate tool-loop transcript may contain `query`; runtime must not create additional persisted query copies.
- Todo Plan is run-scoped runtime truth. It is not restored from conversation history, compression summaries, trace, or UI state.
- `runtime_todo_plan` is injected into ordinary task frames, counted by token estimation, never appended to durable conversation, and never included as independent runtime truth in compression frames.
- `view_image` normalized textual observations are ordinary durable tool observations and may be omitted or compressed like other tool results.
- Phase 2 must not reinterpret Phase 0, Phase 0.5, or Phase 1 runtime databases. Existing mismatched `.sessions/runtime.db` files fail closed before runtime truth rows are read.
- Automated tests must use fake or stubbed `VisionModelClient` behavior. No canonical automated test may require network access, live API keys, or a real multimodal provider.

## Dependency Graph

```text
dependency declarations + schema gate
-> TodoPlanStore
   -> todo runtime-control tool
      -> PromptComposer runtime_todo_plan injection
         -> compression/status/trace Todo Plan continuity

schema gate + complete Phase 2 frozen config snapshot shape
-> multimodal config availability
   -> disabled view_image broker availability
      -> enabled-ready config snapshot facts, with model-visible activation still gated
         -> view_image source validation and metadata
            -> VisionModelClient provider boundary
               -> view_image audit/result redaction
                  -> enabled view_image model-visible activation
                     -> trace/status/CLI surfaces

Todo Plan vertical slice + view_image vertical slice
-> integration acceptance and full Phase 2 verification
```

The dependency order is mandatory. The Todo Plan and `view_image` slices can be implemented by separate workers only after the shared schema/config/broker foundations are stable. Fresh user-facing Phase 2 sessions must not become the default main path until the Phase 2 schema gate, Todo Plan vertical slice, and complete Phase 2 frozen config snapshot shape, including multimodal availability and disabled `view_image` facts, are all implemented. Enabled `view_image` must not be exposed to ordinary model-visible tool bindings until validation, provider normalization, audit redaction, no-query/no-base64 persistence checks, and disabled-tool behavior are implemented and verified.

## Verification Strategy

Verification uses only canonical commands from `docs/phase-2/operations.md`.

- Each milestone runs the narrowest applicable canonical command or command sequence from `docs/phase-2/operations.md` before the checkpoint is accepted.
- Unit-only milestones run `uv run pytest tests/unit -v`.
- Milestones that add or change integration behavior run `uv run pytest tests/unit -v` followed by `uv run pytest tests/integration -v`.
- Dependency changes require `uv lock` and inclusion of the updated `uv.lock` in the same review patch as `pyproject.toml`.
- Automated `view_image` tests must use fake or stubbed `VisionModelClient` behavior. No automated milestone or acceptance check may require network access, live API keys, or a real multimodal provider.
- Todo Plan verification must prove runtime truth comes from `TodoPlanStore`, not durable conversation, compression summaries, trace, or UI state.
- `view_image` verification must include negative no-leak assertions for image bytes, base64, provider image content parts, and runtime-authored concrete query copies in persisted audit metadata, trace, engine log, context snapshot metadata, ordinary tool output, and `ToolResult.metadata`.
- Milestone 7 runs the Phase 2 canonical acceptance sequence: `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, and `uv run pytest -v`, followed by the manual TTY checks required by `docs/phase-2/operations.md`.
- If a milestone can only be partially verified, stop at the checkpoint and record exactly which command or manual check was not run, why it was not run, and which contract remains unverified.

## Migration / Rollback Strategy

Phase 2 is a breaking runtime-truth schema and tool-contract change. It is not an automatic migration from Phase 0, Phase 0.5, or Phase 1.

- Fresh `.sessions/runtime.db` files are created with `PRAGMA user_version = 2`.
- Existing missing, legacy, unknown, or non-2 runtime databases fail closed before runtime truth is interpreted. Runtime must not migrate, delete, rewrite, clean up ownership rows, or reinterpret legacy `.sessions/runtime.db` files.
- Rollback of implementation work is source-level rollback: revert the current milestone patch before proceeding to the next milestone. If dependency declarations were changed, revert `pyproject.toml` and `uv.lock` together.
- Runtime database rollback is not supported. To run older code or retry from a clean state, move or remove `.sessions/` manually or use a fresh workspace, matching the Phase 2 compatibility error guidance.
- Each milestone must remain reviewable and revertible as a coherent patch. Do not mix unrelated cleanup, broad refactors, or future-phase scaffolding into a milestone patch.
- Before Milestone 4 removes the broad development gate, user-facing startup must fail closed before accepting a prompt rather than persist accepted Phase 2 sessions with incomplete multimodal availability/config snapshot fields. This prevents long-lived `user_version = 2` databases whose session snapshot shape differs from the final Phase 2 contract.
- `view_image` activation is rollback-safe by construction: config parsing and disabled availability land before real routing, real routing lands before user/model-visible enabled exposure, and enabled exposure lands only after no-leak redaction tests pass.

## Execution Stages

The following milestones are the execution stages for Phase 2. They are ordered by dependency, not by feature category or calendar time. Each stage must produce a runnable repository, keep tests executable, preserve the main one-shot/REPL startup path according to the development-gate rules below, and stop at its freeze/review checkpoint before the next stage begins.

### Dual-Path Development Gate

Milestones 1 through 4 establish the Phase 2 schema, the first complete Todo Plan vertical slice, and the complete Phase 2 frozen config snapshot shape. During these milestones, Phase 2-only startup, prompt execution, and model-visible tool paths must remain behind an internal development/test gate until the repository can satisfy the Todo Plan minimum runnable slice end-to-end and can freeze multimodal `view_image` availability facts for every fresh Phase 2 session.

The default user-facing one-shot, plain REPL, and TUI entrypoints must either keep the last completed runnable path intact or fail closed before accepting a user prompt with a clear milestone-gated error. They must not enter a partially wired Phase 2 prompt loop where the database has `user_version = 2` but `todo`, `runtime_todo_plan` injection, compression survival, basic Todo status/trace, and complete Phase 2 config snapshot shape are not all implemented.

This gate is an implementation-transition rule only. It must not create a shipped compatibility mode, a legacy schema reader, a migration path, a Phase 1 runtime-truth reinterpretation path, or a long-lived mixed Phase 2 schema/snapshot shape. Before Milestone 4 completes, user-facing startup must not persist an accepted Phase 2 session/run/config snapshot that later code would interpret as a complete Phase 2 session. Once Milestone 4 completes, the gated Phase 2 Todo Plan path with frozen multimodal availability facts becomes the default main path for fresh Phase 2 workspaces and the temporary broad gate is removed.

After Milestone 4, narrower internal gates may still hide incomplete enabled `view_image` behavior. In particular, enabled-ready `view_image` config facts may be frozen before real routing, real routing may be tested behind fake-provider seams before ordinary exposure, and enabled `view_image` must not become model-visible until audit redaction and no-leak verification complete in Milestone 6.

## Milestone 1: Phase 2 Schema Gate And Dependencies

**Objective:** establish Phase 2 database compatibility and runtime dependencies before any new runtime truth is used.

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/debug_agent/persistence/sqlite.py`
- Modify: `src/debug_agent/persistence/sessions.py`
- Test: `tests/unit/persistence/test_phase2_schema.py`
- Test: `tests/integration/test_phase2_compatibility.py`

**Deliverables:** Phase 2 dependency declarations, a fresh Phase 2 SQLite schema with Todo Plan storage, `PHASE_2_SCHEMA_USER_VERSION = 2`, and fail-closed legacy/unknown schema handling before runtime truth reads.

**Modified boundaries:** persistence bootstrap, schema identity, dependency lockfile, and active ownership schema-check ordering only.

**Invariants:** legacy runtime databases are not migrated, deleted, rewritten, or interpreted; startup, status, trace, and ownership checks read only `PRAGMA user_version` before fail-closed return; user-facing Phase 2 prompt execution remains gated until Milestone 4 completes the Todo Plan vertical slice plus full Phase 2 config snapshot shape.

**Verification steps:** run `uv lock` after dependency edits, then run the canonical unit and integration commands because this milestone changes startup/status/trace compatibility behavior.

**Freeze/review checkpoint:** do not start Todo Plan tool work until fresh Phase 2 bootstrap and all legacy fail-closed paths are verified.

- [x] Add `openai` and `Pillow` to `pyproject.toml` dependencies.
- [x] Run `uv lock`.
  - Command: `uv lock`
  - Expected: lockfile updates without dependency resolution errors.
- [x] Replace Phase 1 schema identity with `PHASE_2_SCHEMA_USER_VERSION = 2`.
- [x] Update the unsupported database message to the Phase 2 text from `docs/phase-2/specs/compatibility.md`.
- [x] Extend SQLite schema with Todo Plan current-state persistence. Use either normalized rows or JSON payload; ensure it can atomically store `session_id`, `run_id`, `plan_version`, ordered items, `created_at`, and `updated_at`.
- [x] Ensure `RuntimeDatabase.bootstrap()` reads `PRAGMA user_version` before interpreting any existing runtime truth rows.
- [x] Ensure startup, active workspace ownership checks, `status`, and `trace` cannot read session/run/event/checkpoint/artifact/Todo rows from legacy or unknown schemas.
- [x] Write tests for fresh DB `user_version = 2`.
- [x] Write tests for existing `user_version = 0`, `1`, and unknown future versions failing closed with `config_error` and unsupported DB guidance.
- [x] Write tests proving legacy DB files are not migrated, deleted, or rewritten.
- [x] Run canonical unit tests.
  - Command: `uv run pytest tests/unit -v`
  - Expected: all unit tests pass.
- [x] Run canonical integration tests.
  - Command: `uv run pytest tests/integration -v`
  - Expected: all integration tests pass.

Freeze/review checkpoint: Phase 2 can bootstrap only fresh Phase 2 databases and rejects legacy databases before runtime truth interpretation; user-facing Phase 2 prompt execution remains gated and must not persist accepted user-facing sessions with an incomplete Phase 2 config snapshot shape.

## Milestone 2: Todo Plan Store And `todo` Tool

**Objective:** add run-scoped Todo Plan runtime truth and the brokered whole-plan replacement tool.

**Files:**

- Create: `src/debug_agent/persistence/todo_plans.py`
- Modify: `src/debug_agent/tools/runtime_control.py`
- Modify: `src/debug_agent/tools/broker.py`
- Modify: `src/debug_agent/runtime/contracts.py` if a local event-kind enum or helper exists.
- Test: `tests/unit/persistence/test_todo_plans.py`
- Test: `tests/unit/tools/test_todo_tool.py`

**Deliverables:** `TodoPlanStore`, brokered `todo` tool, atomic whole-plan replacement, `todo_updated` event writes, structured `ToolResult` output, compact redacted output, and approval-exception coverage.

**Modified boundaries:** Todo Plan persistence, runtime-control tool registry, ToolBroker approval exception handling, and event-kind registration if required by existing contracts.

**Invariants:** `todo` remains the only new Phase 2 runtime-control approval exception; failed mutations leave current plan and events unchanged; Todo Plan state is run-scoped and independent from conversation, trace, compression, and UI state; no `view_image` behavior changes in this milestone.

**Verification steps:** run the canonical unit command.

**Freeze/review checkpoint:** do not add prompt injection until `todo` can replace and clear the current run plan through ToolBroker with atomic persistence and audit evidence.

- [ ] Implement `TodoPlanStore.get_current(run_id)` returning version `0`, `items=[]`, and explicit empty state when no plan exists.
- [ ] Implement `TodoPlanStore.replace_plan(session_id, run_id, items, event_writer)` as one SQLite transaction that persists the current plan and writes `todo_updated`.
- [ ] Preserve input order and assign runtime-owned 1-based indexes.
- [ ] Increment `plan_version` monotonically per run; first successful mutation reports `previous_plan_version = 0`, `plan_version = 1`.
- [ ] Ensure failed persistence leaves prior plan unchanged and commits no `todo_updated` event.
- [ ] Add the `todo` tool definition with `category="runtime_control"`, `risk_level="runtime_control"`, `access=[]`, required `items`, maximum 20 items, and status enum.
- [ ] Add semantic validation for trimmed `content` and `activeForm`: non-empty, `content <= 240`, `activeForm <= 120`, at most one `in_progress`.
- [ ] Normalize `activeForm` away for non-`in_progress` items before persistence, output, events, trace, prompt injection, and TUI display.
- [ ] Add broker policy handling so valid `todo` calls are audit-only in all approval modes and do not create approval grants or interactive approval events.
- [ ] Return structured `ToolResult.output`, `ToolResult.metadata`, and compact `redacted_output` exactly matching `docs/phase-2/specs/todo-plan.md`.
- [ ] Write tests for schema rejection, semantic rejection, empty clear, item ordering, versioning, run isolation, atomicity, and approval exception behavior.
- [ ] Run canonical unit tests.
  - Command: `uv run pytest tests/unit -v`
  - Expected: all unit tests pass.

Freeze/review checkpoint: `todo` can replace and clear a run's current plan through ToolBroker, and persisted plan truth is independent from conversation history; user-facing Phase 2 prompt execution remains gated until prompt injection, basic Todo observability, and full Phase 2 config snapshot shape land through Milestone 4.

## Milestone 3: Todo Plan Prompt Injection, Status/Trace, And Compression Survival

**Objective:** make current Todo Plan visible to ordinary model calls and basic observability without making it conversation or compression truth.

**Files:**

- Modify: `src/debug_agent/runtime/model_context.py`
- Modify: `src/debug_agent/runtime/prompt_composer.py`
- Modify: `src/debug_agent/runtime/context_manager.py`
- Modify: `src/debug_agent/runtime/query_control.py` if model-call frame construction requires store access.
- Modify: `src/debug_agent/runtime/prompt_executor.py`
- Modify: `src/debug_agent/runtime/orchestrator.py`
- Modify: `src/debug_agent/observability/trace_writer.py`
- Test: `tests/unit/runtime/test_phase2_prompt_composer.py`
- Test: `tests/unit/observability/test_phase2_trace_status.py`
- Test: `tests/integration/test_phase2_todo_plan.py`

**Deliverables:** non-persistent `runtime_todo_plan` model-context segment, deterministic placement and token estimation, compression exclusion, basic Todo Plan status/trace rendering, and one-shot/REPL continuity tests.

**Modified boundaries:** ordinary task `ModelContextFrame` construction, prompt materialization, context-window accounting, compression exclusion wiring, executor/store plumbing, and Todo-only status/trace rendering.

**Invariants:** Todo Plan is always injected into ordinary task frames, including empty state; Todo Plan is never appended to durable conversation or rebuilt from summaries; compression and `/compress` never mutate `TodoPlanStore`; status and trace remain observational and never become recovery truth; `view_image` remains unimplemented or disabled.

**Verification steps:** run the canonical unit and integration commands because this milestone completes the Todo Plan vertical slice that will become user-facing after Milestone 4 freezes the complete Phase 2 config snapshot shape.

**Freeze/review checkpoint:** do not begin multimodal config work until every ordinary task model call receives Todo Plan from `TodoPlanStore`, compression cannot mutate or reconstruct it, and basic Todo status/trace is observable from persisted plan truth.

- [ ] Add `runtime_todo_plan` as a non-persistent `ModelContextFrame` segment kind.
- [ ] Inject the current run's Todo Plan after active skill context and before rolling summary, retained raw history, live/unconsumed messages, tool-loop messages, and current user input.
- [ ] Always inject the segment, including `plan_version = 0`, `items = []`, and `Current Todo Plan is empty.` when no mutation has happened.
- [ ] Inject the persisted version and empty summary after a successful `todo(items=[])` clear.
- [ ] Render plan item `content` and `activeForm` as delimited structured data, not free-form instructions.
- [ ] Include a runtime-owned instruction telling the model to call `todo` when status changes or the plan no longer matches the work.
- [ ] Include the injected segment in deterministic token estimation and context-window accounting.
- [ ] Exclude Todo Plan from compression frames and ensure `/compress`, automatic omission, and automatic compression leave `TodoPlanStore` unchanged.
- [ ] Render `todo` calls, `todo_updated` events, current plan summaries, and compact counts in trace/status from `TodoPlanStore` and existing event records.
- [ ] Ensure status/trace schema-version checks still run before reading session, run, event, checkpoint, artifact, or Todo Plan rows.
- [ ] Write tests proving injected Todo Plan is not appended to durable conversation history.
- [ ] Write tests proving compression summary text is not used to rebuild Todo Plan.
- [ ] Write tests proving status/trace uses persisted Todo Plan truth and does not reconstruct Todo Plan from trace, UI state, or compression summaries.
- [ ] Run canonical unit tests.
  - Command: `uv run pytest tests/unit -v`
  - Expected: all unit tests pass.
- [ ] Run canonical integration tests.
  - Command: `uv run pytest tests/integration -v`
  - Expected: all integration tests pass.

Freeze/review checkpoint: every ordinary task model call receives authoritative Todo Plan state from `TodoPlanStore`, compression cannot mutate or reconstruct it, and basic Todo status/trace is available. User-facing Phase 2 prompt execution remains gated until Milestone 4 freezes complete multimodal availability facts and disabled `view_image` behavior.

## Milestone 4: Multimodal Config And `view_image` Availability Foundation

**Objective:** freeze the complete Phase 2 multimodal provider and `view_image` availability facts into session config, establish disabled/activation-gated `view_image` behavior, and only then remove the broad Phase 2 development gate.

**Files:**

- Modify: `src/debug_agent/runtime/config.py`
- Modify: `src/debug_agent/runtime/orchestrator.py`
- Modify: `src/debug_agent/tools/broker.py`
- Test: `tests/unit/runtime/test_phase2_multimodal_config.py`
- Test: `tests/unit/tools/test_view_image.py`
- Test: `tests/integration/test_phase2_todo_plan.py`

**Deliverables:** multimodal config parsing, no-secret config snapshot facts, disabled reasons, no-hot-reload behavior, broker-side disabled `view_image` handling, an internal activation gate that keeps enabled `view_image` out of ordinary model-visible bindings until Milestone 6, and removal of the broad Phase 2 development gate for fresh workspaces.

**Modified boundaries:** runtime config loading/freezing, orchestrator startup wiring, ToolBroker tool-availability records, and tool-binding omission logic only.

**Invariants:** missing, invalid, unsupported, or env-incomplete multimodal config disables `view_image` without failing session startup; every user-facing fresh Phase 2 session persisted after this milestone has the complete Phase 2 config snapshot shape; valid config may freeze enabled-ready facts but must not expose or route enabled `view_image` for ordinary sessions before Milestone 6; unknown tool behavior remains Phase 1 behavior; `todo` and Phase 1 tools remain visible.

**Verification steps:** run the canonical unit and integration commands because this milestone changes startup snapshot shape and removes the broad Phase 2 development gate. Enabled real routing is deliberately not accepted in this milestone.

**Freeze/review checkpoint:** do not implement `ViewImageTool` routing until config freezing, disabled behavior, no-secret snapshot facts, and activation gating are reviewed.

- [ ] Parse `[multimodal.defaults]`, `[multimodal.auth]`, and `[multimodal.providers.openai]` from `~/.debug-agent/config.toml`.
- [ ] Require explicit `provider`, `model`, `api_key_env`, and `base_url_env` before enabling real `view_image`.
- [ ] Support defaults only for `timeout_seconds = 60`, `max_tokens = 4096`, `max_query_chars = 8192`, and `max_analysis_chars = 8192`.
- [ ] Validate `provider == "openai"` and `model == "kimi-k2.5"` for real multimodal execution.
- [ ] Validate positive integer timeout/token/query/analysis settings.
- [ ] Freeze no-secret facts in `sessions.config_snapshot_json`: provider, model, timeout, max tokens, query and analysis limits, env var names, env-present booleans, `view_image_enabled`, and disabled reason.
- [ ] Disable `view_image` instead of failing session startup when multimodal config is missing, invalid, unsupported, or required env vars are absent.
- [ ] Keep config and environment changes from hot-reloading into active sessions.
- [ ] Add broker-side disabled `view_image` availability so direct or stale valid calls return `ToolResult.status = "denied"` with `error_class = "config_error"` and `tool_call_denied`.
- [ ] Preserve existing unknown-tool behavior for all other tool names.
- [ ] Omit disabled `view_image` from `ModelContextFrame.tool_schema_bindings` and the model-visible tool list while keeping `todo` and Phase 1 tools visible.
- [ ] Add an internal Phase 2 implementation gate so enabled-ready `view_image` config facts are frozen but ordinary model-visible enabled exposure remains off until Milestone 6 completes no-leak audit redaction.
- [ ] Remove the broad Phase 2 development gate for fresh workspaces only after Todo Plan continuity, complete Phase 2 config snapshot shape, disabled `view_image` behavior, and activation-gated enabled-ready config facts are all verified.
- [ ] Write tests proving invalid `timeout_seconds`, `max_tokens`, `max_query_chars`, and `max_analysis_chars` disable `view_image` without failing session startup or hiding `todo` and Phase 1 tools.
- [ ] Run canonical unit tests.
  - Command: `uv run pytest tests/unit -v`
  - Expected: all unit tests pass.
- [ ] Run canonical integration tests.
  - Command: `uv run pytest tests/integration -v`
  - Expected: all integration tests pass.

Freeze/review checkpoint: sessions freeze multimodal availability deterministically, disabled `view_image` cannot be seen by the model or accidentally routed, enabled-ready `view_image` remains activation-gated, and the default Phase 2 main path is no longer behind the broad development gate.

## Milestone 5: `view_image` Validation, Metadata, And Provider Boundary

**Objective:** implement brokered local PNG/JPEG inspection with strict metadata, request limits, provider-call shape, and structured result validation.

**Files:**

- Create: `src/debug_agent/tools/view_image.py`
- Create: `src/debug_agent/adapters/vision_client.py`
- Modify: `src/debug_agent/tools/broker.py`
- Test: `tests/unit/tools/test_view_image.py`
- Test: `tests/unit/adapters/test_vision_client.py`
- Test: `tests/integration/test_phase2_view_image.py`

**Deliverables:** `ViewImageTool`, `VisionModelClient`, enabled tool schema behind the activation gate, local image validation, metadata extraction, request-size projection, fake-client provider tests, and structured result normalization.

**Modified boundaries:** `view_image` handler module, vision client adapter, ToolBroker routing internals, timeout propagation, and test-only fake provider injection.

**Invariants:** ordinary sessions still do not expose enabled `view_image` until Milestone 6; every enabled-path test uses fake/stubbed `VisionModelClient` through a test-only internal activation seam, not through ordinary user config or ordinary model-visible exposure; image bytes are read only after ToolBroker permission allows every path; no provider call occurs after any schema, policy, image, query, or request-size failure; no source image is copied into `ArtifactStore`.

**Verification steps:** run the canonical unit and integration commands with fake provider behavior only.

**Freeze/review checkpoint:** do not activate enabled `view_image` for ordinary model-visible bindings until provider normalization and no-image/base64/query persistence protections are implemented and verified in Milestone 6.

- [ ] Add enabled `view_image` tool definition behind the internal activation gate with `category="native"`, `risk_level="read"`, `access=["read"]`, required `paths`, optional `query`, `minItems=1`, `maxItems=4`, and `additionalProperties=false`.
- [ ] Enforce schema failures as `ToolResult.status = "denied"`, `error_class = "user_error"`, and `tool_call_denied`.
- [ ] Normalize path facts before permission evaluation; emit pre-read broker audit facts available at that stage; read image bytes only after schema validation, path policy, approval, and pre-read audit have completed for every path.
- [ ] Reject remote URLs, `file://`, `data:`, directories, missing files, symlink escapes, structured artifact-source fields, and explicit artifact URI-style inputs.
- [ ] Treat bare string values that look like artifact ids as local path candidates, not artifact references.
- [ ] Verify PNG/JPEG type from bytes and parse width/height through Pillow; do not trust extensions alone.
- [ ] Accept valid PNG/JPEG files with uncommon extensions when path policy allows them.
- [ ] Compute MIME type, byte size, SHA-256, width, height, and display path for every input image in input order.
- [ ] Enforce width and height `<= 4096`, pixel budget `<= 4096 * 2160`, and projected compact UTF-8 Chat Completions request body size `<= 100,000,000` bytes before provider call.
- [ ] Add deterministic request-projection tests using a compact JSON golden/snapshot that includes `model`, `messages`, `response_format`, `max_tokens`, every image data URL content part, the text instruction content part, and SDK-equivalent merged Kimi thinking disable fields.
- [ ] Trim `query`; reject empty/whitespace-only query and query longer than frozen `max_query_chars`; use runtime default query when omitted.
- [ ] Build one non-streaming OpenAI-compatible Chat Completions request with image data URL content parts followed by one text instruction part.
- [ ] Include `response_format={"type": "json_object"}`, `max_tokens`, and Kimi thinking disabled via `extra_body={"thinking": {"type": "disabled"}}` or SDK-equivalent merged request field.
- [ ] Disable SDK/client implicit retry for this provider path and perform at most one provider request per `view_image` call.
- [ ] Pass the same effective timeout from ToolBroker into `VisionModelClient`.
- [ ] Re-check required API key and base URL environment variables at `view_image` execution time when the frozen startup snapshot enabled `view_image`; if either value is missing, return `ToolResult.status = "error"` with `error_class = "config_error"` before constructing the provider client or request.
- [ ] Extract provider text from `completion.choices[0].message.content`, parse as JSON object, require non-empty `analysis`, and enforce `max_analysis_chars`.
- [ ] Ignore provider-returned source metadata and use only runtime-computed local image metadata.
- [ ] Return `ToolResult.output.analysis` and display metadata, and put runtime metadata images, provider/model, duration, and `effective_query_source` in `ToolResult.metadata`.
- [ ] Artifact large raw textual provider output only under existing large-output rules; never artifact source image bytes merely because `view_image` was called.
- [ ] Write tests proving successful and failed `view_image` calls do not create `ArtifactStore` records or files for source image bytes; only large textual provider output may use the existing artifact path.
- [ ] Write tests for execution-time missing API key env var and execution-time missing base URL env var after a valid enabled startup snapshot.
- [ ] Write tests proving image file bytes are not read before policy/approval allow decisions and pre-read ToolBroker audit emission.
- [ ] Write tests proving denied policy/approval paths emit denial audit and never open or read image bytes.
- [ ] Use a fake or spied image reader/opener in ordering tests so the assertion covers byte-read timing, not only final `ToolResult` shape.
- [ ] Run canonical unit tests.
  - Command: `uv run pytest tests/unit -v`
  - Expected: all unit tests pass.
- [ ] Run canonical integration tests.
  - Command: `uv run pytest tests/integration -v`
  - Expected: all integration tests pass.

Freeze/review checkpoint: activation-gated `view_image` can inspect authorized local PNG/JPEG inputs through a fake provider in tests and returns only normalized semantic observations; ordinary sessions still cannot see enabled `view_image` until Milestone 6.

## Milestone 6: Audit Redaction, Enabled Activation, Trace, Status, And CLI/TUI Surfaces

**Objective:** complete no-leak audit redaction, then activate enabled `view_image` for ordinary model-visible use and make image-analysis behavior inspectable without leaking image bytes, base64, provider image parts, or concrete query text.

**Files:**

- Modify: `src/debug_agent/tools/broker.py`
- Modify: `src/debug_agent/observability/trace_writer.py`
- Modify: `src/debug_agent/observability/logging.py`
- Modify: `src/debug_agent/runtime/orchestrator.py` if `status` output carries disabled `view_image` reasons or Phase 2 status facts.
- Modify: `src/debug_agent/runtime/prompt_executor.py`
- Modify: `src/debug_agent/cli/repl_controller.py`
- Modify: `src/debug_agent/cli/repl_view.py`
- Modify: `src/debug_agent/cli/plain_repl_view.py`
- Modify: `src/debug_agent/cli/prompt_toolkit_view.py`
- Test: `tests/unit/observability/test_phase2_trace_status.py`
- Test: `tests/unit/cli/test_phase2_tools_display.py`

**Deliverables:** `view_image` audit redaction override, no-query/no-base64/no-image-content persistence assertions, enabled `view_image` model-visible activation for valid frozen config, `view_image` trace/status rendering, `/tools` visibility, disabled reason display, approval target rendering, and optional TUI Todo Plan summary.

**Modified boundaries:** ToolBroker audit normalization, `view_image` trace/status rendering, engine log facts, final tool-binding exposure, and REPL/TUI presentation surfaces.

**Invariants:** enabled `view_image` becomes model-visible only after redaction protections are in place; disabled `view_image` remains omitted and returns frozen `config_error` for stale/direct valid calls; trace/status/UI are observational and never become recovery truth; no new `view_image_*` event kinds are introduced; no runtime-authored concrete query text, image bytes, base64, or provider image content parts are persisted or displayed.

**Verification steps:** run the canonical unit and integration commands because this milestone changes ordinary model-visible `view_image` exposure, broker routing, trace/status, and CLI/TUI surfaces. Keep the Milestone 5 `view_image` no-leak tests passing as part of those commands.

**Freeze/review checkpoint:** do not run Phase 2 acceptance until enabled `view_image` exposure, disabled behavior, trace/status facts, and no-leak assertions are reviewed together.

- [ ] Ensure `view_image` overrides generic normalized-arguments audit persistence so runtime-authored audit metadata excludes concrete query text, raw query argument, query preview, and query length.
- [ ] Ensure runtime-authored persisted audit metadata, trace output, engine log entries, context snapshot metadata, and `ToolResult.metadata` record only `effective_query_source`.
- [ ] Ensure image bytes, base64, and provider image content parts never appear in ordinary conversation, run events, context snapshots, trace, engine log, ordinary tool output, or `ToolResult.metadata`.
- [ ] Keep the Milestone 5 pre-read audit-order tests passing while adding redaction and trace rendering.
- [ ] Remove the internal activation gate for sessions whose frozen multimodal config has `view_image_enabled = true`; expose enabled `view_image` in `ModelContextFrame.tool_schema_bindings`, ordinary model-visible tool lists, and broker routing only after the redaction checks above are implemented.
- [ ] Render `view_image` source display paths, image metadata, provider/model, duration, status, error class, effective query source, projected request size when available, and analysis summary.
- [ ] Do not introduce `view_image_*` event kinds; derive trace from existing `tool_call_*` events.
- [ ] Make `/tools` list `todo` and list `view_image` only when enabled.
- [ ] Show a concise no-secret disabled reason through `/tools` or status when `view_image` is disabled. Prefer status if keeping `/tools` strictly model-visible.
- [ ] Ensure approval prompts for `view_image(paths=[...])` display readable path targets.
- [ ] If TUI Todo Plan summary is implemented, render `[o]`, `[>]`, and `[ ]` markers from persisted plan state only.
- [ ] Run canonical unit tests.
  - Command: `uv run pytest tests/unit -v`
  - Expected: all unit tests pass.
- [ ] Run canonical integration tests.
  - Command: `uv run pytest tests/integration -v`
  - Expected: all integration tests pass.

Freeze/review checkpoint: users can inspect Phase 2 continuity and image-analysis facts without leaking raw images, base64, or concrete query text, and enabled `view_image` is model-visible only for sessions with valid frozen multimodal config.

## Milestone 7: Integration Sweep And Phase 2 Acceptance

**Objective:** verify Phase 2 as one coherent runtime slice.

**Files:**

- Modify tests only as needed to close coverage gaps discovered in earlier milestones.
- Test: all Phase 2 unit and integration tests.
- Test: existing unit and integration suites for regression coverage.

**Deliverables:** full Phase 2 acceptance evidence, manual TTY verification notes, dependency lock confirmation, and explicit confirmation that no forbidden future-phase behavior was introduced.

**Modified boundaries:** tests and verification records only, unless a failing acceptance check exposes a Phase 2 implementation bug that must be fixed in the owning milestone boundary before acceptance is retried.

**Invariants:** no new scope is added during acceptance cleanup; automated tests remain deterministic and network-free; manual provider smoke is optional and never replaces fake-provider acceptance; no Phase 3+ behavior is exposed.

**Verification steps:** run the canonical Phase 2 commands from `docs/phase-2/operations.md`, then perform and record the required manual TTY checks.

**Freeze/review checkpoint:** Phase 2 may enter code review/approval only after all automated acceptance commands pass or any unrun check is explicitly recorded with the remaining risk.

- [ ] Run all narrow Phase 2 unit tests.
  - Command: `uv run pytest tests/unit -v`
  - Expected: all unit tests pass.
- [ ] Run all integration tests.
  - Command: `uv run pytest tests/integration -v`
  - Expected: all integration tests pass.
- [ ] Run full canonical suite for Phase 2 acceptance.
  - Command: `uv run pytest -v`
  - Expected: all tests pass.
- [ ] Manually verify TTY behavior required by `docs/phase-2/operations.md`:
  - `/tools` visibility for `todo` and enabled/disabled `view_image`.
  - disabled `view_image` no-secret reason via `/tools` or status.
  - inline approval prompt for `view_image`.
  - denial returns to prompt input without terminalizing the session.
  - optional Todo Plan TUI summary, if implemented.
- [ ] Record manual verification notes with terminal application, command sequence, expected result, observed result, and known limitations.
- [ ] Confirm `uv lock` has been run after dependency changes and `uv.lock` is included with dependency edits.
- [ ] Confirm no Phase 2 implementation introduced forbidden future-phase features.

Freeze/review checkpoint: Phase 2 satisfies `docs/phase-2/tests.md` acceptance and can move to implementation review/approval.

## Self-Review Checklist For Implementers

- [ ] Every new model-visible tool is brokered.
- [ ] Every new runtime truth change is covered by `PRAGMA user_version = 2`.
- [ ] Legacy DB handling reads only `PRAGMA user_version` before failing closed.
- [ ] Todo Plan is never reconstructed from natural-language summaries.
- [ ] `runtime_todo_plan` is always injected into ordinary task frames, including empty state.
- [ ] `view_image` is unavailable unless frozen multimodal config is complete and valid at startup.
- [ ] `view_image` approval scope is path-only and provider egress is governed by enabled frozen multimodal config.
- [ ] `view_image` never persists image bytes, base64, provider image parts, or runtime-authored concrete query text.
- [ ] Automated tests use fake/stubbed vision providers only.
- [ ] `docs/phase-2/operations.md` canonical commands were used for verification.

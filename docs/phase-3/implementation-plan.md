# Phase 3 Implementation Plan

**Goal:** Deliver Phase 3 session and failure control as a light runtime control plane: normalized errors, durable accepted conversation truth, terminal recovery checkpoints, same-lineage explicit resume, running/idle cancellation behavior, user-confirmed stale fail-close, narrow retry, and shell timeout cleanup.

**Architecture:** Phase 3 changes runtime truth and recovery semantics. It does not add a workflow engine, background task system, subagents, MCP, plugin packaging, PTY shell, long-running shell runtime, token-level resume, tool-mid-flight resume, or generic step retry. The implementation order below converts the Phase 3 contract into small, reviewable, rollback-safe patches.

**Tech Stack:** Python, SQLite, local filesystem artifacts, ToolBroker, prompt_toolkit/rich REPL from earlier phases, LangChain-compatible adapter boundary, OpenAI-compatible vision client boundary, pytest, uv.

---

## Freeze Decision

Phase 3 documentation is frozen for implementation as of this decision.

Frozen document set:

- `docs/phase-3/scope.md`
- `docs/phase-3/architecture.md`
- `docs/phase-3/specs/*`
- `docs/phase-3/tests.md`
- `docs/phase-3/operations.md`
- `docs/phase-3/implementation-plan.md`

Authoritative sources:

1. `docs/project-contract.md`
2. `docs/phase-3/*`
3. accepted `docs/adr/*`

If implementation discovers a conflict or missing contract, stop and request a contract patch before continuing. Do not reinterpret the contract from existing code or from `docs/project-plan.md`.

## Goals

- Initialize fresh Phase 3 runtime databases with `PRAGMA user_version = 3`, perform startup-only destructive reset for legacy Phase 0/0.5/1/2 runtime databases, and make `status`, `trace`, `resume`, startup, and active ownership checks validate schema version before reading runtime truth.
- Centralize normalized error classes, reason symbols, recoverability defaults, model-visible projections, failure event payload shape, and semantic CLI exit-code mapping.
- Add append-only durable `conversation_messages` and mutable current projection state as accepted conversation truth, with deterministic group completeness, checksum, fact-cut, projection, and artifact validation.
- Make `terminal_recovery` the only Phase 3 prompt checkpoint kind and the only resume entrypoint; stop writing ordinary turn/context/error/stream/UI/trace checkpoints or context snapshots for Phase 3 prompt sessions/runs.
- Implement explicit `debug-agent resume <session_id>` as the only path that can revive eligible terminalized prompt sessions/runs to `running`, preserving the same `session_id` and `run_id`.
- Unify one-shot and REPL prompt lifecycle so both produce the same durable conversation, terminal checkpoint, Todo Plan, approval, active skill, failure, ownership, and resume eligibility shape.
- Implement running turn interruption, provider cancellation, active shell best-effort termination, idle terminalization, and ownership release without treating provider/tool/shell mid-flight state as resumable truth.
- Implement user-confirmed stale running session fail-close with same-host stale proof and `owner_token` fencing.
- Implement narrow opt-in runtime retry and text-only `output_token_limit_reached` continuation.
- Split shell default timeout, shell maximum timeout, and cancellation cleanup envelope in frozen execution config.

## Non-Goals

- No `/cancel` command.
- No non-terminal attach.
- No startup/config/schema failure resume.
- No automatic stale ownership cleanup without user confirmation.
- No auto attach, auto resume, or unconfirmed active ownership release.
- No generic step-level retry.
- No default runtime-level automatic tool retry.
- No replay of accepted or completed model-call results.
- No token-level, provider-mid-flight, tool-mid-flight, shell-mid-flight, or subagent-mid-thought resume.
- No workflow runtime, workflow handoff, background task system, subagents, MCP, plugin packaging, PTY shell, interactive terminal, or long-running shell runtime.
- No shader-specific runtime validators, RenderDoc command allowlists, Ralph Loop state machines, or business report schemas.
- No compatibility reader, migration, rewrite, or reinterpretation path for Phase 0/0.5/1/2 runtime truth.
- No recovery from event replay, trace rendering, TUI state, stream observations, natural-language summaries, legacy context snapshots, or current disk config/skill/policy files.

## Modification Boundaries

Allowed Phase 3 modification boundaries:

- `src/debug_agent/cli/`: command routing, semantic exit codes, `resume`, REPL/TUI/plain interrupt and terminalization flow, confirmation prompts, status/trace command behavior.
- `src/debug_agent/runtime/`: orchestrator lifecycle, prompt execution boundaries, model context/projection integration, cancellation workers, retry controller, config parsing, shell timeout config, workspace ownership coordination.
- `src/debug_agent/persistence/`: SQLite schema/bootstrap, session/run transitions, events, checkpoints, artifacts, approval grants, Todo Plan restore, skills references, normalized error helpers, new conversation store.
- `src/debug_agent/tools/`: ToolBroker error normalization, shell active-process registration/cancellation, shell timeout validation, `todo` and `view_image` Phase 3 validation/error remapping, disabled `view_image` behavior.
- `src/debug_agent/adapters/`: main model adapter capability audit, removal of placeholder public adapter cancellation API, runtime-owned cancellable worker integration, provider stop metadata, async/cancellable vision provider path.
- `src/debug_agent/observability/`: read-only status/trace rendering for normalized errors, durable conversation summaries, checkpoints, retry, cancellation, resume, and stale fail-close.
- `tests/unit/` and `tests/integration/`: Phase 3 coverage required by `docs/phase-3/tests.md`.

Forbidden or restricted boundaries:

- Do not modify `docs/project-contract.md`, `docs/phase-3/scope.md`, `docs/phase-3/specs/*`, `docs/phase-3/tests.md`, or `docs/phase-3/operations.md` to match implementation drift without human approval.
- Do not add deferred modules listed in the project contract.
- Do not alter model-visible tool routing so any tool bypasses `ToolBroker`.
- Do not introduce new tool risk categories, lifecycle statuses, terminal reasons, checkpoint kinds, error classes, or retry strategies without a contract patch.
- Do not make status, trace, TUI, streaming observation, event replay, context snapshots, or artifact files authoritative recovery inputs.
- Do not broaden `view_image`, `todo`, shell, approval, path policy, skill, or provider behavior beyond the Phase 3 specs.
- Do not add dependency declarations unless implementation evidence requires them; if dependencies change, run `uv lock` in the same milestone.

Compatibility that must be preserved:

- Phase 1/2 ToolBroker, path policy, approval, Todo Plan, active skill, `view_image`, one-shot, REPL, TUI, status, trace, artifact, and compression behavior must continue unless Phase 3 explicitly changes their runtime truth or error/checkpoint contract.
- `AgentLoopAdapter.run()` remains the authoritative result path; `stream()` remains UI observation. Phase 3 may wrap provider calls in runtime-owned workers but must not require a new public async adapter method.
- `view_image` remains a brokered tool and inherits Phase 2 redaction rules.
- Todo Plan remains run-scoped runtime truth and is restored only because resume revives the same run lineage.
- Shell execution remains structured, non-PTY, non-interactive, and short-lived.

## Global Invariants

- Runtime state is authoritative. Durable recovery truth lives in SQLite runtime rows, terminal recovery checkpoints, durable conversation rows, Todo Plan state, approval records, frozen snapshots, and artifact records.
- All model-visible tools pass through `ToolBroker`.
- Events are audit facts, not event-sourcing inputs.
- Trace, status, streaming observations, TUI state, and natural-language summaries are not resume truth.
- Phase 3 prompt sessions/runs write only `terminal_recovery` checkpoints.
- `latest_checkpoint_id` points only to the latest terminal recovery checkpoint for Phase 3 prompt sessions/runs.
- Startup/config/schema failures are non-resumable and must not receive terminal recovery checkpoints.
- Explicit `debug-agent resume <session_id>` is the only path that may transition a terminalized prompt session/run back to `running`.
- Resume is same-lineage: keep the same `session_id` and `run_id`.
- Running `Ctrl+C` cancels a turn; it does not terminalize, write a terminal checkpoint, or release active ownership.
- Idle terminalization writes terminal facts before releasing ownership.
- Active ownership claim, release, resume reclaim, and stale fail-close use `owner_token` fencing.
- Provider cancellation is best-effort local runtime cancellation only. Runtime must not claim remote execution or billing stopped.
- Provider/tool/shell mid-flight state is never resumable truth.
- Retry decisions come only from the central retry registry and do not own final failure handling.
- Keep the repository importable, testable, and capable of starting or failing closed after every milestone.

## Dependency Graph

```text
Phase 3 schema gate + normalized errors + semantic exit codes
  -> durable conversation store + projection state
    -> terminal recovery checkpoint writer/validator + non-resumable startup failure marker
      -> one-shot/REPL lifecycle unification
        -> explicit same-lineage resume

Phase 3 schema gate + normalized errors
  -> provider capability audit + cancellable workers
    -> running cancellation + active shell termination
      -> idle terminalization + ownership release consistency

terminal recovery checkpoint writer/validator + ownership release fencing
  -> stale fail-close
    -> stale-target resume pre-step

schema/config foundation + minimal frozen execution timeout config
  -> terminal checkpoint frozen references
    -> shell timeout behavior cleanup + frozen tool schema rendering

normalized errors + durable conversation + provider stop metadata
  -> retry controller + output-token continuation

all completed branches
  -> status/trace observability + integration acceptance + manual verification
```

Edges are implementation dependencies, not broad feature categories. Later stages may add tests for earlier behavior, but a later stage must not require unfinished future behavior for compilation, test execution, main startup, or fail-closed command behavior.

## Execution Stages

The stages below are ordered by dependency. Each stage is an incrementally safe patch boundary:

- The repository must compile/import.
- The canonical verification command for the changed surface must run, or the checkpoint must record exactly why it could not run.
- User-facing one-shot/REPL/TUI paths must either use a completed Phase 3 path or fail closed before accepting prompt work.
- No stage may expose a half-wired durable truth shape that later code would interpret as complete.
- Stop at the freeze/review checkpoint before starting the next dependent stage.

### Dual-Path Development Gate

Milestones 1 through 4 replace runtime-truth schema, failure facts, durable conversation, terminal checkpoints, and prompt lifecycle shape. During these milestones, fresh user-facing Phase 3 startup must remain behind an internal development/test gate until the minimum runnable Phase 3 slice can:

1. create a fresh Phase 3 database,
2. append accepted durable conversation rows,
3. terminalize eligible idle prompt sessions with a valid `terminal_recovery` checkpoint,
4. reject startup/config/schema failure sessions as non-resumable,
5. keep one-shot and REPL prompt lifecycle using the same durable truth boundary.

Before that point, default one-shot, plain REPL, and TUI entrypoints must either keep the last completed runnable path intact or fail closed before accepting a prompt with a clear milestone-gated error. They must not create long-lived `user_version = 3` sessions with incomplete conversation/checkpoint/config/ownership shape.

After Milestone 4, the broad gate is removed for fresh Phase 3 workspaces. Narrower internal gates may still hide incomplete cancellation, resume, stale fail-close, retry, or shell-timeout behavior when exposing them would violate downstream contracts.

## Milestone 1: Phase 3 Schema, Compatibility, And Error Foundation

**Objective:** establish the Phase 3 database compatibility gate and normalized failure contract before runtime can write new recovery truth.

**Deliverables:** `PHASE_3_SCHEMA_USER_VERSION = 3`, startup-only legacy DB reset, non-destructive read-only/recovery command gates, centralized normalized errors, semantic exit codes, failure event `payload.error`, and Phase 3 tool validation remapping.

**Modified boundaries:** SQLite bootstrap, schema-version checks, CLI command boundary, normalized error helpers, EventStore failure payloads, ToolBroker validation/error normalization, status/trace schema gate ordering.

**Invariants:** schema version is checked before runtime truth reads; startup reset deletes only legacy `.sessions/runtime.db`; `status`, `trace`, and `resume` never reset or create a missing DB; call sites cannot invent error class/reason symbols; malformed/local invalid `todo` and `view_image` calls map to `tool_error/tool_schema_invalid`.

**Verification steps:** run `uv run pytest tests/unit -v` and targeted integration tests for startup/status/trace/resume schema behavior once available; run `uv run pytest tests/integration -v` if CLI compatibility paths change.

**Freeze/review checkpoint:** do not implement `conversation_messages` until Phase 3 schema identity, reset/fail-closed behavior, error registry validation, CLI exit mapping, and failure event shape are reviewed.

**Stop conditions:** stop if implementation requires migrating, interpreting, or rewriting legacy rows; stop if any needed error class/reason is absent from `docs/phase-3/specs/errors.md`.

**Runnable state:** fresh Phase 3 persistence can initialize; legacy handling is deterministic; incomplete Phase 3 prompt execution remains gated.

- [x] Define `PHASE_3_SCHEMA_USER_VERSION = 3`.
- [x] Write `PRAGMA user_version = 3` for fresh Phase 3 databases before runtime truth is interpreted.
- [x] Implement startup-only destructive reset for missing/`0` or Phase 0/0.5/1/2 `.sessions/runtime.db` before reading any legacy rows.
- [x] Ensure startup reset deletes only `.sessions/runtime.db` and does not reference orphaned legacy artifacts/logs/traces/checkpoint payloads.
- [x] Ensure unknown future schema versions always fail closed and are never deleted.
- [x] Ensure `status`, `trace`, and `resume` never create a missing runtime DB and never delete an existing DB.
- [x] Ensure startup, active ownership checks, `status`, `trace`, and `resume` validate schema version before session/run/event/checkpoint/artifact/Todo/ownership reads.
- [x] Add user-facing startup reset guidance and read-only/recovery fail-closed guidance required by `scope.md`.
- [x] Add centralized normalized error classes, reason registry, constructor validation, recoverability defaults, and model-visible projection helpers.
- [x] Add semantic CLI exit-code constants and route one-shot, REPL, `resume`, `status`, and `trace` boundaries through the Phase 3 dispatch order.
- [x] Update failure-class events to persist normalized error objects at `payload.error`.
- [x] Remap model-visible tool argument and local semantic validation failures for `todo` and `view_image` to `tool_error/tool_schema_invalid`.
- [x] Preserve disabled `view_image` behavior: malformed disabled calls validate first; valid calls against frozen disabled availability return `config_error/tool_unavailable`; unknown tools remain `tool_error/unknown_tool`.
- [x] Audit model factory, adapter construction, provider config, approval provider, context/compression, and old top-level error paths for unnormalized errors.
- [x] Add unit tests for fresh DB user version, startup legacy reset, read-only/recovery fail-closed behavior, error registry rejection, CLI exit mapping, `payload.error`, and tool validation remapping.
- [x] Run canonical verification.

## Milestone 2: Durable Conversation Store And Projection State

**Objective:** introduce append-only accepted conversation truth and mutable current projection state before checkpoint/resume behavior depends on it.

**Deliverables:** `ConversationStore`, append-only closed-group durable rows, projection state, atomic append + projection update, checksum canonicalization, fact-cut validation, projection validation, artifact-backed content validation, and prompt executor integration for accepted user/assistant/tool/failure/cancellation/context-summary messages.

**Modified boundaries:** SQLite schema, new conversation persistence module, Prompt Agent Runtime append boundaries, ContextManager/PromptComposer projection integration, ArtifactStore references, status/trace read helpers.

**Invariants:** only closed accepted groups are visible as durable conversation truth; open/staging groups are invisible to projection/checkpoint/resume/status/trace; stream deltas, partial provider output, pending tool/shell state, approval drafts, and TUI state are never appended; append commits message indexes and projection update atomically.

**Verification steps:** run `uv run pytest tests/unit -v`; add integration coverage for ordinary one-shot/REPL durable conversation append once the lifecycle gate allows it.

**Freeze/review checkpoint:** do not write terminal checkpoints until closed-group validation, fact cuts, projection snapshots, append atomicity, and checksum canonicalization are reviewed.

**Stop conditions:** stop if any design uses event replay, trace, UI state, context snapshots, stream deltas, or checkpoint-inlined full conversation as the primary durable conversation path.

**Runnable state:** ordinary prompt execution in the gated path can append accepted durable messages and maintain current projection state; existing user-facing paths remain runnable or fail closed before accepting prompt work.

- [x] Add `conversation_messages` logical schema with required Phase 3 fields or equivalent normalized tables.
- [x] Add `conversation_projection_state` logical schema with one mutable current projection state per prompt run.
- [x] Implement canonical JSON byte serialization and checksum helpers shared by conversation, projection, checkpoints, Todo Plan, and approval grant cuts.
- [x] Enforce one canonical content source per row: inline `content_json` or artifact-backed `artifact_id`.
- [x] Enforce 1-based per-run `message_index`.
- [x] Enforce explicit `message_group_id`, `model_call_id`, 0-based `group_position`, `group_status = "closed"`, and deterministic group completeness source.
- [x] Ensure accepted durable truth never exposes `group_status = "open"`.
- [x] Implement atomic accepted-message append with per-run message index allocation, row insertion, content checksum, and projection-state update.
- [x] Ensure failed appends leave no message index gaps, half-inserted groups, or projection references to uncommitted rows.
- [x] Implement conversation fact-cut validation, including closed groups, contiguous positions, row counts, tool-call/tool-result pairing, checksums, and artifact references.
- [x] Implement projection snapshot validation using ordered refs and referenced `content_sha256` values.
- [x] Implement empty fact-cut and empty projection checksum support for allowed zero-message idle terminalization.
- [x] Append accepted `user_input`, final `assistant_output`, complete `assistant_tool_call`, accepted `tool_result`, runtime `failure_fact`, runtime `cancellation_fact`, and accepted `context_summary` only at documented boundaries.
- [x] Convert Phase 1 compression/omission continuity so resumable context summaries are durable conversation rows and current projection updates, not Phase 3 context checkpoints.
- [x] Make ordinary runtime drift between process-local conversation, projection state, and durable rows fail closed outside explicit resume.
- [x] Add unit tests for closed group append, invalid open group rejection, duplicate/truncated group rejection, tool-call pairing, artifact-backed checksum validation, append atomicity, empty checksums, projection validation, UTF-8 preservation, and drift fail-closed behavior.
- [x] Run canonical verification.

## Milestone 3: Terminal Recovery Checkpoints And Terminalization Foundation

**Objective:** make terminal recovery checkpoints the only Phase 3 prompt-session resume entrypoints.

**Deliverables:** terminal recovery checkpoint manifest writer/validator, rejection of non-terminal checkpoint writes, `latest_checkpoint_id` narrowed to terminal recovery, terminalization consistency boundary, zero-message checkpoint paths, non-resumable startup/config/schema failure marker, minimal frozen execution timeout config needed by checkpoint tool-availability references, and removal/disablement of Phase 3 prompt context snapshot/checkpoint write paths.

**Modified boundaries:** CheckpointStore, SessionStore/RunStore terminal transitions, context snapshot write callers, TodoPlanStore snapshot read/validation, approval grant cut validation, active skill/runtime snapshot references, frozen config/policy/tool availability references, execution config parsing/snapshot defaults needed for frozen references, artifact validation.

**Invariants:** Phase 3 prompt sessions/runs write only `terminal_recovery`; startup/config/schema failures never get terminal recovery checkpoints; `latest_checkpoint_id` is unset when no valid terminal recovery checkpoint exists; terminal checkpoint creation, terminal status, and latest checkpoint update are one consistency boundary.

**Verification steps:** run `uv run pytest tests/unit -v`; run `uv run pytest tests/integration -v` when terminalization/startup failure paths are wired into CLI lifecycle.

**Freeze/review checkpoint:** do not implement explicit resume until terminal checkpoint validation can reject missing, wrong-kind, checksum-invalid, non-prompt, startup-failure, and invalid durable-conversation states.

**Stop conditions:** stop if implementation needs ordinary turn/context/error checkpoints or current context snapshots as resume entrypoints; stop if recovery manifest validation would require reading current config, policy, skill files, provider env, or trace output.

**Runnable state:** eligible idle prompt sessions in the gated path can terminalize with terminal recovery checkpoints; non-resumable terminalization remains explicit and observable.

- [x] Restrict Phase 3 prompt checkpoint writes to `terminal_recovery`.
- [x] Reject or remove ordinary turn, context, error, stream, trace, UI, and non-terminal provenance checkpoint writes for Phase 3 prompt sessions/runs.
- [x] Stop Phase 3 prompt-session writes to `context_snapshots`; retain legacy table only as non-authoritative incompatible/unused schema surface if needed by existing code until removed.
- [x] Narrow `sessions.latest_checkpoint_id` and `runs.latest_checkpoint_id` semantics to terminal recovery checkpoint ids.
- [x] Add terminal recovery manifest creation with session/run identity, run type, terminal status/reason/error matrix, durable conversation fact cut, projection snapshot, Todo Plan snapshot, approval grant cut, active skill records, frozen snapshots, tool availability references, and artifact refs.
- [x] Add manifest and payload checksum validation.
- [x] Add minimal Phase 3 `[execution]` config parsing for `default_shell_timeout_seconds`, `max_shell_timeout_seconds`, and `cancellation_timeout_seconds` with documented defaults.
- [x] Validate Phase 3 execution timeout config as positive integers and `max_shell_timeout_seconds >= default_shell_timeout_seconds`; invalid values use `config_error/invalid_runtime_config`.
- [x] Freeze Phase 3 execution timeout values into the session config snapshot so terminal checkpoints can reference the original shell maximum.
- [x] Validate frozen tool availability references, including `view_image` availability and shell schema limit derived from frozen maximum timeout.
- [x] Limit Milestone 3 shell timeout work to frozen config/reference validation; do not change `shell_exec.timeout_seconds` runtime behavior, approval signatures, or timeout execution semantics until Milestone 9.
- [x] Add terminal reasons `terminal_completion`, `user_exit`, `user_cancel_idle`, `terminal_failure`, and `terminal_stale` exactly as specified.
- [x] Implement zero-message `/exit` and normal graceful shutdown checkpoint shape.
- [x] Reject zero-message checkpoint shape for idle `Ctrl+C` and terminal prompt failure.
- [x] Add structured non-resumable startup/config/schema failure marker on session/run lifecycle or terminal metadata.
- [x] Ensure startup/config/schema failure after session/run creation writes normalized audit facts/events, terminalizes, releases ownership if acquired, leaves `latest_checkpoint_id` unset, and writes no terminal checkpoint.
- [x] Treat terminal checkpoint creation failure as non-resumable: do not set `latest_checkpoint_id` or present terminal-recoverable status.
- [x] Add tests for successful terminal checkpoints, no non-terminal checkpoint writes, zero-message allowed/rejected paths, startup failure non-resumability, checksum validation, minimal frozen execution timeout config validation, frozen reference validation, latest checkpoint semantics, and checkpoint-write failure behavior.
- [x] Run canonical verification.

## Milestone 4: One-Shot/REPL Lifecycle Unification And Development Gate Removal

**Objective:** route one-shot and REPL prompt execution through the same Phase 3 durable conversation, failure, terminal checkpoint, Todo Plan, approval, skill, and ownership lifecycle.

**Deliverables:** shared prompt turn lifecycle, accepted user input append, accepted assistant/tool append, normalized failure/cancellation append boundaries, one-shot terminal completion checkpoint, REPL idle terminalization checkpoint, startup failure non-resumability, and removal of the broad Phase 3 development gate for fresh workspaces.

**Modified boundaries:** Runtime Orchestrator, PromptExecutor, REPL runtime/controller, one-shot CLI path, ToolBroker conversation append integration, ContextManager compression/omission integration, terminalization helpers, status/trace basic Phase 3 reads.

**Invariants:** one-shot and REPL use the same durable truth model; no user-facing prompt path creates incomplete Phase 3 sessions after this milestone; no model-visible resume observation exists; prompt failures before the first closed durable conversation cut are non-resumable; prompt failures after a valid cut may terminalize recoverably only under the terminal checkpoint rules.

**Verification steps:** run `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

**Freeze/review checkpoint:** do not add explicit resume until one-shot and REPL terminalization produce the same validated terminal recovery shape.

**Stop conditions:** stop if one-shot requires a separate recovery model from REPL or if accepted conversation appends cannot be made atomic with prompt lifecycle boundaries.

**Runnable state:** fresh Phase 3 user-facing one-shot/REPL sessions can start, append accepted durable conversation, terminalize eligible sessions, and fail closed for non-resumable startup failures.

- [x] Route one-shot and REPL prompt execution through a shared Phase 3 turn lifecycle.
- [x] Append accepted user input to durable conversation before model execution.
- [x] Append accepted final assistant output only after complete authoritative result.
- [x] Append accepted assistant tool-call messages only after complete tool-call id/name/arguments exist.
- [x] Append accepted tool observations only after ToolBroker returns normalized model-visible output.
- [x] Append runtime failure/cancellation facts only at recovery boundaries.
- [x] Update compression/omission so process-local conversation and projection state are updated together with durable conversation rows.
- [x] Ensure `todo`, active skills, approval state, frozen snapshots, and `view_image` availability stay out of durable conversation and remain dedicated runtime truth.
- [x] Implement one-shot normal completion terminalization with terminal reason `terminal_completion`, status `completed`, no terminal error, and terminal recovery checkpoint.
- [x] Implement `/exit` and normal graceful REPL shutdown terminalization with terminal reason `user_exit`.
- [x] Ensure graceful terminalization releases active ownership only after terminal facts are consistent.
- [x] Keep prompt execution paths fail-closed if terminal checkpoint or durable fact consistency cannot be established.
- [x] Remove the broad Phase 3 development gate for fresh workspaces after the minimum runnable slice is complete.
- [x] Add integration tests for one-shot completion checkpoint, REPL `/exit` checkpoint, zero-message `/exit`, startup failure non-resume marker, durable conversation ordering, Todo Plan independence, approval state preservation, and context summary durability.
- [x] Run canonical verification.

## Milestone 5: Explicit Same-Lineage Resume

**Objective:** implement `debug-agent resume <session_id>` as the only terminal session revival path.

**Deliverables:** CLI `resume`, orchestrated eligibility validation, full checkpoint/durable state validation, active ownership reacquire, same-lineage lifecycle transition, process-local conversation rebuild, Todo Plan current row restore, approval grant restore, active skill/frozen snapshot restore, tool schema restore from frozen availability, and resume audit events.

**Modified boundaries:** CLI main command dispatch, Runtime Orchestrator resume path, SessionStore/RunStore explicit terminal-to-running transition APIs, active ownership claim APIs, ConversationStore restore, TodoPlanStore restore, ApprovalGrantStore restore, skills runtime records, ToolBroker schema rendering, REPL startup from restored context.

**Invariants:** only `debug-agent resume <session_id>` can revive terminalized prompt session/run rows; resume keeps same session/run lineage; resume never creates successor sessions/runs; resume never appends a model-visible observation; resume reads recovery truth only from terminal checkpoint, durable conversation rows, checkpoint-frozen projection, Todo Plan snapshot, approval grants, frozen snapshots, and artifacts.

**Verification steps:** run `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`.

**Freeze/review checkpoint:** do not add stale-target resume pre-step until ordinary resume validation and same-lineage revival are complete and fail-closed.

**Stop conditions:** stop if resume needs event replay, trace rendering, current disk config/policy/skill files, current mutable projection row, or a successor run.

**Runnable state:** eligible terminalized REPL and one-shot prompt sessions can resume into REPL using the same session/run lineage.

- [x] Add `debug-agent resume <session_id>` and usage/lookup error mapping.
- [x] Route resume through Runtime Orchestrator.
- [x] Validate schema before reading runtime truth.
- [x] Perform target preflight: session/run exist, prompt lineage, not startup/config/schema failure.
- [x] Reject non-terminal targets except the later stale-target branch added in Milestone 8.
- [x] Validate terminal lifecycle, `latest_checkpoint_id`, checkpoint kind/schema/checksum, durable conversation fact cut, projection snapshot, Todo Plan snapshot, approval grant cut, active skill snapshot refs, frozen config/policy/tool availability refs, terminal facts, and artifacts.
- [x] Reacquire active workspace ownership with current `pid`, `host_id`, and fresh `owner_token` before lifecycle revival.
- [x] Make ownership claim and lifecycle revival one consistency boundary or an equivalent fenced sequence with cleanup on failure.
- [x] Add store APIs that allow terminal-to-running only from explicit resume orchestration.
- [x] Preserve prior terminal facts and terminal recovery checkpoint rows.
- [x] Write `session_resumed` and `run_resumed` audit events.
- [x] Rebuild process-local conversation from checkpoint-frozen projection snapshot and durable rows.
- [x] Restore the same run's current Todo Plan row from the checkpoint-embedded snapshot without incrementing plan version or writing `todo_updated`.
- [x] Restore approval mode and valid session-scoped grants; do not reactivate `approved_once` grants.
- [x] Restore active skill runtime records and frozen snapshot references without hot reload.
- [x] Restore tool schemas and availability from frozen session facts, including `view_image` and shell maximum timeout.
- [x] Start REPL with restored runtime context and no model-visible resume observation.
- [x] Add tests for REPL resume, one-shot resume into REPL, same lineage, no successor run, startup failure rejection, missing/invalid checkpoint rejection, active ownership conflict, Todo Plan restore, approval grant restore, active skill/frozen config restore, and no model-visible resume append.
- [x] Run canonical verification.

## Milestone 6: Provider Capability Audit And Cancellable Workers

**Objective:** establish runtime-owned cancellable provider execution without changing the public adapter contract.

**Deliverables:** main model and `view_image` provider capability audit, removal of placeholder public `AgentLoopAdapter.cancel(run_id)`, runtime-owned cancellable workers for `run()`/`stream()` provider calls, async/cancellable vision provider execution, late-result ignoring, provider cancellation metadata, and output-token stop metadata extraction.

**Modified boundaries:** `src/debug_agent/adapters/langchain_adapter.py`, `src/debug_agent/adapters/model_factory.py`, `src/debug_agent/adapters/vision_client.py`, Prompt Agent Runtime provider invocation, ToolBroker/runtime cancellation-handle registry, fake provider test seams.

**Invariants:** `run()` remains authoritative; `stream()` remains UI observation; all provider calls run through runtime-owned cancellable workers; sync-only uncancellable provider execution is not an accepted fallback; late provider results after accepted cancellation never become durable conversation or tool results; remote-stop/billing uncertainty is metadata only.

**Verification steps:** run `uv run pytest tests/unit -v`; run integration tests if adapter behavior changes visible prompt/tool loops.

**Freeze/review checkpoint:** do not implement running `Ctrl+C` cancellation until provider and vision calls can close local runtime boundaries under cancellation tests.

**Stop conditions:** stop if the concrete main-model adapter or `view_image` provider path can only run through sync-only uncancellable execution.

**Runnable state:** ordinary successful provider calls still work through existing public adapter methods, and provider paths are ready for runtime cancellation control.

- [x] Audit concrete main model adapter invocation path and document any blocking provider limitations in the milestone checkpoint.
- [x] Audit concrete `view_image` provider invocation path and document any blocking provider limitations in the milestone checkpoint.
- [x] Remove placeholder public `AgentLoopAdapter.cancel(run_id)` from protocol, concrete adapter, and tests that only model future cancellation state.
- [x] Preserve `AgentLoopAdapter.run()` and `AgentLoopAdapter.stream()` public contracts.
- [x] Wrap main model provider calls in runtime-owned cancellable workers or async tasks.
- [x] Ensure streaming fallback to non-streaming provider invocation still runs through a cancellable worker.
- [x] Add async/cancellable `view_image` provider path and cancellation-handle registration.
- [x] Ignore and audit late provider results after cancellation or retry abandonment.
- [x] Ensure stream deltas are presentation-only and not accepted after cancellation.
- [x] Capture provider stop/finish metadata needed for `output_token_limit_reached`.
- [x] Represent provider remote-stop/billing uncertainty only in internal/audit metadata.
- [x] Add fake provider tests for cancellable main model calls, cancellable `view_image`, late-result ignoring, stream delta non-acceptance, uncertainty metadata, and output-token metadata.
- [x] Run canonical verification.

## Milestone 7: Running Cancellation, Active Shell Termination, And Idle Terminalization

**Objective:** implement user interruption semantics for active turns and idle sessions.

**Deliverables:** runtime control states, running `Ctrl+C` cancellation, provider cancellation requests, shell best-effort termination, cancellation cleanup envelope, durable cancellation/failure fact ordering, double interrupt behavior, idle `Ctrl+C` terminalization, `/exit`/graceful shutdown terminalization, and owner-token-fenced ordinary ownership release.

**Modified boundaries:** REPL/TUI/plain controllers, Runtime Orchestrator control path, Prompt Agent Runtime cancellation boundary, ToolBroker active tool tracking, CommandRunner/shell process handles, ConversationStore cancellation append, SessionStore/RunStore terminalization and ownership release.

**Invariants:** running cancellation keeps session/run `running` and ownership held; runtime returns to prompt input only after local provider/tool/shell boundary closes and durable cancellation/failure fact is accepted; shell/provider mid-flight state is not resumable; double `Ctrl+C` exits/aborts with `INTERRUPTED`; idle terminalization writes terminal facts before ownership release.

**Verification steps:** run `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`; record manual TTY verification for running `Ctrl+C`, shell cancellation, idle `Ctrl+C`, double `Ctrl+C`, and TUI terminal summary behavior.

**Freeze/review checkpoint:** do not implement stale fail-close until ordinary terminalization and owner-token-fenced release are complete.

**Stop conditions:** stop if implementation would return to input while provider/tool/shell local boundary remains hidden and uncollected; stop if shell/provider mid-flight state is treated as resumable.

**Runnable state:** interactive session control matches Phase 3 running and idle semantics; active ownership remains consistent across cancellation and terminalization.

- [x] Add transient control states `idle`, `running_turn`, `cancelling`, `terminalizing`, and `resuming` without making them lifecycle truth.
- [x] Parse and freeze `[execution].cancellation_timeout_seconds` with default `10`; invalid values are startup config failures.
- [x] Route running `Ctrl+C` from plain REPL and TUI to runtime cancellation control.
- [x] Request active provider cancellation when a provider call is in flight.
- [x] Register active `shell_exec` subprocess handles only after command start audit and command-start boundary.
- [x] Request active shell best-effort termination on running cancellation.
- [x] Keep pending model/tool/shell state out of durable truth.
- [x] Append cancelled tool observation first when an accepted assistant tool call is already in flight, then append turn-scoped `cancelled/user_cancel_running`.
- [x] Append only turn-scoped `cancelled/user_cancel_running` when no complete assistant tool-call message was accepted.
- [x] Persist provider-boundary `cancelled/model_call_cancelled` as internal/audit detail only, not a separate durable conversation message for running `Ctrl+C`.
- [x] Ensure running cancellation does not write a terminal checkpoint, terminalize, or release active ownership.
- [x] Lock out ordinary prompts, slash commands, and unrelated approval input while `cancelling`.
- [x] Implement double `Ctrl+C` as process-level interruption with `INTERRUPTED`, no partial accepted state, and no prompt return from the same cancelling state.
- [x] Implement idle `Ctrl+C` as session-scoped `cancelled/user_cancel_idle`, terminal reason `user_cancel_idle`, terminal checkpoint when eligible, terminal lifecycle, owner-token-fenced release, and exit.
- [x] Ensure `/exit` and normal graceful shutdown use terminal reason `user_exit`.
- [x] Use owner-token fencing for normal ownership release; record `runtime_error/ownership_release_failed` if release fails after terminalization.
- [x] Add unit/integration tests for running cancellation, no terminalization/release, shell termination request, cancellation timeout envelope, cleanup timeout fail-closed behavior, double interrupt, idle terminalization, `/exit`, and durable conversation ordering.
- [x] Repair main model provider execution to use a runtime-owned async provider primitive shared with `view_image` where practical.
- [x] Repair authoritative main-model `run()` internals to use the configured provider async invocation API, such as `ainvoke`, when available, while preserving the public synchronous adapter API.
- [x] Repair observational main-model `stream()` internals to use the configured provider async streaming API, such as `astream`, when available, while preserving stream deltas as presentation-only.
- [x] Reject sync `invoke()` / `stream()` wrapped in a worker as the accepted concrete main-model fallback when the configured provider exposes usable async APIs.
- [x] Add regression coverage for one-shot/non-stream REPL and TUI/streaming REPL async provider cancellation, including late stream chunks not becoming durable output.
- [x] Run canonical verification and record required manual verification evidence.
  Manual TTY verification recorded 2026-06-08: running `Ctrl+C` in model
  call, model output, `view_image`, long shell/tool, idle `Ctrl+C`, double
  `Ctrl+C` while cancelling, `/exit`, same-lineage resume after idle
  terminalization, repeated resume after post-resume idle terminalization, TUI
  terminal summary with trace/resume guidance, and `/exit`
  `terminal_reason = user_exit` checkpoint/runtime truth all matched
  Milestone 7 expectations.

## Milestone 8: User-Confirmed Stale Fail-Close

**Objective:** add fail-closed recovery for active ownership blocked by a proven-stale owner, without auto-attach or auto-resume.

**Deliverables:** `pid`/`host_id`/`owner_token` ownership facts, host identity provider, stale proof, interactive confirmation, owner-token-fenced stale fail-close transaction, `terminal_stale` terminal facts, `stale_fail_closed` administrative event, non-resumable stale closure, resumable stale closure, and stale-target resume pre-step.

**Modified boundaries:** active ownership store, host identity provider, CLI confirmation flow, Runtime Orchestrator startup/resume blockage handling, CheckpointStore terminalization helpers, SessionStore/RunStore terminal transitions, status/trace stale rendering.

**Invariants:** stale fail-close only runs in user-triggered startup or resume workflows; proof requires same host, absent recorded pid, and captured owner token; confirmation is mandatory; fenced compare-and-swap protects the exact owner record; stale fail-close writes no normalized error fact and no durable conversation failure/cancellation fact.

**Verification steps:** run `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`; record manual confirmation-flow verification.

**Freeze/review checkpoint:** do not finalize acceptance observability until stale fail-close is proven safe for live owner, insufficient proof, confirmation unavailable, token mismatch, resumable closure, non-resumable closure, and stale-target resume.

**Stop conditions:** stop if host identity behavior must differ from `stale-fail-close.md`; stop if a dedicated fail-close command or unconfirmed cleanup path appears necessary.

**Runnable state:** startup/resume blocked by a proven-stale owner can proceed only after confirmation and owner-token-fenced durable closure.

- [x] Persist active ownership `pid`, `host_id`, and `owner_token`.
- [x] Generate a fresh `owner_token` on every startup claim and successful resume reclaim.
- [x] Use `owner_token` fencing for ordinary ownership release.
- [x] Implement the documented `host-v1:sha256(<platform-stable-machine-id>)` host identity provider with fake test seam.
- [x] Prove stale only when recorded host matches current host, recorded pid is absent, and owner token is present and captured.
- [x] Fail closed when host id is unavailable/mismatched, pid is missing/live/unreliably checkable, or token is missing.
- [x] Prompt interactive startup/resume users with blocked owner id and concise stale evidence summary; do not promise resumability.
- [x] Fail closed when confirmation cannot be obtained.
- [x] Implement owner-token-fenced stale fail-close transaction that compares workspace root, session id, run id, host id, pid, and owner token.
- [x] For checkpoint-eligible stale owners, commit terminal checkpoint, terminal status `failed`, terminal reason `terminal_stale`, `latest_checkpoint_id`, `stale_fail_closed`, and ownership release in one fenced transaction.
- [x] For non-resumable stale owners, commit terminal status `failed`, terminal reason `terminal_stale`, `stale_fail_closed`, clear/unset `latest_checkpoint_id`, and ownership release in one fenced transaction.
- [x] Ensure prepared checkpoint payloads outside SQLite are not runtime truth unless the fenced transaction commits a checkpoint row/reference.
- [x] Write exactly the redacted `stale_fail_closed` proof summary and no raw host id, pid, token, process name, command line, confirmation text, or user input details.
- [x] Do not write normalized error facts or durable conversation failure/cancellation facts for stale fail-close.
- [x] Implement stale-target resume pre-step: if `resume <session_id>` targets the current proven-stale active owner, confirm fail-close first, then continue ordinary resume only when a valid terminal checkpoint exists.
- [x] Ensure ordinary startup blocked by stale owner can only create a new session after administrative closure; it must not resume the old stale session.
- [x] Add tests for live owner blocking, insufficient proof, missing token, token mismatch rollback, confirmation unavailable, confirmed stale release, event redaction, non-resumable closure clearing latest checkpoint, resumable stale closure, and stale-target resume.
- [x] Run canonical verification and record required manual verification evidence.
  - Canonical verification: `git diff --check`, `uv run pytest tests/unit -v`,
    and `uv run pytest tests/integration -v` passed on 2026-06-08.
  - Manual stale fail-close confirmation smoke: terminal application was Codex
    PTY exec; command sequence created a temporary stale owner in
    `/private/tmp/myagent-m8-manual/workspace`, then ran
    `HOME=/private/tmp/myagent-m8-manual/home uv run --project /Users/xinzhu/Workspace/MyAgent debug-agent -p "manual stale confirmation"`,
    confirmed the prompt with `y`, and observed `manual stale ok`.
  - Expected result: prompt shows blocked session/run and concise stale evidence,
    confirmed stale owner is terminalized as `terminal_stale`, ownership is
    released, and startup continues into a new one-shot session.
  - Observed result: prompt rendered `sess_manual_stale` / `run_manual_stale`
    with same-host/pid-absent/token-captured evidence; database showed old
    session `failed` with `terminal_stale`, new session `completed`, and
    `stale_fail_closed` payload exactly
    `{"stale_proof_summary": {"host_match": true, "pid_absent": true, "token_fenced": true}}`.

## Milestone 9: Retry Controller And Shell Timeout Cleanup

**Objective:** add narrow runtime retry and replace Phase 1 shell timeout silent capping with Phase 3 frozen default/maximum behavior.

**Deliverables:** central retry registry, `RetryController`, retry attempt/exhaustion audit, `repeat_call` for registered runtime-owned transient failures, text-only `output_token_limit_reached` continuation, late retry result ignoring, shell timeout runtime behavior cleanup, frozen shell schema rendering, explicit timeout validation, and resume restoration of original shell limits.

**Modified boundaries:** new retry policy/runtime module, model adapter/provider result handling, compression model invocation, persistence transaction retry boundaries, Prompt Agent Runtime assistant acceptance, ToolBroker schema rendering, shell handler timeout validation, approval scope signatures, status/trace retry rendering.

**Invariants:** call sites do not duplicate retry budgets or predicates; retry does not own terminalization/checkpoint/ownership decisions; ordinary tools, shell commands, file writes, approvals, accepted model results, and completed tool results are not automatically retried; partial output is not durable conversation until continuation succeeds.

**Verification steps:** run `uv run pytest tests/unit -v`; run `uv run pytest tests/integration -v` when model/tool-loop behavior changes.

**Freeze/review checkpoint:** do not begin final acceptance until retry registry values, continuation safety, and shell timeout contract are covered by deterministic tests.

**Stop conditions:** stop if a needed retry falls outside `repeat_call` or `continue_generation`; stop if continuation would accept partial output or execute tool fragments.

**Runnable state:** runtime retry is bounded, auditable, and narrow; shell timeout behavior matches the frozen Phase 3 config contract.

- [x] Add central `RetrySpec` registry with the exact Phase 3 default rules.
- [x] Validate retry specs and reject invalid enabled/backoff/precondition/strategy combinations.
- [x] Implement `RetryController` precondition evaluation for `none`, `metadata_transient_true`, `text_only_no_tool_fragment`, and `sqlite_no_partial_commit`.
- [x] Implement `repeat_call` only for registered retry-safe runtime-owned transient failures before any response/result/downstream tool execution is accepted.
- [x] Implement late retry-abandoned result ignoring.
- [x] Implement audited attempt and exhaustion metadata.
- [x] Implement `continue_generation` for `model_error/output_token_limit_reached` only when partial output is `text_only_no_tool_fragment`.
- [x] Ensure partial output is transient/audit input only until successful continuation.
- [x] Reject continuation when partial or continuation response contains complete or partial tool-use fragments.
- [x] On successful continuation, append exactly one accepted final `assistant_output` from deterministic `partial_text + continuation_text`.
- [x] Make omitted `shell_exec.timeout_seconds` use the frozen default.
- [x] Make explicit `shell_exec.timeout_seconds` honored exactly when positive and less than or equal to frozen maximum.
- [x] Reject explicit timeout above maximum with `tool_error/tool_schema_invalid`; do not silently cap.
- [x] Render frozen maximum in model-visible `shell_exec` schema.
- [x] Ensure approval grant scope signatures use Phase 3 effective timeout calculation.
- [x] Ensure resume uses original frozen timeout values and schema limit, not current config.
- [x] Add tests for retry registry values, invalid specs, no duplicated budgets, repeat-call safety, late result ignoring, output-token continuation success/failure, tool-fragment rejection, shell timeout defaults/maximum runtime behavior, explicit timeout validation, no silent cap, approval signatures, and resumed schema limits.
- [x] Run canonical verification.

## Milestone 10: Status, Trace, Integration Acceptance, And Manual Verification

**Objective:** complete read-only observability and collect deterministic acceptance evidence for the full Phase 3 slice.

**Deliverables:** Phase 3 status/trace rendering, broad integration tests, manual TTY verification record, and final canonical acceptance command results.

**Modified boundaries:** status query layer, trace writer, CLI presentation, REPL/TUI presentation summaries, integration tests, manual verification record.

**Invariants:** status and trace remain observational; they never repair, revive, migrate, delete, terminalize, infer resumability from events alone, or use raw stale proof details after administrative closure.

**Verification steps:** run `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, and `uv run pytest -v`; complete manual checks required by `docs/phase-3/operations.md`.

**Freeze/review checkpoint:** Phase 3 implementation is not complete until all acceptance criteria in `docs/phase-3/tests.md` are either verified or explicitly reported as unverified with reason.

**Stop conditions:** stop if any acceptance path cannot be verified by canonical commands or documented manual evidence; stop if observability starts acting as recovery truth.

**Runnable state:** Phase 3 acceptance is ready for review using the canonical operations contract.

- [ ] Render normalized error class, reason, message, scope/recoverability where appropriate, and model-visible projection where appropriate.
- [ ] Render terminal checkpoint id, terminal reason, terminal status, and eligibility without treating events as recovery truth.
- [ ] Render durable conversation high-watermark and projection summaries.
- [ ] Render retry attempts and exhaustion metadata.
- [ ] Render cancellation facts and remote-stop/billing uncertainty metadata without claiming remote stop.
- [ ] Render resume attempts and outcomes.
- [ ] Render stale fail-close as administrative `terminal_stale` closure with redacted proof summary and no required `payload.error`.
- [ ] Ensure `status`, `trace`, and `resume` missing-DB behavior matches Phase 3 compatibility contract.
- [ ] Ensure legacy DB startup reset versus read-only fail-closed behavior is covered end-to-end.
- [ ] Add broad integration coverage across schema, normalized errors, durable conversation, terminal checkpoints, one-shot/REPL resume, startup failure non-resumability, cancellation, stale fail-close, retry, and shell timeout.
- [ ] Record manual verification evidence for running `Ctrl+C` in REPL/TUI, active shell cancellation, idle `Ctrl+C`, `debug-agent resume <session_id>`, stale fail-close confirmation, double interrupt while cancelling, and TUI terminal summary after alternate-screen exit.
- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run `uv run pytest -v`.

## Verification Strategy

Verification uses only canonical commands from `docs/phase-3/operations.md`:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use the narrowest command that meaningfully validates the changed behavior. Run `uv run pytest -v` for Phase 3 acceptance or broad cross-module changes.

If dependency declarations change, run:

```bash
uv lock
```

No other lint, type-check, build, formatting, or external service command is canonical for Phase 3 unless repository evidence is reported and humans approve an update to `docs/phase-3/operations.md`.

Automated Phase 3 tests must use fake or stubbed provider behavior. They must not require network access, live API keys, or a real model provider.

Manual verification is required for TTY behavior that automated tests cannot reliably cover. Manual records must include terminal application, command sequence, expected result, observed result, session id, run id, relevant trace/status excerpts, and known limitations.

Each milestone must keep previously completed milestone behavior passing. If verification cannot be run, the checkpoint must state exactly what was not verified and why.

## Migration / Rollback Strategy

Phase 3 is a breaking runtime-truth schema, checkpoint, conversation, event payload, retry metadata, tool-result, and status/control semantics change.

- Fresh `.sessions/runtime.db` files are created with `PRAGMA user_version = 3`.
- Startup paths that will create a new REPL or one-shot session may delete missing/`0` or legacy Phase 0/0.5/1/2 `.sessions/runtime.db` before interpreting rows, then create a fresh Phase 3 DB.
- Startup reset deletes only `.sessions/runtime.db`. Orphaned legacy artifacts/logs/traces/checkpoint payloads/session subdirectories may remain on disk but must not be interpreted or referenced by fresh Phase 3 runtime truth.
- `status`, `trace`, and `resume` are non-destructive. They fail closed for missing/`0`, legacy, unknown future, or mismatched schema versions before reading runtime truth.
- `status`, `trace`, and `resume` do not create `.sessions/runtime.db` when it is missing. `status` returns read-only no-session observation; `trace` and `resume` return lookup-not-found.
- Runtime database rollback is not supported. To run older code or retry from a clean state, move or remove `.sessions/` manually or use a fresh workspace.
- Source rollback is milestone-level rollback: revert the current coherent milestone patch before proceeding to the next dependent milestone.
- If dependency declarations were changed, revert `pyproject.toml` and `uv.lock` together.
- User-facing exposure is rollback-safe by construction: schema/error foundation lands before durable conversation, durable conversation lands before terminal checkpoints, terminal checkpoints land before resume, provider cancellation workers land before interrupt handling, ownership token fencing lands before stale fail-close, and retry/timeout cleanup lands after the core lifecycle is stable.
- If terminal checkpoint creation fails, do not expose resumability or set `latest_checkpoint_id`.
- If ownership release fails after durable terminalization, preserve terminal facts, leave ownership blocked, and require later user-confirmed stale fail-close or manual cleanup.
- If stale fail-close fenced compare-and-swap fails, roll back terminal/checkpoint/event/ownership mutations for that stale proof and keep ownership blocked.

## Completion Definition

Phase 3 implementation is complete only when:

- all milestones are implemented in dependency order or any deviation has explicit human approval;
- all acceptance criteria in `docs/phase-3/tests.md` pass or are explicitly reported as unverified with reason;
- canonical verification commands from `docs/phase-3/operations.md` have been run as applicable;
- required manual verification records exist for TTY-only behavior;
- startup legacy reset and read-only/recovery fail-closed schema behavior are proven;
- startup/config/schema failure sessions are proven non-resumable;
- terminal-checkpoint-backed resume restores eligible REPL and one-shot prompt sessions into REPL using the same session/run lineage;
- no deferred module or future-phase behavior was introduced.

# Phase 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This is an implementation-process instruction only. Phase 4 runtime itself does not implement subagents, workflow, MCP, plugin packaging, PTY shell, long-running shell runtime, or a provider-generic reasoning API.

**Goal:** Deliver Phase 4 RenderDoc Debug Runtime readiness by adding the narrow runtime support required for main-agent thinking, schema version 5 compatibility, non-authoritative run metrics, fake `rdc` automated readiness, manual RenderDoc smoke evidence, and package deployment smoke.

**Architecture:** Phase 4 refines existing runtime, persistence, provider projection, observability, and test surfaces. It keeps RenderDoc procedure knowledge in prompt skills and tests, keeps metrics/thinking output outside runtime truth, and preserves Phase 3.5 durable conversation, terminal checkpoint, ToolBroker, path policy, approval, and trace semantics.

**Tech Stack:** Python, SQLite, local filesystem artifacts, ToolBroker, LangChain-compatible Anthropic/Kimi adapter path, OpenAI-compatible `view_image` boundary, pytest, uv, test-only fake `rdc`.

---

## Freeze Decision

Phase 4 documentation is frozen for implementation planning as of this file.

Authoritative sources:

1. `docs/project-contract.md`
2. `docs/phase-4/*`
3. accepted `docs/adr/*`

If implementation discovers a conflict or missing contract, stop and request a contract patch before continuing. Do not reinterpret the contract from existing code, source drafts, or `docs/project-plan.md`.

## Goals

- Initialize fresh Phase 4 runtime databases with SQLite `PRAGMA user_version = 5`.
- Implement the documented startup-only forward upgrade from Phase 3.5 `user_version = 4` to Phase 4 `user_version = 5`.
- Backfill frozen session config snapshots with disabled default thinking during the v4-to-v5 startup upgrade without rewriting checkpoint payloads, checkpoint checksums, frozen snapshot checksum fields, manifests, active ownership, events, durable conversation, Todo Plan, approval grants, or artifact metadata.
- Add frozen `[thinking]` config with defaults `enabled = false` and `effort = "high"`, including validation and resume no-hot-reload behavior.
- Apply thinking request options only to main-agent model calls when frozen thinking is enabled.
- Strip provider `thinking` content blocks before accepted assistant content enters durable conversation, subsequent model calls, trace, metrics, compression, UI, audit, or final assistant text.
- Normalize provider usage from the existing Anthropic/Kimi-compatible response path and use cumulative token accounting for run metrics and existing REPL/TUI token surfaces.
- Write non-authoritative per-invocation `run_metrics_*.json` files for terminal prompt sessions with deterministic filenames, atomic finalization, and best-effort failure handling.
- Verify RenderDoc runtime readiness through an automated fake `rdc` scenario that uses brokered `shell_exec` and `view_image`.
- Standardize package deployment smoke with `uv build`, `uv tool install`, and installed `debug-agent --help` outside the source checkout.
- Record manual smoke evidence for the externally adapted `renderdoc-gpu-debug` skill and Windows + real `rdc` v1 completion gate.

## Non-Goals

- No generic constants/config refactoring beyond the documented `[thinking]` fields.
- No generic native tool framework expansion.
- No generic engine log, trace, REPL/TUI, or status overhaul.
- No RenderDoc command allowlist, RenderDoc-specific ToolBroker handler, or runtime-owned RenderDoc daemon state.
- No shader-specific runtime validators, report schema validation, Ralph Loop state machine, shader patch loop, or `shader-debug-loop` readiness.
- No workflow runtime, subagent runtime, MCP, plugin integration, tool-call cache, background task system, PTY shell, interactive shell, long-running shell, or background shell runtime.
- No thinking retention, thinking replay, thinking trace artifacts, signed thinking validation, redacted thinking support, or provider-generic reasoning abstraction.
- No use of metrics files, trace files, fake `rdc` output, deployment smoke output, or manual smoke records as runtime truth.
- No legacy `< 4` migration, destructive reset, corrupt database repair, stale fail-close, terminalization, ownership release, metrics rebuild, or trace rebuild as part of the v4-to-v5 upgrade.
- No real RenderDoc capture automation as an ordinary CI requirement.

## Modification Boundaries

Allowed Phase 4 modification boundaries:

- `src/debug_agent/persistence/`: schema version constant, SQLite bootstrap gates, v4-to-v5 startup upgrade, read-only/recovery schema guards, frozen config checksum compatibility, tests for runtime truth preservation.
- `src/debug_agent/runtime/`: config parsing, frozen config defaults/backfill helpers, orchestrator startup ordering, resume frozen-config consumption, per-invocation metrics collector lifecycle, token accounting surfaces.
- `src/debug_agent/adapters/`: Anthropic/Kimi-compatible main-agent request projection, provider response normalization, usage extraction, thinking-block stripping before runtime acceptance.
- `src/debug_agent/tools/`: only compatibility tests or narrow wiring needed to prove `view_image` remains thinking-disabled and fake `rdc` readiness still uses existing brokered `shell_exec`/`view_image` contracts.
- `src/debug_agent/cli/`: user-facing schema compatibility messages, best-effort metrics write warnings, package smoke entrypoint preservation.
- `src/debug_agent/observability/`: metrics writer module or helper, terminalization integration near existing terminal trace refresh, non-authoritative warning/reporting behavior.
- `tests/unit/` and `tests/integration/`: Phase 4 coverage required by `docs/phase-4/tests.md`, including test-only fake `rdc` fixture/helper.
- `docs/phase-4/`: implementation plan and manual smoke evidence records only. Contract/spec changes require human approval.

Forbidden or restricted boundaries:

- Do not modify `docs/project-contract.md`, `docs/phase-4/scope.md`, `docs/phase-4/specs/*`, `docs/phase-4/tests.md`, or `docs/phase-4/operations.md` to match implementation drift without human approval.
- Do not place fake `rdc` under `src/debug_agent` or package metadata.
- Do not route any model-visible tool around ToolBroker, schema validation, path policy, approval, timeout, artifact handling, normalized result projection, or audit.
- Do not add new model-visible runtime tools, tool risk categories, tool result statuses, lifecycle statuses, checkpoint kinds, event kinds, runtime truth tables, or error reason symbols without a contract patch.
- Do not make metrics, thinking text, trace output, events JSONL, TUI state, streaming observations, manual smoke records, or deployment output authoritative recovery inputs.
- Do not change runtime workspace semantics for installed CLI usage.
- Do not change project-local `config.toml` support; it remains unsupported.

Compatibility that must be preserved:

- `AgentLoopAdapter.run()` remains the authoritative result path; `stream()` remains UI observation.
- Runtime state remains authoritative in SQLite runtime rows, durable conversation rows, terminal recovery checkpoints, frozen snapshots, approval records, Todo Plan state, and artifact records.
- Phase 3.5 active owner, session, run, checkpoint, event, durable conversation, Todo Plan, approval grant, and artifact metadata semantics are preserved across the v4-to-v5 upgrade.
- `status`, `trace`, and `resume` do not create, reset, or upgrade runtime databases.
- `view_image` remains brokered, image-only, and keeps its thinking-disabled provider behavior independent of Phase 4 main-agent thinking config.
- Context compression does not receive Phase 4 thinking request options.
- Existing REPL/TUI token surfaces may consume corrected cumulative token accounting but must not gain new layout, control, authority, or runtime truth semantics.
- Installed `debug-agent` operates from the invocation workspace, not from the tool installation directory.

## Global Invariants

- Every accepted milestone leaves the repository importable and testable.
- User-facing one-shot, REPL, TUI, `status`, `trace`, and `resume` entrypoints must either run on a complete path or fail closed before accepting prompt work.
- Schema compatibility gates run before runtime truth interpretation.
- Fresh Phase 4 runtime databases use `PRAGMA user_version = 5`.
- The only allowed Phase 4 upgrade is startup path `user_version = 4` to `5`, limited to schema version update and frozen config thinking-default backfill.
- Corrupt/unreadable databases, missing schema version, `0`, legacy `< 4`, and unknown future `> 5` fail closed.
- Metrics files are non-authoritative evaluation artifacts and are never read for `status`, `trace`, `resume`, checkpoint validation, recovery, or audit truth.
- Thinking content may exist only in transient provider parsing state and must be stripped before runtime accepts assistant content.
- Token totals in metrics and REPL/TUI surfaces are cumulative over the counted model-call window, not last-known provider values.
- All RenderDoc readiness command execution uses existing brokered `shell_exec` and `view_image` paths.
- Manual smoke records are acceptance evidence, not contract or runtime truth.

## Dependency Graph

```text
schema version 5 constants + startup/read-only schema gates
  -> frozen thinking config defaults + v4-to-v5 snapshot backfill
    -> resume config-checksum compatibility for upgraded Phase 3.5 snapshots
      -> main-agent thinking request projection
        -> provider response normalization + thinking-block stripping
          -> durable/subsequent-path no-thinking assertions

provider usage normalizer + deterministic token estimator boundary
  -> cumulative token accounting collector
    -> REPL/TUI token surface correction
    -> terminal run metrics writer

terminal run metrics writer
  -> fake rdc integration readiness evidence

fake rdc integration readiness evidence
  -> automated RenderDoc readiness acceptance

manual adapted skill smoke evidence
  -> manual RenderDoc skill acceptance

Windows + real rdc smoke evidence
  -> v1 completion gate

package entrypoint preservation
  -> deployment smoke

all automated branches + manual gates complete
  -> full Phase 4 acceptance verification
```

Edges are implementation dependencies or acceptance-gate dependencies, not feature categories. Manual adapted-skill smoke and Windows + real `rdc` smoke are independent manual gates unless one recorded run intentionally satisfies both. Later milestones may add tests for earlier behavior, but a later milestone must not require unfinished future behavior for compilation, test execution, main startup, or fail-closed command behavior.

## Verification Strategy

Use only canonical commands from `docs/phase-4/operations.md`:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
uv lock
uv build
uv tool install --force dist/debug_agent-0.1.0-py3-none-any.whl
mkdir -p /tmp/debug-agent-smoke
cd /tmp/debug-agent-smoke
debug-agent --help
```

Use targeted tests during focused milestones. Run `uv run pytest -v` for final Phase 4 automated acceptance or broad cross-module changes. Run `uv lock` only if dependency declarations change; Phase 4 should not need new runtime dependencies.

Milestone verification rules:

- Persistence/config milestones run `uv run pytest tests/unit -v`; when startup, `status`, `trace`, or `resume` behavior changes, also run `uv run pytest tests/integration -v`.
- Provider projection and thinking stripping milestones run `uv run pytest tests/unit -v` with focused adapter/runtime tests, then integration tests when durable conversation or tool continuation paths are affected.
- Token usage and metrics milestones run `uv run pytest tests/unit -v`; terminalization integration runs `uv run pytest tests/integration -v`.
- Fake `rdc` readiness runs under `uv run pytest tests/integration -v` and must not require the external adapted `renderdoc-gpu-debug` skill.
- Deployment smoke runs the complete canonical package smoke after automated tests pass: `uv build`, `uv tool install --force <wheel>`, create a throwaway workspace outside the source checkout, then run installed `debug-agent --help`. Use the single generated wheel path if the versioned filename differs.
- Final acceptance runs `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, `uv run pytest -v`, package deployment smoke, and records the required manual smoke evidence.
- If a verification command cannot be run, stop at the checkpoint and record the exact command, why it was not run, and which contract remains unverified.

## Migration / Rollback Strategy

Phase 4 is a narrow forward-compatible runtime-truth change from Phase 3.5.

- Fresh Phase 4 databases write `PRAGMA user_version = 5`.
- Startup paths that create or start a new prompt session may upgrade an existing Phase 3.5 database with `PRAGMA user_version = 4` to `5`.
- The v4-to-v5 upgrade only backfills missing `sessions.config_snapshot_json.thinking` with `{enabled: false, effort: "high"}` and updates SQLite user version.
- The upgrade must not rewrite terminal recovery checkpoint payloads, `payload_sha256`, frozen snapshot checksum fields, checkpoint manifests, durable conversation rows, Todo Plan rows, approval grants, active ownership, events, runs, artifacts, trace files, or metrics files.
- `status`, `trace`, and `resume` never upgrade, create, reset, delete, or repair runtime databases.
- Legacy `< 4`, missing-version, corrupt/unreadable, and unknown future runtime databases fail closed. The user-facing guidance is to move or delete `.sessions/` or use a fresh workspace, except for the documented v4-to-v5 startup upgrade.
- Rollback after a v5 database exists requires source rollback plus moving/removing `.sessions/` or using a fresh workspace; older code is not expected to interpret Phase 4 v5 runtime truth.
- Source rollback is milestone-level rollback. Revert the current milestone patch before continuing to the next dependent milestone. If dependencies change, revert `pyproject.toml` and `uv.lock` together.
- Metrics rollback is file-level and non-authoritative: removing `run_metrics_*.json` does not alter runtime truth, but it removes Phase 4 readiness evidence for that invocation.
- Manual smoke evidence rollback is documentation/artifact rollback only and must not be used to alter runtime behavior.

## Execution Milestones

The milestones below are ordered by dependency. Each milestone is an incrementally safe patch boundary:

- repository imports and tests remain runnable.
- main entrypoints remain executable through completed runtime paths, or fail closed before accepting prompt work when a required compatibility gate fails.
- no milestone writes partially shaped runtime truth that a later milestone would interpret as complete.
- no milestone relies on future-milestone behavior to pass its own verification.
- stop at each freeze/review checkpoint before starting dependent work.

### Milestone 1: Schema, Frozen Thinking Config, And Resume Compatibility

**Objective:** establish the Phase 4 schema/config foundation in one reviewable patch: schema version `5`, v4-to-v5 startup upgrade, frozen `[thinking]` config, snapshot backfill, and narrow resume checksum compatibility.

**Deliverables:** Phase 4 schema user version constant `5`; fresh database bootstrap with `PRAGMA user_version = 5`; startup-only v4-to-v5 upgrade; fail-closed schema gates; `[thinking]` config parsing/defaults/validation; frozen snapshot persistence; v4-to-v5 default-disabled thinking backfill; no-hot-reload resume behavior; and config checksum fallback for upgraded Phase 3.5 checkpoints.

**Modified boundaries:** `persistence/settings.py`, `persistence/sqlite.py`, session snapshot serialization, checkpoint validation helpers, `runtime/config.py`, frozen-default helpers, startup bootstrap helpers, CLI schema guard messaging, status/trace/resume tests, config tests, persistence upgrade tests, resume validation tests.

**Invariants:** schema version is checked before runtime truth reads; only startup paths may upgrade `4` to `5`; read-only/recovery commands never create, reset, delete, or upgrade `.sessions/runtime.db`; corrupt/unreadable databases are not repaired; invalid thinking config fails before database bootstrap or runtime truth interpretation; v4-to-v5 backfill never reads mutable current config; checkpoint payloads, `payload_sha256`, checkpoint manifests, frozen snapshot checksum fields, active owner, session, run, events, durable conversation, Todo Plan, approval grants, and artifact metadata are preserved.

**Verification steps:** run `uv run pytest tests/unit -v`; run `uv run pytest tests/integration -v` with focused startup/status/trace/resume and resume-compatibility coverage.

**Freeze/review checkpoint:** do not implement model-call thinking projection until schema identity, upgrade preservation, frozen config behavior, and upgraded-checkpoint checksum compatibility are reviewed.

- [x] Define the Phase 4 SQLite schema user version as `5` in the persistence settings owner.
- [x] Ensure fresh Phase 4 database bootstrap writes `PRAGMA user_version = 5`.
- [x] Add an explicit startup-only compatibility branch for existing `PRAGMA user_version = 4`.
- [x] Keep missing schema version, `0`, legacy `1`, `2`, `3`, corrupt/unreadable, and unknown future versions fail-closed.
- [x] Ensure `status`, `trace`, and `resume` with schema version `4` fail closed unless startup has already upgraded the database to `5`.
- [x] Ensure `status`, `trace`, and `resume` with missing database do not create `.sessions/runtime.db`.
- [x] Update user-facing schema guidance for Phase 4 without describing unsupported destructive reset behavior.
- [x] Add unit and integration tests for all schema gate cases.
- [x] Add `thinking.enabled` default `false`, boolean validation, and frozen snapshot persistence.
- [x] Add `thinking.effort` default `"high"`, enum validation for `"low"`, `"medium"`, and `"high"`, and frozen snapshot persistence.
- [x] Reject non-boolean `enabled`, non-string `effort`, and unsupported effort strings as `config_error/invalid_runtime_config`.
- [x] Accept configured `effort` when `enabled = false`.
- [x] Ensure fake-provider and real-provider session snapshots use the same `thinking` object shape.
- [x] Implement v4-to-v5 backfill for existing `sessions.config_snapshot_json` rows missing `thinking`.
- [x] Ensure v4-to-v5 backfill uses disabled defaults and never current mutable `config.toml`.
- [x] Add preservation assertions for checkpoint payloads, `payload_sha256`, checkpoint manifests, frozen snapshot checksum fields, active owner, session, run, event, durable conversation, Todo Plan, approval grant, and artifact metadata rows.
- [x] Prove startup upgrade does not terminalize sessions, release ownership, write metrics, or rebuild trace.
- [x] Add resume tests proving frozen thinking config is restored and current config changes are ignored.
- [x] Add canonical config projection helper that omits only the exact default disabled Phase 4 `thinking` object for fallback validation.
- [x] Validate full Phase 4 frozen config checksum first.
- [x] Retry checksum validation against the fallback projection only when the full checksum fails and `thinking` is exactly `{enabled: false, effort: "high"}`.
- [x] Reject fallback when `thinking.enabled = true`, when `effort` is non-default, or when any other config shape differs.
- [x] Prove fallback does not mutate checkpoint, snapshot, or manifest storage.
- [x] Prove resume uses upgraded frozen disabled thinking after fallback validation succeeds.
- [x] Run canonical verification for the changed surface.

### Milestone 2: Main-Agent Thinking Projection And Response Stripping

**Objective:** enable narrow main-agent thinking request support while guaranteeing thinking content never becomes accepted runtime content.

**Deliverables:** main-agent request projection that sends explicit thinking-enable options and effort only when frozen thinking is enabled; unchanged `view_image` and context-compression projections; response parsing that strips `thinking` blocks while preserving text and tool-use blocks.

**Modified boundaries:** `adapters/langchain_adapter.py`, model request construction, provider response parsing/normalization, prompt executor acceptance boundary, context/compression projection tests, view_image provider tests.

**Invariants:** `effort` alone never enables thinking; disabled thinking sends no thinking options and no effort; thinking applies only to main-agent model calls; stripped thinking text never enters durable conversation, tool continuation messages, future model calls, compression input, terminal checkpoint projection, resume projection, trace, events JSONL, metrics, TUI/REPL display, final assistant text, or audit truth.

**Verification steps:** run `uv run pytest tests/unit -v`; run focused integration tests when tool continuation or durable conversation paths are affected.

**Freeze/review checkpoint:** do not wire metrics/token accounting until provider response normalization and thinking stripping are reviewed.

- [x] Project no thinking options and no effort when frozen `thinking.enabled = false`.
- [x] Project explicit provider thinking enable option and frozen effort when `thinking.enabled = true`.
- [x] Add tests proving `effort` alone is not treated as thinking enabled.
- [x] Preserve accepted text blocks when adjacent thinking blocks are stripped.
- [x] Preserve accepted `tool_use` blocks, ids, names, and arguments when adjacent thinking blocks are stripped.
- [x] Ensure tool calls adjacent to stripped thinking blocks still execute through ToolBroker and continue correctly.
- [x] Ensure durable `conversation_messages` never contain thinking blocks.
- [x] Ensure subsequent model-call tool continuation messages never contain thinking blocks.
- [x] Ensure trace rendering, metrics inputs, compression inputs, TUI/REPL display, assistant final text, and audit outputs never include thinking text.
- [x] Prove `view_image` request projection remains thinking-disabled and ignores Phase 4 main-agent thinking config.
- [x] Prove context compression calls do not include Phase 4 thinking options.
- [x] Ensure thinking-enabled tests do not rely on provider-forced tool choice.
- [x] Run canonical verification for the changed surface.

### Milestone 3: Provider Usage Normalization And Cumulative Token Accounting

**Objective:** create the shared usage accounting foundation required by run metrics and corrected REPL/TUI token surfaces.

**Deliverables:** provider usage normalizer for direct `usage`, `usage_metadata`, and `response_metadata.usage`; cumulative accounting over counted model calls; deterministic estimated fallback for whole counted windows; exclusion of brokered `view_image` provider usage; REPL/TUI token surface correction.

**Modified boundaries:** adapter response metadata normalization, runtime metrics/usage collector helpers, token estimator helpers, stream event/token display state, REPL/TUI token tests.

**Invariants:** `input_tokens`, `output_tokens`, and `total_tokens` are sums across counted calls, not last-known values; if any counted model call lacks provider usage, the whole window uses deterministic estimates; counted calls include main-agent and context-compression calls and exclude `view_image` provider internals; thinking content is never used as estimator input; no separate reasoning/thinking token fields are introduced.

**Verification steps:** run `uv run pytest tests/unit -v`.

**Freeze/review checkpoint:** do not write metrics files until usage normalization, estimator fallback, and REPL/TUI cumulative semantics are reviewed.

- [x] Normalize usage from direct `usage`.
- [x] Normalize usage from `usage_metadata`.
- [x] Normalize usage from `response_metadata.usage`.
- [x] Accept `prompt_tokens`/`completion_tokens` aliases where existing provider response shapes expose them.
- [x] Derive per-call `total_tokens` from input plus output when provider omits total.
- [x] Accumulate provider token totals across all counted model calls in the invocation when every call has usage.
- [x] Switch the whole counted window to deterministic estimates when any counted model call lacks provider usage.
- [x] Estimate input tokens from provider-visible requests and output tokens from accepted provider outputs.
- [x] Tag estimated windows with a stable estimator version.
- [x] Keep `view_image` provider usage out of model-call token totals while leaving `view_image` as a brokered tool for timing/counting.
- [x] Update existing REPL/TUI token surfaces to show cumulative provider or estimated `input_tokens`, `output_tokens`, and `total_tokens`.
- [x] Add tests proving latest context-token estimates are not substituted for cumulative token totals.
- [x] Run canonical verification for the changed surface.

### Milestone 4: Non-Authoritative Run Metrics Writer

**Objective:** write per-invocation metrics artifacts for terminal prompt sessions without changing runtime lifecycle, exit behavior, recovery, or audit truth.

**Deliverables:** in-memory metrics collector lifecycle, UTC millisecond filename generation with deterministic collision suffixes, schema version `1` JSON shape, atomic same-directory finalization, best-effort failure warnings, and terminalization integration near the trace refresh path.

**Modified boundaries:** new or existing observability metrics helper, runtime orchestrator terminalization flow, tool/model observation hooks, CLI/UI warning surface, metrics tests.

**Invariants:** metrics are never stored in SQLite; metrics files are never read by `status`, `trace`, `resume`, checkpoint validation, recovery, or audit truth; write failure does not change terminalization, checkpoint creation, ownership release, run/session status, or exit code; resume writes a separate metrics file for the current invocation and does not read earlier metrics.

**Verification steps:** run `uv run pytest tests/unit -v`; run `uv run pytest tests/integration -v` with terminal prompt-session coverage.

**Freeze/review checkpoint:** do not implement fake `rdc` readiness until metrics files are produced reliably for terminal prompt sessions.

- [x] Initialize an in-memory metrics collector at fresh start invocation.
- [x] Initialize a separate metrics collector at explicit resume invocation.
- [x] Record model-call timing observations for main-agent and context-compression calls.
- [x] Record brokered tool completion observations, including `view_image` as a tool.
- [x] Build metrics schema version `1` with session id, run id, invocation kind, start/end times, timing, token, and tool sections.
- [x] Generate UTC millisecond filenames in `YYYYMMDDTHHMMSS.SSSZ` format.
- [x] Use deterministic `_1`, `_2` suffixes for filename collisions and never overwrite an existing metrics file.
- [x] Write valid UTF-8 JSON through same-directory temporary file plus atomic finalization.
- [x] Exclude `estimated_context_tokens`, `reasoning_tokens`, `thinking_tokens`, and thinking text from metrics.
- [x] Compute tool success/failure counts and breakdown from `ToolResult.status`.
- [x] Represent missing tool timing data through `tool_time_coverage`.
- [x] Inject metrics write failure in tests and prove original terminal outcome and exit code are preserved.
- [x] Prove `status`, `trace`, `resume`, checkpoint validation, and recovery do not read metrics files.
- [x] Run canonical verification for the changed surface.

### Milestone 5: Automated Fake `rdc` Readiness Scenario

**Objective:** prove the runtime can carry the RenderDoc readiness tool path through existing brokered tools without adding RenderDoc semantics to runtime core.

**Deliverables:** test-only fake `rdc` fixture/helper, deterministic sample workspace, scripted model or test skill flow, valid PNG export inspected by `view_image`, terminal session trace and metrics assertions.

**Modified boundaries:** `tests/integration/`, `tests/integration/fixtures/` or equivalent test-only helpers, existing fake/scripted model test seams. Runtime code may change only to fix contract-compliant generic behavior revealed by the test.

**Invariants:** fake `rdc` does not live under `src/debug_agent`; no package metadata includes fake `rdc`; runtime core gains no RenderDoc command allowlist, RenderDoc tool, daemon state, report validator, workflow, subagent, MCP, cache, PTY, or long-running shell behavior.

**Verification steps:** run the focused fake readiness integration test; run `uv run pytest tests/integration -v`.

**Freeze/review checkpoint:** do not claim RenderDoc readiness until fake `rdc` uses brokered `shell_exec` and `view_image`, terminalizes, and produces both trace and metrics evidence.

- [ ] Add a deterministic test-only fake `rdc` helper that can be materialized as `<tmp>/bin/rdc`.
- [ ] Implement fake `rdc doctor` with exit `0`.
- [ ] Implement fake `rdc open sample.rdc` with per-test state under `tmp_path`.
- [ ] Implement fake `rdc info --json` with deterministic capture metadata JSON.
- [ ] Implement fake `rdc draws --limit 20` with deterministic draw output.
- [ ] Implement fake `rdc rt <eid> -o <output.png>` that writes a valid PNG.
- [ ] Implement fake `rdc close` that clears per-test state.
- [ ] Create a temporary workspace with sample `.rdc` input and fake `rdc` first on `PATH`.
- [ ] Drive a prompt session through `rdc doctor`, `open`, `info --json`, `draws --limit 20`, `rt ... -o <png>`, `view_image <png>`, `close`, and final answer.
- [ ] Assert every `rdc` command is observed through brokered `shell_exec` tool results.
- [ ] Assert fake command `cwd` behavior matches workspace expectations.
- [ ] Assert exported PNG exists, is valid, and is inspected through brokered `view_image`.
- [ ] Assert the session terminalizes.
- [ ] Assert `.sessions/<session_id>/logs/trace.md` exists and renders the tool transcript.
- [ ] Assert `.sessions/<session_id>/logs/run_metrics_*.json` exists and includes tool counts.
- [ ] Run canonical verification for the changed surface.

### Milestone 6: Deployment, Manual Smokes, And Final Acceptance

**Objective:** complete the human-operated and broad acceptance gates after all automated runtime branches are implemented.

**Deliverables:** package smoke evidence from canonical `uv build`, `uv tool install`, and installed `debug-agent --help`; manual adapted `renderdoc-gpu-debug` skill smoke record; Windows + real `rdc` smoke record; final automated verification evidence; and implementation review summary.

**Modified boundaries:** `pyproject.toml` only if package metadata is actually wrong; manual evidence files under `docs/phase-4/` or an approved release evidence location; no runtime workspace semantic changes. Runtime code changes in this milestone are prohibited unless a smoke reveals a contract-compliant bug; if so, return to the owning earlier milestone.

**Invariants:** generated `dist/` artifacts are not committed; installed CLI roots `.sessions/`, skill discovery, active ownership, path policy, and artifacts in the invocation workspace; package smoke does not require real provider config or RenderDoc; the external adapted skill is not vendored into `src/debug_agent`; manual records do not modify contract/spec; manual evidence remains non-authoritative.

**Verification steps:** run `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, `uv run pytest -v`, package deployment smoke, and the manual operations from `docs/phase-4/operations.md`.

**Freeze/review checkpoint:** Phase 4 is ready for review only when all automated commands pass and manual gates are recorded, or when the final report explicitly lists unverified gates and why Phase 4/v1 acceptance remains blocked.

- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run `uv run pytest -v`.
- [ ] Run `uv build`.
- [ ] Install the generated wheel with `uv tool install --force`.
- [ ] Create `/tmp/debug-agent-smoke` or equivalent throwaway workspace.
- [ ] Run installed `debug-agent --help` outside the source checkout.
- [ ] If package metadata changes were required, run `uv lock` and verify the lockfile change belongs to that dependency/metadata change.
- [ ] Record package smoke command output summary in the implementation review evidence.
- [ ] Record external adapted skill location/version or content hash.
- [ ] Record how the adapted skill was exposed under project skill discovery.
- [ ] Record evidence that `renderdoc-gpu-debug` was discoverable and activated.
- [ ] Record session id, run id, command sequence, brokered `shell_exec` observations, brokered `view_image` result when an image is produced, trace path, metrics path, observed result, and known limitations.
- [ ] Record Windows version and runner/machine details.
- [ ] Record RenderDoc and `rdc` versions.
- [ ] Record relevant environment variables and PATH notes.
- [ ] Record sample `.rdc` path, real `rdc` command sequence, expected result, observed result, session id, run id, trace path, metrics path, and known limitations.
- [ ] If the same run satisfies adapted skill smoke and Windows + real `rdc` smoke, ensure all fields required by both records are present.
- [ ] Confirm fake `rdc` readiness runs in ordinary integration automation.
- [ ] Confirm manual adapted `renderdoc-gpu-debug` skill smoke record exists.
- [ ] Confirm Windows + real `rdc` smoke record exists for v1 completion.
- [ ] Review `git diff` for scope creep, future-phase behavior, fake `rdc` runtime leakage, docs/spec edits, and formatting churn.
- [ ] Prepare implementation review summary with commands run, evidence paths, and any residual risk.

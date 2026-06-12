# Phase 3.5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This is an implementation-process instruction only. Phase 3.5 runtime itself does not implement subagents, workflow, MCP, plugin packaging, PTY shell, or long-running shell runtime.

**Goal:** Deliver Phase 3.5 runtime ergonomics, native tooling, and audit hardening while preserving Phase 3 recovery truth, ToolBroker authority, frozen config discipline, and non-authoritative UI/trace/log boundaries.

**Architecture:** Phase 3.5 refines existing runtime, persistence, ToolBroker, native tool, and observability modules. It converts the Phase 3.5 contracts into small dependency-ordered patches: settings/config foundation, schema compatibility, shared ToolBroker mechanics, native tool replacement, trace/events observability, and final acceptance hardening.

**Tech Stack:** Python, SQLite, local filesystem artifacts, ToolBroker, prompt_toolkit/rich REPL from earlier phases, LangChain-compatible adapter boundary, OpenAI-compatible `view_image` boundary, ripgrep executable for `search_text`, pytest, uv.

---

## Freeze Decision

Phase 3.5 documentation is frozen for implementation planning as of this file.

Authoritative sources:

1. `docs/project-contract.md`
2. `docs/phase-3.5/*`
3. accepted `docs/adr/*`

Former root-level source drafts have been absorbed into the frozen Phase 3.5
documentation set. They are no longer implementation inputs and may be removed
without changing this plan.

If implementation discovers a conflict or missing contract, stop and request a contract patch before continuing. Do not reinterpret the contract from existing code, removed drafts, or `docs/project-plan.md`.

## Goals

- Centralize built-in constants into the documented directory-level settings modules without making constants configurable unless `docs/phase-3.5/specs/configuration.md` allows it.
- Add frozen `[agent_loop].max_tool_call_iterations` and `[execution].default_tool_timeout_seconds` while preserving Phase 3 startup ordering, resume no-hot-reload behavior, and existing shell/view_image timeout boundaries.
- Initialize fresh Phase 3.5 runtime databases with SQLite `PRAGMA user_version = 4`, implement startup-only legacy reset for `< 4`, and keep `status`, `trace`, and `resume` read-only/recovery paths fail-closed and non-destructive.
- Extend terminal recovery checkpoint validation to `manifest_schema_version = 2` and Phase 3.5 tool-availability facts without introducing per-tool schema hashes, result hashes, or deterministic call/audit signatures.
- Extend ToolBroker schema validation, normalized argument handling, timeout envelope, field-level artifacting, audit argument persistence, and volatile file metadata cache before exposing changed native tool behavior.
- Deliver Phase 3.5 native tool contracts for `find_file`, `read_file`, `list_dir`, `search_text`, `edit_file`, `write_file`, and successful `shell_exec` output, with `search_text` implemented as its own milestone.
- Preserve `view_image` as image-only with Phase 2 ordinary display-path output and query redaction boundaries.
- Preserve `activate_skill`, `load_skill_resource`, and `todo` tool-specific semantics while routing their Phase 3.5 ToolResult envelope/status/error projection through the shared ToolBroker boundary.
- Replace legacy trace/log outputs with `.sessions/<session_id>/logs/trace.md` conversation transcript and `.sessions/<session_id>/logs/events.jsonl`, keeping both non-authoritative.
- Keep one-shot, REPL, TUI, status, trace, resume, checkpoint, artifact, approval, policy, and durable conversation paths runnable or fail-closed at every milestone.

## Non-Goals

- No RenderDoc, `rdc`, Ralph Loop, shader-specific runtime validator, shader report schema, shader trace rule, or business tool.
- No subagents, workflow runtime, background task system, MCP, plugin packaging, hook system, memory system, tool-call cache, OS/container sandbox, PTY shell, interactive shell, background shell, or long-running shell runtime.
- No project-local `config.toml`, config/model/tool hot reload, path policy or shell policy migration into `config.toml`, global unknown-key fail-closed config behavior, or new config fields beyond the Phase 3.5 config spec.
- No raw shell string, `command`, `directory`, `description`, background, interactive, or PTY expansion for `shell_exec`.
- No `view_image` video, audio, URL, base64, artifact-id, or general multimedia input support.
- No legacy runtime-truth migration, compatibility reader, row rewrite, old trace/log symlink/copy, or old tool schema alias such as `search_text.query`.
- No durable file metadata cache, model-visible revision token, `expected_sha256`, deterministic call/audit signature, per-tool schema hash, or per-tool result hash persistence.
- No changes to `activate_skill`, `load_skill_resource`, or `todo` tool-specific schemas, target validation, approval exceptions, persistence semantics, checkpoint facts, or logical result objects.

## Modification Boundaries

Allowed Phase 3.5 modification boundaries:

- `src/debug_agent/runtime/`: settings, config resolution, frozen defaults, orchestrator startup ordering, prompt executor runtime wiring, adapter request config, retry/settings constants, provider/platform constants, policy constant sources.
- `src/debug_agent/persistence/`: settings, SQLite schema version gate, checkpoint manifest version and tool-availability facts, artifact inline threshold constants, read-only schema gate helpers, collision fail-closed behavior.
- `src/debug_agent/tools/`: settings, ToolBroker schema validation/default injection, normalized path handling, approval/audit argument normalization, result envelope/durable serialization, timeout envelope, volatile file metadata cache, native tool handlers, shell output shape, view_image fixed constants.
- `src/debug_agent/observability/`: conversation trace renderer, events JSONL writer, trace path/write atomicity, render validation, non-authoritative log path changes.
- `src/debug_agent/cli/`: CLI trace command path, user-facing legacy reset/fail-closed messages, REPL/TUI/plain display of Phase 3.5 tool results and guard failures, constants moved to `cli/settings.py`.
- `src/debug_agent/adapters/`: `AgentLoopAdapter` loop-bound consumption from frozen config only.
- `tests/unit/` and `tests/integration/`: Phase 3.5 coverage required by `docs/phase-3.5/tests.md`.

Forbidden or restricted boundaries:

- Do not modify `docs/project-contract.md`, `docs/phase-3.5/scope.md`, `docs/phase-3.5/specs/*`, `docs/phase-3.5/tests.md`, or `docs/phase-3.5/operations.md` to match implementation drift without human approval.
- Do not add deferred modules listed in the project contract.
- Do not route any model-visible tool around ToolBroker, path policy, shell policy, approval, timeout, artifact handling, normalized result projection, or audit.
- Do not introduce new runtime truth schema, event kinds, error class/reason symbols, tool result statuses, checkpoint placements, lifecycle statuses, tool risk categories, or state machine semantics without a contract patch.
- Do not make trace, events JSONL, TUI, streaming observation, run events, context snapshots, natural-language summaries, source drafts, or artifact body previews authoritative recovery inputs.
- Do not modify runtime-control tool-specific semantics for `activate_skill`, `load_skill_resource`, or `todo` beyond the shared ToolBroker envelope/status/error projection.

Compatibility that must be preserved:

- `AgentLoopAdapter.run()` remains the authoritative result path; `stream()` remains UI observation.
- Runtime state remains authoritative in SQLite runtime rows, durable conversation rows, checkpoint payloads, frozen snapshots, approval records, Todo Plan state, and artifact records.
- `view_image` remains brokered, image-only, and keeps Phase 2 display-path ordinary output while using canonical paths internally for approval, policy, and audit.
- `shell_exec` remains structured `argv`, `shell=False`, non-PTY, non-interactive, and short-lived.
- Resume uses the original session frozen config snapshot and Phase 3.5 terminal recovery checkpoint facts; it must not rebuild dynamic facts from current mutable config files.

## Global Invariants

- Every accepted milestone leaves the repository importable and testable.
- User-facing one-shot, REPL, TUI, `status`, `trace`, and `resume` entrypoints must either run on a complete path or fail closed before accepting prompt work; they must never write partially shaped Phase 3.5 runtime truth that a later milestone would interpret as complete.
- All model-visible tools pass through ToolBroker.
- Schema compatibility gates run before runtime truth interpretation.
- Invalid Phase 3.5 config fails before database bootstrap, startup legacy reset, session/run creation, active ownership checks, stale fail-close, model calls, or tool calls.
- `PRAGMA user_version = 4` is the Phase 3.5 cross-version runtime database boundary.
- Phase 3.5 startup may delete legacy `.sessions/runtime.db`, `.sessions/runtime.db-wal`, and `.sessions/runtime.db-shm`; read-only/recovery commands never delete or create runtime databases.
- Legacy orphaned `.sessions/` files and directories may remain on disk but must not be interpreted, merged, reused, or referenced by fresh Phase 3.5 truth.
- ToolBroker large-output artifact registration must complete before artifact ids are exposed in `ToolResult`, durable conversation, or audit.
- Successful Phase 3.5 structured native `tool_result` rows must remain inline after documented field-level artifacting, or the tool call returns `tool_error/tool_execution_failed`.
- Volatile file metadata cache is process-local, session-runtime-local, and not resume truth.
- `trace.md` and `events.jsonl` are non-authoritative observability outputs.

## Dependency Graph

```text
settings modules without behavior drift
  -> config loader additions + frozen defaults + adapter/tool timeout consumption
    -> schema version 4 compatibility + checkpoint manifest v2 foundation
      -> ToolBroker schema/default/audit normalization foundation
        -> ToolResult serialization + field-level artifacting foundation
          -> ToolBroker timeout envelope + volatile cache/write-lock foundation
            -> portable glob + read/list/find discovery tools
              -> controlled ripgrep search_text
                -> stale-write edit_file/write_file
                  -> shell_exec/view_image/runtime-control compatibility + final tool availability marker
                    -> conversation trace + events.jsonl observability
                      -> REPL/TUI/status/trace polish + full acceptance verification
```

Additional dependency constraints:

- `search_text` must remain its own implementation milestone because it has distinct traversal, filtering, ripgrep, chunking, output-mode, pagination, and error-mapping risks.
- ToolResult durable serialization and field-level artifacting must be implemented before changed native handlers expose structured outputs.
- ToolBroker timeout/cache/write-lock mechanics must be implemented before read/write handlers depend on them for stale-write and no-partial-success behavior.
- `edit_file` and overwrite `write_file` depend on a working `read_file` whole-file hash cache and ToolBroker stale-write guard.
- `trace.md` conversation rendering depends on Phase 3 durable `conversation_messages` and Phase 3.5 ToolResult status/content serialization.
- The Phase 3.5 native tool contract marker must not be treated as complete until all Phase 3.5 model-visible native schemas, result serialization, and dynamic tool facts are implemented and verified.

## Verification Strategy

Verification uses only canonical commands from `docs/phase-3.5/operations.md`:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use the narrowest command that meaningfully validates the modified behavior. Use `uv run pytest -v` for final Phase 3.5 acceptance or broad cross-module changes. Run `uv lock` only if dependency declarations change; Phase 3.5 should not need new Python dependencies for glob matching.

Milestone verification rules:

- Settings/config/persistence/tool foundation milestones run `uv run pytest tests/unit -v`; when startup/status/trace/resume behavior changes, also run `uv run pytest tests/integration -v` with coverage focused on those paths.
- Native tool milestones run `uv run pytest tests/unit -v` before review, with unit coverage focused on the tool and helper behavior changed by the milestone.
- Search milestone tests must cover both available and missing `rg` behavior without requiring network access.
- Observability milestones run `uv run pytest tests/unit -v`; when CLI `trace`, terminalization, or resume behavior changes, also run `uv run pytest tests/integration -v` with coverage focused on those paths.
- Final acceptance runs `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, and `uv run pytest -v`, followed by the manual checks called out in `docs/phase-3.5/operations.md`.
- If a verification command cannot be run, stop at the checkpoint and record the exact command, why it was not run, and which contract remains unverified.

## Migration / Rollback Strategy

Phase 3.5 is a breaking runtime-truth and tool-contract change with an explicitly approved startup-only destructive reset.

- Fresh Phase 3.5 databases write `PRAGMA user_version = 4`.
- Startup paths that create a new REPL or one-shot session/run delete legacy missing-version, `0`, or `< 4` `.sessions/runtime.db` plus `.sessions/runtime.db-wal` and `.sessions/runtime.db-shm` before interpreting rows.
- Startup does not reset corrupt/unreadable databases or unknown future versions; those fail closed.
- `status`, `trace`, and `resume` never create or reset `.sessions/runtime.db`.
- Legacy rows are not migrated, preserved, rewritten, interpreted, or translated into Phase 3.5 shape.
- Legacy orphaned session, log, trace, checkpoint-payload, temp, and artifact paths may remain on disk. Fresh Phase 3.5 path collision with such orphaned files fails closed instead of deleting or reusing them.
- Runtime database rollback is not supported. To run older code, move or remove `.sessions/` manually or use a fresh workspace, matching user-facing compatibility guidance.
- Source rollback is milestone-level rollback. Revert the current milestone patch before continuing to the next dependent milestone. If dependencies change, revert `pyproject.toml` and `uv.lock` together.
- During gated milestones, accepted user-facing Phase 3.5 prompt sessions must not be persisted until the snapshot/checkpoint/tool-availability shape is complete enough for later Phase 3.5 code to interpret safely.

## Execution Stages

The stages below are ordered by dependency. Each stage is an incrementally safe patch boundary:

- repository imports and tests remain runnable.
- main entrypoints remain executable through the existing Phase 3 prompt path; when an internal Phase 3.5 partial path is incomplete, that partial path fails closed before accepting prompt work.
- no stage exposes a half-wired model-visible tool contract as complete.
- no stage relies on future-stage behavior to pass its own verification.
- stop at each freeze/review checkpoint before starting dependent work.

### Dual-Path Transition Gate

Milestones 1 and 2 are additive or no-behavior-drift foundations and may keep the last completed Phase 3 runtime path working.

Milestones 3 through 9, including Milestones 4A through 4C, replace runtime database compatibility, checkpoint tool-availability facts, model-visible native tool contracts, and observability output paths. During these milestones, the existing Phase 3 prompt path must remain the default user-facing one-shot, plain REPL, and TUI path until the minimum complete Phase 3.5 runtime slice can:

1. resolve Phase 3.5 config before database bootstrap,
2. create or validate a schema version 4 database,
3. freeze `agent_loop`, expanded `execution`, policy, and dynamic tool facts,
4. bind only Phase 3.5-compliant model-visible tools,
5. serialize Phase 3.5 ToolResult envelopes into durable conversation rows,
6. write terminal recovery checkpoints with manifest schema version 2 and valid tool-availability facts,
7. write Phase 3.5 observability outputs to `.sessions/<session_id>/logs/trace.md` and `.sessions/<session_id>/logs/events.jsonl`,
8. avoid generating legacy `.sessions/<session_id>/trace.md` or `.sessions/<session_id>/logs/engine.log`,
9. rebuild manual `debug-agent trace <session_id>` output through the Phase 3.5 conversation trace renderer.

Before that point, default one-shot, plain REPL, and TUI entrypoints must continue to create and run Phase 3-shaped prompt sessions rather than creating long-lived schema 4 sessions with incomplete snapshot, tool, trace, or checkpoint shape. Phase 3.5 implementation may use internal test seams to exercise partial schema 4 components, but those seams must not become a shipped compatibility mode, legacy reader, or user-facing switch. Each milestone must leave the Phase 3 prompt path runnable and the Phase 3.5 partial path either test-only or fail-closed before accepting prompt work.

The schema version 4 bootstrap, read-only/recovery schema gates, Phase 3.5 tool bindings, Phase 3.5 checkpoint validation, and Phase 3.5 trace/events writers introduced during Milestones 3 through 9 are therefore internal Phase 3.5 construction paths until cutover. They may be invoked by focused tests and lower-level integration seams, but they must not be bound to the default CLI dispatcher, default prompt session factory, or default trace/status/resume command routing before Milestone 10. This avoids persisting schema version 4 runtime rows that still have Phase 3-shaped snapshot, tool-result, checkpoint, or observability contracts.

After Milestone 9 completes, trace/events integration verification passes, and the Milestone 9 review checkpoint accepts the evidence, the default user-facing prompt path becomes eligible for a single reviewed cutover from Phase 3 behavior to Phase 3.5 behavior. Milestone 10 performs that cutover for fresh Phase 3.5 workspaces as an explicit reviewed patch by binding the already-verified Phase 3.5 startup/schema, prompt session factory, tool registry, checkpoint, trace/status/resume, and observability paths into the default CLI routing. Narrower test seams may still be used to simulate missing `rg`, timeout, artifact, policy, approval, or provider failures.

## Milestone 1: Settings Modules Without Behavior Drift

**Objective:** centralize existing constants into documented settings modules before changing config, schema, native tool behavior, or trace paths.

**Deliverables:** `runtime/settings.py`, `tools/settings.py`, `cli/settings.py`, and `persistence/settings.py` exist; existing constants move to the correct settings owner; imports compile; defaults and runtime behavior remain unchanged except for import source.

**Modified boundaries:** constant definitions and import sites only.

**Invariants:** no new `config.toml` fields; no schema version change; no model-visible tool schema change; no checkpoint payload change; no trace/log path change; no duplicate long-term compatibility shims or stale copied constants.

**Verification steps:** run settings/import smoke tests and `uv run pytest tests/unit -v`.

**Freeze/review checkpoint:** do not add Phase 3.5 config fields until settings ownership, comments, and no-behavior-drift tests are reviewed.

- [x] Create `src/debug_agent/runtime/settings.py` with main model, context, execution, development, agent loop, retry, token estimator, policy, provider execution, platform, prompt, and runtime ordering constants.
- [x] Create `src/debug_agent/tools/settings.py` with native tool pagination constants, ToolBroker internal limits, fixed `view_image` defaults and image/request limits.
- [x] Create `src/debug_agent/cli/settings.py` with REPL/TUI presentation, flush, scroll, and preview constants.
- [x] Create or update `src/debug_agent/persistence/settings.py` with SQLite schema, legacy schema, checkpoint manifest, and inline/artifact threshold constants.
- [x] Move constants from existing modules to settings imports while preserving current behavior for this milestone.
- [x] Add short adjacent comments for moved constants that explain their contract boundary or operational role.
- [x] Update tests that import old constants to import from the new settings owners.
- [x] Add import smoke coverage for all four settings modules.
- [x] Run `uv run pytest tests/unit -v`.

## Milestone 2: Phase 3.5 Frozen Config Additions

**Objective:** add Phase 3.5 configurable runtime settings and wire consumers to frozen session config before any schema reset can create Phase 3.5 runtime truth.

**Deliverables:** `[agent_loop].max_tool_call_iterations`, `[execution].default_tool_timeout_seconds`, frozen snapshot shape, frozen-default backfill, adapter loop bound from frozen config, and generic ToolBroker default timeout from frozen execution config.

**Modified boundaries:** `runtime/config.py`, adapter request config consumption, ToolBroker construction/context, frozen-default helpers, config tests, adapter tests, ToolBroker timeout tests.

**Invariants:** invalid new config fails before database bootstrap/reset; booleans are not integers; neither field has a Phase 3.5 hard maximum; `shell_exec` timeout semantics and `view_image` provider timeout remain separate; no `agent_loop` or `default_tool_timeout_seconds` facts are added to tool availability.

**Verification steps:** run `uv run pytest tests/unit -v` with coverage proving invalid config does not open, delete, create, or interpret `.sessions/runtime.db`.

**Freeze/review checkpoint:** do not bump schema version to 4 until startup ordering and frozen snapshot behavior for new config fields are verified.

- [x] Update `src/debug_agent/runtime/config.py` to read defaults from settings modules.
- [x] Add `[agent_loop].max_tool_call_iterations` default `1000`, positive-integer validation, boolean rejection, no hard cap, and frozen snapshot persistence.
- [x] Add `[execution].default_tool_timeout_seconds` default `30`, positive-integer validation, boolean rejection, no hard cap, and frozen snapshot persistence.
- [x] Preserve unknown `config.toml` key behavior; do not add global unknown-key fail-closed parsing.
- [x] Ensure `config.toml` does not parse `multimodal.defaults.max_images`, `max_image_edge`, `max_image_pixels`, or `max_request_bytes`.
- [x] Update frozen-default backfill so test/helper paths receive `agent_loop` and expanded `execution` defaults without hot reload.
- [x] Update `src/debug_agent/adapters/langchain_adapter.py` to read tool-call iteration limit from `AgentRunRequest.model_config["agent_loop"]`, with settings fallback only for direct lower-level tests.
- [x] Update `src/debug_agent/tools/broker.py` construction/context so brokered tools without a tool-specific timeout source use frozen `execution.default_tool_timeout_seconds`.
- [x] Prove `shell_exec` still uses `default_shell_timeout_seconds` and `max_shell_timeout_seconds`, not generic tool timeout.
- [x] Prove `view_image` still uses frozen multimodal `timeout_seconds`, not generic tool timeout.
- [x] Add resume tests proving current `config.toml` changes do not alter frozen `agent_loop` or expanded `execution` settings.
- [x] Run `uv run pytest tests/unit -v`.

## Milestone 3: Schema Version 4 Compatibility And Checkpoint Manifest Foundation

**Objective:** establish Phase 3.5 compatibility gates and terminal recovery checkpoint versioning before any Phase 3.5 runtime truth is accepted.

**Deliverables:** `PRAGMA user_version = 4`, startup-only legacy reset including SQLite sidecars, read-only/recovery fail-closed behavior, orphan path collision fail-closed behavior, user-facing compatibility messages, checkpoint `manifest_schema_version = 2` validation foundation, and gated prompt execution while tool contracts remain incomplete.

**Modified boundaries:** SQLite bootstrap, schema-version helpers, CLI startup/status/trace/resume schema gates, session/log/artifact/checkpoint/temp path allocation, checkpoint manifest version constants and validators.

**Invariants:** config failure still happens first; startup reset reads only `PRAGMA user_version` before deleting legacy DB files; corrupt/unreadable databases and unknown future versions fail closed; read-only/recovery commands never create or delete the DB; legacy orphaned `.sessions/` paths are not interpreted or reused.

**Verification steps:** run `uv run pytest tests/unit -v`, then run `uv run pytest tests/integration -v` with coverage focused on startup, `status`, `trace`, and `resume` compatibility behavior.

**Freeze/review checkpoint:** do not implement Phase 3.5 native tool schemas until schema 4 reset/fail-closed behavior and checkpoint manifest version gates are reviewed.

- [x] Define Phase 3.5 schema user version in `src/debug_agent/persistence/settings.py`.
- [x] Create fresh Phase 3.5 runtime databases with SQLite `PRAGMA user_version = 4` through the internal Phase 3.5 bootstrap seam only; do not bind this bootstrap to default user-facing startup before Milestone 10.
- [x] Implement startup-only reset for missing schema version, `0`, and legacy `< 4` databases before interpreting rows in the internal Phase 3.5 startup path.
- [x] Delete `.sessions/runtime.db-wal` and `.sessions/runtime.db-shm` with the legacy DB when present.
- [x] Keep corrupt/unreadable DB handling as `persistence_error/persistence_read_failed` without reset.
- [x] Keep unknown future DB versions fail-closed and non-destructive.
- [x] Implement Phase 3.5 `status`, `trace`, and `resume` read-only/recovery schema gates through internal routing seams, proving they do not create a missing DB and fail closed for mismatched existing DB versions; keep default command routing on the existing Phase 3 path until Milestone 10.
- [x] Add collision checks for fresh Phase 3.5 session/log/artifact/checkpoint-payload/temp paths against orphaned legacy files or directories.
- [x] Update user-facing startup reset and read-only/recovery fail-closed messages.
- [x] Add checkpoint manifest schema version constant `2` and reject non-2 Phase 3.5 terminal recovery checkpoints after schema version 4 gate passes.
- [x] Keep default user-facing prompt execution on the existing Phase 3 path while schema 4, tool contracts, and observability are incomplete; partial Phase 3.5 prompt execution remains internal/test-only or fail-closed before accepting prompt work.
- [x] Run `uv run pytest tests/unit -v`.
- [x] Run `uv run pytest tests/integration -v` with coverage focused on startup/status/trace/resume schema behavior.

## Milestone 4A: ToolBroker Schema, Defaults, Path Normalization, And Audit Arguments

**Objective:** establish the Phase 3.5 input-normalization and audit foundation before any changed tool result or handler behavior is exposed.

**Deliverables:** Phase 3.5 JSON schema validation/default injection, raw field-presence preservation where needed, common string trimming rules, native path canonicalization before policy/approval/handler/audit, `load_skill_resource.path` exclusion, normalized approval-scope inputs, and normalized/redacted audit argument construction for the audit outcomes already reachable in this milestone.

**Modified boundaries:** `tools/broker.py`, shared tool schema helpers, approval scope construction helpers, audit event payload construction, schema validation tests, approval/audit tests.

**Invariants:** ToolBroker remains the only model-visible execution boundary; defaults are injected before approval, audit, and handler execution; execution-before semantic validation wins before path policy for documented local semantic errors; no changed native handler is exposed as Phase 3.5-complete in this milestone.

**Verification steps:** run `uv run pytest tests/unit -v` with coverage focused on schema/default/audit behavior.

**Freeze/review checkpoint:** do not implement ToolResult serialization changes until schema/default injection, raw field-presence tracking, path normalization, and audit argument tests are reviewed.

- [x] Extend schema validation for object, string, integer, `boolean`, arrays, nested objects, required fields, `enum`, `minimum`, `maximum`, default injection, `minItems`, and `maxItems`.
- [x] Reject unknown fields with `tool_error/tool_schema_invalid`.
- [x] Reject JSON booleans for integer fields.
- [x] Preserve raw argument field presence for `search_text.context` conflict detection before default injection.
- [x] Apply common non-empty trimmed string validation to native filesystem `path`, `shell_exec.cwd`, and `view_image.paths[]`.
- [x] Keep `load_skill_resource.path` excluded from workspace path canonicalization and test that it still follows skill-local target rules.
- [x] Canonicalize native filesystem paths before policy, approval, handler execution, and audit.
- [x] Build normalized approval-scope inputs for read/write/search/shell/view_image/runtime-control tools without adding a second call-signature mechanism.
- [x] Store normalized or redacted behavior-affecting arguments in ToolBroker audit events for started, completed, failed, and denied calls.
- [x] Redact `write_file.content` audit arguments to `content_sha256` and `content_bytes`.
- [x] Redact `edit_file.old_text` and `edit_file.new_text` audit arguments to SHA-256 and UTF-8 byte counts.
- [x] Preserve `view_image` audit redaction by recording only `effective_query_source`, never query text, query preview, or query length.
- [x] Add tests proving default-injected fields participate in approval scope and audit, while pagination exclusions and runtime-control approval exceptions remain as documented.
- [x] Run `uv run pytest tests/unit -v`.

## Milestone 4B: ToolResult Serialization And Field-Level Artifacting

**Objective:** make Phase 3.5 ToolResult and durable conversation serialization safe before structured native outputs depend on it.

**Deliverables:** exact Phase 3.5 ToolResult status set, provider-visible success observations derived from `ToolResult.output`, durable `tool_result.content_json` shape, deterministic field-level artifacting for successful native results, no row-level artifact fallback for successful Phase 3.5 native tool results, and atomic ArtifactStore finalization before artifact ids are exposed.

**Modified boundaries:** `tools/broker.py`, ToolResult envelope helpers, durable conversation tool-result serialization, artifact registration/finalization path, serialization tests, artifacting tests.

**Invariants:** allowed statuses are exactly `ok`, `error`, `denied`, `timeout`, and `cancelled`; schema/config failures use `status="error"` not `denied`; policy/approval denials use `status="denied"`; successful structured native outputs serialize through `ToolResult.output`; artifact ids are exposed only after accepted ArtifactStore truth exists; no deterministic call/audit signature, per-tool schema hash, or per-tool result hash is persisted.

**Verification steps:** run `uv run pytest tests/unit -v` with coverage focused on ToolResult serialization and artifacting behavior.

**Freeze/review checkpoint:** do not add ToolBroker timeout/cache/write-lock mechanics until durable serialization and artifacting behavior are reviewed.

- [x] Implement the exact Phase 3.5 ToolResult statuses: `ok`, `error`, `denied`, `timeout`, and `cancelled`.
- [x] Serialize successful structured native outputs through `ToolResult.output` and durable `tool_result.content_json.content`.
- [x] Serialize non-success tool results with `content=null`, normalized `error`, and mirrored diagnostic `artifact_ids`.
- [x] Ensure provider-visible observations for successful structured native tools are derived from `ToolResult.output`, not `redacted_output`.
- [x] Implement deterministic field-level artifacting triggered by the complete native-tool observation exceeding the durable inline threshold.
- [x] Externalize only eligible fields in documented stable order: `read_file.content`, `search_text.matches`, `search_text.paths`, `search_text.counts`, `shell_exec.stdout`, `shell_exec.stderr`.
- [x] Preserve inline control metadata such as path identity, pagination, guard data, status, checksum, and byte counts when field-level artifacting occurs.
- [x] Return `status="error"` with `tool_error/tool_execution_failed` when a successful native observation remains too large after field-level artifacting.
- [x] Prevent row-level artifact-backed conversation fallback for successful Phase 3.5 native tool results.
- [x] Ensure artifact temp files are atomically finalized and accepted ArtifactStore records exist before artifact ids are exposed in `ToolResult`, durable conversation content, or audit.
- [x] Verify timeout, cancellation, write failure, or registration failure cannot expose incomplete artifact ids or accepted conversation references.
- [x] Run `uv run pytest tests/unit -v`.

## Milestone 4C: ToolBroker Timeout, Volatile Cache, And Write Locks

**Objective:** provide the shared runtime mechanics that read/search/write handlers need for no-partial-success, stale-write protection, and same-path write serialization.

**Deliverables:** generic ToolBroker timeout envelope, timeout error projection, ArtifactStore work inside the envelope, process-local volatile file metadata cache skeleton with documented shape, resume-empty-cache behavior, and per-canonical-path in-process write serialization.

**Modified boundaries:** `tools/broker.py`, timeout/deadline helpers, cache guard helpers, write-lock helpers, artifact timeout tests, cache tests, serialization-lock tests.

**Invariants:** timeout measurement starts after interactive approval and before handler/traversal/provider/command work; approval wait, audit emission, and final result envelope formatting stay outside the timeout envelope; timeout returns `tool_error/tool_execution_timeout` without partial success; cache is process-local, session-runtime-local, and never resume truth.

**Verification steps:** run `uv run pytest tests/unit -v` with coverage focused on ToolBroker timeout/cache/lock behavior.

**Freeze/review checkpoint:** do not expose changed native tool handlers until Milestones 4A, 4B, and 4C have all been reviewed.

- [x] Start generic tool timeout measurement after interactive approval and before handler/traversal/provider/command work.
- [x] Include ArtifactStore registration and artifact writes inside the timeout envelope.
- [x] Keep approval wait, audit emission, and final result envelope formatting outside the timeout envelope.
- [x] Map brokered timeout to `status="timeout"` and `tool_error/tool_execution_timeout`.
- [x] Store normalized or redacted behavior-affecting arguments in ToolBroker audit events for timed-out and cancelled calls.
- [x] Add process-local volatile file metadata cache keyed by canonical absolute path.
- [x] Store cache entries with `sha256`, `size`, `mtime_ns`, `observed_at`, and `source_tool`.
- [x] Ensure cache entries can be created only by `read_file`, successful guarded `edit_file`, successful overwrite `write_file`, and successful create-new `write_file`.
- [x] Add resume tests proving the volatile file metadata cache starts empty and is not checkpoint or durable truth.
- [x] Add per-canonical-path in-process write serialization.
- [x] Add broker-level tests proving timed-out generic handler/traversal/read test seams return no partial success and do not advance the file metadata cache; real `search_text` timeout coverage belongs to Milestone 6.
- [x] Run `uv run pytest tests/unit -v`.

## Milestone 5: Portable Glob, `find_file`, `read_file`, And `list_dir`

**Objective:** deliver the read/discovery native tool slice that provides deterministic traversal, pagination, and stale-write cache observations.

**Deliverables:** portable glob matcher, candidate traversal respecting path deny/hidden/symlink rules, model-visible `find_file`, enhanced `read_file`, enhanced `list_dir`, canonical absolute output paths for these tools, and `read_file` whole-file SHA-256 cache updates.

**Modified boundaries:** `tools/native.py` or focused helper modules, ToolBroker native tool registry, native tool schemas, path traversal helpers, tests for glob, traversal, pagination, symlinks, denies, hidden paths, and cache update behavior.

**Invariants:** no Python glob/Path.glob primary traversal; denied paths do not leak names; `include_hidden=true` does not override denies; symlink file policy checks use resolved target while results sort/paginate by candidate path; `read_file` is UTF-8 text only; page sizes above hard maximums are schema invalid and not capped.

**Verification steps:** run `uv run pytest tests/unit -v` with coverage focused on the native tools changed in this milestone.

**Freeze/review checkpoint:** do not implement `search_text` until controlled traversal, portable glob matching, deterministic sorting, and read cache updates are reviewed.

- [ ] Implement portable glob subset for `*`, `?`, `[...]`, and full-segment `**`.
- [ ] Reject unsupported glob syntax and backslash with `tool_error/tool_schema_invalid`.
- [ ] Implement approved-root candidate traversal with builtin/user deny, hidden filtering, symlink directory non-recursion, and symlink file target policy checks.
- [ ] Add `find_file` schema, approval scope behavior, deterministic canonical-path sorting, file-only results, pagination, and structured output.
- [ ] Enhance `read_file` with `offset`, default and maximum `limit = 2000`, structured output, UTF-8 failure mapping, streaming or bounded-memory whole-file hashing, and cache update on success.
- [ ] Enhance `list_dir` with `ignore`, `offset`, `include_hidden`, default `limit = 200`, hard maximum `1000`, immediate-child filtering, and structured output.
- [ ] Ensure `find_file`, `read_file`, and `list_dir` use canonical absolute paths in ordinary model-visible output.
- [ ] Add tests for empty/whitespace paths, omitted-path defaults where allowed, `"."` workspace-root behavior, pagination metadata, and next offset rules.
- [ ] Add portable glob tests for `*`, `?`, `[...]`, full-segment `**`, non-segment `**` rejection, negated/malformed character class rejection, brace/extglob/backslash rejection, `/` separator behavior, and `case_sensitive=false` `str.casefold()` matching with canonical-path sorting.
- [ ] Add `list_dir.ignore` tests for literal names, `*`, `?`, `foo/`, `foo/**`, and rejection of `a/b`, bare `**`, `*.py/`, nested patterns, character classes, brace expansion, extglob, and backslash.
- [ ] Add discovery traversal tests for inherited builtin denies including `.sessions/`, `.git/`, dependency/build/cache directories, global skill source roots, and project skill source roots without denying unrelated directories named `skills`.
- [ ] Add symlink tests proving symlink directories are not recursively followed, symlink file target policy is checked on the resolved target, and successful results return/sort/paginate by the normalized symlink candidate path.
- [ ] Add `read_file` tests for UTF-8 decode failure, `offset` beyond EOF, final line without newline, whole-file byte count, whole-file raw-byte SHA-256, and bounded or streaming hash/page collection behavior.
- [ ] Add tests proving `list_dir`, `find_file`, and `view_image` do not create write-guard cache entries; `search_text` cache behavior is verified in Milestone 6 after the tool exists.
- [ ] Run `uv run pytest tests/unit -v`.

## Milestone 6: Controlled Ripgrep `search_text`

**Objective:** replace the Phase 1 `search_text.query` behavior with the Phase 3.5 line-oriented controlled ripgrep tool as an isolated reviewable patch.

**Deliverables:** `search_text.pattern` schema, no `query` alias, no `multiline`, approved-root candidate enumeration, optional portable glob/type filtering, ripgrep availability and regex validation, `--no-config` isolated execution, content/files/count output modes, deterministic pagination, bounded context attachment, skipped-file counters, and normalized error mapping.

**Modified boundaries:** `tools/native.py` or dedicated search module, ripgrep command runner boundary, type allowlist, temporary empty-file regex compile check, tests for search schema, argv construction, candidate filtering, output modes, pagination, skipped counters, and missing/invalid `rg` behavior.

**Invariants:** runtime enumerates and filters candidate files before ripgrep; ripgrep runs with `shell=False`, `--json`, `--no-config`, `--regexp`, pattern, `--`, candidate paths, and controlled environment; no Python regex fallback; missing `rg` and regex compile errors return `tool_error/tool_execution_failed`; unknown `type` returns `tool_error/tool_schema_invalid`; content-mode pagination occurs before context rows are attached.

**Verification steps:** run `uv run pytest tests/unit -v` with coverage focused on `search_text`.

**Freeze/review checkpoint:** do not start write-tool tightening until `search_text` traversal, ripgrep isolation, pagination, and timeout behavior are reviewed.

- [ ] Remove model-visible `search_text.query`; reject it as an unknown field.
- [ ] Reject CR/LF in `search_text.pattern`.
- [ ] Reject `multiline` as an unknown field.
- [ ] Implement `output_mode` values `content`, `files_with_matches`, and `count`.
- [ ] Implement `case_sensitive`, `fixed_strings`, `before_context`, `after_context`, `context`, `include_hidden`, `glob`, and `type`.
- [ ] Implement `offset` default `0`, `maxResults` default `100`, `maxResults` hard maximum `1000`, integer minimum validation, and above-maximum `tool_error/tool_schema_invalid` behavior for `search_text`.
- [ ] Preserve raw field presence so explicit `context` conflicts with explicit `before_context` or `after_context`.
- [ ] Build candidate file list only after root approval.
- [ ] Apply denied/hidden/symlink/glob/type filtering before ripgrep execution.
- [ ] Use runtime-owned text type allowlist with case-insensitive file-family matching over candidate relative paths.
- [ ] Verify `rg` availability for every call.
- [ ] Validate regex patterns through ripgrep only when `fixed_strings=false`, using a runtime-owned empty UTF-8 temporary file.
- [ ] Return empty success for empty candidate sets only after required availability/regex validation.
- [ ] Invoke ripgrep with `shell=False`, controlled argv, `--json`, `--no-config`, `--regexp`, the pattern value, `--`, special-character-safe candidate path argv, and environment isolation from `RIPGREP_CONFIG_PATH`.
- [ ] Parse ripgrep JSON and normalize records into documented result shapes.
- [ ] Sort and paginate deterministically by canonical path and line ordering.
- [ ] Attach content-mode context rows through bounded runtime reads after matching-line pagination.
- [ ] Count matching lines once per line, not captures or repeated same-line submatches.
- [ ] Emit aggregate `skipped_files` counters without denied path names or denied subtree traversal.
- [ ] Pre-screen candidate files as UTF-8 and populate `skipped_files.decode_error` for UTF-8 pre-screen failures and ripgrep JSON byte payload decode failures without exposing path names for denied files.
- [ ] Populate `skipped_files.other` for symlink escape, ordinary candidate stat/read/pre-screen failures, and ripgrep JSON records or line payloads that exceed internal parser safety limits.
- [ ] Ensure runtime does not enter denied or hidden directory subtrees merely to compute `skipped_files.denied` or `skipped_files.hidden`.
- [ ] Add tests proving no ripgrep context flags are used and context read/stat/decode failure after matching-line pagination returns `tool_error/tool_execution_failed` without a partial successful page.
- [ ] Add tests for context rows repeating across adjacent pages, duplicate context-line de-duplication, match lines winning over context lines, and line previews over 4000 codepoints setting `line_truncated=true`.
- [ ] Add tests proving only the active output-mode result field is present for `matches`, `paths`, or `counts`.
- [ ] Add tests for `search_text.offset` and `maxResults` default injection, minimum validation, hard-maximum rejection, `total_returned`, `truncated`, and `next_offset = offset + total_returned` for all output modes.
- [ ] Add tests for optional candidate chunking or argv-safety behavior while preserving canonical path and line ordering across chunks.
- [ ] Add tests proving `fixed_strings=true` skips regex compilation after `rg` availability, while `fixed_strings=false` returns `tool_error/tool_execution_failed` with the documented ripgrep diagnostic form for regex compile failures.
- [ ] Return timeout as `tool_error/tool_execution_timeout` without partial success or cache updates.
- [ ] Add tests proving successful and timed-out `search_text` calls do not create or advance write-guard cache entries.
- [ ] Run `uv run pytest tests/unit -v`.

## Milestone 7: Stale-Write `edit_file` And `write_file`

**Objective:** tighten model-visible writes with volatile stale-write protection, atomic replacement, structured outputs, and minimal side-effect audit facts.

**Deliverables:** `edit_file.replace_all`, default unique-match semantics, existing-file stale-write guard, overwrite `write_file` guard, create-new `write_file` cache entry, same-directory temporary-file atomic replace for existing targets, structured hash/guard outputs, and failure/timeout side-effect audit for created parent directories.

**Modified boundaries:** native write handlers, ToolBroker cache guard helpers, audit side-effect payloads, tests for read-edit-write and read-write guard flows.

**Invariants:** `edit_file` never creates files; `old_text=""` is schema/local semantic invalid; existing-file edit/overwrite fails until cache has a valid revision; resume starts with empty cache; stale guard is not durable truth; failed or timed-out writes do not report partial write success or update cache.

**Verification steps:** run `uv run pytest tests/unit -v` with coverage focused on write/edit behavior.

**Freeze/review checkpoint:** do not cut over default user-facing prompt execution to Phase 3.5 until guarded write behavior and durable tool-result serialization are reviewed.

- [ ] Add `replace_all` boolean default `false` to `edit_file`.
- [ ] Make default `edit_file` replacement require exactly one LF-normalized match.
- [ ] Make `replace_all=true` replace all matches while failing on zero matches.
- [ ] Preserve dominant existing line endings for `edit_file` write-back after LF-normalized matching; write LF when no dominant existing style can be determined.
- [ ] Reject empty `old_text` with `tool_error/tool_schema_invalid`.
- [ ] Require cache guard for every existing-file `edit_file`.
- [ ] Require cache guard for every overwrite `write_file`.
- [ ] Recompute current raw-byte SHA-256 before guarded writes and fail on cache mismatch.
- [ ] Use same-directory temporary files and atomic replace for existing-file writes.
- [ ] For create-new `write_file`, compute the minimal missing parent-directory chain and require each candidate parent directory plus the final target path to pass path policy before approval or creation.
- [ ] Include the exact canonical planned parent-directory creation set in `write_file` reusable approval scope, approval/UI presentation, and normalized audit arguments.
- [ ] Keep parent-directory approval scoped to the final canonical target file path; do not widen it to a reusable directory grant.
- [ ] Keep create-new `write_file` exclusive-create semantics and create cache entry after success.
- [ ] Fail a create-new `write_file` race with `tool_error/tool_execution_failed` if the target appears between missing-target classification and exclusive create; do not convert it to overwrite.
- [ ] Add cooperative deadline checks before parent-directory creation, exclusive create, overwrite temp-file write, atomic replace, cache update, and artifact/result handling.
- [ ] Advance cache after successful guarded `edit_file` and overwrite `write_file`.
- [ ] Emit structured outputs with canonical `path`, byte count, before/after hashes, created/overwritten flags where applicable, and guard metadata.
- [ ] Record documented minimal side-effect audit facts when `write_file` creates parent directories before failure or timeout.
- [ ] Verify failed or timed-out `write_file` calls that created parent directories do not report file write success and do not update the file metadata cache.
- [ ] Add tests for `edit_file` line-ending preservation, `write_file` parent-directory approval scope/audit, target-race failure, and timeout side-effect audit.
- [ ] Add tests proving direct existing-file write after resume fails until `read_file` observes the file.
- [ ] Run `uv run pytest tests/unit -v`.

## Milestone 8: Shell, View Image, Runtime-Control Compatibility, And Tool Availability Completion

**Objective:** complete the remaining model-visible tool compatibility surface and make Phase 3.5 tool availability checkpoint facts truthful.

**Deliverables:** successful `shell_exec` structured output, unchanged shell schema capability boundary, fixed `view_image` constants from `tools/settings.py`, preserved `view_image` display-path output/query redaction, runtime-control tool envelope/status/error projection, Phase 3.5 native tool contract marker, and dynamic tool facts.

**Modified boundaries:** shell handler result normalization, view_image schema constant sources, runtime-control ToolResult wrapping, tool registry/model-visible bindings, checkpoint tool-availability facts and checksum validation, resume validation tests.

**Invariants:** `shell_exec` does not gain raw command strings or background/interactive/PTY behavior; nonzero shell exit remains `tool_error/shell_nonzero_exit`; `view_image` ordinary output path behavior remains Phase 2 display path; `view_image` approval/policy/audit use canonical paths and query redaction; runtime-control logical semantics remain earlier-phase semantics.

**Verification steps:** run `uv run pytest tests/unit -v` with focused shell/view_image/runtime-control/checkpoint coverage, then run `uv run pytest tests/integration -v` through internal test seams for fresh prompt startup and resume eligibility. Default one-shot, plain REPL, and TUI prompt entrypoints continue using the existing Phase 3 prompt path until Milestone 9 completes and is reviewed.

**Freeze/review checkpoint:** do not start observability path replacement until fresh Phase 3.5 sessions can bind tools, terminalize, and resume with valid manifest schema version 2 and tool-availability facts.

- [ ] Enhance successful `shell_exec` output with `argv`, canonical `cwd`, `stdout`, `stderr`, `returncode`, `signal`, and integer `duration_ms`.
- [ ] Preserve structured argv execution and existing nonzero failure behavior.
- [ ] Prove the generated `shell_exec.timeout_seconds.maximum` schema uses frozen `execution.max_shell_timeout_seconds`, including a non-default configured maximum, and does not hard-code `3600`.
- [ ] Source `view_image` max image count, image edge, pixel, request body, and default query constants from `tools/settings.py`.
- [ ] Prove fixed `view_image` limits cannot be configured through `config.toml`.
- [ ] Preserve `view_image` enabled/disabled behavior and display-path ordinary output.
- [ ] Preserve `view_image.query` validation: trim provided query, reject non-string, whitespace-only, and over-limit values with `tool_error/tool_schema_invalid`, and use the frozen default query only when omitted.
- [ ] Preserve disabled-`view_image` validation ordering: malformed disabled calls validate first with `tool_error/tool_schema_invalid`, while valid disabled calls return `config_error/tool_unavailable`.
- [ ] Preserve `view_image` query redaction outside accepted assistant-authored trace tool-call arguments.
- [ ] Wrap `activate_skill`, `load_skill_resource`, and `todo` through Phase 3/3.5 ToolBroker status/error projection without changing their logical contracts.
- [ ] Add Phase 3.5 native tools contract marker to terminal recovery tool-availability facts.
- [ ] Include `shell_exec.max_timeout_seconds` and `view_image` dynamic facts in tool availability.
- [ ] Compute and validate tool-availability checksum over facts excluding checksum.
- [ ] Reject resume when manifest schema version, native marker, shell facts, view_image facts, or checksum mismatch.
- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v` with coverage focused on startup, terminal checkpoint creation, and resume validation.

## Milestone 9: Conversation Trace And Events JSONL Observability

**Objective:** replace event-dump trace and `engine.log` paths with Phase 3.5 non-authoritative conversation trace and events JSONL outputs.

**Deliverables:** `.sessions/<session_id>/logs/trace.md`, no legacy root trace, `.sessions/<session_id>/logs/events.jsonl`, no `engine.log`, unchanged JSONL entry schema, `metadata.event_id` on persisted run-event observations, conversation transcript renderer from durable rows, render validation, atomic trace writes, manual `debug-agent trace <session_id>` rebuild, automatic post-terminal checkpoint trace refresh, failure behavior that does not affect runtime truth or lifecycle, and reviewed evidence that the Phase 3 to Phase 3.5 prompt cutover is eligible for Milestone 10.

**Modified boundaries:** `observability/trace_writer.py`, `observability/logging.py`, CLI trace command, orchestrator terminalization hook, trace/status read helpers, tests for trace path/content/failure/atomicity/events path.

**Invariants:** trace renders only accepted closed durable conversation rows ordered by `message_index`; context summaries and ordinary run events are excluded from trace body; manual trace does not claim ownership, start/resume/terminalize/fail-close/model-call/tool-call anything; automatic trace failure after terminal checkpoint success does not write runtime truth, audit/run events, or events JSONL and does not change original exit code.

**Verification steps:** run `uv run pytest tests/unit -v` with focused observability coverage, then run `uv run pytest tests/integration -v` with coverage focused on terminal trace generation, manual trace against running sessions, and resume-to-terminal trace rebuild.

**Freeze/review checkpoint:** do not cut over default user-facing prompt execution to Phase 3.5 or perform final UI polish until trace/events paths, render validation, non-authoritative failure semantics, and trace/events integration verification are reviewed.

- [ ] Rename `EngineLogWriter` to `EventsJsonlWriter` and update imports/tests.
- [ ] Change JSONL observability output path from `logs/engine.log` to `logs/events.jsonl`.
- [ ] Preserve the existing JSONL entry schema while changing only the canonical writer name and output path.
- [ ] Ensure `write_event_log` entries for persisted run-event observations include `metadata.event_id`.
- [ ] Ensure `write_runtime_log` entries remain runtime diagnostic observations and may lack `metadata.event_id`.
- [ ] Add tests proving `status`, `trace`, `resume`, checkpoint validation, and recovery do not read `events.jsonl` as runtime truth.
- [ ] Change trace output path from `.sessions/<session_id>/trace.md` to `.sessions/<session_id>/logs/trace.md`.
- [ ] Remove legacy trace/log compatibility, migration, copy, or symlink behavior.
- [ ] Render trace header and session summary exactly from the Phase 3.5 observability spec.
- [ ] Render user messages, assistant final messages, assistant tool-call groups with paired tool results, and runtime failure/cancellation facts.
- [ ] Filter `context_summary` rows without replacement notice.
- [ ] Exclude ordinary run events, checkpoint internals, approval internals, context compression internals, admin timeline events, and event counts from trace.
- [ ] Validate durable rows, closed groups, contiguous positions, tool-call/result pairing, supported statuses, artifact references, and session/run scope before rendering.
- [ ] Preserve user and assistant content as-is without Markdown escaping or sanitization.
- [ ] Render tool arguments/results as indented plain preview blocks, not fenced code blocks.
- [ ] Redact write/edit sensitive arguments in trace using documented redaction hashes and byte counts.
- [ ] Use unique same-directory temporary files and atomic replace for automatic and manual trace writes.
- [ ] Ensure concurrent automatic and manual trace writes are last-writer-wins while the final `logs/trace.md` is always one complete render.
- [ ] Make `debug-agent trace <session_id>` rebuild trace from a consistent SQLite read transaction without claiming ownership.
- [ ] Ensure manual `debug-agent trace <session_id>` may run against a running session, does not wait for or block on an active runner, and never changes active owner state.
- [ ] Map SQLite busy after bounded read handling to `persistence_error/sqlite_busy_timeout`; map ordinary trace read failures to `persistence_error/persistence_read_failed`.
- [ ] Ensure manual trace failures leave existing `logs/trace.md` unchanged.
- [ ] Map manual trace render/write failure after lookup succeeds to `ui_error/trace_render_failed` and CLI `ERROR_TRACE_RENDER = 11`.
- [ ] Ensure automatic trace refresh failures after terminal checkpoint success do not alter lifecycle, exit code, runtime truth, or events JSONL.
- [ ] Show automatic trace refresh failure in the current CLI/UI surface, including a REPL/TUI error block, without persisting that failure as runtime truth.
- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v` with coverage focused on trace generation and events path behavior.
- [ ] Record trace/events integration verification evidence for review. Default user-facing prompt execution remains on the existing Phase 3 path until this milestone has passed review.

## Milestone 10: REPL/TUI Presentation, Status/Trace Surface, And Acceptance Prep

**Objective:** cut over the default user-facing Phase 3.5 path, finish baseline user-facing presentation, and prepare the implementation for final interactive UI review.

**Deliverables:** REPL/TUI/plain display updates for structured native tool results, pagination, guard failures, trace path messages, legacy reset messages, no legacy output paths, representative integration coverage, and baseline automated verification before interactive review.

**Modified boundaries:** `cli/repl_controller.py`, `cli/repl_view.py`, `cli/plain_repl_view.py`, `cli/prompt_toolkit_view.py`, `cli/main.py`, status/trace output helpers, integration tests, manual verification notes if used.

**Invariants:** UI remains non-authoritative; display changes do not alter ToolBroker, approval, policy, checkpoint, durable conversation, artifact, resume, or runtime truth semantics; no future-phase scaffolding is added.

**Verification steps:** run `uv run pytest tests/unit -v` and `uv run pytest tests/integration -v`. Final full-suite and manual UI evidence are deferred to Milestone 11 after interactive polish is complete.

**Freeze/review checkpoint:** do not start interactive REPL/TUI polish until cutover, baseline presentation, and focused integration verification are reviewed.

- [ ] Cut over default user-facing one-shot, plain REPL, and TUI prompt execution from the existing Phase 3 path to the Phase 3.5 path only after Milestone 9 review confirms schema 4, frozen config, model-visible tools, ToolResult serialization, checkpoint facts, `logs/trace.md`, `logs/events.jsonl`, and no-legacy-path behavior are complete and verified.
- [ ] Update CLI output to print the new `logs/trace.md` path.
- [ ] Update REPL/TUI/plain rendering for structured native tool outputs, field-level artifact references, pagination metadata, timeout/error statuses, and stale-write guard failures.
- [ ] Update user-facing legacy reset and read-only/recovery fail-closed messages in CLI/TUI surfaces.
- [ ] Update status/trace tests so no assertion expects `.sessions/<session_id>/trace.md` or `logs/engine.log`.
- [ ] Add integration coverage for a representative one-shot session using Phase 3.5 tool bindings and terminal trace generation.
- [ ] Add integration coverage for schema 4 startup reset, read-only fail-closed behavior, terminal checkpoint tool availability, and resume validation.
- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.

## Milestone 11: Interactive REPL/TUI Manual Review And Final Acceptance

**Objective:** perform a human-driven REPL/TUI review pass after the complete Phase 3.5 runtime path is cut over, apply only narrowly scoped presentation polish, and run final acceptance verification.

**Deliverables:** manual review evidence for REPL/TUI interaction quality, any small CLI-only presentation polish required by that review, updated UI tests if display behavior changes, final automated acceptance results, and recorded manual verification notes.

**Modified boundaries:** `src/debug_agent/cli/` presentation/controller/view files and directly related tests only. Documentation updates are limited to recording verification evidence if the implementation workflow keeps notes in the repository.

**Invariants:** UI remains non-authoritative; polish must not change ToolBroker, approval, path policy, shell policy, checkpoint, durable conversation, artifact, trace, resume, runtime truth, config, model-visible tool schemas, tool result contracts, or lifecycle semantics. If manual review identifies a required behavior or contract change, stop and request a contract/spec patch before implementing it.

**Verification steps:** after any UI polish, run the narrowest affected UI tests plus `uv run pytest tests/unit -v`, `uv run pytest tests/integration -v`, and `uv run pytest -v`. Perform and record the manual verification checks from `docs/phase-3.5/operations.md`.

**Freeze/review checkpoint:** Phase 3.5 implementation is ready for review only after automated commands and required manual checks are recorded with observed results.

- [ ] Run an interactive REPL/TUI review of structured native tool output rendering, field-level artifact references, pagination metadata, timeout/error statuses, stale-write guard failures, automatic trace refresh failure presentation, and user-facing legacy reset/fail-closed messages.
- [ ] Limit any resulting polish to CLI presentation and interaction flow; do not alter runtime truth, ToolBroker behavior, tool contracts, persistence semantics, or checkpoint/resume behavior.
- [ ] Add or update focused UI tests for any changed rendering or interaction behavior.
- [ ] Add manual verification notes for conversation transcript readability, JSONL readability, REPL/TUI pagination/guard rendering, user-facing reset messages, and shell output presentation when those surfaces changed.
- [ ] Record each manual verification note with command sequence, expected result, observed result, session id and run id when applicable, trace/status excerpts for observability changes, and known limitations.
- [ ] Run focused affected UI tests when UI polish changes rendering behavior.
- [ ] Run `uv run pytest tests/unit -v`.
- [ ] Run `uv run pytest tests/integration -v`.
- [ ] Run `uv run pytest -v`.

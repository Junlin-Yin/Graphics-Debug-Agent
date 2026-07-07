# Phase 4 Scope

## Goal

Phase 4 validates that `debug-agent` can carry the adapted
`renderdoc-gpu-debug` prompt skill as the v1 RenderDoc Debug Runtime.

This phase is a readiness and business-adaptation validation phase. It does not
continue Phase 3.5 generic framework hardening. Runtime changes are allowed only
when they are required to support the documented Phase 4 readiness checks:

- Kimi K2.5 main-agent thinking mode with discarded thinking blocks.
- non-authoritative per-invocation run metrics for readiness review.
- provider usage normalization and cumulative token accounting for run metrics
  and existing REPL/TUI token surfaces.
- main-agent default harness prompt replacement and skill-resource affordance
  clarification needed for real RenderDoc skill-driven runs.
- generic `view_image` default query strengthening needed for real image-driven
  debugging observations.
- fake `rdc` automated readiness scenario.
- Windows + real `rdc` smoke as the v1 completion gate.
- package/deployment smoke verification.

The `renderdoc-gpu-debug` skill content adaptation is an external prerequisite
for this phase. This repository does not vendor or implement that skill
adaptation in `src/debug_agent`. Ordinary automated tests verify the runtime
readiness path with a fake `rdc` scenario; the externally adapted
`renderdoc-gpu-debug` skill is verified through a canonical manual smoke record.

## Must Implement

### Main-Agent Harness Prompt

- replace the legacy Phase 0 default system prompt with a generic
  `debug-agent` harness prompt for Phase 4 prompt sessions. The exact built-in
  `SYSTEM_PROMPT` text is defined in `architecture.md` and must be implemented
  from that document, not from external drafts or source notes.
- rename the runtime default prompt constant from the Phase 0-specific name to a
  phase-neutral `SYSTEM_PROMPT`.
- keep the default prompt generic: it must define runtime harness discipline,
  tool usage discipline, evidence/failure discipline, Todo Plan usage for
  multi-step debugging, skill-resource loading discipline, and output
  completion discipline.
- do not encode RenderDoc, `rdc`, shader, report-schema, or case-specific
  workflow semantics in the default prompt. Domain procedure remains owned by
  the user prompt and active prompt skills.
- make active skill resource lists explicit model-visible indexes: listing a
  resource path is not loaded content, and the model must call
  `load_skill_resource` before relying on a listed resource's contents. Large
  resources may return a model-readable artifact reference, in which case the
  model must use `read_file` with the returned `artifact_path` and pagination to
  inspect the needed content. The exact active skill context guidance text is
  defined in `architecture.md`.
- strengthen the model-visible `load_skill_resource` tool description so the
  tool's intended trigger is clear when active skill instructions or
  `available_resources` reference needed content, including artifact-backed
  large resource results. The exact tool description is defined in
  `architecture.md`.

### Compatibility

- identify fresh Phase 4 runtime databases with `PRAGMA user_version = 5`.
- support forward startup from Phase 3.5 runtime databases with
  `PRAGMA user_version = 4` into Phase 4 schema version `5`.
- during the v4-to-v5 startup upgrade, backfill existing
  `sessions.config_snapshot_json` rows with
  `thinking: {enabled: false, effort: "high"}` when the object is absent.
- do not rewrite existing terminal recovery checkpoint payloads,
  `payload_sha256`, frozen snapshot checksum fields, or checkpoint manifests
  during the v4-to-v5 startup upgrade.
- when resume validates a checkpoint whose referenced config snapshot contains
  exactly the Phase 4 default disabled `thinking` object and the stored config
  checksum does not match the full Phase 4 snapshot, retry config checksum
  validation against the Phase 3.5-compatible canonical config shape with that
  default `thinking` object omitted. This compatibility rule affects checksum
  validation only; runtime still uses the upgraded frozen config with disabled
  thinking.
- preserve existing Phase 3.5 active owner, session, run, checkpoint, approval
  grant, event, durable conversation, Todo Plan, and artifact metadata truth
  during the upgrade.
- fail closed for missing schema version, `0`, legacy `< 4`, corrupt or
  unreadable databases, and unknown future versions `> 5`.
- keep `status`, `trace`, and `resume` read/recovery paths fail-closed for
  schema mismatch. These commands must not create, reset, or upgrade a runtime
  database.
- do not use the v4-to-v5 upgrade to perform stale owner fail-close,
  terminalization, ownership release, trace/metrics rebuild, legacy `< 4`
  migration, or corrupt database repair.
- keep Phase 3/3.5 runtime truth and durable conversation semantics. Phase 4
  must not reinterpret thinking blocks, metrics files, trace files, or
  deployment smoke output as runtime truth.

### Main-Agent Thinking Mode

- add `specs/thinking.md` as the authoritative Phase 4 thinking contract.
- add frozen runtime config fields:

  ```toml
  [thinking]
  enabled = false
  effort = "high"
  ```

- freeze both fields into the session config snapshot.
- apply thinking only to main agent model calls.
- do not apply thinking to `view_image` provider calls.
- do not apply thinking to context compression model calls unless a later phase
  explicitly changes that contract.
- pass `effort` only when thinking is enabled. Configuring `effort` while
  thinking is disabled is valid and must not fail startup.
- discard provider `thinking` content blocks from all durable, visible, and
  subsequent-model-call paths.

### Run Metrics

- add `specs/run-metrics.md` as the authoritative Phase 4 run metrics contract.
- write `.sessions/<session_id>/logs/run_metrics_<timestamp>.json` when a
  prompt session terminalizes.
- treat metrics as a non-authoritative per-invocation evaluation artifact.
- normalize provider usage from the existing Kimi/Anthropic-compatible provider
  response path so metrics and existing REPL/TUI token accounting can use
  provider token truth when available.
- compute cumulative `input_tokens`, `output_tokens`, and `total_tokens` for
  metrics and existing REPL/TUI token accounting. These fields must not mean
  "last known provider usage value."
- when provider usage is unavailable, compute cumulative estimated
  `input_tokens`, `output_tokens`, and `total_tokens` instead of writing null
  token totals or substituting latest context-token estimates.
- keep metrics out of SQLite runtime truth, checkpoint truth, resume,
  recovery validation, status, trace rendering, and audit truth.
- make metrics write failure best-effort only. It may surface as a CLI/UI
  warning but must not change terminalization, checkpoint creation, ownership
  release, or exit code.

### RenderDoc Readiness

- add `specs/renderdoc-readiness.md` as the authoritative Phase 4 RenderDoc
  readiness contract.
- strengthen the default `view_image` query as generic visual debugging
  guidance. The exact default query text is defined in `architecture.md` and
  supersedes the Phase 2 default query for current Phase 4 runtime behavior.
- keep the default `view_image` query domain-neutral. It must not encode
  RenderDoc, `rdc`, shader, report-schema, or case-specific workflow semantics.
- verify the runtime RenderDoc readiness path through a fake `rdc` automated
  scenario.
- verify the externally adapted `renderdoc-gpu-debug` skill through a manual
  canonical smoke record. This may be combined with the Windows + real `rdc`
  smoke when the same run uses the adapted skill.
- implement fake `rdc` as a test fixture/helper, not as runtime code and not as
  packaged CLI functionality.
- fake `rdc` must cover the short structured command sequence:
  `doctor`, `open`, `info --json`, `draws --limit 20`, `rt ... -o <png>`, and
  `close`.
- fake `rdc rt` must write a real PNG that is inspected through the brokered
  `view_image` tool.
- keep the fake scenario behind ToolBroker `shell_exec` and `view_image`
  paths. It must not require PTY, interactive shell, long-running shell,
  background tasks, workflow runtime, or tool-call cache.
- define Windows + real `rdc` smoke as a canonical manual operation and optional
  self-hosted automation gate. It is not required for ordinary PR CI.

### Deployment Smoke

- add `specs/deployment.md` as the authoritative Phase 4 deployment smoke
  contract.
- standardize `uv build` plus `uv tool install` package smoke in
  `operations.md`.
- verify that an installed `debug-agent` console script can run outside the
  source checkout.
- keep runtime workspace semantics unchanged: sessions, project skills, active
  ownership, path policy, and artifacts are rooted in the invocation workspace,
  not in the tool installation directory.

## Must Not Implement

Phase 4 must not add:

- generic constants/config refactoring beyond the documented `[thinking]`
  fields.
- generic native tool framework expansion.
- generic engine log, trace, or TUI overhaul.
- RenderDoc command allowlists in runtime core.
- RenderDoc daemon state as runtime-owned session truth.
- shader-specific runtime validators.
- `rdc_report`, `shader_report`, or `final_report` schema validation in runtime
  core.
- Ralph Loop state machines.
- `shader-debug-loop` readiness.
- shader patch loop behavior.
- workflow runtime.
- subagent runtime.
- MCP or plugin integration.
- PTY shell, interactive shell, long-running shell, or background shell runtime.
- thinking retention, thinking replay, signed thinking support, or
  redacted-thinking replay.
- provider-generic reasoning abstraction.
- real RenderDoc capture automation as an ordinary CI requirement.

## Acceptance Boundary

Phase 4 is complete when:

- Phase 4 docs define the schema, thinking, metrics, RenderDoc readiness,
  deployment, tests, and operations contracts.
- fresh runtime databases use `user_version = 5`.
- Phase 3.5 `user_version = 4` startup compatibility is implemented as
  documented.
- existing Phase 3.5 session snapshots upgraded to Phase 4 contain default
  disabled thinking config.
- thinking defaults are frozen and thinking mode can be enabled for main agent
  model calls without persisting thinking content.
- fake `rdc` readiness passes in automated tests.
- a manual adapted `renderdoc-gpu-debug` skill smoke is recorded.
- run metrics are written for terminal prompt sessions as non-authoritative
  artifacts.
- the Phase 4 default system prompt uses the phase-neutral `SYSTEM_PROMPT`
  constant and contains the documented generic harness discipline.
- active skill resource lists and the `load_skill_resource` tool description
  make resource loading requirements model-visible, including the follow-up
  `read_file(artifact_path)` path for artifact-backed large resources.
- the Phase 4 default `view_image` query uses the exact generic visual
  debugging text defined in `architecture.md`.
- package deployment smoke is part of canonical Phase 4 verification.
- Windows + real `rdc` smoke is executed and recorded as the v1 completion gate,
  either manually or through optional self-hosted automation.

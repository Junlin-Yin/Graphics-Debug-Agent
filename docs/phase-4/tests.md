# Phase 4 Test Plan

## Acceptance Criteria

Phase 4 acceptance requires:

- fresh runtime databases initialize SQLite `PRAGMA user_version = 5`.
- startup from Phase 3.5 `user_version = 4` is supported exactly as documented
  by Phase 4 compatibility rules.
- v4-to-v5 startup upgrade backfills existing session snapshots with
  `thinking: {enabled: false, effort: "high"}` and preserves existing runtime
  truth.
- startup fails closed for missing schema version, `0`, legacy `< 4`,
  corrupt/unreadable databases, and unknown future versions `> 5`.
- `status`, `trace`, and `resume` do not create, reset, or upgrade runtime
  databases and fail closed for schema mismatch before interpreting runtime
  truth.
- frozen session config snapshots include the `thinking` object with defaults
  `enabled = false` and `effort = "high"`.
- configured `thinking.enabled` and `thinking.effort` values freeze into the
  session snapshot and resume uses frozen values instead of current
  `config.toml`.
- `thinking.effort` is accepted when `thinking.enabled = false`.
- invalid thinking value types or unsupported effort strings return
  `config_error/invalid_runtime_config`.
- main agent model calls include thinking request options only when frozen
  `thinking.enabled = true`.
- `effort` is sent only when thinking is enabled.
- `view_image` provider calls continue to disable Kimi thinking and do not use
  Phase 4 main-agent thinking settings.
- context compression calls do not use Phase 4 thinking settings.
- the built-in default system prompt is exposed through a phase-neutral
  `SYSTEM_PROMPT` constant and exactly matches the Phase 4 text defined in
  `architecture.md`.
- configured `system_prompt` values continue to override the built-in default
  in frozen config snapshots.
- prompt composition keeps the runtime safety prefix, main agent system prompt,
  active skill context, and tool schema bindings on their existing
  model-visible paths.
- active skill context contains the exact `available_resources` index guidance
  text defined in `architecture.md`.
- the model-visible `load_skill_resource` tool description exactly matches the
  Phase 4 text defined in `architecture.md`.
- provider `thinking` content blocks are stripped before durable conversation,
  trace, events, metrics, compression, resume projection, tool continuation,
  TUI display, or assistant final text.
- tool calls adjacent to stripped thinking blocks still execute through
  ToolBroker and continue correctly.
- thinking-enabled tests do not rely on provider-forced tool choice.
- terminal prompt sessions write
  `.sessions/<session_id>/logs/run_metrics_<timestamp>.json`.
- metrics files are valid UTF-8 JSON and match schema version `1`.
- metrics files are not read by `status`, `trace`, `resume`, checkpoint
  validation, or recovery.
- metrics write failure does not affect terminalization, checkpoint creation,
  ownership release, or exit code.
- fake `rdc` automated readiness runs in ordinary test automation.
- fake `rdc` fixture is test-only and is not included in package runtime code.
- fake `rdc` automated readiness does not require the external
  `renderdoc-gpu-debug` skill.
- fake `rdc rt` writes a real PNG inspected through `view_image`.
- fake `rdc` readiness uses brokered `shell_exec` and `view_image`, not PTY,
  interactive shell, long-running shell, workflow, subagent, MCP, or cache.
- default `view_image` query exactly matches the Phase 4 text defined in
  `architecture.md`.
- default `view_image` query remains generic visual debugging guidance and does
  not include RenderDoc, `rdc`, shader, report-schema, or case-specific
  workflow terms.
- fake `rdc` readiness terminalizes and produces trace plus run metrics.
- deployment smoke builds a wheel, installs it with `uv tool install`, and runs
  installed `debug-agent --help` outside the source checkout.
- adapted `renderdoc-gpu-debug` skill smoke is recorded as a manual canonical
  operation before Phase 4 acceptance.
- Windows + real `rdc` smoke is documented and recorded as a manual canonical
  operation or optional self-hosted automation gate before v1 completion.

## Unit Tests

### Compatibility

- fresh bootstrap writes `PRAGMA user_version = 5`.
- startup with `user_version = 4` follows the documented Phase 4 forward
  compatibility path and ends with schema version `5`.
- startup with `user_version = 4` backfills missing `thinking` objects in
  existing session config snapshots with disabled defaults.
- startup with `user_version = 4` does not rewrite existing terminal recovery
  checkpoint payloads, `payload_sha256`, checkpoint manifests, or frozen
  snapshot checksum fields.
- resume after v4-to-v5 upgrade accepts a Phase 3.5 checkpoint config checksum
  when it matches the upgraded config snapshot with only the default disabled
  `thinking` object omitted.
- resume after v4-to-v5 upgrade rejects a checkpoint config checksum fallback
  when the upgraded config snapshot contains any non-default `thinking` value.
- startup with `user_version = 4` preserves existing active owner, session, run,
  checkpoint, event, durable conversation, Todo Plan, and artifact metadata
  truth.
- startup with `user_version = 4` does not terminalize sessions, release
  ownership, write metrics, or rebuild trace as part of upgrade.
- startup with missing schema version fails closed.
- startup with schema version `0`, `1`, `2`, or `3` fails closed.
- startup with unknown future schema version fails closed.
- corrupt or unreadable runtime database fails closed without reset.
- `status`, `trace`, and `resume` with schema version `4` fail closed unless
  the database has already been upgraded by an allowed startup path.
- `status`, `trace`, and `resume` with missing database do not create one.

### Thinking Config

- config loading defaults `[thinking].enabled` to `false`.
- config loading defaults `[thinking].effort` to `"high"`.
- config loading accepts `enabled = true`.
- config loading accepts `effort = "low"`, `"medium"`, and `"high"`.
- config loading accepts configured `effort` while `enabled = false`.
- config loading rejects non-boolean `enabled`.
- config loading rejects non-string or unsupported `effort`.
- fresh fake-provider and real-provider snapshots include the same `thinking`
  object shape.
- resume restores frozen thinking config and ignores current mutable config.

### Thinking Projection And Stripping

- main-agent request projection omits thinking options when disabled.
- main-agent LangChain Anthropic-compatible model construction omits thinking
  options when disabled.
- main-agent LangChain Anthropic-compatible model construction includes an
  explicit thinking-enable option and frozen effort when enabled.
- main-agent per-call `invoke()` / `stream()` kwargs do not include top-level
  `effort` for Phase 4 thinking projection.
- thinking-enabled request projection is not satisfied by sending `effort`
  alone.
- request projection omits effort when thinking is disabled.
- `view_image` request projection remains unchanged and keeps Kimi thinking
  disabled.
- context compression request projection does not include Phase 4 thinking
  options.
- assistant response parsing removes `thinking` blocks while preserving text
  blocks.
- assistant response parsing removes `thinking` blocks while preserving
  `tool_use` blocks.
- durable conversation rows never contain thinking blocks.
- tool continuation messages sent to later model calls never contain thinking
  blocks.
- trace rendering never includes thinking text.
- metrics files never include thinking text.

### Harness Prompt And Skill Resources

- runtime settings expose `SYSTEM_PROMPT` and no longer expose a
  Phase 0-named default prompt constant.
- built-in non-provider defaults use `SYSTEM_PROMPT` for `system_prompt`.
- the default prompt exactly matches the Phase 4 `SYSTEM_PROMPT` text defined
  in `architecture.md`, including generic runtime harness discipline for tool
  use, Todo Plan usage, active skill resource loading, evidence/failure
  handling, hidden thinking non-disclosure, output formatting, and completion
  checks.
- the default prompt does not include RenderDoc, `rdc`, shader, report-schema,
  or case-specific workflow instructions.
- custom `system_prompt` values from config continue to freeze into the session
  snapshot and override the built-in default.
- active skill context includes `available_resources` and the exact resource
  index guidance text defined in `architecture.md`.
- `load_skill_resource` tool schema description exactly matches the Phase 4
  text defined in `architecture.md`.
- `DEFAULT_VIEW_IMAGE_QUERY` exactly matches the Phase 4 text defined in
  `architecture.md`.
- omitted `view_image.query` still uses the runtime-owned provider instruction
  wrapper with the Phase 4 default query as the analysis focus.
- custom assistant-supplied `view_image.query` remains supported and continues
  to override the default query for that tool call.
- the default `view_image` query does not include RenderDoc, `rdc`, shader,
  report-schema, or case-specific workflow terms.

### Run Metrics

- terminalized prompt session writes one metrics file for the current
  invocation.
- resume terminalization writes a separate metrics file and does not overwrite
  or read earlier metrics.
- metrics filename uses UTC millisecond `YYYYMMDDTHHMMSS.SSSZ`.
- metrics filename collision uses deterministic `_1`, `_2` suffixes and never
  overwrites an existing metrics file.
- metrics JSON includes session id, run id, invocation kind, started/ended
  times, timing, token, and tool sections.
- provider usage values are normalized from direct `usage`, `usage_metadata`,
  and `response_metadata.usage` when available.
- provider usage `input_tokens`, `output_tokens`, and `total_tokens` are
  cumulative across model calls, not last-known values.
- metrics token accounting includes main-agent and context-compression model
  calls and excludes provider usage internal to brokered `view_image`.
- metrics token section records `token_source = "provider"` only when every
  counted model call has provider usage.
- when any counted model call lacks provider usage, including a mixed
  provider/estimated window, metrics use deterministic estimates for the whole
  counted window and record `token_source = "estimated"`.
- when provider usage is unavailable, metrics `input_tokens`, `output_tokens`,
  and `total_tokens` contain cumulative deterministic estimates rather than
  `null`.
- metrics do not include an `estimated_context_tokens` field.
- metrics do not include separate `reasoning_tokens`, `thinking_tokens`, or
  equivalent fields derived from stripped thinking content.
- estimated token usage is marked by estimator version and is not treated as
  billing truth.
- existing REPL/TUI token accounting accumulates provider `input_tokens`,
  `output_tokens`, and `total_tokens` instead of preserving last-known
  input/output values.
- existing REPL/TUI estimated fallback displays cumulative estimated
  `input_tokens`, `output_tokens`, and `total_tokens` rather than latest-context
  token counts.
- tool success/failure counts and breakdown follow `ToolResult.status`.
- missing timing data is reflected in `tool_time_coverage`.
- metrics write uses atomic finalization and never exposes partial JSON.
- injected metrics write failure preserves original terminal outcome and exit
  code.

## Integration Tests

### Fake `rdc` Readiness

- create a temporary workspace with project skill availability and sample
  `.rdc` input.
- create a temporary fake `rdc` executable and prepend it to `PATH`.
- run a scripted one-shot prompt session that calls:
  `rdc doctor`, `rdc open`, `rdc info --json`, `rdc draws --limit 20`,
  `rdc rt ... -o output.png`, `view_image output.png`, and `rdc close`.
- assert each shell command is observed through `shell_exec` tool results.
- assert `output.png` is a valid image and `view_image` ran successfully.
- assert the session terminalized.
- assert `logs/trace.md` exists and contains the rendered tool transcript.
- assert `logs/run_metrics_*.json` exists and includes tool counts.

The fake readiness test may use a local test skill fixture or scripted model
messages. It must not require the external adapted `renderdoc-gpu-debug` skill.

### Deployment Smoke

- build the project with `uv build`.
- install the generated wheel with `uv tool install --force`.
- run installed `debug-agent --help` from a directory outside the source
  checkout.

Deployment smoke may be an operations-level verification rather than a standard
unit/integration pytest if local tool-environment mutation is not appropriate
for ordinary test runs.

## Manual Tests

### Adapted `renderdoc-gpu-debug` Skill Smoke

Record:

- location/version or content hash of the external adapted skill.
- how the skill was made available as a project skill.
- evidence that `renderdoc-gpu-debug` was discoverable and activated.
- session id and run id.
- whether fake `rdc` or real `rdc` was used.
- brokered `shell_exec` command sequence.
- brokered `view_image` result for the exported PNG when an image is produced.
- trace path.
- metrics path.
- result and known limitations.

### Windows + Real `rdc` Smoke

Record:

- Windows version and runner/machine details.
- RenderDoc and `rdc` versions.
- relevant environment variables, including RenderDoc Python/module paths.
- sample `.rdc` path.
- command sequence.
- observed output.
- `debug-agent` session id and run id.
- trace path.
- metrics path.
- result and known limitations.

The command sequence should mirror the fake scenario and use an existing sample
capture when possible.

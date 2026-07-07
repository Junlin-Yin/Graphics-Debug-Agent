# Phase 4 Architecture

## Module Impact

Phase 4 refines existing runtime modules. It does not add a new architecture
layer and does not make RenderDoc, `rdc`, thinking content, or metrics files
runtime truth.

### CLI Entrypoint

Responsibilities:

- initialize fresh Phase 4 runtime databases with `PRAGMA user_version = 5`.
- keep `status`, `trace`, and `resume` fail-closed for schema mismatch without
  creating, resetting, or upgrading `.sessions/runtime.db`.
- run package smoke commands defined by `operations.md` outside the source
  checkout when manually invoked by a developer or CI job.
- surface best-effort warnings when automatic `run_metrics_*.json` writing
  fails after terminalization.
- preserve existing workspace-root semantics for installed console-script usage.

The CLI must not parse metrics files, trace files, fake `rdc` output, or
deployment smoke output as runtime truth.

### Persistence Services

Responsibilities:

- define the Phase 4 SQLite schema version constant as `5`.
- support startup forward compatibility from Phase 3.5 schema version `4` to
  Phase 4 schema version `5` for startup paths that create a new prompt session.
- perform the v4-to-v5 startup upgrade before interpreting Phase 3.5 runtime
  truth, and limit it to the documented schema version update plus
  `sessions.config_snapshot_json` thinking-default backfill.
- preserve existing terminal recovery checkpoint payloads, `payload_sha256`,
  checkpoint manifests, and frozen snapshot checksum fields during the
  v4-to-v5 upgrade.
- reject missing, `0`, legacy `< 4`, corrupt/unreadable, and unknown future
  databases before interpreting runtime truth.
- keep Phase 3.5 read-only and recovery commands fail-closed for non-Phase-4
  schema versions.
- store the expanded frozen config snapshot shape containing the `[thinking]`
  object.
- preserve existing owner/session/run/event/checkpoint/durable conversation
  semantics during upgrade. The upgrade must not terminalize sessions, release
  ownership, rebuild trace or metrics, or repair corrupt databases.
- during resume validation, support the Phase 4 frozen-config checksum
  compatibility rule for upgraded Phase 3.5 snapshots with exactly the default
  disabled `thinking` object. This rule must not rewrite checkpoints or change
  runtime config semantics.

The Phase 4 schema bump is required because the frozen session config snapshot
shape changes. Metrics files are not a schema reason by themselves because they
are non-authoritative filesystem artifacts outside SQLite runtime truth.

### Configuration

Responsibilities:

- parse `[thinking].enabled` as a boolean with default `false`.
- parse `[thinking].effort` with default `"high"`.
- freeze both fields into `sessions.config_snapshot_json`.
- accept configured `thinking.effort` even when `thinking.enabled = false`.
- reject invalid value types or unsupported effort values as
  `config_error/invalid_runtime_config`.
- keep project-local `config.toml` unsupported.

The thinking config group is runtime config, not model-visible tool config and
not policy config. It must not move path policy or shell policy into
`config.toml`.

### Prompt Composition And Harness Prompt

Responsibilities:

- replace the legacy Phase 0 default system prompt with the Phase 4 generic
  `debug-agent` harness prompt.
- use a phase-neutral `SYSTEM_PROMPT` runtime constant for the built-in default
  prompt.
- keep the default prompt domain-neutral. It may describe runtime harness
  discipline, ToolBroker/tool-use discipline, Todo Plan usage for multi-step
  debugging, skill-resource loading discipline, evidence/failure discipline,
  and completion discipline. It must not contain RenderDoc, shader, `rdc`,
  report-schema, or case-specific workflow instructions.
- preserve existing config semantics: a configured `system_prompt` in the
  frozen config snapshot continues to override the built-in default.
- preserve the provider-visible layout where runtime safety and the main agent
  system prompt are system messages in the ordinary `ModelContextFrame`.
- make active skill `available_resources` entries visibly behave as resource
  indexes. The active skill context must tell the model that resource paths are
  not loaded content and that needed resource content must be loaded through
  `load_skill_resource`.

The exact built-in `SYSTEM_PROMPT` text for Phase 4 is:

```text
You are debug-agent, a local debugging assistant that helps complete user
debugging tasks inside the runtime harness.

Authority and scope:
- Follow higher-priority system and runtime instructions first.
- Treat runtime-supplied active skill context as authoritative procedural
  guidance for activated skills, but not as tool authorization and not as task
  evidence unless the user prompt explicitly allows it.
- Follow the user's task prompt for the domain role, task boundary, evidence
  rules, workflow, output format, and completion checks.
- If instructions conflict, required inputs are missing, or the task contract is
  ambiguous, stop and report the issue clearly instead of silently choosing a
  different scope.
- Do only the work requested by the user prompt. Do not add unrelated source
  edits, environment changes, persistence changes, cleanup, or extra
  investigations.

Tool and execution discipline:
- Use only tools exposed by the runtime for this session. Do not claim to use
  unavailable tools or hidden capabilities.
- All tool execution must go through runtime-provided tool interfaces. Do not
  bypass ToolBroker with alternate shell, filesystem, network, process, or
  external-tool access.
- If the runtime exposes a Todo Plan tool for multi-step debugging tasks, keep
  the plan current as the plan, status, or next action changes.
- Treat active skill resource lists as indexes, not loaded content. If a task
  requires the content of a listed resource, call `load_skill_resource` for the
  relevant active skill resource before relying on it. If the result provides an
  `artifact_path` instead of inline content, use `read_file` with that path and
  pagination to inspect the needed content.
- When running shell commands, use the runtime's structured shell execution
  interface and pass commands as argument vectors. Treat quoted command examples
  in user prompts as display examples unless the runtime explicitly requests a
  raw shell string.
- Respect runtime path, approval, timeout, artifact, and audit boundaries. If a
  needed operation is denied or unavailable, report the block instead of working
  around it.

Evidence and failure discipline:
- Do not fabricate observations, tool results, file contents, validation
  results, or completion status.
- Distinguish procedural guidance from factual evidence. A skill, prompt, file
  name, directory name, or prior expectation is not evidence unless the user
  prompt explicitly permits it.
- Do not expose hidden reasoning or provider thinking content. Report concise
  observations, decisions, evidence, and remaining uncertainty instead.
- If a required tool, input, or validation step fails, report the failure and
  preserve the original cause. Do not present a best-effort guess as a verified
  result.

Output discipline:
- Follow the user prompt's requested output format exactly.
- Write only the requested business outputs, unless the user prompt asks for
  notes or intermediate artifacts.
- Do not claim completion until the user-specified existence checks,
  validations, or acceptance checks have passed. If they cannot be run, say
  exactly what remains unverified and why.
```

When active skill context includes `available_resources`, the runtime must make
the following exact guidance model-visible in the active skill context block:

```text
Resource paths listed under available_resources are indexes only, not loaded
content. Call load_skill_resource(skill_name, path) before relying on any listed
resource's content. If load_skill_resource returns an artifact_path instead of
inline content, call read_file(path=artifact_path) with pagination to read the
needed content.
```

This Phase 4 prompt replacement supersedes the legacy Phase 0 default prompt
text for current runtime behavior. It does not change Phase 0 historical
documentation and does not create a new runtime truth schema.

### Runtime Orchestrator

Responsibilities:

- create Phase 4 sessions with frozen thinking config.
- provide main-agent model calls with thinking request options only when the
  frozen snapshot has `thinking.enabled = true`.
- preserve frozen thinking config on resume. For sessions originally created
  under Phase 3.5 and upgraded to Phase 4, the frozen value is the backfilled
  disabled thinking default. Resume must not hot-reload current `config.toml`.
- initialize an in-memory metrics collector per process invocation.
- finalize metrics after a prompt session reaches its terminal outcome and
  before or near the existing automatic terminal trace refresh path.

Metrics write failure must not change runtime lifecycle. It must not roll back
checkpoint creation, block ownership release, write runtime truth, write
audit/run events, or alter the original exit code.

### AgentLoopAdapter And Provider Projection

Responsibilities:

- pass Kimi/Anthropic-compatible thinking request fields to the main model call
  only when enabled in frozen config.
- include `effort` only when thinking is enabled.
- ensure thinking-enabled projection sends an explicit provider thinking-enable
  option; `effort` by itself is not treated as enabling thinking.
- normalize provider usage from the existing Kimi/Anthropic-compatible response
  shapes into the runtime `usage` shape:
  `input_tokens`, `output_tokens`, and `total_tokens`.
- read provider usage from known LangChain response locations, including
  direct `usage`, `usage_metadata`, and `response_metadata.usage`, without
  persisting provider-specific response objects.
- normalize provider responses by removing all `thinking` content blocks before
  accepted assistant content is converted into runtime conversation rows, tool
  call continuation messages, trace input, compression input, or UI output.
- preserve accepted `text` and `tool_use` content blocks.
- not depend on forced provider `tool_choice` semantics when thinking is
  enabled.

Thinking support is narrow Phase 4 support for the existing main provider path.
It is not a provider-generic reasoning API and does not introduce thinking
retention, replay, signature validation, redacted thinking handling, or
provider-native chain-of-thought persistence.

### ToolBroker And Native Tools

Phase 4 does not add new model-visible runtime tools.

ToolBroker responsibilities for RenderDoc readiness are inherited from earlier
phases:

- `shell_exec` runs short structured `rdc` commands under path/shell policy,
  approval, timeout, artifact, result normalization, and audit.
- `view_image` inspects local PNG/JPEG files produced by `rdc rt`.
- `load_skill_resource` remains the brokered path for loading frozen active
  skill resource content or a model-readable artifact reference for large frozen
  resources; its model-visible description should make this trigger clear when a
  task needs content listed under an active skill's `available_resources`.
- both tools preserve their existing result, error, audit, and durable
  conversation contracts.

Phase 4 supersedes the Phase 2 default `view_image` query text while preserving
the existing `view_image` tool schema, provider instruction wrapper, structured
JSON response contract, redaction behavior, and non-RenderDoc-specific runtime
boundary. The default query remains generic visual debugging guidance and must
not mention RenderDoc, `rdc`, shaders, report schemas, or case-specific
workflow.

The exact Phase 4 `DEFAULT_VIEW_IMAGE_QUERY` text is:

```text
Describe the visible contents of the image(s). When multiple images are
provided, compare them directly and call out visible differences or anomalies.
For any anomaly, describe the affected region, color or brightness change,
missing or extra visual elements, transparency, geometry, edges, text, or other
observable symptoms when visible. Transcribe visible text when useful. Note
uncertainty and do not infer causes that are not visible in the image(s).
```

The exact Phase 4 model-visible description for `load_skill_resource` is:

```text
Load one frozen resource file for an active skill. Use this when active skill
instructions or available_resources reference a file whose contents are needed.
Large resources may return an artifact_path instead of inline content; use
read_file with that artifact_path and pagination when the returned content is
needed.
```

Runtime must not add a RenderDoc command allowlist or a RenderDoc-specific tool
handler in Phase 4. The adapted prompt skill owns RenderDoc procedure choices.

### Observability And Metrics

Phase 4 keeps `trace.md` and `events.jsonl` non-authoritative.

The new `run_metrics_*.json` files are also non-authoritative. They are
per-invocation evaluation artifacts written under:

```text
.sessions/<session_id>/logs/run_metrics_<timestamp>.json
```

The metrics writer may use runtime-observed model and tool completion
observations from the current process invocation. It must not reconstruct
metrics from old metrics files, trace files, events JSONL, or checkpoint
payloads.

Provider usage accounting is shared conceptually by run metrics and existing
REPL/TUI token display state:

- normalized provider `input_tokens`, `output_tokens`, and `total_tokens` are
  cumulative counters over model calls in the relevant invocation/session
  window, not last-known values.
- the counted token window includes main-agent and context-compression model
  calls, and excludes provider usage internal to brokered `view_image`.
- `token_source = "provider"` only when every counted model call in the window
  has provider usage; if any counted call lacks provider usage, the whole
  counted window uses deterministic estimates and `token_source = "estimated"`.
- when provider usage is unavailable for the counted window, deterministic
  token estimates provide cumulative estimated `input_tokens`, `output_tokens`,
  and `total_tokens`.
  These estimates are derived from provider-visible requests and accepted
  provider outputs, not from the latest context-token estimate.
- run metrics must not include `estimated_context_tokens`.
- run metrics must not include separate `reasoning_tokens`, `thinking_tokens`,
  or equivalent fields derived from thinking content.
- this token-accounting correction must not change TUI layout, controls,
  streaming authority, or runtime truth semantics.

### Test Fixtures

Fake `rdc` belongs under tests as a fixture/helper. It must not live under
`src/debug_agent`, must not be included in package metadata, and must not become
a user-facing command.

Tests may dynamically create an executable `rdc` wrapper under `tmp_path` and
prepend that directory to `PATH`. The wrapper may maintain per-test state under
the temporary workspace and must write a valid PNG for `rdc rt`.

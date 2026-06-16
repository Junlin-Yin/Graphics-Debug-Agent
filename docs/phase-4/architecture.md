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
- both tools preserve their existing result, error, audit, and durable
  conversation contracts.

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

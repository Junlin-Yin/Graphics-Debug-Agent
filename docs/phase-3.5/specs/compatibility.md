# Phase 3.5 Compatibility Specification

## Purpose

Phase 3.5 changes model-visible native tool schemas, native tool result shapes,
terminal recovery checkpoint tool availability facts, and session frozen config
shape. These are breaking runtime contracts under the Phase 2+ compatibility
rule.

Phase 3.5 identifies fresh databases with:

```text
PHASE_3_5_SCHEMA_USER_VERSION = 4
```

Fresh Phase 3.5 databases must write:

```sql
PRAGMA user_version = 4
```

## Startup Schema Handling

Startup means a REPL or one-shot command path that will create a new
session/run.

Before startup interprets any runtime truth row, it must read SQLite
`PRAGMA user_version` when `.sessions/runtime.db` exists.

For startup only:

- missing schema version, `0`, or any legacy version `< 4` is reset by deleting
  only `.sessions/runtime.db`.
- after deletion, runtime creates a fresh Phase 3.5 database with
  `PRAGMA user_version = 4`.
- runtime must not migrate, reinterpret, preserve, or rewrite legacy rows.
- runtime must not reference orphaned legacy artifacts, logs, traces,
  checkpoint payloads, or session directories left under `.sessions/`.
- user-facing output must say the legacy runtime database was deleted and a
  fresh Phase 3.5 database was created. It must also say legacy files under
  `.sessions/` may remain on disk but are not interpreted by the fresh runtime.

Unknown future schema versions `> 4` must fail closed during startup and must
not be deleted.

## Read-Only And Recovery Commands

`debug-agent status`, `debug-agent trace <session_id>`, and
`debug-agent resume <session_id>` are not startup-reset paths.

They must:

- read SQLite `PRAGMA user_version` before interpreting runtime truth.
- never create `.sessions/runtime.db` when it is missing.
- never delete `.sessions/runtime.db`.
- fail closed for missing schema version, `0`, legacy `< 4`, unknown future
  `> 4`, or any non-startup schema mismatch.

If `.sessions/runtime.db` does not exist:

- `status` returns a read-only no-session observation.
- `trace <session_id>` returns lookup-not-found.
- `resume <session_id>` returns lookup-not-found.

## Error Mapping

Schema compatibility failures use existing Phase 3 normalized reasons:

- `config_error/legacy_schema_version`
- `config_error/unknown_schema_version`
- `config_error/schema_version_missing`

CLI boundary mapping follows the Phase 3 startup/read-only persistence failure
rules.

Startup legacy reset is not a schema-version failure session because runtime
deletes the legacy DB before interpreting rows or creating Phase 3.5 runtime
truth.

## Tool Availability Manifest

Phase 3.5 extends terminal recovery checkpoint `tool_availability`. It does not
add a complete per-tool schema hash or result-contract hash.

Required shape:

```json
{
  "native_tools_contract": {
    "phase": "3.5",
    "contract_marker": "phase-3.5-native-tools-v1"
  },
  "shell_exec": {
    "max_timeout_seconds": 3600
  },
  "view_image": {
    "enabled": false,
    "disabled_reason": "missing_multimodal_config",
    "timeout_seconds": 60,
    "max_tokens": 4096,
    "max_query_chars": 8192,
    "max_analysis_chars": 8192
  },
  "checksum": "sha256:..."
}
```

Rules:

- `native_tools_contract.phase` must be `"3.5"`.
- `native_tools_contract.contract_marker` must be
  `"phase-3.5-native-tools-v1"`.
- `shell_exec.max_timeout_seconds` is derived from frozen
  `execution.max_shell_timeout_seconds`.
- `view_image` facts are derived from frozen multimodal config.
- disabled `view_image` requires a non-empty no-secret disabled reason.
- `checksum` is computed over the manifest excluding `checksum`, using the
  existing canonical JSON checksum helper.
- resume recomputes this manifest from the session frozen config and rejects a
  terminal recovery checkpoint when the stored manifest does not match.

## Tool Contract Compatibility Boundary

Phase 3.5 deliberately does not persist:

- per-tool input schema hashes.
- per-tool result contract hashes.
- deterministic call/audit signature hashes.

Rationale:

- schema version 4 is the cross-version compatibility boundary.
- Phase 3.5 startup/reset and read-only fail-closed behavior prevent old
  sessions and checkpoints from being interpreted under new tool contracts.
- within a Phase 3.5 database, only dynamic tool facts derived from frozen
  config need checkpoint validation.
- keeping the manifest small avoids introducing a new persistence mechanism for
  what is already protected by schema versioning.

## Frozen Config Interaction

Phase 3.5 may use existing frozen config facts, including:

- `agent_loop.max_tool_call_iterations`
- `execution.default_shell_timeout_seconds`
- `execution.max_shell_timeout_seconds`
- `execution.cancellation_timeout_seconds`
- `execution.default_tool_timeout_seconds`
- `multimodal.view_image_enabled`
- `multimodal.view_image_disabled_reason`
- `multimodal.timeout_seconds`
- `multimodal.max_tokens`
- `multimodal.max_query_chars`
- `multimodal.max_analysis_chars`

`agent_loop.max_tool_call_iterations` and
`execution.default_tool_timeout_seconds` are session frozen config facts. They do
not enter terminal recovery checkpoint `tool_availability`, and resume must not
re-read current `config.toml` to rebuild them.

Any new config item introduced outside this list must be documented in
`scope.md`, this compatibility spec, and tests before implementation.

Native tool page sizes are fixed built-in contracts in Phase 3.5 and are not
new config items.

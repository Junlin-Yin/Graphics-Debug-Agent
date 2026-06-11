# Phase 3.5 Compatibility Specification

## Purpose

Phase 3.5 changes model-visible native tool schemas, native tool result shapes,
terminal recovery checkpoint tool availability facts, and session frozen config
shape. These are breaking runtime contracts under the Phase 2+ compatibility
rule.

The project contract explicitly approves Phase 3.5 startup legacy reset. The
reset is limited to startup paths that will create a new REPL or one-shot
session/run; read-only and recovery commands remain fail-closed and
non-destructive.

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
`PRAGMA user_version` when `.sessions/runtime.db` exists. If
`.sessions/runtime.db` exists but cannot be opened as a SQLite database or cannot
serve `PRAGMA user_version` for ordinary persistence reasons, startup fails
closed with `persistence_error/persistence_read_failed`. Corrupt or unreadable
runtime databases are not legacy-reset candidates.

Phase 3.5 runtime config resolution happens before startup schema handling. If
runtime config is invalid, startup must return the config failure before
opening, deleting, resetting, creating, or interpreting `.sessions/runtime.db`.

For startup only:

- missing schema version, `0`, or any legacy version `< 4` is reset by deleting
  `.sessions/runtime.db` and its SQLite sidecar files
  `.sessions/runtime.db-wal` and `.sessions/runtime.db-shm` when present.
- reset is non-interactive and does not ask for active owner confirmation. Legacy
  owner/session/run truth is unsupported by Phase 3.5 startup and may be deleted
  even if the legacy database contained a running owner.
- after deletion, runtime creates a fresh Phase 3.5 database with
  `PRAGMA user_version = 4`.
- runtime must not migrate, reinterpret, preserve, or rewrite legacy rows.
- runtime must not leave legacy SQLite sidecars attached to the fresh Phase 3.5
  runtime database after reset.
- runtime must not reference orphaned legacy artifacts, logs, traces,
  checkpoint payloads, or session directories left under `.sessions/`.
- fresh Phase 3.5 session, log, artifact, checkpoint-payload, and temporary
  paths must not reuse orphaned legacy paths left under `.sessions/`. If a
  freshly generated path collides with an existing orphaned legacy path, startup
  must fail closed with `persistence_error/persistence_write_failed` rather than
  deleting, merging, reusing, or interpreting the legacy directory or file.
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

## Tool Availability Facts

Phase 3.5 extends the existing Phase 3 terminal recovery checkpoint
tool-availability facts. It does not add a complete per-tool schema hash or
result-contract hash.

Phase 3 defines tool availability as a logical recovery input represented by the
existing checkpoint mechanism: either a dedicated
`tool_availability_snapshot_id` plus checksum or an explicitly checksummed field
inside the frozen config/policy snapshot. Phase 3.5 preserves whichever of those
Phase 3 placement forms the implementation already uses. It updates the facts
contained in that existing representation, gated by
`manifest_schema_version = 2` and SQLite `PRAGMA user_version = 4`; it must not
move tool availability to a new placement, migrate between placement forms, or
require implementations to switch between the Phase 3 representation forms.

Because Phase 3.5 changes the terminal recovery checkpoint payload shape,
fresh Phase 3.5 terminal recovery checkpoints must use:

```text
manifest_schema_version = 2
```

Resume must reject Phase 3.5 checkpoints whose checkpoint payload manifest
schema version is not `2`, after the SQLite `PRAGMA user_version = 4` database
gate has passed. `manifest_schema_version` remains the checkpoint payload
version only; it is not a replacement for SQLite `PRAGMA user_version`.

Minimum Phase 3.5 terminal recovery payload shape when the existing Phase 3
representation uses a dedicated tool-availability snapshot reference:

```json
{
  "manifest_schema_version": 2,
  "checkpoint_kind": "terminal_recovery",
  "session_id": "session_...",
  "run_id": "run_...",
  "run_type": "prompt",
  "terminal_status": "completed",
  "terminal_reason": "terminal_completion",
  "terminal_error": null,
  "conversation": {"fact_cut": {}, "projection_snapshot": {}},
  "todo_plan": {},
  "approval_state": {},
  "active_skills": {"records": []},
  "frozen_snapshots": {
    "config_snapshot_id": "config:session_...",
    "config_checksum": "sha256:...",
    "policy_snapshot_id": "policy:session_...",
    "policy_checksum": "sha256:...",
    "tool_availability_snapshot_id": "tool_availability:session_...",
    "tool_availability_checksum": "sha256:..."
  },
  "artifacts": {"artifact_ids": []}
}
```

Other Phase 3 terminal recovery payload fields keep their Phase 3 semantics.
The skeleton above shows one existing Phase 3 placement option, not a
replacement for the detailed Phase 3 conversation, Todo Plan, approval, active
skill, frozen snapshot, artifact, terminal status, or alternate in-snapshot
tool-availability placement contracts.

Required Phase 3.5 tool-availability facts:

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

- `manifest_schema_version` must be `2`.
- `native_tools_contract.phase` must be `"3.5"`.
- `native_tools_contract.contract_marker` must be
  `"phase-3.5-native-tools-v1"`.
- `shell_exec.max_timeout_seconds` is derived from frozen
  `execution.max_shell_timeout_seconds`.
- `view_image` facts are derived from frozen multimodal config.
- disabled `view_image` requires a non-empty no-secret disabled reason.
- `checksum` is computed over the facts object excluding `checksum`, using the
  existing canonical JSON checksum helper.
- resume recomputes these facts from the session frozen config and rejects a
  terminal recovery checkpoint when the stored facts or referenced checksum do
  not match.

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
- keeping the facts small and inside the existing Phase 3 representation avoids
  introducing a new persistence mechanism for what is already protected by
  schema versioning.

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
not enter Phase 3.5 tool-availability facts, and resume must not re-read current
`config.toml` to rebuild them.

Any new config item introduced outside this list must be documented in
`scope.md`, this compatibility spec, and tests before implementation.

Native tool page sizes are fixed built-in contracts in Phase 3.5 and are not
new config items.

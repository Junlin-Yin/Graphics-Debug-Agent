from __future__ import annotations


# Phase 3 runtime database user_version; default prompt routing stays on this path
# until the Phase 3.5 cutover milestone.
PHASE_3_SCHEMA_USER_VERSION = 3

# Phase 3.5 runtime database user_version for internal compatibility seams.
PHASE_3_5_SCHEMA_USER_VERSION = 4

# Phase 2 and Phase 3 share the same current runtime database shape here.
PHASE_2_SCHEMA_USER_VERSION = PHASE_3_SCHEMA_USER_VERSION

# Legacy runtime database versions reset on startup but fail closed in read-only paths.
LEGACY_SCHEMA_USER_VERSIONS = frozenset({0, 1, 2})

# Phase 3.5 treats Phase 0/0.5/1/2/3 databases as legacy in startup reset seams.
PHASE_3_5_LEGACY_SCHEMA_USER_VERSIONS = frozenset({0, 1, 2, 3})

# Startup guidance describes the approved destructive legacy reset behavior.
STARTUP_LEGACY_RESET_GUIDANCE = (
    "Older runtime databases are unsupported by Phase 3. The old "
    ".sessions/runtime.db was deleted and a fresh Phase 3 database was created. "
    "Legacy artifact, log, trace, checkpoint-payload, or session files may "
    "remain under .sessions/ but are not interpreted by the fresh Phase 3 runtime."
)

# Internal Phase 3.5 startup guidance for schema-4 bootstrap seams.
PHASE_3_5_STARTUP_LEGACY_RESET_GUIDANCE = (
    "Older runtime databases are unsupported by Phase 3.5. The old "
    ".sessions/runtime.db was deleted and a fresh Phase 3.5 database was created. "
    "Legacy artifact, log, trace, checkpoint-payload, or session files may remain "
    "under .sessions/ but are not interpreted by the fresh Phase 3.5 runtime."
)

# Read-only guidance is used when status, trace, or resume must not reset state.
READ_ONLY_SCHEMA_FAILURE_GUIDANCE = (
    "Older runtime databases are unsupported by Phase 3. Start a new session or "
    "use a fresh workspace."
)

# Internal Phase 3.5 read-only guidance for schema-4 status/trace/resume seams.
PHASE_3_5_READ_ONLY_SCHEMA_FAILURE_GUIDANCE = (
    "Older runtime databases are unsupported by Phase 3.5. Move or delete "
    ".sessions/, or use a fresh workspace."
)

# Compatibility alias for user-facing Phase 2 database failure messaging.
UNSUPPORTED_PHASE_2_DATABASE_MESSAGE = READ_ONLY_SCHEMA_FAILURE_GUIDANCE

# SQLite DDL is centralized so schema ownership stays inside persistence.
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  workspace_root TEXT NOT NULL,
  status TEXT NOT NULL,
  approval_mode TEXT NOT NULL,
  active_run_id TEXT,
  artifact_root TEXT NOT NULL,
  config_snapshot_json TEXT NOT NULL,
  latest_checkpoint_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error_summary TEXT,
  terminal_reason TEXT,
  terminal_error_json TEXT,
  non_resumable_startup_failure INTEGER NOT NULL DEFAULT 0,
  owner_pid INTEGER,
  owner_host_id TEXT,
  owner_token TEXT,
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  parent_run_id TEXT,
  run_type TEXT NOT NULL,
  status TEXT NOT NULL,
  active_skills_json TEXT NOT NULL,
  latest_checkpoint_id TEXT,
  context_snapshot_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error_summary TEXT,
  terminal_reason TEXT,
  terminal_error_json TEXT,
  non_resumable_startup_failure INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
  FOREIGN KEY(context_snapshot_id) REFERENCES context_snapshots(context_snapshot_id)
);

CREATE TABLE IF NOT EXISTS run_events (
  event_id TEXT PRIMARY KEY,
  timestamp TEXT NOT NULL,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  step_id TEXT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  version INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  state_json TEXT NOT NULL,
  summary TEXT,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT,
  relative_path TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS approval_grants (
  grant_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  scope_signature TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (
    decision IN ('approved_once', 'approved_for_session', 'denied')
  ),
  grant_scope TEXT NOT NULL CHECK (grant_scope IN ('once', 'session', 'none')),
  approval_request TEXT NOT NULL,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_snapshots (
  skill_snapshot_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  execution_mode TEXT NOT NULL,
  source_scope TEXT NOT NULL,
  source_path TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  skill_md_content TEXT NOT NULL,
  skill_md_content_hash TEXT NOT NULL,
  overall_content_hash TEXT NOT NULL,
  payload_artifact_id TEXT,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL,
  UNIQUE(session_id, run_id, skill_name)
);

CREATE TABLE IF NOT EXISTS skill_resource_snapshots (
  resource_snapshot_id TEXT PRIMARY KEY,
  skill_snapshot_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  skill_name TEXT NOT NULL,
  resource_path TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  media_kind TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  inline_text_payload TEXT,
  payload_artifact_id TEXT,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL,
  UNIQUE(skill_snapshot_id, resource_path),
  FOREIGN KEY(skill_snapshot_id) REFERENCES skill_snapshots(skill_snapshot_id)
);

CREATE TABLE IF NOT EXISTS context_snapshots (
  context_snapshot_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  trigger TEXT NOT NULL CHECK (
    trigger IN ('manual', 'omission', 'compression', 'omission | compression')
  ),
  source_checkpoint_id TEXT,
  active_skill_records_json TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  retained_messages_json TEXT NOT NULL,
  omitted_tool_result_count INTEGER NOT NULL,
  evicted_message_count INTEGER NOT NULL DEFAULT 0,
  evicted_model_call_group_count INTEGER NOT NULL DEFAULT 0,
  artifact_refs_json TEXT NOT NULL,
  token_estimate_json TEXT NOT NULL,
  payload_artifact_id TEXT,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS todo_plans (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  items_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS conversation_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  turn_id TEXT,
  message_index INTEGER NOT NULL CHECK (message_index >= 1),
  message_group_id TEXT NOT NULL,
  model_call_id TEXT,
  group_position INTEGER NOT NULL CHECK (group_position >= 0),
  group_status TEXT NOT NULL CHECK (group_status IN ('open', 'closed')),
  group_row_count INTEGER NOT NULL CHECK (group_row_count >= 1),
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'runtime')),
  kind TEXT NOT NULL CHECK (
    kind IN (
      'user_input',
      'assistant_output',
      'assistant_tool_call',
      'tool_result',
      'failure_fact',
      'cancellation_fact',
      'context_summary'
    )
  ),
  content_json TEXT,
  artifact_id TEXT,
  content_sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  tool_call_id TEXT,
  source_event_id TEXT,
  accepted_at TEXT NOT NULL,
  version INTEGER NOT NULL,
  CHECK (
    (content_json IS NOT NULL AND artifact_id IS NULL)
    OR (content_json IS NULL AND artifact_id IS NOT NULL)
  ),
  UNIQUE(run_id, message_index),
  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id),
  FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
);

CREATE TABLE IF NOT EXISTS conversation_projection_state (
  projection_state_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE,
  source_high_watermark INTEGER NOT NULL CHECK (source_high_watermark >= 0),
  message_refs_json TEXT NOT NULL,
  projection_sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  update_reason TEXT NOT NULL CHECK (
    update_reason IN ('message_append', 'omission', 'compression')
  ),
  source_event_id TEXT,
  version INTEGER NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_running_per_workspace
ON sessions(workspace_root)
WHERE status = 'running';
"""

# Terminal recovery checkpoint payload contract version for default Phase 3 paths.
TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION = 1

# Terminal recovery checkpoint payload contract version for internal Phase 3.5 paths.
PHASE_3_5_TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION = 2

# Terminal reasons that may produce terminal recovery checkpoints.
TERMINAL_REASONS = frozenset(
    {
        "terminal_completion",
        "user_exit",
        "user_cancel_idle",
        "terminal_failure",
        "terminal_stale",
    }
)

# Terminal reasons that allow checkpointing with no conversation messages.
ZERO_MESSAGE_REASONS = frozenset({"user_exit"})

# Context snapshot payloads above this size are stored through ArtifactStore.
SNAPSHOT_INLINE_THRESHOLD_BYTES = 16 * 1024

# Skill snapshot payloads above this size are stored through ArtifactStore.
SKILL_INLINE_PAYLOAD_THRESHOLD_BYTES = 16 * 1024

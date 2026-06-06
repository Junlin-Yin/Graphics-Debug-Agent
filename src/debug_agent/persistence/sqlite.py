from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from debug_agent.runtime.errors import NormalizedError

PHASE_3_SCHEMA_USER_VERSION = 3
PHASE_2_SCHEMA_USER_VERSION = PHASE_3_SCHEMA_USER_VERSION
LEGACY_SCHEMA_USER_VERSIONS = frozenset({0, 1, 2})

STARTUP_LEGACY_RESET_GUIDANCE = (
    "Older runtime databases are unsupported by Phase 3. The old "
    ".sessions/runtime.db was deleted and a fresh Phase 3 database was created. "
    "Legacy artifact, log, trace, checkpoint-payload, or session files may "
    "remain under .sessions/ but are not interpreted by the fresh Phase 3 runtime."
)
READ_ONLY_SCHEMA_FAILURE_GUIDANCE = (
    "Older runtime databases are unsupported by Phase 3. Start a new session or "
    "use a fresh workspace."
)
UNSUPPORTED_PHASE_2_DATABASE_MESSAGE = READ_ONLY_SCHEMA_FAILURE_GUIDANCE


SCHEMA = """
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_running_per_workspace
ON sessions(workspace_root)
WHERE status = 'running';
"""


class RuntimeBootstrapError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str = "config_error",
        reason: str = "startup_schema_validation_failed",
        source: str = "persistence",
        recoverable: bool = True,
        normalized_error: NormalizedError | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.reason = reason
        self.source = source
        self.recoverable = recoverable
        self.normalized_error = normalized_error or NormalizedError.create(
            error_class,
            reason,
            message=message,
            scope="persistence",
        )


@dataclass
class RuntimeDatabase:
    path: Path
    connection: sqlite3.Connection
    startup_messages: tuple[str, ...] = ()

    @classmethod
    def bootstrap(cls, workspace_root: str | Path) -> Self:
        sessions_root = Path(workspace_root).resolve() / ".sessions"
        try:
            sessions_root.mkdir(parents=True, exist_ok=True)
            db_path = sessions_root / "runtime.db"
            existed = db_path.exists()
            startup_messages: list[str] = []
            if existed:
                user_version = _read_user_version(db_path)
                if user_version in LEGACY_SCHEMA_USER_VERSIONS:
                    db_path.unlink()
                    existed = False
                    startup_messages.append(STARTUP_LEGACY_RESET_GUIDANCE)
                elif user_version != PHASE_3_SCHEMA_USER_VERSION:
                    raise _schema_error(user_version, startup=True)
            connection = sqlite3.connect(db_path, check_same_thread=False)
            connection.execute("PRAGMA foreign_keys = ON")
            if existed:
                _validate_open_connection_user_version(connection, startup=True)
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version = {PHASE_3_SCHEMA_USER_VERSION}")
            connection.commit()
        except RuntimeBootstrapError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RuntimeBootstrapError(
                f"Runtime database bootstrap failed: {exc}"
            ) from exc
        return cls(path=db_path, connection=connection, startup_messages=tuple(startup_messages))

    @classmethod
    def bootstrap_read_only(cls, workspace_root: str | Path) -> Self | None:
        sessions_root = Path(workspace_root).resolve() / ".sessions"
        db_path = sessions_root / "runtime.db"
        if not db_path.exists():
            return None
        try:
            connection = sqlite3.connect(
                f"file:{db_path.as_posix()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                _validate_open_connection_user_version(connection, startup=False)
            except RuntimeBootstrapError:
                connection.close()
                raise
        except RuntimeBootstrapError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RuntimeBootstrapError(
                f"Runtime database read-only bootstrap failed: {exc}",
                error_class="persistence_error",
                reason="persistence_read_failed",
            ) from exc
        return cls(path=db_path, connection=connection)

    def close(self) -> None:
        self.connection.close()


def _read_user_version(db_path: Path) -> int:
    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeBootstrapError(
            f"Runtime database schema validation failed: {exc}",
            error_class="persistence_error",
            reason="persistence_read_failed",
        ) from exc
    return int(row[0]) if row is not None else 0


def _validate_open_connection_user_version(
    connection: sqlite3.Connection, *, startup: bool
) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    user_version = int(row[0]) if row is not None else 0
    if user_version != PHASE_3_SCHEMA_USER_VERSION:
        raise _schema_error(user_version, startup=startup)


def _schema_error(user_version: int, *, startup: bool) -> RuntimeBootstrapError:
    if user_version == 0:
        reason = "schema_version_missing"
    elif user_version in LEGACY_SCHEMA_USER_VERSIONS:
        reason = "legacy_schema_version"
    else:
        reason = "unknown_schema_version"
    guidance = STARTUP_LEGACY_RESET_GUIDANCE if startup else READ_ONLY_SCHEMA_FAILURE_GUIDANCE
    if reason == "unknown_schema_version":
        guidance = READ_ONLY_SCHEMA_FAILURE_GUIDANCE
    message = f"{guidance} Found user_version={user_version}."
    return RuntimeBootstrapError(
        message,
        error_class="config_error",
        reason=reason,
        recoverable=False,
    )

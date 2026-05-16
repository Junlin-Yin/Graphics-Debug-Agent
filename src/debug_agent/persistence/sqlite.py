from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self


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
  FOREIGN KEY(session_id) REFERENCES sessions(session_id)
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_running_per_workspace
ON sessions(workspace_root)
WHERE status = 'running';
"""


class RuntimeBootstrapError(RuntimeError):
    pass


@dataclass
class RuntimeDatabase:
    path: Path
    connection: sqlite3.Connection

    @classmethod
    def bootstrap(cls, workspace_root: str | Path) -> Self:
        sessions_root = Path(workspace_root).resolve() / ".sessions"
        try:
            sessions_root.mkdir(parents=True, exist_ok=True)
            db_path = sessions_root / "runtime.db"
            connection = sqlite3.connect(db_path, check_same_thread=False)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            connection.commit()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RuntimeBootstrapError(
                f"Runtime database bootstrap failed: {exc}"
            ) from exc
        return cls(path=db_path, connection=connection)

    def close(self) -> None:
        self.connection.close()

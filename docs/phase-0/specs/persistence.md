# Phase 0 Persistence Specification

## Storage Layout

The storage root is `<workspace_root>/.sessions/`.

```text
.sessions/
  runtime.db
  <session_id>/
    artifacts/
    logs/
      engine.log
    temp/
    trace.md
```

SQLite is the metadata and audit truth. Filesystem is the artifact truth.

Default generated session ids use the local creation timestamp plus a short random
suffix:

```text
sess_YYYY-mm-dd-HH-MM-ss-hash
```

`hash` is the first four lowercase hexadecimal characters from the generated
session randomness. Explicitly supplied `session_id` values are stored as
provided.

## SQLite Tables

### `sessions`

```sql
CREATE TABLE sessions (
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
```

`workspace_root` must have at most one active session. In Phase 0, active means `running`.

### `runs`

```sql
CREATE TABLE runs (
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
```

Phase 0 only writes `run_type = 'prompt'`.

`context_snapshot_id` is stored as `NULL` in Phase 0 and is reserved for Phase 1 ContextManager work.

### `run_events`

```sql
CREATE TABLE run_events (
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
```

`run_events` is append-only. No update or delete path is allowed in runtime code.

### `checkpoints`

```sql
CREATE TABLE checkpoints (
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
```

### `artifacts`

```sql
CREATE TABLE artifacts (
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
```

## Write Timing

- Session row is written before model execution begins.
- Run row is written before the first prompt turn.
- `run_started` event is written before calling the adapter.
- `model_call_started` is written before each model call.
- `model_call_completed` or `model_call_failed` is written after each model call.
- For tool-using turns, the model call that requested tools is completed before
  any `tool_call_*` events for those requested tools are written.
- `assistant_message` is written before final checkpoint.
- A `turn` checkpoint is written after each completed one-shot and after each successful REPL turn.
- A `terminal` checkpoint is written when the run/session exits successfully.
- An `error` checkpoint is written before a failed terminal state when enough runtime state exists to persist.
- Session and run terminal statuses are written after checkpoint.

## Checkpoint Rules

- Checkpoint contains authoritative state only.
- Checkpoint references artifacts by `artifact_id`.
- Checkpoint does not contain raw large outputs.
- Latest checkpoint id is copied to `sessions.latest_checkpoint_id` and `runs.latest_checkpoint_id`.

## Artifact Rules

- Runtime never stores long content in SQLite payloads when it can store an artifact reference.
- Artifact paths are relative to `.sessions/<session_id>/`.
- Long-term references use `artifact_id`, not raw filesystem path.
- `temp/` can be cleaned after terminal session status.

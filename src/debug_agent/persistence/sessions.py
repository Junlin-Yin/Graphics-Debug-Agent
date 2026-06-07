from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from debug_agent.persistence.errors import StoreError
from debug_agent.runtime.contracts import CONTRACT_VERSION, Checkpoint, Session, utc_now_iso


@dataclass(frozen=True)
class SessionStore:
    connection: sqlite3.Connection

    def create(
        self,
        *,
        workspace_root: str | Path,
        approval_mode: str,
        config_snapshot: dict,
        session_id: str | None = None,
    ) -> Session:
        session_id = session_id or _default_session_id()
        workspace = Path(workspace_root).resolve()
        artifact_root = workspace / ".sessions" / session_id / "artifacts"
        now = utc_now_iso()
        session = Session(
            session_id=session_id,
            workspace_root=str(workspace),
            status="running",
            approval_mode=approval_mode,
            active_run_id=None,
            artifact_root=str(artifact_root),
            config_snapshot=config_snapshot,
            latest_checkpoint_id=None,
            created_at=now,
            updated_at=now,
            error_summary=None,
        )
        try:
            self.connection.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace_root, status, approval_mode,
                    active_run_id, artifact_root, config_snapshot_json,
                    latest_checkpoint_id, created_at, updated_at,
                    error_summary, terminal_reason, terminal_error_json,
                    non_resumable_startup_failure, version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.workspace_root,
                    session.status,
                    session.approval_mode,
                    session.active_run_id,
                    session.artifact_root,
                    json.dumps(session.config_snapshot, ensure_ascii=False, sort_keys=True),
                    session.latest_checkpoint_id,
                    session.created_at,
                    session.updated_at,
                    session.error_summary,
                    session.terminal_reason,
                    None,
                    1 if session.non_resumable_startup_failure else 0,
                    session.version,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise StoreError(
                error_class="user_error",
                message="An active session already owns this workspace.",
                recoverable=True,
            ) from exc
        return session

    def get(self, session_id: str) -> Session:
        row = self.connection.execute(
            """
            SELECT session_id, workspace_root, status, approval_mode,
                   active_run_id, artifact_root, config_snapshot_json,
                   latest_checkpoint_id, created_at, updated_at,
                   error_summary, terminal_reason, terminal_error_json,
                   non_resumable_startup_failure, version
            FROM sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise StoreError(
                error_class="user_error",
                message=f"No session found for id: {session_id}",
                recoverable=True,
            )
        return _session_from_row(row)

    def find_active_for_workspace(self, workspace_root: str | Path) -> Session | None:
        row = self.connection.execute(
            """
            SELECT session_id, workspace_root, status, approval_mode,
                   active_run_id, artifact_root, config_snapshot_json,
                   latest_checkpoint_id, created_at, updated_at,
                   error_summary, terminal_reason, terminal_error_json,
                   non_resumable_startup_failure, version
            FROM sessions
            WHERE workspace_root = ? AND status = 'running'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(Path(workspace_root).resolve()),),
        ).fetchone()
        return None if row is None else _session_from_row(row)

    def set_active_run(self, session_id: str, run_id: str | None) -> Session:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE sessions
            SET active_run_id = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (run_id, now, session_id),
        )
        self.connection.commit()
        return self.get(session_id)

    def record_owner(
        self,
        *,
        session_id: str,
        owner_pid: int,
        owner_host_id: str,
        owner_token: str,
    ) -> Session:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE sessions
            SET owner_pid = ?, owner_host_id = ?, owner_token = ?, updated_at = ?
            WHERE session_id = ? AND status = 'running'
            """,
            (owner_pid, owner_host_id, owner_token, now, session_id),
        )
        self.connection.commit()
        return self.get(session_id)

    def release_ownership(self, *, session_id: str, owner_token: str) -> bool:
        now = utc_now_iso()
        result = self.connection.execute(
            """
            UPDATE sessions
            SET owner_pid = NULL, owner_host_id = NULL, owner_token = NULL,
                updated_at = ?
            WHERE session_id = ? AND status IN ('completed', 'failed')
              AND owner_token = ?
            """,
            (now, session_id, owner_token),
        )
        self.connection.commit()
        return result.rowcount == 1

    def fail_close_stale_owner(
        self,
        *,
        workspace_root: str | Path,
        session_id: str,
        run_id: str,
        owner_pid: int,
        owner_host_id: str,
        owner_token: str,
        checkpoint_id: str | None,
        checkpoint: Checkpoint | None = None,
    ) -> bool:
        workspace = str(Path(workspace_root).resolve())
        if checkpoint is not None:
            checkpoint_id = checkpoint.checkpoint_id
        now = utc_now_iso()
        event = {
            "stale_proof_summary": {
                "host_match": True,
                "pid_absent": True,
                "token_fenced": True,
            }
        }
        with self.connection:
            session_result = self.connection.execute(
                """
                UPDATE sessions
                SET status = 'failed', active_run_id = NULL,
                    latest_checkpoint_id = ?, error_summary = ?,
                    terminal_reason = 'terminal_stale', terminal_error_json = NULL,
                    non_resumable_startup_failure = 0,
                    owner_pid = NULL, owner_host_id = NULL, owner_token = NULL,
                    updated_at = ?
                WHERE session_id = ? AND workspace_root = ? AND status = 'running'
                  AND active_run_id = ? AND owner_pid = ? AND owner_host_id = ?
                  AND owner_token = ?
                """,
                (
                    checkpoint_id,
                    "Session failed closed because a prior active owner was proven stale.",
                    now,
                    session_id,
                    workspace,
                    run_id,
                    owner_pid,
                    owner_host_id,
                    owner_token,
                ),
            )
            if session_result.rowcount != 1:
                return False
            run_result = self.connection.execute(
                """
                UPDATE runs
                SET status = 'failed', latest_checkpoint_id = ?,
                    error_summary = ?, terminal_reason = 'terminal_stale',
                    terminal_error_json = NULL, non_resumable_startup_failure = 0,
                    updated_at = ?
                WHERE run_id = ? AND session_id = ? AND status = 'running'
                """,
                (
                    checkpoint_id,
                    "Run failed closed because a prior active owner was proven stale.",
                    now,
                    run_id,
                    session_id,
                ),
            )
            if run_result.rowcount != 1:
                raise StoreError(
                    error_class="persistence_error",
                    message="Stale fail-close run transition failed.",
                    recoverable=False,
                )
            if checkpoint is not None:
                self.connection.execute(
                    """
                    INSERT INTO checkpoints (
                        checkpoint_id, session_id, run_id, kind, state_json,
                        summary, created_at, version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.checkpoint_id,
                        checkpoint.session_id,
                        checkpoint.run_id,
                        checkpoint.kind,
                        json.dumps(
                            checkpoint.state,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        checkpoint.summary,
                        checkpoint.created_at,
                        checkpoint.version,
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO run_events (
                    event_id, timestamp, session_id, run_id, step_id, kind,
                    payload_json, version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evt_{uuid4().hex}",
                    now,
                    session_id,
                    run_id,
                    None,
                    "stale_fail_closed",
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                    1,
                ),
            )
        return True

    def update_approval_mode(self, session_id: str, approval_mode: str) -> Session:
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE sessions
            SET approval_mode = ?, updated_at = ?
            WHERE session_id = ? AND status = 'running'
            """,
            (approval_mode, now, session_id),
        )
        self.connection.commit()
        return self.get(session_id)

    def mark_completed(
        self, session_id: str, latest_checkpoint_id: str | None = None
    ) -> Session:
        return self._mark_terminal(
            session_id,
            status="completed",
            error_summary=None,
            latest_checkpoint_id=latest_checkpoint_id,
            terminal_reason=None,
            terminal_error=None,
            non_resumable_startup_failure=False,
        )

    def mark_failed(
        self,
        session_id: str,
        error_summary: str,
        latest_checkpoint_id: str | None = None,
    ) -> Session:
        return self._mark_terminal(
            session_id,
            status="failed",
            error_summary=error_summary,
            latest_checkpoint_id=latest_checkpoint_id,
            terminal_reason=None,
            terminal_error=None,
            non_resumable_startup_failure=False,
        )

    def mark_startup_failure(self, session_id: str, error_summary: str) -> Session:
        return self._mark_terminal(
            session_id,
            status="failed",
            error_summary=error_summary,
            latest_checkpoint_id=None,
            terminal_reason="startup_failure",
            terminal_error=None,
            non_resumable_startup_failure=True,
        )

    def revive_for_explicit_resume(
        self,
        *,
        session_id: str,
        run_id: str,
        owner_pid: int,
        owner_host_id: str,
        owner_token: str,
    ) -> None:
        now = utc_now_iso()
        result = self.connection.execute(
            """
            UPDATE sessions
            SET status = 'running', active_run_id = ?, updated_at = ?,
                owner_pid = ?, owner_host_id = ?, owner_token = ?
            WHERE session_id = ? AND status IN ('completed', 'failed')
              AND non_resumable_startup_failure = 0
              AND latest_checkpoint_id IS NOT NULL
            """,
            (
                run_id,
                now,
                owner_pid,
                owner_host_id,
                owner_token,
                session_id,
            ),
        )
        if result.rowcount != 1:
            raise StoreError(
                error_class="persistence_error",
                message="Explicit resume session transition failed.",
                recoverable=False,
            )

    def _mark_terminal(
        self,
        session_id: str,
        *,
        status: str,
        error_summary: str | None,
        latest_checkpoint_id: str | None,
        terminal_reason: str | None,
        terminal_error: dict | None,
        non_resumable_startup_failure: bool,
    ) -> Session:
        current = self.get(session_id)
        if current.status != "running":
            raise StoreError(
                error_class="internal_error",
                message=f"Invalid session transition from {current.status} to {status}",
            )
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE sessions
            SET status = ?, active_run_id = NULL, latest_checkpoint_id = ?,
                error_summary = ?, terminal_reason = ?, terminal_error_json = ?,
                non_resumable_startup_failure = ?, updated_at = ?
            WHERE session_id = ? AND status = 'running'
            """,
            (
                status,
                latest_checkpoint_id,
                error_summary,
                terminal_reason,
                None
                if terminal_error is None
                else json.dumps(terminal_error, ensure_ascii=False, sort_keys=True),
                1 if non_resumable_startup_failure else 0,
                now,
                session_id,
            ),
        )
        self.connection.commit()
        return self.get(session_id)


def _session_from_row(row: tuple) -> Session:
    return Session(
        session_id=row[0],
        workspace_root=row[1],
        status=row[2],
        approval_mode=row[3],
        active_run_id=row[4],
        artifact_root=row[5],
        config_snapshot=json.loads(row[6]),
        latest_checkpoint_id=row[7],
        created_at=row[8],
        updated_at=row[9],
        error_summary=row[10],
        terminal_reason=row[11],
        terminal_error=None if row[12] is None else json.loads(row[12]),
        non_resumable_startup_failure=bool(row[13]),
        version=row[14],
    )


def _default_session_id() -> str:
    created_at = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return f"sess_{created_at}-{uuid4().hex[:4]}"

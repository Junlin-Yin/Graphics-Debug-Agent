from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from debug_agent.persistence.errors import StoreError
from debug_agent.runtime.contracts import Run, utc_now_iso


@dataclass(frozen=True)
class RunStore:
    connection: sqlite3.Connection

    def create_prompt_run(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> Run:
        run_id = run_id or f"run_{uuid4().hex}"
        now = utc_now_iso()
        run = Run(
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            run_type="prompt",
            status="running",
            active_skills=[],
            latest_checkpoint_id=None,
            context_snapshot_id=None,
            created_at=now,
            updated_at=now,
            error_summary=None,
        )
        self.connection.execute(
            """
            INSERT INTO runs (
                run_id, session_id, parent_run_id, run_type, status,
                active_skills_json, latest_checkpoint_id, context_snapshot_id,
                created_at, updated_at, error_summary, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.session_id,
                run.parent_run_id,
                run.run_type,
                run.status,
                json.dumps(run.active_skills),
                run.latest_checkpoint_id,
                run.context_snapshot_id,
                run.created_at,
                run.updated_at,
                run.error_summary,
                run.version,
            ),
        )
        self.connection.commit()
        return run

    def get(self, run_id: str) -> Run:
        row = self.connection.execute(
            """
            SELECT run_id, session_id, parent_run_id, run_type, status,
                   active_skills_json, latest_checkpoint_id, context_snapshot_id,
                   created_at, updated_at, error_summary, version
            FROM runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise StoreError(
                error_class="user_error",
                message=f"No run found for id: {run_id}",
                recoverable=True,
            )
        return _run_from_row(row)

    def latest_for_session(self, session_id: str) -> Run | None:
        row = self.connection.execute(
            """
            SELECT run_id, session_id, parent_run_id, run_type, status,
                   active_skills_json, latest_checkpoint_id, context_snapshot_id,
                   created_at, updated_at, error_summary, version
            FROM runs
            WHERE session_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return None if row is None else _run_from_row(row)

    def list_for_session(self, session_id: str) -> list[Run]:
        rows = self.connection.execute(
            """
            SELECT run_id, session_id, parent_run_id, run_type, status,
                   active_skills_json, latest_checkpoint_id, context_snapshot_id,
                   created_at, updated_at, error_summary, version
            FROM runs
            WHERE session_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (session_id,),
        ).fetchall()
        return [_run_from_row(row) for row in rows]

    def mark_completed(
        self, run_id: str, latest_checkpoint_id: str | None = None
    ) -> Run:
        return self._mark_terminal(
            run_id,
            status="completed",
            error_summary=None,
            latest_checkpoint_id=latest_checkpoint_id,
        )

    def mark_failed(
        self,
        run_id: str,
        error_summary: str,
        latest_checkpoint_id: str | None = None,
    ) -> Run:
        return self._mark_terminal(
            run_id,
            status="failed",
            error_summary=error_summary,
            latest_checkpoint_id=latest_checkpoint_id,
        )

    def _mark_terminal(
        self,
        run_id: str,
        *,
        status: str,
        error_summary: str | None,
        latest_checkpoint_id: str | None,
    ) -> Run:
        current = self.get(run_id)
        if current.status != "running":
            raise StoreError(
                error_class="internal_error",
                message=f"Invalid run transition from {current.status} to {status}",
            )
        now = utc_now_iso()
        self.connection.execute(
            """
            UPDATE runs
            SET status = ?, latest_checkpoint_id = ?, error_summary = ?,
                updated_at = ?
            WHERE run_id = ?
            """,
            (status, latest_checkpoint_id, error_summary, now, run_id),
        )
        self.connection.commit()
        return self.get(run_id)


def _run_from_row(row: tuple) -> Run:
    return Run(
        run_id=row[0],
        session_id=row[1],
        parent_run_id=row[2],
        run_type=row[3],
        status=row[4],
        active_skills=json.loads(row[5]),
        latest_checkpoint_id=row[6],
        context_snapshot_id=row[7],
        created_at=row[8],
        updated_at=row[9],
        error_summary=row[10],
        version=row[11],
    )

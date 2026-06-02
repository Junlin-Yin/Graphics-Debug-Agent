from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from debug_agent.persistence.errors import StoreError
from debug_agent.runtime.contracts import Checkpoint


@dataclass(frozen=True)
class CheckpointStore:
    connection: sqlite3.Connection

    def save(self, checkpoint: Checkpoint) -> Checkpoint:
        self.connection.execute(
            """
            INSERT INTO checkpoints (
                checkpoint_id, session_id, run_id, kind, state_json, summary,
                created_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.session_id,
                checkpoint.run_id,
                checkpoint.kind,
                json.dumps(checkpoint.state, ensure_ascii=False, sort_keys=True),
                checkpoint.summary,
                checkpoint.created_at,
                checkpoint.version,
            ),
        )
        self.connection.execute(
            """
            UPDATE sessions
            SET latest_checkpoint_id = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.created_at,
                checkpoint.session_id,
            ),
        )
        self.connection.execute(
            """
            UPDATE runs
            SET latest_checkpoint_id = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.created_at,
                checkpoint.run_id,
            ),
        )
        self.connection.commit()
        return checkpoint

    def get(self, checkpoint_id: str) -> Checkpoint:
        row = self.connection.execute(
            """
            SELECT checkpoint_id, session_id, run_id, kind, state_json, summary,
                   created_at, version
            FROM checkpoints
            WHERE checkpoint_id = ?
            """,
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            raise StoreError(
                error_class="user_error",
                message=f"No checkpoint found for id: {checkpoint_id}",
                recoverable=True,
            )
        return _checkpoint_from_row(row)

    def latest_for_run(self, run_id: str) -> Checkpoint | None:
        row = self.connection.execute(
            """
            SELECT checkpoint_id, session_id, run_id, kind, state_json, summary,
                   created_at, version
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return None if row is None else _checkpoint_from_row(row)

    def list_for_session(self, session_id: str) -> list[Checkpoint]:
        rows = self.connection.execute(
            """
            SELECT checkpoint_id, session_id, run_id, kind, state_json, summary,
                   created_at, version
            FROM checkpoints
            WHERE session_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (session_id,),
        ).fetchall()
        return [_checkpoint_from_row(row) for row in rows]


def _checkpoint_from_row(row: tuple) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=row[0],
        session_id=row[1],
        run_id=row[2],
        kind=row[3],
        state=json.loads(row[4]),
        summary=row[5],
        created_at=row[6],
        version=row[7],
    )

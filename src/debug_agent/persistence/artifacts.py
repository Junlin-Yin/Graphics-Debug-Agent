from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from debug_agent.persistence.errors import StoreError
from debug_agent.runtime.contracts import Artifact, utc_now_iso


@dataclass(frozen=True)
class ArtifactStore:
    connection: sqlite3.Connection

    def write_text(
        self,
        *,
        session_id: str,
        run_id: str | None,
        filename: str,
        content: str,
        metadata: dict,
        artifact_id: str | None = None,
    ) -> Artifact:
        artifact_id = artifact_id or f"art_{uuid4().hex}"
        relative_path = Path(session_id) / "artifacts" / Path(filename).name
        absolute_path = self._sessions_root() / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_text(content, encoding="utf-8")
        return self._insert(
            artifact_id=artifact_id,
            session_id=session_id,
            run_id=run_id,
            relative_path=relative_path.as_posix(),
            artifact_type="text",
            metadata=metadata,
        )

    def register_existing_file(
        self,
        *,
        session_id: str,
        run_id: str | None,
        path: str | Path,
        artifact_type: str,
        metadata: dict,
        artifact_id: str | None = None,
    ) -> Artifact:
        artifact_id = artifact_id or f"art_{uuid4().hex}"
        absolute_path = Path(path).resolve()
        session_root = (self._sessions_root() / session_id).resolve()
        try:
            relative_path = absolute_path.relative_to(self._sessions_root())
            absolute_path.relative_to(session_root)
        except ValueError as exc:
            raise StoreError(
                error_class="policy_denied",
                message="Artifact path must be under the session root.",
                recoverable=True,
            ) from exc
        return self._insert(
            artifact_id=artifact_id,
            session_id=session_id,
            run_id=run_id,
            relative_path=relative_path.as_posix(),
            artifact_type=artifact_type,
            metadata=metadata,
        )

    def get(self, artifact_id: str) -> Artifact:
        row = self.connection.execute(
            """
            SELECT artifact_id, session_id, run_id, relative_path, artifact_type,
                   metadata_json, created_at, version
            FROM artifacts
            WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise StoreError(
                error_class="user_error",
                message=f"No artifact found for id: {artifact_id}",
                recoverable=True,
            )
        return _artifact_from_row(row)

    def resolve_path(self, artifact_id: str) -> Path:
        artifact = self.get(artifact_id)
        return self._sessions_root() / artifact.relative_path

    def list_for_session(self, session_id: str) -> list[Artifact]:
        rows = self.connection.execute(
            """
            SELECT artifact_id, session_id, run_id, relative_path, artifact_type,
                   metadata_json, created_at, version
            FROM artifacts
            WHERE session_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (session_id,),
        ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def _insert(
        self,
        *,
        artifact_id: str,
        session_id: str,
        run_id: str | None,
        relative_path: str,
        artifact_type: str,
        metadata: dict,
    ) -> Artifact:
        artifact = Artifact(
            artifact_id=artifact_id,
            session_id=session_id,
            run_id=run_id,
            relative_path=relative_path,
            artifact_type=artifact_type,
            metadata=metadata,
            created_at=utc_now_iso(),
        )
        self.connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, session_id, run_id, relative_path, artifact_type,
                metadata_json, created_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.artifact_id,
                artifact.session_id,
                artifact.run_id,
                artifact.relative_path,
                artifact.artifact_type,
                json.dumps(artifact.metadata, sort_keys=True),
                artifact.created_at,
                artifact.version,
            ),
        )
        self.connection.commit()
        return artifact

    def _sessions_root(self) -> Path:
        row = self.connection.execute("PRAGMA database_list").fetchone()
        if row is None or not row[2]:
            raise StoreError(
                error_class="internal_error",
                message="Runtime database path is unavailable.",
            )
        return Path(row[2]).resolve().parent


def _artifact_from_row(row: tuple) -> Artifact:
    return Artifact(
        artifact_id=row[0],
        session_id=row[1],
        run_id=row[2],
        relative_path=row[3],
        artifact_type=row[4],
        metadata=json.loads(row[5]),
        created_at=row[6],
        version=row[7],
    )

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from debug_agent.skills.registry import SkillSnapshot


@dataclass(frozen=True)
class SkillSnapshotStore:
    connection: sqlite3.Connection

    def save_many(self, snapshots: list[SkillSnapshot]) -> None:
        for snapshot in snapshots:
            self.connection.execute(
                """
                INSERT INTO skill_snapshots (
                    skill_snapshot_id, session_id, run_id, skill_name,
                    execution_mode, source_scope, source_path, manifest_json,
                    skill_md_content, skill_md_content_hash, overall_content_hash,
                    payload_artifact_id, created_at, version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.skill_snapshot_id,
                    snapshot.session_id,
                    snapshot.run_id,
                    snapshot.name,
                    snapshot.execution_mode,
                    snapshot.source_scope,
                    snapshot.source_path,
                    json.dumps(snapshot.manifest, sort_keys=True),
                    snapshot.skill_md_content,
                    snapshot.skill_md_content_hash,
                    snapshot.overall_content_hash,
                    snapshot.payload_artifact_id,
                    snapshot.created_at,
                    snapshot.version,
                ),
            )
            for reference in snapshot.references:
                self.connection.execute(
                    """
                    INSERT INTO skill_reference_snapshots (
                        reference_snapshot_id, skill_snapshot_id, session_id,
                        run_id, skill_name, reference_path, media_kind,
                        size_bytes, content_hash, inline_text_payload,
                        payload_artifact_id, created_at, version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reference.reference_snapshot_id,
                        snapshot.skill_snapshot_id,
                        snapshot.session_id,
                        snapshot.run_id,
                        snapshot.name,
                        reference.reference_path,
                        reference.media_kind,
                        reference.size_bytes,
                        reference.content_hash,
                        reference.inline_text_payload,
                        reference.payload_artifact_id,
                        reference.created_at,
                        reference.version,
                    ),
                )
        self.connection.commit()

    def available_skill_headers(self, *, session_id: str, run_id: str) -> str:
        rows = self.connection.execute(
            """
            SELECT skill_name, execution_mode, source_scope, manifest_json,
                   overall_content_hash
            FROM skill_snapshots
            WHERE session_id = ? AND run_id = ?
            ORDER BY skill_name ASC
            """,
            (session_id, run_id),
        ).fetchall()
        if not rows:
            return "Available prompt skills: none"
        lines = ["Available prompt skills for activation:"]
        for name, execution_mode, source_scope, manifest_json, content_hash in rows:
            manifest = json.loads(manifest_json)
            lines.append(
                "- "
                f"{name}: {manifest['description']} "
                f"(mode={execution_mode}, scope={source_scope}, hash={content_hash})"
            )
        return "\n".join(lines)


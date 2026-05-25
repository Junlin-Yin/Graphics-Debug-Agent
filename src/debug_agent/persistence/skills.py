from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from debug_agent.skills.registry import SkillSnapshot


@dataclass(frozen=True)
class FrozenSkillSnapshot:
    skill_snapshot_id: str
    session_id: str
    run_id: str
    skill_name: str
    execution_mode: str
    manifest: dict
    skill_md_content: str
    skill_md_content_hash: str
    overall_content_hash: str


@dataclass(frozen=True)
class SkillListingRecord:
    name: str
    description: str
    execution_mode: str
    source_scope: str
    content_hash: str
    active: bool


@dataclass(frozen=True)
class FrozenReferenceSnapshot:
    reference_snapshot_id: str
    skill_snapshot_id: str
    session_id: str
    run_id: str
    skill_name: str
    reference_path: str
    media_kind: str
    size_bytes: int
    content_hash: str
    inline_text_payload: str | None
    payload_artifact_id: str | None


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

    def list_for_run(
        self,
        *,
        session_id: str,
        run_id: str,
        active_skills: list[dict],
    ) -> list[SkillListingRecord]:
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
        active_keys = {
            (record.get("name"), record.get("content_hash"))
            for record in active_skills
            if isinstance(record, dict)
        }
        records: list[SkillListingRecord] = []
        for name, execution_mode, source_scope, manifest_json, content_hash in rows:
            manifest = json.loads(manifest_json)
            records.append(
                SkillListingRecord(
                    name=name,
                    description=str(manifest["description"]),
                    execution_mode=execution_mode,
                    source_scope=source_scope,
                    content_hash=content_hash,
                    active=(name, content_hash) in active_keys,
                )
            )
        return records

    def get_skill(
        self, *, session_id: str, run_id: str, skill_name: str
    ) -> FrozenSkillSnapshot | None:
        row = self.connection.execute(
            """
            SELECT skill_snapshot_id, session_id, run_id, skill_name,
                   execution_mode, manifest_json, skill_md_content,
                   skill_md_content_hash, overall_content_hash
            FROM skill_snapshots
            WHERE session_id = ? AND run_id = ? AND skill_name = ?
            """,
            (session_id, run_id, skill_name),
        ).fetchone()
        if row is None:
            return None
        return FrozenSkillSnapshot(
            skill_snapshot_id=row[0],
            session_id=row[1],
            run_id=row[2],
            skill_name=row[3],
            execution_mode=row[4],
            manifest=json.loads(row[5]),
            skill_md_content=row[6],
            skill_md_content_hash=row[7],
            overall_content_hash=row[8],
        )

    def get_reference(
        self,
        *,
        skill_snapshot_id: str,
        reference_path: str,
    ) -> FrozenReferenceSnapshot | None:
        row = self.connection.execute(
            """
            SELECT reference_snapshot_id, skill_snapshot_id, session_id,
                   run_id, skill_name, reference_path, media_kind,
                   size_bytes, content_hash, inline_text_payload,
                   payload_artifact_id
            FROM skill_reference_snapshots
            WHERE skill_snapshot_id = ? AND reference_path = ?
            """,
            (skill_snapshot_id, reference_path),
        ).fetchone()
        if row is None:
            return None
        return FrozenReferenceSnapshot(
            reference_snapshot_id=row[0],
            skill_snapshot_id=row[1],
            session_id=row[2],
            run_id=row[3],
            skill_name=row[4],
            reference_path=row[5],
            media_kind=row[6],
            size_bytes=row[7],
            content_hash=row[8],
            inline_text_payload=row[9],
            payload_artifact_id=row[10],
        )

    def list_references(self, *, skill_snapshot_id: str) -> list[FrozenReferenceSnapshot]:
        rows = self.connection.execute(
            """
            SELECT reference_snapshot_id, skill_snapshot_id, session_id,
                   run_id, skill_name, reference_path, media_kind,
                   size_bytes, content_hash, inline_text_payload,
                   payload_artifact_id
            FROM skill_reference_snapshots
            WHERE skill_snapshot_id = ?
            ORDER BY reference_path ASC
            """,
            (skill_snapshot_id,),
        ).fetchall()
        return [
            FrozenReferenceSnapshot(
                reference_snapshot_id=row[0],
                skill_snapshot_id=row[1],
                session_id=row[2],
                run_id=row[3],
                skill_name=row[4],
                reference_path=row[5],
                media_kind=row[6],
                size_bytes=row[7],
                content_hash=row[8],
                inline_text_payload=row[9],
                payload_artifact_id=row[10],
            )
            for row in rows
        ]

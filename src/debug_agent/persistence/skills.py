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
class FrozenResourceSnapshot:
    resource_snapshot_id: str
    skill_snapshot_id: str
    session_id: str
    run_id: str
    skill_name: str
    resource_path: str
    resource_kind: str
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
                    json.dumps(snapshot.manifest, ensure_ascii=False, sort_keys=True),
                    snapshot.skill_md_content,
                    snapshot.skill_md_content_hash,
                    snapshot.overall_content_hash,
                    snapshot.payload_artifact_id,
                    snapshot.created_at,
                    snapshot.version,
                ),
            )
            for resource in snapshot.resources:
                self.connection.execute(
                    """
                    INSERT INTO skill_resource_snapshots (
                        resource_snapshot_id, skill_snapshot_id, session_id,
                        run_id, skill_name, resource_path, resource_kind, media_kind,
                        size_bytes, content_hash, inline_text_payload,
                        payload_artifact_id, created_at, version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource.resource_snapshot_id,
                        snapshot.skill_snapshot_id,
                        snapshot.session_id,
                        snapshot.run_id,
                        snapshot.name,
                        resource.resource_path,
                        resource.resource_kind,
                        resource.media_kind,
                        resource.size_bytes,
                        resource.content_hash,
                        resource.inline_text_payload,
                        resource.payload_artifact_id,
                        resource.created_at,
                        resource.version,
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

    def get_resource(
        self,
        *,
        skill_snapshot_id: str,
        resource_path: str,
    ) -> FrozenResourceSnapshot | None:
        row = self.connection.execute(
            """
            SELECT resource_snapshot_id, skill_snapshot_id, session_id,
                   run_id, skill_name, resource_path, resource_kind, media_kind,
                   size_bytes, content_hash, inline_text_payload,
                   payload_artifact_id
            FROM skill_resource_snapshots
            WHERE skill_snapshot_id = ? AND resource_path = ?
            """,
            (skill_snapshot_id, resource_path),
        ).fetchone()
        if row is None:
            return None
        return FrozenResourceSnapshot(
            resource_snapshot_id=row[0],
            skill_snapshot_id=row[1],
            session_id=row[2],
            run_id=row[3],
            skill_name=row[4],
            resource_path=row[5],
            resource_kind=row[6],
            media_kind=row[7],
            size_bytes=row[8],
            content_hash=row[9],
            inline_text_payload=row[10],
            payload_artifact_id=row[11],
        )

    def list_resources(self, *, skill_snapshot_id: str) -> list[FrozenResourceSnapshot]:
        rows = self.connection.execute(
            """
            SELECT resource_snapshot_id, skill_snapshot_id, session_id,
                   run_id, skill_name, resource_path, resource_kind, media_kind,
                   size_bytes, content_hash, inline_text_payload,
                   payload_artifact_id
            FROM skill_resource_snapshots
            WHERE skill_snapshot_id = ?
            ORDER BY resource_path ASC
            """,
            (skill_snapshot_id,),
        ).fetchall()
        return [
            FrozenResourceSnapshot(
                resource_snapshot_id=row[0],
                skill_snapshot_id=row[1],
                session_id=row[2],
                run_id=row[3],
                skill_name=row[4],
                resource_path=row[5],
                resource_kind=row[6],
                media_kind=row[7],
                size_bytes=row[8],
                content_hash=row[9],
                inline_text_payload=row[10],
                payload_artifact_id=row[11],
            )
            for row in rows
        ]

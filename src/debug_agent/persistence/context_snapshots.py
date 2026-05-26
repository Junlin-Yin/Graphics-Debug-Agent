from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.runtime.contracts import utc_now_iso


SNAPSHOT_INLINE_THRESHOLD_BYTES = 16 * 1024


@dataclass(frozen=True)
class ContextSnapshot:
    context_snapshot_id: str
    session_id: str
    run_id: str
    trigger: str
    source_checkpoint_id: str | None
    active_skill_records: list[dict]
    summary: str
    retained_messages: list[dict]
    omitted_tool_result_count: int
    evicted_message_count: int
    evicted_model_call_group_count: int
    artifact_refs: list[str]
    token_estimate: dict
    payload_artifact_id: str | None
    created_at: str
    version: int = 1


@dataclass(frozen=True)
class ContextSnapshotStore:
    connection: sqlite3.Connection
    artifact_store: ArtifactStore

    def save_omission_snapshot(
        self,
        *,
        session_id: str,
        run_id: str,
        source_checkpoint_id: str | None,
        active_skill_records: list[dict],
        retained_messages: list[dict],
        omitted_tool_result_count: int,
        artifact_refs: list[str],
        token_estimate: dict,
    ) -> ContextSnapshot:
        context_snapshot_id = f"ctx_{uuid4().hex}"
        created_at = utc_now_iso()
        payload = {
            "context_snapshot_id": context_snapshot_id,
            "session_id": session_id,
            "run_id": run_id,
            "trigger": "omission",
            "source_checkpoint_id": source_checkpoint_id,
            "active_skill_records": active_skill_records,
            "summary": "",
            "retained_messages": retained_messages,
            "omitted_tool_result_count": omitted_tool_result_count,
            "evicted_message_count": 0,
            "evicted_model_call_group_count": 0,
            "artifact_refs": artifact_refs,
            "token_estimate": token_estimate,
            "created_at": created_at,
            "version": 1,
        }
        payload_artifact_id = self._maybe_artifact_payload(
            session_id=session_id,
            run_id=run_id,
            context_snapshot_id=context_snapshot_id,
            payload=payload,
        )
        retained_messages_json = json.dumps(retained_messages, sort_keys=True)
        if payload_artifact_id is not None:
            retained_messages_json = "[]"
        snapshot = ContextSnapshot(
            context_snapshot_id=context_snapshot_id,
            session_id=session_id,
            run_id=run_id,
            trigger="omission",
            source_checkpoint_id=source_checkpoint_id,
            active_skill_records=[dict(record) for record in active_skill_records],
            summary="",
            retained_messages=[dict(message) for message in retained_messages],
            omitted_tool_result_count=omitted_tool_result_count,
            evicted_message_count=0,
            evicted_model_call_group_count=0,
            artifact_refs=list(artifact_refs),
            token_estimate=dict(token_estimate),
            payload_artifact_id=payload_artifact_id,
            created_at=created_at,
        )
        self.connection.execute(
            """
            INSERT INTO context_snapshots (
                context_snapshot_id, session_id, run_id, trigger,
                source_checkpoint_id, active_skill_records_json, summary,
                retained_messages_json, omitted_tool_result_count,
                evicted_message_count, evicted_model_call_group_count,
                artifact_refs_json, token_estimate_json, payload_artifact_id,
                created_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.context_snapshot_id,
                snapshot.session_id,
                snapshot.run_id,
                snapshot.trigger,
                snapshot.source_checkpoint_id,
                json.dumps(snapshot.active_skill_records, sort_keys=True),
                snapshot.summary,
                retained_messages_json,
                snapshot.omitted_tool_result_count,
                snapshot.evicted_message_count,
                snapshot.evicted_model_call_group_count,
                json.dumps(snapshot.artifact_refs, sort_keys=True),
                json.dumps(snapshot.token_estimate, sort_keys=True),
                snapshot.payload_artifact_id,
                snapshot.created_at,
                snapshot.version,
            ),
        )
        self.connection.commit()
        return snapshot

    def save_compression_snapshot(
        self,
        *,
        session_id: str,
        run_id: str,
        trigger: str,
        source_checkpoint_id: str | None,
        active_skill_records: list[dict],
        summary: str,
        retained_messages: list[dict],
        omitted_tool_result_count: int,
        evicted_message_count: int,
        evicted_model_call_group_count: int,
        artifact_refs: list[str],
        token_estimate: dict,
    ) -> ContextSnapshot:
        if trigger not in {"manual", "compression", "omission | compression"}:
            raise ValueError("compression snapshot trigger must be compression-related")
        context_snapshot_id = f"ctx_{uuid4().hex}"
        created_at = utc_now_iso()
        payload = {
            "context_snapshot_id": context_snapshot_id,
            "session_id": session_id,
            "run_id": run_id,
            "trigger": trigger,
            "source_checkpoint_id": source_checkpoint_id,
            "active_skill_records": active_skill_records,
            "summary": summary,
            "retained_messages": retained_messages,
            "omitted_tool_result_count": omitted_tool_result_count,
            "evicted_message_count": evicted_message_count,
            "evicted_model_call_group_count": evicted_model_call_group_count,
            "artifact_refs": artifact_refs,
            "token_estimate": token_estimate,
            "created_at": created_at,
            "version": 1,
        }
        payload_artifact_id = self._maybe_artifact_payload(
            session_id=session_id,
            run_id=run_id,
            context_snapshot_id=context_snapshot_id,
            payload=payload,
        )
        retained_messages_json = json.dumps(retained_messages, sort_keys=True)
        if payload_artifact_id is not None:
            retained_messages_json = "[]"
        snapshot = ContextSnapshot(
            context_snapshot_id=context_snapshot_id,
            session_id=session_id,
            run_id=run_id,
            trigger=trigger,
            source_checkpoint_id=source_checkpoint_id,
            active_skill_records=[dict(record) for record in active_skill_records],
            summary=summary,
            retained_messages=[dict(message) for message in retained_messages],
            omitted_tool_result_count=omitted_tool_result_count,
            evicted_message_count=evicted_message_count,
            evicted_model_call_group_count=evicted_model_call_group_count,
            artifact_refs=list(artifact_refs),
            token_estimate=dict(token_estimate),
            payload_artifact_id=payload_artifact_id,
            created_at=created_at,
        )
        self.connection.execute(
            """
            INSERT INTO context_snapshots (
                context_snapshot_id, session_id, run_id, trigger,
                source_checkpoint_id, active_skill_records_json, summary,
                retained_messages_json, omitted_tool_result_count,
                evicted_message_count, evicted_model_call_group_count,
                artifact_refs_json, token_estimate_json, payload_artifact_id,
                created_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.context_snapshot_id,
                snapshot.session_id,
                snapshot.run_id,
                snapshot.trigger,
                snapshot.source_checkpoint_id,
                json.dumps(snapshot.active_skill_records, sort_keys=True),
                snapshot.summary,
                retained_messages_json,
                snapshot.omitted_tool_result_count,
                snapshot.evicted_message_count,
                snapshot.evicted_model_call_group_count,
                json.dumps(snapshot.artifact_refs, sort_keys=True),
                json.dumps(snapshot.token_estimate, sort_keys=True),
                snapshot.payload_artifact_id,
                snapshot.created_at,
                snapshot.version,
            ),
        )
        self.connection.commit()
        return snapshot

    def _maybe_artifact_payload(
        self,
        *,
        session_id: str,
        run_id: str,
        context_snapshot_id: str,
        payload: dict,
    ) -> str | None:
        serialized = json.dumps(payload, sort_keys=True)
        if len(serialized.encode("utf-8")) <= SNAPSHOT_INLINE_THRESHOLD_BYTES:
            return None
        artifact = self.artifact_store.write_text(
            session_id=session_id,
            run_id=run_id,
            filename=f"{context_snapshot_id}.json",
            content=serialized,
            metadata={
                "artifact_role": "context_snapshot_payload",
                "context_snapshot_id": context_snapshot_id,
            },
        )
        return artifact.artifact_id

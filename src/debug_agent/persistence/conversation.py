from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.errors import StoreError
from debug_agent.runtime.contracts import utc_now_iso


ALLOWED_ROLES = frozenset({"user", "assistant", "tool", "runtime"})
ALLOWED_KINDS = frozenset(
    {
        "user_input",
        "assistant_output",
        "assistant_tool_call",
        "tool_result",
        "failure_fact",
        "cancellation_fact",
        "context_summary",
    }
)


def canonical_json_bytes(value: Any) -> bytes:
    _validate_canonical_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ConversationAppend:
    turn_id: str | None
    message_group_id: str
    model_call_id: str | None
    group_position: int
    group_row_count: int
    role: str
    kind: str
    content: Any | None = None
    artifact_id: str | None = None
    metadata: dict[str, Any] | None = None
    source_event_id: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ConversationMessageRow:
    id: int
    session_id: str
    run_id: str
    turn_id: str | None
    message_index: int
    message_group_id: str
    model_call_id: str | None
    group_position: int
    group_status: str
    group_row_count: int
    role: str
    kind: str
    content: Any | None
    artifact_id: str | None
    content_sha256: str
    metadata: dict[str, Any]
    tool_call_id: str | None
    source_event_id: str | None
    accepted_at: str
    version: int = 1


@dataclass(frozen=True)
class ConversationProjectionState:
    projection_state_id: str
    session_id: str
    run_id: str
    source_high_watermark: int
    message_refs: list[dict[str, int]]
    projection_sha256: str
    updated_at: str
    update_reason: str
    source_event_id: str | None
    version: int = 1


@dataclass(frozen=True)
class ConversationFactCut:
    run_id: str
    highest_message_index: int
    message_count: int
    checksum: str


@dataclass(frozen=True)
class ConversationStore:
    connection: sqlite3.Connection
    artifact_store: ArtifactStore | None = None

    def append_closed_group(
        self,
        *,
        session_id: str,
        run_id: str,
        messages: list[ConversationAppend],
        update_reason: str = "message_append",
    ) -> list[ConversationMessageRow]:
        if not messages:
            raise _store_error("Conversation append requires at least one message.")
        if update_reason not in {"message_append", "omission", "compression"}:
            raise _store_error("Invalid projection update reason.")
        prepared = [self._prepare_append(message) for message in messages]
        self._validate_closed_group(prepared)

        try:
            with self.connection:
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(message_index), 0) FROM conversation_messages WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                next_index = int(row[0]) + 1
                accepted_at = utc_now_iso()
                inserted: list[ConversationMessageRow] = []
                for offset, item in enumerate(prepared):
                    message_index = next_index + offset
                    self.connection.execute(
                        """
                        INSERT INTO conversation_messages (
                            session_id, run_id, turn_id, message_index,
                            message_group_id, model_call_id, group_position,
                            group_status, group_row_count, role, kind,
                            content_json, artifact_id, content_sha256,
                            metadata_json, tool_call_id, source_event_id,
                            accepted_at, version
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            session_id,
                            run_id,
                            item["append"].turn_id,
                            message_index,
                            item["append"].message_group_id,
                            item["append"].model_call_id,
                            item["append"].group_position,
                            item["append"].group_row_count,
                            item["append"].role,
                            item["append"].kind,
                            item["content_json"],
                            item["append"].artifact_id,
                            item["content_sha256"],
                            item["metadata_json"],
                            item["append"].tool_call_id,
                            item["append"].source_event_id,
                            accepted_at,
                        ),
                    )
                    inserted.append(
                        self._row_by_index(run_id=run_id, message_index=message_index)
                    )
                projection = self._projection_for_append_locked(
                    session_id=session_id,
                    run_id=run_id,
                    source_high_watermark=inserted[-1].message_index,
                    update_reason=update_reason,
                    source_event_id=inserted[-1].source_event_id,
                )
                self._upsert_projection_locked(projection)
        except sqlite3.DatabaseError as exc:
            raise _store_error(f"Conversation append failed: {exc}") from exc
        return inserted

    def list_messages(self, run_id: str) -> list[ConversationMessageRow]:
        rows = self.connection.execute(
            """
            SELECT id, session_id, run_id, turn_id, message_index,
                   message_group_id, model_call_id, group_position, group_status,
                   group_row_count, role, kind, content_json, artifact_id,
                   content_sha256, metadata_json, tool_call_id, source_event_id,
                   accepted_at, version
            FROM conversation_messages
            WHERE run_id = ?
            ORDER BY message_index ASC
            """,
            (run_id,),
        ).fetchall()
        return [_message_from_row(row) for row in rows]

    def get_projection(self, run_id: str) -> ConversationProjectionState:
        row = self.connection.execute(
            """
            SELECT projection_state_id, session_id, run_id, source_high_watermark,
                   message_refs_json, projection_sha256, updated_at, update_reason,
                   source_event_id, version
            FROM conversation_projection_state
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return self._empty_projection_state(run_id=run_id)
        return _projection_from_row(row)

    def validate_fact_cut(
        self, *, run_id: str, highest_message_index: int
    ) -> ConversationFactCut:
        if highest_message_index < 0:
            raise _store_error("Conversation fact cut high-watermark cannot be negative.")
        rows = [
            row
            for row in self.list_messages(run_id)
            if row.message_index <= highest_message_index
        ]
        if highest_message_index == 0:
            if rows:
                raise _store_error("Empty conversation cut cannot include rows.")
            return ConversationFactCut(
                run_id=run_id,
                highest_message_index=0,
                message_count=0,
                checksum=self._fact_cut_checksum(run_id=run_id, rows=[]),
            )
        if len(rows) != highest_message_index:
            raise _store_error("Conversation fact cut is missing message rows.")
        if [row.message_index for row in rows] != list(range(1, highest_message_index + 1)):
            raise _store_error("Conversation message indexes are not contiguous.")
        self._validate_rows(rows)
        return ConversationFactCut(
            run_id=run_id,
            highest_message_index=highest_message_index,
            message_count=len(rows),
            checksum=self._fact_cut_checksum(run_id=run_id, rows=rows),
        )

    def validate_projection_snapshot(
        self,
        *,
        run_id: str,
        source_high_watermark: int,
        message_refs: list[dict[str, int]],
        checksum: str,
    ) -> None:
        rows = self._rows_for_refs(
            run_id=run_id,
            source_high_watermark=source_high_watermark,
            message_refs=message_refs,
        )
        for row in rows:
            self._validate_content_checksum(row)
        expected = self._projection_checksum(
            run_id=run_id,
            source_high_watermark=source_high_watermark,
            message_refs=message_refs,
            rows=rows,
        )
        if checksum != expected:
            raise _store_error("Conversation projection checksum is invalid.")

    def empty_projection_snapshot(self, *, run_id: str) -> dict[str, Any]:
        return {
            "projection_state_id": None,
            "source_high_watermark": 0,
            "message_refs": [],
            "checksum": self._projection_checksum(
                run_id=run_id,
                source_high_watermark=0,
                message_refs=[],
                rows=[],
            ),
        }

    def overwrite_projection(
        self,
        *,
        session_id: str,
        run_id: str,
        message_refs: list[dict[str, int]],
        update_reason: str,
        source_event_id: str | None = None,
    ) -> ConversationProjectionState:
        if update_reason not in {"message_append", "omission", "compression"}:
            raise _store_error("Invalid projection update reason.")
        rows = self._rows_for_refs(
            run_id=run_id,
            source_high_watermark=self._highest_message_index(run_id),
            message_refs=message_refs,
        )
        source_high_watermark = self._highest_message_index(run_id)
        projection = ConversationProjectionState(
            projection_state_id=self._current_projection_id(run_id),
            session_id=session_id,
            run_id=run_id,
            source_high_watermark=source_high_watermark,
            message_refs=message_refs,
            projection_sha256=self._projection_checksum(
                run_id=run_id,
                source_high_watermark=source_high_watermark,
                message_refs=message_refs,
                rows=rows,
            ),
            updated_at=utc_now_iso(),
            update_reason=update_reason,
            source_event_id=source_event_id,
        )
        try:
            with self.connection:
                self._upsert_projection_locked(projection)
        except sqlite3.DatabaseError as exc:
            raise _store_error(f"Conversation projection update failed: {exc}") from exc
        return projection

    def validate_runtime_projection_alignment(
        self,
        *,
        run_id: str,
        process_message_indexes: list[int],
        explicit_resume: bool,
    ) -> None:
        if explicit_resume:
            return
        projection = self.get_projection(run_id)
        expected = _indexes_from_refs(projection.message_refs)
        if process_message_indexes != expected:
            raise _store_error(
                "Process-local conversation drifted from durable projection state."
            )

    def _prepare_append(self, message: ConversationAppend) -> dict[str, Any]:
        if message.role not in ALLOWED_ROLES:
            raise _store_error(f"Unsupported conversation role: {message.role}")
        if message.kind not in ALLOWED_KINDS:
            raise _store_error(f"Unsupported conversation kind: {message.kind}")
        if not message.message_group_id:
            raise _store_error("Conversation message_group_id is required.")
        if message.group_position < 0:
            raise _store_error("Conversation group_position must be 0-based.")
        if message.group_row_count < 1:
            raise _store_error("Conversation group_row_count must be positive.")
        metadata = {} if message.metadata is None else dict(message.metadata)
        if metadata.get("group_status") == "open":
            raise _store_error("Accepted durable conversation groups must be closed.")
        has_inline = message.content is not None
        has_artifact = message.artifact_id is not None
        if has_inline == has_artifact:
            raise _store_error("Conversation rows require exactly one content source.")
        metadata_json = canonical_json_bytes(metadata).decode("utf-8")
        if has_inline:
            content_json = canonical_json_bytes(message.content).decode("utf-8")
            content_sha256 = sha256_hex(canonical_json_bytes(message.content))
        else:
            content_json = None
            content_sha256 = self._artifact_checksum(message.artifact_id or "")
        return {
            "append": message,
            "content_json": content_json,
            "metadata_json": metadata_json,
            "content_sha256": content_sha256,
        }

    def _validate_closed_group(self, prepared: list[dict[str, Any]]) -> None:
        group_ids = {item["append"].message_group_id for item in prepared}
        if len(group_ids) != 1:
            raise _store_error("append_closed_group accepts one message group at a time.")
        row_counts = {item["append"].group_row_count for item in prepared}
        if row_counts != {len(prepared)}:
            raise _store_error("Closed group row count does not match appended rows.")
        positions = sorted(item["append"].group_position for item in prepared)
        if positions != list(range(len(prepared))):
            raise _store_error("Closed group positions must be contiguous from 0.")

    def _validate_rows(self, rows: list[ConversationMessageRow]) -> None:
        groups: dict[str, list[ConversationMessageRow]] = {}
        for row in rows:
            if row.group_status != "closed":
                raise _store_error("Accepted conversation fact cut includes an open group.")
            if row.role not in ALLOWED_ROLES or row.kind not in ALLOWED_KINDS:
                raise _store_error("Conversation row uses unsupported role or kind.")
            self._validate_content_source(row)
            self._validate_content_checksum(row)
            groups.setdefault(row.message_group_id, []).append(row)
        for group_rows in groups.values():
            expected = group_rows[0].group_row_count
            positions = sorted(row.group_position for row in group_rows)
            if len(group_rows) != expected:
                raise _store_error("Conversation fact cut truncates a message group.")
            if positions != list(range(expected)):
                raise _store_error("Conversation message group positions are invalid.")
            if {row.group_row_count for row in group_rows} != {expected}:
                raise _store_error("Conversation message group row counts disagree.")
        self._validate_tool_pairing(rows)

    def _validate_tool_pairing(self, rows: list[ConversationMessageRow]) -> None:
        calls = {
            (row.model_call_id, row.tool_call_id)
            for row in rows
            if row.kind == "assistant_tool_call" and row.tool_call_id
        }
        results = {
            (row.model_call_id, row.tool_call_id)
            for row in rows
            if row.kind == "tool_result" and row.tool_call_id
        }
        if calls - results:
            raise _store_error("Accepted assistant tool call is missing a tool result.")
        if results - calls:
            raise _store_error("Accepted tool result is missing an assistant tool call.")

    def _validate_content_source(self, row: ConversationMessageRow) -> None:
        if (row.content is None) == (row.artifact_id is None):
            raise _store_error("Conversation row has invalid canonical content source.")

    def _validate_content_checksum(self, row: ConversationMessageRow) -> None:
        if row.artifact_id is not None:
            expected = self._artifact_checksum(row.artifact_id)
        else:
            expected = sha256_hex(canonical_json_bytes(row.content))
        if row.content_sha256 != expected:
            raise _store_error("Conversation content checksum is invalid.")

    def _artifact_checksum(self, artifact_id: str) -> str:
        if self.artifact_store is None:
            raise _store_error("Artifact validation requires an ArtifactStore.")
        try:
            artifact = self.artifact_store.get(artifact_id)
        except Exception as exc:
            raise _store_error(f"Conversation artifact reference is invalid: {artifact_id}") from exc
        payload_sha256 = artifact.metadata.get("payload_sha256")
        if not isinstance(payload_sha256, str) or not payload_sha256.startswith("sha256:"):
            raise _store_error(
                f"Conversation artifact reference is missing payload checksum: {artifact_id}"
            )
        return payload_sha256

    def _fact_cut_checksum(
        self, *, run_id: str, rows: list[ConversationMessageRow]
    ) -> str:
        payload_rows = []
        for row in rows:
            payload_rows.append(
                {
                    "session_id": row.session_id,
                    "run_id": row.run_id,
                    "message_index": row.message_index,
                    "message_group_id": row.message_group_id,
                    "model_call_id": row.model_call_id,
                    "group_position": row.group_position,
                    "group_status": row.group_status,
                    "role": row.role,
                    "kind": row.kind,
                    "content_sha256": row.content_sha256,
                    "artifact_id": row.artifact_id,
                    "metadata_json": row.metadata,
                    "source_event_id": row.source_event_id,
                }
            )
        return sha256_hex(canonical_json_bytes({"run_id": run_id, "rows": payload_rows}))

    def _projection_for_append_locked(
        self,
        *,
        session_id: str,
        run_id: str,
        source_high_watermark: int,
        update_reason: str,
        source_event_id: str | None,
    ) -> ConversationProjectionState:
        previous_projection = self.get_projection(run_id)
        previous_refs = list(previous_projection.message_refs)
        previous_indexes = set(_indexes_from_refs(previous_refs))
        rows = [row for row in self.list_messages(run_id) if row.message_index <= source_high_watermark]
        inserted_refs = [
            {"index": row.message_index}
            for row in rows
            if row.message_index not in previous_indexes
        ]
        refs = [*previous_refs, *inserted_refs]
        projected_rows = self._rows_for_refs(
            run_id=run_id,
            source_high_watermark=source_high_watermark,
            message_refs=refs,
        )
        checksum = self._projection_checksum(
            run_id=run_id,
            source_high_watermark=source_high_watermark,
            message_refs=refs,
            rows=projected_rows,
        )
        return ConversationProjectionState(
            projection_state_id=self._current_projection_id(run_id),
            session_id=session_id,
            run_id=run_id,
            source_high_watermark=source_high_watermark,
            message_refs=refs,
            projection_sha256=checksum,
            updated_at=utc_now_iso(),
            update_reason=update_reason,
            source_event_id=source_event_id,
        )

    def _current_projection_id(self, run_id: str) -> str:
        current = self.connection.execute(
            "SELECT projection_state_id FROM conversation_projection_state WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return str(current[0]) if current is not None else f"projection_{uuid4().hex}"

    def _highest_message_index(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(message_index), 0) FROM conversation_messages WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row[0])

    def _upsert_projection_locked(self, projection: ConversationProjectionState) -> None:
        self.connection.execute(
            """
            INSERT INTO conversation_projection_state (
                projection_state_id, session_id, run_id, source_high_watermark,
                message_refs_json, projection_sha256, updated_at, update_reason,
                source_event_id, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                source_high_watermark = excluded.source_high_watermark,
                message_refs_json = excluded.message_refs_json,
                projection_sha256 = excluded.projection_sha256,
                updated_at = excluded.updated_at,
                update_reason = excluded.update_reason,
                source_event_id = excluded.source_event_id,
                version = excluded.version
            """,
            (
                projection.projection_state_id,
                projection.session_id,
                projection.run_id,
                projection.source_high_watermark,
                canonical_json_bytes(projection.message_refs).decode("utf-8"),
                projection.projection_sha256,
                projection.updated_at,
                projection.update_reason,
                projection.source_event_id,
                projection.version,
            ),
        )

    def _projection_checksum(
        self,
        *,
        run_id: str,
        source_high_watermark: int,
        message_refs: list[dict[str, int]],
        rows: list[ConversationMessageRow],
    ) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "run_id": run_id,
                    "source_high_watermark": source_high_watermark,
                    "message_refs": message_refs,
                    "content_sha256": [row.content_sha256 for row in rows],
                }
            )
        )

    def _rows_for_refs(
        self,
        *,
        run_id: str,
        source_high_watermark: int,
        message_refs: list[dict[str, int]],
    ) -> list[ConversationMessageRow]:
        rows_by_index = {row.message_index: row for row in self.list_messages(run_id)}
        selected: list[ConversationMessageRow] = []
        for ref in message_refs:
            if "index" in ref:
                indexes = [int(ref["index"])]
            elif "start" in ref and "end" in ref:
                indexes = list(range(int(ref["start"]), int(ref["end"]) + 1))
            else:
                raise _store_error("Projection message ref is invalid.")
            for index in indexes:
                if index < 1 or index > source_high_watermark:
                    raise _store_error("Projection message ref is outside the fact cut.")
                row = rows_by_index.get(index)
                if row is None:
                    raise _store_error("Projection references a missing message row.")
                selected.append(row)
        return selected

    def _row_by_index(self, *, run_id: str, message_index: int) -> ConversationMessageRow:
        row = self.connection.execute(
            """
            SELECT id, session_id, run_id, turn_id, message_index,
                   message_group_id, model_call_id, group_position, group_status,
                   group_row_count, role, kind, content_json, artifact_id,
                   content_sha256, metadata_json, tool_call_id, source_event_id,
                   accepted_at, version
            FROM conversation_messages
            WHERE run_id = ? AND message_index = ?
            """,
            (run_id, message_index),
        ).fetchone()
        if row is None:
            raise _store_error("Conversation row insert could not be read back.")
        return _message_from_row(row)

    def _empty_projection_state(self, *, run_id: str) -> ConversationProjectionState:
        row = self.connection.execute(
            "SELECT session_id FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        session_id = str(row[0]) if row is not None else ""
        return ConversationProjectionState(
            projection_state_id="",
            session_id=session_id,
            run_id=run_id,
            source_high_watermark=0,
            message_refs=[],
            projection_sha256=self._projection_checksum(
                run_id=run_id,
                source_high_watermark=0,
                message_refs=[],
                rows=[],
            ),
            updated_at="",
            update_reason="message_append",
            source_event_id=None,
        )


def _message_from_row(row: tuple[Any, ...]) -> ConversationMessageRow:
    content = None if row[12] is None else json.loads(row[12])
    return ConversationMessageRow(
        id=int(row[0]),
        session_id=row[1],
        run_id=row[2],
        turn_id=row[3],
        message_index=int(row[4]),
        message_group_id=row[5],
        model_call_id=row[6],
        group_position=int(row[7]),
        group_status=row[8],
        group_row_count=int(row[9]),
        role=row[10],
        kind=row[11],
        content=content,
        artifact_id=row[13],
        content_sha256=row[14],
        metadata=json.loads(row[15]),
        tool_call_id=row[16],
        source_event_id=row[17],
        accepted_at=row[18],
        version=int(row[19]),
    )


def _projection_from_row(row: tuple[Any, ...]) -> ConversationProjectionState:
    return ConversationProjectionState(
        projection_state_id=row[0],
        session_id=row[1],
        run_id=row[2],
        source_high_watermark=int(row[3]),
        message_refs=json.loads(row[4]),
        projection_sha256=row[5],
        updated_at=row[6],
        update_reason=row[7],
        source_event_id=row[8],
        version=int(row[9]),
    )


def _indexes_from_refs(message_refs: list[dict[str, int]]) -> list[int]:
    indexes: list[int] = []
    for ref in message_refs:
        if "index" in ref:
            indexes.append(int(ref["index"]))
            continue
        if "start" in ref and "end" in ref:
            indexes.extend(range(int(ref["start"]), int(ref["end"]) + 1))
            continue
        raise _store_error("Projection message ref is invalid.")
    return indexes


def _validate_canonical_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _store_error("Canonical JSON does not support NaN or Infinity.")
        raise _store_error("Canonical JSON does not support floating point values.")
    if isinstance(value, list):
        for item in value:
            _validate_canonical_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _store_error("Canonical JSON object keys must be strings.")
            _validate_canonical_json_value(item)
        return
    raise _store_error(f"Unsupported canonical JSON value: {type(value).__name__}")


def _store_error(message: str) -> StoreError:
    return StoreError(
        error_class="persistence_error",
        message=message,
        recoverable=False,
    )

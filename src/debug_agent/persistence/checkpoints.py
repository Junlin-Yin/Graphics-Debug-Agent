from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.conversation import (
    ConversationStore,
    canonical_json_bytes,
    sha256_hex,
)
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.settings import (
    TERMINAL_REASONS,
    TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION,
    ZERO_MESSAGE_REASONS,
)
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.contracts import Checkpoint


@dataclass(frozen=True)
class CheckpointStore:
    connection: sqlite3.Connection
    conversation_store: ConversationStore | None = None
    todo_plan_store: TodoPlanStore | None = None
    approval_grant_store: ApprovalGrantStore | None = None
    artifact_store: ArtifactStore | None = None

    def save(self, checkpoint: Checkpoint) -> Checkpoint:
        if checkpoint.kind != "terminal_recovery":
            raise _store_error(
                "Phase 3 prompt checkpoints must be terminal_recovery checkpoints."
            )
        return self.terminalize_with_recovery_checkpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            session_id=checkpoint.session_id,
            run_id=checkpoint.run_id,
            terminal_status=str(checkpoint.state.get("terminal_status")),
            terminal_reason=str(checkpoint.state.get("terminal_reason")),
            terminal_error=checkpoint.state.get("terminal_error"),
            error_summary=_terminal_error_summary(checkpoint.state.get("terminal_error")),
            created_at=checkpoint.created_at,
            artifact_ids=(
                checkpoint.state.get("artifacts", {}).get("artifact_ids")
                if isinstance(checkpoint.state.get("artifacts"), dict)
                else []
            ),
        )

    def create_terminal_recovery(
        self,
        *,
        checkpoint_id: str,
        session_id: str,
        run_id: str,
        terminal_status: str,
        terminal_reason: str,
        terminal_error: dict[str, Any] | None,
        created_at: str,
        artifact_ids: list[str] | None = None,
    ) -> Checkpoint:
        return self.terminalize_with_recovery_checkpoint(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            run_id=run_id,
            terminal_status=terminal_status,
            terminal_reason=terminal_reason,
            terminal_error=terminal_error,
            error_summary=_terminal_error_summary(terminal_error),
            created_at=created_at,
            artifact_ids=[] if artifact_ids is None else artifact_ids,
        )

    def terminalize_with_recovery_checkpoint(
        self,
        *,
        checkpoint_id: str,
        session_id: str,
        run_id: str,
        terminal_status: str,
        terminal_reason: str,
        terminal_error: dict[str, Any] | None,
        error_summary: str | None,
        created_at: str,
        artifact_ids: list[str] | None = None,
    ) -> Checkpoint:
        checkpoint = self._new_terminal_recovery_checkpoint(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            run_id=run_id,
            terminal_status=terminal_status,
            terminal_reason=terminal_reason,
            terminal_error=terminal_error,
            created_at=created_at,
            artifact_ids=[] if artifact_ids is None else artifact_ids,
        )
        with self.connection:
            self._insert_checkpoint(checkpoint)
            self._mark_run_terminal_with_checkpoint(
                run_id=run_id,
                status=terminal_status,
                error_summary=error_summary,
                checkpoint=checkpoint,
                terminal_reason=terminal_reason,
                terminal_error=terminal_error,
            )
            self._mark_session_terminal_with_checkpoint(
                session_id=session_id,
                status=terminal_status,
                error_summary=error_summary,
                checkpoint=checkpoint,
                terminal_reason=terminal_reason,
                terminal_error=terminal_error,
            )
        return self.get(checkpoint.checkpoint_id)

    def _new_terminal_recovery_checkpoint(
        self,
        *,
        checkpoint_id: str,
        session_id: str,
        run_id: str,
        terminal_status: str,
        terminal_reason: str,
        terminal_error: dict[str, Any] | None,
        created_at: str,
        artifact_ids: list[str],
    ) -> Checkpoint:
        manifest = self._build_terminal_manifest(
            session_id=session_id,
            run_id=run_id,
            terminal_status=terminal_status,
            terminal_reason=terminal_reason,
            terminal_error=terminal_error,
            artifact_ids=artifact_ids,
        )
        manifest["payload_sha256"] = sha256_hex(canonical_json_bytes(manifest))
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            run_id=run_id,
            kind="terminal_recovery",
            state=manifest,
            summary=f"Terminal recovery checkpoint: {terminal_reason}",
            created_at=created_at,
        )
        self.validate_terminal_recovery(checkpoint)
        return checkpoint

    def validate_terminal_recovery(
        self, checkpoint: Checkpoint, *, validate_current_todo: bool = True
    ) -> None:
        if checkpoint.kind != "terminal_recovery":
            raise _store_error("Checkpoint kind is not terminal_recovery.")
        payload = checkpoint.state
        if payload.get("manifest_schema_version") != TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION:
            raise _store_error("Unsupported terminal recovery manifest schema version.")
        if payload.get("checkpoint_kind") != "terminal_recovery":
            raise _store_error("Terminal recovery payload kind is invalid.")
        if payload.get("session_id") != checkpoint.session_id:
            raise _store_error("Terminal recovery checkpoint session identity mismatch.")
        if payload.get("run_id") != checkpoint.run_id:
            raise _store_error("Terminal recovery checkpoint run identity mismatch.")
        if payload.get("run_type") != "prompt":
            raise _store_error("Terminal recovery checkpoint is not for a prompt run.")
        self._validate_payload_checksum(payload)
        self._validate_terminal_matrix(payload)
        self._validate_conversation(payload, checkpoint.run_id)
        self._validate_todo(
            payload,
            checkpoint.run_id,
            validate_current=validate_current_todo,
        )
        self._validate_approval(payload, checkpoint.session_id)
        self._validate_active_skills(payload, checkpoint.session_id, checkpoint.run_id)
        self._validate_frozen_refs(payload, checkpoint.session_id)
        self._validate_artifacts(payload, checkpoint.session_id)

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
            WHERE run_id = ? AND kind = 'terminal_recovery'
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

    def _build_terminal_manifest(
        self,
        *,
        session_id: str,
        run_id: str,
        terminal_status: str,
        terminal_reason: str,
        terminal_error: dict[str, Any] | None,
        artifact_ids: list[str],
    ) -> dict[str, Any]:
        session_row = self.connection.execute(
            "SELECT approval_mode, config_snapshot_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        run_row = self.connection.execute(
            "SELECT run_type, active_skills_json FROM runs WHERE run_id = ? AND session_id = ?",
            (run_id, session_id),
        ).fetchone()
        if session_row is None or run_row is None:
            raise _store_error("Terminal recovery checkpoint references missing session/run.")
        if run_row[0] != "prompt":
            raise _store_error("Terminal recovery checkpoint requires a prompt run.")
        conversation_store = self._conversation_store()
        highest = _highest_message_index(self.connection, run_id)
        fact_cut = conversation_store.validate_fact_cut(
            run_id=run_id,
            highest_message_index=highest,
        )
        projection = conversation_store.get_projection(run_id)
        projection_snapshot = {
            "projection_state_id": projection.projection_state_id or None,
            "source_high_watermark": projection.source_high_watermark,
            "message_refs": projection.message_refs,
            "checksum": projection.projection_sha256,
        }
        if highest == 0:
            projection_snapshot = conversation_store.empty_projection_snapshot(run_id=run_id)
        manifest = {
            "manifest_schema_version": TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION,
            "checkpoint_kind": "terminal_recovery",
            "session_id": session_id,
            "run_id": run_id,
            "run_type": run_row[0],
            "terminal_status": terminal_status,
            "terminal_reason": terminal_reason,
            "terminal_error": terminal_error,
            "conversation": {
                "fact_cut": {
                    "run_id": fact_cut.run_id,
                    "highest_message_index": fact_cut.highest_message_index,
                    "message_count": fact_cut.message_count,
                    "checksum": fact_cut.checksum,
                },
                "projection_snapshot": projection_snapshot,
            },
            "todo_plan": self._todo_plan_store().checkpoint_snapshot(run_id),
            "approval_state": self._approval_cut(session_id, session_row[0]),
            "active_skills": {
                "records": json.loads(run_row[1]),
                "checksum": sha256_hex(canonical_json_bytes(json.loads(run_row[1]))),
            },
            "frozen_snapshots": self._frozen_snapshots(
                session_id=session_id,
                config_snapshot=json.loads(session_row[1]),
            ),
            "tool_availability": self._tool_availability(json.loads(session_row[1])),
            "artifacts": {"artifact_ids": artifact_ids},
        }
        self._validate_zero_message_allowed(manifest)
        self._validate_terminal_matrix(manifest)
        return manifest

    def _insert_checkpoint(self, checkpoint: Checkpoint) -> None:
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

    def _set_latest_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.connection.execute(
            """
            UPDATE sessions
            SET latest_checkpoint_id = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (checkpoint.checkpoint_id, checkpoint.created_at, checkpoint.session_id),
        )
        self.connection.execute(
            """
            UPDATE runs
            SET latest_checkpoint_id = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (checkpoint.checkpoint_id, checkpoint.created_at, checkpoint.run_id),
        )

    def _mark_run_terminal_with_checkpoint(
        self,
        *,
        run_id: str,
        status: str,
        error_summary: str | None,
        checkpoint: Checkpoint,
        terminal_reason: str,
        terminal_error: dict[str, Any] | None,
    ) -> None:
        result = self.connection.execute(
            """
            UPDATE runs
            SET status = ?, latest_checkpoint_id = ?, error_summary = ?,
                terminal_reason = ?, terminal_error_json = ?,
                non_resumable_startup_failure = 0, updated_at = ?
            WHERE run_id = ? AND status = 'running'
            """,
            (
                status,
                checkpoint.checkpoint_id,
                error_summary,
                terminal_reason,
                None
                if terminal_error is None
                else json.dumps(terminal_error, ensure_ascii=False, sort_keys=True),
                checkpoint.created_at,
                run_id,
            ),
        )
        if result.rowcount != 1:
            raise _store_error("Terminal recovery run transition failed.")

    def _mark_session_terminal_with_checkpoint(
        self,
        *,
        session_id: str,
        status: str,
        error_summary: str | None,
        checkpoint: Checkpoint,
        terminal_reason: str,
        terminal_error: dict[str, Any] | None,
    ) -> None:
        result = self.connection.execute(
            """
            UPDATE sessions
            SET status = ?, active_run_id = NULL, latest_checkpoint_id = ?,
                error_summary = ?, terminal_reason = ?, terminal_error_json = ?,
                non_resumable_startup_failure = 0, updated_at = ?
            WHERE session_id = ? AND status = 'running'
            """,
            (
                status,
                checkpoint.checkpoint_id,
                error_summary,
                terminal_reason,
                None
                if terminal_error is None
                else json.dumps(terminal_error, ensure_ascii=False, sort_keys=True),
                checkpoint.created_at,
                session_id,
            ),
        )
        if result.rowcount != 1:
            raise _store_error("Terminal recovery session transition failed.")

    def _validate_payload_checksum(self, payload: dict[str, Any]) -> None:
        checksum = payload.get("payload_sha256")
        if not isinstance(checksum, str):
            raise _store_error("Terminal recovery payload checksum is missing.")
        comparable = dict(payload)
        comparable.pop("payload_sha256", None)
        expected = sha256_hex(canonical_json_bytes(comparable))
        if checksum != expected:
            raise _store_error("Terminal recovery payload checksum is invalid.")

    def _validate_terminal_matrix(self, payload: dict[str, Any]) -> None:
        reason = payload.get("terminal_reason")
        status = payload.get("terminal_status")
        error = payload.get("terminal_error")
        if reason not in TERMINAL_REASONS:
            raise _store_error("Unsupported terminal recovery terminal reason.")
        if reason in {"terminal_completion", "user_exit"}:
            if status != "completed" or error is not None:
                raise _store_error("Terminal reason/status/error matrix is invalid.")
        elif reason == "user_cancel_idle":
            if status != "failed" or not _is_error(error, "cancelled", "user_cancel_idle"):
                raise _store_error("Terminal reason/status/error matrix is invalid.")
        elif reason == "terminal_failure":
            if status != "failed" or not isinstance(error, dict):
                raise _store_error("Terminal reason/status/error matrix is invalid.")
        elif reason == "terminal_stale" and (status != "failed" or error is not None):
            raise _store_error("Terminal reason/status/error matrix is invalid.")

    def _validate_conversation(self, payload: dict[str, Any], run_id: str) -> None:
        conversation = payload.get("conversation")
        if not isinstance(conversation, dict):
            raise _store_error("Terminal recovery conversation payload is missing.")
        fact_cut = conversation.get("fact_cut")
        projection = conversation.get("projection_snapshot")
        if not isinstance(fact_cut, dict) or not isinstance(projection, dict):
            raise _store_error("Terminal recovery conversation cut is invalid.")
        validated = self._conversation_store().validate_fact_cut(
            run_id=run_id,
            highest_message_index=int(fact_cut.get("highest_message_index", -1)),
        )
        if fact_cut != {
            "run_id": validated.run_id,
            "highest_message_index": validated.highest_message_index,
            "message_count": validated.message_count,
            "checksum": validated.checksum,
        }:
            raise _store_error("Terminal recovery conversation fact cut is invalid.")
        self._conversation_store().validate_projection_snapshot(
            run_id=run_id,
            source_high_watermark=int(projection.get("source_high_watermark", -1)),
            message_refs=projection.get("message_refs"),
            checksum=projection.get("checksum"),
        )
        self._validate_zero_message_allowed(payload)

    def _validate_zero_message_allowed(self, payload: dict[str, Any]) -> None:
        fact_cut = payload["conversation"]["fact_cut"]
        if fact_cut["highest_message_index"] == 0 and (
            payload.get("terminal_reason") not in ZERO_MESSAGE_REASONS
        ):
            raise _store_error("Terminal recovery zero-message checkpoint is not allowed.")

    def _validate_todo(
        self, payload: dict[str, Any], run_id: str, *, validate_current: bool
    ) -> None:
        try:
            if validate_current:
                self._todo_plan_store().validate_checkpoint_snapshot(
                    run_id, payload.get("todo_plan")
                )
            else:
                self._todo_plan_store().validate_checkpoint_snapshot_payload(
                    run_id, payload.get("todo_plan")
                )
        except Exception as exc:
            raise _store_error("Terminal recovery Todo Plan snapshot is invalid.") from exc

    def _validate_approval(self, payload: dict[str, Any], session_id: str) -> None:
        approval = payload.get("approval_state")
        if not isinstance(approval, dict):
            raise _store_error("Terminal recovery approval state is missing.")
        expected = self._approval_cut(session_id, approval.get("approval_mode"))
        if approval != expected:
            raise _store_error("Terminal recovery approval grant cut is invalid.")

    def _validate_active_skills(
        self, payload: dict[str, Any], session_id: str, run_id: str
    ) -> None:
        active = payload.get("active_skills")
        if not isinstance(active, dict) or not isinstance(active.get("records"), list):
            raise _store_error("Terminal recovery active skill records are invalid.")
        row = self.connection.execute(
            "SELECT active_skills_json FROM runs WHERE session_id = ? AND run_id = ?",
            (session_id, run_id),
        ).fetchone()
        if row is None:
            raise _store_error("Terminal recovery active skill run reference is invalid.")
        records = json.loads(row[0])
        if active.get("records") != records:
            raise _store_error("Terminal recovery active skill records are invalid.")
        if active.get("checksum") != sha256_hex(canonical_json_bytes(records)):
            raise _store_error("Terminal recovery active skill checksum is invalid.")

    def _validate_frozen_refs(self, payload: dict[str, Any], session_id: str) -> None:
        row = self.connection.execute(
            "SELECT config_snapshot_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise _store_error("Terminal recovery frozen session reference is invalid.")
        config_snapshot = json.loads(row[0])
        expected = self._frozen_snapshots(
            session_id=session_id,
            config_snapshot=config_snapshot,
        )
        if payload.get("frozen_snapshots") != expected:
            raise _store_error("Terminal recovery frozen snapshot reference is invalid.")
        expected_tools = self._tool_availability(config_snapshot)
        if payload.get("tool_availability") != expected_tools:
            raise _store_error("Terminal recovery tool availability reference is invalid.")

    def _validate_artifacts(self, payload: dict[str, Any], session_id: str) -> None:
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, dict) or not isinstance(artifacts.get("artifact_ids"), list):
            raise _store_error("Terminal recovery artifact refs are invalid.")
        if self.artifact_store is None:
            if artifacts["artifact_ids"]:
                raise _store_error("Terminal recovery artifact validation requires an ArtifactStore.")
            return
        for artifact_id in artifacts["artifact_ids"]:
            artifact = self.artifact_store.get(artifact_id)
            if artifact.session_id != session_id:
                raise _store_error("Terminal recovery artifact session mismatch.")

    def _approval_cut(self, session_id: str, approval_mode: str | None) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT rowid, grant_id, session_id, run_id, tool_name, risk_level,
                   scope_signature, decision, grant_scope
            FROM approval_grants
            WHERE session_id = ?
            ORDER BY rowid ASC
            """,
            (session_id,),
        ).fetchall()
        high_watermark = int(rows[-1][0]) if rows else 0
        canonical_rows = [
            {
                "grant_sequence": int(row[0]),
                "grant_id": row[1],
                "session_id": row[2],
                "run_id": row[3],
                "tool_name": row[4],
                "risk_level": row[5],
                "scope_signature": row[6],
                "decision": row[7],
                "grant_scope": row[8],
            }
            for row in rows
        ]
        return {
            "approval_mode": approval_mode,
            "grant_high_watermark": high_watermark,
            "grant_count": len(rows),
            "grant_checksum": sha256_hex(canonical_json_bytes(canonical_rows)),
        }

    def _frozen_snapshots(
        self, *, session_id: str, config_snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        policy = config_snapshot.get("policy") if isinstance(config_snapshot, dict) else None
        config_ref = {
            "provider": config_snapshot.get("provider"),
            "model": config_snapshot.get("model"),
            "execution": config_snapshot.get("execution"),
            "multimodal": config_snapshot.get("multimodal"),
        }
        return {
            "config_snapshot_id": f"config:{session_id}",
            "config_checksum": sha256_hex(canonical_json_bytes(config_ref)),
            "policy_snapshot_id": f"policy:{session_id}",
            "policy_checksum": sha256_hex(canonical_json_bytes({} if policy is None else policy)),
        }

    def _tool_availability(self, config_snapshot: dict[str, Any]) -> dict[str, Any]:
        execution = config_snapshot.get("execution")
        if not isinstance(execution, dict):
            raise _store_error("Frozen execution config is missing.")
        max_timeout = execution.get("max_shell_timeout_seconds")
        default_timeout = execution.get("default_shell_timeout_seconds")
        cancellation_timeout = execution.get("cancellation_timeout_seconds")
        if not isinstance(max_timeout, int) or max_timeout <= 0:
            raise _store_error("Frozen execution max_shell_timeout_seconds is invalid.")
        if not isinstance(default_timeout, int) or default_timeout <= 0:
            raise _store_error("Frozen execution default_shell_timeout_seconds is invalid.")
        if not isinstance(cancellation_timeout, int) or cancellation_timeout <= 0:
            raise _store_error("Frozen execution cancellation_timeout_seconds is invalid.")
        if max_timeout < default_timeout:
            raise _store_error("Frozen execution max_shell_timeout_seconds is invalid.")
        multimodal = config_snapshot.get("multimodal")
        if not isinstance(multimodal, dict):
            raise _store_error("Frozen multimodal config is missing.")
        timeout_seconds = _positive_int(
            multimodal.get("timeout_seconds"),
            "Frozen view_image timeout_seconds is invalid.",
        )
        max_tokens = _positive_int(
            multimodal.get("max_tokens"),
            "Frozen view_image max_tokens is invalid.",
        )
        max_query_chars = _positive_int(
            multimodal.get("max_query_chars"),
            "Frozen view_image max_query_chars is invalid.",
        )
        max_analysis_chars = _positive_int(
            multimodal.get("max_analysis_chars"),
            "Frozen view_image max_analysis_chars is invalid.",
        )
        view_image_enabled = multimodal.get("view_image_enabled") is True
        disabled_reason = None
        if not view_image_enabled:
            disabled_reason = multimodal.get("view_image_disabled_reason")
            if not isinstance(disabled_reason, str) or not disabled_reason:
                raise _store_error("Frozen view_image disabled reason is invalid.")
        availability = {
            "shell_exec": {
                "max_timeout_seconds": max_timeout,
            },
            "view_image": {
                "enabled": view_image_enabled,
                "disabled_reason": disabled_reason,
                "timeout_seconds": timeout_seconds,
                "max_tokens": max_tokens,
                "max_query_chars": max_query_chars,
                "max_analysis_chars": max_analysis_chars,
            },
        }
        availability["checksum"] = sha256_hex(canonical_json_bytes(availability))
        return availability

    def _conversation_store(self) -> ConversationStore:
        if self.conversation_store is not None:
            return self.conversation_store
        return ConversationStore(self.connection, artifact_store=self.artifact_store)

    def _todo_plan_store(self) -> TodoPlanStore:
        return self.todo_plan_store or TodoPlanStore(self.connection)


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


def _highest_message_index(connection: sqlite3.Connection, run_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(message_index), 0) FROM conversation_messages WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row[0])


def _is_error(value: Any, error_class: str, reason: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("error_class") == error_class
        and value.get("reason") == reason
    )


def _positive_int(value: Any, message: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise _store_error(message)
    return value


def _terminal_error_summary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    message = value.get("message")
    return message if isinstance(message, str) else None


def _store_error(message: str) -> StoreError:
    return StoreError(
        error_class="persistence_error",
        message=message,
        recoverable=False,
    )

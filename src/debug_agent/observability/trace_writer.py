from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.conversation import ConversationStore
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.runtime.contracts import TOOL_RESULT_STATUSES, utc_now_iso


@dataclass(frozen=True)
class TraceRenderResult:
    trace_path: Path
    refreshed: bool
    session_id: str
    workspace_root: str
    run_count: int
    artifact_count: int
    terminal_status: str
    error_summary: str | None


class TraceRenderError(StoreError):
    pass


class TraceWriter:
    def __init__(self, connection: sqlite3.Connection, sessions_root: Path) -> None:
        self.connection = connection
        self.sessions_root = sessions_root.resolve()

    def refresh_if_stale(self, session_id: str) -> TraceRenderResult:
        data = self._load_conversation_trace(session_id)
        trace_path = self.sessions_root / session_id / "logs" / "trace.md"
        rendered = _render_conversation_trace(data)
        _atomic_write_text(trace_path, rendered)
        return TraceRenderResult(
            trace_path=trace_path,
            refreshed=True,
            session_id=session_id,
            workspace_root=data["session"].workspace_root,
            run_count=1 if data["run"] is not None else 0,
            artifact_count=len(data["artifacts"]),
            terminal_status=data["session"].status,
            error_summary=data["session"].error_summary,
        )

    def _load_conversation_trace(self, session_id: str) -> dict[str, Any]:
        try:
            self.connection.execute("BEGIN")
            session = SessionStore(self.connection).get(session_id)
            run = RunStore(self.connection).latest_for_session(session.session_id)
            rows = [] if run is None else ConversationStore(
                self.connection,
                artifact_store=ArtifactStore(self.connection, self.sessions_root),
            ).list_messages(run.run_id)
            artifacts = ArtifactStore(self.connection, self.sessions_root).list_for_session(
                session.session_id
            )
            _validate_trace_rows(
                session_id=session.session_id,
                run_id=run.run_id if run is not None else None,
                rows=rows,
                artifacts=artifacts,
                sessions_root=self.sessions_root,
            )
            data = {
                "session": session,
                "run": run,
                "rows": rows,
                "artifacts": artifacts,
                "exported_at": utc_now_iso(),
            }
            self.connection.execute("ROLLBACK")
            return data
        except TraceRenderError:
            _rollback_if_needed(self.connection)
            raise
        except sqlite3.OperationalError as exc:
            _rollback_if_needed(self.connection)
            if "busy" in str(exc).lower() or "locked" in str(exc).lower():
                raise StoreError(
                    error_class="persistence_error",
                    message="persistence_error/sqlite_busy_timeout: SQLite remained busy while reading trace data.",
                    recoverable=True,
                ) from exc
            raise StoreError(
                error_class="persistence_error",
                message=f"persistence_error/persistence_read_failed: Trace read failed: {exc}",
                recoverable=False,
            ) from exc
        except StoreError:
            _rollback_if_needed(self.connection)
            raise
        except sqlite3.DatabaseError as exc:
            _rollback_if_needed(self.connection)
            raise StoreError(
                error_class="persistence_error",
                message=f"persistence_error/persistence_read_failed: Trace read failed: {exc}",
                recoverable=False,
            ) from exc

def _render_conversation_trace(data: dict[str, Any]) -> str:
    session = data["session"]
    run = data["run"]
    rows = [row for row in data["rows"] if row.kind != "context_summary"]
    assistant_count = sum(
        1 for row in rows if row.kind in {"assistant_output", "assistant_tool_call"}
    )
    tool_call_count = sum(
        len(_tool_calls(row)) for row in rows if row.kind == "assistant_tool_call"
    )
    lines = [
        "# debug-agent conversation trace",
        "",
        f"*Exported on {data['exported_at']}*",
        "",
        "**📊 Session Information**",
        f"- **Session ID**: `{session.session_id}`",
        f"- **Run ID**: `{run.run_id if run is not None else ''}`",
        f"- **Workspace**: `{session.workspace_root}`",
        f"- **Status**: `{session.status}`",
    ]
    if session.status in {"completed", "failed"} and session.terminal_reason:
        lines.append(f"- **Terminal Reason**: `{session.terminal_reason}`")
    lines.extend(
        [
            f"- **Started**: {session.created_at}",
            f"- **Last Updated**: {session.updated_at}",
            f"- **Approval Mode**: `{session.approval_mode}`",
            f"- **Total Messages**: {len(rows)}",
            f"- **Total User Messages**: {sum(1 for row in rows if row.kind == 'user_input')}",
            f"- **Total Assistant Messages**: {assistant_count}",
            f"- **Total Tool Calls**: {tool_call_count}",
        ]
    )
    tool_results = _tool_results_by_pair(rows)
    artifact_by_id = {artifact.artifact_id: artifact for artifact in data["artifacts"]}
    for row in rows:
        if row.kind == "user_input":
            _append_message(
                lines,
                "## 👤 User",
                row,
                _message_content(row, artifact_by_id),
            )
        elif row.kind == "assistant_output":
            _append_message(
                lines,
                "## 🤖 Assistant",
                row,
                _message_content(row, artifact_by_id),
            )
        elif row.kind == "assistant_tool_call":
            _append_tool_call_message(lines, row, tool_results)
        elif row.kind in {"failure_fact", "cancellation_fact"}:
            _append_runtime_fact(lines, row)
        elif row.kind == "tool_result":
            continue
    lines.append("")
    return "\n".join(lines)


def _append_message(
    lines: list[str], heading: str, row: Any, content: str
) -> None:
    lines.extend(["", "---", "", heading, _message_meta(row), ""])
    lines.append(content)


def _append_tool_call_message(
    lines: list[str],
    row: Any,
    tool_results: dict[tuple[str | None, str], Any],
) -> None:
    lines.extend(["", "---", "", "## 🤖 Assistant", _message_meta(row), ""])
    text = _assistant_tool_call_text(row.content)
    if isinstance(text, str) and text:
        lines.extend([text, ""])
    lines.append("### 🔧 Tool Calls")
    for call in _tool_calls(row):
        call_id = str(call["id"])
        result = tool_results[(row.model_call_id, call_id)]
        status = _tool_result_status(result)
        icon = _status_icon(status)
        name = str(call["name"])
        lines.extend(
            [
                "",
                f"**{icon} {name}** (`{name}`)",
                f"- **Status**: `{status}`",
                f"- **Call ID**: `{call_id}`",
                f"- **Tool Result Index**: {result.message_index}",
                f"- **Timestamp**: {result.accepted_at}",
                "- **Arguments**:",
            ]
        )
        lines.extend(_indented_preview(_redacted_arguments(name, call.get("args", {}))))
        error = _tool_result_error(result)
        preview = _tool_result_preview(result)
        if error is not None:
            lines.append("- **Error**:")
            lines.extend(_indented_preview(error))
            if preview is not None:
                lines.append("- **Result**:")
                lines.extend(_indented_preview(preview))
        else:
            lines.append("- **Result**:")
            lines.extend(_indented_preview(preview))


def _append_runtime_fact(lines: list[str], row: Any) -> None:
    content = row.content if isinstance(row.content, dict) else {}
    error_class = content.get("error_class") or content.get("class") or row.kind
    reason = content.get("reason") or row.kind
    message = content.get("message") or ""
    lines.extend(
        [
            "",
            "---",
            "",
            "## ⚠️ Runtime Fact",
            f"*{row.accepted_at}* • **Message Index**: {row.message_index} • **Kind**: `{row.kind}`",
            "",
            f"`{error_class}/{reason}`: {message}",
        ]
    )


def _message_meta(row: Any) -> str:
    suffix = f" • **Turn**: `{row.turn_id}`" if row.turn_id else ""
    return f"*{row.accepted_at}* • **Message Index**: {row.message_index}{suffix}"


def _message_content(row: Any, artifact_by_id: dict[str, Any]) -> str:
    content = row.content
    if row.artifact_id:
        return json.dumps(
            _row_artifact_reference(row, artifact_by_id),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    if isinstance(content, dict) and isinstance(content.get("content"), str):
        return content["content"]
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2)


def _assistant_tool_call_text(content: Any) -> str | None:
    if not isinstance(content, dict):
        return None
    for key in ("content", "text"):
        value = content.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            text_blocks = [
                str(item["text"])
                for item in value
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
                and item.get("text")
            ]
            if text_blocks:
                return "\n".join(text_blocks)
    return None


def _row_artifact_reference(row: Any, artifact_by_id: dict[str, Any]) -> dict[str, Any]:
    artifact = artifact_by_id.get(row.artifact_id)
    if artifact is None:
        raise StoreError(
            error_class="persistence_error",
            message=f"persistence_error/artifact_missing: Missing artifact record {row.artifact_id}.",
            recoverable=False,
        )
    _validate_row_artifact_metadata(row, artifact)
    reference: dict[str, Any] = {
        "artifact_id": artifact.artifact_id,
        "relative_path": artifact.relative_path,
    }
    if isinstance(row.metadata, dict):
        for key in ("preview", "preview_metadata", "reference", "reference_metadata"):
            if key in row.metadata:
                reference[key] = row.metadata[key]
    return reference


def _validate_trace_rows(
    *,
    session_id: str,
    run_id: str | None,
    rows: list[Any],
    artifacts: list[Any],
    sessions_root: Path,
) -> None:
    if run_id is not None:
        if any(row.session_id != session_id or row.run_id != run_id for row in rows):
            _invalid_trace("Conversation row session/run scope mismatch.")
    rendered_rows = [row for row in rows if row.kind != "context_summary"]
    indexes = [row.message_index for row in rows]
    if indexes != list(range(1, len(rows) + 1)):
        _invalid_trace("Conversation message indexes are not contiguous.")
    groups: dict[str, list[Any]] = {}
    for row in rows:
        if row.group_status != "closed":
            _invalid_trace("Accepted conversation row is not in a closed group.")
        if row.role not in {"user", "assistant", "tool", "runtime"}:
            _invalid_trace("Conversation row uses unsupported role.")
        if row.kind not in {
            "user_input",
            "assistant_output",
            "assistant_tool_call",
            "tool_result",
            "failure_fact",
            "cancellation_fact",
            "context_summary",
        }:
            _invalid_trace("Conversation row uses unsupported kind.")
        groups.setdefault(row.message_group_id, []).append(row)
        if row.kind == "tool_result":
            status = _tool_result_status(row)
            if status not in TOOL_RESULT_STATUSES:
                _invalid_trace("Tool result uses unsupported status.")
        _validate_row_artifacts(row, artifacts, sessions_root)
    for group_rows in groups.values():
        positions = sorted(row.group_position for row in group_rows)
        expected = group_rows[0].group_row_count
        if positions != list(range(expected)) or len(group_rows) != expected:
            _invalid_trace("Conversation group positions are not contiguous.")
    calls: dict[tuple[str | None, str], None] = {}
    results: dict[tuple[str | None, str], int] = {}
    for row in rendered_rows:
        if row.kind == "assistant_tool_call":
            if not row.model_call_id:
                _invalid_trace("Assistant tool-call row is missing model_call_id.")
            for call in _tool_calls(row):
                key = (row.model_call_id, str(call["id"]))
                if key in calls:
                    _invalid_trace("Duplicate assistant tool call.")
                calls[key] = None
        elif row.kind == "tool_result":
            if not row.model_call_id or not row.tool_call_id:
                _invalid_trace("Tool result row is missing pairing identifiers.")
            key = (row.model_call_id, row.tool_call_id)
            results[key] = results.get(key, 0) + 1
    if set(calls) != set(results) or any(count != 1 for count in results.values()):
        _invalid_trace("conversation_cut_invalid: tool call/result pairing is invalid.")


def _validate_row_artifacts(row: Any, artifacts: list[Any], sessions_root: Path) -> None:
    artifact_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    if row.artifact_id:
        _validate_artifact_reference(
            artifact_id=row.artifact_id,
            expected_relative_path=None,
            row=row,
            artifact_by_id=artifact_by_id,
            sessions_root=sessions_root,
        )
        _validate_row_artifact_metadata(row, artifact_by_id[row.artifact_id])
    if row.kind == "tool_result" and isinstance(row.content, dict):
        artifact_ids = row.content.get("artifact_ids", [])
        if artifact_ids is None:
            artifact_ids = []
        if not isinstance(artifact_ids, list) or not all(
            isinstance(item, str) for item in artifact_ids
        ):
            _invalid_trace("Tool result artifact_ids is malformed.")
        inline_refs = _inline_artifact_refs(row.content.get("content"))
        inline_ids = [ref["artifact_id"] for ref in inline_refs]
        if set(artifact_ids) != set(inline_ids) or len(artifact_ids) != len(inline_ids):
            _invalid_trace(
                "Tool result artifact_ids do not match inline artifact references."
            )
        for ref in inline_refs:
            _validate_artifact_reference(
                artifact_id=ref["artifact_id"],
                expected_relative_path=ref.get("relative_path"),
                row=row,
                artifact_by_id=artifact_by_id,
                sessions_root=sessions_root,
            )


def _validate_artifact_reference(
    *,
    artifact_id: str,
    expected_relative_path: Any,
    row: Any,
    artifact_by_id: dict[str, Any],
    sessions_root: Path,
) -> None:
    artifact = artifact_by_id.get(artifact_id)
    if artifact is None:
        raise StoreError(
            error_class="persistence_error",
            message=f"persistence_error/artifact_missing: Missing artifact record {artifact_id}.",
            recoverable=False,
        )
    if artifact.session_id != row.session_id:
        _invalid_trace("Artifact session scope does not match conversation row.")
    if artifact.run_id not in {None, row.run_id}:
        _invalid_trace("Artifact run scope does not match conversation row.")
    if expected_relative_path is not None and expected_relative_path != artifact.relative_path:
        _invalid_trace("Artifact relative_path conflicts with ArtifactStore record.")
    if not (sessions_root / artifact.relative_path).exists():
        raise StoreError(
            error_class="persistence_error",
            message=f"persistence_error/artifact_missing: Missing artifact content {artifact_id}.",
            recoverable=False,
        )


def _validate_row_artifact_metadata(row: Any, artifact: Any) -> None:
    if row.kind not in {"user_input", "assistant_output"}:
        return
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    reference = metadata.get("reference")
    if not isinstance(reference, dict):
        raise StoreError(
            error_class="persistence_error",
            message=(
                "persistence_error/artifact_missing: "
                "Artifact-backed row reference metadata is missing."
            ),
            recoverable=False,
        )
    if reference.get("artifact_id") != row.artifact_id:
        _invalid_trace("Artifact-backed row artifact_id metadata conflicts.")
    if reference.get("relative_path") != artifact.relative_path:
        _invalid_trace("Artifact-backed row relative_path metadata conflicts.")


def _inline_artifact_refs(value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("artifact_id"), str):
            refs.append(
                {
                    "artifact_id": value["artifact_id"],
                    "relative_path": value.get("relative_path"),
                }
            )
        for child in value.values():
            refs.extend(_inline_artifact_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_inline_artifact_refs(child))
    return refs


def _tool_calls(row: Any) -> list[dict[str, Any]]:
    content = row.content
    if not isinstance(content, dict) or not isinstance(content.get("tool_calls"), list):
        _invalid_trace("Assistant tool-call content is malformed.")
    calls: list[dict[str, Any]] = []
    for call in content["tool_calls"]:
        if not isinstance(call, dict) or not call.get("id") or not call.get("name"):
            _invalid_trace("Assistant tool-call item is malformed.")
        calls.append(call)
    return calls


def _tool_results_by_pair(rows: list[Any]) -> dict[tuple[str | None, str], Any]:
    return {
        (row.model_call_id, row.tool_call_id): row
        for row in rows
        if row.kind == "tool_result" and row.tool_call_id is not None
    }


def _tool_result_status(row: Any) -> str:
    if not isinstance(row.content, dict):
        _invalid_trace("Tool result content is malformed.")
    status = row.content.get("status")
    metadata_status = row.metadata.get("status") if isinstance(row.metadata, dict) else None
    if metadata_status is not None and metadata_status != status:
        _invalid_trace("Tool result metadata status conflicts with content status.")
    if not isinstance(status, str):
        _invalid_trace("Tool result status is missing.")
    return status


def _tool_result_preview(row: Any) -> Any:
    content = row.content if isinstance(row.content, dict) else {}
    return content.get("content")


def _tool_result_error(row: Any) -> Any:
    content = row.content if isinstance(row.content, dict) else {}
    return content.get("error")


def _redacted_arguments(tool_name: str, args: Any) -> Any:
    if not isinstance(args, dict):
        return args
    redacted = dict(args)
    if tool_name == "write_file" and isinstance(redacted.get("content"), str):
        redacted["content"] = _redaction_object(redacted["content"])
    if tool_name == "edit_file":
        for key in ("old_text", "new_text"):
            if isinstance(redacted.get(key), str):
                redacted[key] = _redaction_object(redacted[key])
    return redacted


def _redaction_object(value: str) -> dict[str, Any]:
    payload = value.encode("utf-8")
    return {
        "redacted": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _indented_preview(value: Any) -> list[str]:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    elif value is None:
        text = "null"
    else:
        text = str(value)
    lines = text.splitlines() or [""]
    truncated = False
    if len(lines) > 100:
        lines = lines[:100]
        truncated = True
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000]
        lines = text.splitlines()
        truncated = True
    if truncated:
        lines.append("[truncated]")
    return [f"    {line}" for line in lines]


def _status_icon(status: str) -> str:
    if status == "ok":
        return "✅"
    if status in {"error", "denied"}:
        return "❌"
    if status == "timeout":
        return "⏱️"
    if status == "cancelled":
        return "⏹️"
    _invalid_trace("Tool result uses unsupported status.")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, path)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise StoreError(
            error_class="ui_error",
            message=f"ui_error/trace_render_failed: Trace render/write failed: {exc}",
            source="ui",
            recoverable=False,
        ) from exc


def _rollback_if_needed(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        pass


def _invalid_trace(message: str) -> None:
    raise TraceRenderError(
        error_class="persistence_error",
        message=f"persistence_error/conversation_cut_invalid: {message}",
        recoverable=False,
    )


def build_phase3_observability_summary(
    connection: sqlite3.Connection,
    *,
    session: Any,
    latest_run: Any | None,
    events: list[Any] | None = None,
) -> dict[str, Any]:
    run_id = latest_run.run_id if latest_run is not None else None
    loaded_events = (
        events
        if events is not None
        else EventWriter(connection, Path(".")).list_for_session(session.session_id)
    )
    return {
        "terminal_checkpoint": _terminal_checkpoint_summary(
            connection, session=session, latest_run=latest_run
        ),
        "durable_conversation": _durable_conversation_summary(
            connection, run_id=run_id
        ),
        "normalized_errors": _normalized_error_summaries(loaded_events),
        "retry": _retry_summaries(loaded_events),
        "cancellation": _cancellation_summaries(loaded_events),
        "resume": _resume_summaries(loaded_events),
        "stale_fail_close": _stale_fail_close_summaries(loaded_events),
    }


def _terminal_checkpoint_summary(
    connection: sqlite3.Connection,
    *,
    session: Any,
    latest_run: Any | None,
) -> dict[str, Any]:
    checkpoint_id = session.latest_checkpoint_id or (
        latest_run.latest_checkpoint_id if latest_run is not None else None
    )
    if not checkpoint_id:
        return {
            "checkpoint_id": None,
            "terminal_reason": getattr(session, "terminal_reason", None),
            "terminal_status": getattr(session, "status", None),
            "checkpoint_valid": False,
            "eligible": False,
        }
    summary: dict[str, Any] = {
        "checkpoint_id": checkpoint_id,
        "terminal_reason": getattr(session, "terminal_reason", None),
        "terminal_status": getattr(session, "status", None),
        "checkpoint_valid": False,
        "eligible": False,
    }
    store = CheckpointStore(connection).for_phase_3_5_internal()
    try:
        checkpoint = store.get(checkpoint_id)
    except StoreError as exc:
        summary["validation_error"] = exc.message
        return summary
    try:
        store.validate_terminal_recovery(checkpoint, validate_current_todo=False)
        checkpoint_valid = True
    except StoreError as exc:
        checkpoint_valid = False
        summary["validation_error"] = exc.message
    payload = checkpoint.state if isinstance(checkpoint.state, dict) else {}
    lifecycle_terminal = (
        getattr(session, "status", None) in {"completed", "failed"}
        and latest_run is not None
        and getattr(latest_run, "status", None) in {"completed", "failed"}
    )
    checkpoint_refs_current = (
        latest_run is not None
        and session.latest_checkpoint_id == latest_run.latest_checkpoint_id
        and checkpoint.checkpoint_id == session.latest_checkpoint_id
    )
    summary.update(
        {
            "kind": checkpoint.kind,
            "terminal_reason": payload.get("terminal_reason")
            or getattr(session, "terminal_reason", None),
            "terminal_status": payload.get("terminal_status")
            or getattr(session, "status", None),
            "terminal_error": payload.get("terminal_error"),
            "checkpoint_valid": checkpoint_valid,
            "eligible": (
                checkpoint_valid
                and checkpoint.kind == "terminal_recovery"
                and lifecycle_terminal
                and checkpoint_refs_current
                and latest_run is not None
                and latest_run.run_type == "prompt"
                and not getattr(session, "non_resumable_startup_failure", False)
                and not getattr(latest_run, "non_resumable_startup_failure", False)
            ),
        }
    )
    return summary


def _durable_conversation_summary(
    connection: sqlite3.Connection, *, run_id: str | None
) -> dict[str, Any]:
    if run_id is None:
        return {
            "high_watermark": 0,
            "message_count": 0,
            "projection_high_watermark": 0,
            "projection_ref_count": 0,
            "projection_update_reason": None,
        }
    store = ConversationStore(connection)
    messages = store.list_messages(run_id)
    projection = store.get_projection(run_id)
    return {
        "high_watermark": max((row.message_index for row in messages), default=0),
        "message_count": len(messages),
        "projection_high_watermark": projection.source_high_watermark,
        "projection_ref_count": len(projection.message_refs),
        "projection_update_reason": projection.update_reason,
    }


def _normalized_error_summaries(events: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for event in events:
        error = _payload_error(event.payload)
        if not isinstance(error, dict):
            continue
        artifact_ids = error.get("artifact_ids", [])
        if not isinstance(artifact_ids, list):
            artifact_ids = []
        summaries.append(
            {
                "event_kind": event.kind,
                "error_class": error.get("error_class"),
                "reason": error.get("reason"),
                "message": error.get("message"),
                "scope": error.get("scope"),
                "recoverability": error.get("recoverability"),
                "model_visible_projection": {
                    "error_class": error.get("error_class"),
                    "reason": error.get("reason"),
                    "message": error.get("message"),
                    "artifact_ids": artifact_ids,
                },
            }
        )
    return summaries


def _payload_error(payload: dict[str, Any]) -> dict[str, Any] | None:
    error = payload.get("error")
    if isinstance(error, dict):
        return error
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        return result["error"]
    return None


def _retry_summaries(events: list[Any]) -> dict[str, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    exhausted: list[dict[str, Any]] = []
    for event in events:
        retry = event.payload.get("retry")
        if not isinstance(retry, dict):
            continue
        item = {
            "event_kind": event.kind,
            "strategy": retry.get("strategy"),
            "attempt": retry.get("attempt"),
            "max_attempts": retry.get("max_attempts"),
            "source_error_class": retry.get("source_error_class"),
            "source_reason": retry.get("source_reason"),
            "result_error_class": retry.get("result_error_class"),
            "result_reason": retry.get("result_reason"),
            "exhausted": bool(retry.get("exhausted", False)),
        }
        if item["exhausted"]:
            exhausted.append(item)
        else:
            attempts.append(item)
    return {"attempts": attempts, "exhausted": exhausted}


def _cancellation_summaries(events: list[Any]) -> list[dict[str, Any]]:
    cancellations: list[dict[str, Any]] = []
    for event in events:
        error = _payload_error(event.payload)
        if not isinstance(error, dict) or error.get("error_class") != "cancelled":
            continue
        metadata = error.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        cancellations.append(
            {
                "event_kind": event.kind,
                "reason": error.get("reason"),
                "scope": error.get("scope"),
                "message": error.get("message"),
                "remote_stop_confirmed": bool(
                    metadata.get("remote_stop_confirmed", False)
                ),
                "billing_stop_confirmed": bool(
                    metadata.get("billing_stop_confirmed", False)
                ),
            }
        )
    return cancellations


def _resume_summaries(events: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for event in events:
        if event.kind not in {"session_resumed", "run_resumed"}:
            continue
        summaries.append(
            {
                "event_kind": event.kind,
                "session_id": event.payload.get("session_id"),
                "run_id": event.payload.get("run_id"),
                "outcome": event.payload.get("outcome", "succeeded"),
            }
        )
    return summaries


def _stale_fail_close_summaries(events: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for event in events:
        if event.kind != "stale_fail_closed":
            continue
        summaries.append(
            {
                "session_id": event.payload.get("session_id"),
                "run_id": event.payload.get("run_id"),
                "terminal_reason": event.payload.get("terminal_reason"),
                "stale_proof_summary": event.payload.get("stale_proof_summary", {}),
                "has_normalized_error": isinstance(event.payload.get("error"), dict),
            }
        )
    return summaries


def _render_phase3_observability(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    checkpoint = summary["terminal_checkpoint"]
    lines.append(
        "- terminal_checkpoint: "
        f"checkpoint_id={checkpoint.get('checkpoint_id') or ''} "
        f"terminal_reason={checkpoint.get('terminal_reason') or ''} "
        f"terminal_status={checkpoint.get('terminal_status') or ''} "
        f"checkpoint_valid={str(bool(checkpoint.get('checkpoint_valid'))).lower()} "
        f"eligible={str(bool(checkpoint.get('eligible'))).lower()}"
    )
    conversation = summary["durable_conversation"]
    lines.append(
        "- durable_conversation: "
        f"conversation_high_watermark={conversation['high_watermark']} "
        f"message_count={conversation['message_count']} "
        f"projection_high_watermark={conversation['projection_high_watermark']} "
        f"projection_refs={conversation['projection_ref_count']} "
        f"projection_update_reason={conversation['projection_update_reason'] or ''}"
    )
    if summary["normalized_errors"]:
        for error in summary["normalized_errors"]:
            lines.append(
                "- normalized_error: "
                f"{error.get('error_class')}/{error.get('reason')} "
                f"scope={error.get('scope') or ''} "
                f"recoverability={error.get('recoverability') or ''} "
                f"message={_shorten(str(error.get('message') or ''))} "
                f"model_visible_projection={error.get('model_visible_projection')}"
            )
    else:
        lines.append("- normalized_error: none")
    retry = summary["retry"]
    for attempt in retry["attempts"]:
        lines.append(
            "- retry_attempt "
            f"strategy={attempt.get('strategy')} "
            f"attempt={attempt.get('attempt')}/{attempt.get('max_attempts')} "
            f"source={attempt.get('source_error_class')}/{attempt.get('source_reason')}"
        )
    for item in retry["exhausted"]:
        lines.append(
            "- retry_exhausted "
            f"strategy={item.get('strategy')} "
            f"attempt={item.get('attempt')}/{item.get('max_attempts')} "
            f"source={item.get('source_error_class')}/{item.get('source_reason')} "
            f"result={item.get('result_error_class')}/{item.get('result_reason')}"
        )
    if not retry["attempts"] and not retry["exhausted"]:
        lines.append("- retry: none")
    for cancellation in summary["cancellation"]:
        lines.append(
            "- cancellation: "
            f"{cancellation.get('reason')} "
            f"scope={cancellation.get('scope') or ''} "
            f"remote_stop_confirmed={str(cancellation.get('remote_stop_confirmed')).lower()} "
            f"billing_stop_confirmed={str(cancellation.get('billing_stop_confirmed')).lower()}"
        )
    if not summary["cancellation"]:
        lines.append("- cancellation: none")
    for resume in summary["resume"]:
        lines.append(
            "- resume "
            f"outcome={resume.get('outcome')} "
            f"event={resume.get('event_kind')} "
            f"session_id={resume.get('session_id') or ''} "
            f"run_id={resume.get('run_id') or ''}"
        )
    if not summary["resume"]:
        lines.append("- resume: none")
    for stale in summary["stale_fail_close"]:
        proof = stale.get("stale_proof_summary", {})
        proof = proof if isinstance(proof, dict) else {}
        lines.append(
            "- stale_fail_closed "
            f"terminal_reason={stale.get('terminal_reason') or ''} "
            f"host_match={str(bool(proof.get('host_match'))).lower()} "
            f"pid_absent={str(bool(proof.get('pid_absent'))).lower()} "
            f"token_fenced={str(bool(proof.get('token_fenced'))).lower()} "
            f"has_normalized_error={str(bool(stale.get('has_normalized_error'))).lower()}"
        )
    if not summary["stale_fail_close"]:
        lines.append("- stale_fail_closed: none")
    return lines


def _shorten(text: str) -> str:
    if len(text) <= 160:
        return text
    return text[:157] + "..."

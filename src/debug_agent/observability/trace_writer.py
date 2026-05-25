from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore


@dataclass(frozen=True)
class TraceRenderResult:
    trace_path: Path
    refreshed: bool
    session_id: str
    workspace_root: str
    run_count: int
    event_count: int
    artifact_count: int
    terminal_status: str
    error_summary: str | None


class TraceWriter:
    def __init__(self, connection: sqlite3.Connection, sessions_root: Path) -> None:
        self.connection = connection
        self.sessions_root = sessions_root.resolve()

    def refresh_if_stale(self, session_id: str) -> TraceRenderResult:
        data = self._load(session_id)
        trace_path = self.sessions_root / session_id / "trace.md"
        current_metadata = _trace_metadata(data["events"])
        rendered_metadata = _read_rendered_metadata(trace_path)
        refreshed = rendered_metadata != current_metadata
        if refreshed:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(_render_trace(data, current_metadata), encoding="utf-8")
        return TraceRenderResult(
            trace_path=trace_path,
            refreshed=refreshed,
            session_id=session_id,
            workspace_root=data["session"].workspace_root,
            run_count=len(data["runs"]),
            event_count=current_metadata["event_count"],
            artifact_count=len(data["artifacts"]),
            terminal_status=data["session"].status,
            error_summary=data["session"].error_summary,
        )

    def _load(self, session_id: str) -> dict[str, Any]:
        artifact_store = ArtifactStore(self.connection, self.sessions_root)
        artifacts = artifact_store.list_for_session(session_id)
        return {
            "session": SessionStore(self.connection).get(session_id),
            "runs": RunStore(self.connection).list_for_session(session_id),
            "events": EventWriter(
                self.connection, self.sessions_root
            ).list_for_session(session_id),
            "checkpoints": CheckpointStore(self.connection).list_for_session(session_id),
            "artifacts": artifacts,
            "artifact_exists": {
                artifact.artifact_id: (
                    self.sessions_root / artifact.relative_path
                ).exists()
                for artifact in artifacts
            },
        }


def _render_trace(data: dict[str, Any], metadata: dict[str, Any]) -> str:
    session = data["session"]
    lines = [
        f"<!-- event_count: {metadata['event_count']} -->",
        f"<!-- latest_event_id: {metadata['latest_event_id'] or ''} -->",
        f"# debug-agent trace {session.session_id}",
        "",
        "## Session Summary",
        f"- session_id: {session.session_id}",
        f"- workspace_root: {session.workspace_root}",
        f"- status: {session.status}",
        f"- approval_mode: {session.approval_mode}",
        f"- active_run_id: {session.active_run_id or ''}",
        f"- latest_checkpoint_id: {session.latest_checkpoint_id or ''}",
        f"- created_at: {session.created_at}",
        f"- updated_at: {session.updated_at}",
        f"- error_summary: {session.error_summary or ''}",
        "",
        "## Runs",
    ]
    for run in data["runs"]:
        lines.append(
            f"- {run.run_id}: type={run.run_type} status={run.status} "
            f"latest_checkpoint_id={run.latest_checkpoint_id or ''} "
            f"error_summary={run.error_summary or ''}"
        )
    lines.extend(["", "## Timeline"])
    for event in data["events"]:
        lines.append(
            f"- {event.timestamp} {event.kind} run={event.run_id} "
            f"payload={_summarize_payload(event.payload, event.kind)}"
        )
    lines.extend(["", "## Checkpoints"])
    for checkpoint in data["checkpoints"]:
        lines.append(
            f"- {checkpoint.created_at} {checkpoint.checkpoint_id}: "
            f"kind={checkpoint.kind} run={checkpoint.run_id} "
            f"summary={checkpoint.summary or ''}"
        )
    lines.extend(["", "## Artifacts"])
    for artifact in data["artifacts"]:
        exists = data["artifact_exists"].get(artifact.artifact_id, False)
        status = "present" if exists else "missing"
        lines.append(
            f"- {artifact.artifact_id}: type={artifact.artifact_type} "
            f"path={artifact.relative_path} exists={str(exists).lower()} "
            f"status={status} run={artifact.run_id or ''}"
        )
    lines.extend(["", "## Errors"])
    error_events = [event for event in data["events"] if event.kind.endswith("_failed")]
    if session.error_summary:
        lines.append(f"- session_error: {session.error_summary}")
    for event in error_events:
        lines.append(
            f"- {event.timestamp} {event.kind}: "
            f"{_summarize_payload(event.payload, event.kind)}"
        )
    if not session.error_summary and not error_events:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _trace_metadata(events: list[Any]) -> dict[str, Any]:
    return {
        "event_count": len(events),
        "latest_event_id": events[-1].event_id if events else None,
    }


def _read_rendered_metadata(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    event_count = re.search(r"<!-- event_count: (\d+) -->", text)
    latest_event = re.search(r"<!-- latest_event_id: ([^ ]*) -->", text)
    if event_count is None or latest_event is None:
        return None
    latest_event_id = latest_event.group(1) or None
    return {"event_count": int(event_count.group(1)), "latest_event_id": latest_event_id}


def _summarize_payload(payload: dict[str, Any], kind: str | None = None) -> str:
    if kind == "model_call_completed":
        return _summarize_model_completed(payload)
    if kind == "tool_call_completed":
        return _summarize_tool_completed(payload)
    if kind == "skill_snapshot_created":
        return _summarize_skill_snapshot(payload)
    if kind == "skill_activated":
        return _summarize_skill_activation(payload)
    if kind == "skill_reference_loaded":
        return _summarize_skill_reference_loaded(payload)
    text = str(payload)
    if len(text) <= 240:
        return text
    return text[:237] + "..."


def _summarize_model_completed(payload: dict[str, Any]) -> str:
    response = payload.get("redacted_output") or payload.get("content") or ""
    tool_names = [
        str(call.get("name", ""))
        for call in payload.get("tool_calls", [])
        if isinstance(call, dict)
    ]
    return (
        f"{{'duration': {payload.get('duration')}, "
        f"response={_shorten(str(response))}, "
        f"tool_calls={','.join(tool_names)}, "
        f"artifact_ids={payload.get('artifact_ids', [])}, "
        f"usage={payload.get('usage', {})}}}"
    )


def _summarize_tool_completed(payload: dict[str, Any]) -> str:
    result = payload.get("result", {})
    result_text = ""
    if isinstance(result, dict):
        result_text = str(result.get("redacted_output") or result.get("output") or "")
    return (
        f"{{'duration': {payload.get('duration')}, "
        f"tool_name={payload.get('tool_name', '')}, "
        f"status={payload.get('status', '')}, "
        f"result={_shorten(result_text)}, "
        f"artifact_ids={payload.get('artifact_ids', [])}}}"
    )


def _summarize_skill_snapshot(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"skill={payload.get('skill_name', '')}, "
        f"mode={payload.get('execution_mode', '')}, "
        f"scope={payload.get('source_scope', '')}, "
        f"hash={payload.get('content_hash', '')}, "
        f"references={payload.get('reference_count', 0)}"
        "}"
    )


def _summarize_skill_activation(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"skill={payload.get('skill_name', '')}, "
        f"hash={payload.get('content_hash', '')}, "
        f"reason={payload.get('activation_reason', '')}, "
        f"scope={payload.get('scope', '')}"
        "}"
    )


def _summarize_skill_reference_loaded(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"skill={payload.get('skill_name', '')}, "
        f"skill_hash={payload.get('skill_content_hash', '')}, "
        f"reference={payload.get('reference_path', '')}, "
        f"reference_hash={payload.get('reference_content_hash', '')}, "
        f"media={payload.get('media_kind', '')}, "
        f"bytes={payload.get('size_bytes', 0)}, "
        f"artifact_id={payload.get('artifact_id') or ''}"
        "}"
    )


def _shorten(text: str) -> str:
    if len(text) <= 160:
        return text
    return text[:157] + "..."

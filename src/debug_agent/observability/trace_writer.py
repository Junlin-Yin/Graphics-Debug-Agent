from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.conversation import ConversationStore
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.todo_plans import TodoPlanStore


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
        runs = RunStore(self.connection).list_for_session(session_id)
        session = SessionStore(self.connection).get(session_id)
        events = EventWriter(
            self.connection, self.sessions_root
        ).list_for_session(session_id)
        latest_run = runs[-1] if runs else None
        return {
            "session": session,
            "runs": runs,
            "events": events,
            "checkpoints": CheckpointStore(self.connection).list_for_session(session_id),
            "context_snapshots": _load_context_snapshots(self.connection, session_id),
            "todo_plans": _load_todo_plans(self.connection, runs),
            "approval_grants": _load_approval_grants(self.connection, session_id),
            "artifacts": artifacts,
            "artifact_exists": {
                artifact.artifact_id: (
                    self.sessions_root / artifact.relative_path
                ).exists()
                for artifact in artifacts
            },
            "phase3_observability": build_phase3_observability_summary(
                self.connection,
                session=session,
                latest_run=latest_run,
                events=events,
            ),
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
        active_skills = ", ".join(
            _format_active_skill(record) for record in run.active_skills
        )
        lines.append(
            f"- {run.run_id}: type={run.run_type} status={run.status} "
            f"latest_checkpoint_id={run.latest_checkpoint_id or ''} "
            f"context_snapshot_id={run.context_snapshot_id or ''} "
            f"active_skills=[{active_skills}] "
            f"error_summary={run.error_summary or ''}"
        )
    lines.extend(["", "## Todo Plans"])
    if data["todo_plans"]:
        for plan in data["todo_plans"]:
            counts = plan["counts"]
            lines.append(
                f"- {plan['run_id']}: v{plan['plan_version']} "
                f"{counts['pending']} pending, "
                f"{counts['in_progress']} in_progress, "
                f"{counts['completed']} completed"
            )
            for item in plan["items"]:
                active_form = (
                    f" activeForm={item['activeForm']}"
                    if "activeForm" in item
                    else ""
                )
                lines.append(
                    f"  - {item['index']}. {item['status']}: "
                    f"{item['content']}{active_form}"
                )
    else:
        lines.append("- none")
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
    lines.extend(["", "## Phase 3 Observability"])
    lines.extend(_render_phase3_observability(data["phase3_observability"]))
    lines.extend(["", "## Context Snapshots"])
    if data["context_snapshots"]:
        for snapshot in data["context_snapshots"]:
            lines.append(
                f"- {snapshot['created_at']} {snapshot['context_snapshot_id']}: "
                f"trigger={snapshot['trigger']} run={snapshot['run_id']} "
                f"omitted_tool_results={snapshot['omitted_tool_result_count']} "
                f"evicted_messages={snapshot['evicted_message_count']} "
                f"evicted_groups={snapshot['evicted_model_call_group_count']} "
                f"artifacts={snapshot['artifact_refs']} "
                f"payload_artifact_id={snapshot['payload_artifact_id'] or ''} "
                f"active_skills=[{', '.join(_format_active_skill(record) for record in snapshot['active_skill_records'])}]"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Approval Grants"])
    if data["approval_grants"]:
        for grant in data["approval_grants"]:
            lines.append(
                f"- {grant['created_at']} {grant['grant_id']}: "
                f"tool={grant['tool_name']} risk={grant['risk_level']} "
                f"decision={grant['decision']} scope={grant['grant_scope']} "
                f"scope_signature={grant['scope_signature']}"
            )
    else:
        lines.append("- none")
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
    store = CheckpointStore(connection)
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
    if kind == "skill_resource_loaded":
        return _summarize_skill_resource_loaded(payload)
    if kind == "approval_requested":
        return _summarize_approval_requested(payload)
    if kind == "approval_decision_recorded":
        return _summarize_approval_decision(payload)
    if kind == "approval_mode_changed":
        return _summarize_approval_mode_changed(payload)
    if kind == "tool_call_denied":
        return _summarize_tool_denied(payload)
    if kind == "tool_call_failed":
        return _summarize_tool_failed(payload)
    if kind == "artifact_registered":
        return _summarize_artifact_registered(payload)
    if kind == "context_optimized":
        return _summarize_context_optimized(payload)
    if kind == "compression_failed":
        return _summarize_compression_failed(payload)
    if kind == "context_limit_exceeded":
        return _summarize_context_limit_exceeded(payload)
    if kind == "todo_updated":
        return _summarize_todo_updated(payload)
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
    if payload.get("tool_name") == "view_image":
        return _summarize_view_image_tool_event(payload)
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
        f"resources={payload.get('resource_count', 0)}"
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


def _summarize_skill_resource_loaded(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"skill={payload.get('skill_name', '')}, "
        f"skill_hash={payload.get('skill_content_hash', '')}, "
        f"resource={payload.get('resource_path', '')}, "
        f"resource_kind={payload.get('resource_kind', '')}, "
        f"resource_hash={payload.get('resource_content_hash', '')}, "
        f"media={payload.get('media_kind', '')}, "
        f"bytes={payload.get('size_bytes', 0)}, "
        f"artifact_id={payload.get('artifact_id') or ''}"
        "}"
    )


def _summarize_approval_requested(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"tool={payload.get('tool_name', '')}, "
        f"risk={payload.get('risk_level', '')}, "
        f"target={_shorten(str(payload.get('target', '')))}, "
        f"scope_signature={payload.get('scope_signature', '')}"
        "}"
    )


def _summarize_approval_decision(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"tool={payload.get('tool_name', '')}, "
        f"decision={payload.get('decision', '')}, "
        f"grant_scope={payload.get('grant_scope', '')}, "
        f"scope_signature={payload.get('scope_signature', '')}"
        "}"
    )


def _summarize_approval_mode_changed(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"old_mode={payload.get('old_mode', '')}, "
        f"new_mode={payload.get('new_mode', '')}"
        "}"
    )


def _summarize_tool_denied(payload: dict[str, Any]) -> str:
    if payload.get("tool_name") == "view_image":
        return _summarize_view_image_tool_event(payload)
    result = payload.get("result", {})
    error = result.get("error", {}) if isinstance(result, dict) else {}
    arguments = payload.get("arguments", {})
    return (
        "{"
        f"tool={payload.get('tool_name', '')}, "
        f"error_class={error.get('error_class', '') if isinstance(error, dict) else ''}, "
        f"message={_shorten(str(error.get('message', '')) if isinstance(error, dict) else '')}, "
        f"arguments={_shorten(str(arguments))}"
        "}"
    )


def _summarize_tool_failed(payload: dict[str, Any]) -> str:
    if payload.get("tool_name") == "view_image":
        return _summarize_view_image_tool_event(payload)
    result = payload.get("result", {})
    error = result.get("error", {}) if isinstance(result, dict) else {}
    metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
    return (
        "{"
        f"tool={payload.get('tool_name', '')}, "
        f"status={payload.get('status', '')}, "
        f"error_class={error.get('error_class', '') if isinstance(error, dict) else ''}, "
        f"timeout={metadata.get('effective_timeout_seconds', '') if isinstance(metadata, dict) else ''}, "
        f"message={_shorten(str(error.get('message', '')) if isinstance(error, dict) else '')}"
        "}"
    )


def _summarize_artifact_registered(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata", {})
    return (
        "{"
        f"artifact_id={payload.get('artifact_id', '')}, "
        f"type={payload.get('artifact_type', '')}, "
        f"path={payload.get('relative_path', '')}, "
        f"metadata={_shorten(str(metadata))}"
        "}"
    )


def _summarize_context_optimized(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"trigger={payload.get('trigger', '')}, "
        f"context_snapshot_id={payload.get('context_snapshot_id', '')}, "
        f"checkpoint_id={payload.get('checkpoint_id', '')}, "
        f"omitted={payload.get('omitted_tool_result_count', 0)}, "
        f"evicted_messages={payload.get('evicted_message_count', 0)}, "
        f"evicted_groups={payload.get('evicted_model_call_group_count', 0)}, "
        f"reduced={payload.get('reduced_from_tokens', '')}->{payload.get('reduced_to_tokens', '')}, "
        f"artifacts={payload.get('artifact_refs', [])}"
        "}"
    )


def _summarize_compression_failed(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"error_class={payload.get('error_class', '')}, "
        f"reason={payload.get('reason', '')}, "
        f"message={_shorten(str(payload.get('message', '')))}"
        "}"
    )


def _summarize_context_limit_exceeded(payload: dict[str, Any]) -> str:
    return (
        "{"
        f"error_class={payload.get('error_class', '')}, "
        f"estimated={payload.get('estimated_tokens', '')}, "
        f"window={payload.get('window_tokens', '')}, "
        f"optimization_applied={payload.get('optimization_applied', [])}, "
        f"message={_shorten(str(payload.get('message', '')))}"
        "}"
    )


def _summarize_todo_updated(payload: dict[str, Any]) -> str:
    raw_counts = payload.get("counts", {})
    counts = {
        "pending": raw_counts.get("pending", 0) if isinstance(raw_counts, dict) else 0,
        "in_progress": raw_counts.get("in_progress", 0)
        if isinstance(raw_counts, dict)
        else 0,
        "completed": raw_counts.get("completed", 0)
        if isinstance(raw_counts, dict)
        else 0,
    }
    return (
        "{"
        f"previous_plan_version={payload.get('previous_plan_version', '')}, "
        f"plan_version={payload.get('plan_version', '')}, "
        f"item_count={payload.get('item_count', 0)}, "
        f"counts={counts}"
        "}"
    )


def _summarize_view_image_tool_event(payload: dict[str, Any]) -> str:
    result = payload.get("result", {})
    result_dict = result if isinstance(result, dict) else {}
    error = result_dict.get("error", {})
    output = result_dict.get("output", {})
    metadata = result_dict.get("metadata", {})
    output_dict = output if isinstance(output, dict) else {}
    metadata_dict = metadata if isinstance(metadata, dict) else {}
    images = payload.get("images")
    if not isinstance(images, list):
        images = metadata_dict.get("images")
    image_summary = _summarize_view_image_images(images if isinstance(images, list) else [])
    analysis = output_dict.get("analysis", "")
    error_class = error.get("error_class", "") if isinstance(error, dict) else ""
    if not error_class:
        top_level_error_class = payload.get("error_class")
        error_class = (
            top_level_error_class if isinstance(top_level_error_class, str) else ""
        )
    query_source = payload.get("effective_query_source")
    if not isinstance(query_source, str):
        query_source = metadata_dict.get("effective_query_source")
    if not isinstance(query_source, str):
        arguments = payload.get("arguments")
        if isinstance(arguments, dict):
            query_source = arguments.get("effective_query_source")
    if not isinstance(query_source, str):
        query_source = ""
    return (
        "{"
        f"tool=view_image, "
        f"status={payload.get('status', result_dict.get('status', ''))}, "
        f"error_class={error_class}, "
        f"provider={payload.get('vision_provider', metadata_dict.get('vision_provider', ''))}, "
        f"model={payload.get('vision_model', metadata_dict.get('vision_model', ''))}, "
        f"duration_ms={payload.get('duration_ms', metadata_dict.get('duration_ms', ''))}, "
        f"effective_query_source={query_source}, "
        f"projected_request_bytes={payload.get('projected_request_bytes', metadata_dict.get('projected_request_bytes', ''))}, "
        f"images=[{image_summary}], "
        f"analysis={_shorten(str(analysis))}"
        "}"
    )


def _summarize_view_image_images(images: list[Any]) -> str:
    parts: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        parts.append(
            "{"
            f"path={image.get('path', '')}, "
            f"mime={image.get('mime_type', '')}, "
            f"size={image.get('byte_size', '')}, "
            f"sha256={image.get('sha256', '')}, "
            f"dimensions={image.get('width', '')}x{image.get('height', '')}"
            "}"
        )
    return ", ".join(parts)


def _load_todo_plans(connection: sqlite3.Connection, runs: list[Any]) -> list[dict[str, Any]]:
    store = TodoPlanStore(connection)
    plans: list[dict[str, Any]] = []
    for run in runs:
        plan = store.get_current(run.run_id)
        if plan.version == 0:
            continue
        plans.append(
            {
                "run_id": run.run_id,
                "plan_version": plan.version,
                "counts": _todo_counts(plan.items),
                "items": [
                    {
                        key: value
                        for key, value in item.items()
                        if key in {"index", "status", "content", "activeForm"}
                    }
                    for item in plan.items
                ],
            }
        )
    return plans


def _todo_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "pending": sum(1 for item in items if item.get("status") == "pending"),
        "in_progress": sum(
            1 for item in items if item.get("status") == "in_progress"
        ),
        "completed": sum(1 for item in items if item.get("status") == "completed"),
    }


def _load_context_snapshots(
    connection: sqlite3.Connection, session_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT context_snapshot_id, session_id, run_id, trigger,
               source_checkpoint_id, active_skill_records_json, summary,
               retained_messages_json, omitted_tool_result_count,
               evicted_message_count, evicted_model_call_group_count,
               artifact_refs_json, token_estimate_json, payload_artifact_id,
               created_at, version
        FROM context_snapshots
        WHERE session_id = ?
        ORDER BY created_at ASC, rowid ASC
        """,
        (session_id,),
    ).fetchall()
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        snapshots.append(
            {
                "context_snapshot_id": row[0],
                "session_id": row[1],
                "run_id": row[2],
                "trigger": row[3],
                "source_checkpoint_id": row[4],
                "active_skill_records": json.loads(row[5]),
                "summary": row[6],
                "retained_messages": json.loads(row[7]),
                "omitted_tool_result_count": row[8],
                "evicted_message_count": row[9],
                "evicted_model_call_group_count": row[10],
                "artifact_refs": json.loads(row[11]),
                "token_estimate": json.loads(row[12]),
                "payload_artifact_id": row[13],
                "created_at": row[14],
                "version": row[15],
            }
        )
    return snapshots


def _load_approval_grants(
    connection: sqlite3.Connection, session_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT grant_id, session_id, run_id, tool_name, risk_level,
               scope_signature, decision, grant_scope, approval_request,
               created_at, version
        FROM approval_grants
        WHERE session_id = ?
        ORDER BY created_at ASC, rowid ASC
        """,
        (session_id,),
    ).fetchall()
    return [
        {
            "grant_id": row[0],
            "session_id": row[1],
            "run_id": row[2],
            "tool_name": row[3],
            "risk_level": row[4],
            "scope_signature": row[5],
            "decision": row[6],
            "grant_scope": row[7],
            "approval_request": row[8],
            "created_at": row[9],
            "version": row[10],
        }
        for row in rows
    ]


def _format_active_skill(record: dict[str, Any]) -> str:
    return (
        f"{record.get('name', '')}@{record.get('content_hash', '')}"
        f":{record.get('activation_reason', '')}:{record.get('scope', '')}"
    )


def _shorten(text: str) -> str:
    if len(text) <= 160:
        return text
    return text[:157] + "..."

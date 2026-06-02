from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from debug_agent.runtime.contracts import RunEvent, utc_now_iso


@dataclass(frozen=True)
class EngineLogWriter:
    path: Path

    def write(
        self,
        *,
        timestamp: str,
        session_id: str,
        run_id: str | None,
        step_id: str | None,
        level: str,
        event: str,
        message: str,
        metadata: dict[str, Any],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": timestamp,
            "session_id": session_id,
            "run_id": run_id,
            "step_id": step_id,
            "level": level,
            "event": event,
            "message": message,
            "metadata": metadata,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def write_event_log(sessions_root: Path, event: RunEvent) -> None:
    EngineLogWriter(_log_path(sessions_root, event.session_id)).write(
        timestamp=event.timestamp,
        session_id=event.session_id,
        run_id=event.run_id,
        step_id=event.step_id,
        level=_level_for_event(event.kind),
        event=event.kind,
        message=_message_for_event(event.kind, event.payload),
        metadata={"payload": event.payload, "event_id": event.event_id},
    )


def write_runtime_log(
    sessions_root: Path,
    *,
    session_id: str,
    run_id: str | None,
    level: str,
    event: str,
    message: str,
    metadata: dict[str, Any],
) -> None:
    EngineLogWriter(_log_path(sessions_root, session_id)).write(
        timestamp=utc_now_iso(),
        session_id=session_id,
        run_id=run_id,
        step_id=None,
        level=level,
        event=event,
        message=message,
        metadata=metadata,
    )


def _log_path(sessions_root: Path, session_id: str) -> Path:
    return Path(sessions_root).resolve() / session_id / "logs" / "engine.log"


def _level_for_event(kind: str) -> str:
    if kind.endswith("_failed"):
        return "ERROR"
    if kind in {"tool_call_denied", "context_limit_exceeded"}:
        return "WARN"
    return "INFO"


def _message_for_event(kind: str, payload: dict[str, Any]) -> str:
    if kind in {
        "skill_snapshot_created",
        "skill_activated",
        "skill_resource_loaded",
    }:
        return skill_log_message(kind, payload)
    if kind in {
        "approval_requested",
        "approval_decision_recorded",
        "approval_mode_changed",
    }:
        return approval_log_message(kind, payload)
    if kind == "tool_call_denied":
        return policy_log_message(kind, payload)
    if kind in {"context_optimized", "compression_failed", "context_limit_exceeded"}:
        return context_log_message(kind, payload)
    if kind == "artifact_registered":
        return artifact_log_message(kind, payload)
    return kind


def skill_log_message(kind: str, payload: dict[str, Any]) -> str:
    if kind == "skill_snapshot_created":
        return (
            "skill_snapshot_created "
            f"skill={payload.get('skill_name', '')} "
            f"hash={payload.get('content_hash', '')}"
        )
    if kind == "skill_activated":
        return (
            "skill_activated "
            f"skill={payload.get('skill_name', '')} "
            f"hash={payload.get('content_hash', '')}"
        )
    if kind == "skill_resource_loaded":
        return (
            "skill_resource_loaded "
            f"skill={payload.get('skill_name', '')} "
            f"resource={payload.get('resource_path', '')} "
            f"kind={payload.get('resource_kind', '')}"
        )
    return kind


def approval_log_message(kind: str, payload: dict[str, Any]) -> str:
    if kind == "approval_requested":
        return (
            "approval_requested "
            f"tool={payload.get('tool_name', '')} "
            f"target={payload.get('target', '')}"
        )
    if kind == "approval_decision_recorded":
        return (
            "approval_decision_recorded "
            f"tool={payload.get('tool_name', '')} "
            f"decision={payload.get('decision', '')} "
            f"scope={payload.get('grant_scope', '')}"
        )
    if kind == "approval_mode_changed":
        return (
            "approval_mode_changed "
            f"{payload.get('old_mode', '')}->{payload.get('new_mode', '')}"
        )
    return kind


def policy_log_message(kind: str, payload: dict[str, Any]) -> str:
    result = payload.get("result", {})
    error = result.get("error", {}) if isinstance(result, dict) else {}
    error_class = error.get("error_class", "") if isinstance(error, dict) else ""
    message = error.get("message", "") if isinstance(error, dict) else ""
    return (
        f"{kind} tool={payload.get('tool_name', '')} "
        f"error_class={error_class} message={message}"
    )


def context_log_message(kind: str, payload: dict[str, Any]) -> str:
    if kind == "context_optimized":
        return (
            "context_optimized "
            f"trigger={payload.get('trigger', '')} "
            f"snapshot={payload.get('context_snapshot_id', '')} "
            f"tokens={payload.get('reduced_from_tokens', '')}->{payload.get('reduced_to_tokens', '')}"
        )
    if kind == "compression_failed":
        return (
            "compression_failed "
            f"reason={payload.get('reason', '')} "
            f"message={payload.get('message', '')}"
        )
    if kind == "context_limit_exceeded":
        return (
            "context_limit_exceeded "
            f"estimated={payload.get('estimated_tokens', '')} "
            f"window={payload.get('window_tokens', '')}"
        )
    return kind


def artifact_log_message(kind: str, payload: dict[str, Any]) -> str:
    return (
        f"{kind} artifact={payload.get('artifact_id', '')} "
        f"type={payload.get('artifact_type', '')} "
        f"path={payload.get('relative_path', '')}"
    )

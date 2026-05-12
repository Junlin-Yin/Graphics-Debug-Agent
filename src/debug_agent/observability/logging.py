from __future__ import annotations

import json
import sqlite3
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
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def write_event_log(connection: sqlite3.Connection, event: RunEvent) -> None:
    EngineLogWriter(_log_path(connection, event.session_id)).write(
        timestamp=event.timestamp,
        session_id=event.session_id,
        run_id=event.run_id,
        step_id=event.step_id,
        level=_level_for_event(event.kind),
        event=event.kind,
        message=event.kind,
        metadata={"payload": event.payload, "event_id": event.event_id},
    )


def write_runtime_log(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    run_id: str | None,
    level: str,
    event: str,
    message: str,
    metadata: dict[str, Any],
) -> None:
    EngineLogWriter(_log_path(connection, session_id)).write(
        timestamp=utc_now_iso(),
        session_id=session_id,
        run_id=run_id,
        step_id=None,
        level=level,
        event=event,
        message=message,
        metadata=metadata,
    )


def _log_path(connection: sqlite3.Connection, session_id: str) -> Path:
    row = connection.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise RuntimeError("Runtime database path is unavailable.")
    return Path(row[2]).resolve().parent / session_id / "logs" / "engine.log"


def _level_for_event(kind: str) -> str:
    if kind.endswith("_failed") or kind == "tool_call_failed":
        return "ERROR"
    if kind == "tool_call_denied":
        return "WARN"
    return "INFO"

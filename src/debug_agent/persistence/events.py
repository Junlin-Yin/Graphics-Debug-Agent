from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from debug_agent.observability.logging import write_event_log
from debug_agent.runtime.contracts import RunEvent


@dataclass(frozen=True)
class EventWriter:
    connection: sqlite3.Connection
    sessions_root: Path

    def append(self, event: RunEvent) -> RunEvent:
        self.append_in_transaction(event)
        self.connection.commit()
        write_event_log(self.sessions_root, event)
        return event

    def append_in_transaction(self, event: RunEvent) -> RunEvent:
        self.connection.execute(
            """
            INSERT INTO run_events (
                event_id, timestamp, session_id, run_id, step_id, kind,
                payload_json, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.timestamp,
                event.session_id,
                event.run_id,
                event.step_id,
                event.kind,
                json.dumps(event.payload, sort_keys=True),
                event.version,
            ),
        )
        return event

    def list_for_session(self, session_id: str) -> list[RunEvent]:
        rows = self.connection.execute(
            """
            SELECT event_id, timestamp, session_id, run_id, step_id, kind,
                   payload_json, version
            FROM run_events
            WHERE session_id = ?
            ORDER BY timestamp ASC, rowid ASC
            """,
            (session_id,),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_for_run(self, run_id: str) -> list[RunEvent]:
        rows = self.connection.execute(
            """
            SELECT event_id, timestamp, session_id, run_id, step_id, kind,
                   payload_json, version
            FROM run_events
            WHERE run_id = ?
            ORDER BY timestamp ASC, rowid ASC
            """,
            (run_id,),
        ).fetchall()
        return [_event_from_row(row) for row in rows]


def _event_from_row(row: tuple) -> RunEvent:
    return RunEvent(
        event_id=row[0],
        timestamp=row[1],
        session_id=row[2],
        run_id=row[3],
        step_id=row[4],
        kind=row[5],
        payload=json.loads(row[6]),
        version=row[7],
    )

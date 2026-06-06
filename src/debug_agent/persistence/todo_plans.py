from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.conversation import canonical_json_bytes, sha256_hex
from debug_agent.runtime.contracts import RunEvent, utc_now_iso


STATUS_ORDER = ("pending", "in_progress", "completed")


@dataclass(frozen=True)
class TodoPlan:
    run_id: str
    version: int
    items: list[dict[str, Any]]
    updated_at: str | None
    is_empty: bool


@dataclass(frozen=True)
class TodoPlanReplacement:
    previous_plan_version: int
    plan: TodoPlan
    event: RunEvent


@dataclass(frozen=True)
class TodoPlanStore:
    connection: sqlite3.Connection

    def get_current(self, run_id: str) -> TodoPlan:
        row = self.connection.execute(
            """
            SELECT plan_version, items_json, updated_at
            FROM todo_plans
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return TodoPlan(
                run_id=run_id,
                version=0,
                items=[],
                updated_at=None,
                is_empty=True,
            )
        items = json.loads(row[1])
        return TodoPlan(
            run_id=run_id,
            version=row[0],
            items=items,
            updated_at=row[2],
            is_empty=len(items) == 0,
        )

    def replace_plan(
        self,
        session_id: str,
        run_id: str,
        items: list[dict[str, Any]],
        event_writer: EventWriter,
    ) -> TodoPlanReplacement:
        previous = self.get_current(run_id)
        next_version = previous.version + 1
        normalized_items = _indexed_items(items)
        now = utc_now_iso()
        payload_items = _event_items(normalized_items)
        payload = {
            "previous_plan_version": previous.version,
            "plan_version": next_version,
            "item_count": len(normalized_items),
            "counts": _status_counts(normalized_items),
            "items": payload_items,
        }
        event = RunEvent(
            event_id=f"evt_{uuid4().hex}",
            timestamp=now,
            session_id=session_id,
            run_id=run_id,
            step_id=None,
            kind="todo_updated",
            payload=payload,
        )
        items_json = json.dumps(
            normalized_items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.connection:
            created_at = self.connection.execute(
                "SELECT created_at FROM todo_plans WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self.connection.execute(
                """
                INSERT INTO todo_plans (
                    run_id, session_id, plan_version, items_json, created_at,
                    updated_at, version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    plan_version = excluded.plan_version,
                    items_json = excluded.items_json,
                    updated_at = excluded.updated_at,
                    version = excluded.version
                """,
                (
                    run_id,
                    session_id,
                    next_version,
                    items_json,
                    created_at[0] if created_at is not None else now,
                    now,
                    event.version,
                ),
            )
            event_writer.append_in_transaction(event)
        return TodoPlanReplacement(
            previous_plan_version=previous.version,
            plan=TodoPlan(
                run_id=run_id,
                version=next_version,
                items=normalized_items,
                updated_at=now,
                is_empty=len(normalized_items) == 0,
            ),
            event=event,
        )

    def checkpoint_snapshot(self, run_id: str) -> dict[str, Any]:
        plan = self.get_current(run_id)
        payload = {
            "run_id": run_id,
            "plan_version": plan.version,
            "items": plan.items,
        }
        return {
            "plan_version": plan.version,
            "items": plan.items,
            "checksum": sha256_hex(canonical_json_bytes(payload)),
        }

    def validate_checkpoint_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> None:
        expected = self.checkpoint_snapshot(run_id)
        if snapshot != expected:
            raise ValueError("Todo Plan checkpoint checksum is invalid.")

    def validate_checkpoint_snapshot_payload(
        self, run_id: str, snapshot: dict[str, Any]
    ) -> None:
        if not isinstance(snapshot, dict):
            raise ValueError("Todo Plan checkpoint snapshot is invalid.")
        payload = {
            "run_id": run_id,
            "plan_version": snapshot.get("plan_version"),
            "items": snapshot.get("items"),
        }
        expected_checksum = sha256_hex(canonical_json_bytes(payload))
        if snapshot.get("checksum") != expected_checksum:
            raise ValueError("Todo Plan checkpoint checksum is invalid.")

    def restore_checkpoint_snapshot(
        self,
        *,
        session_id: str,
        run_id: str,
        snapshot: dict[str, Any],
    ) -> TodoPlan:
        self.validate_checkpoint_snapshot_payload(run_id, snapshot)
        plan_version = snapshot["plan_version"]
        items = snapshot["items"]
        now = utc_now_iso()
        items_json = json.dumps(
            items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        created_at = self.connection.execute(
            "SELECT created_at FROM todo_plans WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        self.connection.execute(
            """
            INSERT INTO todo_plans (
                run_id, session_id, plan_version, items_json, created_at,
                updated_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(run_id) DO UPDATE SET
                session_id = excluded.session_id,
                plan_version = excluded.plan_version,
                items_json = excluded.items_json,
                updated_at = excluded.updated_at,
                version = excluded.version
            """,
            (
                run_id,
                session_id,
                plan_version,
                items_json,
                created_at[0] if created_at is not None else now,
                now,
            ),
        )
        return TodoPlan(
            run_id=run_id,
            version=plan_version,
            items=list(items),
            updated_at=now,
            is_empty=len(items) == 0,
        )


def _indexed_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        normalized = {
            "index": index,
            "content": item["content"],
            "status": item["status"],
            "metadata": dict(item.get("metadata") or {}),
        }
        if item.get("status") == "in_progress" and item.get("activeForm") is not None:
            normalized["activeForm"] = item["activeForm"]
        indexed.append(normalized)
    return indexed


def _event_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload_items: list[dict[str, Any]] = []
    for item in items:
        payload_item = {
            "index": item["index"],
            "content": item["content"],
            "status": item["status"],
        }
        if "activeForm" in item:
            payload_item["activeForm"] = item["activeForm"]
        payload_items.append(payload_item)
    return payload_items


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    return {status: sum(1 for item in items if item["status"] == status) for status in STATUS_ORDER}

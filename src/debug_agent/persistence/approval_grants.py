from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from debug_agent.runtime.contracts import utc_now_iso


@dataclass(frozen=True)
class ApprovalGrant:
    grant_id: str
    session_id: str
    run_id: str
    tool_name: str
    risk_level: str
    scope_signature: str
    decision: str
    grant_scope: str
    approval_request: str
    created_at: str
    version: int


@dataclass(frozen=True)
class ApprovalGrantStore:
    connection: sqlite3.Connection

    def record(
        self,
        *,
        grant_id: str,
        session_id: str,
        run_id: str,
        tool_name: str,
        risk_level: str,
        scope_signature: str,
        decision: str,
        grant_scope: str,
        approval_request: str,
    ) -> ApprovalGrant:
        created_at = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO approval_grants (
                grant_id, session_id, run_id, tool_name, risk_level,
                scope_signature, decision, grant_scope, approval_request,
                created_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grant_id,
                session_id,
                run_id,
                tool_name,
                risk_level,
                scope_signature,
                decision,
                grant_scope,
                approval_request,
                created_at,
                1,
            ),
        )
        self.connection.commit()
        return ApprovalGrant(
            grant_id=grant_id,
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            risk_level=risk_level,
            scope_signature=scope_signature,
            decision=decision,
            grant_scope=grant_scope,
            approval_request=approval_request,
            created_at=created_at,
            version=1,
        )

    def find_reusable(
        self,
        *,
        session_id: str,
        tool_name: str,
        risk_level: str,
        scope_signature: str,
    ) -> ApprovalGrant | None:
        row = self.connection.execute(
            """
            SELECT grant_id, session_id, run_id, tool_name, risk_level,
                   scope_signature, decision, grant_scope, approval_request,
                   created_at, version
            FROM approval_grants
            WHERE session_id = ?
              AND tool_name = ?
              AND risk_level = ?
              AND scope_signature = ?
              AND decision = 'approved_for_session'
              AND grant_scope = 'session'
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (session_id, tool_name, risk_level, scope_signature),
        ).fetchone()
        return None if row is None else ApprovalGrant(*row)

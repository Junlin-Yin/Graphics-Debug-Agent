from __future__ import annotations

import sqlite3

import pytest

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.sqlite import RuntimeDatabase


def test_approval_grant_store_reuses_only_session_grants(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    store = ApprovalGrantStore(db.connection)

    store.record(
        grant_id="grant_once",
        session_id="sess_1",
        run_id="run_1",
        tool_name="write_file",
        risk_level="write",
        scope_signature="write:/repo/file.txt",
        decision="approved_once",
        grant_scope="once",
        approval_request="Write /repo/file.txt?",
    )
    store.record(
        grant_id="grant_session",
        session_id="sess_1",
        run_id="run_1",
        tool_name="write_file",
        risk_level="write",
        scope_signature="write:/repo/other.txt",
        decision="approved_for_session",
        grant_scope="session",
        approval_request="Write /repo/other.txt?",
    )

    assert (
        store.find_reusable(
            session_id="sess_1",
            tool_name="write_file",
            risk_level="write",
            scope_signature="write:/repo/file.txt",
        )
        is None
    )
    grant = store.find_reusable(
        session_id="sess_1",
        tool_name="write_file",
        risk_level="write",
        scope_signature="write:/repo/other.txt",
    )
    assert grant is not None
    assert grant.grant_id == "grant_session"
    assert grant.approval_request == "Write /repo/other.txt?"
    db.close()


def test_approval_grants_enforce_decision_and_scope_constraints(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)

    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO approval_grants (
                grant_id, session_id, run_id, tool_name, risk_level,
                scope_signature, decision, grant_scope, approval_request,
                created_at, version
            )
            VALUES (
                'grant_bad', 'sess_1', 'run_1', 'read_file', 'read',
                'read:/repo/file.txt', 'approved_forever', 'session',
                'Read?', '2026-05-25T00:00:00Z', 1
            )
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO approval_grants (
                grant_id, session_id, run_id, tool_name, risk_level,
                scope_signature, decision, grant_scope, approval_request,
                created_at, version
            )
            VALUES (
                'grant_bad_scope', 'sess_1', 'run_1', 'read_file', 'read',
                'read:/repo/file.txt', 'approved_for_session', 'forever',
                'Read?', '2026-05-25T00:00:00Z', 1
            )
            """
        )
    db.close()

from __future__ import annotations

import json
import sqlite3
import hashlib

from debug_agent.cli.exit_codes import ERROR_ACTIVE_SESSION_CONFLICT
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime import orchestrator as orchestrator_module
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


def _config() -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": "new answer",
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": "system",
    }


def _active_owner(workspace, *, owner_token: str = "owner_old", owner_pid: int = 98765):
    db = RuntimeDatabase.bootstrap_phase_3_5_internal(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="normal",
        config_snapshot=_config(),
        session_id="sess_old",
        require_fresh_phase_3_5_paths=True,
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_old")
    sessions.set_active_run(session.session_id, run.run_id)
    sessions.record_owner(
        session_id=session.session_id,
        owner_pid=owner_pid,
        owner_host_id="host-v1:sha256(test-host)",
        owner_token=owner_token,
    )
    db.close()
    return session.session_id, run.run_id


class _HostIdentity:
    def current_host_id(self) -> str:
        return "host-v1:sha256(test-host)"


class _DeadProcess:
    def pid_exists(self, _pid: int) -> bool:
        return False


class _LiveProcess:
    def pid_exists(self, _pid: int) -> bool:
        return True


class _UnavailableHostIdentity:
    def current_host_id(self) -> None:
        return None


class _UnreliableProcess:
    def pid_exists(self, _pid: int) -> bool:
        raise OSError("cannot inspect process")


def test_confirmed_stale_startup_fail_closes_old_owner_and_creates_new_session(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_session_id, old_run_id = _active_owner(workspace)
    monkeypatch.setattr(
        orchestrator_module,
        "_current_owner_facts",
        lambda: {
            "pid": 111,
            "host_id": "host-v1:sha256(test-host)",
            "owner_token": "owner_new",
        },
    )

    confirmation_requests = []

    def confirm(request: dict) -> bool:
        confirmation_requests.append(request)
        return True

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=confirm,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == 0
    assert result.session_id != old_session_id
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        old_session = conn.execute(
            """
            SELECT status, active_run_id, latest_checkpoint_id, terminal_reason,
                   terminal_error_json, owner_pid, owner_host_id, owner_token
            FROM sessions
            WHERE session_id = ?
            """,
            (old_session_id,),
        ).fetchone()
        old_run = conn.execute(
            """
            SELECT status, latest_checkpoint_id, terminal_reason, terminal_error_json
            FROM runs
            WHERE run_id = ?
            """,
            (old_run_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT kind, payload_json FROM run_events WHERE run_id = ? ORDER BY rowid",
            (old_run_id,),
        ).fetchall()
        conversation_count = conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE run_id = ?",
            (old_run_id,),
        ).fetchone()[0]

    assert old_session == (
        "failed",
        None,
        None,
        "terminal_stale",
        None,
        None,
        None,
        None,
    )
    assert old_run == ("failed", None, "terminal_stale", None)
    assert events == [
        (
            "stale_fail_closed",
            json.dumps(
                {
                    "stale_proof_summary": {
                        "host_match": True,
                        "pid_absent": True,
                        "token_fenced": True,
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    ]
    assert conversation_count == 0
    assert confirmation_requests == [
        {
            "session_id": old_session_id,
            "run_id": old_run_id,
            "evidence": {
                "host_match": True,
                "pid_absent": True,
                "owner_token_present": True,
            },
            "message": (
                "The active owner appears stale on this host. Confirm fail-close of "
                "the old session before continuing?"
            ),
        }
    ]


def test_stale_startup_live_pid_fails_closed_without_confirmation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_session_id, _old_run_id = _active_owner(workspace)
    confirmation_calls = 0

    def confirm(_request) -> bool:
        nonlocal confirmation_calls
        confirmation_calls += 1
        return True

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=confirm,
        host_identity_provider=_HostIdentity(),
        process_liveness=_LiveProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert result.error["reason"] == "workspace_owner_not_proven_stale"
    assert confirmation_calls == 0
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        row = conn.execute(
            "SELECT status, owner_token FROM sessions WHERE session_id = ?",
            (old_session_id,),
        ).fetchone()
    assert row == ("running", "owner_old")


def test_stale_startup_without_confirmation_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_session_id, _old_run_id = _active_owner(workspace)

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=None,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert result.error["reason"] == "workspace_owner_confirmation_unavailable"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        row = conn.execute(
            "SELECT status, owner_token FROM sessions WHERE session_id = ?",
            (old_session_id,),
        ).fetchone()
    assert row == ("running", "owner_old")


def test_stale_startup_missing_token_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_session_id, _old_run_id = _active_owner(workspace, owner_token="owner_old")
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        conn.execute(
            "UPDATE sessions SET owner_token = NULL WHERE session_id = ?",
            (old_session_id,),
        )
        conn.commit()

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert result.error["reason"] == "workspace_owner_not_proven_stale"


def test_stale_startup_unavailable_host_fails_closed_without_confirmation(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _active_owner(workspace)
    confirmation_calls = 0

    def confirm(_request) -> bool:
        nonlocal confirmation_calls
        confirmation_calls += 1
        return True

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=confirm,
        host_identity_provider=_UnavailableHostIdentity(),
        process_liveness=_DeadProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert result.error["reason"] == "workspace_owner_not_proven_stale"
    assert confirmation_calls == 0


def test_stale_startup_unreliable_pid_check_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _active_owner(workspace)

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_UnreliableProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert result.error["reason"] == "workspace_owner_not_proven_stale"


def test_stale_startup_host_mismatch_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _active_owner(workspace)

    class OtherHostIdentity:
        def current_host_id(self) -> str:
            return "host-v1:sha256(other-host)"

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=OtherHostIdentity(),
        process_liveness=_DeadProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert result.error["reason"] == "workspace_owner_not_proven_stale"


def test_stale_startup_missing_pid_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_session_id, _old_run_id = _active_owner(workspace)
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        conn.execute(
            "UPDATE sessions SET owner_pid = NULL WHERE session_id = ?",
            (old_session_id,),
        )
        conn.commit()

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).run_one_shot("hello", _config())

    assert result.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert result.error["reason"] == "workspace_owner_not_proven_stale"


def test_host_identity_hashes_platform_machine_id(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_module, "_platform_machine_id", lambda: "machine-1")

    host_id = orchestrator_module._HostIdentityProvider().current_host_id()

    expected = hashlib.sha256(b"machine-1").hexdigest()
    assert host_id == f"host-v1:sha256({expected})"


def test_confirmed_stale_startup_writes_terminal_checkpoint_when_eligible(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        orchestrator_module,
        "_current_owner_facts",
        lambda: {
            "pid": 222,
            "host_id": "host-v1:sha256(test-host)",
            "owner_token": "owner_old",
        },
    )
    started = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).start_repl(_config())
    assert started.runtime is not None
    old_session_id = started.runtime.session_id
    old_run_id = started.runtime.run_id
    result = started.runtime.run_turn("hello")
    assert result.status == "completed"
    started.runtime.close()
    monkeypatch.setattr(
        orchestrator_module,
        "_current_owner_facts",
        lambda: {
            "pid": 333,
            "host_id": "host-v1:sha256(test-host)",
            "owner_token": "owner_new",
        },
    )

    result = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).run_one_shot("new prompt", _config())

    assert result.exit_code == 0
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        old_session = conn.execute(
            "SELECT status, latest_checkpoint_id, terminal_reason FROM sessions WHERE session_id = ?",
            (old_session_id,),
        ).fetchone()
        old_run = conn.execute(
            "SELECT status, latest_checkpoint_id, terminal_reason FROM runs WHERE run_id = ?",
            (old_run_id,),
        ).fetchone()
        checkpoint = conn.execute(
            "SELECT checkpoint_id, kind, state_json FROM checkpoints WHERE session_id = ?",
            (old_session_id,),
        ).fetchone()

    assert old_session[0] == "failed"
    assert old_session[1] is not None
    assert old_session[2] == "terminal_stale"
    assert old_run == ("failed", old_session[1], "terminal_stale")
    assert checkpoint[0] == old_session[1]
    assert checkpoint[1] == "terminal_recovery"
    assert json.loads(checkpoint[2])["terminal_reason"] == "terminal_stale"


def test_resume_target_stale_owner_fail_closes_then_resumes_same_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        orchestrator_module,
        "_current_owner_facts",
        lambda: {
            "pid": 222,
            "host_id": "host-v1:sha256(test-host)",
            "owner_token": "owner_old",
        },
    )
    started = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).start_repl(_config())
    assert started.runtime is not None
    old_session_id = started.runtime.session_id
    result = started.runtime.run_turn("hello")
    assert result.status == "completed"
    started.runtime.close()
    monkeypatch.setattr(
        orchestrator_module,
        "_current_owner_facts",
        lambda: {
            "pid": 333,
            "host_id": "host-v1:sha256(test-host)",
            "owner_token": "owner_new",
        },
    )

    resume = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).resume(old_session_id)

    assert resume.exit_code == 0
    assert resume.session_id == old_session_id
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session = conn.execute(
            """
            SELECT status, active_run_id, owner_pid, owner_host_id, owner_token
            FROM sessions
            WHERE session_id = ?
            """,
            (old_session_id,),
        ).fetchone()
        event_kinds = [
            row[0]
            for row in conn.execute(
                "SELECT kind FROM run_events WHERE session_id = ? ORDER BY rowid",
                (old_session_id,),
            )
        ]

    assert session[0] == "running"
    assert session[1] is not None
    assert session[2:] == (333, "host-v1:sha256(test-host)", "owner_new")
    assert "stale_fail_closed" in event_kinds
    assert event_kinds[-2:] == ["session_resumed", "run_resumed"]


def test_resume_blocked_by_different_stale_owner_fail_closes_then_resumes_target(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    completed = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "done",
        _config(),
    )
    assert completed.exit_code == 0
    active_session_id, _active_run_id = _active_owner(
        workspace,
        owner_token="owner_blocking",
        owner_pid=55555,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_current_owner_facts",
        lambda: {
            "pid": 444,
            "host_id": "host-v1:sha256(test-host)",
            "owner_token": "owner_resumed",
        },
    )

    resume = RuntimeOrchestrator(
        workspace_root=workspace,
        stale_confirmation=lambda _request: True,
        host_identity_provider=_HostIdentity(),
        process_liveness=_DeadProcess(),
    ).resume(completed.session_id)

    assert resume.exit_code == 0
    assert resume.session_id == completed.session_id
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        blocking = conn.execute(
            "SELECT status, terminal_reason, owner_token FROM sessions WHERE session_id = ?",
            (active_session_id,),
        ).fetchone()
        target = conn.execute(
            "SELECT status, owner_token FROM sessions WHERE session_id = ?",
            (completed.session_id,),
        ).fetchone()
    assert blocking == ("failed", "terminal_stale", None)
    assert target == ("running", "owner_resumed")

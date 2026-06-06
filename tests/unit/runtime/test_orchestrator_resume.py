from __future__ import annotations

import json
import sqlite3

from debug_agent.cli.exit_codes import (
    ERROR_ACTIVE_SESSION_CONFLICT,
    ERROR_EXECUTION_FAILED,
    ERROR_PERSISTENCE_READ,
)
from debug_agent.adapters.model_factory import ModelFactoryResult
from debug_agent.persistence.errors import StoreError
from debug_agent.runtime import orchestrator as orchestrator_module
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


def _config(response: str = "fake answer") -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": response,
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer concisely "
            "and use only tools exposed by the runtime."
        ),
    }


def test_resume_revives_one_shot_same_lineage_without_conversation_append(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    assert one_shot.exit_code == 0

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == 0
    assert resume.session_id == one_shot.session_id
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        sessions = conn.execute(
            """
            SELECT session_id, status, active_run_id, latest_checkpoint_id
            FROM sessions
            """
        ).fetchall()
        runs = conn.execute(
            """
            SELECT run_id, session_id, status, latest_checkpoint_id
            FROM runs
            """
        ).fetchall()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_ids = [
            row[0] for row in conn.execute("SELECT checkpoint_id FROM checkpoints")
        ]
        durable_rows = conn.execute(
            "SELECT role, kind, content_json FROM conversation_messages ORDER BY message_index"
        ).fetchall()
        checkpoint_id = checkpoint_ids[0]

    assert sessions == [(one_shot.session_id, "running", one_shot.run_id, checkpoint_id)]
    assert runs == [(one_shot.run_id, one_shot.session_id, "running", checkpoint_id)]
    assert event_kinds[-2:] == ["session_resumed", "run_resumed"]
    assert len(runs) == 1
    assert len(checkpoint_ids) == 1
    assert [(row[0], row[1], json.loads(row[2])) for row in durable_rows] == [
        ("user", "user_input", {"content": "hello"}),
        ("assistant", "assistant_output", {"content": "one shot answer"}),
    ]


def test_resume_rejects_non_terminal_target(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert controller.runtime is not None
    session_id = controller.runtime.session_id
    try:
        resume = RuntimeOrchestrator(workspace_root=workspace).resume(session_id)
    finally:
        controller.runtime.close()

    assert resume.exit_code == ERROR_EXECUTION_FAILED
    assert resume.error["error_class"] == "runtime_error"
    assert resume.error["reason"] == "resume_not_eligible"


def test_resume_rejects_missing_checkpoint(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        conn.execute("UPDATE sessions SET latest_checkpoint_id = NULL")
        conn.commit()

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == ERROR_EXECUTION_FAILED
    assert resume.error["error_class"] == "runtime_error"
    assert resume.error["reason"] == "resume_checkpoint_required"


def test_resume_rejects_invalid_checkpoint_payload(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        state = json.loads(conn.execute("SELECT state_json FROM checkpoints").fetchone()[0])
        state["terminal_status"] = "failed"
        conn.execute(
            "UPDATE checkpoints SET state_json = ?",
            (json.dumps(state, ensure_ascii=False, sort_keys=True),),
        )
        conn.commit()

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == ERROR_PERSISTENCE_READ
    assert resume.error["error_class"] == "persistence_error"
    assert resume.error["reason"] == "checkpoint_invalid"


def test_resume_rejects_active_ownership_conflict(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    active = RuntimeOrchestrator(workspace_root=other_workspace).start_repl(_config())
    assert active.runtime is not None
    try:
        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            active_run_id = active.runtime.run_id
            active_session_id = active.runtime.session_id
            # Insert a conflicting active owner directly into the target DB.
            config_json = json.dumps(_config(), ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace_root, status, approval_mode, active_run_id,
                    artifact_root, config_snapshot_json, latest_checkpoint_id,
                    created_at, updated_at, error_summary, terminal_reason,
                    terminal_error_json, non_resumable_startup_failure, version
                )
                VALUES (?, ?, 'running', 'normal', ?, ?, ?, NULL,
                        '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z',
                        NULL, NULL, NULL, 0, 1)
                """,
                (
                    active_session_id,
                    str(workspace.resolve()),
                    active_run_id,
                    str(workspace / ".sessions" / active_session_id / "artifacts"),
                    config_json,
                ),
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, session_id, parent_run_id, run_type, status,
                    active_skills_json, latest_checkpoint_id, context_snapshot_id,
                    created_at, updated_at, error_summary, terminal_reason,
                    terminal_error_json, non_resumable_startup_failure, version
                )
                VALUES (?, ?, NULL, 'prompt', 'running', '[]', NULL, NULL,
                        '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z',
                        NULL, NULL, NULL, 0, 1)
                """,
                (active_run_id, active_session_id),
            )
            conn.commit()

        resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)
    finally:
        active.runtime.close()

    assert resume.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert resume.error["error_class"] == "policy_error"
    assert resume.error["reason"] == "workspace_owner_active"


def test_resume_restores_drifted_current_todo_without_update_event(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "make a plan",
        _config("planned"),
    )
    assert one_shot.exit_code == 0
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        conn.execute(
            """
            INSERT INTO todo_plans (
                run_id, session_id, plan_version, items_json, created_at,
                updated_at, version
            )
            VALUES (?, ?, 99,
                    '[{"index":1,"content":"drift","status":"pending","metadata":{}}]',
                    '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z', 1)
            """,
            (one_shot.run_id, one_shot.session_id),
        )
        before_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE kind = 'todo_updated'"
        ).fetchone()[0]
        conn.commit()

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == 0
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        plan_version, items_json = conn.execute(
            "SELECT plan_version, items_json FROM todo_plans WHERE run_id = ?",
            (one_shot.run_id,),
        ).fetchone()
        after_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE kind = 'todo_updated'"
        ).fetchone()[0]

    assert plan_version == 0
    assert json.loads(items_json) == []
    assert after_events == before_events


def test_resume_preserves_approval_grants_active_skills_and_frozen_tools(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\nUse alpha.\n",
        encoding="utf-8",
    )
    config = _config("skill activated")
    config["fake_tool_calls"] = [
        {"name": "activate_skill", "args": {"name": "alpha"}, "id": "call_alpha"}
    ]
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "activate alpha",
        config,
        approval_mode="semi-auto",
    )
    assert one_shot.exit_code == 0
    resumed = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        one_shot.session_id
    )
    assert resumed.runtime is not None
    try:
        session = resumed.runtime.sessions.get(one_shot.session_id)
        run = resumed.runtime.runs.get(one_shot.run_id)
        approval_state = json.loads(
            resumed.runtime.db.connection.execute(
                "SELECT state_json FROM checkpoints WHERE checkpoint_id = ?",
                (run.latest_checkpoint_id,),
            ).fetchone()[0]
        )["approval_state"]
        tools = resumed.runtime.tool_lines()
        skills = resumed.runtime.skill_lines()
    finally:
        resumed.runtime.close()

    assert session.approval_mode == "semi-auto"
    assert approval_state["approval_mode"] == "semi-auto"
    assert approval_state["grant_count"] == 0
    assert run.active_skills[0]["name"] == "alpha"
    assert any("alpha" in line for line in skills)
    assert any("shell_exec" in line for line in tools)
    assert any("view_image" in line and "disabled" in line for line in tools)


def test_start_resumed_repl_runtime_construction_failure_does_not_revive(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    assert one_shot.exit_code == 0

    class FailingModelFactory:
        def create(self, _config_snapshot):
            return ModelFactoryResult(
                model=None,
                error={
                    "schema_version": 1,
                    "error_class": "config_error",
                    "reason": "provider_config_invalid",
                    "message": "model construction failed",
                    "scope": "startup",
                    "recoverability": "terminal_non_resumable",
                    "metadata": {},
                    "artifact_ids": [],
                },
            )

    monkeypatch.setattr(orchestrator_module, "ModelFactory", FailingModelFactory)

    result = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        one_shot.session_id
    )

    assert result.runtime is None
    assert result.error is not None
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_row = conn.execute(
            """
            SELECT status, active_run_id, owner_pid, owner_host_id, owner_token
            FROM sessions
            WHERE session_id = ?
            """,
            (one_shot.session_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?",
            (one_shot.run_id,),
        ).fetchone()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]

    assert session_row == ("completed", None, None, None, None)
    assert run_row == ("completed",)
    assert "session_resumed" not in event_kinds
    assert "run_resumed" not in event_kinds


def test_start_resumed_repl_post_revival_failure_rolls_back_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    assert one_shot.exit_code == 0

    def fail_after_revival(**_kwargs):
        raise StoreError(
            error_class="persistence_error",
            message="runtime construction failed after revival",
            recoverable=False,
        )

    monkeypatch.setattr(
        orchestrator_module,
        "_runtime_from_resumed_session",
        fail_after_revival,
    )

    result = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        one_shot.session_id
    )

    assert result.runtime is None
    assert result.error is not None
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_row = conn.execute(
            """
            SELECT status, active_run_id, owner_pid, owner_host_id, owner_token
            FROM sessions
            WHERE session_id = ?
            """,
            (one_shot.session_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?",
            (one_shot.run_id,),
        ).fetchone()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]

    assert session_row == ("completed", None, None, None, None)
    assert run_row == ("completed",)
    assert "session_resumed" not in event_kinds
    assert "run_resumed" not in event_kinds

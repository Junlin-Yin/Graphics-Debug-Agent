from __future__ import annotations

import json
import sqlite3

from debug_agent.runtime.contracts import AgentRunResult
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


def test_one_shot_success_persists_lifecycle_and_completes_session(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\nUse alpha.\n",
        encoding="utf-8",
    )
    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config("one shot answer")
    )

    assert result.exit_code == 0
    assert result.assistant_output == "one shot answer"
    assert result.error is None

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_row = conn.execute(
            "SELECT status, approval_mode, active_run_id FROM sessions"
        ).fetchone()
        run_row = conn.execute("SELECT status, run_type FROM runs").fetchone()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_rows = conn.execute(
            "SELECT checkpoint_id, kind FROM checkpoints ORDER BY rowid"
        ).fetchall()
        session_latest_checkpoint_id = conn.execute(
            "SELECT latest_checkpoint_id FROM sessions"
        ).fetchone()[0]
        run_latest_checkpoint_id = conn.execute(
            "SELECT latest_checkpoint_id FROM runs"
        ).fetchone()[0]
        skill_rows = conn.execute("SELECT skill_name FROM skill_snapshots").fetchall()

    assert session_row == ("completed", "normal", None)
    assert run_row == ("completed", "prompt")
    assert event_kinds == [
        "session_started",
        "run_started",
        "skill_snapshot_created",
        "user_message",
        "model_call_started",
        "model_call_completed",
        "assistant_message",
        "checkpoint_written",
        "checkpoint_written",
        "run_completed",
        "session_completed",
    ]
    assert [row[1] for row in checkpoint_rows] == ["turn", "terminal"]
    terminal_checkpoint_id = checkpoint_rows[-1][0]
    assert session_latest_checkpoint_id == terminal_checkpoint_id
    assert run_latest_checkpoint_id == terminal_checkpoint_id
    assert skill_rows == [("alpha",)]


def test_one_shot_skill_headers_do_not_mutate_config_snapshots_or_model_input(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\nSECRET BODY\n",
        encoding="utf-8",
    )
    config = _config("unused")
    original_config = json.loads(json.dumps(config))
    captured: dict[str, dict] = {}

    class CapturingAdapter:
        def __init__(self, *, model: object, tool_broker: object) -> None:
            self.model = model
            self.tool_broker = tool_broker

        def run(self, request, context):
            captured["model_config"] = dict(request.model_config)
            return AgentRunResult(
                status="completed",
                assistant_output="captured",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    monkeypatch.setattr(
        orchestrator_module, "LangChainAgentLoopAdapter", CapturingAdapter
    )

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 0
    assert config == original_config
    assert "available_skill_headers" not in captured["model_config"]
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        persisted_config = json.loads(
            conn.execute("SELECT config_snapshot_json FROM sessions").fetchone()[0]
        )
    assert "available_skill_headers" not in persisted_config


def test_one_shot_default_path_does_not_expose_phase1_native_tools_before_gate(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, list[dict]] = {}

    class CapturingAdapter:
        def __init__(self, *, model: object, tool_broker: object) -> None:
            self.model = model
            self.tool_broker = tool_broker

        def run(self, request, context):
            captured["tools"] = request.tools
            return AgentRunResult(
                status="completed",
                assistant_output="captured",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    monkeypatch.setattr(
        orchestrator_module, "LangChainAgentLoopAdapter", CapturingAdapter
    )

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config("unused")
    )

    assert result.exit_code == 0
    assert captured["tools"] == []


def test_one_shot_model_failure_marks_run_and_session_failed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config()
    config["fake_error"] = "provider failed"

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 1
    assert result.assistant_output is None
    assert result.error["error_class"] == "model_error"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
    assert "run_failed" in event_kinds
    assert "session_failed" in event_kinds


def test_one_shot_invalid_skill_fails_before_model_call_and_releases_ownership(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "bad"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad\ndescription: Bad\nexecution_mode: workflow\n---\nbody\n",
        encoding="utf-8",
    )

    def fail_if_model_created(self, config_snapshot):
        raise AssertionError("model must not be created after skill startup failure")

    monkeypatch.setattr(orchestrator_module.ModelFactory, "create", fail_if_model_created)

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", _config())

    assert result.exit_code == 1
    assert result.error["error_class"] == "config_error"
    assert "Only prompt skills" in result.message
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id = conn.execute(
            "SELECT status, active_run_id FROM sessions"
        ).fetchone()
        run_status = conn.execute("SELECT status FROM runs").fetchone()[0]
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_kind, checkpoint_summary = conn.execute(
            "SELECT kind, summary FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()

    assert (session_status, active_run_id, run_status) == ("failed", None, "failed")
    assert checkpoint_kind == "error"
    assert "Only prompt skills" in checkpoint_summary
    assert event_kinds == [
        "session_started",
        "run_started",
        "checkpoint_written",
        "run_failed",
        "session_failed",
    ]

    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad\ndescription: Fixed\n---\nbody\n", encoding="utf-8"
    )
    monkeypatch.undo()
    second = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )
    assert second.exit_code == 0


def test_repl_invalid_skill_fails_startup_before_returning_runtime(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "bad"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad\ndescription: Bad\nexecution_mode: subagent\n---\nbody\n",
        encoding="utf-8",
    )

    result = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())

    assert result.runtime is None
    assert result.error is not None
    assert result.error.error["error_class"] == "config_error"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id = conn.execute(
            "SELECT status, active_run_id FROM sessions"
        ).fetchone()
        run_status = conn.execute("SELECT status FROM runs").fetchone()[0]

    assert (session_status, active_run_id, run_status) == ("failed", None, "failed")


def test_repl_skill_lines_render_from_frozen_snapshots_and_active_run_state(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\nORIGINAL\n",
        encoding="utf-8",
    )

    result = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())

    assert result.error is None
    assert result.runtime is not None
    runtime = result.runtime
    try:
        frozen_lines = runtime.skill_lines()
        frozen_hash = runtime.db.connection.execute(
            "SELECT overall_content_hash FROM skill_snapshots WHERE skill_name = 'alpha'"
        ).fetchone()[0]
        runtime.runs.activate_skill(
            runtime.run_id,
            name="alpha",
            content_hash=frozen_hash,
        )
        skill_file.write_text(
            "---\nname: alpha\ndescription: Mutated skill\n---\nMUTATED\n",
            encoding="utf-8",
        )
        active_lines = runtime.skill_lines()
    finally:
        runtime.close()

    assert frozen_lines == [
        "Skills:",
        f"- alpha | Alpha skill | mode=prompt | scope=project | hash={frozen_hash} | active=no",
    ]
    assert active_lines == [
        "Skills:",
        f"- alpha | Alpha skill | mode=prompt | scope=project | hash={frozen_hash} | active=yes",
    ]
    assert "Mutated skill" not in active_lines[1]


def test_one_shot_model_cancellation_marks_failed_and_releases_ownership(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config()
    config["fake_cancelled"] = True

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 1
    assert result.assistant_output is None
    assert result.error["error_class"] == "cancelled"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id, session_error = conn.execute(
            "SELECT status, active_run_id, error_summary FROM sessions"
        ).fetchone()
        run_status, run_error = conn.execute(
            "SELECT status, error_summary FROM runs"
        ).fetchone()
        checkpoint_kind, checkpoint_state = conn.execute(
            "SELECT kind, state_json FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        failed_error_class = conn.execute(
            """
            SELECT json_extract(payload_json, '$.error_class')
            FROM run_events
            WHERE kind = 'session_failed'
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()[0]

    assert (session_status, active_run_id, run_status) == ("failed", None, "failed")
    assert session_error == "fake model cancelled"
    assert run_error == "fake model cancelled"
    assert checkpoint_kind == "error"
    assert '"latest_error_summary": "fake model cancelled"' in checkpoint_state
    assert failed_error_class == "cancelled"

    second = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )
    assert second.exit_code == 0


def test_one_shot_model_timeout_marks_failed_and_releases_ownership(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config()
    config["fake_timeout"] = True

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 1
    assert result.assistant_output is None
    assert result.error["error_class"] == "timeout"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id, session_error = conn.execute(
            "SELECT status, active_run_id, error_summary FROM sessions"
        ).fetchone()
        run_status, run_error = conn.execute(
            "SELECT status, error_summary FROM runs"
        ).fetchone()
        checkpoint_kind, checkpoint_state = conn.execute(
            "SELECT kind, state_json FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        failed_error_class = conn.execute(
            """
            SELECT json_extract(payload_json, '$.error_class')
            FROM run_events
            WHERE kind = 'session_failed'
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()[0]

    assert (session_status, active_run_id, run_status) == ("failed", None, "failed")
    assert session_error == "fake model timeout"
    assert run_error == "fake model timeout"
    assert checkpoint_kind == "error"
    assert '"latest_error_summary": "fake model timeout"' in checkpoint_state
    assert failed_error_class == "timeout"

    second = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )
    assert second.exit_code == 0


def test_one_shot_active_workspace_conflict_returns_policy_exit(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", _config())
    assert first.exit_code == 0

    db_path = workspace / ".sessions" / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE sessions SET status = 'running' WHERE session_id = ?", (first.session_id,))
        conn.commit()

    conflict = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", _config())

    assert conflict.exit_code == 3
    assert conflict.error["error_class"] == "user_error"
    assert "An active debug-agent session already owns this workspace." in conflict.message
    assert f"Session: {first.session_id}" in conflict.message

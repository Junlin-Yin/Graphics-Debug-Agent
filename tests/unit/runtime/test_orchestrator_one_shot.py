from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.provider_execution import ProviderBoundaryNotClosed
from debug_agent.adapters.model_factory import ModelFactoryResult
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
        "development": {
            "allow_incomplete_phase3_prompt_execution": True,
        },
    }


def _terminal_failure_errors(workspace) -> dict[str, dict]:
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        rows = conn.execute(
            """
            SELECT kind, payload_json
            FROM run_events
            WHERE kind IN ('run_failed', 'session_failed')
            ORDER BY kind
            """
        ).fetchall()
    return {kind: json.loads(payload)["error"] for kind, payload in rows}


def _assert_normalized_terminal_errors(
    workspace,
    *,
    error_class: str,
    reason: str,
    scope: str,
) -> None:
    errors = _terminal_failure_errors(workspace)
    assert set(errors) == {"run_failed", "session_failed"}
    for error in errors.values():
        assert error["schema_version"] == 1
        assert error["error_class"] == error_class
        assert error["reason"] == reason
        assert error["scope"] == scope
        assert error["recoverability"]
        assert isinstance(error["metadata"], dict)
        assert isinstance(error["artifact_ids"], list)
        assert error["message"]


def test_one_shot_closes_model_provider_resources(tmp_path, monkeypatch) -> None:
    closed: list[str] = []

    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    class _Model:
        def __init__(self) -> None:
            self._client = _Client("sync")
            self._async_client = _Client("async")

        def invoke(self, _messages):
            return type(
                "Response",
                (),
                {"content": "one shot answer", "tool_calls": [], "usage": {}},
            )()

    class _Factory:
        def create(self, _config):
            return ModelFactoryResult(model=_Model(), error=None)

    monkeypatch.setattr(orchestrator_module, "ModelFactory", _Factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )

    assert result.exit_code == 0
    assert closed == ["sync", "async"]


def test_repl_close_closes_model_provider_resources(tmp_path, monkeypatch) -> None:
    closed: list[str] = []

    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    class _Model:
        def __init__(self) -> None:
            self._client = _Client("sync")
            self._async_client = _Client("async")

        def invoke(self, _messages):
            return type(
                "Response",
                (),
                {"content": "repl answer", "tool_calls": [], "usage": {}},
            )()

    class _Factory:
        def create(self, _config):
            return ModelFactoryResult(model=_Model(), error=None)

    monkeypatch.setattr(orchestrator_module, "ModelFactory", _Factory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    started = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())

    assert started.runtime is not None
    started.runtime.close()
    assert closed == ["sync", "async"]


def test_one_shot_success_persists_lifecycle_and_completes_session(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
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
        "run_completed",
        "session_completed",
    ]
    assert [row[1] for row in checkpoint_rows] == ["terminal_recovery"]
    terminal_checkpoint_id = checkpoint_rows[-1][0]
    assert session_latest_checkpoint_id == terminal_checkpoint_id
    assert run_latest_checkpoint_id == terminal_checkpoint_id
    assert skill_rows == [("alpha",)]


def test_one_shot_release_failure_persists_normalized_runtime_error_event(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fail_release(self, *, session_id: str, owner_token: str) -> bool:
        return False

    monkeypatch.setattr(
        orchestrator_module.SessionStore,
        "release_ownership",
        fail_release,
    )

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config("one shot answer")
    )

    assert result.exit_code == 0

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        rows = conn.execute(
            """
            SELECT kind, payload_json
            FROM run_events
            WHERE kind = 'run_failed'
            ORDER BY rowid
            """
        ).fetchall()
        owner_token = conn.execute("SELECT owner_token FROM sessions").fetchone()[0]

    assert owner_token is not None
    assert len(rows) == 1
    error = json.loads(rows[0][1])["error"]
    assert error["error_class"] == "runtime_error"
    assert error["reason"] == "ownership_release_failed"
    assert error["scope"] == "session"
    assert error["schema_version"] == 1


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
            captured["frame"] = request.model_context_frame.to_dict()
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
    frame_text = json.dumps(captured["frame"], sort_keys=True)
    assert "available_skill_headers" in frame_text
    assert "alpha: Alpha skill" in frame_text
    assert "SECRET BODY" not in frame_text
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        persisted_config = json.loads(
            conn.execute("SELECT config_snapshot_json FROM sessions").fetchone()[0]
        )
    assert "available_skill_headers" not in persisted_config


def test_one_shot_default_path_exposes_todo_but_keeps_view_image_gated(
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
            captured["tool_schema_bindings"] = (
                request.model_context_frame.tool_schema_bindings
            )
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
    assert [tool["name"] for tool in captured["tool_schema_bindings"]] == [
        "read_file",
        "list_dir",
        "find_file",
        "search_text",
        "write_file",
        "edit_file",
        "shell_exec",
        "activate_skill",
        "load_skill_resource",
        "todo",
    ]
    assert "view_image" not in [
        tool["name"] for tool in captured["tool_schema_bindings"]
    ]


def test_one_shot_enabled_multimodal_exposes_view_image_tool_binding(
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
            captured["tool_schema_bindings"] = (
                request.model_context_frame.tool_schema_bindings
            )
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
    config = _config("unused")
    config["multimodal"] = {
        "provider": "openai",
        "model": "kimi-k2.5",
        "timeout_seconds": 60,
        "max_tokens": 4096,
        "max_query_chars": 8192,
        "max_analysis_chars": 8192,
        "api_key_env": "MOONSHOT_API_KEY",
        "api_key_present": True,
        "base_url_env": "MOONSHOT_BASE_URL",
        "base_url_present": True,
        "view_image_enabled": True,
        "view_image_disabled_reason": None,
    }

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 0
    assert "view_image" in [
        tool["name"] for tool in captured["tool_schema_bindings"]
    ]


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
    _assert_normalized_terminal_errors(
        workspace,
        error_class="model_error",
        reason="model_call_failed",
        scope="provider",
    )


def test_one_shot_context_limit_exceeded_records_context_fact_before_terminal_failure(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config("should not be called")
    config["system_prompt"] = "system " * 100
    config["context"] = {
        "window_tokens": 40,
        "omit_old_tool_results_at_ratio": 1.0,
        "compress_history_at_ratio": 1.0,
        "retain_recent_model_calls": 4,
        "compression_reserved_output_tokens": 10,
    }

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    expected_message = (
        "Context window still exceeds the limit after compression. "
        "The current turn was aborted."
    )
    assert result.exit_code == 1
    assert result.error["error_class"] == "model_error"
    assert result.error["reason"] == "context_limit_exceeded"
    assert result.message == expected_message
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM checkpoints ORDER BY rowid")
        ]
    assert "context_limit_exceeded" in event_kinds
    assert event_kinds.index("context_limit_exceeded") < event_kinds.index("run_failed")
    assert checkpoint_kinds == ["terminal_recovery"]
    _assert_normalized_terminal_errors(
        workspace,
        error_class="model_error",
        reason="context_limit_exceeded",
        scope="turn",
    )


def test_one_shot_compression_failed_records_context_fact_before_terminal_failure(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_run_turn = orchestrator_module.PromptAgentExecutor.run_turn

    def compression_failed_run_turn(self, *, session, run, **_kwargs):
        failure = self._record_compression_failed(
            session=session,
            run=run,
            reason="oldest_group_too_large",
            prompt_turn_counter=1,
            token_estimate={"before": {"total_tokens": 1000}},
        )
        return AgentRunResult(
            status="failed",
            assistant_output=None,
            tool_results=[],
            usage={},
            error={
                "error_class": "compression_failed",
                "message": failure["message"],
            },
            metadata={
                "failure_scope": "turn",
                "context_optimization": failure["metadata"],
            },
        )

    monkeypatch.setattr(
        orchestrator_module.PromptAgentExecutor,
        "run_turn",
        compression_failed_run_turn,
    )
    try:
        result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
            "hello", _config("unused")
        )
    finally:
        monkeypatch.setattr(
            orchestrator_module.PromptAgentExecutor,
            "run_turn",
            original_run_turn,
        )

    expected_message = (
        "Context compression could not fit the oldest eligible history group. "
        "The current turn was aborted. Start a new session to continue with a "
        "fresh context window."
    )
    assert result.exit_code == 1
    assert result.error["error_class"] == "model_error"
    assert result.error["reason"] == "compression_failed"
    assert result.message == expected_message
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM checkpoints ORDER BY rowid")
        ]
    assert "compression_failed" in event_kinds
    assert event_kinds.index("compression_failed") < event_kinds.index("run_failed")
    assert checkpoint_kinds == []
    _assert_normalized_terminal_errors(
        workspace,
        error_class="model_error",
        reason="compression_failed",
        scope="turn",
    )


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
    assert result.terminal_failure_summary is False
    assert result.error["error_class"] == "config_error"
    assert "Only prompt skills" in result.message
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id, session_latest, session_startup_marker = conn.execute(
            """
            SELECT status, active_run_id, latest_checkpoint_id,
                   non_resumable_startup_failure
            FROM sessions
            """
        ).fetchone()
        run_status, run_latest, run_startup_marker = conn.execute(
            """
            SELECT status, latest_checkpoint_id, non_resumable_startup_failure
            FROM runs
            """
        ).fetchone()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_count = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]

    assert (session_status, active_run_id, run_status) == ("failed", None, "failed")
    assert session_latest is None
    assert run_latest is None
    assert session_startup_marker == 1
    assert run_startup_marker == 1
    assert checkpoint_count == 0
    assert event_kinds == [
        "session_started",
        "run_started",
        "run_failed",
        "session_failed",
    ]
    _assert_normalized_terminal_errors(
        workspace,
        error_class="config_error",
        reason="startup_schema_validation_failed",
        scope="startup",
    )

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


def test_repl_skill_lines_render_from_frozen_snapshots_and_active_run_state(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
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
        "",
        "- alpha (project) [inactive]",
        "Alpha skill",
    ]
    assert active_lines == [
        "",
        "- alpha (project) [active]",
        "Alpha skill",
    ]
    assert "Mutated skill" not in active_lines[2]


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
    checkpoint_payload = json.loads(checkpoint_state)
    assert checkpoint_kind == "terminal_recovery"
    assert checkpoint_payload["terminal_error"]["message"] == "fake model cancelled"
    assert failed_error_class == "cancelled"

    second = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )
    assert second.exit_code == 0



def test_one_shot_keyboard_interrupt_cancels_without_collecting_provider_boundary(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cancelled = {"called": False, "collected": False}

    class Handle:
        def cancel(self):
            cancelled["called"] = True

        def collect_boundary(self):
            cancelled["collected"] = True

    class InterruptingExecutor:
        adapter = SimpleNamespace(model=None)

        def __init__(self, *args, **kwargs):
            pass

        def run_turn(self, **kwargs):
            registry = kwargs.get("provider_cancellation_registry")
            assert callable(registry)
            registry(Handle())
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "debug_agent.runtime.orchestrator.PromptAgentExecutor",
        InterruptingExecutor,
    )

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config(),
    )

    assert result.exit_code == 1
    assert result.terminal_failure_summary is True
    assert result.error["error_class"] == "cancelled"
    assert result.error["reason"] == "user_cancel_running"
    assert result.session_id is not None
    assert cancelled == {"called": True, "collected": False}


def test_one_shot_provider_boundary_not_closed_returns_interrupted_result(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class BoundaryExecutor:
        adapter = SimpleNamespace(model=None)

        def __init__(self, *args, **kwargs):
            pass

        def run_turn(self, **kwargs):
            raise ProviderBoundaryNotClosed("view_image async provider task did not close locally.")

    monkeypatch.setattr(
        "debug_agent.runtime.orchestrator.PromptAgentExecutor",
        BoundaryExecutor,
    )

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config(),
    )

    assert result.exit_code == 130
    assert result.terminal_failure_summary is False
    assert result.error["error_class"] == "cancelled"
    assert result.error["reason"] == "user_cancel_process"
    assert "did not close locally" in result.message
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "running"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "running"


def test_one_shot_model_timeout_marks_failed_and_releases_ownership(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config()
    config["fake_timeout"] = True

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 1
    assert result.assistant_output is None
    assert result.terminal_failure_summary is True
    assert result.error["error_class"] == "model_error"
    assert result.error["reason"] == "model_call_timeout"
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
    checkpoint_payload = json.loads(checkpoint_state)
    assert checkpoint_kind == "terminal_recovery"
    assert checkpoint_payload["terminal_error"]["message"] == "fake model timeout"
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
    assert conflict.terminal_failure_summary is False
    assert conflict.error["error_class"] == "policy_error"
    assert conflict.error["reason"] == "workspace_owner_not_proven_stale"
    assert "An active debug-agent session already owns this workspace." in conflict.message
    assert f"Session: {first.session_id}" in conflict.message

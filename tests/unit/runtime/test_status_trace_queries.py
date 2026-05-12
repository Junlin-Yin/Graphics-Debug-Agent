from __future__ import annotations

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


def test_status_query_returns_documented_fields_after_one_shot(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RuntimeOrchestrator(workspace_root=workspace)
    one_shot = orchestrator.run_one_shot("hello", _config())

    status = RuntimeOrchestrator(workspace_root=workspace).status(one_shot.session_id)

    assert status.exit_code == 0
    assert status.fields["session_id"] == one_shot.session_id
    assert status.fields["workspace_root"] == str(workspace.resolve())
    assert status.fields["session_status"] == "completed"
    assert status.fields["approval_mode"] == "yolo"
    assert status.fields["active_run_id"] is None
    assert status.fields["latest_run_id"] == one_shot.run_id
    assert status.fields["latest_run_status"] == "completed"
    assert status.fields["latest_checkpoint_id"]
    assert status.fields["created_at"]
    assert status.fields["updated_at"]
    assert status.fields["error_summary"] is None


def test_trace_query_refreshes_trace_and_returns_summary_after_one_shot(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello", _config()
    )

    trace = RuntimeOrchestrator(workspace_root=workspace).trace(one_shot.session_id)

    assert trace.exit_code == 0
    assert trace.summary["session_id"] == one_shot.session_id
    assert trace.summary["workspace_root"] == str(workspace.resolve())
    assert trace.summary["run_count"] == 1
    assert trace.summary["event_count"] >= 1
    assert trace.summary["artifact_count"] == 0
    assert trace.summary["terminal_status"] == "completed"
    assert trace.trace_path.is_file()
    assert "## Timeline" in trace.trace_path.read_text(encoding="utf-8")


def test_status_query_returns_missing_session_error(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    status = RuntimeOrchestrator(workspace_root=workspace).status("sess_missing")

    assert status.exit_code == 1
    assert status.message == "No session found for id: sess_missing"

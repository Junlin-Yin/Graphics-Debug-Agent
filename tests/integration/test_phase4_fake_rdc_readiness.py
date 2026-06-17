from __future__ import annotations

from concurrent.futures import Future
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

from PIL import Image

from debug_agent.adapters import vision_client as vision_client_module
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


def _load_fake_rdc_helper():
    helper_path = Path(__file__).parent / "fixtures" / "fake_rdc.py"
    spec = importlib.util.spec_from_file_location("phase4_fake_rdc_fixture", helper_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(output_png: str, rdc_command: str = "rdc") -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "temperature": 0.0,
        "max_tokens": 256,
        "timeout_seconds": 30,
        "system_prompt": "You are running a fake rdc readiness check.",
        "context": {
            "window_tokens": 12000,
            "omit_old_tool_results_at_ratio": 0.8,
            "compress_history_at_ratio": 0.95,
            "retain_recent_model_calls": 2,
            "compression_reserved_output_tokens": 512,
        },
        "execution": {
            "default_tool_timeout_seconds": 30,
            "default_shell_timeout_seconds": 30,
            "max_shell_timeout_seconds": 60,
            "cancellation_timeout_seconds": 1,
        },
        "agent_loop": {"max_tool_call_iterations": 5},
        "development": {"allow_incomplete_phase3_prompt_execution": True},
        "multimodal": {
            "provider": "openai",
            "model": "kimi-k2.5",
            "timeout_seconds": 30,
            "max_tokens": 256,
            "max_query_chars": 200,
            "max_analysis_chars": 200,
            "api_key_env": "MOONSHOT_API_KEY",
            "api_key_present": True,
            "base_url_env": "MOONSHOT_BASE_URL",
            "base_url_present": True,
            "view_image_enabled": True,
            "view_image_disabled_reason": None,
        },
        "thinking": {"enabled": False, "effort": "high"},
        "fake_response": "fake rdc readiness complete",
        "fake_tool_calls": [
            {"name": "shell_exec", "args": {"argv": [rdc_command, "doctor"]}, "id": "call_rdc_doctor"},
            {"name": "shell_exec", "args": {"argv": [rdc_command, "open", "sample.rdc"]}, "id": "call_rdc_open"},
            {"name": "shell_exec", "args": {"argv": [rdc_command, "info", "--json"]}, "id": "call_rdc_info"},
            {"name": "shell_exec", "args": {"argv": [rdc_command, "draws", "--limit", "20"]}, "id": "call_rdc_draws"},
            {"name": "shell_exec", "args": {"argv": [rdc_command, "rt", "42", "-o", output_png]}, "id": "call_rdc_rt"},
            {"name": "view_image", "args": {"paths": [output_png], "query": "Inspect the exported render target."}, "id": "call_view_image"},
            {"name": "shell_exec", "args": {"argv": [rdc_command, "close"]}, "id": "call_rdc_close"},
        ],
    }


def test_phase4_fake_rdc_readiness_uses_brokered_shell_and_view_image(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / "sample.rdc").write_bytes(b"fake deterministic capture\n")
    materialize_fake_rdc = _load_fake_rdc_helper().materialize_fake_rdc
    fake_rdc = materialize_fake_rdc(tmp_path, workspace)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOONSHOT_API_KEY", "fake-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://vision.invalid/v1")
    monkeypatch.setenv("PATH", f"{fake_rdc.parent}{os.pathsep}{os.environ['PATH']}")
    rdc_command = str(fake_rdc) if os.name == "nt" else "rdc"

    vision_calls: list[dict] = []

    def fake_analyze_async(self, **kwargs):
        vision_calls.append(kwargs)
        future = Future()
        future.set_result(
            vision_client_module.VisionModelResponse(
                text='{"analysis":"valid fake rdc PNG inspected"}',
                provider_metadata={"provider": "fake-vision"},
            )
        )
        return future

    monkeypatch.setattr(
        vision_client_module.VisionModelClient,
        "analyze_async",
        fake_analyze_async,
    )

    output_png = "exports/target.png"
    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "Run the fake rdc readiness flow.",
        _config(output_png, rdc_command),
        approval_mode="yolo",
    )

    assert result.exit_code == 0
    assert result.assistant_output == "fake rdc readiness complete"
    assert result.session_id is not None
    assert result.run_id is not None

    exported_png = workspace / output_png
    assert exported_png.is_file()
    with Image.open(exported_png) as image:
        assert image.format == "PNG"
        assert image.size == (2, 2)
    assert len(vision_calls) == 1

    command_log = workspace / ".fake_rdc" / "commands.jsonl"
    assert command_log.is_file()
    fake_commands = [
        json.loads(line)
        for line in command_log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [entry["argv"] for entry in fake_commands] == [
        ["doctor"],
        ["open", "sample.rdc"],
        ["info", "--json"],
        ["draws", "--limit", "20"],
        ["rt", "42", "-o", output_png],
        ["close"],
    ]
    assert {entry["cwd"] for entry in fake_commands} == {str(workspace)}
    assert not (workspace / ".fake_rdc" / "state.json").exists()

    db_path = workspace / ".sessions" / "runtime.db"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "completed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "completed"
        assert conn.execute("SELECT COUNT(*) FROM skill_snapshots").fetchone()[0] == 0
        tool_rows = conn.execute(
            """
            SELECT json_extract(payload_json, '$.tool_name'),
                   json_extract(payload_json, '$.result.output.argv'),
                   json_extract(payload_json, '$.result.output.cwd'),
                   json_extract(payload_json, '$.result.status')
            FROM run_events
            WHERE kind = 'tool_call_completed'
            ORDER BY rowid
            """
        ).fetchall()

    shell_rows = [row for row in tool_rows if row[0] == "shell_exec"]
    assert len(shell_rows) == 6
    assert [json.loads(row[1]) for row in shell_rows] == [
        [rdc_command, "doctor"],
        [rdc_command, "open", "sample.rdc"],
        [rdc_command, "info", "--json"],
        [rdc_command, "draws", "--limit", "20"],
        [rdc_command, "rt", "42", "-o", output_png],
        [rdc_command, "close"],
    ]
    assert {row[2] for row in shell_rows} == {str(workspace.resolve())}
    assert all(row[3] == "ok" for row in shell_rows)
    view_rows = [row for row in tool_rows if row[0] == "view_image"]
    assert len(view_rows) == 1
    assert view_rows[0][3] == "ok"

    logs_dir = workspace / ".sessions" / result.session_id / "logs"
    trace_path = logs_dir / "trace.md"
    assert trace_path.is_file()
    trace_text = trace_path.read_text(encoding="utf-8")
    assert "**✅ shell_exec** (`shell_exec`)" in trace_text
    assert "**✅ view_image** (`view_image`)" in trace_text
    assert "rdc" in trace_text

    metrics_paths = sorted(logs_dir.glob("run_metrics_*.json"))
    assert len(metrics_paths) == 1
    metrics = json.loads(metrics_paths[0].read_text(encoding="utf-8"))
    assert metrics["schema_version"] == 1
    assert metrics["session_id"] == result.session_id
    assert metrics["run_id"] == result.run_id
    assert metrics["tools"]["total_tool_calls"] == 7
    assert metrics["tools"]["successful_tool_calls"] == 7
    assert metrics["tools"]["failed_tool_calls"] == 0

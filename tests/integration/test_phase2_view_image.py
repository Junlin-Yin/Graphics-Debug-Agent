from __future__ import annotations

from concurrent.futures import Future
from io import BytesIO

from PIL import Image

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.policy import build_builtin_policy
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker


class _FakeVisionClient:
    def __init__(self, text: str = '{"analysis":"multi image analysis"}') -> None:
        self.calls: list[dict] = []
        self.text = text

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        return type("VisionResponse", (), {"text": self.text, "provider_metadata": {}})()

    def analyze_async(self, **kwargs):
        cleaned = dict(kwargs)
        cleaned.pop("register_cancellation_handle", None)
        cleaned.pop("cancellation_token", None)
        future = Future()
        future.set_result(self.analyze(**cleaned))
        return future


def _enabled_multimodal() -> dict:
    return {
        "provider": "openai",
        "model": "kimi-k2.5",
        "timeout_seconds": 12,
        "max_tokens": 256,
        "max_query_chars": 100,
        "max_analysis_chars": 100,
        "api_key_env": "MOONSHOT_API_KEY",
        "api_key_present": True,
        "base_url_env": "MOONSHOT_BASE_URL",
        "base_url_present": True,
        "view_image_enabled": True,
        "view_image_disabled_reason": None,
    }


def _write_image(path, *, fmt: str, size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(100, 20, 40)).save(buffer, format=fmt)
    data = buffer.getvalue()
    path.write_bytes(data)
    return data


def _runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    config_snapshot = {
        "provider": "fake",
        "model": "fake-model",
        "multimodal": _enabled_multimodal(),
    }
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot=config_snapshot,
        session_id="sess_view",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_view")
    sessions.set_active_run(session.session_id, run.run_id)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    return {
        "workspace": workspace,
        "db": db,
        "session": session,
        "run": run,
        "events": events,
        "artifacts": artifacts,
        "broker": ToolBroker(event_writer=events, artifact_store=artifacts),
        "config_snapshot": config_snapshot,
    }


def _invoke(runtime, arguments, *, vision_client):
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="view_image",
        arguments=arguments,
        context={
            "workspace_root": str(runtime["workspace"]),
            "approval_mode": "yolo",
            "policy_facts": build_builtin_policy(runtime["workspace"]),
            "approval_grants": ApprovalGrantStore(runtime["db"].connection),
            "approval_provider": FakeApprovalProvider("denied"),
            "frozen_config": runtime["config_snapshot"],
            "internal_enable_view_image": True,
            "vision_client": vision_client,
        },
    )


def test_internal_gated_view_image_inspects_multiple_images_without_source_artifacts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    png = runtime["workspace"] / "first.bin"
    jpeg = runtime["workspace"] / "second.dat"
    png_bytes = _write_image(png, fmt="PNG", size=(5, 4))
    jpeg_bytes = _write_image(jpeg, fmt="JPEG", size=(6, 3))
    vision_client = _FakeVisionClient()

    result = _invoke(
        runtime,
        {"paths": ["first.bin", "second.dat"], "query": " compare "},
        vision_client=vision_client,
    )

    assert result.status == "ok"
    assert result.output == {
        "analysis": "multi image analysis",
        "metadata": [
            {"path": "first.bin", "mime_type": "image/png", "width": 5, "height": 4},
            {"path": "second.dat", "mime_type": "image/jpeg", "width": 6, "height": 3},
        ],
    }
    assert result.artifacts == []
    assert result.metadata["effective_query_source"] == "assistant"
    assert [image["byte_size"] for image in result.metadata["images"]] == [
        len(png_bytes),
        len(jpeg_bytes),
    ]
    assert [image.mime_type for image in vision_client.calls[0]["images"]] == [
        "image/png",
        "image/jpeg",
    ]
    assert vision_client.calls[0]["timeout_seconds"] == 12
    assert runtime["artifacts"].list_for_session(runtime["session"].session_id) == []
    assert [event.kind for event in runtime["events"].list_for_run("run_view")] == [
        "tool_call_started",
        "tool_call_completed",
    ]
    runtime["db"].close()


def test_failed_view_image_does_not_create_source_image_artifacts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    image = runtime["workspace"] / "bad.png"
    image.write_bytes(b"not really an image")
    vision_client = _FakeVisionClient()

    result = _invoke(runtime, {"paths": ["bad.png"]}, vision_client=vision_client)

    assert result.status == "error"
    assert result.error["error_class"] == "tool_error"
    assert vision_client.calls == []
    assert runtime["artifacts"].list_for_session(runtime["session"].session_id) == []
    runtime["db"].close()

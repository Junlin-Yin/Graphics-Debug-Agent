from __future__ import annotations

from dataclasses import replace

import httpx
import openai

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.policy import build_builtin_policy
from debug_agent.tools.broker import (
    LARGE_OUTPUT_THRESHOLD_BYTES,
    FakeApprovalProvider,
    ToolBroker,
    ToolRouter,
)
from debug_agent.tools.view_image import ViewImageTool


try:
    from PIL import Image
except ImportError:  # pragma: no cover - dependency is required by Phase 2
    Image = None


class ExplodingRouter(ToolRouter):
    def route(self, context, arguments):
        raise AssertionError("disabled view_image must not route to a handler")


def _runtime(tmp_path, *, multimodal: dict | None = None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    config_snapshot = {
        "provider": "fake",
        "model": "fake-model",
        "multimodal": multimodal
        or {
            "provider": None,
            "model": None,
            "timeout_seconds": 60,
            "max_tokens": 4096,
            "max_query_chars": 8192,
            "max_analysis_chars": 8192,
            "api_key_env": None,
            "api_key_present": False,
            "base_url_env": None,
            "base_url_present": False,
            "view_image_enabled": False,
            "view_image_disabled_reason": "missing_multimodal_config",
        },
    }
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot=config_snapshot,
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    return {
        "workspace": workspace,
        "db": db,
        "broker": broker,
        "artifacts": artifacts,
        "session": session,
        "run": run,
        "events": events,
        "approval_mode": "yolo",
        "policy_facts": build_builtin_policy(workspace),
        "config_snapshot": config_snapshot,
    }


def _enabled_multimodal() -> dict:
    return {
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


def _invoke(runtime, tool_name, arguments):
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name=tool_name,
        arguments=arguments,
        context={
            "workspace_root": str(runtime["workspace"]),
            "approval_mode": runtime["approval_mode"],
            "policy_facts": runtime["policy_facts"],
            "approval_grants": ApprovalGrantStore(runtime["db"].connection),
            "approval_provider": FakeApprovalProvider("denied"),
            "frozen_config": runtime["config_snapshot"],
        },
    )


def _invoke_enabled(runtime, arguments, *, vision_client=None, image_reader=None):
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="view_image",
        arguments=arguments,
        context={
            "workspace_root": str(runtime["workspace"]),
            "approval_mode": runtime["approval_mode"],
            "policy_facts": runtime["policy_facts"],
            "approval_grants": ApprovalGrantStore(runtime["db"].connection),
            "approval_provider": FakeApprovalProvider("denied"),
            "frozen_config": runtime["config_snapshot"],
            "internal_enable_view_image": True,
            "vision_client": vision_client or _FakeVisionClient(),
            "view_image_reader": image_reader,
        },
    )


class _FakeVisionClient:
    def __init__(self, text: str = '{"analysis":"the image is visible"}') -> None:
        self.text = text
        self.calls: list[dict] = []

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        return type("VisionResponse", (), {"text": self.text, "provider_metadata": {}})()


class _OpenAITimeoutVisionClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        raise openai.APITimeoutError(httpx.Request("POST", "https://example.test/v1"))


def _write_image(path, *, fmt: str = "PNG", size: tuple[int, int] = (4, 3)) -> bytes:
    assert Image is not None
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(buffer, format=fmt)
    data = buffer.getvalue()
    path.write_bytes(data)
    return data


def test_disabled_view_image_is_known_tool_denied_by_config_without_routing(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, "view_image", {"paths": ["image.png"]})

    assert result.status == "denied"
    assert result.error["error_class"] == "config_error"
    assert result.error["reason"] == "tool_unavailable"
    assert result.error["message"] == "view_image is disabled: missing_multimodal_config"
    assert [event.kind for event in runtime["events"].list_for_run("run_1")] == [
        "tool_call_denied"
    ]
    event = runtime["events"].list_for_run("run_1")[0]
    assert event.payload["tool_name"] == "view_image"
    assert event.payload["error"]["error_class"] == "config_error"
    assert event.payload["error"]["reason"] == "tool_unavailable"
    runtime["db"].close()


def test_unknown_tool_behavior_is_unchanged(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, "view_video", {"paths": ["video.mp4"]})

    assert result.status == "denied"
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "unknown_tool"
    assert result.error["message"] == "Unknown tool: view_video"
    runtime["db"].close()


def test_enabled_view_image_routes_without_internal_activation_gate(
    tmp_path,
) -> None:
    runtime = _runtime(
        tmp_path,
        multimodal={
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
        },
    )

    image = runtime["workspace"] / "capture.png"
    _write_image(image)

    result = _invoke(runtime, "view_image", {"paths": ["capture.png"]})

    assert result.status != "denied"
    assert runtime["events"].list_for_run("run_1")[0].kind == "tool_call_started"
    runtime["db"].close()


def test_view_image_audit_redacts_assistant_query_from_runtime_authored_events(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    image = runtime["workspace"] / "capture.png"
    _write_image(image)
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")

    result = runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="view_image",
        arguments={"paths": ["capture.png"], "query": "secret query focus"},
        context={
            "workspace_root": str(runtime["workspace"]),
            "approval_mode": runtime["approval_mode"],
            "policy_facts": runtime["policy_facts"],
            "approval_grants": ApprovalGrantStore(runtime["db"].connection),
            "approval_provider": FakeApprovalProvider("denied"),
            "frozen_config": runtime["config_snapshot"],
            "vision_client": _FakeVisionClient(),
        },
    )

    assert result.status == "ok"
    events = [event.payload for event in runtime["events"].list_for_run("run_1")]
    serialized_events = repr(events)
    assert "secret query focus" not in serialized_events
    assert "'query'" not in serialized_events
    assert "query_preview" not in serialized_events
    assert "query_length" not in serialized_events
    assert "effective_query_source" in serialized_events
    runtime["db"].close()


def test_enabled_view_image_definition_routes_with_fake_provider(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    image = runtime["workspace"] / "capture.weird"
    data = _write_image(image)
    vision_client = _FakeVisionClient()
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")

    result = _invoke_enabled(runtime, {"paths": ["capture.weird"]}, vision_client=vision_client)

    assert result.status == "ok"
    assert result.output == {
        "analysis": "the image is visible",
        "metadata": [
            {
                "path": "capture.weird",
                "mime_type": "image/png",
                "width": 4,
                "height": 3,
            }
        ],
    }
    assert result.metadata["tool_name"] == "view_image"
    assert result.metadata["vision_provider"] == "openai"
    assert result.metadata["vision_model"] == "kimi-k2.5"
    assert result.metadata["effective_query_source"] == "default"
    assert result.metadata["images"][0]["byte_size"] == len(data)
    assert "query" not in result.metadata
    assert vision_client.calls[0]["timeout_seconds"] == 60
    assert vision_client.calls[0]["config"].model == "kimi-k2.5"
    assert vision_client.calls[0]["images"][0].mime_type == "image/png"
    runtime["db"].close()


def test_view_image_schema_failure_is_user_error_denial(tmp_path) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())

    result = _invoke_enabled(runtime, {"paths": [], "extra": True})

    assert result.status == "denied"
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "tool_schema_invalid"
    assert [event.kind for event in runtime["events"].list_for_run("run_1")] == [
        "tool_call_denied"
    ]
    event = runtime["events"].list_for_run("run_1")[0]
    assert event.payload["error"]["error_class"] == "tool_error"
    assert event.payload["error"]["reason"] == "tool_schema_invalid"
    runtime["db"].close()


def test_disabled_view_image_malformed_call_returns_schema_denial_first(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, "view_image", {"paths": []})

    assert result.status == "denied"
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "tool_schema_invalid"
    assert [event.kind for event in runtime["events"].list_for_run("run_1")] == [
        "tool_call_denied"
    ]
    runtime["db"].close()


def test_view_image_rejects_remote_and_structured_artifact_inputs(tmp_path) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())

    remote = _invoke_enabled(runtime, {"paths": ["https://example.test/image.png"]})
    artifact = _invoke_enabled(runtime, {"paths": [{"artifact_id": "art_123"}]})

    assert remote.status == "error"
    assert remote.error["error_class"] == "tool_error"
    assert artifact.status == "denied"
    assert artifact.error["error_class"] == "tool_error"
    assert artifact.error["reason"] == "tool_schema_invalid"
    runtime["db"].close()


def test_policy_denial_happens_before_image_bytes_are_read(tmp_path) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    denied_dir = runtime["workspace"] / ".sessions"
    denied_dir.mkdir(exist_ok=True)
    image = denied_dir / "capture.png"
    _write_image(image)

    def exploding_reader(_path):
        raise AssertionError("image bytes were read before policy allowed the path")

    result = _invoke_enabled(
        runtime,
        {"paths": [".sessions/capture.png"]},
        image_reader=exploding_reader,
    )

    assert result.status == "denied"
    assert result.error["error_class"] == "policy_error"
    assert result.error["reason"] == "path_policy_denied"
    assert [event.kind for event in runtime["events"].list_for_run("run_1")] == [
        "tool_call_denied"
    ]
    runtime["db"].close()


def test_view_image_approval_scope_uses_ordered_canonical_paths(tmp_path) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    first = runtime["workspace"] / "first.png"
    second = runtime["workspace"] / "second.png"
    _write_image(first)
    _write_image(second)
    approval = FakeApprovalProvider("denied")

    runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="view_image",
        arguments={"paths": ["second.png", "first.png"], "query": "focus"},
        context={
            "workspace_root": str(runtime["workspace"]),
            "approval_mode": "normal",
            "policy_facts": replace(runtime["policy_facts"], user_path_trust=[]),
            "approval_grants": ApprovalGrantStore(runtime["db"].connection),
            "approval_provider": approval,
            "frozen_config": runtime["config_snapshot"],
            "internal_enable_view_image": True,
            "vision_client": _FakeVisionClient(),
        },
    )

    assert len(approval.requests) == 1
    facts = approval.requests[0][1]
    assert facts["scope_signature"] == (
        f"view_image|read|read:{second.resolve()}|read:{first.resolve()}"
    )
    assert "focus" not in facts["scope_signature"]
    runtime["db"].close()


def test_view_image_rejects_symlink_escape_without_provider_call(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    outside = tmp_path / "outside.png"
    _write_image(outside)
    link = runtime["workspace"] / "linked.png"
    link.symlink_to(outside)
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")
    vision_client = _FakeVisionClient()

    result = _invoke_enabled(
        runtime,
        {"paths": ["linked.png"]},
        vision_client=vision_client,
    )

    assert result.status == "error"
    assert result.error["error_class"] == "tool_error"
    assert vision_client.calls == []
    runtime["db"].close()


def test_query_validation_and_provider_json_validation(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, multimodal={**_enabled_multimodal(), "max_query_chars": 5})
    image = runtime["workspace"] / "capture.png"
    _write_image(image)
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")

    empty = _invoke_enabled(runtime, {"paths": ["capture.png"], "query": "   "})
    too_long = _invoke_enabled(runtime, {"paths": ["capture.png"], "query": "123456"})
    invalid_json = _invoke_enabled(
        runtime,
        {"paths": ["capture.png"], "query": "focus"},
        vision_client=_FakeVisionClient("not json"),
    )

    assert empty.status == "denied"
    assert empty.error["error_class"] == "tool_error"
    assert empty.error["reason"] == "tool_schema_invalid"
    assert runtime["events"].list_for_run("run_1")[-3].kind == "tool_call_denied"
    assert too_long.status == "denied"
    assert too_long.error["error_class"] == "tool_error"
    assert too_long.error["reason"] == "tool_schema_invalid"
    assert invalid_json.status == "error"
    assert invalid_json.error["error_class"] == "model_error"
    runtime["db"].close()


def test_view_image_openai_sdk_timeout_returns_timeout_result(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    image = runtime["workspace"] / "capture.png"
    _write_image(image)
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")
    vision_client = _OpenAITimeoutVisionClient()

    result = _invoke_enabled(
        runtime,
        {"paths": ["capture.png"]},
        vision_client=vision_client,
    )

    assert result.status == "timeout"
    assert result.error["error_class"] == "timeout"
    assert vision_client.calls[0]["timeout_seconds"] == 60
    runtime["db"].close()


def test_execution_time_missing_env_returns_config_error_before_provider_call(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    image = runtime["workspace"] / "capture.png"
    _write_image(image)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")
    vision_client = _FakeVisionClient()

    result = _invoke_enabled(runtime, {"paths": ["capture.png"]}, vision_client=vision_client)

    assert result.status == "error"
    assert result.error["error_class"] == "config_error"
    assert vision_client.calls == []
    runtime["db"].close()


def test_view_image_request_size_limit_prevents_provider_call(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, multimodal=_enabled_multimodal())
    image = runtime["workspace"] / "capture.png"
    _write_image(image)
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(ViewImageTool, "MAX_REQUEST_BODY_BYTES", 10)
    vision_client = _FakeVisionClient()

    result = _invoke_enabled(runtime, {"paths": ["capture.png"]}, vision_client=vision_client)

    assert result.status == "error"
    assert result.error["error_class"] == "tool_error"
    assert vision_client.calls == []
    runtime["db"].close()


def test_large_raw_provider_text_is_artifacted_without_source_image_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, multimodal={**_enabled_multimodal(), "max_analysis_chars": 100})
    image = runtime["workspace"] / "capture.png"
    _write_image(image)
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://example.test/v1")
    raw_provider_text = (
        '{"analysis":"small analysis","debug":"' + ("x" * LARGE_OUTPUT_THRESHOLD_BYTES) + '"}'
    )

    result = _invoke_enabled(
        runtime,
        {"paths": ["capture.png"]},
        vision_client=_FakeVisionClient(raw_provider_text),
    )

    assert result.status == "ok"
    assert result.output == {
        "analysis": "small analysis",
        "metadata": [
            {
                "path": "capture.png",
                "mime_type": "image/png",
                "width": 4,
                "height": 3,
            }
        ],
    }
    assert result.metadata["effective_query_source"] == "default"
    assert "query" not in result.metadata
    assert len(result.artifacts) == 1
    artifact = runtime["artifacts"].get(result.artifacts[0])
    assert artifact.artifact_type == "text"
    assert artifact.metadata["tool_name"] == "view_image"
    assert artifact.metadata["bytes"] == len(raw_provider_text.encode("utf-8"))
    assert artifact.metadata["source"] == "raw_provider_output"
    assert artifact.metadata["payload_sha256"].startswith("sha256:")
    assert runtime["artifacts"].resolve_path(artifact.artifact_id).read_text(
        encoding="utf-8"
    ) == raw_provider_text
    assert len(runtime["artifacts"].list_for_session(runtime["session"].session_id)) == 1
    runtime["db"].close()

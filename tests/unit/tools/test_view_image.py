from __future__ import annotations

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.policy import build_builtin_policy
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker, ToolRouter


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
    broker = ToolBroker(
        event_writer=events,
        artifact_store=artifacts,
        router=ExplodingRouter(),
    )
    return {
        "workspace": workspace,
        "db": db,
        "broker": broker,
        "session": session,
        "run": run,
        "events": events,
        "approval_mode": "yolo",
        "policy_facts": build_builtin_policy(workspace),
        "config_snapshot": config_snapshot,
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


def test_disabled_view_image_is_known_tool_denied_by_config_without_routing(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, "view_image", {"paths": ["image.png"]})

    assert result.status == "denied"
    assert result.error == {
        "error_class": "config_error",
        "message": "view_image is disabled: missing_multimodal_config",
        "source": "toolbroker",
        "recoverable": True,
    }
    assert [event.kind for event in runtime["events"].list_for_run("run_1")] == [
        "tool_call_denied"
    ]
    event = runtime["events"].list_for_run("run_1")[0]
    assert event.payload["tool_name"] == "view_image"
    assert event.payload["error_class"] == "config_error"
    runtime["db"].close()


def test_unknown_tool_behavior_is_unchanged(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, "view_video", {"paths": ["video.mp4"]})

    assert result.status == "denied"
    assert result.error["error_class"] == "policy_denied"
    assert result.error["message"] == "Unknown tool: view_video"
    runtime["db"].close()


def test_enabled_ready_view_image_is_activation_gated_until_milestone_6(
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

    result = _invoke(runtime, "view_image", {"paths": ["image.png"]})

    assert result.status == "denied"
    assert result.error["error_class"] == "config_error"
    assert result.error["message"] == (
        "view_image is activation-gated until Phase 2 Milestone 6."
    )
    assert [event.kind for event in runtime["events"].list_for_run("run_1")] == [
        "tool_call_denied"
    ]
    runtime["db"].close()

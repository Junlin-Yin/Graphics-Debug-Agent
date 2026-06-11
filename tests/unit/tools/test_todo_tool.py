from __future__ import annotations

import pytest

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.policy import build_builtin_policy
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker
from debug_agent.tools.runtime_control import tool_definitions


def _runtime(tmp_path, *, approval_mode: str = "normal"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode=approval_mode,
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    context = {
        "workspace_root": str(workspace),
        "approval_mode": approval_mode,
        "policy_facts": build_builtin_policy(workspace),
        "approval_grants": ApprovalGrantStore(db.connection),
        "approval_provider": FakeApprovalProvider("denied"),
        "todo_plan_store": TodoPlanStore(db.connection),
    }
    return {
        "db": db,
        "workspace": workspace,
        "session": session,
        "run": run,
        "events": events,
        "broker": broker,
        "context": context,
    }


def _invoke(runtime, arguments, **context):
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="todo",
        arguments=arguments,
        context={**runtime["context"], **context},
    )


def _event_kinds(runtime) -> list[str]:
    return [event.kind for event in runtime["events"].list_for_run("run_1")]


def test_todo_tool_definition_matches_phase_2_contract() -> None:
    definitions = {definition.name: definition for definition in tool_definitions()}

    todo = definitions["todo"]
    assert todo.category == "runtime_control"
    assert todo.risk_level == "runtime_control"
    assert todo.access == []
    assert todo.input_schema == {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 0,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                        "activeForm": {
                            "type": "string",
                            "description": "Optional present-continuous label.",
                        },
                    },
                    "required": ["content", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"items": "not-list"},
        {"items": [{"content": "x"}]},
        {"items": [{"content": "x", "status": "blocked"}]},
        {"items": [{"content": "x", "status": "pending", "extra": True}]},
        {"items": [{"content": "x", "status": "pending", "activeForm": 3}]},
        {"items": [{"content": "x", "status": "pending"}] * 21},
    ],
)
def test_todo_schema_rejects_invalid_shape_before_routing(tmp_path, arguments) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, arguments)

    assert result.status == "error"
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "tool_schema_invalid"
    assert _event_kinds(runtime) == ["tool_call_failed"]
    event = runtime["events"].list_for_run("run_1")[0]
    assert event.payload["error"]["error_class"] == "tool_error"
    assert event.payload["error"]["reason"] == "tool_schema_invalid"
    assert runtime["context"]["todo_plan_store"].get_current("run_1").version == 0
    runtime["db"].close()


@pytest.mark.parametrize(
    "item",
    [
        {"content": "   ", "status": "pending"},
        {"content": "x" * 241, "status": "pending"},
        {"content": "x", "status": "pending", "activeForm": "  "},
        {"content": "x", "status": "pending", "activeForm": "x" * 121},
        {"content": "x", "status": "in_progress", "activeForm": "  "},
        {"content": "x", "status": "in_progress", "activeForm": "x" * 121},
    ],
)
def test_todo_semantic_validation_denies_invalid_items(tmp_path, item) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, {"items": [item]})

    assert result.status == "error"
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "tool_schema_invalid"
    assert _event_kinds(runtime) == ["tool_call_failed"]
    assert runtime["context"]["todo_plan_store"].get_current("run_1").version == 0
    runtime["db"].close()


def test_todo_rejects_multiple_in_progress_items(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(
        runtime,
        {
            "items": [
                {"content": "One", "status": "in_progress", "activeForm": "Doing one"},
                {"content": "Two", "status": "in_progress", "activeForm": "Doing two"},
            ]
        },
    )

    assert result.status == "error"
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "tool_schema_invalid"
    assert _event_kinds(runtime) == ["tool_call_failed"]
    runtime["db"].close()


@pytest.mark.parametrize("approval_mode", ["normal", "semi-auto", "yolo"])
def test_valid_todo_replaces_plan_without_approval_and_normalizes_result(
    tmp_path, approval_mode: str
) -> None:
    runtime = _runtime(tmp_path, approval_mode=approval_mode)
    provider = FakeApprovalProvider("denied")

    result = _invoke(
        runtime,
        {
            "items": [
                {
                    "content": "  Review docs  ",
                    "status": "completed",
                    "activeForm": "ignored",
                },
                {
                    "content": "Patch tool",
                    "status": "in_progress",
                    "activeForm": "  Patching tool  ",
                },
                {"content": "Run tests", "status": "pending"},
            ]
        },
        approval_provider=provider,
    )

    assert result.status == "ok"
    assert result.output == {
        "plan_version": 1,
        "item_count": 3,
        "counts": {"pending": 1, "in_progress": 1, "completed": 1},
        "items": [
            {"index": 1, "content": "Review docs", "status": "completed"},
            {
                "index": 2,
                "content": "Patch tool",
                "status": "in_progress",
                "activeForm": "Patching tool",
            },
            {"index": 3, "content": "Run tests", "status": "pending"},
        ],
    }
    assert result.metadata == {
        "tool_name": "todo",
        "previous_plan_version": 0,
        "plan_version": 1,
        "mutation": "replace",
        "item_count": 3,
        "counts": {"pending": 1, "in_progress": 1, "completed": 1},
    }
    assert result.redacted_output == (
        "Todo Plan v1: 1 pending, 1 in_progress, 1 completed\n"
        "[o] 1. Review docs\n"
        "[>] 2. Patch tool\n"
        "[ ] 3. Run tests"
    )
    assert provider.requests == []
    assert "approval_requested" not in _event_kinds(runtime)
    assert "approval_decision_recorded" not in _event_kinds(runtime)
    assert _event_kinds(runtime) == [
        "tool_call_started",
        "todo_updated",
        "tool_call_completed",
    ]
    assert runtime["db"].connection.execute(
        "SELECT COUNT(*) FROM approval_grants"
    ).fetchone()[0] == 0
    runtime["db"].close()


def test_todo_clear_returns_explicit_empty_rendering(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    _invoke(runtime, {"items": [{"content": "Start", "status": "pending"}]})

    result = _invoke(runtime, {"items": []})

    assert result.status == "ok"
    assert result.output == {
        "plan_version": 2,
        "item_count": 0,
        "counts": {"pending": 0, "in_progress": 0, "completed": 0},
        "items": [],
    }
    assert result.metadata["previous_plan_version"] == 1
    assert result.metadata["plan_version"] == 2
    assert result.redacted_output == "Todo Plan v2: empty"
    runtime["db"].close()


def test_todo_redacted_output_compacts_completed_and_later_pending_items(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")

    result = _invoke(
        runtime,
        {
            "items": [
                {"content": "Done one", "status": "completed"},
                {"content": "Pending first", "status": "pending"},
                {"content": "Done three", "status": "completed"},
                {"content": "Pending second", "status": "pending"},
                {
                    "content": "Current work",
                    "status": "in_progress",
                    "activeForm": "Doing current work",
                },
                {"content": "Done six", "status": "completed"},
                {"content": "Pending third", "status": "pending"},
                {"content": "Pending fourth", "status": "pending"},
                {"content": "Pending fifth", "status": "pending"},
            ]
        },
    )

    assert result.output["items"] == [
        {"index": 1, "content": "Done one", "status": "completed"},
        {"index": 2, "content": "Pending first", "status": "pending"},
        {"index": 3, "content": "Done three", "status": "completed"},
        {"index": 4, "content": "Pending second", "status": "pending"},
        {
            "index": 5,
            "content": "Current work",
            "status": "in_progress",
            "activeForm": "Doing current work",
        },
        {"index": 6, "content": "Done six", "status": "completed"},
        {"index": 7, "content": "Pending third", "status": "pending"},
        {"index": 8, "content": "Pending fourth", "status": "pending"},
        {"index": 9, "content": "Pending fifth", "status": "pending"},
    ]
    assert result.redacted_output == (
        "Todo Plan v1: 5 pending, 1 in_progress, 3 completed\n"
        "[o] (steps 1, 3, 6 done)\n"
        "[ ] 2. Pending first\n"
        "[ ] 4. Pending second\n"
        "[>] 5. Current work\n"
        "[ ] 7. Pending third\n"
        "[ ] (steps 8-9 pending)"
    )
    assert len(result.redacted_output.splitlines()) == 7
    runtime["db"].close()

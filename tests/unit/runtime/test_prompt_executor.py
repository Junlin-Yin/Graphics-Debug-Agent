from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.adapters.model_factory import FakeChatModel
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.conversation import ConversationAppend, ConversationStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.context_manager import ContextManager
from debug_agent.runtime.model_context import ConversationMessage
from debug_agent.runtime.model_context import TokenEstimator
from debug_agent.runtime.orchestrator import ReplRuntime
from debug_agent.runtime.prompt_executor import PromptAgentExecutor, _with_compression_usage
from debug_agent.runtime.query_control import QueryControlPlane
from debug_agent.runtime.settings import SYSTEM_PROMPT
from debug_agent.runtime.stream_events import AgentStreamEvent
from debug_agent.tools.broker import ToolBroker
from debug_agent.tools.native import tool_definitions
from debug_agent.tools.broker import ApprovalDecision


def test_compression_usage_switches_provider_result_window_to_estimated() -> None:
    result = AgentRunResult(
        status="completed",
        assistant_output="answer",
        tool_results=[],
        usage={"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
        error=None,
        metadata={
            "provider_usage_available": True,
            "token_source": "provider",
            "estimated_usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        },
    )

    merged = _with_compression_usage(
        result,
        {"input_tokens": 7, "output_tokens": 11, "total_tokens": 18},
    )

    assert merged.usage == {"input_tokens": 9, "output_tokens": 14, "total_tokens": 23}
    assert merged.metadata["provider_usage_available"] is False
    assert merged.metadata["token_source"] == "estimated"
    assert merged.metadata["estimated_usage"] == {
        "input_tokens": 9,
        "output_tokens": 14,
        "total_tokens": 23,
        "estimator_version": "deterministic-char-v1",
    }


def _runtime(tmp_path, model):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 30},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    adapter = LangChainAgentLoopAdapter(model=model, tool_broker=broker)
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
    )
    return (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        executor,
    )


def _with_durable_history(
    *,
    store: ConversationStore,
    session_id: str,
    run_id: str,
    conversation: list[dict],
) -> list[dict]:
    durable_conversation: list[dict] = []
    for offset, message in enumerate(conversation, start=1):
        durable_kind = (
            "assistant_tool_call"
            if message.get("kind") == "tool_call"
            else str(message.get("kind") or "assistant_output")
        )
        rows = store.append_closed_group(
            session_id=session_id,
            run_id=run_id,
            messages=[
                ConversationAppend(
                    turn_id=str(message.get("turn_id") or f"turn-fixture-{offset}"),
                    message_group_id=f"fixture-{offset}",
                    model_call_id=message.get("model_call_id"),
                    group_position=0,
                    group_row_count=1,
                    role=str(message.get("role") or "assistant"),
                    kind=durable_kind,
                    content={"content": message.get("content", "")},
                    tool_call_id=message.get("tool_call_id"),
                    artifact_id=None,
                    metadata=dict(message.get("metadata", {})),
                )
            ],
        )
        retained = dict(message)
        retained["durable_message_index"] = rows[0].message_index
        durable_conversation.append(retained)
    return durable_conversation


def _compression_session(session):
    return type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 420,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )


def _compression_conversation() -> list[dict]:
    return [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output " * 40,
            "estimated_tokens": 120,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "consumed old",
            "estimated_tokens": 8,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]


def test_prompt_executor_appends_accepted_user_and_assistant_durable_messages(
    tmp_path,
) -> None:
    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="assistant answer"))

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )
    rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )

    assert result.status == "completed"
    assert [(row.message_index, row.role, row.kind, row.content) for row in rows] == [
        (1, "user", "user_input", {"content": "hello"}),
        (2, "assistant", "assistant_output", {"content": "assistant answer"}),
    ]
    assert ConversationStore(db.connection, artifact_store=artifacts).get_projection(
        run.run_id
    ).source_high_watermark == 2
    db.close()


def test_prompt_executor_retries_provider_timeout_before_accepting_result(
    tmp_path,
) -> None:
    class RetryableAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, context):
            self.calls += 1
            if self.calls == 1:
                return AgentRunResult(
                    status="failed",
                    assistant_output=None,
                    tool_results=[],
                    usage={},
                    error={
                        "error_class": "model_error",
                        "reason": "provider_timeout",
                        "message": "provider timed out",
                    },
                    metadata={},
                )
            return AgentRunResult(
                status="completed",
                assistant_output="retried answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = RetryableAdapter()
    executor = type(executor)(
        **{**executor.__dict__, "adapter": adapter}
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )
    rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )

    assert adapter.calls == 2
    assert result.status == "completed"
    assert result.assistant_output == "retried answer"
    assert [(row.role, row.kind, row.content) for row in rows] == [
        ("user", "user_input", {"content": "hello"}),
        ("assistant", "assistant_output", {"content": "retried answer"}),
    ]
    assert result.metadata["retry"]["attempts"][0]["strategy"] == "repeat_call"
    assert result.metadata["retry"]["attempts"][0]["reason"] == "provider_timeout"
    db.close()


def test_prompt_executor_repeat_call_exhaustion_records_resulting_error(tmp_path) -> None:
    class ExhaustingAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, context):
            self.calls += 1
            return AgentRunResult(
                status="failed",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "error_class": "model_error",
                    "reason": "provider_timeout",
                    "message": "provider timed out",
                },
                metadata={},
            )

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = ExhaustingAdapter()
    executor = type(executor)(**{**executor.__dict__, "adapter": adapter})

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    assert adapter.calls == 3
    assert result.status == "failed"
    assert result.metadata["retry"]["exhausted"] is True
    assert result.metadata["retry"]["resulting_error_class"] == "model_error"
    assert result.metadata["retry"]["resulting_reason"] == "provider_timeout"
    db.close()


def test_prompt_executor_continues_text_only_output_token_limit_before_durable_append(
    tmp_path,
) -> None:
    class ContinuationAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, context):
            self.calls += 1
            if self.calls == 1:
                return AgentRunResult(
                    status="completed",
                    assistant_output="partial ",
                    tool_results=[],
                    usage={},
                    error=None,
                    metadata={
                        "provider_finish": {
                            "finish_reason": "max_tokens",
                            "output_token_limit_reached": True,
                        }
                    },
                )
            return AgentRunResult(
                status="completed",
                assistant_output="continued",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = ContinuationAdapter()
    executor = type(executor)(
        **{**executor.__dict__, "adapter": adapter}
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
    )
    rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )

    assert adapter.calls == 2
    assert result.status == "completed"
    assert result.assistant_output == "partial continued"
    assert [(row.role, row.kind, row.content) for row in rows] == [
        ("user", "user_input", {"content": "continue"}),
        ("assistant", "assistant_output", {"content": "partial continued"}),
    ]
    assert result.metadata["retry"]["attempts"][0]["strategy"] == "continue_generation"
    assert result.metadata["retry"]["attempts"][0]["partial_output_kind"] == (
        "text_only_no_tool_fragment"
    )
    db.close()


def test_prompt_executor_rejects_output_token_limit_partial_tool_fragment(
    tmp_path,
) -> None:
    class ToolFragmentAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, context):
            self.calls += 1
            return AgentRunResult(
                status="completed",
                assistant_output="partial",
                tool_results=[{"status": "ok"}],
                usage={},
                error=None,
                metadata={
                    "provider_finish": {
                        "finish_reason": "max_tokens",
                        "output_token_limit_reached": True,
                    }
                },
            )

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = ToolFragmentAdapter()
    executor = type(executor)(**{**executor.__dict__, "adapter": adapter})

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
    )
    rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )

    assert adapter.calls == 1
    assert result.status == "failed"
    assert result.error["reason"] == "output_token_limit_reached"
    assert result.metadata["retry"]["exhausted"] is True
    assert result.metadata["partial_output_kind"] == "tool_fragment"
    assert [row.kind for row in rows] == ["user_input", "failure_fact"]
    db.close()


def test_prompt_executor_rejects_continuation_tool_fragment_without_accepting_partial(
    tmp_path,
) -> None:
    class ContinuationToolFragmentAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, context):
            self.calls += 1
            if self.calls == 1:
                return AgentRunResult(
                    status="completed",
                    assistant_output="partial ",
                    tool_results=[],
                    usage={},
                    error=None,
                    metadata={
                        "provider_finish": {
                            "finish_reason": "max_tokens",
                            "output_token_limit_reached": True,
                        }
                    },
                )
            return AgentRunResult(
                status="completed",
                assistant_output="tool-ish",
                tool_results=[{"status": "ok"}],
                usage={},
                error=None,
                metadata={},
            )

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = ContinuationToolFragmentAdapter()
    executor = type(executor)(**{**executor.__dict__, "adapter": adapter})

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
    )
    rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )

    assert adapter.calls == 2
    assert result.status == "failed"
    assert result.error["reason"] == "output_token_limit_reached"
    assert result.metadata["retry"]["exhausted"] is True
    assert result.metadata["retry"]["resulting_error_class"] == "model_error"
    assert result.metadata["retry"]["resulting_reason"] == "output_token_limit_reached"
    assert [row.kind for row in rows] == ["user_input", "failure_fact"]
    db.close()


def test_prompt_executor_routes_retry_result_token_limit_through_continuation(
    tmp_path,
) -> None:
    class TimeoutThenContinuationAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request, context):
            self.calls += 1
            if self.calls == 1:
                return AgentRunResult(
                    status="failed",
                    assistant_output=None,
                    tool_results=[],
                    usage={},
                    error={
                        "error_class": "model_error",
                        "reason": "provider_timeout",
                        "message": "provider timed out",
                    },
                    metadata={},
                )
            if self.calls == 2:
                return AgentRunResult(
                    status="completed",
                    assistant_output="partial ",
                    tool_results=[],
                    usage={},
                    error=None,
                    metadata={
                        "provider_finish": {
                            "finish_reason": "max_tokens",
                            "output_token_limit_reached": True,
                        }
                    },
                )
            return AgentRunResult(
                status="completed",
                assistant_output="continued",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = TimeoutThenContinuationAdapter()
    executor = type(executor)(**{**executor.__dict__, "adapter": adapter})

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
    )
    rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )

    assert adapter.calls == 3
    assert result.status == "completed"
    assert result.assistant_output == "partial continued"
    assert [(row.role, row.kind, row.content) for row in rows] == [
        ("user", "user_input", {"content": "continue"}),
        ("assistant", "assistant_output", {"content": "partial continued"}),
    ]
    assert [attempt["strategy"] for attempt in result.metadata["retry"]["attempts"]] == [
        "repeat_call",
        "continue_generation",
    ]
    db.close()


def test_prompt_executor_appends_accepted_tool_call_and_tool_result_messages(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "notes say hello", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
    )
    store = ConversationStore(db.connection, artifact_store=artifacts)
    rows = store.list_messages(run.run_id)

    assert result.status == "completed"
    assert [row.kind for row in rows] == [
        "user_input",
        "assistant_tool_call",
        "tool_result",
        "assistant_output",
    ]
    assert rows[1].tool_call_id == "turn-1:model_call_1_tool_1"
    assert rows[2].tool_call_id == "turn-1:model_call_1_tool_1"
    assert rows[2].content == {
        "message_type": "tool_result",
        "tool_name": "",
        "tool_call_id": "turn-1:model_call_1_tool_1",
        "status": "ok",
        "content": {
            "path": str((workspace / "notes.txt").resolve()),
            "content": "hello",
            "offset": 0,
            "limit": 2000,
            "total_returned": 1,
            "truncated": False,
            "next_offset": None,
            "sha256": sha256(b"hello").hexdigest(),
            "bytes": 5,
        },
        "error": None,
        "artifact_ids": [],
        "metadata": {},
    }
    assert store.validate_fact_cut(run_id=run.run_id, highest_message_index=4).message_count == 4
    db.close()


def test_prompt_executor_fails_closed_on_projection_drift_before_ordinary_turn(
    tmp_path,
) -> None:
    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="assistant answer"))
    store = ConversationStore(db.connection, artifact_store=artifacts)
    store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            ConversationAppend(
                turn_id="turn-previous",
                message_group_id="turn-previous:user",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="user",
                kind="user_input",
                content={"content": "previous"},
            )
        ],
    )

    try:
        executor.run_turn(
            session=session,
            run=run,
            user_input="hello",
            workspace_root=str(workspace),
            conversation=[],
        )
    except Exception as exc:
        assert "drifted from durable projection" in str(exc)
    else:
        raise AssertionError("ordinary prompt execution must fail closed on projection drift")
    db.close()


def _provider_message_content(message: object) -> str:
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", ""))


def test_prompt_executor_writes_model_events_assistant_event_without_turn_checkpoint(
    tmp_path,
) -> None:
    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="assistant answer"))

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    persisted_events = events.list_for_run(run.run_id)
    latest_checkpoint = checkpoints.latest_for_run(run.run_id)
    assert result.assistant_output == "assistant answer"
    assert [event.kind for event in persisted_events] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "assistant_message",
    ]
    assert persisted_events[2].payload["duration"] >= 0
    assert persisted_events[2].payload["content"] == "assistant answer"
    assert persisted_events[2].payload["tool_calls"] == []
    assert persisted_events[2].payload["artifact_ids"] == []
    assert persisted_events[2].payload["redacted_output"] is None
    assert latest_checkpoint is None
    assert sessions.get(session.session_id).status == "running"
    assert runs.get(run.run_id).status == "running"
    assert sessions.get(session.session_id).latest_checkpoint_id is None
    assert runs.get(run.run_id).latest_checkpoint_id is None
    db.close()


def test_prompt_executor_records_model_completion_before_react_tool_events(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "notes say hello", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
    )

    assert result.assistant_output == "notes say hello"
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "tool_call_started",
        "tool_call_completed",
        "model_call_started",
        "model_call_completed",
        "assistant_message",
    ]
    first_model_completed = events.list_for_run(run.run_id)[2]
    assert first_model_completed.payload["content"] == ""
    assert first_model_completed.payload["tool_calls"] == [
        {
            "id": "model_call_1_tool_1",
            "name": "read_file",
            "args": {"path": "notes.txt"},
            "provider_tool_call_id": "read_file_0",
        }
    ]
    db.close()


def test_repl_runtime_denial_history_allows_next_turn_model_call(tmp_path) -> None:
    class DenyApprovalProvider:
        is_interactive = True

        def request_approval(self, request, metadata):
            return ApprovalDecision(decision="denied", grant_scope="none")

    class DenyThenInspectModel:
        def __init__(self) -> None:
            self.calls = 0
            self.next_turn_messages = None

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "write_file_0",
                                "name": "write_file",
                                "args": {"path": "notes.txt", "content": "hello"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            self.next_turn_messages = messages
            from langchain_core.messages import ToolMessage, convert_to_messages

            converted = convert_to_messages(messages)
            tool_messages = [
                message for message in converted if isinstance(message, ToolMessage)
            ]
            assert tool_messages
            assert tool_messages[-1].tool_call_id == "model_call_1_tool_1"
            return type(
                "Response",
                (),
                {"content": "denial was visible", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, DenyThenInspectModel())
    session = sessions.update_approval_mode(session.session_id, "normal")
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )
    runtime.set_approval_provider(DenyApprovalProvider())

    denied = runtime.run_turn("write notes")
    next_turn = runtime.run_turn("what happened?")

    assert denied.metadata["approval_denied_abort"] is True
    assert denied.metadata["continuation_history"] == [
        "initial_model_call",
        "approval_denied_abort",
    ]
    assert denied.metadata["query_state"]["continuation_reason"] == (
        "approval_denied_abort"
    )
    assert next_turn.status == "completed"
    assert next_turn.assistant_output == "denial was visible"
    db.close()


def test_prompt_executor_converts_approval_unblock_after_cancel_to_running_cancellation(
    tmp_path,
) -> None:
    class CancellationToken:
        def __init__(self) -> None:
            self.cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    class DenyApprovalProvider:
        is_interactive = True

        def __init__(self, token: CancellationToken) -> None:
            self.token = token

        def request_approval(self, request, metadata):
            self.token.cancelled = True
            return ApprovalDecision(
                decision="denied",
                grant_scope="none",
                message="Turn cancelled by user.",
            )

    class ToolCallModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return type(
                "Response",
                (),
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write_file_0",
                            "name": "write_file",
                            "args": {"path": "notes.txt", "content": "hello"},
                        }
                    ],
                    "usage": {},
                },
            )()

    (
        _workspace,
        db,
        sessions,
        _runs,
        _events,
        _checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolCallModel())
    session = sessions.update_approval_mode(session.session_id, "normal")

    token = CancellationToken()
    result = executor.run_turn(
        session=session,
        run=run,
        user_input="write notes",
        workspace_root=str(tmp_path / "workspace"),
        approval_provider=DenyApprovalProvider(token),
        cancellation_token=token,
    )
    messages = ConversationStore(db.connection).list_messages(run.run_id)

    assert result.status == "cancelled"
    assert result.error["reason"] == "user_cancel_running"
    assert [(message.role, message.kind) for message in messages] == [
        ("user", "user_input"),
        ("assistant", "assistant_tool_call"),
        ("tool", "tool_result"),
        ("runtime", "cancellation_fact"),
    ]
    assert messages[2].tool_call_id == "model_call_1_tool_1"
    assert messages[2].content["message_type"] == "tool_result"
    assert messages[2].content["tool_call_id"] == "model_call_1_tool_1"
    assert messages[2].content["error"]["error_class"] == "cancelled"
    assert messages[2].content["error"]["reason"] == "tool_call_cancelled"
    assert messages[-1].content["reason"] == "user_cancel_running"
    db.close()


def test_prompt_executor_reuses_session_approval_grant_for_same_tool_scope(
    tmp_path,
) -> None:
    class SessionApprovalProvider:
        is_interactive = True

        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request, metadata):
            self.requests.append((request, metadata))
            return ApprovalDecision(
                decision="approved_for_session",
                grant_scope="session",
            )

    class RepeatWriteModel:
        def __init__(self) -> None:
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls in {1, 2}:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"write_file_{self.calls}",
                                "name": "write_file",
                                "args": {
                                    "path": "notes.txt",
                                    "content": f"hello {self.calls}",
                                },
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        sessions,
        _runs,
        events,
        _checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, RepeatWriteModel())
    session = sessions.update_approval_mode(session.session_id, "normal")
    approval_provider = SessionApprovalProvider()

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="write twice",
        workspace_root=str(workspace),
        approval_provider=approval_provider,
    )

    assert result.status == "completed"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "hello 2"
    assert len(approval_provider.requests) == 1
    assert db.connection.execute(
        "SELECT COUNT(*) FROM approval_grants WHERE decision = 'approved_for_session'"
    ).fetchone()[0] == 1
    assert [event.kind for event in events.list_for_run(run.run_id)].count(
        "approval_requested"
    ) == 1
    db.close()


def test_tool_loop_followup_records_new_context_estimate_before_second_call(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0
            self.message_counts = []

        def invoke(self, messages):
            self.calls += 1
            self.message_counts.append(len(messages))
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "notes say hello", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
    )

    query_state = result.metadata["query_state"]
    initial_estimate = result.metadata["context_estimate_history"][0]
    followup_estimate = result.metadata["context_estimate"]
    assert result.status == "completed"
    assert result.metadata["continuation_history"] == [
        "initial_model_call",
        "tool_result_continuation",
        "final_assistant_response",
    ]
    assert followup_estimate["total_tokens"] > initial_estimate["total_tokens"]
    assert query_state["latest_context_estimate"]["total_tokens"] == (
        followup_estimate["total_tokens"]
    )
    assert query_state["continuation_reason"] == "final_assistant_response"
    db.close()


def test_tool_loop_followup_runs_omission_before_second_model_call(tmp_path) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def invoke(self, messages):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    executor = replace(executor, system_prompt="system")
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1500,
                    "omit_old_tool_results_at_ratio": 0.9,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "Inspect old output.",
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "tool_call",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": "read_file",
        },
        {
            "seq": 3,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": "full old tool output " * 20,
            "artifact_refs": ["art_old"],
        },
        {
            "seq": 4,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]
    conversation = _with_durable_history(
        store=ConversationStore(db.connection, artifact_store=artifacts),
        session_id=session.session_id,
        run_id=run.run_id,
        conversation=conversation,
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    first_call_text = "\n".join(
        _provider_message_content(message)
        for message in executor.adapter.model.messages_by_call[0]
    )
    second_call_text = "\n".join(
        _provider_message_content(message)
        for message in executor.adapter.model.messages_by_call[1]
    )
    assert result.status == "completed"
    assert "full old tool output" in first_call_text
    assert marker not in first_call_text
    assert marker in second_call_text
    assert "full old tool output" not in second_call_text
    assert result.metadata["context_optimization"]["trigger"] == "omission"
    assert result.metadata["conversation_writeback"][2]["content"] == marker
    assert runs.get(run.run_id).context_snapshot_id is None
    assert checkpoints.list_for_session(session.session_id) == []
    assert [event.kind for event in events.list_for_run(run.run_id)].count(
        "context_optimized"
    ) == 1
    db.close()


def test_tool_loop_followup_runs_compression_before_second_model_call(tmp_path) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def invoke(self, messages):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    compression_calls = 0

    def compression_model(_frame):
        nonlocal compression_calls
        compression_calls += 1
        return json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["compressed before tool follow-up"],
                "inspected_or_modified_files": [],
                "remaining_work": [],
                "next_plan": [],
                "key_decisions": [],
                "constraints": [],
            }
        )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        _checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 2000,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.25,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=CheckpointStore(db.connection),
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(
            model=ToolLoopModel(),
            tool_broker=ToolBroker(event_writer=events, artifact_store=artifacts),
        ),
        tool_definitions=tool_definitions(),
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "old output " * 12,
            "estimated_tokens": 40,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]
    conversation = _with_durable_history(
        store=ConversationStore(db.connection, artifact_store=artifacts),
        session_id=session.session_id,
        run_id=run.run_id,
        conversation=conversation,
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    second_call_text = "\n".join(
        _provider_message_content(message)
        for message in executor.adapter.model.messages_by_call[1]
    )
    assert result.status == "completed"
    assert compression_calls == 1
    assert "old output " * 4 not in second_call_text
    assert "compressed before tool follow-up" in second_call_text
    assert result.metadata["context_optimization"]["trigger"] == "compression"
    assert [event.kind for event in events.list_for_run(run.run_id)].count(
        "context_optimized"
    ) == 1
    db.close()


def test_tool_loop_followup_compression_failure_preserves_boundary(tmp_path) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            raise AssertionError("ordinary follow-up model call should not run")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1000,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.7,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(
            model=ToolLoopModel(),
            tool_broker=ToolBroker(event_writer=events, artifact_store=artifacts),
        ),
        tool_definitions=tool_definitions(),
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: "",
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "old output " * 12,
            "estimated_tokens": 40,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]
    conversation = _with_durable_history(
        store=ConversationStore(db.connection, artifact_store=artifacts),
        session_id=session.session_id,
        run_id=run.run_id,
        conversation=conversation,
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    assert result.status == "failed"
    assert result.error["error_class"] == "compression_failed"
    assert result.metadata["conversation_writeback"] is not None
    assert [message["content"] for message in result.metadata["conversation_writeback"]] == [
        message["content"] for message in conversation
    ]
    assert all(
        message["kind"] != "context_summary"
        for message in result.metadata["conversation_writeback"]
    )
    persisted = events.list_for_run(run.run_id)
    assert "compression_failed" in [event.kind for event in persisted]
    assert "assistant_message" not in [event.kind for event in persisted]
    assert checkpoints.list_for_session(session.session_id) == []
    assert runs.get(run.run_id).status == "running"
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    db.close()


def test_tool_loop_followup_omission_then_compression_failure_does_not_write_back_omission(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            raise AssertionError("ordinary follow-up model call should not run")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1500,
                    "omit_old_tool_results_at_ratio": 0.95,
                    "compress_history_at_ratio": 0.7,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(
            model=ToolLoopModel(),
            tool_broker=ToolBroker(event_writer=events, artifact_store=artifacts),
        ),
        tool_definitions=tool_definitions(),
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: "",
    )
    long_tool_output = "full old tool output " * 20
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "old output",
            "estimated_tokens": 10,
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": long_tool_output,
            "artifact_refs": ["art_old"],
            "estimated_tokens": 60,
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    assert result.status == "failed"
    assert result.error["error_class"] == "compression_failed"
    writeback_contents = [
        message["content"] for message in result.metadata["conversation_writeback"]
    ]
    assert marker not in writeback_contents
    assert long_tool_output in writeback_contents
    assert checkpoints.list_for_session(session.session_id) == []
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    assert "compression_failed" in [
        event.kind for event in events.list_for_run(run.run_id)
    ]
    db.close()


def test_tool_loop_current_buffer_seq_does_not_protect_retained_tool_result(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def invoke(self, messages):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 2000,
                    "omit_old_tool_results_at_ratio": 0.4,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    conversation = [
        {
            "seq": 1,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": "full old tool output " * 20,
            "artifact_refs": ["art_old"],
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]
    conversation = _with_durable_history(
        store=ConversationStore(db.connection, artifact_store=artifacts),
        session_id=session.session_id,
        run_id=run.run_id,
        conversation=conversation,
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    second_call_text = "\n".join(
        _provider_message_content(message)
        for message in executor.adapter.model.messages_by_call[1]
    )
    assert result.status == "completed"
    assert marker in second_call_text
    assert "full old tool output" not in second_call_text
    assert result.metadata["conversation_writeback"][0]["content"] == marker
    db.close()


def test_prompt_executor_writes_large_model_response_to_text_artifact(
    tmp_path,
) -> None:
    large_response = "x" * (16 * 1024 + 1)
    (
        workspace,
        db,
        _sessions,
        _runs,
        events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response=large_response))

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    persisted_events = events.list_for_run(run.run_id)
    model_completed = next(
        event for event in persisted_events if event.kind == "model_call_completed"
    )
    artifact_id = model_completed.payload["artifact_ids"][0]
    assert result.assistant_output == large_response
    assert [event.kind for event in persisted_events[:4]] == [
        "user_message",
        "model_call_started",
        "artifact_registered",
        "model_call_completed",
    ]
    assert model_completed.payload["content"] is None
    assert model_completed.payload["redacted_output"].startswith(
        "[model response stored as artifact:"
    )
    assert artifacts.resolve_path(artifact_id).read_text(encoding="utf-8") == large_response
    metadata = artifacts.get(artifact_id).metadata
    assert metadata["bytes"] == 16 * 1024 + 1
    assert metadata["event_kind"] == "model_call_completed"
    assert metadata["payload_sha256"].startswith("sha256:")
    db.close()


def test_prompt_executor_passes_session_timeout_to_adapter_request(tmp_path) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.timeout_seconds = None

        def run(self, request, context):
            self.timeout_seconds = request.timeout_seconds
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    assert result.status == "completed"
    assert adapter.timeout_seconds == 7
    db.close()


def test_prompt_executor_passes_estimated_model_context_frame_identity_to_adapter(
    tmp_path,
) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="semi-auto",
        config_snapshot={"provider": "fake", "model": "fake-model", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    assert result.status == "completed"
    frame = adapter.request.model_context_frame
    assert frame is not None
    assert adapter.request.system_prompt == ""
    assert adapter.request.conversation == []
    assert adapter.request.user_input == ""
    assert adapter.request.tools == []
    estimate = TokenEstimator().estimate_model_context_frame(frame)
    assert result.metadata["context_estimate"] == estimate.to_dict()
    assert result.metadata["query_state"]["latest_context_estimate"] == {
        "total_tokens": estimate.total_tokens,
        "estimator_version": estimate.estimator_version,
    }
    assert result.metadata["query_state"]["continuation_reason"] == "final_assistant_response"
    assert result.metadata["query_state"]["latest_model_context_frame"] is frame
    db.close()


def test_prompt_executor_injects_todo_plan_from_store_without_persisting_to_conversation(
    tmp_path,
) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "model": "fake-model"},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    todo_store = TodoPlanStore(db.connection)
    todo_store.replace_plan(
        session.session_id,
        run.run_id,
        [{"content": "Persisted plan item", "status": "in_progress"}],
        events,
    )
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=todo_store,
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "content": "A summary says a different plan exists.",
        }
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    frame = adapter.request.model_context_frame
    todo_segments = [
        segment
        for segment in frame.ordered_message_segments()
        if segment.kind == "runtime_todo_plan"
    ]
    assert result.status == "completed"
    assert len(todo_segments) == 1
    assert "Persisted plan item" in str(todo_segments[0].content)
    assert "different plan" not in str(todo_segments[0].content)
    assert not any(
        message.kind == "runtime_todo_plan"
        for message in result.metadata.get("conversation_writeback", [])
    )
    db.close()


def test_prompt_executor_uses_stream_path_when_callback_is_supplied(tmp_path) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.run_called = False
            self.stream_called = False

        def run(self, request, context):
            self.run_called = True
            raise AssertionError("run should not be called when stream callback is supplied")

        def stream(self, request, context, on_event):
            self.stream_called = True
            on_event(
                AgentStreamEvent(
                    kind="stream_text_delta",
                    payload={"model_call_id": "model_1", "text": "answer"},
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
    )
    stream_events = []

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
        agent_stream_callback=stream_events.append,
    )

    assert result.status == "completed"
    assert adapter.stream_called is True
    assert adapter.run_called is False
    assert stream_events[0].kind == "stream_context_estimate_updated"
    assert stream_events[0].payload["context_estimate"] == result.metadata[
        "context_estimate"
    ]
    assert stream_events[1:] == [
        AgentStreamEvent(
            kind="stream_text_delta",
            payload={"model_call_id": "model_1", "text": "answer"},
        )
    ]
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "user_message",
        "assistant_message",
    ]
    db.close()


def test_prompt_executor_streams_context_estimate_before_adapter_call(tmp_path) -> None:
    class RecordingAdapter:
        def run(self, request, context):
            raise AssertionError("run should not be called")

        def stream(self, request, context, on_event):
            assert stream_events
            assert stream_events[0].kind == "stream_context_estimate_updated"
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_completed",
                    payload={
                        "model_call_id": "model_1",
                        "is_final": True,
                        "usage": {},
                        "duration_ms": 1,
                    },
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
    )
    stream_events = []

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
        agent_stream_callback=stream_events.append,
    )

    assert result.status == "completed"
    estimate = result.metadata["context_estimate"]
    assert stream_events[0].payload["context_estimate"] == estimate
    assert stream_events[1].payload["context_estimate"] == estimate
    db.close()


def test_prompt_executor_does_not_persist_agent_stream_events(tmp_path) -> None:
    class StreamingAdapter:
        def run(self, request, context):
            raise AssertionError("run should not be called")

        def stream(self, request, context, on_event):
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_started",
                    payload={"model_call_id": "model_1"},
                )
            )
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_completed",
                    payload={
                        "model_call_id": "model_1",
                        "is_final": True,
                        "usage": {},
                        "duration_ms": 1,
                    },
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=StreamingAdapter(),
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
    )

    executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
        agent_stream_callback=lambda _event: None,
    )

    persisted_kinds = [event.kind for event in events.list_for_run(run.run_id)]
    assert "stream_model_call_started" not in persisted_kinds
    assert "stream_model_call_completed" not in persisted_kinds
    assert persisted_kinds == [
        "user_message",
        "assistant_message",
    ]
    db.close()


def test_prompt_executor_writes_failed_model_event_without_error_checkpoint(tmp_path) -> None:
    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(error=RuntimeError("provider failed")))

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    latest_checkpoint = checkpoints.latest_for_run(run.run_id)
    assert result.status == "failed"
    assert result.error["error_class"] == "model_error"
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "user_message",
        "model_call_started",
        "model_call_failed",
    ]
    failed_event = events.list_for_run(run.run_id)[2]
    assert failed_event.payload["error"] == {
        "schema_version": 1,
        "error_class": "model_error",
        "reason": "model_call_failed",
        "message": "provider failed",
        "scope": "provider",
        "recoverability": "non_recoverable",
        "metadata": {"purpose": "main"},
        "artifact_ids": [],
    }
    assert failed_event.payload["error_class"] == "model_error"
    assert failed_event.payload["message"] == "provider failed"
    assert failed_event.payload["source"] == "model"
    assert failed_event.payload["recoverable"] is True
    assert failed_event.payload["duration"] >= 0
    assert latest_checkpoint is None
    assert sessions.get(session.session_id).status == "running"
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_prompt_executor_omits_old_tool_results_and_persists_context_snapshot(
    tmp_path,
) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 500,
                    "omit_old_tool_results_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    long_output = "full old tool output " * 80
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "I will inspect",
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "tool_call",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "read_file",
        },
        {
            "seq": 3,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": long_output,
            "artifact_refs": ["art_full"],
            "metadata": {"path": "old.log"},
        },
        {
            "seq": 4,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed older result.",
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    sent_contents = [
        segment.content
        for segment in adapter.request.model_context_frame.ordered_message_segments()
    ]
    assert result.status == "completed"
    assert marker in sent_contents
    assert long_output not in sent_contents
    assert result.metadata["context_optimization"] == {
        "message": "Context optimized: reduced from "
        + f"{result.metadata['context_optimization']['reduced_from_tokens']} to "
        + f"{result.metadata['context_optimization']['reduced_to_tokens']} tokens "
        + "by omitting earlier tool results.",
        "omitted_tool_result_count": 1,
        "reduced_from_tokens": result.metadata["context_estimate_history"][0]["total_tokens"],
        "reduced_to_tokens": result.metadata["context_estimate"]["total_tokens"],
        "trigger": "omission",
    }
    assert result.metadata["context_optimization"]["reduced_to_tokens"] < (
        result.metadata["context_optimization"]["reduced_from_tokens"]
    )

    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    assert runs.get(run.run_id).context_snapshot_id is None
    assert checkpoints.list_for_session(session.session_id) == []
    assert checkpoints.latest_for_run(run.run_id) is None
    context_events = [event for event in events.list_for_run(run.run_id) if event.kind == "context_optimized"]
    assert "context_snapshot_id" not in context_events[0].payload
    assert "checkpoint_id" not in context_events[0].payload
    assert context_events[0].payload["reduced_to_tokens"] == (
        result.metadata["context_estimate"]["total_tokens"]
    )
    db.close()


def test_prompt_executor_automatically_compresses_before_initial_model_call(
    tmp_path,
) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    compression_frames = []

    def compression_model(frame):
        compression_frames.append(frame)
        return json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["read old output"],
                "inspected_or_modified_files": ["old.py"],
                "remaining_work": ["finish fix"],
                "next_plan": ["run unit tests"],
                "key_decisions": ["keep snapshots non-authoritative"],
                "constraints": ["no manual compression"],
                "visible_artifact_refs": ["art_old"],
            }
        )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=adapter,
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 420,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output " * 40,
            "estimated_tokens": 120,
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "artifact art_old",
            "artifact_refs": ["art_old"],
            "estimated_tokens": 8,
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "consumed old call",
            "estimated_tokens": 8,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    sent_contents = [
        segment.content
        for segment in adapter.request.model_context_frame.ordered_message_segments()
    ]
    assert result.status == "completed"
    assert len(compression_frames) == 1
    assert [message.seq for message in compression_frames[0].evicted_messages] == [1, 2]
    assert "old output " * 4 not in sent_contents
    assert any('"task_goal":"continue debugging"' in content for content in sent_contents)
    assert result.metadata["context_optimization"]["trigger"] == "compression"
    assert result.metadata["context_optimization"]["evicted_model_call_group_count"] == 1
    assert result.metadata["conversation_writeback"][0]["kind"] == "context_summary"
    assert result.metadata["conversation_writeback"][0]["role"] == "runtime"

    persisted_kinds = [event.kind for event in events.list_for_run(run.run_id)]
    assert persisted_kinds[:4] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "context_optimized",
    ]
    assert events.list_for_run(run.run_id)[1].payload["purpose"] == "compression"
    assert events.list_for_run(run.run_id)[1].payload["tool_schema_bindings"] == []
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    assert checkpoints.list_for_session(session.session_id) == []
    db.close()


def test_prompt_executor_retries_transient_compression_model_failure(tmp_path) -> None:
    class RecordingAdapter:
        def run(self, request, context):
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    attempts = 0

    def compression_model(_frame):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary compression transport failure")
        return json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["read old output"],
                "inspected_or_modified_files": ["old.py"],
                "remaining_work": ["finish fix"],
                "next_plan": ["run unit tests"],
                "key_decisions": ["keep snapshots non-authoritative"],
                "constraints": ["no manual compression"],
                "visible_artifact_refs": [],
            }
        )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=RecordingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = _compression_session(session)

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=_compression_conversation(),
    )

    assert attempts == 2
    assert result.status == "completed"
    assert result.metadata["context_optimization"]["retry"]["attempts"][0]["strategy"] == (
        "repeat_call"
    )
    assert result.metadata["context_optimization"]["retry"]["attempts"][0]["reason"] == (
        "compression_model_failed"
    )
    assert [event.kind for event in events.list_for_run(run.run_id)].count(
        "model_call_failed"
    ) == 1
    db.close()


def test_prompt_executor_exhausts_transient_compression_model_failure_retry(
    tmp_path,
) -> None:
    class FailingAdapter:
        def run(self, request, context):
            raise AssertionError("ordinary model call should not run after compression failure")

    attempts = 0

    def compression_model(_frame):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("temporary compression transport failure")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        adapter=FailingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = _compression_session(session)

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=_compression_conversation(),
    )

    assert attempts == 2
    assert result.status == "failed"
    assert result.metadata["context_optimization"]["retry"]["exhausted"] is True
    assert (
        result.metadata["context_optimization"]["retry"]["resulting_error_class"]
        == "model_error"
    )
    assert (
        result.metadata["context_optimization"]["retry"]["resulting_reason"]
        == "compression_model_failed"
    )
    durable_rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )
    assert durable_rows[-1].kind == "failure_fact"
    assert durable_rows[-1].content["reason"] == "compression_failed"
    db.close()


def test_prompt_executor_does_not_retry_deterministic_compression_output_failure(
    tmp_path,
) -> None:
    class FailingAdapter:
        def run(self, request, context):
            raise AssertionError("ordinary model call should not run after compression failure")

    attempts = 0

    def compression_model(_frame):
        nonlocal attempts
        attempts += 1
        return ""

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        adapter=FailingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = _compression_session(session)

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=_compression_conversation(),
    )

    assert attempts == 1
    assert result.status == "failed"
    assert "retry" not in result.metadata["context_optimization"]
    db.close()


def test_prompt_executor_compression_failure_aborts_without_conversation_mutation(
    tmp_path,
) -> None:
    class FailingAdapter:
        def run(self, request, context):
            raise AssertionError("ordinary model call should not run after compression failure")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        adapter=FailingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: "",
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 420,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output " * 40,
            "estimated_tokens": 120,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "consumed old",
            "estimated_tokens": 8,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    assert result.status == "failed"
    assert result.error["error_class"] == "compression_failed"
    assert [message["content"] for message in result.metadata["conversation_writeback"]] == [
        message["content"] for message in conversation
    ]
    assert all(
        message["kind"] != "context_summary"
        for message in result.metadata["conversation_writeback"]
    )
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    persisted = events.list_for_run(run.run_id)
    assert [event.kind for event in persisted] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "compression_failed",
    ]
    assert persisted[1].payload["purpose"] == "compression"
    assert persisted[3].payload["error_class"] == "compression_failed"
    assert persisted[3].payload["error"]["error_class"] == "model_error"
    assert persisted[3].payload["error"]["reason"] == "compression_failed"
    assert persisted[3].payload["error"]["scope"] == "turn"
    assert checkpoints.list_for_session(session.session_id) == []
    durable_rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )
    assert durable_rows[-1].kind == "failure_fact"
    assert durable_rows[-1].content["reason"] == "compression_failed"
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_prompt_executor_context_limit_exceeded_aborts_without_model_call(
    tmp_path,
) -> None:
    class FailingAdapter:
        def run(self, request, context):
            raise AssertionError("ordinary model call should not run over context limit")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        adapter=FailingAdapter(),
        tool_definitions=[],
        system_prompt="system " * 100,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=None,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 40,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 1.0,
                    "retain_recent_model_calls": 4,
                    "compression_reserved_output_tokens": 10,
                },
            },
        }
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=[],
    )

    expected_message = (
        "Context window still exceeds the limit after compression. "
        "The current turn was aborted."
    )
    assert result.status == "failed"
    assert result.error == {
        "error_class": "context_limit_exceeded",
        "message": expected_message,
    }
    assert result.metadata["failure_scope"] == "turn"
    assert result.metadata["context_estimate"]["total_tokens"] > 40
    persisted = events.list_for_run(run.run_id)
    assert [event.kind for event in persisted] == [
        "user_message",
        "context_limit_exceeded",
    ]
    assert persisted[1].payload["message"] == expected_message
    assert persisted[1].payload["error_class"] == "context_limit_exceeded"
    assert persisted[1].payload["error"]["error_class"] == "model_error"
    assert persisted[1].payload["error"]["reason"] == "context_limit_exceeded"
    assert persisted[1].payload["error"]["scope"] == "turn"
    assert checkpoints.latest_for_run(run.run_id) is None
    durable_rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )
    assert durable_rows[-1].kind == "failure_fact"
    assert durable_rows[-1].content["reason"] == "context_limit_exceeded"
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_prompt_executor_manual_compress_noop_does_not_call_model_or_snapshot(
    tmp_path,
) -> None:
    compression_calls = 0

    def compression_model(_frame):
        nonlocal compression_calls
        compression_calls += 1
        return "{}"

    (
        _workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )

    result = executor.manual_compress(
        session=session,
        run=run,
        conversation=[],
        prompt_turn_counter=1,
    )

    assert result.status == "completed"
    assert result.assistant_output == "No compressible history."
    assert compression_calls == 0
    assert "conversation_writeback" not in result.metadata
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    assert events.list_for_run(run.run_id) == []
    assert checkpoints.latest_for_run(run.run_id) is None
    db.close()


def test_prompt_executor_manual_compress_success_writes_snapshot_and_message(
    tmp_path,
) -> None:
    (
        _workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=ConversationStore(db.connection, artifact_store=artifacts),
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["manual compression ran"],
                "inspected_or_modified_files": [],
                "remaining_work": [],
                "next_plan": [],
                "key_decisions": [],
                "constraints": [],
            }
        ),
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 420,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 1.0,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )

    conversation = _with_durable_history(
        store=ConversationStore(db.connection, artifact_store=artifacts),
        session_id=session.session_id,
        run_id=run.run_id,
        conversation=[
            {
                "seq": 1,
                "role": "assistant",
                "kind": "assistant_output",
                "turn_id": "turn-1",
                "model_call_id": "call-1",
                "content": "old output " * 40,
                "estimated_tokens": 120,
            },
            {
                "seq": 2,
                "role": "assistant",
                "kind": "assistant_output",
                "turn_id": "turn-2",
                "model_call_id": "call-2",
                "content": "Consumed old output.",
                "estimated_tokens": 10,
                "metadata": {"consumed_model_call_ids": ["call-1"]},
            },
        ],
    )

    result = executor.manual_compress(
        session=session,
        run=run,
        conversation=conversation,
        prompt_turn_counter=1,
    )

    assert result.status == "completed"
    assert result.assistant_output.startswith("Context compressed: reduced from ")
    assert " to " in result.assistant_output
    assert result.metadata["context_optimization"]["trigger"] == "manual"
    assert result.metadata["conversation_writeback"][0]["kind"] == "context_summary"
    assert result.metadata["conversation_writeback"][0]["role"] == "runtime"
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "model_call_started",
        "model_call_completed",
        "context_optimized",
    ]
    assert checkpoints.latest_for_run(run.run_id) is None
    durable_rows = ConversationStore(
        db.connection,
        artifact_store=artifacts,
    ).list_messages(run.run_id)
    assert (durable_rows[-1].role, durable_rows[-1].kind) == (
        "runtime",
        "context_summary",
    )
    db.close()


def test_prompt_executor_manual_compress_oldest_group_failure_preserves_boundary(
    tmp_path,
) -> None:
    compression_calls = 0

    def compression_model(_frame):
        nonlocal compression_calls
        compression_calls += 1
        return "{}"

    (
        _workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 800,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 1.0,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output",
            "estimated_tokens": 1000,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.manual_compress(
        session=session,
        run=run,
        conversation=conversation,
        prompt_turn_counter=1,
    )

    expected_message = (
        "Context compression could not fit the oldest eligible history group. "
        "The current turn was aborted. Start a new session to continue with a "
        "fresh context window."
    )
    assert result.status == "failed"
    assert result.error == {
        "error_class": "compression_failed",
        "message": expected_message,
    }
    assert result.metadata["failure_scope"] == "turn"
    assert [message["content"] for message in result.metadata["conversation_writeback"]] == [
        message["content"] for message in conversation
    ]
    assert all(
        message["kind"] != "context_summary"
        for message in result.metadata["conversation_writeback"]
    )
    assert compression_calls == 0
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    persisted = events.list_for_run(run.run_id)
    assert [event.kind for event in persisted] == [
        "compression_failed",
    ]
    assert persisted[0].payload["message"] == expected_message
    assert checkpoints.latest_for_run(run.run_id) is None
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_omission_plus_compression_writes_only_final_snapshot(tmp_path) -> None:
    class RecordingAdapter:
        def run(self, request, context):
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    def compression_model(_frame):
        return json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["omitted and compressed"],
                "inspected_or_modified_files": [],
                "remaining_work": [],
                "next_plan": [],
                "key_decisions": [],
                "constraints": [],
            }
        )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        _checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=CheckpointStore(db.connection),
        artifact_store=artifacts,
        adapter=RecordingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 500,
                    "omit_old_tool_results_at_ratio": 0.1,
                    "compress_history_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old",
            "estimated_tokens": 10,
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "full old tool output " * 80,
            "artifact_refs": ["art_full"],
            "estimated_tokens": 60,
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed older result.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    snapshot_count = db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0]
    context_events = [
        event for event in events.list_for_run(run.run_id)
        if event.kind == "context_optimized"
    ]
    assert result.status == "completed"
    assert snapshot_count == 0
    assert [event.payload["trigger"] for event in context_events] == [
        "omission | compression"
    ]
    assert result.metadata["context_optimization"]["omitted_tool_result_count"] == 1
    db.close()


def test_projection_overwrite_preserves_retained_summary_durable_index(
    tmp_path,
) -> None:
    (
        _workspace,
        db,
        _sessions,
        _runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    store = ConversationStore(db.connection, artifact_store=artifacts)
    store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        messages=[
            ConversationAppend(
                turn_id="turn-old",
                message_group_id="turn-old:assistant",
                model_call_id="call-old",
                group_position=0,
                group_row_count=1,
                role="assistant",
                kind="assistant_output",
                content={"content": "old evicted row"},
            )
        ],
    )
    summary_rows = store.append_closed_group(
        session_id=session.session_id,
        run_id=run.run_id,
        update_reason="compression",
        messages=[
            ConversationAppend(
                turn_id="turn-2",
                message_group_id="turn-2:context-summary",
                model_call_id=None,
                group_position=0,
                group_row_count=1,
                role="runtime",
                kind="context_summary",
                content={"content": "summary row"},
            )
        ],
    )
    summary_index = summary_rows[0].message_index
    assert summary_index != 1
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        conversation_store=store,
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=None,
    )

    executor._overwrite_durable_projection_from_messages(
        session=session,
        run=run,
        messages=[
            ConversationMessage(
                seq=1,
                role="runtime",
                kind="context_summary",
                turn_id=None,
                model_call_id=None,
                tool_call_id=None,
                content="summary row",
                metadata={"durable_message_index": summary_index},
            )
        ],
        update_reason="omission",
    )

    assert store.get_projection(run.run_id).message_refs == [{"index": summary_index}]
    db.close()


def test_repl_runtime_writes_back_omitted_conversation_and_metadata(tmp_path) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                        "window_tokens": 500,
                    "omit_old_tool_results_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    db.connection.execute(
        "UPDATE sessions SET config_snapshot_json = ? WHERE session_id = ?",
        (json.dumps(session.config_snapshot, sort_keys=True), session.session_id),
    )
    db.connection.commit()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=RecordingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
    )
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )
    runtime.conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "tool_call",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "shell_exec",
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "full old tool output " * 80,
            "artifact_refs": ["art_full"],
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed older result.",
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = runtime.run_turn("continue")

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    assert result.status == "completed"
    assert runtime.conversation[1]["content"] == marker
    assert runtime.conversation[1]["artifact_refs"] == ["art_full"]
    assert runtime.conversation[-2]["kind"] == "current_user_input"
    assert runtime.conversation[-2]["turn_id"] == "turn-1"
    assert runtime.conversation[-2]["seq"] > 3
    assert runtime.conversation[-1]["kind"] == "assistant_output"
    assert runtime.conversation[-1]["model_call_id"].startswith("repl_turn_1")
    assert runtime.conversation[-1]["metadata"]["consumed_model_call_ids"] == [
        "call-1",
        "call-2",
    ]
    db.close()


def test_repl_runtime_persists_tool_loop_messages_for_next_turn_context(tmp_path) -> None:
    class ToolHistoryModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "I will read the notes before answering.",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "answer", "tool_calls": [], "usage": {}},
            )()

    model = ToolHistoryModel()
    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, model)
    (workspace / "notes.txt").write_text("persisted tool output", encoding="utf-8")
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )

    first = runtime.run_turn("read notes")
    second = runtime.run_turn("what did the tool return?")

    assert first.status == "completed"
    assert second.status == "completed"
    assert [message["kind"] for message in runtime.conversation] == [
        "current_user_input",
        "tool_call",
        "tool_result",
        "assistant_output",
        "current_user_input",
        "assistant_output",
    ]
    assert runtime.conversation[1]["model_call_id"] == "turn-1:model_call_1"
    assert runtime.conversation[1]["content"]["content"] == (
        "I will read the notes before answering."
    )
    assert runtime.conversation[2]["model_call_id"] == "turn-1:model_call_1"
    assert runtime.conversation[2]["content"]["content"]["content"] == (
        "persisted tool output"
    )
    second_turn_messages = [
        _provider_message_content(message) for message in model.messages_by_call[2]
    ]
    second_turn_text = "\n".join(second_turn_messages)
    assert "read notes" in second_turn_text
    assert any(
        "read_file" in str(getattr(message, "tool_calls", ""))
        for message in model.messages_by_call[2]
    )
    assert "persisted tool output" in second_turn_text
    assert "what did the tool return?" in second_turn_text
    db.close()


def test_repl_runtime_strips_thinking_before_durable_and_followup_context(tmp_path) -> None:
    class ThinkingToolHistoryModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": [
                            {"type": "thinking", "thinking": "hidden durable plan"},
                            {"type": "text", "text": "reading notes"},
                            {
                                "type": "tool_use",
                                "id": "provider_tool_1",
                                "name": "read_file",
                                "input": {"path": "notes.txt"},
                            },
                        ],
                        "tool_calls": [],
                        "usage": {},
                    },
                )()
            if self.calls == 2:
                assert "hidden durable plan" not in str(messages)
                return type(
                    "Response",
                    (),
                    {"content": "first answer", "tool_calls": [], "usage": {}},
                )()
            assert "hidden durable plan" not in str(messages)
            return type(
                "Response",
                (),
                {"content": "second answer", "tool_calls": [], "usage": {}},
            )()

    model = ThinkingToolHistoryModel()
    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, model)
    (workspace / "notes.txt").write_text("persisted tool output", encoding="utf-8")
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )

    first = runtime.run_turn("read notes")
    second = runtime.run_turn("continue")
    rows = ConversationStore(db.connection, artifact_store=artifacts).list_messages(
        run.run_id
    )
    durable_payload = json.dumps(
        [row.content for row in rows],
        ensure_ascii=False,
        sort_keys=True,
    )
    event_payload = json.dumps(
        [event.payload for event in events.list_for_run(run.run_id)],
        ensure_ascii=False,
        sort_keys=True,
    )

    assert first.status == "completed"
    assert second.status == "completed"
    assert "hidden durable plan" not in json.dumps(
        runtime.conversation,
        ensure_ascii=False,
    )
    assert "hidden durable plan" not in durable_payload
    assert "hidden durable plan" not in event_payload
    assert runtime.conversation[1]["content"] == {
        "content": "reading notes",
        "tool_calls": [
            {
                "id": "turn-1:model_call_1_tool_1",
                "name": "read_file",
                "args": {"path": "notes.txt"},
            }
        ],
    }
    second_turn_text = "\n".join(
        _provider_message_content(message) for message in model.messages_by_call[2]
    )
    assert "reading notes" in second_turn_text
    assert "persisted tool output" in second_turn_text
    assert "hidden durable plan" not in second_turn_text
    db.close()


def test_repl_runtime_excludes_runtime_cancellation_fact_from_provider_prompt(
    tmp_path,
) -> None:
    class InspectingModel(FakeChatModel):
        def __init__(self) -> None:
            super().__init__(response="continued")
            self.messages: list[object] = []

        def invoke(self, messages: list[object]) -> object:
            self.messages = messages
            return super().invoke(messages)

    model = InspectingModel()
    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, model)
    store = ConversationStore(db.connection, artifact_store=artifacts)
    conversation = _with_durable_history(
        store=store,
        session_id=session.session_id,
        run_id=run.run_id,
        conversation=[
            {
                "seq": 1,
                "role": "runtime",
                "kind": "cancellation_fact",
                "turn_id": "turn-1",
                "content": {
                    "error_class": "cancelled",
                    "reason": "user_cancel_running",
                    "message": "Turn cancelled by user.",
                    "artifact_ids": [],
                },
            }
        ],
    )
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
        conversation=conversation,
    )

    result = runtime.run_turn("continue")

    assert result.status == "completed"
    assert "Turn cancelled by user." not in "\n".join(
        _provider_message_content(message) for message in model.messages
    )
    assert all(
        not (isinstance(message, dict) and message.get("role") == "runtime")
        for message in model.messages
    )
    db.close()


def test_repl_runtime_persists_failed_turn_tool_loop_and_failure_observation(
    tmp_path,
) -> None:
    class FailedTurnExecutor:
        def run_turn(self, **_kwargs):
            return AgentRunResult(
                status="failed",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "error_class": "internal_error",
                    "message": "Tool call loop exceeded Phase 0 iteration limit.",
                    "source": "adapter",
                    "recoverable": True,
                },
                metadata={
                    "failure_scope": "turn",
                    "conversation_writeback": [
                        {
                            "seq": 1,
                            "role": "assistant",
                            "kind": "context_summary",
                            "turn_id": "summary",
                            "model_call_id": None,
                            "tool_call_id": None,
                            "content": "retained summary",
                            "artifact_refs": [],
                            "metadata": {},
                        }
                    ],
                    "turn_tool_loop_messages": [
                        {
                            "role": "assistant",
                            "kind": "tool_call",
                            "turn_id": "turn-1",
                            "model_call_id": "model_call_1",
                            "tool_call_id": None,
                            "content": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "read_file_0",
                                        "name": "read_file",
                                        "args": {"path": "notes.txt"},
                                    }
                                ],
                            },
                            "artifact_refs": [],
                            "metadata": {},
                        },
                        {
                            "role": "tool",
                            "kind": "tool_result",
                            "turn_id": "turn-1",
                            "model_call_id": "model_call_1",
                            "tool_call_id": "read_file_0",
                            "content": {
                                "message_type": "tool_result",
                                "content": "notes content",
                                "tool_call_id": "read_file_0",
                            },
                            "artifact_refs": [],
                            "metadata": {},
                        },
                    ],
                },
            )

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=FailedTurnExecutor(),
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )

    result = runtime.run_turn("read notes")

    assert result.status == "failed"
    assert [message["kind"] for message in runtime.conversation] == [
        "context_summary",
        "current_user_input",
        "tool_call",
        "tool_result",
        "failure_fact",
    ]
    assert runtime.conversation[1]["content"] == "read notes"
    assert runtime.conversation[2]["seq"] == 3
    assert runtime.conversation[3]["seq"] == 4
    failure = runtime.conversation[4]
    assert failure["role"] == "runtime"
    assert failure["content"] == {
        "error_class": "internal_error",
        "reason": "internal_error",
        "message": "Tool call loop exceeded Phase 0 iteration limit.",
        "artifact_ids": [],
    }
    db.close()


def test_repl_runtime_approval_denial_uses_unified_failure_observation(
    tmp_path,
) -> None:
    class ApprovalDeniedExecutor:
        def run_turn(self, **_kwargs):
            return AgentRunResult(
                status="failed",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "error_class": "policy_denied",
                    "message": "Approval denied.",
                    "source": "toolbroker",
                    "recoverable": True,
                },
                metadata={
                    "failure_scope": "turn",
                    "approval_denied_abort": True,
                    "denied_tool_calls": [
                        {"id": "", "name": "write_file", "args": {"path": "count.py"}}
                    ],
                },
            )

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=ApprovalDeniedExecutor(),
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )

    result = runtime.run_turn("write count.py")

    assert result.status == "failed"
    assert [message["kind"] for message in runtime.conversation] == [
        "current_user_input",
        "failure_fact",
    ]
    assert runtime.conversation[1]["role"] == "runtime"
    assert runtime.conversation[1]["content"] == {
        "error_class": "policy_denied",
        "reason": "policy_denied",
        "message": "Approval denied.",
        "artifact_ids": [],
    }
    assert not any(
        message["kind"] == "approval_denied_observation"
        for message in runtime.conversation
    )
    db.close()


def test_repl_runtime_tool_history_group_is_closed_and_evictable(tmp_path) -> None:
    class ToolHistoryModel:
        def __init__(self) -> None:
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "Reading first.",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "answer", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolHistoryModel())
    (workspace / "notes.txt").write_text("grouped tool output", encoding="utf-8")
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )

    result = runtime.run_turn("read notes")

    messages = [
        ConversationMessage.from_dict(message)
        for message in runtime.conversation
    ]
    query_control = QueryControlPlane()
    groups = query_control.derive_model_call_groups(messages)
    tool_group = next(
        group for group in groups if group.model_call_id == "turn-1:model_call_1"
    )
    plan = ContextManager(query_control=query_control).prepare_compression(
        retained_messages=messages,
        current_messages=[],
        retain_recent_model_calls=0,
        window_tokens=10_000,
        compression_reserved_output_tokens=100,
    )

    assert result.status == "completed"
    assert tool_group.status == "closed"
    assert tool_group.consumed_by_later_model_call is True
    assert [message.kind for message in plan.evicted_messages] == [
        "tool_call",
        "tool_result",
    ]
    assert plan.selected_model_call_group_ids == ["turn-1:model_call_1"]
    db.close()


def test_repl_runtime_updates_latest_context_estimate_from_stream_event(tmp_path) -> None:
    class StreamingAdapter:
        def run(self, request, context):
            raise AssertionError("run should not be called")

        def stream(self, request, context, on_event):
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_completed",
                    payload={
                        "model_call_id": "model_1",
                        "is_final": True,
                        "usage": {},
                        "duration_ms": 1,
                    },
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=StreamingAdapter(),
        tool_definitions=tool_definitions(),
        system_prompt=SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        run_store=runs,
    )
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace,
    )
    stream_events: list[AgentStreamEvent] = []

    result = runtime.run_turn("hello", agent_stream_callback=stream_events.append)

    assert result.status == "completed"
    assert stream_events[0].kind == "stream_context_estimate_updated"
    assert runtime.latest_context_estimate == stream_events[0].payload[
        "context_estimate"
    ]
    db.close()


def test_provider_messages_to_conversation_namespaces_tool_call_ids_by_turn() -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    from debug_agent.runtime.prompt_executor import _provider_messages_to_conversation

    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "read_file_0",
                    "name": "read_file",
                    "args": {"path": "a.txt"},
                }
            ],
        ),
        ToolMessage(content="file text", tool_call_id="read_file_0"),
    ]

    converted = _provider_messages_to_conversation(messages, turn_id="turn-1")

    assert converted[0].role == "assistant"
    assert converted[0].kind == "tool_call"
    assert converted[0].model_call_id == "turn-1:model_call_1"
    assert converted[0].tool_call_id == "turn-1:model_call_1_tool_1"
    assert converted[0].content == {
        "content": "",
        "tool_calls": [
            {
                "id": "turn-1:model_call_1_tool_1",
                "name": "read_file",
                "args": {"path": "a.txt"},
            }
        ],
    }
    assert converted[1].role == "tool"
    assert converted[1].kind == "tool_result"
    assert converted[1].model_call_id == "turn-1:model_call_1"
    assert converted[1].tool_call_id == "turn-1:model_call_1_tool_1"
    assert converted[1].content == {
        "message_type": "tool_result",
        "tool_name": "",
        "tool_call_id": "turn-1:model_call_1_tool_1",
        "status": "ok",
        "content": "file text",
        "error": None,
        "artifact_ids": [],
        "metadata": {},
    }


def test_provider_messages_to_conversation_preserves_shell_nonzero_error_projection() -> None:
    from langchain_core.messages import ToolMessage

    from debug_agent.runtime.prompt_executor import _provider_messages_to_conversation

    messages = [
        ToolMessage(
            content=json.dumps(
                {
                    "error_class": "tool_error",
                    "reason": "shell_nonzero_exit",
                    "message": "err (exit code 7)",
                    "artifact_ids": [],
                },
                sort_keys=True,
            ),
            tool_call_id="model_call_1_tool_1",
        )
    ]

    converted = _provider_messages_to_conversation(messages, turn_id="turn-1")

    assert converted[0].kind == "tool_result"
    assert converted[0].content == {
        "message_type": "tool_result",
        "tool_name": "",
        "tool_call_id": "turn-1:model_call_1_tool_1",
        "status": "error",
        "content": None,
        "error": {
            "error_class": "tool_error",
            "reason": "shell_nonzero_exit",
            "message": "err (exit code 7)",
            "artifact_ids": [],
        },
        "artifact_ids": [],
        "metadata": {},
    }


def test_provider_messages_to_conversation_splits_multiple_tool_calls() -> None:
    from langchain_core.messages import AIMessage, ToolMessage

    from debug_agent.runtime.prompt_executor import _provider_messages_to_conversation

    messages = [
        AIMessage(
            content="checking files",
            tool_calls=[
                {
                    "id": "model_call_1_tool_1",
                    "name": "shell_exec",
                    "args": {"argv": ["git", "status"]},
                },
                {
                    "id": "model_call_1_tool_3",
                    "name": "shell_exec",
                    "args": {"argv": ["git", "diff"]},
                },
            ],
        ),
        ToolMessage(content="status", tool_call_id="model_call_1_tool_1"),
        ToolMessage(content="diff", tool_call_id="model_call_1_tool_3"),
    ]

    converted = _provider_messages_to_conversation(messages, turn_id="turn-1")

    assert [message.seq for message in converted] == [1, 2, 3, 4]
    assert [
        (message.kind, message.model_call_id, message.tool_call_id)
        for message in converted
    ] == [
        ("tool_call", "turn-1:model_call_1", "turn-1:model_call_1_tool_1"),
        ("tool_call", "turn-1:model_call_1", "turn-1:model_call_1_tool_3"),
        ("tool_result", "turn-1:model_call_1", "turn-1:model_call_1_tool_1"),
        ("tool_result", "turn-1:model_call_1", "turn-1:model_call_1_tool_3"),
    ]
    assert converted[0].content == {
        "content": "checking files",
        "tool_calls": [
            {
                "id": "turn-1:model_call_1_tool_1",
                "name": "shell_exec",
                "args": {"argv": ["git", "status"]},
            }
        ],
    }
    assert converted[1].content == {
        "content": "checking files",
        "tool_calls": [
            {
                "id": "turn-1:model_call_1_tool_3",
                "name": "shell_exec",
                "args": {"argv": ["git", "diff"]},
            }
        ],
    }


def test_provider_message_from_conversation_projects_runtime_as_user_not_system() -> None:
    from debug_agent.runtime.prompt_executor import _provider_message_from_conversation

    message = ConversationMessage(
        seq=1,
        role="runtime",
        kind="context_summary",
        turn_id=None,
        model_call_id=None,
        tool_call_id=None,
        content="compressed continuity",
    )

    assert _provider_message_from_conversation(message) == {
        "role": "user",
        "content": "compressed continuity",
    }

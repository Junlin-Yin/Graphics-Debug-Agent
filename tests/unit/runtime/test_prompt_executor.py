from __future__ import annotations

from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.adapters.model_factory import FakeChatModel
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.config import PHASE_0_SYSTEM_PROMPT
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.model_context import TokenEstimator
from debug_agent.runtime.prompt_executor import PromptAgentExecutor
from debug_agent.runtime.stream_events import AgentStreamEvent
from debug_agent.tools.broker import ToolBroker
from debug_agent.tools.native import tool_definitions


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
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
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


def test_prompt_executor_writes_model_events_assistant_event_and_turn_checkpoint(
    tmp_path,
) -> None:
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
        "checkpoint_written",
    ]
    assert persisted_events[2].payload["duration"] >= 0
    assert persisted_events[2].payload["content"] == "assistant answer"
    assert persisted_events[2].payload["tool_calls"] == []
    assert persisted_events[2].payload["artifact_ids"] == []
    assert persisted_events[2].payload["redacted_output"] is None
    assert latest_checkpoint.kind == "turn"
    assert latest_checkpoint.state["session_status"] == "running"
    assert latest_checkpoint.state["run_status"] == "running"
    assert latest_checkpoint.state["prompt_turn_counter"] == 1
    checkpoint_metadata = latest_checkpoint.state["latest_model_response_metadata"]
    assert checkpoint_metadata["context_estimate"] == result.metadata["context_estimate"]
    assert checkpoint_metadata["query_state"]["continuation_reason"] == (
        "final_assistant_response"
    )
    assert sessions.get(session.session_id).status == "running"
    assert runs.get(run.run_id).status == "running"
    assert sessions.get(session.session_id).latest_checkpoint_id == latest_checkpoint.checkpoint_id
    assert runs.get(run.run_id).latest_checkpoint_id == latest_checkpoint.checkpoint_id
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
        "checkpoint_written",
    ]
    first_model_completed = events.list_for_run(run.run_id)[2]
    assert first_model_completed.payload["content"] == ""
    assert first_model_completed.payload["tool_calls"] == [
        {"id": "read_file_0", "name": "read_file", "args": {"path": "notes.txt"}}
    ]
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
    assert artifacts.get(artifact_id).metadata == {
        "bytes": 16 * 1024 + 1,
        "event_kind": "model_call_completed",
    }
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

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

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
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
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

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

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
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
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

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

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
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
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
    assert stream_events == [
        AgentStreamEvent(
            kind="stream_text_delta",
            payload={"model_call_id": "model_1", "text": "answer"},
        )
    ]
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "user_message",
        "assistant_message",
        "checkpoint_written",
    ]
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

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

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
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
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
        "checkpoint_written",
    ]
    db.close()


def test_prompt_executor_writes_failed_model_event_and_error_checkpoint(tmp_path) -> None:
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
        "checkpoint_written",
    ]
    failed_event = events.list_for_run(run.run_id)[2]
    assert failed_event.payload["error_class"] == "model_error"
    assert failed_event.payload["message"] == "provider failed"
    assert failed_event.payload["source"] == "model"
    assert failed_event.payload["recoverable"] is True
    assert failed_event.payload["duration"] >= 0
    assert latest_checkpoint.kind == "error"
    assert latest_checkpoint.state["latest_error_summary"] == "provider failed"
    assert sessions.get(session.session_id).status == "running"
    assert runs.get(run.run_id).status == "running"
    db.close()

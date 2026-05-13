from __future__ import annotations

from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.adapters.model_factory import FakeChatModel
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.config import PHASE_0_SYSTEM_PROMPT
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.prompt_executor import PromptAgentExecutor
from debug_agent.tools.broker import ToolBroker
from debug_agent.tools.native_readonly import tool_definitions


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
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
    )
    return workspace, db, sessions, runs, events, checkpoints, session, run, executor


def test_prompt_executor_writes_model_events_assistant_event_and_turn_checkpoint(
    tmp_path,
) -> None:
    workspace, db, sessions, runs, events, checkpoints, session, run, executor = _runtime(
        tmp_path, FakeChatModel(response="assistant answer")
    )

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
    assert latest_checkpoint.kind == "turn"
    assert latest_checkpoint.state["session_status"] == "running"
    assert latest_checkpoint.state["run_status"] == "running"
    assert latest_checkpoint.state["prompt_turn_counter"] == 1
    assert latest_checkpoint.state["latest_model_response_metadata"] == {}
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

    workspace, db, sessions, runs, events, checkpoints, session, run, executor = _runtime(
        tmp_path, ToolLoopModel()
    )
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
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
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


def test_prompt_executor_writes_failed_model_event_and_error_checkpoint(tmp_path) -> None:
    workspace, db, sessions, runs, events, checkpoints, session, run, executor = _runtime(
        tmp_path, FakeChatModel(error=RuntimeError("provider failed"))
    )

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

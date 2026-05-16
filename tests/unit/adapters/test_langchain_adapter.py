from __future__ import annotations

import time

from debug_agent.adapters.langchain_adapter import (
    LangChainAgentLoopAdapter,
    MAX_TOOL_CALL_ITERATIONS,
    _langchain_tools,
)
from debug_agent.adapters.model_factory import FakeChatModel
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.contracts import (
    AgentRunRequest,
    AgentRunResult,
    RunContext,
    ToolResult,
)


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        session_id="sess_1",
        run_id="run_1",
        user_input="hello",
        system_prompt="system prompt",
        conversation=[],
        tools=[
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }
        ],
        model_config={"provider": "fake"},
        timeout_seconds=30,
    )


def _context() -> RunContext:
    return RunContext(
        workspace_root="/repo",
        artifact_root="/repo/.sessions/sess_1/artifacts",
        approval_mode="yolo",
        cancellation_token=None,
        metadata={},
    )


def test_langchain_adapter_maps_model_success() -> None:
    adapter = LangChainAgentLoopAdapter(model=FakeChatModel(response="answer"))

    result = adapter.run(_request(), _context())

    assert result.status == "completed"
    assert result.assistant_output == "answer"
    assert result.error is None
    assert result.usage == {}
    assert adapter.model.messages[0]["role"] == "system"
    assert "runtime safety" in adapter.model.messages[0]["content"]
    assert adapter.model.messages[-1]["role"] == "user"
    assert adapter.model.messages[-1]["content"] == "hello"


def test_langchain_adapter_stream_falls_back_to_non_streaming_invoke() -> None:
    stream_events = []
    model = FakeChatModel(response="answer")
    adapter = LangChainAgentLoopAdapter(model=model)

    result = adapter.stream(_request(), _context(), stream_events.append)

    assert result.status == "completed"
    assert result.assistant_output == "answer"
    assert result.metadata["streaming_fallback"] is True
    assert stream_events == []
    assert model.messages[-1]["content"] == "hello"


def test_langchain_adapter_stream_preserves_existing_result_metadata_on_fallback() -> None:
    class MetadataAdapter(LangChainAgentLoopAdapter):
        def run(self, request, context):
            result = super().run(request, context)
            return AgentRunResult(
                status=result.status,
                assistant_output=result.assistant_output,
                tool_results=result.tool_results,
                usage=result.usage,
                error=result.error,
                metadata={"provider": "fake"},
            )

    result = MetadataAdapter(model=FakeChatModel(response="answer")).stream(
        _request(),
        _context(),
        lambda _event: None,
    )

    assert result.metadata == {"provider": "fake", "streaming_fallback": True}


def test_langchain_adapter_maps_model_failure_timeout_and_cancellation() -> None:
    failure = LangChainAgentLoopAdapter(
        model=FakeChatModel(error=RuntimeError("provider failed"))
    ).run(_request(), _context())
    timeout = LangChainAgentLoopAdapter(model=FakeChatModel(timeout=True)).run(
        _request(), _context()
    )
    cancelled = LangChainAgentLoopAdapter(model=FakeChatModel(cancelled=True)).run(
        _request(), _context()
    )

    assert failure.status == "failed"
    assert failure.error["error_class"] == "model_error"
    assert timeout.status == "timeout"
    assert timeout.error["error_class"] == "timeout"
    assert cancelled.status == "cancelled"
    assert cancelled.error["error_class"] == "cancelled"


def test_langchain_adapter_times_out_blocking_model_call() -> None:
    events = []

    class SlowModel:
        def invoke(self, messages):
            time.sleep(0.2)
            return type(
                "Response",
                (),
                {"content": "too late", "tool_calls": [], "usage": {}},
            )()

    request = AgentRunRequest(
        session_id="sess_1",
        run_id="run_1",
        user_input="hello",
        system_prompt="system prompt",
        conversation=[],
        tools=[],
        model_config={"provider": "fake", "model": "slow"},
        timeout_seconds=0.01,
    )
    context = RunContext(
        workspace_root="/repo",
        artifact_root="/repo/.sessions/sess_1/artifacts",
        approval_mode="yolo",
        cancellation_token=None,
        metadata={},
        model_event_recorder=lambda kind, payload: events.append((kind, payload)),
    )

    started = time.monotonic()
    result = LangChainAgentLoopAdapter(model=SlowModel()).run(request, context)
    duration = time.monotonic() - started

    assert result.status == "timeout"
    assert result.error["error_class"] == "timeout"
    assert duration < 0.15
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]
    assert events[-1][1]["error_class"] == "timeout"


def test_langchain_adapter_delegates_tool_calls_to_toolbroker() -> None:
    calls = []

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            calls.append((session_id, run_id, tool_name, arguments, context))
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

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
                            {"name": "read_file", "args": {"path": "a.txt"}}
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "used tool", "tool_calls": [], "usage": {}},
            )()

    adapter = LangChainAgentLoopAdapter(
        model=ToolLoopModel(),
        tool_broker=RecordingBroker(),
    )

    result = adapter.run(_request(), _context())

    assert result.status == "completed"
    assert result.tool_results == [
        {
            "status": "ok",
            "output": "file text",
            "error": None,
            "artifacts": [],
            "metadata": {},
            "redacted_output": None,
        }
    ]
    assert calls == [
        (
            "sess_1",
            "run_1",
            "read_file",
            {"path": "a.txt"},
            {
                "workspace_root": "/repo",
                "artifact_root": "/repo/.sessions/sess_1/artifacts",
                "approval_mode": "yolo",
                "cancellation_token": None,
                "timeout_seconds": 30,
                "metadata": {},
            },
        )
    ]


def test_langchain_adapter_does_not_mutate_runtime_state(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        checkpoints = CheckpointStore(db.connection)
        session = sessions.create(
            workspace_root=workspace,
            approval_mode="yolo",
            config_snapshot={"provider": "fake"},
            session_id="sess_1",
        )
        run = runs.create_prompt_run(session.session_id, run_id="run_1")
        before_session = sessions.get(session.session_id)
        before_run = runs.get(run.run_id)
        before_checkpoint = checkpoints.latest_for_run(run.run_id)

        result = LangChainAgentLoopAdapter(model=FakeChatModel(response="answer")).run(
            _request(),
            RunContext(
                workspace_root=str(workspace),
                artifact_root=session.artifact_root,
                approval_mode=session.approval_mode,
                cancellation_token=None,
                metadata={},
                model_event_recorder=lambda _kind, _payload: None,
            ),
        )

        assert result.status == "completed"
        assert sessions.get(session.session_id) == before_session
        assert runs.get(run.run_id) == before_run
        assert checkpoints.latest_for_run(run.run_id) == before_checkpoint
        assert EventWriter(db.connection, db.path.parent).list_for_run(run.run_id) == []
    finally:
        db.close()


def test_langchain_adapter_feeds_tool_results_back_to_model() -> None:
    calls = []

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            calls.append((session_id, run_id, tool_name, arguments, context))
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    class ToolLoopModel:
        def __init__(self) -> None:
            self.messages_by_call = []

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.messages_by_call.append(messages)
            if len(self.messages_by_call) == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "a.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "a.txt contains file text", "tool_calls": [], "usage": {}},
            )()

    model = ToolLoopModel()
    adapter = LangChainAgentLoopAdapter(model=model, tool_broker=RecordingBroker())

    result = adapter.run(_request(), _context())

    assert result.status == "completed"
    assert result.assistant_output == "a.txt contains file text"
    assert len(model.messages_by_call) == 2
    second_call_messages = model.messages_by_call[1]
    assert second_call_messages[-1].content == "file text"
    assert second_call_messages[-1].tool_call_id == "read_file_0"
    assert calls == [
        (
            "sess_1",
            "run_1",
            "read_file",
            {"path": "a.txt"},
            {
                "workspace_root": "/repo",
                "artifact_root": "/repo/.sessions/sess_1/artifacts",
                "approval_mode": "yolo",
                "cancellation_token": None,
                "timeout_seconds": 30,
                "metadata": {},
            },
        )
    ]


def test_langchain_adapter_stops_after_phase_0_tool_call_iteration_limit() -> None:
    class RepeatingToolModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return type(
                "Response",
                (),
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"read_file_{self.calls}",
                            "name": "read_file",
                            "args": {"path": "a.txt"},
                        }
                    ],
                    "usage": {},
                },
            )()

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    model = RepeatingToolModel()
    adapter = LangChainAgentLoopAdapter(model=model, tool_broker=RecordingBroker())

    result = adapter.run(_request(), _context())

    assert model.calls == MAX_TOOL_CALL_ITERATIONS
    assert result.status == "failed"
    assert result.error["error_class"] == "internal_error"
    assert "iteration limit" in result.error["message"]


def test_langchain_adapter_converts_runtime_tool_schemas_to_structured_tools() -> None:
    tools = _langchain_tools(_request(), _context(), tool_broker=object())

    assert len(tools) == 1
    assert tools[0].name == "read_file"
    assert tools[0].description == "Read a file"
    assert tools[0].args_schema == {
        "type": "object",
        "properties": {},
        "required": [],
    }


def test_langchain_adapter_binds_generated_tools_before_model_invoke() -> None:
    class BindingModel(FakeChatModel):
        def __init__(self) -> None:
            super().__init__(response="bound answer")
            self.bound_tools = None

        def bind_tools(self, tools):
            self.bound_tools = tools
            return self

    model = BindingModel()
    adapter = LangChainAgentLoopAdapter(model=model, tool_broker=object())

    result = adapter.run(_request(), _context())

    assert result.status == "completed"
    assert result.assistant_output == "bound answer"
    assert model.bound_tools is not None
    assert [tool.name for tool in model.bound_tools] == ["read_file"]


def test_generated_langchain_tool_callable_delegates_only_to_toolbroker() -> None:
    calls = []

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            calls.append((session_id, run_id, tool_name, arguments, context))
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    tools = _langchain_tools(_request(), _context(), tool_broker=RecordingBroker())

    result = tools[0].invoke({"path": "a.txt"})

    assert result == {
        "status": "ok",
        "output": "file text",
        "error": None,
        "artifacts": [],
        "metadata": {},
        "redacted_output": None,
    }
    assert calls == [
        (
            "sess_1",
            "run_1",
            "read_file",
            {"path": "a.txt"},
            {
                "workspace_root": "/repo",
                "artifact_root": "/repo/.sessions/sess_1/artifacts",
                "approval_mode": "yolo",
                "cancellation_token": None,
                "timeout_seconds": 30,
                "metadata": {},
            },
        )
    ]

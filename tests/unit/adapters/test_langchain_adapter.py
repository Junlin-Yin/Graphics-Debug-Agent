from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

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
from debug_agent.runtime.model_context import ConversationMessage, ModelContextFrame
from debug_agent.runtime.stream_events import AgentStreamEvent


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


def _frame_request() -> AgentRunRequest:
    return AgentRunRequest(
        session_id="sess_1",
        run_id="run_1",
        user_input="legacy user must be ignored",
        system_prompt="legacy system must be ignored",
        conversation=[{"role": "user", "content": "legacy conversation must be ignored"}],
        tools=[
            {
                "name": "legacy_tool",
                "description": "Legacy tool must be ignored",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }
        ],
        model_context_frame=ModelContextFrame(
            message_segments=[
                ConversationMessage(
                    seq=20,
                    role="user",
                    kind="current_user_input",
                    turn_id="turn-1",
                    model_call_id=None,
                    tool_call_id=None,
                    content="frame user",
                ),
                ConversationMessage(
                    seq=10,
                    role="system",
                    kind="main_agent_system_prompt",
                    turn_id=None,
                    model_call_id=None,
                    tool_call_id=None,
                    content="frame system",
                ),
            ],
            tool_schema_bindings=[
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                }
            ],
        ),
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


def _event_payloads(events: list[AgentStreamEvent], kind: str) -> list[dict[str, Any]]:
    return [event.payload for event in events if event.kind == kind]


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


def test_langchain_adapter_materializes_messages_from_model_context_frame() -> None:
    model = FakeChatModel(response="answer")

    result = LangChainAgentLoopAdapter(model=model).run(_frame_request(), _context())

    assert result.status == "completed"
    assert model.messages == [
        {"role": "system", "content": "frame system"},
        {"role": "user", "content": "frame user"},
    ]
    assert "legacy" not in "\n".join(message["content"] for message in model.messages)


def test_langchain_adapter_binds_tools_from_model_context_frame() -> None:
    class BindingModel(FakeChatModel):
        def __init__(self) -> None:
            super().__init__(response="bound answer")
            self.bound_tools = None

        def bind_tools(self, tools):
            self.bound_tools = tools
            return self

    model = BindingModel()

    result = LangChainAgentLoopAdapter(model=model, tool_broker=object()).run(
        _frame_request(),
        _context(),
    )

    assert result.status == "completed"
    assert [tool.name for tool in model.bound_tools] == ["read_file"]


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


def test_langchain_adapter_stream_emits_model_lifecycle_and_text_deltas() -> None:
    events = []
    model = FakeChatModel(
        response="unused",
        stream_chunks=["hel", "lo"],
        usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    )

    result = LangChainAgentLoopAdapter(model=model).stream(
        _request(),
        _context(),
        events.append,
    )

    assert result.status == "completed"
    assert result.assistant_output == "hello"
    assert result.usage == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    assert "streaming_fallback" not in result.metadata
    assert [event.kind for event in events] == [
        "stream_model_call_started",
        "stream_text_delta",
        "stream_text_delta",
        "stream_model_call_completed",
    ]
    model_call_id = events[0].payload["model_call_id"]
    assert _event_payloads(events, "stream_text_delta") == [
        {"model_call_id": model_call_id, "text": "hel"},
        {"model_call_id": model_call_id, "text": "lo"},
    ]
    assert events[-1].payload == {
        "model_call_id": model_call_id,
        "is_final": True,
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        "duration_ms": events[-1].payload["duration_ms"],
    }
    assert events[-1].payload["duration_ms"] >= 0


def test_langchain_adapter_stream_keeps_intermediate_text_out_of_final_answer() -> None:
    events = []

    class ToolStreamingModel:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield type(
                    "Chunk",
                    (),
                    {
                        "content": "checking file",
                        "tool_calls": [
                            {
                                "id": "tool_1",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
                return
            yield type(
                "Chunk",
                (),
                {"content": "final answer", "tool_calls": [], "usage": {}},
            )()

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={"duration_ms": 4},
                redacted_output=None,
            )

    result = LangChainAgentLoopAdapter(
        model=ToolStreamingModel(),
        tool_broker=RecordingBroker(),
    ).stream(_request(), _context(), events.append)

    assert result.status == "completed"
    assert result.assistant_output == "final answer"
    assert [payload["text"] for payload in _event_payloads(events, "stream_text_delta")] == [
        "checking file",
        "final answer",
    ]
    completed_payloads = _event_payloads(events, "stream_model_call_completed")
    assert completed_payloads[0]["is_final"] is False
    assert completed_payloads[1]["is_final"] is True


def test_langchain_adapter_stream_filters_non_displayable_chunks() -> None:
    events = []

    class NonDisplayStreamingModel:
        def stream(self, messages):
            yield type(
                "Chunk",
                (),
                {"content": "", "tool_calls": [], "usage": {}, "tool_call_chunks": [{"name": "read_file"}]},
            )()
            yield type(
                "Chunk",
                (),
                {"content": "", "tool_calls": [], "usage": {}, "additional_kwargs": {"function_call": {"name": "read_file"}}},
            )()
            yield type(
                "Chunk",
                (),
                {"content": "answer", "tool_calls": [], "usage": {}},
            )()

    result = LangChainAgentLoopAdapter(model=NonDisplayStreamingModel()).stream(
        _request(),
        _context(),
        events.append,
    )

    assert result.assistant_output == "answer"
    assert _event_payloads(events, "stream_text_delta") == [
        {"model_call_id": events[0].payload["model_call_id"], "text": "answer"}
    ]


def test_langchain_adapter_stream_extracts_structured_text_chunks() -> None:
    events = []

    class StructuredTextStreamingModel:
        def stream(self, messages):
            yield AIMessageChunk(content=[])
            yield AIMessageChunk(content=[{"type": "text", "text": "structured "}])
            yield AIMessageChunk(content=[{"type": "text", "text": "answer"}])
            yield AIMessageChunk(content=[])

    result = LangChainAgentLoopAdapter(model=StructuredTextStreamingModel()).stream(
        _request(),
        _context(),
        events.append,
    )

    assert result.status == "completed"
    assert result.assistant_output == "structured answer"
    assert _event_payloads(events, "stream_text_delta") == [
        {"model_call_id": events[0].payload["model_call_id"], "text": "structured "},
        {"model_call_id": events[0].payload["model_call_id"], "text": "answer"},
    ]


def test_langchain_adapter_run_extracts_structured_response_text() -> None:
    class StructuredTextModel:
        def invoke(self, messages):
            return AIMessage(content=[{"type": "text", "text": "structured answer"}])

    result = LangChainAgentLoopAdapter(model=StructuredTextModel()).run(
        _request(),
        _context(),
    )

    assert result.status == "completed"
    assert result.assistant_output == "structured answer"


def test_langchain_adapter_stream_reconstructs_tool_call_chunks_before_invocation() -> None:
    events = []
    broker_calls = []

    class ToolChunkStreamingModel:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield type(
                    "Chunk",
                    (),
                    {
                        "content": "",
                        "tool_calls": [],
                        "tool_call_chunks": [
                            {
                                "id": "read_file_0",
                                "index": 0,
                                "name": "read_file",
                                "args": '{"path":',
                            }
                        ],
                        "usage": {},
                    },
                )()
                yield type(
                    "Chunk",
                    (),
                    {
                        "content": "",
                        "tool_calls": [],
                        "tool_call_chunks": [
                            {
                                "id": "read_file_0",
                                "index": 0,
                                "name": None,
                                "args": '"docs/prompt-templates/planning.md"}',
                            }
                        ],
                        "usage": {},
                    },
                )()
                return
            yield type(
                "Chunk",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            broker_calls.append((tool_name, arguments))
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    result = LangChainAgentLoopAdapter(
        model=ToolChunkStreamingModel(),
        tool_broker=RecordingBroker(),
    ).stream(_request(), _context(), events.append)

    assert result.status == "completed"
    assert broker_calls == [
        ("read_file", {"path": "docs/prompt-templates/planning.md"})
    ]
    assert _event_payloads(events, "stream_tool_call_started") == [
        {
            "tool_call_id": "model_call_1_tool_1",
            "model_call_id": _event_payloads(events, "stream_model_call_started")[0][
                "model_call_id"
            ],
            "name": "read_file",
            "args": {"path": "docs/prompt-templates/planning.md"},
        }
    ]


def test_langchain_adapter_stream_emits_tool_events_with_runtime_tool_id() -> None:
    events = []

    class ToolStreamingModel:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield type(
                    "Chunk",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "provider_tool_1",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
                return
            yield type(
                "Chunk",
                (),
                {"content": "used tool", "tool_calls": [], "usage": {}},
            )()

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output={"text": "hello"},
                error=None,
                artifacts=["art_1"],
                metadata={"duration_ms": 12},
                redacted_output="hello",
            )

    result = LangChainAgentLoopAdapter(
        model=ToolStreamingModel(),
        tool_broker=RecordingBroker(),
    ).stream(_request(), _context(), events.append)

    assert result.status == "completed"
    assert result.tool_results[0]["output"] == {"text": "hello"}
    assert _event_payloads(events, "stream_tool_call_started") == [
        {
            "tool_call_id": "model_call_1_tool_1",
            "model_call_id": _event_payloads(events, "stream_model_call_started")[0]["model_call_id"],
            "name": "read_file",
            "args": {"path": "notes.txt"},
        }
    ]
    assert _event_payloads(events, "stream_tool_call_completed") == [
        {
            "tool_call_id": "model_call_1_tool_1",
            "model_call_id": _event_payloads(events, "stream_model_call_started")[0]["model_call_id"],
            "name": "read_file",
            "status": "ok",
            "duration_ms": 12,
        }
    ]
    assert _event_payloads(events, "stream_tool_result") == [
        {
            "tool_call_id": "model_call_1_tool_1",
            "model_call_id": _event_payloads(events, "stream_model_call_started")[0]["model_call_id"],
            "output": {"text": "hello"},
            "redacted_output": "hello",
            "artifact_ids": ["art_1"],
        }
    ]


def test_langchain_adapter_stream_ignores_empty_name_tool_calls() -> None:
    events = []
    broker_calls = []

    class EmptyNameToolStreamingModel:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield type(
                    "Chunk",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "valid_tool",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            },
                            {"id": "empty_tool", "name": "", "args": {}},
                        ],
                        "usage": {},
                    },
                )()
                return
            yield type(
                "Chunk",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            broker_calls.append((tool_name, arguments))
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    result = LangChainAgentLoopAdapter(
        model=EmptyNameToolStreamingModel(),
        tool_broker=RecordingBroker(),
    ).stream(_request(), _context(), events.append)

    assert result.status == "completed"
    assert broker_calls == [("read_file", {"path": "notes.txt"})]
    assert _event_payloads(events, "stream_tool_call_started") == [
        {
            "tool_call_id": "model_call_1_tool_1",
            "model_call_id": _event_payloads(events, "stream_model_call_started")[0]["model_call_id"],
            "name": "read_file",
            "args": {"path": "notes.txt"},
        }
    ]
    assert all(
        payload.get("name") != ""
        for kind in ("stream_tool_call_started", "stream_tool_call_completed")
        for payload in _event_payloads(events, kind)
    )


def test_langchain_adapter_stream_generates_distinct_tool_ids_for_duplicate_names() -> None:
    events = []

    class DuplicateToolStreamingModel:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield type(
                    "Chunk",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {"name": "read_file", "args": {"path": "a.txt"}},
                            {"name": "read_file", "args": {"path": "b.txt"}},
                        ],
                        "usage": {},
                    },
                )()
                return
            yield type(
                "Chunk",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output=arguments["path"],
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    result = LangChainAgentLoopAdapter(
        model=DuplicateToolStreamingModel(),
        tool_broker=RecordingBroker(),
    ).stream(_request(), _context(), events.append)

    started_ids = [
        payload["tool_call_id"]
        for payload in _event_payloads(events, "stream_tool_call_started")
    ]
    completed_ids = [
        payload["tool_call_id"]
        for payload in _event_payloads(events, "stream_tool_call_completed")
    ]
    result_ids = [
        payload["tool_call_id"]
        for payload in _event_payloads(events, "stream_tool_result")
    ]
    assert result.status == "completed"
    assert len(started_ids) == 2
    assert len(set(started_ids)) == 2
    assert completed_ids == started_ids
    assert result_ids == started_ids


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


def test_langchain_adapter_times_out_blocking_stream_call() -> None:
    events = []
    stream_events = []

    class SlowStreamingModel:
        stream_chunks = ["too late"]

        def stream(self, messages):
            time.sleep(0.2)
            yield AIMessageChunk(content="too late")

        def invoke(self, messages):
            raise AssertionError("streaming timeout must not fall back to invoke")

    request = AgentRunRequest(
        session_id="sess_1",
        run_id="run_1",
        user_input="hello",
        system_prompt="system prompt",
        conversation=[],
        tools=[],
        model_config={"provider": "fake", "model": "slow-stream"},
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
    result = LangChainAgentLoopAdapter(model=SlowStreamingModel()).stream(
        request,
        context,
        stream_events.append,
    )
    duration = time.monotonic() - started

    assert result.status == "timeout"
    assert result.error["error_class"] == "timeout"
    assert duration < 0.15
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]
    assert events[-1][1]["error_class"] == "timeout"
    assert [event.kind for event in stream_events] == ["stream_model_call_started"]


def test_langchain_adapter_stream_timeout_resets_after_each_chunk() -> None:
    events = []
    stream_events = []

    class ActiveStreamingModel:
        stream_chunks = ["one", "two", "three"]

        def stream(self, messages):
            for chunk in self.stream_chunks:
                time.sleep(0.03)
                yield AIMessageChunk(content=chunk)

        def invoke(self, messages):
            raise AssertionError("streaming path must not fall back to invoke")

    request = AgentRunRequest(
        session_id="sess_1",
        run_id="run_1",
        user_input="hello",
        system_prompt="system prompt",
        conversation=[],
        tools=[],
        model_config={"provider": "fake", "model": "active-stream"},
        timeout_seconds=0.05,
    )
    context = RunContext(
        workspace_root="/repo",
        artifact_root="/repo/.sessions/sess_1/artifacts",
        approval_mode="yolo",
        cancellation_token=None,
        metadata={},
        model_event_recorder=lambda kind, payload: events.append((kind, payload)),
    )

    result = LangChainAgentLoopAdapter(model=ActiveStreamingModel()).stream(
        request,
        context,
        stream_events.append,
    )

    assert result.status == "completed"
    assert result.assistant_output == "onetwothree"
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_completed",
    ]
    assert [event.kind for event in stream_events] == [
        "stream_model_call_started",
        "stream_text_delta",
        "stream_text_delta",
        "stream_text_delta",
        "stream_model_call_completed",
    ]


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
    assert second_call_messages[-1].tool_call_id == "model_call_1_tool_1"
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


def test_langchain_adapter_stream_propagates_tool_display_metadata_from_broker_events() -> None:
    events: list[AgentStreamEvent] = []
    recorded_events: list[tuple[str, dict[str, Any]]] = []

    class ToolStreamingModel:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield type(
                    "Chunk",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "provider_tool_1",
                                "name": "shell_exec",
                                "args": {"argv": ["pytest", "tests"]},
                            }
                        ],
                        "usage": {},
                    },
                )()
                return
            yield type(
                "Chunk",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            context["tool_audit_recorder"](
                "tool_call_completed",
                {
                    "tool_name": "shell_exec",
                    "target": "pytest tests",
                    "status": "ok",
                    "execution_duration_ms": 1400,
                    "approval_wait_duration_ms": 0,
                    "result": {},
                },
            )
            return ToolResult(
                status="ok",
                output={"stdout": "ok\n", "stderr": "", "returncode": 0},
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    context = RunContext(
        workspace_root="/repo",
        artifact_root="/repo/.sessions/sess_1/artifacts",
        approval_mode="yolo",
        cancellation_token=None,
        metadata={},
        model_event_recorder=lambda kind, payload: recorded_events.append((kind, payload)),
    )

    result = LangChainAgentLoopAdapter(
        model=ToolStreamingModel(),
        tool_broker=RecordingBroker(),
    ).stream(_request(), context, events.append)

    assert result.status == "completed"
    assert _event_payloads(events, "stream_tool_call_completed") == [
        {
            "tool_call_id": "model_call_1_tool_1",
            "model_call_id": _event_payloads(events, "stream_model_call_started")[0]["model_call_id"],
            "name": "shell_exec",
            "status": "ok",
            "target": "pytest tests",
            "execution_duration_ms": 1400,
            "duration_ms": 1400,
        }
    ]
    assert _event_payloads(events, "stream_tool_result") == [
        {
            "tool_call_id": "model_call_1_tool_1",
            "model_call_id": _event_payloads(events, "stream_model_call_started")[0]["model_call_id"],
            "output": {"stdout": "ok\n", "stderr": "", "returncode": 0},
            "redacted_output": None,
            "artifact_ids": [],
        }
    ]


def test_langchain_adapter_preserves_runtime_tool_call_ids_after_context_refresh() -> None:
    from langchain_core.messages import convert_to_messages

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

    class AnthropicLikeToolLoopModel:
        def __init__(self) -> None:
            self.messages_by_call = []

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.messages_by_call.append(messages)
            if len(self.messages_by_call) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "read_file_0",
                            "name": "read_file",
                            "args": {"path": "a.txt"},
                        }
                    ],
                )
            converted = convert_to_messages(messages)
            assert converted[-2].tool_calls[0]["id"] == "model_call_1_tool_1"
            assert converted[-1].tool_call_id == "model_call_1_tool_1"
            return AIMessage(content="a.txt contains file text")

    def refresh_model_context_frame(tool_loop_messages):
        converted = []
        for index, message in enumerate(tool_loop_messages, start=1):
            role = getattr(message, "type", None)
            if role == "ai":
                role = "assistant"
            content = getattr(message, "content", "")
            if role == "assistant":
                content = {
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "name": call["name"],
                            "args": call.get("args", {}),
                        }
                        for call in getattr(message, "tool_calls", [])
                    ],
                }
            converted.append(
                ConversationMessage(
                    seq=index,
                    role=str(role),
                    kind="tool_result" if role == "tool" else "tool_call",
                    turn_id="turn-1",
                    model_call_id=None,
                    tool_call_id=getattr(message, "tool_call_id", None),
                    content={
                        "message_type": "tool_result",
                        "content": content,
                        "tool_call_id": getattr(message, "tool_call_id"),
                    }
                    if role == "tool"
                    else content,
                )
            )
        return ModelContextFrame(
            message_segments=[
                ConversationMessage(
                    seq=0,
                    role="user",
                    kind="current_user_input",
                    turn_id="turn-1",
                    model_call_id=None,
                    tool_call_id=None,
                    content="hello",
                ),
                *converted,
            ],
            tool_schema_bindings=_frame_request().model_context_frame.tool_schema_bindings,
        )

    model = AnthropicLikeToolLoopModel()
    context = _context()
    context.metadata["refresh_model_context_frame"] = refresh_model_context_frame

    result = LangChainAgentLoopAdapter(model=model, tool_broker=RecordingBroker()).run(
        _frame_request(),
        context,
    )

    assert result.status == "completed"
    assert result.assistant_output == "a.txt contains file text"


def test_langchain_adapter_namespaces_repeated_provider_tool_call_ids() -> None:
    from langchain_core.messages import convert_to_messages

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output=f"{arguments['path']} text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    class RepeatedProviderIdModel:
        def __init__(self) -> None:
            self.messages_by_call = []

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.messages_by_call.append(messages)
            if len(self.messages_by_call) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "duplicate_tool_id",
                            "name": "read_file",
                            "args": {"path": "a.txt"},
                        }
                    ],
                )
            converted = convert_to_messages(messages)
            if len(self.messages_by_call) == 2:
                assert converted[-2].tool_calls[0]["id"] == "model_call_1_tool_1"
                assert converted[-1].tool_call_id == "model_call_1_tool_1"
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "duplicate_tool_id",
                            "name": "read_file",
                            "args": {"path": "b.txt"},
                        }
                    ],
                )
            assert converted[-2].tool_calls[0]["id"] == "model_call_2_tool_1"
            assert converted[-1].tool_call_id == "model_call_2_tool_1"
            return AIMessage(content="done")

    model = RepeatedProviderIdModel()
    result = LangChainAgentLoopAdapter(model=model, tool_broker=RecordingBroker()).run(
        _request(),
        _context(),
    )

    assert result.status == "completed"
    assert result.assistant_output == "done"


def test_langchain_adapter_short_circuits_same_turn_after_approval_denial() -> None:
    class ApprovalDeniedBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="denied",
                output=None,
                error={
                    "error_class": "policy_denied",
                    "message": "Approval denied.",
                    "source": "toolbroker",
                    "recoverable": True,
                },
                artifacts=[],
                metadata={"turn_aborted": True},
                redacted_output=None,
            )

    class ToolThenAnswerModel:
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
                                "args": {"path": "outside.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "should not be called", "tool_calls": [], "usage": {}},
            )()

    model = ToolThenAnswerModel()

    result = LangChainAgentLoopAdapter(
        model=model,
        tool_broker=ApprovalDeniedBroker(),
    ).run(_request(), _context())

    assert model.calls == 1
    assert result.status == "failed"
    assert result.error["error_class"] == "policy_denied"
    assert result.metadata["failure_scope"] == "turn"
    assert result.metadata["approval_denied_abort"] is True
    assert result.tool_results[0]["metadata"]["turn_aborted"] is True


def test_langchain_adapter_materializes_denied_terminal_observation_for_next_turn() -> None:
    from langchain_core.messages import convert_to_messages

    denied_tool_call = ConversationMessage(
        seq=30,
        role="assistant",
        kind="tool_call",
        turn_id="turn-1",
        model_call_id="repl_turn_1_denied",
        tool_call_id=None,
        content={
            "content": "",
            "tool_calls": [
                {
                    "id": "read_file_0",
                    "name": "read_file",
                    "args": {"path": "outside.txt"},
                }
            ],
        },
        metadata={"terminal_observation": True},
    )
    denied_observation = ConversationMessage(
        seq=40,
        role="tool",
        kind="tool_result",
        turn_id="turn-1",
        model_call_id=None,
        tool_call_id="read_file_0",
        content={
            "message_type": "tool_result",
            "content": "Approval denied.",
            "tool_call_id": "read_file_0",
        },
        metadata={
            "turn_aborted": True,
            "tool_call_id": "read_file_0",
            "terminal_observation": True,
        },
    )

    class ProviderValidatingModel:
        def invoke(self, messages):
            converted = convert_to_messages(messages)
            assert converted[-1].tool_call_id == "read_file_0"
            return AIMessage(content="saw denial")

    request = _frame_request()
    request.model_context_frame.message_segments.extend(
        [denied_tool_call, denied_observation]
    )

    result = LangChainAgentLoopAdapter(model=ProviderValidatingModel()).run(
        request,
        _context(),
    )

    assert result.status == "completed"
    assert result.assistant_output == "saw denial"


def test_langchain_adapter_aggregates_usage_across_tool_loop_model_calls() -> None:
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
                                "args": {"path": "a.txt"},
                            }
                        ],
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "total_tokens": 5,
                        },
                    },
                )()
            return type(
                "Response",
                (),
                {
                    "content": "done",
                    "tool_calls": [],
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 11,
                        "total_tokens": 18,
                    },
                },
            )()

    result = LangChainAgentLoopAdapter(
        model=ToolLoopModel(),
        tool_broker=RecordingBroker(),
    ).run(_request(), _context())

    assert result.status == "completed"
    assert result.usage == {
        "input_tokens": 9,
        "output_tokens": 14,
        "total_tokens": 23,
    }


def test_langchain_adapter_stream_aggregates_usage_across_tool_loop_model_calls() -> None:
    events = []

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

    class ToolLoopStreamingModel:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield type(
                    "Chunk",
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
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "total_tokens": 5,
                        },
                    },
                )()
                return
            yield type(
                "Chunk",
                (),
                {
                    "content": "done",
                    "tool_calls": [],
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 11,
                        "total_tokens": 18,
                    },
                },
            )()

    result = LangChainAgentLoopAdapter(
        model=ToolLoopStreamingModel(),
        tool_broker=RecordingBroker(),
    ).stream(_request(), _context(), events.append)

    assert result.status == "completed"
    assert result.usage == {
        "input_tokens": 9,
        "output_tokens": 14,
        "total_tokens": 23,
    }


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
    assert result.metadata == {"failure_scope": "turn"}


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

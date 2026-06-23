from __future__ import annotations

import json
import asyncio
import threading
import time
from dataclasses import asdict, replace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from debug_agent.adapters.langchain_adapter import (
    LangChainAgentLoopAdapter,
    _langchain_tools,
    _provider_messages_from_segments,
    _tool_observation,
)
from debug_agent.adapters.model_factory import FakeChatModel
from debug_agent.adapters.vision_client import VisionImageInput, project_chat_completions_request
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
from debug_agent.runtime.provider_execution import ProviderBoundaryNotClosed
from debug_agent.runtime.prompt_executor import make_compression_model_callable
from debug_agent.runtime.settings import DEFAULT_AGENT_LOOP_MAX_TOOL_CALL_ITERATIONS
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


def test_tool_observation_preserves_redacted_output_for_durable_replay() -> None:
    observation = _tool_observation(
        {
            "status": "ok",
            "output": "raw secret output",
            "redacted_output": "[redacted output]",
            "artifacts": [],
            "metadata": {"tool_name": "view_image"},
        },
        tool_call_id="model_call_1_tool_1",
    )

    assert observation["content"] == "raw secret output"
    assert observation["redacted_output"] == "[redacted output]"


def _event_payloads(events: list[AgentStreamEvent], kind: str) -> list[dict[str, Any]]:
    return [event.payload for event in events if event.kind == kind]


def test_provider_messages_from_segments_projects_runtime_as_user_not_system() -> None:
    messages = _provider_messages_from_segments(
        [
            ConversationMessage(
                seq=1,
                role="runtime",
                kind="failure_fact",
                turn_id="turn-1",
                model_call_id=None,
                tool_call_id=None,
                content={
                    "error_class": "tool_error",
                    "reason": "tool_execution_timeout",
                    "message": "shell_exec exceeded timeout_seconds.",
                    "artifact_ids": [],
                },
            )
        ]
    )

    assert messages == [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "error_class": "tool_error",
                    "reason": "tool_execution_timeout",
                    "message": "shell_exec exceeded timeout_seconds.",
                    "artifact_ids": [],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
    ]


def test_langchain_adapter_maps_model_success() -> None:
    adapter = LangChainAgentLoopAdapter(model=FakeChatModel(response="answer"))

    result = adapter.run(_request(), _context())

    assert result.status == "completed"
    assert result.assistant_output == "answer"
    assert result.error is None
    assert result.usage["input_tokens"] > 0
    assert result.usage["output_tokens"] > 0
    assert result.usage["total_tokens"] == (
        result.usage["input_tokens"] + result.usage["output_tokens"]
    )
    assert result.metadata["token_source"] == "estimated"
    assert adapter.model.messages[0]["role"] == "system"
    assert "runtime safety" in adapter.model.messages[0]["content"]
    assert adapter.model.messages[-1]["role"] == "user"
    assert adapter.model.messages[-1]["content"] == "hello"


def test_langchain_adapter_omits_thinking_request_options_when_disabled() -> None:
    class RecordingModel:
        def __init__(self) -> None:
            self.kwargs = None

        def invoke(self, messages, **kwargs):
            self.kwargs = kwargs
            return type(
                "Response",
                (),
                {"content": "answer", "tool_calls": [], "usage": {}},
            )()

    model = RecordingModel()
    request = replace(
        _request(),
        model_config={
            "provider": "fake",
            "thinking": {"enabled": False, "effort": "high"},
        },
    )

    result = LangChainAgentLoopAdapter(model=model).run(request, _context())

    assert result.status == "completed"
    assert model.kwargs == {}


def test_langchain_adapter_does_not_enable_thinking_from_effort_alone() -> None:
    class RecordingModel:
        def __init__(self) -> None:
            self.kwargs = None

        def invoke(self, messages, **kwargs):
            self.kwargs = kwargs
            return type(
                "Response",
                (),
                {"content": "answer", "tool_calls": [], "usage": {}},
            )()

    model = RecordingModel()
    request = replace(
        _request(),
        model_config={"provider": "fake", "thinking": {"effort": "low"}},
    )

    result = LangChainAgentLoopAdapter(model=model).run(request, _context())

    assert result.status == "completed"
    assert model.kwargs == {}


def test_langchain_adapter_does_not_send_per_call_thinking_kwargs() -> None:
    class RecordingModel:
        def __init__(self) -> None:
            self.kwargs = None

        def invoke(self, messages, **kwargs):
            self.kwargs = kwargs
            return type(
                "Response",
                (),
                {"content": "answer", "tool_calls": [], "usage": {}},
            )()

    model = RecordingModel()
    request = replace(
        _request(),
        model_config={
            "provider": "fake",
            "thinking": {"enabled": True, "effort": "medium"},
        },
    )

    result = LangChainAgentLoopAdapter(model=model).run(request, _context())

    assert result.status == "completed"
    assert model.kwargs == {}


def test_langchain_adapter_stream_does_not_send_per_call_thinking_kwargs() -> None:
    class RecordingStreamingModel:
        stream_chunks = ["answer"]

        def __init__(self) -> None:
            self.kwargs = None

        def stream(self, messages, **kwargs):
            self.kwargs = kwargs
            yield type(
                "Chunk",
                (),
                {"content": "answer", "tool_calls": [], "usage": {}},
            )()

    model = RecordingStreamingModel()
    request = replace(
        _request(),
        model_config={
            "provider": "fake",
            "thinking": {"enabled": True, "effort": "high"},
        },
    )

    result = LangChainAgentLoopAdapter(model=model).stream(
        request,
        _context(),
        lambda _event: None,
    )

    assert result.status == "completed"
    assert model.kwargs == {}


def test_langchain_adapter_strips_thinking_blocks_but_preserves_text_blocks() -> None:
    class ThinkingTextModel:
        def invoke(self, messages):
            return type(
                "Response",
                (),
                {
                    "content": [
                        {"type": "thinking", "thinking": "hidden reasoning"},
                        {"type": "text", "text": "visible "},
                        {"type": "thinking", "thinking": "more hidden"},
                        {"type": "text", "text": "answer"},
                    ],
                    "tool_calls": [],
                    "usage": {},
                },
            )()

    events = []
    result = LangChainAgentLoopAdapter(model=ThinkingTextModel()).run(
        _request(),
        replace(_context(), model_event_recorder=lambda kind, payload: events.append((kind, payload))),
    )

    assert result.status == "completed"
    assert result.assistant_output == "visible answer"
    assert "hidden" not in json.dumps(asdict(result), ensure_ascii=False)
    completed_payload = [payload for kind, payload in events if kind == "model_call_completed"][0]
    assert completed_payload["content"] == "visible answer"
    assert "hidden" not in json.dumps(completed_payload, ensure_ascii=False)


def test_langchain_adapter_preserves_tool_use_blocks_when_stripping_thinking() -> None:
    calls = []

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            calls.append((tool_name, arguments))
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    class ThinkingToolUseModel:
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
                        "content": [
                            {"type": "thinking", "thinking": "hidden tool plan"},
                            {"type": "text", "text": "checking"},
                            {
                                "type": "tool_use",
                                "id": "provider_tool_1",
                                "name": "read_file",
                                "input": {"path": "a.txt"},
                            },
                        ],
                        "tool_calls": [],
                        "usage": {},
                    },
                )()
            assert "hidden tool plan" not in str(messages)
            return type(
                "Response",
                (),
                {"content": "used tool", "tool_calls": [], "usage": {}},
            )()

    model = ThinkingToolUseModel()

    result = LangChainAgentLoopAdapter(
        model=model,
        tool_broker=RecordingBroker(),
    ).run(_request(), _context())

    assert result.status == "completed"
    assert result.assistant_output == "used tool"
    assert calls == [("read_file", {"path": "a.txt"})]
    assert len(model.messages_by_call) == 2
    assistant_message = model.messages_by_call[1][-2]
    assert assistant_message.content == "checking"
    assert [
        {
            "id": call["id"],
            "name": call["name"],
            "args": call["args"],
        }
        for call in assistant_message.tool_calls
    ] == [
        {
            "id": "model_call_1_tool_1",
            "name": "read_file",
            "args": {"path": "a.txt"},
        }
    ]


def test_langchain_adapter_conversation_writeback_excludes_thinking_on_tool_denial() -> None:
    class DenyingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="denied",
                output=None,
                error={
                    "error_class": "policy_error",
                    "reason": "approval_denied",
                    "message": "Turn cancelled by user.",
                    "source": "toolbroker",
                    "recoverable": True,
                },
                artifacts=[],
                metadata={"turn_aborted": True},
                redacted_output=None,
            )

    class ThinkingToolUseModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return type(
                "Response",
                (),
                {
                    "content": [
                        {"type": "thinking", "thinking": "hidden denial plan"},
                        {"type": "text", "text": "checking"},
                        {
                            "type": "tool_use",
                            "id": "provider_tool_1",
                            "name": "read_file",
                            "input": {"path": "a.txt"},
                        },
                    ],
                    "tool_calls": [],
                    "usage": {},
                },
            )()

    result = LangChainAgentLoopAdapter(
        model=ThinkingToolUseModel(),
        tool_broker=DenyingBroker(),
    ).run(_request(), _context())

    assert result.status == "failed"
    assert "hidden denial plan" not in json.dumps(result.metadata, ensure_ascii=False)
    assert result.metadata["turn_tool_loop_messages"][0]["content"] == {
        "content": "checking",
        "tool_calls": [
            {
                "id": "model_call_1_tool_1",
                "name": "read_file",
                "args": {"path": "a.txt"},
            }
        ],
    }


def test_view_image_projection_remains_thinking_disabled() -> None:
    projected = project_chat_completions_request(
        model="kimi-k2.5",
        images=[VisionImageInput(mime_type="image/png", data=b"png")],
        instruction="inspect",
        max_tokens=128,
    )

    assert projected["thinking"] == {"type": "disabled"}
    assert "effort" not in projected


def test_compression_model_callable_does_not_send_thinking_options() -> None:
    class RecordingCompressionModel:
        def __init__(self) -> None:
            self.kwargs = None

        def invoke(self, messages, **kwargs):
            self.kwargs = kwargs
            return type("Response", (), {"content": "compressed"})()

    model = RecordingCompressionModel()
    compression = make_compression_model_callable(model)
    frame = ModelContextFrame(
        message_segments=[
            ConversationMessage(
                seq=1,
                role="user",
                kind="compressed_history",
                turn_id="turn-1",
                model_call_id=None,
                tool_call_id=None,
                content="old content",
            )
        ]
    )
    compression_frame = type(
        "CompressionFrame",
        (),
        {
            "previous_summary": None,
            "evicted_messages": frame.message_segments,
            "instruction_segment": ConversationMessage(
                seq=2,
                role="user",
                kind="compression_instruction",
                turn_id=None,
                model_call_id=None,
                tool_call_id=None,
                content="summarize",
            ),
        },
    )()

    assert compression(compression_frame) == "compressed"
    assert model.kwargs == {}


def test_langchain_adapter_run_uses_async_invoke_when_available() -> None:
    class AsyncOnlyModel:
        def __init__(self) -> None:
            self.messages = None
            self.ainvoke_called = False

        def invoke(self, _messages):
            raise AssertionError("sync invoke must not be used when ainvoke is available")

        async def ainvoke(self, messages):
            self.ainvoke_called = True
            self.messages = messages
            await asyncio.sleep(0)
            return type(
                "Response",
                (),
                {"content": "async answer", "tool_calls": [], "usage": {}},
            )()

    model = AsyncOnlyModel()

    result = LangChainAgentLoopAdapter(model=model).run(_request(), _context())

    assert result.status == "completed"
    assert result.assistant_output == "async answer"
    assert model.ainvoke_called is True
    assert model.messages[-1]["content"] == "hello"


def test_langchain_adapter_extracts_provider_finish_metadata_for_token_limit() -> None:
    class TokenLimitModel:
        def invoke(self, messages):
            return type(
                "Response",
                (),
                {
                    "content": "partial",
                    "tool_calls": [],
                    "usage": {"output_tokens": 128},
                    "response_metadata": {"stop_reason": "max_tokens"},
                },
            )()

    result = LangChainAgentLoopAdapter(model=TokenLimitModel()).run(_request(), _context())

    assert result.status == "completed"
    assert result.metadata["provider_finish"] == {
        "finish_reason": "max_tokens",
        "output_token_limit_reached": True,
    }


@pytest.mark.parametrize(
    ("response_attrs", "expected_usage"),
    [
        (
            {"usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}},
            {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        ),
        (
            {"usage_metadata": {"input_tokens": 7, "output_tokens": 11}},
            {"input_tokens": 7, "output_tokens": 11, "total_tokens": 18},
        ),
        (
            {
                "response_metadata": {
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 17,
                        "total_tokens": 30,
                    }
                }
            },
            {"input_tokens": 13, "output_tokens": 17, "total_tokens": 30},
        ),
    ],
)
def test_langchain_adapter_normalizes_provider_usage_sources(
    response_attrs: dict[str, Any],
    expected_usage: dict[str, int],
) -> None:
    class UsageModel:
        def invoke(self, messages):
            attrs = {
                "content": "answer",
                "tool_calls": [],
                **response_attrs,
            }
            return type("Response", (), attrs)()

    events = []

    result = LangChainAgentLoopAdapter(model=UsageModel()).run(
        _request(),
        replace(
            _context(),
            model_event_recorder=lambda kind, payload: events.append((kind, payload)),
        ),
    )

    assert result.status == "completed"
    assert result.usage == expected_usage
    completed_payload = [
        payload for kind, payload in events if kind == "model_call_completed"
    ][0]
    assert completed_payload["usage"] == expected_usage


def test_langchain_adapter_uses_estimates_for_whole_mixed_usage_window() -> None:
    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output="tool result",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    class MixedUsageToolModel:
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
                        "content": [
                            {"type": "text", "text": "checking"},
                            {
                                "type": "tool_use",
                                "id": "provider_tool_1",
                                "name": "read_file",
                                "input": {"path": "a.txt"},
                            },
                        ],
                        "tool_calls": [],
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 200,
                            "total_tokens": 300,
                        },
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "final", "tool_calls": [], "usage": {}},
            )()

    result = LangChainAgentLoopAdapter(
        model=MixedUsageToolModel(),
        tool_broker=RecordingBroker(),
    ).run(_request(), _context())

    assert result.status == "completed"
    assert result.metadata["provider_usage_available"] is False
    assert result.metadata["token_source"] == "estimated"
    assert result.metadata["estimated_usage"] == {
        **result.usage,
        "estimator_version": "deterministic-char-v1",
    }
    assert result.usage["total_tokens"] > 0
    assert result.usage["total_tokens"] != 300


def test_langchain_adapter_stream_extracts_provider_finish_metadata() -> None:
    class TokenLimitStreamingModel:
        stream_chunks = ["partial"]

        def stream(self, messages):
            yield type(
                "Chunk",
                (),
                {
                    "content": "partial",
                    "tool_calls": [],
                    "usage": {"output_tokens": 128},
                    "response_metadata": {"finish_reason": "length"},
                },
            )()

    events = []
    result = LangChainAgentLoopAdapter(model=TokenLimitStreamingModel()).stream(
        _request(),
        _context(),
        events.append,
    )

    assert result.status == "completed"
    assert result.metadata["provider_finish"] == {
        "finish_reason": "length",
        "output_token_limit_reached": True,
    }
    completed = _event_payloads(events, "stream_model_call_completed")[0]
    assert completed["provider_finish"]["finish_reason"] == "length"


def test_langchain_adapter_stream_uses_async_stream_when_available() -> None:
    class AsyncStreamingModel:
        def __init__(self) -> None:
            self.messages = None
            self.astream_called = False

        def stream(self, _messages):
            raise AssertionError("sync stream must not be used when astream is available")

        async def astream(self, messages):
            self.astream_called = True
            self.messages = messages
            await asyncio.sleep(0)
            yield type("Chunk", (), {"content": "async ", "tool_calls": [], "usage": {}})()
            await asyncio.sleep(0)
            yield type(
                "Chunk",
                (),
                {"content": "stream", "tool_calls": [], "usage": {"output_tokens": 2}},
            )()

    events = []
    model = AsyncStreamingModel()

    result = LangChainAgentLoopAdapter(model=model).stream(
        _request(),
        _context(),
        events.append,
    )

    assert result.status == "completed"
    assert result.assistant_output == "async stream"
    assert model.astream_called is True
    assert model.messages[-1]["content"] == "hello"
    assert [payload["text"] for payload in _event_payloads(events, "stream_text_delta")] == [
        "async ",
        "stream",
    ]


def test_langchain_adapter_has_no_public_cancel_placeholder() -> None:
    adapter = LangChainAgentLoopAdapter(model=FakeChatModel(response="answer"))

    assert not hasattr(adapter, "cancel")


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
        "estimated_usage": events[-1].payload["estimated_usage"],
        "duration_ms": events[-1].payload["duration_ms"],
    }
    assert events[-1].payload["estimated_usage"]["total_tokens"] > 0
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
                redacted_output="[redacted preview]",
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
                redacted_output="[redacted preview]",
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
    assert timeout.error["error_class"] == "model_error"
    assert timeout.error["reason"] == "model_call_timeout"
    assert cancelled.status == "cancelled"
    assert cancelled.error["error_class"] == "cancelled"


def test_langchain_adapter_times_out_blocking_model_call() -> None:
    events = []

    class SlowModel:
        def invoke(self, messages):
            time.sleep(0.03)
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
    assert duration < 0.15
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]
    assert result.error["error_class"] == "model_error"
    assert result.error["reason"] == "model_call_timeout"
    assert events[-1][1]["error_class"] == "model_error"
    assert events[-1][1]["reason"] == "model_call_timeout"
    assert events[-1][1]["error"]["error_class"] == "model_error"
    assert events[-1][1]["error"]["reason"] == "model_call_timeout"


def test_langchain_adapter_failed_model_call_events_include_metrics_observation() -> None:
    events = []

    class FailingModel:
        def invoke(self, messages):
            raise TimeoutError("provider timed out")

    result = LangChainAgentLoopAdapter(model=FailingModel()).run(
        _request(),
        replace(
            _context(),
            model_event_recorder=lambda kind, payload: events.append((kind, payload)),
        ),
    )

    assert result.status == "timeout"
    started = [payload for kind, payload in events if kind == "model_call_started"][0]
    failed = [payload for kind, payload in events if kind == "model_call_failed"][0]
    assert started["model_call_observation_id"] == failed["model_call_observation_id"]
    assert started["model_call_observation_id"] != "model_call_1"
    assert started["estimated_usage"]["input_tokens"] > 0
    assert started["estimated_usage"]["output_tokens"] == 0
    assert started["estimated_usage"]["total_tokens"] == started["estimated_usage"][
        "input_tokens"
    ]
    assert failed["estimated_usage"] == started["estimated_usage"]


def test_langchain_adapter_metrics_observation_does_not_change_tool_loop_ids() -> None:
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

    class ToolThenFailingModel:
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
                                "id": "provider_read_file_1",
                                "name": "read_file",
                                "args": {"path": "a.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            raise TimeoutError("provider timed out")

    model = ToolThenFailingModel()
    result = LangChainAgentLoopAdapter(
        model=model,
        tool_broker=RecordingBroker(),
    ).run(
        _request(),
        replace(
            _context(),
            model_event_recorder=lambda kind, payload: events.append((kind, payload)),
        ),
    )

    assert result.status == "timeout"
    assistant_message = model.messages_by_call[1][-2]
    assert assistant_message.tool_calls[0]["id"] == "model_call_1_tool_1"
    started = [payload for kind, payload in events if kind == "model_call_started"]
    failed = [payload for kind, payload in events if kind == "model_call_failed"][0]
    assert started[0]["model_call_observation_id"] == "model_call_1_metrics"
    assert started[1]["model_call_observation_id"] == failed["model_call_observation_id"]
    assert failed["model_call_observation_id"] == "model_call_2_metrics"


def test_langchain_adapter_classifies_provider_connection_error_as_transient_exception() -> None:
    events = []

    class APIConnectionError(Exception):
        __module__ = "anthropic"

    class FailingModel:
        def invoke(self, messages):
            raise APIConnectionError("Connection error.")

    result = LangChainAgentLoopAdapter(model=FailingModel()).run(
        _request(),
        replace(
            _context(),
            model_event_recorder=lambda kind, payload: events.append((kind, payload)),
        ),
    )

    assert result.status == "failed"
    assert result.error["error_class"] == "model_error"
    assert result.error["reason"] == "provider_exception"
    assert result.error["metadata"]["transient"] is True
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]
    assert events[-1][1]["error"]["reason"] == "provider_exception"
    assert events[-1][1]["error"]["metadata"]["transient"] is True


def test_langchain_adapter_cancels_worker_and_ignores_late_model_result() -> None:
    events = []
    registered_handles = []
    cancel_requested = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class SlowModel:
        def invoke(self, messages):
            while not cancel_requested.is_set():
                time.sleep(0.001)
            return type(
                "Response",
                (),
                {"content": "too late", "tool_calls": [], "usage": {"output_tokens": 3}},
            )()

    def register_handle(handle):
        registered_handles.append(handle)
        cancel_requested.set()

    request = replace(_request(), timeout_seconds=30)
    context = replace(
        _context(),
        cancellation_token=Token(),
        metadata={"provider_cancellation_registry": register_handle},
        model_event_recorder=lambda kind, payload: events.append((kind, payload)),
    )

    result = LangChainAgentLoopAdapter(model=SlowModel()).run(request, context)

    assert result.status == "cancelled"
    assert result.assistant_output is None
    assert result.error["error_class"] == "cancelled"
    assert result.error["reason"] == "model_call_cancelled"
    assert result.metadata["provider_cancellation"]["local_cancel_requested"] is True
    assert result.metadata["provider_cancellation"]["remote_stop_uncertain"] is True
    assert result.metadata["provider_cancellation"]["billing_stop_uncertain"] is True
    assert registered_handles and registered_handles[0].cancel_requested is True
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]
    assert events[-1][1]["error"]["reason"] == "model_call_cancelled"


def test_langchain_adapter_user_keyboard_interrupt_escapes_provider_wait(monkeypatch) -> None:
    from debug_agent.adapters import langchain_adapter as adapter_module

    events = []

    def interrupting_wait(**kwargs):
        raise KeyboardInterrupt("user interrupt")

    monkeypatch.setattr(adapter_module, "run_async_provider_call", interrupting_wait)

    context = replace(
        _context(),
        model_event_recorder=lambda kind, payload: events.append((kind, payload)),
    )

    with pytest.raises(KeyboardInterrupt):
        LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")).run(
            _request(),
            context,
        )

    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]
    assert events[-1][1]["error"]["reason"] == "model_call_cancelled"


def test_langchain_adapter_cancellation_collects_model_worker_before_return() -> None:
    registered_handles = []
    cancel_requested = threading.Event()
    provider_finished = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class CooperativeSlowModel:
        def invoke(self, messages):
            while not cancel_requested.is_set():
                time.sleep(0.001)
            provider_finished.set()
            return type(
                "Response",
                (),
                {"content": "late", "tool_calls": [], "usage": {}},
            )()

    def register_handle(handle):
        registered_handles.append(handle)
        cancel_requested.set()

    result = LangChainAgentLoopAdapter(model=CooperativeSlowModel()).run(
        replace(_request(), timeout_seconds=30),
        replace(
            _context(),
            cancellation_token=Token(),
            metadata={"provider_cancellation_registry": register_handle},
        ),
    )

    assert result.status == "cancelled"
    assert provider_finished.is_set()
    assert registered_handles[0].metadata["local_boundary_closed"] is True


def test_langchain_adapter_unclosed_model_worker_propagates_fail_closed() -> None:
    cancel_requested = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class UncooperativeModel:
        def invoke(self, messages):
            time.sleep(0.2)
            return type(
                "Response",
                (),
                {"content": "late", "tool_calls": [], "usage": {}},
            )()

    def register_handle(handle):
        cancel_requested.set()

    with pytest.raises(ProviderBoundaryNotClosed):
        LangChainAgentLoopAdapter(model=UncooperativeModel()).run(
            replace(
                _request(),
                timeout_seconds=30,
                model_config={
                    "provider": "fake",
                    "execution": {"cancellation_timeout_seconds": 0.01},
                },
            ),
            replace(
                _context(),
                cancellation_token=Token(),
                metadata={"provider_cancellation_registry": register_handle},
            ),
        )


def test_langchain_adapter_times_out_blocking_stream_call() -> None:
    events = []
    stream_events = []

    class SlowStreamingModel:
        stream_chunks = ["too late"]

        def stream(self, messages):
            time.sleep(0.03)
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
    assert result.error["error_class"] == "model_error"
    assert result.error["reason"] == "model_call_timeout"
    assert duration < 0.15
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]
    assert events[-1][1]["error_class"] == "model_error"
    assert events[-1][1]["reason"] == "model_call_timeout"
    assert events[-1][1]["error"]["error_class"] == "model_error"
    assert events[-1][1]["error"]["reason"] == "model_call_timeout"
    assert [event.kind for event in stream_events] == ["stream_model_call_started"]


def test_langchain_adapter_stream_cancellation_collects_worker_before_return() -> None:
    cancel_requested = threading.Event()
    provider_finished = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class CooperativeStreamingModel:
        stream_chunks = ["late"]

        def stream(self, messages):
            while not cancel_requested.is_set():
                time.sleep(0.001)
            provider_finished.set()
            yield AIMessageChunk(content="late")

    def register_handle(handle):
        cancel_requested.set()

    result = LangChainAgentLoopAdapter(model=CooperativeStreamingModel()).stream(
        replace(_request(), timeout_seconds=30),
        replace(
            _context(),
            cancellation_token=Token(),
            metadata={"provider_cancellation_registry": register_handle},
        ),
        lambda _event: None,
    )

    assert result.status == "cancelled"
    assert provider_finished.is_set()


def test_langchain_adapter_unclosed_stream_worker_propagates_fail_closed() -> None:
    cancel_requested = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class UncooperativeStreamingModel:
        stream_chunks = ["late"]

        def stream(self, messages):
            time.sleep(0.2)
            yield AIMessageChunk(content="late")

    def register_handle(handle):
        cancel_requested.set()

    with pytest.raises(ProviderBoundaryNotClosed):
        LangChainAgentLoopAdapter(model=UncooperativeStreamingModel()).stream(
            replace(
                _request(),
                timeout_seconds=30,
                model_config={
                    "provider": "fake",
                    "execution": {"cancellation_timeout_seconds": 0.01},
                },
            ),
            replace(
                _context(),
                cancellation_token=Token(),
                metadata={"provider_cancellation_registry": register_handle},
            ),
            lambda _event: None,
        )


def test_langchain_adapter_streaming_fallback_uses_cancellable_worker() -> None:
    events = []
    registered_handles = []
    cancel_requested = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class NonStreamingSlowModel:
        stream_chunks = None

        def invoke(self, messages):
            while not cancel_requested.is_set():
                time.sleep(0.001)
            return type(
                "Response",
                (),
                {"content": "too late fallback", "tool_calls": [], "usage": {}},
            )()

    def register_handle(handle):
        registered_handles.append(handle)
        cancel_requested.set()

    context = replace(
        _context(),
        cancellation_token=Token(),
        metadata={"provider_cancellation_registry": register_handle},
        model_event_recorder=lambda kind, payload: events.append((kind, payload)),
    )

    result = LangChainAgentLoopAdapter(model=NonStreamingSlowModel()).stream(
        replace(_request(), timeout_seconds=30),
        context,
        lambda _event: None,
    )

    assert result.status == "cancelled"
    assert result.assistant_output is None
    assert result.metadata["streaming_fallback"] is True
    assert result.metadata["provider_cancellation"]["remote_stop_uncertain"] is True
    assert registered_handles
    assert [kind for kind, _payload in events] == [
        "model_call_started",
        "model_call_failed",
    ]


def test_langchain_adapter_streaming_fallback_collects_worker_before_return() -> None:
    cancel_requested = threading.Event()
    provider_finished = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class CooperativeNonStreamingModel:
        stream_chunks = None

        def invoke(self, messages):
            while not cancel_requested.is_set():
                time.sleep(0.001)
            provider_finished.set()
            return type(
                "Response",
                (),
                {"content": "late fallback", "tool_calls": [], "usage": {}},
            )()

    def register_handle(handle):
        cancel_requested.set()

    result = LangChainAgentLoopAdapter(model=CooperativeNonStreamingModel()).stream(
        replace(_request(), timeout_seconds=30),
        replace(
            _context(),
            cancellation_token=Token(),
            metadata={"provider_cancellation_registry": register_handle},
        ),
        lambda _event: None,
    )

    assert result.status == "cancelled"
    assert result.metadata["streaming_fallback"] is True
    assert provider_finished.is_set()


def test_langchain_adapter_unclosed_streaming_fallback_worker_propagates_fail_closed() -> None:
    cancel_requested = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    class UncooperativeNonStreamingModel:
        stream_chunks = None

        def invoke(self, messages):
            time.sleep(0.2)
            return type(
                "Response",
                (),
                {"content": "late fallback", "tool_calls": [], "usage": {}},
            )()

    def register_handle(handle):
        cancel_requested.set()

    with pytest.raises(ProviderBoundaryNotClosed):
        LangChainAgentLoopAdapter(model=UncooperativeNonStreamingModel()).stream(
            replace(
                _request(),
                timeout_seconds=30,
                model_config={
                    "provider": "fake",
                    "execution": {"cancellation_timeout_seconds": 0.01},
                },
            ),
            replace(
                _context(),
                cancellation_token=Token(),
                metadata={"provider_cancellation_registry": register_handle},
            ),
            lambda _event: None,
        )


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
                "frozen_config": {"provider": "fake"},
                "metadata": {},
            },
        )
    ]


def test_langchain_adapter_preserves_tool_results_on_provider_failure_after_tool_call() -> None:
    class APIConnectionError(Exception):
        __module__ = "anthropic"

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={"tool_name": tool_name},
                redacted_output=None,
            )

    class FailingAfterToolModel:
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
            raise APIConnectionError("Connection error.")

    model = FailingAfterToolModel()
    result = LangChainAgentLoopAdapter(
        model=model,
        tool_broker=RecordingBroker(),
    ).run(_request(), _context())

    assert result.status == "failed"
    assert result.error["reason"] == "provider_exception"
    assert result.tool_results == [
        {
            "status": "ok",
            "output": "file text",
            "error": None,
            "artifacts": [],
            "metadata": {"tool_name": "read_file"},
            "redacted_output": None,
        }
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
                "frozen_config": {"provider": "fake"},
                "metadata": {},
            },
        )
    ]


def test_langchain_adapter_projects_shell_nonzero_exit_reason_to_tool_observation() -> None:
    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="error",
                output=None,
                error={
                    "schema_version": 1,
                    "error_class": "tool_error",
                    "reason": "shell_nonzero_exit",
                    "message": "err (exit code 7)",
                    "scope": "tool",
                    "recoverability": "turn_recoverable",
                    "metadata": {},
                    "artifact_ids": [],
                },
                artifacts=[],
                metadata={"tool_name": "shell_exec"},
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
                                "id": "shell_exec_0",
                                "name": "shell_exec",
                                "args": {"argv": ["false"]},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "saw failure", "tool_calls": [], "usage": {}},
            )()

    request = replace(
        _request(),
        tools=[
            {
                "name": "shell_exec",
                "description": "Run shell",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }
        ],
    )
    model = ToolLoopModel()

    result = LangChainAgentLoopAdapter(
        model=model,
        tool_broker=RecordingBroker(),
    ).run(request, _context())

    assert result.status == "completed"
    observation = json.loads(model.messages_by_call[1][-1].content)
    assert observation == {
        "error_class": "tool_error",
        "reason": "shell_nonzero_exit",
        "message": "err (exit code 7)",
        "artifact_ids": [],
    }

def test_langchain_adapter_sends_full_todo_output_back_to_model() -> None:
    full_output = {
        "plan_version": 1,
        "item_count": 5,
        "counts": {"pending": 3, "in_progress": 1, "completed": 1},
        "items": [
            {"index": 1, "content": "Review docs", "status": "completed"},
            {
                "index": 2,
                "content": "Patch renderer",
                "status": "in_progress",
                "activeForm": "Patching renderer",
            },
            {"index": 3, "content": "Run unit tests", "status": "pending"},
            {"index": 4, "content": "Update docs", "status": "pending"},
            {"index": 5, "content": "Verify TUI", "status": "pending"},
        ],
    }

    class RecordingBroker:
        def invoke(self, session_id, run_id, tool_name, arguments, context):
            return ToolResult(
                status="ok",
                output=full_output,
                error=None,
                artifacts=[],
                metadata={"tool_name": "todo"},
                redacted_output="Todo Plan v1: compact preview only",
            )

    class TodoToolLoopModel:
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
                                "id": "todo_0",
                                "name": "todo",
                                "args": {"items": []},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "saw full todo", "tool_calls": [], "usage": {}},
            )()

    request = replace(
        _request(),
        tools=[
            {
                "name": "todo",
                "description": "Replace Todo Plan",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }
        ],
    )
    model = TodoToolLoopModel()

    result = LangChainAgentLoopAdapter(
        model=model,
        tool_broker=RecordingBroker(),
    ).run(request, _context())

    assert result.status == "completed"
    second_call_messages = model.messages_by_call[1]
    assert json.loads(second_call_messages[-1].content) == full_output
    assert "compact preview only" not in second_call_messages[-1].content


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


def test_provider_messages_from_segments_merges_split_assistant_tool_calls() -> None:
    segments = [
        ConversationMessage(
            seq=1,
            role="assistant",
            kind="assistant_tool_call",
            turn_id="turn-1",
            model_call_id="model_call_1",
            tool_call_id="model_call_1_tool_1",
            content={
                "content": "checking files",
                "tool_calls": [
                    {
                        "id": "model_call_1_tool_1",
                        "name": "shell_exec",
                        "args": {"argv": ["git", "status"]},
                    }
                ],
            },
        ),
        ConversationMessage(
            seq=2,
            role="assistant",
            kind="assistant_tool_call",
            turn_id="turn-1",
            model_call_id="model_call_1",
            tool_call_id="model_call_1_tool_3",
            content={
                "content": "checking files",
                "tool_calls": [
                    {
                        "id": "model_call_1_tool_3",
                        "name": "shell_exec",
                        "args": {"argv": ["git", "diff"]},
                    }
                ],
            },
        ),
        ConversationMessage(
            seq=3,
            role="tool",
            kind="tool_result",
            turn_id="turn-1",
            model_call_id="model_call_1",
            tool_call_id="model_call_1_tool_1",
            content={
                "message_type": "tool_result",
                "content": "status",
                "tool_call_id": "model_call_1_tool_1",
            },
        ),
        ConversationMessage(
            seq=4,
            role="tool",
            kind="tool_result",
            turn_id="turn-1",
            model_call_id="model_call_1",
            tool_call_id="model_call_1_tool_3",
            content={
                "message_type": "tool_result",
                "content": "diff",
                "tool_call_id": "model_call_1_tool_3",
            },
        ),
    ]

    provider_messages = _provider_messages_from_segments(segments)

    assert len(provider_messages) == 3
    assert isinstance(provider_messages[0], AIMessage)
    assert provider_messages[0].content == "checking files"
    assert [
        {
            "id": call["id"],
            "name": call["name"],
            "args": call["args"],
        }
        for call in provider_messages[0].tool_calls
    ] == [
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
    ]
    assert isinstance(provider_messages[1], ToolMessage)
    assert provider_messages[1].tool_call_id == "model_call_1_tool_1"
    assert isinstance(provider_messages[2], ToolMessage)
    assert provider_messages[2].tool_call_id == "model_call_1_tool_3"


def test_provider_messages_from_segments_projects_non_success_tool_result_error() -> None:
    segments = [
        ConversationMessage(
            seq=1,
            role="tool",
            kind="tool_result",
            turn_id="turn-1",
            model_call_id="model_call_1",
            tool_call_id="model_call_1_tool_1",
            content={
                "message_type": "tool_result",
                "tool_name": "shell_exec",
                "tool_call_id": "model_call_1_tool_1",
                "status": "error",
                "content": None,
                "error": {
                    "error_class": "tool_error",
                    "reason": "shell_nonzero_exit",
                    "message": "command failed",
                    "artifact_ids": [],
                },
                "artifact_ids": [],
                "metadata": {},
            },
        )
    ]

    provider_messages = _provider_messages_from_segments(segments)

    assert len(provider_messages) == 1
    assert isinstance(provider_messages[0], ToolMessage)
    assert json.loads(provider_messages[0].content) == {
        "artifact_ids": [],
        "error_class": "tool_error",
        "message": "command failed",
        "reason": "shell_nonzero_exit",
    }


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
                    "error_class": "policy_error",
                    "reason": "approval_denied",
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
    assert result.error["error_class"] == "policy_error"
    assert result.error["reason"] == "approval_denied"
    assert result.metadata["failure_scope"] == "turn"
    assert result.metadata["approval_denied_abort"] is True
    assert result.tool_results[0]["metadata"]["turn_aborted"] is True


def test_langchain_adapter_compression_abort_accepts_legacy_class_as_mapper_input() -> None:
    from debug_agent.adapters.langchain_adapter import _compression_failed_abort_result

    class LegacyCompressionAbort(Exception):
        def to_result(self):
            return AgentRunResult(
                status="failed",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "error_class": "compression_failed",
                    "message": "Legacy compression failed.",
                },
                metadata={"failure_scope": "turn"},
            )

    class NormalizedCompressionAbort(Exception):
        def to_result(self):
            return AgentRunResult(
                status="failed",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "error_class": "model_error",
                    "reason": "compression_failed",
                    "message": "Compression failed.",
                },
                metadata={"failure_scope": "turn"},
            )

    legacy = _compression_failed_abort_result(LegacyCompressionAbort())
    normalized = _compression_failed_abort_result(NormalizedCompressionAbort())

    assert legacy is not None
    assert legacy.error["error_class"] == "compression_failed"
    assert normalized is not None
    assert normalized.error["error_class"] == "model_error"
    assert normalized.error["reason"] == "compression_failed"


def test_langchain_adapter_accepts_legacy_policy_denied_only_as_abort_mapper_input() -> None:
    from debug_agent.adapters.langchain_adapter import _approval_denied_abort_result

    result = _approval_denied_abort_result(
        [
            {
                "status": "denied",
                "error": {
                    "error_class": "policy_denied",
                    "message": "Approval denied.",
                },
                "metadata": {"turn_aborted": True},
            }
        ],
        [{"id": "tool-1", "name": "read_file", "args": {"path": "notes.txt"}}],
    )

    assert result is not None
    assert result.metadata["approval_denied_abort"] is True
    assert result.error["error_class"] == "policy_denied"


def test_langchain_adapter_stops_parallel_tool_calls_after_turn_abort_denial() -> None:
    class FirstToolDeniedBroker:
        def __init__(self) -> None:
            self.invocations: list[str] = []

        def invoke(self, session_id, run_id, tool_name, arguments, context):
            self.invocations.append(tool_name)
            return ToolResult(
                status="denied",
                output=None,
                error={
                    "error_class": "policy_error",
                    "reason": "approval_denied",
                    "message": "Turn cancelled by user.",
                    "source": "toolbroker",
                    "recoverable": True,
                },
                artifacts=[],
                metadata={"turn_aborted": True},
                redacted_output=None,
            )

    class MultiToolModel:
        def invoke(self, messages):
            return type(
                "Response",
                (),
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "shell_exec_0",
                            "name": "shell_exec",
                            "args": {"argv": ["git", "status", "-s"]},
                        },
                        {
                            "id": "shell_exec_1",
                            "name": "shell_exec",
                            "args": {"argv": ["git", "log", "--oneline"]},
                        },
                    ],
                    "usage": {},
                },
            )()

    broker = FirstToolDeniedBroker()

    result = LangChainAgentLoopAdapter(
        model=MultiToolModel(),
        tool_broker=broker,
    ).run(_request(), _context())

    assert broker.invocations == ["shell_exec"]
    assert result.status == "failed"
    assert result.error["reason"] == "approval_denied"
    assert result.metadata["approval_denied_abort"] is True
    assert len(result.tool_results) == 1


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


def test_langchain_adapter_does_not_materialize_empty_tool_call_id_observation() -> None:
    from langchain_core.messages import ToolMessage, convert_to_messages

    denied_observation = ConversationMessage(
        seq=40,
        role="tool",
        kind="tool_result",
        turn_id="turn-1",
        model_call_id=None,
        tool_call_id="",
        content={
            "message_type": "tool_result",
            "content": "Approval denied.",
            "tool_call_id": "",
        },
        metadata={
            "turn_aborted": True,
            "terminal_observation": True,
        },
    )

    class ProviderValidatingModel:
        def invoke(self, messages):
            converted = convert_to_messages(messages)
            assert not any(
                isinstance(message, ToolMessage) and message.tool_call_id == ""
                for message in converted
            )
            assert "Approval denied." in "\n".join(
                str(getattr(message, "content", "")) for message in converted
            )
            return AIMessage(content="saw plain denial")

    request = _frame_request()
    request.model_context_frame.message_segments.append(denied_observation)

    result = LangChainAgentLoopAdapter(model=ProviderValidatingModel()).run(
        request,
        _context(),
    )

    assert result.status == "completed"
    assert result.assistant_output == "saw plain denial"


def test_langchain_adapter_projects_repeated_historical_tool_ids_uniquely() -> None:
    from langchain_core.messages import ToolMessage, convert_to_messages

    def assistant_tool(seq: int, turn_id: str, tool_name: str) -> ConversationMessage:
        return ConversationMessage(
            seq=seq,
            role="assistant",
            kind="tool_call",
            turn_id=turn_id,
            model_call_id="model_call_1",
            tool_call_id=None,
            content={
                "content": "",
                "tool_calls": [
                    {
                        "id": "model_call_1_tool_1",
                        "name": tool_name,
                        "args": {},
                    }
                ],
            },
        )

    def tool_result(seq: int, turn_id: str, content: str) -> ConversationMessage:
        return ConversationMessage(
            seq=seq,
            role="tool",
            kind="tool_result",
            turn_id=turn_id,
            model_call_id="model_call_1",
            tool_call_id="model_call_1_tool_1",
            content={
                "message_type": "tool_result",
                "content": content,
                "tool_call_id": "model_call_1_tool_1",
            },
        )

    class ProviderValidatingModel:
        def invoke(self, messages):
            converted = convert_to_messages(messages)
            pairs = []
            for index, message in enumerate(converted):
                tool_calls = getattr(message, "tool_calls", None)
                if tool_calls:
                    next_message = converted[index + 1]
                    assert isinstance(next_message, ToolMessage)
                    assert next_message.tool_call_id
                    assert next_message.tool_call_id == tool_calls[0]["id"]
                    pairs.append(next_message.tool_call_id)
            assert len(pairs) == 2
            assert len(set(pairs)) == 2
            return AIMessage(content="history ok")

    request = _frame_request()
    request.model_context_frame.message_segments.extend(
        [
            assistant_tool(30, "turn-1", "view_image"),
            tool_result(40, "turn-1", "cancelled"),
            assistant_tool(50, "turn-2", "shell_exec"),
            tool_result(60, "turn-2", "completed"),
        ]
    )

    result = LangChainAgentLoopAdapter(model=ProviderValidatingModel()).run(
        request,
        _context(),
    )

    assert result.status == "completed"
    assert result.assistant_output == "history ok"


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


def test_langchain_adapter_stops_after_frozen_tool_call_iteration_limit() -> None:
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

    request = replace(
        _request(),
        model_config={
            "provider": "fake",
            "agent_loop": {"max_tool_call_iterations": 3},
        },
    )

    result = adapter.run(request, _context())

    assert model.calls == 3
    assert result.status == "failed"
    assert result.error["error_class"] == "runtime_error"
    assert result.error["reason"] == "adapter_contract_violation"
    assert "iteration limit" in result.error["message"]
    assert result.metadata == {"failure_scope": "turn"}


def test_langchain_adapter_uses_settings_loop_bound_only_without_frozen_config() -> None:
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

    assert model.calls == DEFAULT_AGENT_LOOP_MAX_TOOL_CALL_ITERATIONS
    assert result.status == "failed"


def test_langchain_adapter_does_not_pass_model_timeout_as_generic_tool_timeout() -> None:
    class SingleToolModel:
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
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_1",
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
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    class RecordingBroker:
        def __init__(self) -> None:
            self.contexts = []

        def invoke(self, session_id, run_id, tool_name, arguments, context):
            self.contexts.append(dict(context))
            return ToolResult(
                status="ok",
                output="file text",
                error=None,
                artifacts=[],
                metadata={},
                redacted_output=None,
            )

    broker = RecordingBroker()

    result = LangChainAgentLoopAdapter(
        model=SingleToolModel(),
        tool_broker=broker,
    ).run(
        replace(
            _request(),
            timeout_seconds=120,
            model_config={
                "provider": "fake",
                "execution": {"default_tool_timeout_seconds": 9},
            },
        ),
        _context(),
    )

    assert result.status == "completed"
    assert "timeout_seconds" not in broker.contexts[0]
    assert broker.contexts[0]["frozen_config"]["execution"][
        "default_tool_timeout_seconds"
    ] == 9


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
                "frozen_config": {"provider": "fake"},
                "metadata": {},
            },
        )
    ]

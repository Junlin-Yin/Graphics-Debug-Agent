from __future__ import annotations

from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.adapters.model_factory import FakeChatModel
from debug_agent.runtime.contracts import AgentRunRequest, RunContext, ToolResult


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

    adapter = LangChainAgentLoopAdapter(
        model=FakeChatModel(
            response="used tool",
            tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}}],
        ),
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

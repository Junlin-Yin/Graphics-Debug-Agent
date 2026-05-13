from __future__ import annotations

from collections.abc import Callable
import json
import queue
import threading
from time import monotonic
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from debug_agent.runtime.contracts import AgentRunRequest, AgentRunResult, RunContext


RUNTIME_SAFETY_PREFIX = (
    "runtime safety: use only runtime-provided tools and do not bypass ToolBroker."
)
MAX_TOOL_CALL_ITERATIONS = 8


class LangChainAgentLoopAdapter:
    def __init__(self, *, model: object, tool_broker: object | None = None) -> None:
        self.model = model
        self.tool_broker = tool_broker
        self._cancelled_runs: set[str] = set()

    def run(self, request: AgentRunRequest, context: RunContext) -> AgentRunResult:
        if request.run_id in self._cancelled_runs:
            return _error_result("cancelled", "cancelled", "Run was cancelled.")
        messages = _compose_messages(request)
        model = self.model
        if request.tools and hasattr(model, "bind_tools"):
            model = model.bind_tools(
                _langchain_tools(request, context, tool_broker=self.tool_broker)
        )
        tool_results: list[dict[str, Any]] = []
        try:
            for _ in range(MAX_TOOL_CALL_ITERATIONS):
                response = _invoke_model(model, messages, request, context)
                tool_calls = _tool_calls(response)
                if not tool_calls:
                    return AgentRunResult(
                        status="completed",
                        assistant_output=_response_content(response),
                        tool_results=tool_results,
                        usage=getattr(response, "usage", {}) or {},
                        error=None,
                        metadata={},
                    )
                invoked_results = self._invoke_tool_calls(request, context, tool_calls)
                tool_results.extend(result.to_dict() for _, result in invoked_results)
                messages.extend(_tool_loop_messages(response, invoked_results))
            return _error_result(
                "failed",
                "internal_error",
                "Tool call loop exceeded Phase 0 iteration limit.",
            )
        except TimeoutError as exc:
            return _error_result("timeout", "timeout", str(exc), source="model")
        except KeyboardInterrupt as exc:
            return _error_result("cancelled", "cancelled", str(exc), source="model")
        except Exception as exc:
            return _error_result("failed", "model_error", str(exc), source="model")

    def cancel(self, run_id: str) -> None:
        self._cancelled_runs.add(run_id)

    def _invoke_tool_calls(
        self,
        request: AgentRunRequest,
        context: RunContext,
        tool_calls: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], Any]]:
        if not tool_calls:
            return []
        if self.tool_broker is None:
            raise RuntimeError("Tool calls require ToolBroker")
        context_dict = _tool_context(request, context)
        results = []
        for call in tool_calls:
            result = self.tool_broker.invoke(
                request.session_id,
                request.run_id,
                call["name"],
                call.get("args", {}),
                context_dict,
            )
            results.append((call, result))
        return results


def _langchain_tools(
    request: AgentRunRequest, context: RunContext, *, tool_broker: object | None
) -> list[StructuredTool]:
    return [
        StructuredTool(
            name=tool["name"],
            description=tool["description"],
            args_schema=tool["input_schema"],
            func=_tool_callable(
                request=request,
                context=context,
                tool_name=tool["name"],
                tool_broker=tool_broker,
            ),
        )
        for tool in request.tools
    ]


def _tool_callable(
    *,
    request: AgentRunRequest,
    context: RunContext,
    tool_name: str,
    tool_broker: object | None,
) -> Callable[..., dict[str, Any]]:
    def invoke_tool(**arguments: Any) -> dict[str, Any]:
        if tool_broker is None:
            raise RuntimeError("Tool calls require ToolBroker")
        result = tool_broker.invoke(
            request.session_id,
            request.run_id,
            tool_name,
            dict(arguments),
            _tool_context(request, context),
        )
        return result.to_dict()

    return invoke_tool


def _tool_context(request: AgentRunRequest, context: RunContext) -> dict[str, Any]:
    return {
        "workspace_root": context.workspace_root,
        "artifact_root": context.artifact_root,
        "approval_mode": context.approval_mode,
        "cancellation_token": context.cancellation_token,
        "timeout_seconds": request.timeout_seconds,
        "metadata": context.metadata,
    }


def _compose_messages(request: AgentRunRequest) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": f"{RUNTIME_SAFETY_PREFIX}\n\n{request.system_prompt}",
        },
        *request.conversation,
        {"role": "user", "content": request.user_input},
    ]


def _invoke_model(
    model: object,
    messages: list[object],
    request: AgentRunRequest,
    context: RunContext,
) -> object:
    recorder = context.model_event_recorder
    if recorder is not None:
        recorder(
            "model_call_started",
            {
                "provider": request.model_config.get("provider"),
                "model": request.model_config.get("model"),
            },
        )
    start = monotonic()
    try:
        response = _invoke_with_timeout(model, messages, request.timeout_seconds)
    except TimeoutError as exc:
        _record_model_failure(recorder, "timeout", str(exc), start)
        raise
    except KeyboardInterrupt as exc:
        _record_model_failure(recorder, "cancelled", str(exc), start)
        raise
    except Exception as exc:
        _record_model_failure(recorder, "model_error", str(exc), start)
        raise
    if recorder is not None:
        recorder(
            "model_call_completed",
            {
                "usage": getattr(response, "usage", {}) or {},
                "metadata": {},
                "duration": monotonic() - start,
                "content": _response_content(response),
                "tool_calls": _normalized_tool_calls(_tool_calls(response)),
                "artifact_ids": [],
                "redacted_output": None,
            },
        )
    return response


def _invoke_with_timeout(
    model: object,
    messages: list[object],
    timeout_seconds: int | float | None,
) -> object:
    if timeout_seconds is None or timeout_seconds <= 0:
        return model.invoke(messages)

    result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put(("ok", model.invoke(messages)))
        except BaseException as exc:
            result_queue.put(("error", exc))

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    thread.join(timeout=float(timeout_seconds))
    if thread.is_alive():
        raise TimeoutError(f"Model call timed out after {timeout_seconds:g} seconds.")

    try:
        status, value = result_queue.get_nowait()
    except queue.Empty as exc:
        raise RuntimeError("Model call finished without returning a result.") from exc
    if status == "error":
        if not isinstance(value, BaseException):
            raise RuntimeError("Model call failed without returning an exception.")
        raise value
    if status != "ok":
        raise RuntimeError(f"Unsupported model call result status: {status}")
    return value


def _record_model_failure(
    recorder: Callable[[str, dict[str, Any]], None] | None,
    error_class: str,
    message: str,
    start: float,
) -> None:
    if recorder is None:
        return
    error = {
        "error_class": error_class,
        "message": message,
        "source": "model",
        "recoverable": True,
    }
    recorder(
        "model_call_failed",
        {**error, "error": error, "duration": monotonic() - start},
    )


def _tool_calls(response: object) -> list[dict[str, Any]]:
    return list(getattr(response, "tool_calls", []) or [])


def _normalized_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for index, call in enumerate(tool_calls):
        normalized.append(
            {
                "name": call["name"],
                "args": call.get("args", {}),
                "id": str(call.get("id") or f"{call['name']}_{index}"),
            }
        )
    return normalized


def _tool_loop_messages(
    response: object, invoked_results: list[tuple[dict[str, Any], Any]]
) -> list[object]:
    messages: list[object] = [_assistant_tool_message(response)]
    for index, (call, result) in enumerate(invoked_results):
        messages.append(
            ToolMessage(
                content=_tool_message_content(result.to_dict()),
                tool_call_id=str(call.get("id") or f"{call['name']}_{index}"),
            )
        )
    return messages


def _assistant_tool_message(response: object) -> object:
    if isinstance(response, AIMessage):
        return response
    return AIMessage(
        content=_response_content(response),
        tool_calls=_normalized_tool_calls(_tool_calls(response)),
    )


def _tool_message_content(result: dict[str, Any]) -> str:
    output = result.get("redacted_output") or result.get("output")
    if isinstance(output, str):
        return output
    if output is not None:
        return json.dumps(output, sort_keys=True)
    return json.dumps(result, sort_keys=True)


def _response_content(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    return str(content)


def _error_result(
    status: str,
    error_class: str,
    message: str,
    *,
    source: str = "adapter",
) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        assistant_output=None,
        tool_results=[],
        usage={},
        error={
            "error_class": error_class,
            "message": message,
            "source": source,
            "recoverable": True,
        },
        metadata={},
    )

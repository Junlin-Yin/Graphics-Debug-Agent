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
from debug_agent.runtime.stream_events import AgentStreamEvent


RUNTIME_SAFETY_PREFIX = (
    "runtime safety: use only runtime-provided tools and do not bypass ToolBroker."
)
MAX_TOOL_CALL_ITERATIONS = 8


class _StreamModelResponse:
    def __init__(
        self,
        *,
        content: str,
        tool_calls: list[dict[str, Any]],
        usage: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        self.content = content
        self.text = content
        self.tool_calls = tool_calls
        self.usage = usage
        self.duration_seconds = duration_seconds


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
                metadata={"failure_scope": "turn"},
            )
        except TimeoutError as exc:
            return _error_result("timeout", "timeout", str(exc), source="model")
        except KeyboardInterrupt as exc:
            return _error_result("cancelled", "cancelled", str(exc), source="model")
        except Exception as exc:
            return _error_result("failed", "model_error", str(exc), source="model")

    def stream(
        self,
        request: AgentRunRequest,
        context: RunContext,
        on_event: Callable[[AgentStreamEvent], None],
    ) -> AgentRunResult:
        if request.run_id in self._cancelled_runs:
            return _error_result("cancelled", "cancelled", "Run was cancelled.")
        model = self.model
        if request.tools and hasattr(model, "bind_tools"):
            model = model.bind_tools(
                _langchain_tools(request, context, tool_broker=self.tool_broker)
            )
        if not _supports_native_stream(model):
            return _streaming_fallback(self.run(request, context))

        messages = _compose_messages(request)
        tool_results: list[dict[str, Any]] = []
        try:
            for model_call_index in range(MAX_TOOL_CALL_ITERATIONS):
                model_call_id = f"model_call_{model_call_index + 1}"
                response = _stream_model_call(
                    model=model,
                    messages=messages,
                    request=request,
                    context=context,
                    model_call_id=model_call_id,
                    on_event=on_event,
                )
                tool_calls = _normalized_stream_tool_calls(
                    _tool_calls(response),
                    model_call_id=model_call_id,
                )
                response.tool_calls = tool_calls
                is_final = not tool_calls
                _emit_stream_model_call_completed(
                    response=response,
                    model_call_id=model_call_id,
                    is_final=is_final,
                    duration_seconds=response.duration_seconds,
                    on_event=on_event,
                )
                _record_stream_model_completion(
                    context=context,
                    response=response,
                    duration_seconds=response.duration_seconds,
                )
                if is_final:
                    return AgentRunResult(
                        status="completed",
                        assistant_output=response.text,
                        tool_results=tool_results,
                        usage=response.usage,
                        error=None,
                        metadata={},
                    )
                invoked_results = self._invoke_stream_tool_calls(
                    request=request,
                    context=context,
                    tool_calls=tool_calls,
                    on_event=on_event,
                )
                tool_results.extend(result.to_dict() for _, result in invoked_results)
                messages.extend(_tool_loop_messages(response, invoked_results))
            return _error_result(
                "failed",
                "internal_error",
                "Tool call loop exceeded Phase 0 iteration limit.",
                metadata={"failure_scope": "turn"},
            )
        except NotImplementedError:
            return _streaming_fallback(self.run(request, context))
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

    def _invoke_stream_tool_calls(
        self,
        *,
        request: AgentRunRequest,
        context: RunContext,
        tool_calls: list[dict[str, Any]],
        on_event: Callable[[AgentStreamEvent], None],
    ) -> list[tuple[dict[str, Any], Any]]:
        if not tool_calls:
            return []
        if self.tool_broker is None:
            raise RuntimeError("Tool calls require ToolBroker")
        context_dict = _tool_context(request, context)
        results = []
        for call in tool_calls:
            started_at = monotonic()
            on_event(
                AgentStreamEvent(
                    kind="stream_tool_call_started",
                    payload={
                        "tool_call_id": call["id"],
                        "model_call_id": call["model_call_id"],
                        "name": call["name"],
                        "args": call.get("args", {}),
                    },
                )
            )
            result = self.tool_broker.invoke(
                request.session_id,
                request.run_id,
                call["name"],
                call.get("args", {}),
                context_dict,
            )
            duration_ms = _tool_duration_ms(result.to_dict(), started_at)
            on_event(
                AgentStreamEvent(
                    kind="stream_tool_call_completed",
                    payload={
                        "tool_call_id": call["id"],
                        "model_call_id": call["model_call_id"],
                        "name": call["name"],
                        "status": result.status,
                        "duration_ms": duration_ms,
                    },
                )
            )
            on_event(
                AgentStreamEvent(
                    kind="stream_tool_result",
                    payload={
                        "tool_call_id": call["id"],
                        "model_call_id": call["model_call_id"],
                        "output": result.output,
                        "redacted_output": result.redacted_output,
                        "artifact_ids": list(result.artifacts),
                    },
                )
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


def _supports_native_stream(model: object) -> bool:
    if not callable(getattr(model, "stream", None)):
        return False
    if getattr(model, "stream_chunks", object()) is None:
        return False
    return True


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


def _stream_model_call(
    *,
    model: object,
    messages: list[object],
    request: AgentRunRequest,
    context: RunContext,
    model_call_id: str,
    on_event: Callable[[AgentStreamEvent], None],
) -> _StreamModelResponse:
    recorder = context.model_event_recorder
    if recorder is not None:
        recorder(
            "model_call_started",
            {
                "provider": request.model_config.get("provider"),
                "model": request.model_config.get("model"),
            },
        )
    on_event(
        AgentStreamEvent(
            kind="stream_model_call_started",
            payload={"model_call_id": model_call_id},
        )
    )
    started_at = monotonic()
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_call_chunks: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    try:
        for chunk in _stream_with_timeout(model, messages, request.timeout_seconds):
            chunk_text = _displayable_chunk_text(chunk)
            if chunk_text:
                text_parts.append(chunk_text)
                on_event(
                    AgentStreamEvent(
                        kind="stream_text_delta",
                        payload={"model_call_id": model_call_id, "text": chunk_text},
                    )
                )
            chunk_tool_calls = _tool_calls(chunk)
            if chunk_tool_calls:
                tool_calls.extend(chunk_tool_calls)
            chunk_tool_call_chunks = _tool_call_chunks(chunk)
            if chunk_tool_call_chunks:
                tool_call_chunks.extend(chunk_tool_call_chunks)
            chunk_usage = getattr(chunk, "usage", {}) or {}
            if chunk_usage:
                usage = dict(chunk_usage)
    except TimeoutError as exc:
        _record_model_failure(recorder, "timeout", str(exc), started_at)
        raise
    except KeyboardInterrupt as exc:
        _record_model_failure(recorder, "cancelled", str(exc), started_at)
        raise
    except Exception as exc:
        if isinstance(exc, NotImplementedError):
            raise
        _record_model_failure(recorder, "model_error", str(exc), started_at)
        raise
    return _StreamModelResponse(
        content="".join(text_parts),
        tool_calls=_merge_stream_tool_calls(tool_calls, tool_call_chunks),
        usage=usage,
        duration_seconds=monotonic() - started_at,
    )


def _emit_stream_model_call_completed(
    *,
    response: _StreamModelResponse,
    model_call_id: str,
    is_final: bool,
    duration_seconds: float,
    on_event: Callable[[AgentStreamEvent], None],
) -> None:
    on_event(
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": model_call_id,
                "is_final": is_final,
                "usage": response.usage,
                "duration_ms": int(duration_seconds * 1000),
            },
        )
    )


def _record_stream_model_completion(
    *,
    context: RunContext,
    response: _StreamModelResponse,
    duration_seconds: float,
) -> None:
    recorder = context.model_event_recorder
    if recorder is None:
        return
    recorder(
        "model_call_completed",
        {
            "usage": response.usage,
            "metadata": {},
            "duration": duration_seconds,
            "content": response.text,
            "tool_calls": _normalized_tool_calls(_tool_calls(response)),
            "artifact_ids": [],
            "redacted_output": None,
        },
    )


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


def _stream_with_timeout(
    model: object,
    messages: list[object],
    timeout_seconds: int | float | None,
):
    if timeout_seconds is None or timeout_seconds <= 0:
        yield from model.stream(messages)
        return

    result_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()

    def stream() -> None:
        try:
            for chunk in model.stream(messages):
                result_queue.put(("chunk", chunk))
            result_queue.put(("ok", None))
        except BaseException as exc:
            result_queue.put(("error", exc))

    thread = threading.Thread(target=stream, daemon=True)
    thread.start()
    deadline = monotonic() + float(timeout_seconds)
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Model stream timed out after {timeout_seconds:g} seconds."
            )
        try:
            status, value = result_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError(
                f"Model stream timed out after {timeout_seconds:g} seconds."
            ) from exc
        if status == "chunk":
            yield value
            continue
        if status == "ok":
            return
        if status == "error":
            if not isinstance(value, BaseException):
                raise RuntimeError("Model stream failed without returning an exception.")
            raise value
        raise RuntimeError(f"Unsupported model stream result status: {status}")


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


def _tool_call_chunks(response: object) -> list[dict[str, Any]]:
    return list(getattr(response, "tool_call_chunks", []) or [])


def _merge_stream_tool_calls(
    tool_calls: list[dict[str, Any]], tool_call_chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    chunk_calls = _tool_calls_from_chunks(tool_call_chunks)
    if not chunk_calls:
        return tool_calls
    merged = list(tool_calls)
    for chunk_call in chunk_calls:
        match_index = _matching_tool_call_index(merged, chunk_call)
        if match_index is None:
            merged.append(chunk_call)
            continue
        existing = dict(merged[match_index])
        if chunk_call.get("name"):
            existing["name"] = chunk_call["name"]
        if chunk_call.get("id"):
            existing["id"] = chunk_call["id"]
        chunk_args = chunk_call.get("args")
        if _has_arguments(chunk_args) or not _has_arguments(existing.get("args")):
            existing["args"] = chunk_args
        merged[match_index] = existing
    return merged


def _tool_calls_from_chunks(tool_call_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    states: dict[object, dict[str, Any]] = {}
    order: list[object] = []
    for position, chunk in enumerate(tool_call_chunks):
        key = _tool_call_chunk_key(chunk, position)
        if key not in states:
            states[key] = {"args_text": "", "args": {}}
            order.append(key)
        state = states[key]
        name = chunk.get("name")
        if isinstance(name, str) and name:
            state["name"] = name
        tool_call_id = chunk.get("id")
        if isinstance(tool_call_id, str) and tool_call_id:
            state["id"] = tool_call_id
        args = chunk.get("args")
        if isinstance(args, str):
            state["args_text"] = state.get("args_text", "") + args
            state["saw_args"] = True
        elif isinstance(args, dict):
            state["args"] = {**state.get("args", {}), **args}
            state["saw_args"] = True

    calls: list[dict[str, Any]] = []
    for key in order:
        state = states[key]
        name = state.get("name")
        if not isinstance(name, str) or not name:
            continue
        if not state.get("saw_args"):
            continue
        args = _parse_tool_call_chunk_args(
            str(state.get("args_text") or ""),
            state.get("args", {}),
        )
        calls.append(
            {
                "name": name,
                "args": args,
                "id": str(state.get("id") or f"{name}_{len(calls)}"),
            }
        )
    return calls


def _tool_call_chunk_key(chunk: dict[str, Any], position: int) -> object:
    index = chunk.get("index")
    if isinstance(index, int):
        return ("index", index)
    tool_call_id = chunk.get("id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return ("id", tool_call_id)
    return ("position", position)


def _parse_tool_call_chunk_args(args_text: str, args: object) -> dict[str, Any]:
    if args_text:
        try:
            parsed = json.loads(args_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    if isinstance(args, dict):
        return dict(args)
    return {}


def _matching_tool_call_index(
    tool_calls: list[dict[str, Any]], chunk_call: dict[str, Any]
) -> int | None:
    chunk_id = chunk_call.get("id")
    if isinstance(chunk_id, str) and chunk_id:
        for index, call in enumerate(tool_calls):
            if call.get("id") == chunk_id:
                return index
    chunk_name = chunk_call.get("name")
    if isinstance(chunk_name, str) and chunk_name:
        for index, call in enumerate(tool_calls):
            if call.get("name") == chunk_name and not _has_arguments(call.get("args")):
                return index
    return None


def _has_arguments(value: object) -> bool:
    return isinstance(value, dict) and bool(value)


def _normalized_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for index, call in enumerate(tool_calls):
        name = call.get("name")
        if not isinstance(name, str) or not name:
            continue
        normalized.append(
            {
                "name": name,
                "args": call.get("args", {}),
                "id": str(call.get("id") or f"{name}_{index}"),
            }
        )
    return normalized


def _normalized_stream_tool_calls(
    tool_calls: list[dict[str, Any]], *, model_call_id: str
) -> list[dict[str, Any]]:
    normalized = []
    for index, call in enumerate(tool_calls):
        name = call.get("name")
        if not isinstance(name, str) or not name:
            continue
        normalized.append(
            {
                "name": name,
                "args": call.get("args", {}),
                "id": str(call.get("id") or f"{model_call_id}_tool_{index + 1}"),
                "model_call_id": model_call_id,
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


def _displayable_chunk_text(chunk: object) -> str:
    content = getattr(chunk, "content", "")
    if not content:
        return ""
    if _tool_calls(chunk):
        return _message_text(chunk)
    if getattr(chunk, "tool_call_chunks", None):
        return ""
    additional_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("function_call"):
        return ""
    return _message_text(chunk)


def _response_content(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    text = _message_text(response)
    if text:
        return text
    return str(content)


def _message_text(message: object) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    if callable(text):
        return str(text())
    content = getattr(message, "content", "")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _streaming_fallback(result: AgentRunResult) -> AgentRunResult:
    return AgentRunResult(
        status=result.status,
        assistant_output=result.assistant_output,
        tool_results=result.tool_results,
        usage=result.usage,
        error=result.error,
        metadata={**result.metadata, "streaming_fallback": True},
    )


def _tool_duration_ms(result: dict[str, Any], started_at: float) -> int:
    duration_ms = result.get("metadata", {}).get("duration_ms")
    if isinstance(duration_ms, int):
        return duration_ms
    return int((monotonic() - started_at) * 1000)


def _error_result(
    status: str,
    error_class: str,
    message: str,
    *,
    source: str = "adapter",
    metadata: dict[str, Any] | None = None,
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
        metadata=metadata or {},
    )

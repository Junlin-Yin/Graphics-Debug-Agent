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
from debug_agent.runtime.model_context import ConversationMessage
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
        if _request_tool_bindings(request) and hasattr(model, "bind_tools"):
            model = model.bind_tools(
                _langchain_tools(request, context, tool_broker=self.tool_broker)
        )
        tool_results: list[dict[str, Any]] = []
        aggregate_usage: dict[str, Any] = {}
        try:
            for model_call_index in range(MAX_TOOL_CALL_ITERATIONS):
                model_call_id = f"model_call_{model_call_index + 1}"
                response = _invoke_model(
                    model,
                    messages,
                    request,
                    context,
                    model_call_id=model_call_id,
                )
                aggregate_usage = _aggregate_usage(
                    aggregate_usage,
                    getattr(response, "usage", {}) or {},
                )
                tool_calls = _normalized_tool_calls(
                    _tool_calls(response),
                    model_call_id=model_call_id,
                )
                if not tool_calls:
                    return AgentRunResult(
                        status="completed",
                        assistant_output=_response_content(response),
                        tool_results=tool_results,
                        usage=aggregate_usage,
                        error=None,
                        metadata={},
                    )
                invoked_results = self._invoke_tool_calls(request, context, tool_calls)
                tool_results.extend(result.to_dict() for _, result in invoked_results)
                tool_loop_messages = _tool_loop_messages(response, invoked_results)
                abort_result = _approval_denied_abort_result(tool_results, tool_calls)
                if abort_result is not None:
                    return abort_result
                refreshed = _refresh_frame_messages(context, tool_loop_messages)
                if refreshed is None:
                    messages.extend(tool_loop_messages)
                else:
                    messages = refreshed
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
            compression_failed = _compression_failed_abort_result(exc)
            if compression_failed is not None:
                return compression_failed
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
        if _request_tool_bindings(request) and hasattr(model, "bind_tools"):
            model = model.bind_tools(
                _langchain_tools(request, context, tool_broker=self.tool_broker)
            )
        if not _supports_native_stream(model):
            return _streaming_fallback(self.run(request, context))

        messages = _compose_messages(request)
        tool_results: list[dict[str, Any]] = []
        aggregate_usage: dict[str, Any] = {}
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
                aggregate_usage = _aggregate_usage(aggregate_usage, response.usage)
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
                        usage=aggregate_usage,
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
                abort_result = _approval_denied_abort_result(tool_results, tool_calls)
                if abort_result is not None:
                    return abort_result
                tool_loop_messages = _tool_loop_messages(response, invoked_results)
                refreshed = _refresh_frame_messages(context, tool_loop_messages)
                if refreshed is None:
                    messages.extend(tool_loop_messages)
                else:
                    messages = refreshed
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
            compression_failed = _compression_failed_abort_result(exc)
            if compression_failed is not None:
                return compression_failed
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
            audit_payloads: list[tuple[str, dict[str, Any]]] = []
            context_dict["tool_audit_recorder"] = (
                lambda kind, payload, sink=audit_payloads: sink.append(
                    (kind, dict(payload))
                )
            )
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
            tool_audit = _latest_tool_audit_payload(audit_payloads)
            duration_ms = _stream_execution_duration_ms(
                result.to_dict(),
                started_at,
                tool_audit,
            )
            completed_payload = {
                "tool_call_id": call["id"],
                "model_call_id": call["model_call_id"],
                "name": call["name"],
                "status": result.status,
            }
            if tool_audit is not None:
                target = tool_audit.get("target")
                if isinstance(target, str) and target:
                    completed_payload["target"] = target
                execution_duration_ms = tool_audit.get("execution_duration_ms")
                if isinstance(execution_duration_ms, int):
                    completed_payload["execution_duration_ms"] = execution_duration_ms
                error = _error_from_tool_audit(tool_audit)
                if error is not None:
                    completed_payload["error"] = error
            if duration_ms is not None:
                completed_payload["duration_ms"] = duration_ms
            on_event(
                AgentStreamEvent(
                    kind="stream_tool_call_completed",
                    payload=completed_payload,
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
            context_dict.pop("tool_audit_recorder", None)
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
        for tool in _request_tool_bindings(request)
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
    payload = {
        "workspace_root": context.workspace_root,
        "artifact_root": context.artifact_root,
        "approval_mode": context.approval_mode,
        "cancellation_token": context.cancellation_token,
        "metadata": context.metadata,
        "frozen_config": request.model_config,
        **context.metadata,
    }
    if request.timeout_seconds is not None:
        payload["timeout_seconds"] = request.timeout_seconds
    return payload


def _supports_native_stream(model: object) -> bool:
    if not callable(getattr(model, "stream", None)):
        return False
    if getattr(model, "stream_chunks", object()) is None:
        return False
    return True


def _compose_messages(request: AgentRunRequest) -> list[dict[str, str]]:
    if request.model_context_frame is not None:
        return [
            _provider_message_from_segment(segment)
            for segment in request.model_context_frame.ordered_message_segments()
        ]
    return [
        {
            "role": "system",
            "content": f"{RUNTIME_SAFETY_PREFIX}\n\n{request.system_prompt}",
        },
        *(request.conversation or []),
        {"role": "user", "content": request.user_input},
    ]


def _request_tool_bindings(request: AgentRunRequest) -> list[dict[str, Any]]:
    if request.model_context_frame is not None:
        return [dict(binding) for binding in request.model_context_frame.tool_schema_bindings]
    return [dict(tool) for tool in request.tools or []]


def _provider_message_from_segment(segment: ConversationMessage) -> object:
    content = segment.content
    if segment.role == "tool" and _is_structured_tool_result(content, segment):
        return ToolMessage(
            content=_tool_result_content(content["content"]),
            tool_call_id=str(content["tool_call_id"]),
        )
    if segment.role == "tool" and segment.kind == "tool_result":
        return {"role": "assistant", "content": _tool_result_content(content)}
    if segment.role == "assistant" and segment.kind == "tool_call":
        assistant_content, tool_calls = _assistant_tool_call_content(content)
        if tool_calls:
            return AIMessage(content=assistant_content, tool_calls=tool_calls)
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    if segment.artifact_refs:
        content = (
            f"{content}\n\nArtifact references: "
            f"{', '.join(segment.artifact_refs)}"
        )
    return {"role": segment.role, "content": content}


def _tool_result_content(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _is_structured_tool_result(
    content: object,
    segment: ConversationMessage,
) -> bool:
    return (
        isinstance(content, dict)
        and content.get("message_type") == "tool_result"
        and _non_empty_str(content.get("tool_call_id"))
        and content.get("tool_call_id") == segment.tool_call_id
    )


def _assistant_tool_call_content(content: object) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(content, dict):
        return _tool_result_content(content), []
    raw_tool_calls = content.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return _tool_result_content(content.get("content", "")), []
    assistant_content = content.get("content", "")
    if not isinstance(assistant_content, str):
        assistant_content = _tool_result_content(assistant_content)
    return assistant_content, _provider_visible_tool_calls(
        _normalized_tool_calls(
            [call for call in raw_tool_calls if isinstance(call, dict)]
        )
    )


def _refresh_frame_messages(
    context: RunContext,
    tool_loop_messages: list[object],
) -> list[dict[str, str]] | None:
    refresh = context.metadata.get("refresh_model_context_frame")
    if not callable(refresh):
        return None
    refreshed = refresh(tool_loop_messages)
    frame = refreshed.get("frame") if isinstance(refreshed, dict) else refreshed
    return [
        _provider_message_from_segment(segment)
        for segment in frame.ordered_message_segments()
    ]


def _invoke_model(
    model: object,
    messages: list[object],
    request: AgentRunRequest,
    context: RunContext,
    *,
    model_call_id: str,
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
                "tool_calls": _normalized_tool_calls(
                    _tool_calls(response),
                    model_call_id=model_call_id,
                ),
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
    while True:
        try:
            status, value = result_queue.get(timeout=float(timeout_seconds))
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


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _normalized_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    model_call_id: str | None = None,
) -> list[dict[str, Any]]:
    normalized = []
    for index, call in enumerate(tool_calls):
        name = call.get("name")
        if not isinstance(name, str) or not name:
            continue
        provider_tool_call_id = call.get("provider_tool_call_id")
        if not isinstance(provider_tool_call_id, str):
            raw_id = call.get("id")
            provider_tool_call_id = raw_id if isinstance(raw_id, str) and raw_id else None
        tool_call_id = (
            f"{model_call_id}_tool_{index + 1}"
            if model_call_id is not None
            else str(call.get("id") or f"{name}_{index}")
        )
        normalized_call = {
            "name": name,
            "args": call.get("args", {}),
            "id": tool_call_id,
        }
        if provider_tool_call_id is not None:
            normalized_call["provider_tool_call_id"] = provider_tool_call_id
        normalized.append(normalized_call)
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
                "id": f"{model_call_id}_tool_{index + 1}",
                "model_call_id": model_call_id,
                **(
                    {"provider_tool_call_id": str(call["id"])}
                    if isinstance(call.get("id"), str) and call.get("id")
                    else {}
                ),
            }
        )
    return normalized


def _tool_loop_messages(
    response: object, invoked_results: list[tuple[dict[str, Any], Any]]
) -> list[object]:
    messages: list[object] = [
        _assistant_tool_message(response, [call for call, _ in invoked_results])
    ]
    for index, (call, result) in enumerate(invoked_results):
        messages.append(
            ToolMessage(
                content=_tool_message_content(result.to_dict()),
                tool_call_id=str(call.get("id") or f"{call['name']}_{index}"),
            )
        )
    return messages


def _assistant_tool_message(
    response: object,
    tool_calls: list[dict[str, Any]],
) -> object:
    return AIMessage(
        content=_response_content(response),
        tool_calls=_provider_visible_tool_calls(tool_calls),
    )


def _provider_visible_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(call["id"]),
            "name": str(call["name"]),
            "args": call.get("args", {}),
        }
        for call in tool_calls
    ]


def _tool_message_content(result: dict[str, Any]) -> str:
    output = result.get("redacted_output") or result.get("output")
    if isinstance(output, str):
        return output
    if output is not None:
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


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


def _aggregate_usage(current: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    if not usage:
        return current
    aggregate = dict(current)
    for key, value in usage.items():
        if isinstance(value, int):
            previous = aggregate.get(key)
            aggregate[key] = (previous if isinstance(previous, int) else 0) + value
        else:
            aggregate[key] = value
    return aggregate


def _tool_duration_ms(result: dict[str, Any], started_at: float) -> int:
    duration_ms = result.get("metadata", {}).get("duration_ms")
    if isinstance(duration_ms, int):
        return duration_ms
    return int((monotonic() - started_at) * 1000)


def _stream_execution_duration_ms(
    result: dict[str, Any],
    started_at: float,
    audit_payload: dict[str, Any] | None,
) -> int | None:
    if audit_payload is not None:
        execution_duration_ms = audit_payload.get("execution_duration_ms")
        if isinstance(execution_duration_ms, int):
            return execution_duration_ms
        return None
    return _tool_duration_ms(result, started_at)


def _latest_tool_audit_payload(
    audit_payloads: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    for kind, payload in reversed(audit_payloads):
        if kind in {
            "tool_call_completed",
            "tool_call_failed",
            "tool_call_denied",
        }:
            return payload
    return None


def _error_from_tool_audit(payload: dict[str, Any]) -> dict[str, Any] | None:
    message = payload.get("message")
    error_class = payload.get("error_class")
    if not isinstance(message, str) or not message:
        return None
    return {
        "error_class": str(error_class or "tool_error"),
        "message": message,
        "source": str(payload.get("source") or "toolbroker"),
        "recoverable": bool(payload.get("recoverable", True)),
    }


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


def _compression_failed_abort_result(exc: Exception) -> AgentRunResult | None:
    to_result = getattr(exc, "to_result", None)
    if not callable(to_result):
        return None
    result = to_result()
    if not isinstance(result, AgentRunResult):
        return None
    if not isinstance(result.error, dict):
        return None
    if result.error.get("error_class") != "compression_failed":
        return None
    return result


def _approval_denied_abort_result(
    tool_results: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> AgentRunResult | None:
    if not tool_results:
        return None
    latest = tool_results[-1]
    metadata = latest.get("metadata")
    error = latest.get("error")
    if not isinstance(metadata, dict) or metadata.get("turn_aborted") is not True:
        return None
    if not isinstance(error, dict) or error.get("error_class") != "policy_denied":
        return None
    return AgentRunResult(
        status="failed",
        assistant_output=None,
        tool_results=list(tool_results),
        usage={},
        error={
            "error_class": "policy_denied",
            "message": str(error.get("message") or "Approval denied."),
            "source": str(error.get("source") or "toolbroker"),
            "recoverable": True,
        },
        metadata={
            "failure_scope": "turn",
            "approval_denied_abort": True,
            "denied_tool_calls": [
                {
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "args": call.get("args", {}),
                }
                for call in tool_calls
                if isinstance(call, dict)
            ],
        },
    )

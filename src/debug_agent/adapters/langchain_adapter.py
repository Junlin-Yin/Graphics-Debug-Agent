from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
from time import monotonic
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from debug_agent.runtime.contracts import AgentRunRequest, AgentRunResult, RunContext
from debug_agent.runtime.model_context import (
    ConversationMessage,
    provider_role_for_message_role,
)
from debug_agent.runtime.provider_execution import (
    ProviderBoundaryNotClosed,
    ProviderCallCancelled,
    provider_cancellation_uncertainty_metadata,
    run_async_provider_call,
    run_provider_call,
    stream_async_provider_call,
    stream_provider_call,
)
from debug_agent.runtime.settings import (
    DEFAULT_AGENT_LOOP_MAX_TOOL_CALL_ITERATIONS,
    RUNTIME_SAFETY_PREFIX,
)
from debug_agent.runtime.stream_events import AgentStreamEvent
from debug_agent.runtime.usage_accounting import (
    ModelCallTokenObservation,
    estimate_model_call_usage,
    normalize_provider_usage,
    summarize_model_call_window,
    token_usage_from_mapping,
)


class _StreamModelResponse:
    def __init__(
        self,
        *,
        content: str,
        tool_calls: list[dict[str, Any]],
        usage: dict[str, Any],
        estimated_usage: dict[str, Any],
        duration_seconds: float,
        provider_finish: dict[str, Any],
    ) -> None:
        self.content = content
        self.text = content
        self.tool_calls = tool_calls
        self.usage = usage
        self.estimated_usage = estimated_usage
        self.duration_seconds = duration_seconds
        self.provider_finish = provider_finish


class LangChainAgentLoopAdapter:
    def __init__(self, *, model: object, tool_broker: object | None = None) -> None:
        self.model = model
        self.tool_broker = tool_broker

    def run(self, request: AgentRunRequest, context: RunContext) -> AgentRunResult:
        messages = _compose_messages(request)
        model = self.model
        if _request_tool_bindings(request) and hasattr(model, "bind_tools"):
            model = model.bind_tools(
                _langchain_tools(request, context, tool_broker=self.tool_broker)
            )
        tool_results: list[dict[str, Any]] = []
        token_observations: list[ModelCallTokenObservation] = []
        try:
            for model_call_index in range(_tool_call_iteration_limit(request)):
                model_call_id = f"model_call_{model_call_index + 1}"
                provider_messages = list(messages)
                response = _invoke_model(
                    model,
                    messages,
                    request,
                    context,
                    model_call_id=model_call_id,
                )
                token_observations.append(
                    _model_call_token_observation(
                        response=response,
                        provider_messages=provider_messages,
                        model_call_id=model_call_id,
                    )
                )
                usage_summary = summarize_model_call_window(token_observations)
                tool_calls = _normalized_tool_calls(
                    _tool_calls(response),
                    model_call_id=model_call_id,
                )
                if not tool_calls:
                    return AgentRunResult(
                        status="completed",
                        assistant_output=_response_content(response),
                        tool_results=tool_results,
                        usage=dict(usage_summary["usage"]),
                        error=None,
                        metadata={
                            **_result_metadata(response),
                            **_usage_metadata(usage_summary),
                        },
                    )
                invoked_results = self._invoke_tool_calls(request, context, tool_calls)
                tool_results.extend(result.to_dict() for _, result in invoked_results)
                tool_loop_messages = _tool_loop_messages(response, invoked_results)
                abort_result = _approval_denied_abort_result(
                    tool_results,
                    tool_calls,
                    _tool_loop_conversation_messages(response, invoked_results),
                )
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
                "Tool call loop exceeded configured iteration limit.",
                metadata={"failure_scope": "turn"},
            )
        except TimeoutError as exc:
            return _error_result("timeout", "timeout", str(exc), source="model")
        except ProviderCallCancelled as exc:
            return _error_result(
                "cancelled",
                "cancelled",
                str(exc),
                source="model",
                reason="model_call_cancelled",
                metadata={"provider_cancellation": provider_cancellation_uncertainty_metadata()},
            )
        except ProviderBoundaryNotClosed:
            raise
        except Exception as exc:
            compression_failed = _compression_failed_abort_result(exc)
            if compression_failed is not None:
                return compression_failed
            reason, metadata = _provider_failure_classification(exc)
            return _error_result(
                "failed",
                "model_error",
                str(exc),
                source="model",
                reason=reason,
                metadata=metadata,
            )

    def stream(
        self,
        request: AgentRunRequest,
        context: RunContext,
        on_event: Callable[[AgentStreamEvent], None],
    ) -> AgentRunResult:
        model = self.model
        if _request_tool_bindings(request) and hasattr(model, "bind_tools"):
            model = model.bind_tools(
                _langchain_tools(request, context, tool_broker=self.tool_broker)
            )
        if not _supports_native_stream(model):
            return _streaming_fallback(self.run(request, context))

        messages = _compose_messages(request)
        tool_results: list[dict[str, Any]] = []
        token_observations: list[ModelCallTokenObservation] = []
        try:
            for model_call_index in range(_tool_call_iteration_limit(request)):
                model_call_id = f"model_call_{model_call_index + 1}"
                provider_messages = list(messages)
                response = _stream_model_call(
                    model=model,
                    messages=messages,
                    request=request,
                    context=context,
                    model_call_id=model_call_id,
                    provider_messages=provider_messages,
                    on_event=on_event,
                )
                token_observations.append(
                    _token_observation_from_usage_dicts(
                        provider_usage=response.usage,
                        estimated_usage=response.estimated_usage,
                    )
                )
                usage_summary = summarize_model_call_window(token_observations)
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
                        usage=dict(usage_summary["usage"]),
                        error=None,
                        metadata={
                            **(
                                {"provider_finish": response.provider_finish}
                                if response.provider_finish
                                else {}
                            ),
                            **_usage_metadata(usage_summary),
                        },
                    )
                invoked_results = self._invoke_stream_tool_calls(
                    request=request,
                    context=context,
                    tool_calls=tool_calls,
                    on_event=on_event,
                )
                tool_results.extend(result.to_dict() for _, result in invoked_results)
                abort_result = _approval_denied_abort_result(
                    tool_results,
                    tool_calls,
                    _tool_loop_conversation_messages(response, invoked_results),
                )
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
        except ProviderCallCancelled as exc:
            return _error_result(
                "cancelled",
                "cancelled",
                str(exc),
                source="model",
                reason="model_call_cancelled",
                metadata={"provider_cancellation": provider_cancellation_uncertainty_metadata()},
            )
        except ProviderBoundaryNotClosed:
            raise
        except Exception as exc:
            compression_failed = _compression_failed_abort_result(exc)
            if compression_failed is not None:
                return compression_failed
            reason, metadata = _provider_failure_classification(exc)
            return _error_result(
                "failed",
                "model_error",
                str(exc),
                source="model",
                reason=reason,
                metadata=metadata,
            )

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
            if _tool_result_turn_aborted(result):
                break
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
            if _tool_result_turn_aborted(result):
                break
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
    return payload


def _tool_call_iteration_limit(request: AgentRunRequest) -> int:
    agent_loop = request.model_config.get("agent_loop")
    if isinstance(agent_loop, dict):
        value = agent_loop.get("max_tool_call_iterations")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return DEFAULT_AGENT_LOOP_MAX_TOOL_CALL_ITERATIONS


def _tool_result_turn_aborted(result: object) -> bool:
    metadata = getattr(result, "metadata", None)
    return isinstance(metadata, dict) and metadata.get("turn_aborted") is True


def _supports_native_stream(model: object) -> bool:
    if not callable(getattr(model, "stream", None)) and not callable(
        getattr(model, "astream", None)
    ):
        return False
    if getattr(model, "stream_chunks", object()) is None:
        return False
    return True


def _compose_messages(request: AgentRunRequest) -> list[dict[str, str]]:
    if request.model_context_frame is not None:
        return _provider_messages_from_segments(
            request.model_context_frame.ordered_message_segments()
        )
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
            content=_structured_tool_result_provider_content(content),
            tool_call_id=str(content["tool_call_id"]),
        )
    if segment.role == "tool" and segment.kind == "tool_result":
        return {"role": "assistant", "content": _tool_result_content(content)}
    if segment.role == "assistant" and _is_assistant_tool_call_kind(segment.kind):
        assistant_content, tool_calls = _assistant_tool_call_content(content)
        if tool_calls:
            return AIMessage(content=assistant_content, tool_calls=tool_calls)
    provider_role = provider_role_for_message_role(segment.role)
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    if segment.artifact_refs:
        content = (
            f"{content}\n\nArtifact references: "
            f"{', '.join(segment.artifact_refs)}"
        )
    return {"role": provider_role, "content": content}


def _provider_messages_from_segments(
    segments: list[ConversationMessage],
) -> list[object]:
    duplicate_tool_call_ids = _duplicate_provider_tool_call_ids(segments)
    projected_segments = (
        segments
        if not duplicate_tool_call_ids
        else _remap_duplicate_provider_tool_call_ids(
            segments,
            duplicate_tool_call_ids,
        )
    )
    return _provider_messages_from_projected_segments(projected_segments)


def _provider_messages_from_projected_segments(
    segments: list[ConversationMessage],
) -> list[object]:
    messages: list[object] = []
    index = 0
    while index < len(segments):
        segment = segments[index]
        merged = _merged_assistant_tool_call_message(segments, index)
        if merged is not None:
            message, consumed = merged
            messages.append(message)
            index += consumed
            continue
        messages.append(_provider_message_from_segment(segment))
        index += 1
    return messages


def _merged_assistant_tool_call_message(
    segments: list[ConversationMessage],
    start: int,
) -> tuple[AIMessage, int] | None:
    first = segments[start]
    if first.role != "assistant" or not _is_assistant_tool_call_kind(first.kind):
        return None
    assistant_content, tool_calls = _assistant_tool_call_content(first.content)
    if not tool_calls:
        return None
    group_key = (first.turn_id, first.model_call_id)
    consumed = 1
    merged_calls = list(tool_calls)
    while start + consumed < len(segments):
        current = segments[start + consumed]
        if (
            current.role != "assistant"
            or not _is_assistant_tool_call_kind(current.kind)
            or (current.turn_id, current.model_call_id) != group_key
        ):
            break
        current_content, current_calls = _assistant_tool_call_content(current.content)
        if not current_calls:
            break
        if not assistant_content and current_content:
            assistant_content = current_content
        merged_calls.extend(current_calls)
        consumed += 1
    if consumed == 1:
        return None
    return AIMessage(content=assistant_content, tool_calls=merged_calls), consumed


def _duplicate_provider_tool_call_ids(
    segments: list[ConversationMessage],
) -> set[str]:
    counts: dict[str, int] = {}
    for segment in segments:
        if segment.role != "assistant" or not _is_assistant_tool_call_kind(segment.kind):
            continue
        content = segment.content
        if not isinstance(content, dict):
            continue
        raw_tool_calls = content.get("tool_calls")
        if not isinstance(raw_tool_calls, list):
            continue
        for call in raw_tool_calls:
            if not isinstance(call, dict):
                continue
            tool_call_id = call.get("id")
            if isinstance(tool_call_id, str) and tool_call_id:
                counts[tool_call_id] = counts.get(tool_call_id, 0) + 1
    return {tool_call_id for tool_call_id, count in counts.items() if count > 1}


def _remap_duplicate_provider_tool_call_ids(
    segments: list[ConversationMessage],
    duplicate_tool_call_ids: set[str],
) -> list[ConversationMessage]:
    id_map: dict[tuple[str | None, str], str] = {}
    remapped = []
    for segment in segments:
        remapped.append(
            _remap_duplicate_provider_tool_call_id(
                segment,
                duplicate_tool_call_ids,
                id_map,
            )
        )
    return remapped


def _remap_duplicate_provider_tool_call_id(
    segment: ConversationMessage,
    duplicate_tool_call_ids: set[str],
    id_map: dict[tuple[str | None, str], str],
) -> ConversationMessage:
    if (
        segment.role == "assistant"
        and _is_assistant_tool_call_kind(segment.kind)
        and isinstance(segment.content, dict)
    ):
        raw_tool_calls = segment.content.get("tool_calls")
        if not isinstance(raw_tool_calls, list):
            return segment
        tool_calls = []
        changed = False
        for index, call in enumerate(raw_tool_calls, start=1):
            if not isinstance(call, dict):
                tool_calls.append(call)
                continue
            tool_call = dict(call)
            tool_call_id = tool_call.get("id")
            if isinstance(tool_call_id, str) and tool_call_id in duplicate_tool_call_ids:
                provider_tool_call_id = f"ctx_{segment.seq}_tool_{index}"
                tool_call["id"] = provider_tool_call_id
                id_map[(segment.turn_id, tool_call_id)] = provider_tool_call_id
                changed = True
            tool_calls.append(tool_call)
        if not changed:
            return segment
        content = dict(segment.content)
        content["tool_calls"] = tool_calls
        return replace(segment, content=content)

    if segment.role != "tool" or segment.kind != "tool_result":
        return segment
    tool_call_id = segment.tool_call_id
    if not isinstance(tool_call_id, str) or tool_call_id not in duplicate_tool_call_ids:
        return segment
    provider_tool_call_id = id_map.get((segment.turn_id, tool_call_id))
    if provider_tool_call_id is None:
        return segment
    content = segment.content
    if isinstance(content, dict):
        content = dict(content)
        content["tool_call_id"] = provider_tool_call_id
    return replace(
        segment,
        tool_call_id=provider_tool_call_id,
        content=content,
    )


def _tool_result_content(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _structured_tool_result_provider_content(content: dict[str, Any]) -> str:
    if content.get("status") == "ok":
        return _tool_result_content(content.get("content"))
    error = content.get("error")
    if isinstance(error, dict):
        return _tool_result_content(error)
    return _tool_result_content(content.get("content"))


def _is_assistant_tool_call_kind(kind: str) -> bool:
    return kind in {"tool_call", "assistant_tool_call"}


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
    return _provider_messages_from_segments(frame.ordered_message_segments())


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
        response = _invoke_with_timeout(model, messages, request, context)
    except TimeoutError as exc:
        _record_model_failure(recorder, "timeout", str(exc), start)
        raise
    except ProviderCallCancelled as exc:
        _record_model_failure(
            recorder,
            "cancelled",
            str(exc),
            start,
            reason="model_call_cancelled",
            metadata=provider_cancellation_uncertainty_metadata(),
        )
        raise
    except KeyboardInterrupt as exc:
        _record_model_failure(
            recorder,
            "cancelled",
            str(exc),
            start,
            reason="model_call_cancelled",
            metadata=provider_cancellation_uncertainty_metadata(),
        )
        raise
    except ProviderBoundaryNotClosed:
        raise
    except Exception as exc:
        reason, metadata = _provider_failure_classification(exc)
        _record_model_failure(
            recorder,
            "model_error",
            str(exc),
            start,
            reason=reason,
            metadata=metadata,
        )
        raise
    if recorder is not None:
        usage = normalize_provider_usage(response)
        recorder(
            "model_call_completed",
            {
                "usage": usage,
                "metadata": {},
                "provider_finish": _provider_finish_metadata(response),
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
    provider_messages: list[object],
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
    provider_finish: dict[str, Any] = {}
    try:
        for chunk in _stream_with_timeout(model, messages, request, context):
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
            chunk_usage = normalize_provider_usage(chunk)
            if chunk_usage:
                usage = dict(chunk_usage)
            chunk_finish = _provider_finish_metadata(chunk)
            if chunk_finish:
                provider_finish = chunk_finish
    except TimeoutError as exc:
        _record_model_failure(recorder, "timeout", str(exc), started_at)
        raise
    except ProviderCallCancelled as exc:
        _record_model_failure(
            recorder,
            "cancelled",
            str(exc),
            started_at,
            reason="model_call_cancelled",
            metadata=provider_cancellation_uncertainty_metadata(),
        )
        raise
    except KeyboardInterrupt as exc:
        _record_model_failure(
            recorder,
            "cancelled",
            str(exc),
            started_at,
            reason="model_call_cancelled",
            metadata=provider_cancellation_uncertainty_metadata(),
        )
        raise
    except ProviderBoundaryNotClosed:
        raise
    except Exception as exc:
        if isinstance(exc, NotImplementedError):
            raise
        reason, metadata = _provider_failure_classification(exc)
        _record_model_failure(
            recorder,
            "model_error",
            str(exc),
            started_at,
            reason=reason,
            metadata=metadata,
        )
        raise
    return _StreamModelResponse(
        content="".join(text_parts),
        tool_calls=_merge_stream_tool_calls(tool_calls, tool_call_chunks),
        usage=usage,
        estimated_usage=estimate_model_call_usage(
            provider_messages=provider_messages,
            accepted_output={
                "content": "".join(text_parts),
                "tool_calls": _normalized_tool_calls(
                    _merge_stream_tool_calls(tool_calls, tool_call_chunks),
                    model_call_id=model_call_id,
                ),
            },
        ),
        duration_seconds=monotonic() - started_at,
        provider_finish=provider_finish,
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
                "estimated_usage": response.estimated_usage,
                **(
                    {"provider_finish": response.provider_finish}
                    if response.provider_finish
                    else {}
                ),
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
            "provider_finish": response.provider_finish,
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
    request: AgentRunRequest,
    context: RunContext,
) -> object:
    invoke_kwargs = _main_agent_request_options(request)
    ainvoke = getattr(model, "ainvoke", None)
    if callable(ainvoke):
        return run_async_provider_call(
            operation="main_model",
            provider=request.model_config.get("provider"),
            model=request.model_config.get("model"),
            call=lambda: ainvoke(messages, **invoke_kwargs)
            if invoke_kwargs
            else ainvoke(messages),
            timeout_seconds=request.timeout_seconds,
            cancellation_token=context.cancellation_token,
            register_cancellation_handle=_provider_cancellation_registry(context),
            cleanup_timeout_seconds=_provider_cleanup_timeout_seconds(request),
        )
    return run_provider_call(
        operation="main_model",
        provider=request.model_config.get("provider"),
        model=request.model_config.get("model"),
        call=lambda: model.invoke(messages, **invoke_kwargs)
        if invoke_kwargs
        else model.invoke(messages),
        timeout_seconds=request.timeout_seconds,
        cancellation_token=context.cancellation_token,
        register_cancellation_handle=_provider_cancellation_registry(context),
        cleanup_timeout_seconds=_provider_cleanup_timeout_seconds(request),
    )


def _stream_with_timeout(
    model: object,
    messages: list[object],
    request: AgentRunRequest,
    context: RunContext,
):
    stream_kwargs = _main_agent_request_options(request)
    astream = getattr(model, "astream", None)
    if callable(astream):
        yield from stream_async_provider_call(
            operation="main_model_stream",
            provider=request.model_config.get("provider"),
            model=request.model_config.get("model"),
            stream=lambda: astream(messages, **stream_kwargs)
            if stream_kwargs
            else astream(messages),
            timeout_seconds=request.timeout_seconds,
            cancellation_token=context.cancellation_token,
            register_cancellation_handle=_provider_cancellation_registry(context),
            cleanup_timeout_seconds=_provider_cleanup_timeout_seconds(request),
        )
        return
    yield from stream_provider_call(
        operation="main_model_stream",
        provider=request.model_config.get("provider"),
        model=request.model_config.get("model"),
        stream=lambda: model.stream(messages, **stream_kwargs)
        if stream_kwargs
        else model.stream(messages),
        timeout_seconds=request.timeout_seconds,
        cancellation_token=context.cancellation_token,
        register_cancellation_handle=_provider_cancellation_registry(context),
        cleanup_timeout_seconds=_provider_cleanup_timeout_seconds(request),
    )


def _main_agent_request_options(request: AgentRunRequest) -> dict[str, Any]:
    return {}


def _provider_cancellation_registry(
    context: RunContext,
) -> Callable[[object], None] | None:
    registry = context.metadata.get("provider_cancellation_registry")
    return registry if callable(registry) else None


def _provider_cleanup_timeout_seconds(request: AgentRunRequest) -> int | float:
    execution = request.model_config.get("execution")
    if isinstance(execution, dict):
        value = execution.get("cancellation_timeout_seconds")
        if isinstance(value, (int, float)) and value > 0:
            return value
    return 1


def _record_model_failure(
    recorder: Callable[[str, dict[str, Any]], None] | None,
    error_class: str,
    message: str,
    start: float,
    *,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if recorder is None:
        return
    error = {
        "error_class": error_class,
        **({"reason": reason} if reason is not None else {}),
        "message": message,
        "source": "model",
        "recoverable": True,
        **({"metadata": metadata} if metadata is not None else {}),
    }
    recorder(
        "model_call_failed",
        {**error, "error": error, "duration": monotonic() - start},
    )


def _provider_failure_classification(
    exc: BaseException,
) -> tuple[str | None, dict[str, Any] | None]:
    if _exception_chain_has(exc, {"APITimeoutError"}, {"httpx.TimeoutException"}):
        return "provider_timeout", None
    if _exception_chain_has(exc, {"RateLimitError"}, set()):
        return "provider_rate_limited", None
    if _exception_chain_has(
        exc,
        {"APIConnectionError"},
        {"httpx.TransportError", "httpx.NetworkError", "httpx.ProtocolError"},
    ):
        return "provider_exception", {"transient": True}
    return None, None


def _exception_chain_has(
    exc: BaseException,
    class_names: set[str],
    qualified_names: set[str],
) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for cls in type(current).mro():
            qualified = f"{cls.__module__}.{cls.__name__}"
            provider_sdk_error = (
                cls.__name__ in class_names
                and cls.__module__.split(".", 1)[0] in {"anthropic", "openai"}
            )
            if provider_sdk_error or qualified in qualified_names:
                return True
        current = current.__cause__ or current.__context__
    return False


def _tool_calls(response: object) -> list[dict[str, Any]]:
    direct = list(getattr(response, "tool_calls", []) or [])
    if direct:
        return direct
    return _tool_calls_from_content_blocks(getattr(response, "content", None))


def _tool_calls_from_content_blocks(content: object) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = block.get("input")
        tool_call = {
            "name": name,
            "args": dict(args) if isinstance(args, dict) else {},
        }
        tool_call_id = block.get("id")
        if isinstance(tool_call_id, str) and tool_call_id:
            tool_call["id"] = tool_call_id
        tool_calls.append(tool_call)
    return tool_calls


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


def _tool_loop_conversation_messages(
    response: object,
    invoked_results: list[tuple[dict[str, Any], Any]],
) -> list[dict[str, Any]]:
    calls = [call for call, _ in invoked_results]
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "kind": "tool_call",
            "model_call_id": _model_call_id_from_calls(calls),
            "tool_call_id": None,
            "content": {
                "content": _response_content(response),
                "tool_calls": _provider_visible_tool_calls(calls),
            },
            "artifact_refs": [],
            "metadata": {},
        }
    ]
    for index, (call, result) in enumerate(invoked_results):
        tool_call_id = str(call.get("id") or f"{call['name']}_{index}")
        result_dict = result.to_dict()
        observation = _tool_observation(result_dict, tool_call_id=tool_call_id)
        messages.append(
            {
                "role": "tool",
                "kind": "tool_result",
                "model_call_id": _model_call_id_from_tool_call_id(tool_call_id),
                "tool_call_id": tool_call_id,
                "content": observation,
                "artifact_refs": list(result_dict.get("artifacts", [])),
                "metadata": dict(result_dict.get("metadata", {})),
            }
        )
    return messages


def _model_call_id_from_calls(tool_calls: list[dict[str, Any]]) -> str | None:
    model_call_ids = {
        model_call_id
        for call in tool_calls
        for tool_call_id in [call.get("id")]
        if isinstance(tool_call_id, str)
        for model_call_id in [_model_call_id_from_tool_call_id(tool_call_id)]
        if model_call_id is not None
    }
    if len(model_call_ids) == 1:
        return next(iter(model_call_ids))
    return None


def _model_call_id_from_tool_call_id(tool_call_id: str) -> str | None:
    marker = "_tool_"
    if marker not in tool_call_id:
        return None
    model_call_id, _tool_index = tool_call_id.rsplit(marker, 1)
    return model_call_id or None


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
    observation = _tool_observation(result, tool_call_id=None)
    if result.get("status") != "ok":
        return json.dumps(observation["error"], ensure_ascii=False, sort_keys=True)
    redacted_output = result.get("redacted_output")
    if observation["content"] is None and isinstance(redacted_output, str):
        return redacted_output
    content = observation["content"]
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _tool_observation(
    result: dict[str, Any],
    *,
    tool_call_id: str | None,
) -> dict[str, Any]:
    status = str(result.get("status") or "error")
    artifacts = result.get("artifacts")
    artifact_ids = list(artifacts) if isinstance(artifacts, list) else []
    metadata = result.get("metadata")
    tool_name = ""
    phase3_compatible = False
    if isinstance(metadata, dict):
        tool_name = str(metadata.get("tool_name") or "")
        phase3_compatible = metadata.get("phase3_compatible_tool_result") is True
    if status == "ok":
        content = result.get("output")
        error = None
    else:
        error = _model_visible_tool_error(result.get("error"), artifact_ids=artifact_ids)
        content = json.dumps(result, ensure_ascii=False, sort_keys=True) if phase3_compatible else None
    return {
        "message_type": "tool_result",
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "status": status,
        "content": content,
        "redacted_output": result.get("redacted_output"),
        "error": error,
        "artifact_ids": artifact_ids,
        "metadata": {},
    }


def _model_visible_tool_error(
    error: object,
    *,
    artifact_ids: list[str],
) -> dict[str, Any]:
    if not isinstance(error, dict):
        return {
            "error_class": "tool_error",
            "reason": "tool_execution_failed",
            "message": "Tool failed.",
            "artifact_ids": artifact_ids,
        }
    exposed_artifacts = error.get("artifact_ids")
    return {
        "error_class": str(error.get("error_class") or "tool_error"),
        "reason": str(error.get("reason") or "tool_execution_failed"),
        "message": str(error.get("message") or "Tool failed."),
        "artifact_ids": exposed_artifacts
        if isinstance(exposed_artifacts, list)
        else artifact_ids,
    }


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
    if isinstance(content, list):
        return _text_from_content_blocks(content)
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
        return _text_from_content_blocks(content)
    if isinstance(content, str):
        return content
    return ""


def _text_from_content_blocks(content: list[object]) -> str:
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _streaming_fallback(result: AgentRunResult) -> AgentRunResult:
    return AgentRunResult(
        status=result.status,
        assistant_output=result.assistant_output,
        tool_results=result.tool_results,
        usage=result.usage,
        error=result.error,
        metadata={**result.metadata, "streaming_fallback": True},
    )


def _result_metadata(response: object) -> dict[str, Any]:
    provider_finish = _provider_finish_metadata(response)
    return {"provider_finish": provider_finish} if provider_finish else {}


def _model_call_token_observation(
    *,
    response: object,
    provider_messages: list[object],
    model_call_id: str,
) -> ModelCallTokenObservation:
    usage = normalize_provider_usage(response)
    tool_calls = _normalized_tool_calls(_tool_calls(response), model_call_id=model_call_id)
    estimated_usage = estimate_model_call_usage(
        provider_messages=provider_messages,
        accepted_output={
            "content": _response_content(response),
            "tool_calls": tool_calls,
        },
    )
    return _token_observation_from_usage_dicts(
        provider_usage=usage,
        estimated_usage=estimated_usage,
    )


def _token_observation_from_usage_dicts(
    *,
    provider_usage: dict[str, Any],
    estimated_usage: dict[str, Any],
) -> ModelCallTokenObservation:
    estimated = token_usage_from_mapping(estimated_usage)
    if estimated is None:
        raise ValueError("Estimated model-call usage must be complete.")
    return ModelCallTokenObservation(
        provider_usage=token_usage_from_mapping(provider_usage),
        estimated_usage=estimated,
    )


def _usage_metadata(summary: dict[str, Any]) -> dict[str, Any]:
    estimated_usage = (
        dict(summary["estimated_usage"])
        if isinstance(summary.get("estimated_usage"), dict)
        else {}
    )
    estimator_version = summary.get("estimator_version")
    if isinstance(estimator_version, str):
        estimated_usage["estimator_version"] = estimator_version
    token_source = summary.get("token_source")
    if token_source == "provider":
        return {
            "provider_usage_available": True,
            "token_source": "provider",
            "estimated_usage": estimated_usage,
        }
    return {
        "provider_usage_available": False,
        "token_source": "estimated",
        "estimated_usage": estimated_usage,
    }


def _provider_finish_metadata(response: object) -> dict[str, Any]:
    finish_reason = _provider_finish_reason(response)
    if finish_reason is None:
        return {}
    return {
        "finish_reason": finish_reason,
        "output_token_limit_reached": finish_reason
        in {"length", "max_tokens", "max_output_tokens", "token_limit"},
    }


def _provider_finish_reason(response: object) -> str | None:
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        for key in ("finish_reason", "stop_reason", "stop_sequence"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    usage_metadata = getattr(response, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        value = usage_metadata.get("finish_reason")
        if isinstance(value, str) and value:
            return value
    direct = getattr(response, "finish_reason", None)
    if isinstance(direct, str) and direct:
        return direct
    direct = getattr(response, "stop_reason", None)
    if isinstance(direct, str) and direct:
        return direct
    return None


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
    normalized_error = payload.get("error")
    if isinstance(normalized_error, dict):
        return dict(normalized_error)
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
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        assistant_output=None,
        tool_results=[],
        usage={},
        error={
            "error_class": error_class,
            **({"reason": reason} if reason is not None else {}),
            "message": message,
            "source": source,
            "recoverable": True,
            **({"metadata": metadata} if metadata is not None else {}),
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
    tool_loop_messages: list[dict[str, Any]] | None = None,
) -> AgentRunResult | None:
    if not tool_results:
        return None
    latest = tool_results[-1]
    metadata = latest.get("metadata")
    error = latest.get("error")
    if not isinstance(metadata, dict) or metadata.get("turn_aborted") is not True:
        return None
    if not isinstance(error, dict):
        return None
    is_legacy_denial = error.get("error_class") == "policy_denied"
    is_normalized_denial = (
        error.get("error_class") == "policy_error"
        and error.get("reason") in {"approval_denied", "approval_required_non_interactive"}
    )
    if not is_legacy_denial and not is_normalized_denial:
        return None
    return AgentRunResult(
        status="failed",
        assistant_output=None,
        tool_results=list(tool_results),
        usage={},
        error=error,
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
            **(
                {"turn_tool_loop_messages": tool_loop_messages}
                if tool_loop_messages
                else {}
            ),
        },
    )

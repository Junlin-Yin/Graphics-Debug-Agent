from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, TextIO

from debug_agent.tools.broker import ApprovalDecision
from debug_agent.cli.repl_view import (
    ReplView,
    ReplViewEvent,
    SessionCloseSummary,
    StatusBarSnapshot,
    ToolResultPreviewFormatter,
    WelcomeSnapshot,
    build_welcome_snapshot,
)
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.orchestrator import ReplRuntime, RuntimeOrchestrator
from debug_agent.runtime.stream_events import AgentStreamEvent


BUSY_MESSAGE = "Prompt run is already executing. Input is disabled."
STREAMING_FALLBACK_MESSAGE = (
    "streaming unavailable for this model; using non-streaming response."
)


@dataclass
class ReplController:
    runtime: ReplRuntime
    view: ReplView | None = None
    wakeup_callback: Callable[[], None] | None = None
    time_fn: Callable[[], float] = monotonic
    is_executing: bool = False
    exit_code: int = 0
    _turn_counter: int = 0
    _active_turn_id: int | None = None
    _active_turn_started_at: float | None = None
    _last_elapsed_seconds: int = -1
    _active_thread: threading.Thread | None = None
    _completion_queue: queue.Queue[AgentRunResult] = field(default_factory=queue.Queue)
    _stream_event_queue: queue.Queue[AgentStreamEvent] = field(default_factory=queue.Queue)
    _streamed_model_text: dict[str, str] = field(default_factory=dict)
    _streamed_tool_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    _streamed_output_seen: bool = False
    _usage_input_tokens: int | None = None
    _usage_output_tokens: int | None = None
    _usage_total_tokens: int | None = None
    _context_used_tokens: int | None = None
    _stream_usage_accounted: bool = False
    _approval_condition: threading.Condition = field(
        default_factory=threading.Condition
    )
    _approval_decision: ApprovalDecision | None = None
    _approval_pending: bool = False

    @classmethod
    def start(
        cls,
        *,
        config_snapshot: dict[str, Any],
        approval_mode: str = "normal",
        workspace_root: str | None = None,
        view: ReplView | None = None,
        wakeup_callback: Callable[[], None] | None = None,
    ) -> ReplController:
        result = RuntimeOrchestrator(workspace_root=workspace_root).start_repl(
            config_snapshot,
            approval_mode=approval_mode,
        )
        if result.error is not None or result.runtime is None:
            raise ReplStartFailed(result.error.exit_code, result.error.message)
        return cls(
            runtime=result.runtime,
            view=view,
            wakeup_callback=wakeup_callback,
        )

    def handle_line(self, line: str, output: TextIO) -> bool:
        command = line.strip()
        if not command:
            return True
        if command.startswith("/"):
            return self._handle_plain_slash_command(command, output)
        if self.is_executing:
            print(BUSY_MESSAGE, file=output)
            return True
        self.is_executing = True
        try:
            result = self.runtime.run_turn(command)
        finally:
            self.is_executing = False
        if result.status == "completed":
            print(result.assistant_output or "", file=output)
            return True
        if _is_turn_scoped_failure(result):
            message = result.error["message"] if result.error else "Prompt execution failed."
            print(message, file=output)
            return True
        self.runtime.fail(result)
        self.exit_code = 1
        message = result.error["message"] if result.error else "Prompt execution failed."
        print(message, file=output)
        return False

    def on_submit(self, text: str) -> None:
        command = text.strip()
        if not command:
            return
        if self._approval_pending:
            self._handle_approval_response(command)
            return
        if command.startswith("/"):
            self.on_slash_command(command)
            return
        if self.is_executing:
            self._append_system_message(BUSY_MESSAGE)
            return

        self._turn_counter += 1
        self._active_turn_id = self._turn_counter
        self._active_turn_started_at = self.time_fn()
        self._last_elapsed_seconds = 0
        self.is_executing = True
        if self.view is not None:
            self.view.append_user_message(command)
            self.view.set_input_enabled(False)
            self.view.set_turn_status(self._active_turn_id, "running", 0)

        self._active_thread = threading.Thread(
            target=self._run_turn_background,
            args=(command,),
            daemon=True,
        )
        self._active_thread.start()

    def on_slash_command(self, command: str) -> bool:
        if self.is_executing:
            return True
        if command == "/status":
            self._append_system_message("\n".join(self.runtime.status_lines()))
            return True
        if command == "/skills":
            self._append_system_message("\n".join(self.runtime.skill_lines()))
            return True
        if command == "/tools":
            self._append_system_message("\n".join(self.runtime.tool_lines()))
            return True
        if command == "/compress":
            result = self.runtime.manual_compress()
            estimate = result.metadata.get("context_estimate")
            if isinstance(estimate, dict):
                total = estimate.get("total_tokens")
                if isinstance(total, int):
                    self._context_used_tokens = total
            if result.status == "completed":
                self._append_system_message(result.assistant_output or "")
                if self.view is not None:
                    self.view.update_status_bar(self._status_bar_snapshot())
                return True
            if _is_turn_scoped_failure(result):
                message = result.error["message"] if result.error else "Compression failed."
                if self.view is not None:
                    self.view.show_error(message)
                    self.view.set_input_enabled(True)
                return True
            self.runtime.fail(result)
            self.exit_code = 1
            message = result.error["message"] if result.error else "Compression failed."
            if self.view is not None:
                self.view.show_error(message)
                self.view.set_input_enabled(False)
            return False
        if command == "/exit":
            self.runtime.complete()
            if self.view is not None:
                self.view.show_session_closed(self._session_close_summary("closed"))
            self.exit_code = 0
            return False
        self._append_system_message(f"Unsupported Phase 1 slash command: {command}")
        return True

    def on_approval_mode_cycle(self) -> bool:
        if self.is_executing or self._approval_pending:
            return True
        cycle = getattr(self.runtime, "cycle_approval_mode", None)
        if not callable(cycle):
            return True
        cycle()
        if self.view is not None:
            self.view.update_status_bar(self._status_bar_snapshot())
        return True

    def request_approval(self, request: str, facts: dict[str, Any]) -> ApprovalDecision:
        with self._approval_condition:
            self._approval_decision = None
            self._approval_pending = True
        if self.view is not None and hasattr(self.view, "begin_inline_approval"):
            self.view.begin_inline_approval(request)
        else:
            self._append_system_message(request)
        if self.view is not None:
            self.view.set_input_enabled(True)
        self.notify_event_ready()
        try:
            with self._approval_condition:
                while self._approval_decision is None:
                    self._approval_condition.wait()
                decision = self._approval_decision
                self._approval_decision = None
                self._approval_pending = False
        finally:
            if self.view is not None and hasattr(self.view, "end_inline_approval"):
                self.view.end_inline_approval()
        if self.view is not None and self.is_executing:
            self.view.set_input_enabled(False)
        return decision

    def _handle_approval_response(self, command: str) -> None:
        normalized = command.strip().lower()
        if normalized == "y":
            decision = ApprovalDecision("approved_once", "once")
        elif normalized == "a":
            decision = ApprovalDecision("approved_for_session", "session")
        elif normalized == "n":
            decision = ApprovalDecision("denied", "none")
        else:
            return
        with self._approval_condition:
            self._approval_decision = decision
            self._approval_condition.notify_all()

    def on_interrupt(self) -> None:
        result = _error_result(
            "cancelled",
            "cancelled",
            "REPL interrupted by Ctrl+C.",
        )
        self.runtime.fail(result)
        self.exit_code = 1
        self.is_executing = False
        if self.view is not None:
            self.view.set_input_enabled(False)
            self.view.show_session_closed(
                self._session_close_summary("cancelled", "cancelled")
            )

    def welcome_snapshot(self) -> WelcomeSnapshot:
        session = self.runtime.sessions.get(self.runtime.session_id)
        return build_welcome_snapshot(
            config_snapshot=session.config_snapshot,
            workspace_root=session.workspace_root,
            approval_mode=session.approval_mode,
            session_id=session.session_id,
        )

    def status_bar_snapshot(self) -> StatusBarSnapshot:
        return self._status_bar_snapshot()

    def notify_event_ready(self) -> None:
        if self.wakeup_callback is not None:
            self.wakeup_callback()

    def drain_completed_turns(self) -> int:
        drained = 0
        while True:
            try:
                result = self._completion_queue.get_nowait()
            except queue.Empty:
                return drained
            self.on_turn_finished(result)
            drained += 1

    def on_agent_stream_event(self, event: AgentStreamEvent) -> None:
        self._stream_event_queue.put(event)
        self.notify_event_ready()

    def drain_stream_events(self) -> int:
        drained = 0
        while True:
            try:
                event = self._stream_event_queue.get_nowait()
            except queue.Empty:
                return drained
            self._map_stream_event(event)
            drained += 1

    def on_turn_finished(self, result: AgentRunResult) -> None:
        turn_id = self._active_turn_id or self._turn_counter or 1
        elapsed_seconds = self._elapsed_seconds()
        self._update_usage(result)
        should_reenable_input = True
        if result.status == "completed":
            if result.metadata.get("streaming_fallback"):
                self._append_system_message(STREAMING_FALLBACK_MESSAGE)
                self._append_result_events(result)
            elif not self._streamed_output_seen:
                self._append_result_events(result)
        elif _is_turn_scoped_failure(result):
            if _is_approval_denied_abort(result):
                self._append_system_message("Approval denied. Current turn ended.")
            else:
                message = (
                    result.error["message"] if result.error else "Prompt execution failed."
                )
                if self.view is not None:
                    self.view.show_error(message)
            should_reenable_input = True
        else:
            self.runtime.fail(result)
            self.exit_code = 1
            message = result.error["message"] if result.error else "Prompt execution failed."
            if self.view is not None:
                self.view.show_error(message)
            should_reenable_input = False
        if self.view is not None:
            self.view.set_turn_status(
                turn_id,
                _display_status(result),
                elapsed_seconds,
            )
            self.view.update_status_bar(self._status_bar_snapshot())
            self.view.set_input_enabled(should_reenable_input)
            if not should_reenable_input:
                self.view.show_session_closed(
                    self._session_close_summary(
                        _terminal_summary_status(result),
                        _error_class(result),
                    )
                )
        self.is_executing = False
        self._active_turn_id = None
        self._active_turn_started_at = None
        self._streamed_model_text.clear()
        self._streamed_tool_blocks.clear()
        self._streamed_output_seen = False
        self._stream_usage_accounted = False

    def update_running_turn_status(self) -> None:
        if not self.is_executing or self._active_turn_id is None or self.view is None:
            return
        elapsed_seconds = self._elapsed_seconds()
        if elapsed_seconds <= self._last_elapsed_seconds:
            return
        self._last_elapsed_seconds = elapsed_seconds
        self.view.set_turn_status(self._active_turn_id, "running", elapsed_seconds)

    def wait_for_active_turn(self, timeout: float | None = None) -> None:
        thread = self._active_thread
        if thread is not None:
            thread.join(timeout=timeout)

    def close(self) -> None:
        self.runtime.close()

    def _run_turn_background(self, command: str) -> None:
        try:
            result = self.runtime.run_turn(
                command,
                agent_stream_callback=self.on_agent_stream_event,
            )
        except KeyboardInterrupt as exc:
            result = _error_result("cancelled", "cancelled", str(exc))
        except TimeoutError as exc:
            result = _error_result("timeout", "timeout", str(exc))
        except Exception as exc:
            result = _error_result("failed", "model_error", str(exc))
        self._completion_queue.put(result)
        self.notify_event_ready()

    def _map_stream_event(self, event: AgentStreamEvent) -> None:
        if self.view is None:
            return
        try:
            if event.kind == "stream_text_delta":
                model_call_id = _required_str(event.payload, "model_call_id")
                text = _required_str(event.payload, "text")
                self._streamed_model_text[model_call_id] = (
                    self._streamed_model_text.get(model_call_id, "") + text
                )
                self._streamed_output_seen = True
                self.view.append_view_event(
                    ReplViewEvent(
                        kind="model_text_delta",
                        payload={"model_call_id": model_call_id, "text": text},
                    )
                )
                return
            if event.kind == "stream_context_estimate_updated":
                self._update_context_from_estimate(
                    event.payload.get("context_estimate")
                )
                self.view.update_status_bar(self._status_bar_snapshot())
                return
            if event.kind == "stream_model_call_completed":
                model_call_id = _required_str(event.payload, "model_call_id")
                self._update_usage_from_values(
                    event.payload.get("usage"),
                    fallback_estimate=event.payload.get("context_estimate"),
                )
                self._stream_usage_accounted = True
                if bool(event.payload.get("is_final")):
                    text = self._streamed_model_text.get(model_call_id)
                    if text:
                        self._streamed_output_seen = True
                        self.view.append_view_event(
                            ReplViewEvent(
                                kind="model_markdown_final",
                                payload={"model_call_id": model_call_id, "text": text},
                            )
                        )
                self.view.update_status_bar(self._status_bar_snapshot())
                return
            if event.kind == "stream_tool_call_started":
                self._record_stream_tool_started(event)
                return
            if event.kind == "stream_tool_call_completed":
                self._append_stream_tool_completed(event)
                return
            if event.kind == "stream_tool_result":
                self._append_stream_tool_result(event)
        except Exception as exc:
            self.view.append_view_event(
                ReplViewEvent(
                    kind="error_message",
                    payload={"message": f"Malformed stream event: {exc}"},
                )
            )

    def _record_stream_tool_started(self, event: AgentStreamEvent) -> None:
        tool_call_id = _required_str(event.payload, "tool_call_id")
        model_call_id = _required_str(event.payload, "model_call_id")
        state = self._streamed_tool_state(tool_call_id, model_call_id)
        state["name"] = _required_str(event.payload, "name")

    def _append_stream_tool_completed(self, event: AgentStreamEvent) -> None:
        if self.view is None:
            return
        tool_call_id = _required_str(event.payload, "tool_call_id")
        model_call_id = _required_str(event.payload, "model_call_id")
        state = self._streamed_tool_state(tool_call_id, model_call_id)
        name = event.payload.get("name")
        if isinstance(name, str) and name:
            state["name"] = name
        tool_name = _required_str(state, "name")
        state["status"] = str(event.payload.get("status") or "unknown")
        duration_ms = event.payload.get("duration_ms")
        if isinstance(duration_ms, int):
            state["duration_ms"] = duration_ms
        self._streamed_output_seen = True
        self._emit_stream_tool_block(
            tool_call_id,
            model_call_id,
            tool_name,
            state,
            include_duration=True,
        )

    def _append_stream_tool_result(self, event: AgentStreamEvent) -> None:
        if self.view is None:
            return
        tool_call_id = _required_str(event.payload, "tool_call_id")
        model_call_id = _required_str(event.payload, "model_call_id")
        state = self._streamed_tool_state(tool_call_id, model_call_id)
        tool_name = _required_str(state, "name")
        preview = ToolResultPreviewFormatter().format(
            output=event.payload.get("output"),
            redacted_output=event.payload.get("redacted_output"),
            artifact_ids=list(event.payload.get("artifact_ids", [])),
        )
        state["preview"] = preview
        self._streamed_output_seen = True
        result_state = {**state, "status": "result"}
        self._emit_stream_tool_block(
            tool_call_id,
            model_call_id,
            tool_name,
            result_state,
            include_duration=False,
        )

    def _streamed_tool_state(
        self, tool_call_id: str, model_call_id: str
    ) -> dict[str, Any]:
        return self._streamed_tool_blocks.setdefault(
            tool_call_id,
            {
                "status": "unknown",
                "model_call_id": model_call_id,
            },
        )

    def _emit_stream_tool_block(
        self,
        tool_call_id: str,
        model_call_id: str,
        tool_name: str,
        state: dict[str, Any],
        *,
        include_duration: bool,
    ) -> None:
        if self.view is None:
            return
        metadata: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "model_call_id": model_call_id,
            "tool_name": tool_name,
        }
        duration_ms = state.get("duration_ms")
        if include_duration and isinstance(duration_ms, int):
            metadata["duration_ms"] = duration_ms
        payload: dict[str, Any] = {
            "name": tool_name,
            "status": state.get("status", "unknown"),
            "metadata": metadata,
        }
        preview = state.get("preview")
        if preview is not None:
            payload["preview"] = preview
        self.view.append_view_event(
            ReplViewEvent(
                kind="tool_block",
                payload=payload,
            )
        )

    def _handle_plain_slash_command(self, command: str, output: TextIO) -> bool:
        if command == "/status":
            print("\n".join(self.runtime.status_lines()), file=output)
            return True
        if command == "/skills":
            print("\n".join(self.runtime.skill_lines()), file=output)
            return True
        if command == "/tools":
            print("\n".join(self.runtime.tool_lines()), file=output)
            return True
        if command == "/compress":
            result = self.runtime.manual_compress()
            if result.status == "completed":
                print(result.assistant_output or "", file=output)
                return True
            if _is_turn_scoped_failure(result):
                message = result.error["message"] if result.error else "Compression failed."
                print(message, file=output)
                return True
            self.runtime.fail(result)
            self.exit_code = 1
            message = result.error["message"] if result.error else "Compression failed."
            print(message, file=output)
            return False
        if command == "/exit":
            self.runtime.complete()
            return False
        print(f"Unsupported Phase 1 slash command: {command}", file=output)
        return True

    def _append_result_events(self, result: AgentRunResult) -> None:
        if self.view is None:
            return
        if result.assistant_output is not None:
            self.view.append_view_event(
                ReplViewEvent(
                    kind="model_markdown_final",
                    payload={"text": result.assistant_output},
                )
            )
        formatter = ToolResultPreviewFormatter()
        for tool_result in result.tool_results:
            preview = formatter.format(
                output=tool_result.get("output"),
                redacted_output=tool_result.get("redacted_output"),
                artifact_ids=list(tool_result.get("artifacts", [])),
            )
            self.view.append_view_event(
                ReplViewEvent(
                    kind="tool_block",
                    payload={
                        "status": tool_result.get("status"),
                        "error": tool_result.get("error"),
                        "metadata": tool_result.get("metadata", {}),
                        "preview": preview,
                    },
                )
            )

    def _append_system_message(self, message: str) -> None:
        if self.view is not None:
            self.view.append_view_event(
                ReplViewEvent(kind="system_message", payload={"message": message})
            )

    def _elapsed_seconds(self) -> int:
        if self._active_turn_started_at is None:
            return 0
        return max(0, int(self.time_fn() - self._active_turn_started_at))

    def _update_usage(self, result: AgentRunResult) -> None:
        if not self._stream_usage_accounted:
            self._update_usage_from_values(
                result.usage,
                fallback_estimate=result.metadata.get("context_estimate"),
            )
        estimate = getattr(self.runtime, "latest_context_estimate", None)
        self._update_context_from_estimate(estimate)

    def _update_usage_from_values(
        self,
        usage_value: object,
        *,
        fallback_estimate: object,
    ) -> None:
        usage = usage_value if isinstance(usage_value, dict) else {}
        input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
        total_tokens = _usage_value(usage, "total_tokens")
        if input_tokens is not None:
            self._usage_input_tokens = input_tokens
        if output_tokens is not None:
            self._usage_output_tokens = output_tokens
        if total_tokens is not None:
            self._usage_total_tokens = (self._usage_total_tokens or 0) + total_tokens
        elif input_tokens is not None or output_tokens is not None:
            known_input = self._usage_input_tokens or 0
            known_output = self._usage_output_tokens or 0
            self._usage_total_tokens = known_input + known_output
        else:
            if isinstance(fallback_estimate, dict):
                fallback_total = fallback_estimate.get("total_tokens")
                if isinstance(fallback_total, int):
                    self._usage_total_tokens = (
                        self._usage_total_tokens or 0
                    ) + fallback_total

    def _update_context_from_estimate(self, estimate: object) -> None:
        if isinstance(estimate, dict):
            total = estimate.get("total_tokens")
            if isinstance(total, int):
                self._context_used_tokens = total

    def _status_bar_snapshot(self) -> StatusBarSnapshot:
        context_used = self._context_used_tokens
        estimate = getattr(self.runtime, "latest_context_estimate", None)
        if isinstance(estimate, dict) and isinstance(estimate.get("total_tokens"), int):
            context_used = estimate["total_tokens"]
        context_window = self._context_window_tokens()
        return StatusBarSnapshot(
            input_tokens=self._usage_input_tokens,
            output_tokens=self._usage_output_tokens,
            total_tokens=self._usage_total_tokens,
            approval_mode=self._approval_mode(),
            model=self._model_name(),
            context_used_tokens=context_used,
            context_window_tokens=context_window,
            context_percent=_context_percent(context_used, context_window),
        )

    def _session_close_summary(
        self, status: str, error_type: str | None = None
    ) -> SessionCloseSummary:
        return SessionCloseSummary(
            session_id=str(getattr(self.runtime, "session_id", "")),
            status=status,  # type: ignore[arg-type]
            input_tokens=self._usage_input_tokens,
            output_tokens=self._usage_output_tokens,
            total_tokens=self._usage_total_tokens,
            error_type=error_type,
        )

    def _approval_mode(self) -> str:
        approval_mode = getattr(self.runtime, "approval_mode", None)
        if approval_mode:
            return str(approval_mode)
        try:
            return str(self.runtime.sessions.get(self.runtime.session_id).approval_mode)
        except Exception:
            return "unknown"

    def _model_name(self) -> str:
        config_snapshot = getattr(self.runtime, "config_snapshot", None)
        if isinstance(config_snapshot, dict):
            return str(config_snapshot.get("model") or "unknown")
        try:
            session = self.runtime.sessions.get(self.runtime.session_id)
        except Exception:
            return "unknown"
        return str(session.config_snapshot.get("model") or "unknown")

    def _context_window_tokens(self) -> int | None:
        config_snapshot = getattr(self.runtime, "config_snapshot", None)
        if not isinstance(config_snapshot, dict):
            try:
                config_snapshot = self.runtime.sessions.get(
                    self.runtime.session_id
                ).config_snapshot
            except Exception:
                return None
        context = config_snapshot.get("context")
        if isinstance(context, dict) and isinstance(context.get("window_tokens"), int):
            return context["window_tokens"]
        value = config_snapshot.get("window_tokens")
        return value if isinstance(value, int) else None


class ReplStartFailed(RuntimeError):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


class ControllerApprovalProvider:
    is_interactive = True

    def __init__(self, controller: ReplController) -> None:
        self.controller = controller

    def request_approval(self, request: str, facts: dict[str, Any]) -> ApprovalDecision:
        return self.controller.request_approval(request, facts)


def _usage_value(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


def _context_percent(used: int | None, window: int | None) -> int | None:
    if used is None or window is None or window <= 0:
        return None
    return int((used / window) * 100)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing stream payload field: {key}")
    return value


def _error_result(status: str, error_class: str, message: str) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        assistant_output=None,
        tool_results=[],
        usage={},
        error={
            "error_class": error_class,
            "message": message,
            "source": "controller",
            "recoverable": False,
        },
        metadata={},
    )


def _display_status(result: AgentRunResult) -> str:
    if result.status == "failed" and result.error:
        error_class = result.error.get("error_class")
        if error_class in {"cancelled", "timeout"}:
            return str(error_class)
    return result.status


def _error_class(result: AgentRunResult) -> str | None:
    if result.error is None:
        return None
    value = result.error.get("error_class")
    return str(value) if value else None


def _terminal_summary_status(result: AgentRunResult) -> str:
    return "cancelled" if _error_class(result) == "cancelled" else "failed"


def _is_turn_scoped_failure(result: AgentRunResult) -> bool:
    return result.status == "failed" and result.metadata.get("failure_scope") == "turn"


def _is_approval_denied_abort(result: AgentRunResult) -> bool:
    return (
        _is_turn_scoped_failure(result)
        and result.metadata.get("approval_denied_abort") is True
        and _error_class(result) == "policy_denied"
    )

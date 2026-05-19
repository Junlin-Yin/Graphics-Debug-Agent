from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, TextIO

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

    @classmethod
    def start(
        cls,
        *,
        config_snapshot: dict[str, Any],
        workspace_root: str | None = None,
        view: ReplView | None = None,
        wakeup_callback: Callable[[], None] | None = None,
    ) -> ReplController:
        result = RuntimeOrchestrator(workspace_root=workspace_root).start_repl(
            config_snapshot
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
        if command == "/exit":
            self.runtime.complete()
            if self.view is not None:
                self.view.show_session_closed(self._session_close_summary("closed"))
            self.exit_code = 0
            return False
        self._append_system_message(f"Unsupported Phase 0 slash command: {command}")
        return True

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
        self._update_usage(result.usage)
        should_reenable_input = True
        if result.status == "completed":
            if result.metadata.get("streaming_fallback"):
                self._append_system_message(STREAMING_FALLBACK_MESSAGE)
                self._append_result_events(result)
            elif not self._streamed_output_seen:
                self._append_result_events(result)
        elif _is_turn_scoped_failure(result):
            message = result.error["message"] if result.error else "Prompt execution failed."
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
            if event.kind == "stream_model_call_completed":
                model_call_id = _required_str(event.payload, "model_call_id")
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
        if command == "/exit":
            self.runtime.complete()
            return False
        print(f"Unsupported Phase 0 slash command: {command}", file=output)
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

    def _update_usage(self, usage: dict[str, Any]) -> None:
        input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
        total_tokens = _usage_value(usage, "total_tokens")
        if input_tokens is not None:
            self._usage_input_tokens = input_tokens
        if output_tokens is not None:
            self._usage_output_tokens = output_tokens
        if total_tokens is not None:
            self._usage_total_tokens = total_tokens
        elif input_tokens is not None or output_tokens is not None:
            known_input = self._usage_input_tokens or 0
            known_output = self._usage_output_tokens or 0
            self._usage_total_tokens = known_input + known_output

    def _status_bar_snapshot(self) -> StatusBarSnapshot:
        return StatusBarSnapshot(
            input_tokens=self._usage_input_tokens,
            output_tokens=self._usage_output_tokens,
            total_tokens=self._usage_total_tokens,
            approval_mode=self._approval_mode(),
            model=self._model_name(),
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


class ReplStartFailed(RuntimeError):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


def _usage_value(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


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

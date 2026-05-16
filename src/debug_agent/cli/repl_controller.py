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
)
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.orchestrator import ReplRuntime, RuntimeOrchestrator


BUSY_MESSAGE = "Prompt run is already executing. Use /status or /exit."


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
        self._append_system_message("Interrupt requested; active turn continues to the next safe boundary.")

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

    def on_turn_finished(self, result: AgentRunResult) -> None:
        turn_id = self._active_turn_id or self._turn_counter or 1
        elapsed_seconds = self._elapsed_seconds()
        self._update_usage(result.usage)
        if result.status == "completed":
            self._append_result_events(result)
        else:
            self.runtime.fail(result)
            self.exit_code = 1
            message = result.error["message"] if result.error else "Prompt execution failed."
            if self.view is not None:
                self.view.show_error(message)
        if self.view is not None:
            self.view.set_turn_status(
                turn_id,
                _display_status(result),
                elapsed_seconds,
            )
            self.view.update_status_bar(self._status_bar_snapshot())
            self.view.set_input_enabled(True)
        self.is_executing = False
        self._active_turn_id = None
        self._active_turn_started_at = None

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
            result = self.runtime.run_turn(command)
        except KeyboardInterrupt as exc:
            result = _error_result("cancelled", "cancelled", str(exc))
        except TimeoutError as exc:
            result = _error_result("timeout", "timeout", str(exc))
        except Exception as exc:
            result = _error_result("failed", "model_error", str(exc))
        self._completion_queue.put(result)
        self.notify_event_ready()

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

    def _session_close_summary(self, status: str) -> SessionCloseSummary:
        return SessionCloseSummary(
            session_id=str(getattr(self.runtime, "session_id", "")),
            status=status,  # type: ignore[arg-type]
            input_tokens=self._usage_input_tokens,
            output_tokens=self._usage_output_tokens,
            total_tokens=self._usage_total_tokens,
            error_type=None,
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

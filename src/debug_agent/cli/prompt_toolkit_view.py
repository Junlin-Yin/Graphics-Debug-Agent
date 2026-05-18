from __future__ import annotations

import asyncio
from io import StringIO
import sys
from time import monotonic, sleep
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app, run_in_terminal
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import print_formatted_text
from rich.console import Console
from rich.markdown import Markdown

from debug_agent.cli.repl_view import (
    PromptHistory,
    ReplViewEvent,
    SessionCloseSummary,
    StatusBarSnapshot,
    WelcomeSnapshot,
    format_token_count,
)


max_markdown_render_chars = 50_000
stream_flush_interval_seconds = 0.25


class PromptToolkitReplView:
    def __init__(self) -> None:
        self._history = PromptHistory()
        self._messages: list[str] = []
        self._model_message_indexes: dict[str, int] = {}
        self._model_visible_text: dict[str, str] = {}
        self._pending_model_output: dict[str, str] = {}
        self._turn_status: str | None = None
        self._last_invalidated_toolbar: str | None = None
        self._last_stream_flush_at = 0.0
        self._stream_bottom_status_visible = False
        self._status_bar = _format_status_bar(
            StatusBarSnapshot(
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                approval_mode="unknown",
                model="unknown",
            )
        )
        self._input_enabled = True
        self._closed = False
        self._active_controller: object | None = None
        self._session = PromptSession(
            multiline=False,
            key_bindings=_key_bindings(self._handle_prompt_enter_event),
            bottom_toolbar=self._bottom_toolbar,
        )
        self._session.default_buffer.read_only = Condition(
            lambda: not self._input_enabled
        )

    def run(self, controller: object) -> int:
        setattr(controller, "view", self)
        self._active_controller = controller
        setattr(controller, "wakeup_callback", lambda: None)
        if hasattr(controller, "welcome_snapshot"):
            self.show_welcome(controller.welcome_snapshot())
        if hasattr(controller, "status_bar_snapshot"):
            self.update_status_bar(controller.status_bar_snapshot())

        while not self._closed:
            try:
                text = self._session.prompt(
                    HTML("<b>&gt;</b> "),
                    pre_run=lambda: self._start_prompt_runtime_tasks(controller),
                )
            except (EOFError, KeyboardInterrupt):
                if hasattr(controller, "on_interrupt"):
                    controller.on_interrupt()
                return getattr(controller, "exit_code", 1)
            if text.strip():
                self.submit_input(text, lambda submitted: self._dispatch(controller, submitted))
                self._drain_until_idle(controller)
            self._drain_once(controller)
        return getattr(controller, "exit_code", 0)

    def show_welcome(self, snapshot: WelcomeSnapshot) -> None:
        self._append(
            "\n".join(
                [
                    f"{snapshot.tool_name} {snapshot.version}",
                    f"model: {snapshot.model}",
                    f"workspace: {snapshot.workspace_root}",
                    f"approval: {snapshot.approval_mode}",
                    f"session: {snapshot.session_id_short}",
                ]
            )
        )

    def set_input_enabled(self, enabled: bool) -> None:
        self._input_enabled = enabled
        if not enabled:
            self._session.default_buffer.reset()

    def append_user_message(self, message: str) -> None:
        self._reset_turn_local_model_state()
        self._append(f"you: {message}")

    def append_view_event(self, event: ReplViewEvent) -> None:
        if event.kind == "model_text_delta":
            self._append_model_delta(
                _model_call_id(event.payload),
                str(event.payload.get("text", "")),
            )
            return
        if event.kind == "model_markdown_final":
            text = str(event.payload.get("text", ""))
            self._replace_or_append_model_output(
                _model_call_id(event.payload),
                _render_markdown_or_plain(text),
            )
            return
        if event.kind == "tool_block":
            self._append_or_replace_tool_block(event.payload)
            return
        if event.kind == "system_message":
            self._append(f"system: {event.payload.get('message', '')}")
            return
        if event.kind == "error_message":
            self._append(f"error: {event.payload.get('message', '')}")

    def set_turn_status(
        self, turn_id: int, status: str, elapsed_seconds: int
    ) -> None:
        self._turn_status = f"turn {turn_id}: {status} {elapsed_seconds}s"

    def update_status_bar(self, snapshot: StatusBarSnapshot) -> None:
        self._status_bar = _format_status_bar(snapshot)

    def show_session_closed(self, summary: SessionCloseSummary) -> None:
        self._closed = True
        self._append(_format_session_close_summary(summary))

    def show_error(self, message: str) -> None:
        self._append(f"error: {message}")

    def rendered_text(self) -> str:
        return "\n".join(self._messages + [self._bottom_toolbar()])

    def submit_input(self, text: str, submit: Callable[[str], None]) -> None:
        if not self._input_enabled:
            return
        if not text.strip():
            return
        self._history.add(text)
        submit(text)

    def history_previous(self) -> str | None:
        return self._history.previous()

    def history_next(self) -> str | None:
        return self._history.next()

    def _dispatch(self, controller: object, text: str) -> None:
        if text.strip().startswith("/") and hasattr(controller, "on_slash_command"):
            should_continue = controller.on_slash_command(text.strip())
            if should_continue is False:
                self._closed = True
            return
        if hasattr(controller, "on_submit"):
            controller.on_submit(text)

    def handle_prompt_enter(self, controller: object, event: Any) -> None:
        text = str(event.current_buffer.text)
        event.current_buffer.reset()
        if not text.strip():
            return
        if not self._input_enabled and not text.strip().startswith("/"):
            return
        self._history.add(text)
        self._dispatch(controller, text)
        if self._closed:
            event.app.exit(result="")

    def _append(self, message: str) -> None:
        self._messages.append(message)
        print_formatted_text(message)

    def _append_model_delta(self, model_call_id: str, text: str) -> None:
        if model_call_id in self._model_message_indexes:
            index = self._model_message_indexes[model_call_id]
            self._messages[index] = f"{self._messages[index]}{text}"
            self._pending_model_output[model_call_id] = (
                self._pending_model_output.get(model_call_id, "") + text
            )
        else:
            self._model_message_indexes[model_call_id] = len(self._messages)
            self._messages.append(f"assistant: {text}")
            self._pending_model_output[model_call_id] = f"assistant: {text}"

    def _replace_or_append_model_output(self, model_call_id: str, text: str) -> None:
        message = f"assistant: {text}"
        if model_call_id in self._model_message_indexes:
            self._pending_model_output.pop(model_call_id, None)
            previous = self._model_visible_text.get(model_call_id, "")
            self._messages[self._model_message_indexes[model_call_id]] = message
            self._model_visible_text[model_call_id] = message
            if previous:
                self._clear_stream_bottom_status()
                _write_terminal_control(_clear_terminal_block(previous))
            print_formatted_text(message)
            return
        self._append(message)

    def _append_or_replace_tool_block(self, payload: dict) -> None:
        self._append(_format_tool_block(payload))

    def flush_pending_model_output(self, *, force: bool = False) -> bool:
        if not self._pending_model_output:
            return False
        now = monotonic()
        if not force and now - self._last_stream_flush_at < stream_flush_interval_seconds:
            return False
        self._last_stream_flush_at = now
        for model_call_id, text in list(self._pending_model_output.items()):
            _write_terminal_text(text)
            self._model_visible_text[model_call_id] = (
                self._model_visible_text.get(model_call_id, "") + text
            )
        self._pending_model_output.clear()
        return True

    async def flush_pending_model_output_in_terminal(self) -> bool:
        if not self._pending_model_output:
            return False
        return await run_in_terminal(
            lambda: self.flush_pending_model_output(force=True),
            render_cli_done=False,
        )

    def _clear_stream_bottom_status(self) -> None:
        if not self._stream_bottom_status_visible:
            return
        _write_terminal_control("\r\x1b[2K\x1b[1A\r")
        self._stream_bottom_status_visible = False

    def _reset_turn_local_model_state(self) -> None:
        self._model_message_indexes.clear()
        self._model_visible_text.clear()
        self._pending_model_output.clear()
        self._stream_bottom_status_visible = False

    def _bottom_toolbar(self) -> str:
        if self._turn_status is None:
            return self._status_bar
        return f"{self._turn_status} | {self._status_bar}"

    def _handle_prompt_enter_event(self, event: Any) -> None:
        if self._active_controller is None:
            return
        self.handle_prompt_enter(self._active_controller, event)

    def _start_prompt_runtime_tasks(self, controller: object) -> None:
        app = get_app()
        setattr(controller, "wakeup_callback", lambda: None)
        app.create_background_task(self._drain_prompt_runtime(controller, app))

    async def _drain_prompt_runtime(self, controller: object, app: object) -> None:
        while not self._closed:
            self._drain_once(controller)
            stream_flushed = await self.flush_pending_model_output_in_terminal()
            if stream_flushed and hasattr(app, "invalidate"):
                app.invalidate()
            self.invalidate_toolbar_if_changed(app)
            await asyncio.sleep(0.1)

    def invalidate_toolbar_if_changed(self, app: object) -> None:
        rendered = self._bottom_toolbar()
        if rendered == self._last_invalidated_toolbar:
            return
        self._last_invalidated_toolbar = rendered
        if hasattr(app, "invalidate"):
            app.invalidate()

    def _drain_once(self, controller: object) -> None:
        if hasattr(controller, "update_running_turn_status"):
            controller.update_running_turn_status()
        if hasattr(controller, "drain_stream_events"):
            controller.drain_stream_events()
        if hasattr(controller, "drain_completed_turns"):
            controller.drain_completed_turns()

    def _drain_until_idle(self, controller: object) -> None:
        while getattr(controller, "is_executing", False):
            self._drain_once(controller)
            self.flush_pending_model_output()
            sleep(0.1)
        self.flush_pending_model_output(force=True)


def _format_status_bar(snapshot: StatusBarSnapshot) -> str:
    return (
        "tokens: "
        f"{format_token_count(snapshot.input_tokens)} input, "
        f"{format_token_count(snapshot.output_tokens)} output, "
        f"{format_token_count(snapshot.total_tokens)} total | "
        f"mode: {snapshot.approval_mode} | model: {snapshot.model}"
    )


def _format_session_close_summary(summary: SessionCloseSummary) -> str:
    status = "closed" if summary.status == "closed" else summary.status
    lines = [f"session {summary.session_id} {status}."]
    if summary.status == "closed":
        if (
            summary.input_tokens is None
            or summary.output_tokens is None
            or summary.total_tokens is None
        ):
            lines.append("tokens used: unavailable")
        else:
            lines.append(
                "tokens used: "
                f"{summary.input_tokens} input, "
                f"{summary.output_tokens} output, "
                f"{summary.total_tokens} total"
            )
        return "\n".join(lines)
    if summary.error_type:
        lines.append(f"error: {summary.error_type}")
    lines.append(f"trace: debug-agent trace {summary.session_id}")
    return "\n".join(lines)


def _format_tool_block(payload: dict) -> str:
    metadata = payload.get("metadata", {})
    tool_name = metadata.get("tool_name") or payload.get("name") or "unknown"
    status = payload.get("status") or "unknown"
    preview = payload.get("preview")
    if preview is not None and status == "result":
        lines = _preview_lines(preview)
        if preview.artifact_ids:
            lines.append(f"artifacts: {', '.join(preview.artifact_ids)}")
        return "\n".join(lines)
    lines = [f"tool: {tool_name}", f"status: {status}"]
    duration = _format_duration(metadata.get("duration_ms"))
    if duration is not None:
        lines.append(f"duration: {duration}")
    if preview is not None:
        lines.extend(_preview_lines(preview))
        if preview.artifact_ids:
            lines.append(f"artifacts: {', '.join(preview.artifact_ids)}")
    return "\n".join(lines)


def _preview_lines(preview: object) -> list[str]:
    text = str(getattr(preview, "text", ""))
    lines = text.splitlines() or [""]
    if bool(getattr(preview, "truncated", False)) and "[truncated:" not in text:
        shown_lines = getattr(preview, "shown_lines", "?")
        total_lines = getattr(preview, "total_lines", None)
        total = "unknown" if total_lines is None else str(total_lines)
        lines.append(f"> [truncated: showing {shown_lines} of {total} lines]")
    return lines


def _render_markdown_or_plain(text: str) -> str:
    if len(text) > max_markdown_render_chars:
        return text
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    try:
        console.print(Markdown(text))
    except Exception:
        return text
    rendered = output.getvalue().strip()
    return rendered or text


def _model_call_id(payload: dict) -> str:
    value = payload.get("model_call_id")
    if value is None:
        return "__default_model_call__"
    return str(value)


def _format_duration(duration_ms: object) -> str | None:
    if not isinstance(duration_ms, int):
        return None
    return f"{duration_ms / 1000:.1f}s"


def _clear_terminal_block(text: str) -> str:
    line_count = max(1, text.count("\n") + 1)
    return "\r\x1b[2K" + ("\x1b[1A\r\x1b[2K" * (line_count - 1))


def _write_terminal_control(text: str) -> None:
    try:
        output = get_app().output
    except Exception:
        output = None
    if output is not None and hasattr(output, "write_raw"):
        output.write_raw(text)
        if hasattr(output, "flush"):
            output.flush()
        return
    sys.stdout.write(text)
    sys.stdout.flush()


def _write_terminal_text(text: str) -> None:
    try:
        output = get_app().output
    except Exception:
        output = None
    if output is not None and hasattr(output, "write"):
        output.write(text)
        if hasattr(output, "flush"):
            output.flush()
        return
    sys.stdout.write(text)
    sys.stdout.flush()


def _key_bindings(on_enter: Callable[[Any], None] | None = None) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("c-j")
    def _(event) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("enter")
    def _(event) -> None:
        if on_enter is None:
            event.current_buffer.validate_and_handle()
            return
        on_enter(event)

    try:
        bindings.add("s-enter", eager=True)(
            lambda event: event.current_buffer.insert_text("\n")
        )
    except ValueError:
        pass

    return bindings

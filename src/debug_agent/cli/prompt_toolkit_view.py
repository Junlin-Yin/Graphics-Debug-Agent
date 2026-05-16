from __future__ import annotations

from io import StringIO
from time import sleep
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
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


class PromptToolkitReplView:
    def __init__(self) -> None:
        self._history = PromptHistory()
        self._messages: list[str] = []
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
        self._session = PromptSession(
            multiline=False,
            key_bindings=_key_bindings(),
            bottom_toolbar=lambda: self._status_bar,
        )

    def run(self, controller: object) -> int:
        setattr(controller, "view", self)
        setattr(controller, "wakeup_callback", lambda: None)
        if hasattr(controller, "welcome_snapshot"):
            self.show_welcome(controller.welcome_snapshot())
        if hasattr(controller, "status_bar_snapshot"):
            self.update_status_bar(controller.status_bar_snapshot())

        while not self._closed:
            try:
                text = self._session.prompt(HTML("<b>&gt;</b> "))
            except (EOFError, KeyboardInterrupt):
                if hasattr(controller, "on_interrupt"):
                    controller.on_interrupt()
                return getattr(controller, "exit_code", 1)
            self.submit_input(text, lambda submitted: self._dispatch(controller, submitted))
            while getattr(controller, "is_executing", False):
                if hasattr(controller, "update_running_turn_status"):
                    controller.update_running_turn_status()
                if hasattr(controller, "drain_completed_turns"):
                    controller.drain_completed_turns()
                sleep(0.1)
            if hasattr(controller, "drain_completed_turns"):
                controller.drain_completed_turns()
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

    def append_user_message(self, message: str) -> None:
        self._append(f"you: {message}")

    def append_view_event(self, event: ReplViewEvent) -> None:
        if event.kind == "model_text_delta":
            self._append(f"assistant: {event.payload.get('text', '')}")
            return
        if event.kind == "model_markdown_final":
            text = str(event.payload.get("text", ""))
            self._append(f"assistant: {_render_markdown_or_plain(text)}")
            return
        if event.kind == "tool_block":
            self._append(_format_tool_block(event.payload))
            return
        if event.kind == "system_message":
            self._append(f"system: {event.payload.get('message', '')}")
            return
        if event.kind == "error_message":
            self._append(f"error: {event.payload.get('message', '')}")

    def set_turn_status(
        self, turn_id: int, status: str, elapsed_seconds: int
    ) -> None:
        self._append(f"turn {turn_id}: {status} {elapsed_seconds}s")

    def update_status_bar(self, snapshot: StatusBarSnapshot) -> None:
        self._status_bar = _format_status_bar(snapshot)

    def show_session_closed(self, summary: SessionCloseSummary) -> None:
        self._closed = True
        self._append(_format_session_close_summary(summary))

    def show_error(self, message: str) -> None:
        self._append(f"error: {message}")

    def rendered_text(self) -> str:
        return "\n".join(self._messages + [self._status_bar])

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

    def _append(self, message: str) -> None:
        self._messages.append(message)
        print_formatted_text(message)


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
    lines = [f"tool: {tool_name}", f"status: {status}"]
    if preview is not None:
        lines.append(str(preview.text))
        if preview.artifact_ids:
            lines.append(f"artifacts: {', '.join(preview.artifact_ids)}")
    return "\n".join(lines)


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


def _key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("c-j")
    def _(event) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("enter")
    def _(event) -> None:
        event.current_buffer.validate_and_handle()

    try:
        bindings.add("s-enter", eager=True)(
            lambda event: event.current_buffer.insert_text("\n")
        )
    except ValueError:
        pass

    return bindings

from __future__ import annotations

import asyncio
from io import StringIO
import sys
from time import monotonic, sleep
from typing import Any, Callable, TextIO

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import BufferControl, FormattedTextControl, HSplit, VSplit, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.scrollable_pane import ScrollablePane
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output.base import Output
from prompt_toolkit.utils import get_cwidth
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
message_scroll_step_lines = 2
message_scroll_step_page = 10


class _MessageScrollablePane(ScrollablePane):
    def __init__(
        self,
        content: Any,
        *,
        follow_latest: Callable[[], bool],
        set_follow_latest: Callable[[bool], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._follow_latest = follow_latest
        self._set_follow_latest = set_follow_latest

    def write_to_screen(
        self,
        screen: Any,
        mouse_handlers: Any,
        write_position: Any,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        self._clamp_scroll_for_write_position(write_position)
        super().write_to_screen(
            screen,
            mouse_handlers,
            write_position,
            parent_style,
            erase_bg,
            z_index,
        )

    def _clamp_scroll_for_write_position(self, write_position: Any) -> None:
        virtual_width = (
            write_position.width - 1 if self.show_scrollbar() else write_position.width
        )
        virtual_height = self.content.preferred_height(
            virtual_width,
            self.max_available_height,
        ).preferred
        virtual_height = max(virtual_height, write_position.height)
        virtual_height = min(virtual_height, self.max_available_height)
        max_scroll = max(0, virtual_height - write_position.height)
        if self._follow_latest():
            self.vertical_scroll = max_scroll
            return
        self.vertical_scroll = min(max(0, self.vertical_scroll), max_scroll)
        if self.vertical_scroll >= max_scroll:
            self._set_follow_latest(True)


class PromptToolkitReplView:
    def __init__(
        self,
        *,
        output_stream: TextIO | None = None,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._history = PromptHistory()
        self._output_stream = output_stream or sys.stdout
        self._prompt_toolkit_input = input
        self._prompt_toolkit_output = output
        self._messages: list[str] = []
        self._model_message_indexes: dict[str, int] = {}
        self._turn_status: str | None = None
        self._last_invalidated_toolbar: str | None = None
        self._last_stream_flush_at = 0.0
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
        self._application_exit_requested = False
        self._terminal_summary: str | None = None
        self._terminal_summary_printed = False
        self._active_controller: object | None = None
        self._render_dirty = False
        self._input_visible_lines = 1
        self._message_region_follow_latest = True
        self._input_buffer = Buffer(
            multiline=True,
            read_only=Condition(lambda: not self._input_enabled),
        )
        self._input_buffer.on_text_changed += self._handle_input_text_changed
        self._application = self._build_application()

    def run(self, controller: object) -> int:
        setattr(controller, "view", self)
        self._active_controller = controller
        setattr(controller, "wakeup_callback", self._invalidate)
        if hasattr(controller, "welcome_snapshot"):
            self.show_welcome(controller.welcome_snapshot())
        if hasattr(controller, "status_bar_snapshot"):
            self.update_status_bar(controller.status_bar_snapshot())

        exit_code = getattr(controller, "exit_code", 0)
        try:
            self._application.run(
                pre_run=lambda: self._start_prompt_runtime_tasks(controller)
            )
        except (EOFError, KeyboardInterrupt):
            if hasattr(controller, "on_interrupt"):
                controller.on_interrupt()
            exit_code = getattr(controller, "exit_code", 1)
        else:
            exit_code = getattr(controller, "exit_code", 0)
        self._print_terminal_summary()
        return exit_code

    def show_welcome(self, snapshot: WelcomeSnapshot) -> None:
        self._append(_format_welcome_panel(snapshot))

    def set_input_enabled(self, enabled: bool) -> None:
        self._input_enabled = enabled
        self._invalidate()

    def append_user_message(self, message: str) -> None:
        self._reset_turn_local_model_state()
        self._append(_format_user_message(message))

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
            self._append(
                _format_labeled_message("🤖 System", event.payload.get("message", ""))
            )
            return
        if event.kind == "error_message":
            self._append(
                _format_labeled_message("❌ Error", event.payload.get("message", ""))
            )

    def set_turn_status(
        self, turn_id: int, status: str, elapsed_seconds: int
    ) -> None:
        self._turn_status = f"turn {turn_id}: {status} {elapsed_seconds}s"
        self._invalidate()

    def update_status_bar(self, snapshot: StatusBarSnapshot) -> None:
        self._status_bar = _format_status_bar(snapshot)
        self._invalidate()

    def show_session_closed(self, summary: SessionCloseSummary) -> None:
        self._closed = True
        self._terminal_summary = _format_terminal_summary(summary)
        self._exit_application()

    def show_error(self, message: str) -> None:
        self._append(_format_labeled_message("❌ Error", message))

    def rendered_text(self) -> str:
        parts = [self._message_region_text()]
        turn_status = self._current_turn_status_text()
        if turn_status:
            parts.append(turn_status)
        parts.append(self._status_bar_text())
        return "\n".join(part for part in parts if part)

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

    def apply_history_previous(self) -> None:
        self._replace_input_from_history(self._history.previous())

    def apply_history_next(self) -> None:
        self._replace_input_from_history(self._history.next())

    def handle_history_or_cursor_up(self, event: Any) -> None:
        if self._input_cursor_is_at_end():
            self.apply_history_previous()
            return
        event.current_buffer.cursor_up()

    def handle_history_or_cursor_down(self, event: Any) -> None:
        if self._input_cursor_is_at_end():
            self.apply_history_next()
            return
        event.current_buffer.cursor_down()

    def insert_input_newline(self) -> None:
        self._input_buffer.insert_text("\n")
        self._sync_input_region_height()

    def handle_interrupt_event(self, event: Any) -> None:
        if self._active_controller is not None and hasattr(
            self._active_controller, "on_interrupt"
        ):
            self._active_controller.on_interrupt()
        if not self._closed:
            self._exit_application(getattr(event, "app", None))

    def scroll_message_region_down(self) -> None:
        self._scroll_message_region_down(message_scroll_step_page)

    def scroll_message_region_up(self) -> None:
        self._scroll_message_region_up(message_scroll_step_page)

    def scroll_message_region_line_down(self) -> None:
        self._scroll_message_region_down(message_scroll_step_lines)

    def scroll_message_region_line_up(self) -> None:
        self._scroll_message_region_up(message_scroll_step_lines)

    def _scroll_message_region_down(self, step: int) -> None:
        self._message_region_container.vertical_scroll += step
        self._invalidate()

    def _scroll_message_region_up(self, step: int) -> None:
        self._message_region_follow_latest = False
        self._message_region_container.vertical_scroll = max(
            0,
            self._message_region_container.vertical_scroll - step,
        )
        self._invalidate()

    def handle_message_region_mouse_event(self, event: Any) -> None:
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self.scroll_message_region_line_down()
            return None
        if event.event_type == MouseEventType.SCROLL_UP:
            self.scroll_message_region_line_up()
            return None
        return None

    def _build_application(self) -> Application:
        self._message_control = FormattedTextControl(self._message_region_fragments)
        self._turn_status_control = FormattedTextControl(self._current_turn_status_text)
        self._status_bar_control = FormattedTextControl(self._status_bar_text)
        self._message_region_container = _MessageScrollablePane(
            Window(
                self._message_control,
                wrap_lines=True,
                always_hide_cursor=True,
            ),
            follow_latest=lambda: self._message_region_follow_latest,
            set_follow_latest=self._set_message_region_follow_latest,
            show_scrollbar=True,
            display_arrows=False,
        )
        input_buffer_region = VSplit(
            [
                Window(
                    FormattedTextControl(lambda: HTML("<b>&gt;</b> ")),
                    width=2,
                    dont_extend_width=True,
                    always_hide_cursor=True,
                ),
                Window(
                    BufferControl(buffer=self._input_buffer),
                    height=self._input_region_dimension,
                    wrap_lines=True,
                ),
            ],
            height=self._input_region_dimension,
        )
        self._input_region = HSplit(
            [
                Window(height=1, char="-", always_hide_cursor=True),
                input_buffer_region,
                Window(height=1, char="-", always_hide_cursor=True),
            ],
            height=self._input_shell_dimension,
        )
        root = HSplit(
            [
                self._message_region_container,
                Window(
                    height=self._turn_status_spacer_height,
                    dont_extend_height=True,
                    always_hide_cursor=True,
                ),
                Window(
                    self._turn_status_control,
                    height=1,
                    dont_extend_height=True,
                    always_hide_cursor=True,
                ),
                self._input_region,
                Window(
                    self._status_bar_control,
                    height=1,
                    dont_extend_height=True,
                    always_hide_cursor=True,
                ),
            ]
        )
        return Application(
            layout=Layout(root, focused_element=self._input_buffer),
            key_bindings=_key_bindings(
                self._handle_prompt_enter_event,
                self.handle_history_or_cursor_up,
                self.handle_history_or_cursor_down,
                self.handle_interrupt_event,
                self.insert_input_newline,
                self.scroll_message_region_up,
                self.scroll_message_region_down,
            ),
            full_screen=True,
            mouse_support=True,
            input=self._prompt_toolkit_input,
            output=self._prompt_toolkit_output,
        )

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
        if not text.strip():
            event.current_buffer.reset()
            if event.current_buffer is not self._input_buffer:
                self._input_buffer.reset()
            self._reset_input_region_height()
            return
        if not self._input_enabled:
            return
        self._history.add(text)
        event.current_buffer.reset()
        if event.current_buffer is not self._input_buffer:
            self._input_buffer.reset()
        self._reset_input_region_height()
        self._dispatch(controller, text)

    def _append(self, message: str) -> None:
        self._messages.append(message)
        self._follow_latest_message_if_needed()
        self._mark_dirty()

    def _append_model_delta(self, model_call_id: str, text: str) -> None:
        if not text:
            return
        if model_call_id in self._model_message_indexes:
            index = self._model_message_indexes[model_call_id]
            self._messages[index] = f"{self._messages[index]}{text}"
        else:
            self._model_message_indexes[model_call_id] = len(self._messages)
            self._messages.append(_format_assistant_message(text))
        self._last_stream_flush_at = monotonic()
        self._follow_latest_message_if_needed()
        self._mark_dirty()

    def _replace_or_append_model_output(self, model_call_id: str, text: str) -> None:
        message = _format_assistant_message(text)
        if model_call_id in self._model_message_indexes:
            self._messages[self._model_message_indexes[model_call_id]] = message
            self._follow_latest_message_if_needed()
            self._mark_dirty()
            return
        self._append(message)

    def _append_or_replace_tool_block(self, payload: dict) -> None:
        self._append(_format_tool_block(payload))

    def flush_pending_model_output(self, *, force: bool = False) -> bool:
        if not self._render_dirty:
            return False
        self._render_dirty = False
        return self._invalidate()

    async def flush_pending_model_output_in_terminal(self) -> bool:
        await asyncio.sleep(0)
        return self.flush_pending_model_output(force=True)

    def _reset_turn_local_model_state(self) -> None:
        self._model_message_indexes.clear()

    def _bottom_toolbar(self) -> str:
        if self._turn_status is None:
            return self._status_bar
        return f"{self._turn_status} | {self._status_bar}"

    def _message_region_text(self) -> str:
        return "\n".join(self._messages)

    def _message_region_fragments(self) -> list[tuple[str, str, Callable[[Any], None]]]:
        text = self._message_region_text()
        if not text:
            text = " "
        return [("", text, self.handle_message_region_mouse_event)]

    def _current_turn_status_text(self) -> str:
        return self._turn_status or ""

    def _status_bar_text(self) -> str:
        return self._status_bar

    def _message_region_is_scrollable(self) -> bool:
        return isinstance(self._message_region_container, ScrollablePane)

    def _message_region_scroll_offset(self) -> int:
        return self._message_region_container.vertical_scroll

    def _message_region_following_latest(self) -> bool:
        return self._message_region_follow_latest

    def _set_message_region_follow_latest(self, value: bool) -> None:
        self._message_region_follow_latest = value

    def _input_region_height(self) -> int:
        return self._input_visible_lines

    def _input_region_dimension(self) -> Dimension:
        return Dimension.exact(self._input_region_height())

    def _input_shell_height(self) -> int:
        return self._input_region_height() + 2

    def _input_shell_dimension(self) -> Dimension:
        return Dimension.exact(self._input_shell_height())

    def _turn_status_spacer_height(self) -> int:
        return 1

    def _sync_input_region_height(self) -> None:
        line_count = self._input_buffer.text.count("\n") + 1
        self._input_visible_lines = min(5, max(1, line_count))
        self._follow_latest_message_if_needed()
        self._invalidate()

    def _reset_input_region_height(self) -> None:
        self._input_visible_lines = 1
        self._follow_latest_message_if_needed()
        self._invalidate()

    def _follow_latest_message_if_needed(self) -> None:
        # The concrete scroll offset depends on the rendered viewport height.
        # Clamp it in _MessageScrollablePane.write_to_screen().
        return

    def _input_cursor_is_at_end(self) -> bool:
        return self._input_buffer.cursor_position == len(self._input_buffer.text)

    def _replace_input_from_history(self, value: str | None) -> None:
        self._input_buffer.text = value or ""
        self._input_buffer.cursor_position = len(self._input_buffer.text)
        self._sync_input_region_height()

    def _handle_input_text_changed(self, _event: object) -> None:
        self._sync_input_region_height()

    def _handle_prompt_enter_event(self, event: Any) -> None:
        if self._active_controller is None:
            return
        self.handle_prompt_enter(self._active_controller, event)

    def _start_prompt_runtime_tasks(self, controller: object) -> None:
        try:
            app = get_app()
        except Exception:
            app = self._application
        setattr(controller, "wakeup_callback", self._invalidate)
        app.create_background_task(self._drain_prompt_runtime(controller, app))

    async def _drain_prompt_runtime(self, controller: object, app: object) -> None:
        while not self._closed:
            self._drain_once(controller)
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

    def _invalidate(self) -> bool:
        try:
            self._application.invalidate()
        except Exception:
            return False
        return True

    def _mark_dirty(self) -> bool:
        self._render_dirty = True
        return self._invalidate()

    def _exit_application(self, app: object | None = None) -> None:
        if self._application_exit_requested:
            return
        self._application_exit_requested = True
        try:
            target = app or self._application
            target.exit(result="")
        except Exception:
            pass

    def _print_terminal_summary(self) -> None:
        if self._terminal_summary is None or self._terminal_summary_printed:
            return
        print(self._terminal_summary, file=self._output_stream)
        self._terminal_summary_printed = True

    def _terminal_summary_text(self) -> str | None:
        return self._terminal_summary


def _format_status_bar(snapshot: StatusBarSnapshot) -> str:
    context = "unavailable"
    if (
        snapshot.context_used_tokens is not None
        and snapshot.context_window_tokens is not None
        and snapshot.context_percent is not None
    ):
        context = (
            f"{format_token_count(snapshot.context_used_tokens)} / "
            f"{format_token_count(snapshot.context_window_tokens)} "
            f"({snapshot.context_percent}%)"
        )
    return (
        f"model: {snapshot.model} | "
        f"approval: {snapshot.approval_mode} | "
        f"context: {context} | "
        f"tokens: {format_token_count(snapshot.total_tokens)} used"
    )


def _format_welcome_panel(snapshot: WelcomeSnapshot) -> str:
    lines = [
        f"{snapshot.tool_name} {snapshot.version}",
        f"model: {snapshot.model}",
        f"workspace: {snapshot.workspace_root}",
        f"approval: {snapshot.approval_mode}",
        f"session: {snapshot.session_id_short}",
    ]
    width = max(len(line) for line in lines)
    border = f"+-{'-' * width}-+"
    body = [f"| {line.ljust(width)} |" for line in lines]
    return "\n".join([border, *body, border])


message_block_border = "--------------"


def _format_user_message(message: str) -> str:
    lines = str(message).splitlines() or [""]
    body = [f"> {lines[0]}"]
    body.extend(f"  {line}" for line in lines[1:])
    border = "-" * max(get_cwidth(line) for line in body)
    return "\n".join(["", border, *body, border])


def _format_assistant_message(text: str) -> str:
    return _format_labeled_message("🔮 Assistant", text)


def _format_labeled_message(label: str, text: object) -> str:
    return "\n".join(["", message_block_border, label, "", str(text)])


def _format_terminal_summary(summary: SessionCloseSummary) -> str:
    status = "exit" if summary.status == "closed" else summary.status
    lines = [f"session {summary.session_id} {status}."]
    lines.append(f"trace: debug-agent trace {summary.session_id}")
    return "\n".join(lines)


def _format_tool_block(payload: dict) -> str:
    metadata = payload.get("metadata", {})
    tool_name = metadata.get("tool_name") or payload.get("name") or "unknown"
    status = payload.get("status") or "unknown"
    preview = payload.get("preview")
    if preview is not None and status == "result":
        lines = _indented_preview_lines(preview)
        if preview.artifact_ids:
            lines.append(f"    artifacts: {', '.join(preview.artifact_ids)}")
        return "\n".join(lines)
    lines = ["", _format_tool_summary(tool_name, status, metadata)]
    if preview is not None:
        lines.extend(_indented_preview_lines(preview))
        if preview.artifact_ids:
            lines.append(f"    artifacts: {', '.join(preview.artifact_ids)}")
    return "\n".join(lines)


def _format_tool_summary(tool_name: str, status: object, metadata: dict) -> str:
    indicator = "🟢" if str(status) in {"ok", "completed"} else "🔴"
    duration = _format_duration(metadata.get("duration_ms"))
    suffix = "" if duration is None else f" ({duration})"
    return f"{indicator} {tool_name}{suffix}"


def _indented_preview_lines(preview: object) -> list[str]:
    return [f"    {line}" for line in _preview_lines(preview)]


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


def _key_bindings(
    on_enter: Callable[[Any], None] | None = None,
    on_history_or_cursor_up: Callable[[Any], None] | None = None,
    on_history_or_cursor_down: Callable[[Any], None] | None = None,
    on_interrupt: Callable[[Any], None] | None = None,
    on_insert_newline: Callable[[], None] | None = None,
    on_page_up: Callable[[], None] | None = None,
    on_page_down: Callable[[], None] | None = None,
) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("c-j")
    def _(event) -> None:
        if on_insert_newline is None:
            event.current_buffer.insert_text("\n")
            return
        on_insert_newline()

    @bindings.add("enter")
    def _(event) -> None:
        if on_enter is None:
            event.current_buffer.validate_and_handle()
            return
        on_enter(event)

    @bindings.add("up")
    def _(event) -> None:
        if on_history_or_cursor_up is not None:
            on_history_or_cursor_up(event)

    @bindings.add("down")
    def _(event) -> None:
        if on_history_or_cursor_down is not None:
            on_history_or_cursor_down(event)

    @bindings.add("pageup")
    def _(event) -> None:
        if on_page_up is not None:
            on_page_up()

    @bindings.add("pagedown")
    def _(event) -> None:
        if on_page_down is not None:
            on_page_down()

    @bindings.add("c-c")
    def _(event) -> None:
        if on_interrupt is not None:
            on_interrupt(event)

    try:
        bindings.add("s-enter", eager=True)(
            lambda event: event.current_buffer.insert_text("\n")
        )
    except ValueError:
        pass

    return bindings

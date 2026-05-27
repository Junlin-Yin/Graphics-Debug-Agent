from __future__ import annotations

import asyncio
import inspect
import io

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from debug_agent.cli.repl_view import (
    ReplViewEvent,
    SessionCloseSummary,
    StatusBarSnapshot,
    ToolResultPreview,
    WelcomeSnapshot,
)


def _prompt_toolkit_view(**kwargs):
    from prompt_toolkit.output import DummyOutput

    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    return PromptToolkitReplView(output=DummyOutput(), **kwargs)


class _FakePromptEvent:
    def __init__(self, text: str) -> None:
        self.current_buffer = self
        self.text = text
        self.reset_called = False
        self.app = self
        self.exited = False

    def reset(self) -> None:
        self.reset_called = True
        self.text = ""

    def exit(self, result: str = "") -> None:
        self.exited = True


class _RaisingExitApp:
    def __init__(self) -> None:
        self.exit_calls = 0

    def exit(self, result: str = "") -> None:
        self.exit_calls += 1
        if self.exit_calls > 1:
            raise Exception("Return value already set. Application.exit() failed.")


class _FakeRunApp(_RaisingExitApp):
    def __init__(self, on_run) -> None:
        super().__init__()
        self.on_run = on_run
        self.invalidations = 0

    def run(self, pre_run=None):
        self.on_run()

    def invalidate(self) -> None:
        self.invalidations += 1


class _FakeKeyEvent:
    def __init__(self, buffer: object, app: object | None = None) -> None:
        self.current_buffer = buffer
        self.app = app or _RaisingExitApp()


class _FakeMouseEvent:
    def __init__(self, event_type: MouseEventType) -> None:
        self.event_type = event_type
        self.button = MouseButton.NONE


def _render_message_viewport(view: object, *, width: int = 80, height: int = 5) -> str:
    screen = Screen()
    with set_app(view._application):
        view._message_region_container.write_to_screen(
            screen,
            MouseHandlers(),
            WritePosition(xpos=0, ypos=0, width=width, height=height),
            "",
            False,
            None,
        )
    return "\n".join(
        "".join(screen.data_buffer[y][x].char for x in range(width - 1)).rstrip()
        for y in range(height)
    )


def _render_input_region(view: object, *, width: int = 80) -> str:
    height = view._input_shell_height()
    screen = Screen()

    class _DiscardedTask:
        def add_done_callback(self, callback):
            return None

        def cancel(self):
            return None

    def _discard_background_task(coroutine):
        coroutine.close()
        return _DiscardedTask()

    view._application.create_background_task = _discard_background_task
    with set_app(view._application):
        view._input_region.write_to_screen(
            screen,
            MouseHandlers(),
            WritePosition(xpos=0, ypos=0, width=width, height=height),
            "",
            False,
            None,
        )
    return "\n".join(
        "".join(screen.data_buffer[y][x].char for x in range(width - 1)).rstrip()
        for y in range(height)
    )


def _render_message_viewport_lines(
    view: object, *, width: int = 80, height: int = 5
) -> list[str]:
    return _render_message_viewport(view, width=width, height=height).splitlines()


def test_prompt_toolkit_view_renders_welcome_messages_status_and_close_summary() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.show_welcome(
        WelcomeSnapshot(
            tool_name="debug-agent",
            version="1.2.3",
            model="fake-model",
            workspace_root="/repo",
            approval_mode="normal",
            session_id_short="sess_123",
        )
    )
    view.update_status_bar(
        StatusBarSnapshot(
            input_tokens=999,
            output_tokens=1_000,
            total_tokens=1_999,
            approval_mode="normal",
            model="fake-model",
            context_used_tokens=1250,
            context_window_tokens=5000,
            context_percent=25,
        )
    )
    view.append_user_message("hello")
    view.append_view_event(
        ReplViewEvent(kind="model_markdown_final", payload={"text": "**answer**"})
    )
    view.append_view_event(
        ReplViewEvent(kind="system_message", payload={"message": "status ok"})
    )
    view.show_error("failed")
    view.set_turn_status(1, "completed", 2)
    view.show_session_closed(
        SessionCloseSummary(
            session_id="sess_full",
            status="closed",
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
            error_type=None,
        )
    )

    rendered = view.rendered_text()
    assert "debug-agent 1.2.3" in rendered
    assert "model: fake-model" in rendered
    assert "workspace: /repo" in rendered
    assert "\n-------\n> hello\n-------" in rendered
    assert "answer" in rendered
    assert "🤖 System\n\nstatus ok" in rendered
    assert "❌ Error\n\nfailed" in rendered
    assert "turn 1: completed 2s" in rendered
    assert (
        "model: fake-model | approval: normal | context: 1.2k / 5.0k (25%) | "
        "tokens: 2.0k used"
    ) in rendered
    assert "session sess_full" not in rendered


def test_prompt_toolkit_view_status_bar_renders_initial_zero_values() -> None:
    view = _prompt_toolkit_view()

    view.update_status_bar(
        StatusBarSnapshot(
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            approval_mode="normal",
            model="fake-model",
        )
    )

    assert view._status_bar_text() == (
        "model: fake-model | approval: normal | context: 0 | tokens: 0"
    )


def test_prompt_toolkit_view_status_bar_preserves_estimated_context_and_usage_format() -> None:
    view = _prompt_toolkit_view()

    view.update_status_bar(
        StatusBarSnapshot(
            input_tokens=900,
            output_tokens=350,
            total_tokens=1250,
            approval_mode="normal",
            model="fake-model",
            context_used_tokens=1250,
            context_window_tokens=5000,
            context_percent=25,
        )
    )

    assert view._status_bar_text() == (
        "model: fake-model | approval: normal | context: 1.2k / 5.0k (25%) | "
        "tokens: 1.2k used"
    )


def test_prompt_toolkit_view_normal_submit_keeps_prompt_active() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        submitted: list[str]

        def __init__(self) -> None:
            self.submitted = []

        def on_submit(self, text: str) -> None:
            self.submitted.append(text)

    view = _prompt_toolkit_view()
    controller = Controller()
    event = _FakePromptEvent("hello")

    view.handle_prompt_enter(controller, event)

    assert controller.submitted == ["hello"]
    assert event.reset_called is True
    assert event.exited is False


def test_prompt_toolkit_view_keeps_turn_status_out_of_message_list() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_user_message("hello")
    view.set_turn_status(2, "running", 1)
    view.set_turn_status(2, "running", 2)
    view.set_turn_status(2, "running", 3)

    rendered = view.rendered_text()
    assert rendered.count("turn 2:") == 1
    assert "turn 2: running 1s" not in rendered
    assert "turn 2: running 2s" not in rendered
    assert "turn 2: running 3s" in rendered


def test_prompt_toolkit_view_invalidates_toolbar_only_when_status_text_changes() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class FakeApp:
        def __init__(self) -> None:
            self.invalidations = 0

        def invalidate(self) -> None:
            self.invalidations += 1

    app = FakeApp()
    view = _prompt_toolkit_view()

    view.set_turn_status(2, "running", 1)
    view.invalidate_toolbar_if_changed(app)
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hello"},
        )
    )
    view.invalidate_toolbar_if_changed(app)
    view.set_turn_status(2, "running", 2)
    view.invalidate_toolbar_if_changed(app)

    assert app.invalidations == 2


def test_prompt_toolkit_view_renders_tool_blocks_separately() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="tool_block",
            payload={
                "status": "ok",
                "metadata": {"tool_name": "read_file"},
                "preview": ToolResultPreview(
                    text="> line 1",
                    truncated=False,
                    shown_lines=1,
                    total_lines=1,
                    artifact_ids=["art_1"],
                ),
            },
        )
    )

    rendered = view.rendered_text()
    assert "🟢 read_file" in rendered
    assert "tool: read_file" not in rendered
    assert "status: ok" not in rendered
    assert "    > line 1" in rendered
    assert "artifacts: art_1" in rendered


def test_prompt_toolkit_view_appends_tool_completion_and_result_blocks() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="tool_block",
            payload={
                "name": "git_status",
                "status": "ok",
                "metadata": {
                    "tool_call_id": "tool_1",
                    "model_call_id": "model_1",
                    "tool_name": "git_status",
                    "duration_ms": 1200,
                },
            },
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="tool_block",
            payload={
                "name": "git_status",
                "status": "result",
                "metadata": {
                    "tool_call_id": "tool_1",
                    "model_call_id": "model_1",
                    "tool_name": "git_status",
                    "duration_ms": 1200,
                },
                "preview": ToolResultPreview(
                    text="> M file.py",
                    truncated=False,
                    shown_lines=1,
                    total_lines=1,
                    artifact_ids=[],
                ),
            },
        )
    )

    rendered = view.rendered_text()
    assert rendered.count("🟢 git_status (1.2s)") == 1
    assert "tool: git_status" not in rendered
    assert "tool: unknown" not in rendered
    assert "status: running" not in rendered
    assert "status: ok" not in rendered
    assert "status: result" not in rendered
    assert "duration: 1.2s" not in rendered
    assert "    > M file.py" in rendered


def test_prompt_toolkit_view_truncated_tool_result_includes_detail_line() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="tool_block",
            payload={
                "name": "git_status",
                "status": "result",
                "metadata": {"tool_name": "git_status"},
                "preview": ToolResultPreview(
                    text="> one\n> two\n> ...",
                    truncated=True,
                    shown_lines=2,
                    total_lines=5,
                    artifact_ids=[],
                ),
            },
        )
    )

    rendered = view.rendered_text()
    assert "    > ..." in rendered
    assert "    > [truncated: showing 2 of 5 lines]" in rendered


def test_prompt_toolkit_view_formats_multiline_user_message_like_shell_block() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_user_message("line 1\nline 2\nline 3")

    rendered = view._message_region_text()
    assert "\n--------\n> line 1\n  line 2\n  line 3\n--------" in rendered


def test_prompt_toolkit_view_user_message_borders_cover_prompt_text_only() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_user_message("short\nmuch longer line")

    rendered = view._message_region_text().splitlines()

    assert rendered[1] == "-" * len("  much longer line")
    assert rendered[2] == "> short"
    assert rendered[3] == "  much longer line"
    assert rendered[4] == "-" * len("  much longer line")


def test_prompt_toolkit_view_user_message_border_uses_terminal_cell_width() -> None:
    from prompt_toolkit.utils import get_cwidth

    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_user_message("你好，你能为我做什么？")

    rendered = view._message_region_text().splitlines()
    expected_width = get_cwidth("> 你好，你能为我做什么？")

    assert rendered[1] == "-" * expected_width
    assert rendered[2] == "> 你好，你能为我做什么？"
    assert rendered[3] == "-" * expected_width


def test_prompt_toolkit_view_formats_failed_tool_completion_with_red_indicator() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="tool_block",
            payload={
                "name": "run_tests",
                "status": "failed",
                "metadata": {
                    "tool_name": "run_tests",
                    "duration_ms": 100,
                },
            },
        )
    )

    rendered = view.rendered_text()
    assert "🔴 run_tests (0.1s)" in rendered
    assert "status: failed" not in rendered


def test_prompt_toolkit_view_formats_system_and_error_blocks_with_headers() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(kind="system_message", payload={"message": "status ok"})
    )
    view.append_view_event(
        ReplViewEvent(kind="error_message", payload={"message": "bad stream"})
    )
    view.show_error("failed")

    rendered = view.rendered_text()
    assert "🤖 System\n\nstatus ok" in rendered
    assert rendered.count("❌ Error") == 2
    assert "❌ Error\n\nbad stream" in rendered
    assert "❌ Error\n\nfailed" in rendered
    assert "system: status ok" not in rendered
    assert "error: failed" not in rendered


def test_prompt_toolkit_view_does_not_clear_or_replace_tool_blocks(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="tool_block",
            payload={
                "name": "git_status",
                "status": "ok",
                "metadata": {
                    "tool_call_id": "tool_1",
                    "tool_name": "git_status",
                    "duration_ms": 1200,
                },
            },
        )
    )

    assert "🟢 git_status (1.2s)" in view.rendered_text()


def test_prompt_toolkit_view_keeps_large_model_text_plain() -> None:
    from debug_agent.cli.prompt_toolkit_view import (
        PromptToolkitReplView,
        max_markdown_render_chars,
    )

    view = _prompt_toolkit_view()
    large_text = "#" * (max_markdown_render_chars + 1)

    view.append_view_event(
        ReplViewEvent(kind="model_markdown_final", payload={"text": large_text})
    )

    assert large_text in view.rendered_text()


def test_prompt_toolkit_view_updates_one_model_block_for_streaming_deltas() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "partial"},
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": " text"},
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="model_markdown_final",
            payload={"model_call_id": "model_1", "text": "**final answer**"},
        )
    )

    rendered = view.rendered_text()
    assert rendered.count("🔮 Assistant") == 1
    assert "partial text" not in rendered
    assert "final answer" in rendered


def test_prompt_toolkit_view_reused_model_call_id_after_user_message_starts_new_block() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_user_message("first")
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "one"},
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="model_markdown_final",
            payload={"model_call_id": "model_1", "text": "one"},
        )
    )
    view.append_user_message("second")
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "two"},
        )
    )

    rendered = view.rendered_text()
    assert "> first" in rendered
    assert "one" in rendered
    assert "> second" in rendered
    assert "two" in rendered
    assert rendered.index("> second") < rendered.rindex("🔮 Assistant")


def test_prompt_toolkit_view_streaming_delta_updates_layout_message_model_only(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hel"},
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "lo"},
        )
    )

    view.flush_pending_model_output(force=True)

    assert "hello" in view.rendered_text()
    assert view._message_region_text().count("🔮 Assistant") == 1


def test_prompt_toolkit_view_streaming_redraw_preserves_bottom_status_region(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    view.set_turn_status(1, "running", 2)
    view.update_status_bar(
        StatusBarSnapshot(
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            approval_mode="normal",
            model="fake-model",
            context_used_tokens=1250,
            context_window_tokens=5000,
            context_percent=25,
        )
    )

    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hello"},
        )
    )
    view.flush_pending_model_output(force=True)

    assert view._current_turn_status_text() == "turn 1: running 2s"
    assert view._status_bar_text() == (
        "model: fake-model | approval: normal | context: 1.2k / 5.0k (25%) | "
        "tokens: 0"
    )


def test_prompt_toolkit_view_streaming_flush_invalidates_application() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    invalidations = 0

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    view._application.invalidate = invalidate
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hello"},
        )
    )

    flushed = asyncio.run(view.flush_pending_model_output_in_terminal())

    assert flushed is True
    assert invalidations >= 1


def test_prompt_toolkit_view_streaming_redraw_does_not_emit_terminal_writes(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hel"},
        )
    )
    view.flush_pending_model_output(force=True)
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "lo"},
        )
    )
    view.flush_pending_model_output(force=True)

    assert "hello" in view.rendered_text()


def test_prompt_toolkit_view_disables_prompt_buffer_edits_while_turn_runs() -> None:
    from prompt_toolkit.buffer import EditReadOnlyBuffer

    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.set_input_enabled(False)

    assert view._input_buffer.read_only() is True
    with pytest.raises(EditReadOnlyBuffer):
        view._input_buffer.insert_text("new prompt")

    view.set_input_enabled(True)

    assert view._input_buffer.read_only() is False


def test_prompt_toolkit_view_streaming_flush_reports_when_redraw_is_needed(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    assert view.flush_pending_model_output(force=True) is False
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hello"},
        )
    )

    assert view.flush_pending_model_output(force=True) is True


def test_prompt_toolkit_view_streaming_delta_keeps_literal_markdown_in_layout(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={
                "model_call_id": "model_1",
                "text": "| 文件 | 状态 |\n| --- | --- |\n- `a.py`\n",
            },
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "- `b.py`\n"},
        )
    )
    view.flush_pending_model_output(force=True)

    assert view._message_region_text().splitlines()[-4:] == [
        "| 文件 | 状态 |",
        "| --- | --- |",
        "- `a.py`",
        "- `b.py`",
    ]


def test_prompt_toolkit_view_replaces_final_markdown_in_layout_without_terminal_writes(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "**"},
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "final"},
        )
    )
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "**"},
        )
    )
    view.flush_pending_model_output(force=True)
    view.append_view_event(
        ReplViewEvent(
            kind="model_markdown_final",
            payload={"model_call_id": "model_1", "text": "**final**"},
        )
    )

    assert "🔮 Assistant\n\nfinal" in view.rendered_text()


def test_prompt_toolkit_view_input_history_and_multiline_submission() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    submitted: list[str] = []
    view = _prompt_toolkit_view()

    view.submit_input("line 1\nline 2", lambda text: submitted.append(text))
    view.submit_input("   ", lambda text: submitted.append(text))
    view.submit_input("/status", lambda text: submitted.append(text))

    assert submitted == ["line 1\nline 2", "/status"]
    assert view.history_previous() == "/status"
    assert view.history_previous() == "line 1\nline 2"
    assert view.history_next() == "/status"
    assert view.history_next() is None


def test_prompt_toolkit_view_history_navigation_replaces_input_buffer() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.submit_input("first", lambda text: None)
    view.submit_input("second", lambda text: None)

    view.apply_history_previous()
    assert view._input_buffer.text == "second"
    assert view._input_buffer.cursor_position == len("second")

    view.apply_history_previous()
    assert view._input_buffer.text == "first"
    assert view._input_buffer.cursor_position == len("first")

    view.apply_history_next()
    assert view._input_buffer.text == "second"
    assert view._input_buffer.cursor_position == len("second")

    view.apply_history_next()
    assert view._input_buffer.text == ""
    assert view._input_buffer.cursor_position == 0


def test_prompt_toolkit_view_history_navigation_only_at_buffer_end() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    view.submit_input("history item", lambda text: None)
    view._input_buffer.text = "line 1\nline 2"
    view._input_buffer.cursor_position = 0

    view.handle_history_or_cursor_up(_FakeKeyEvent(view._input_buffer))

    assert view._input_buffer.text == "line 1\nline 2"
    assert view._input_buffer.cursor_position != len(view._input_buffer.text)

    view._input_buffer.cursor_position = len(view._input_buffer.text)
    view.handle_history_or_cursor_up(_FakeKeyEvent(view._input_buffer))

    assert view._input_buffer.text == "history item"
    assert view._input_buffer.cursor_position == len("history item")


def test_prompt_toolkit_view_down_moves_cursor_until_buffer_end_then_history() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    view.submit_input("first", lambda text: None)
    view.submit_input("second", lambda text: None)
    view.apply_history_previous()
    view.apply_history_previous()
    view._input_buffer.text = "line 1\nline 2"
    view._input_buffer.cursor_position = 0

    view.handle_history_or_cursor_down(_FakeKeyEvent(view._input_buffer))

    assert view._input_buffer.text == "line 1\nline 2"
    assert view._input_buffer.cursor_position != 0

    view._input_buffer.cursor_position = len(view._input_buffer.text)
    view.handle_history_or_cursor_down(_FakeKeyEvent(view._input_buffer))

    assert view._input_buffer.text == "second"
    assert view._input_buffer.cursor_position == len("second")


def test_prompt_toolkit_view_ctrl_j_expands_visible_input_region_to_five_lines() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    assert view._input_region_height() == 1
    for _ in range(6):
        view.insert_input_newline()

    assert view._input_buffer.text == "\n\n\n\n\n\n"
    assert view._input_region_height() == 5


def test_prompt_toolkit_view_input_region_dimension_owns_current_height() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    assert view._input_region_dimension().min == 1
    assert view._input_region_dimension().preferred == 1
    assert view._input_region_dimension().max == 1

    view.insert_input_newline()
    view.insert_input_newline()

    assert view._input_region_height() == 3
    assert view._input_region_dimension().min == 3
    assert view._input_region_dimension().preferred == 3
    assert view._input_region_dimension().max == 3


def test_prompt_toolkit_view_submit_resets_input_region_to_one_line() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        def on_submit(self, text: str) -> None:
            pass

    view = _prompt_toolkit_view()
    view.insert_input_newline()
    view.insert_input_newline()

    assert view._input_region_height() == 3

    view.handle_prompt_enter(Controller(), _FakePromptEvent("line 1\nline 2\nline 3"))

    assert view._input_region_height() == 1
    assert view._input_region_dimension().min == 1
    assert view._input_region_dimension().preferred == 1
    assert view._input_region_dimension().max == 1


def test_prompt_toolkit_view_input_borders_do_not_count_toward_buffer_height() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    assert view._input_region_height() == 1
    assert view._input_shell_height() == 3

    view.insert_input_newline()
    view.insert_input_newline()

    assert view._input_region_height() == 3
    assert view._input_shell_height() == 5


def test_prompt_toolkit_view_backspace_line_removal_shrinks_input_region() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    view._input_buffer.text = "line 1\nline 2\nline 3"
    view._input_buffer.cursor_position = len(view._input_buffer.text)

    assert view._input_region_height() == 3

    view._input_buffer.text = "line 1"
    view._input_buffer.cursor_position = len(view._input_buffer.text)

    assert view._input_region_height() == 1


def test_prompt_toolkit_view_input_height_changes_keep_latest_message_visible() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    for index in range(8):
        view.append_user_message(f"message {index}")

    assert view._message_region_following_latest() is True

    view.insert_input_newline()
    rendered = _render_message_viewport(view, height=4)

    assert view._message_region_following_latest() is True
    assert "message 7" in rendered


def test_prompt_toolkit_view_appended_messages_follow_latest_until_user_scrolls_up() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    for index in range(8):
        view.append_user_message(f"message {index}")

    assert view._message_region_following_latest() is True
    assert "message 7" in _render_message_viewport(view, height=4)

    view.scroll_message_region_up()
    scrolled_rendered = _render_message_viewport(view, height=4)
    assert view._message_region_following_latest() is False
    scrolled_away_offset = view._message_region_scroll_offset()

    view.append_user_message("message 8")

    assert view._message_region_following_latest() is False
    assert view._message_region_scroll_offset() == scrolled_away_offset
    assert _render_message_viewport(view, height=4) == scrolled_rendered


def test_prompt_toolkit_view_scroll_down_from_history_does_not_jump_to_latest() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    for index in range(20):
        view.append_user_message(f"message {index}")

    assert "message 19" in _render_message_viewport(view, height=4)

    view.scroll_message_region_up()
    view.scroll_message_region_up()
    historical_viewport = _render_message_viewport(view, height=4)
    assert view._message_region_following_latest() is False

    view.scroll_message_region_down()
    page_down_viewport = _render_message_viewport(view, height=4)

    assert view._message_region_following_latest() is False
    assert page_down_viewport != historical_viewport
    assert "message 19" not in page_down_viewport


def test_prompt_toolkit_view_follow_latest_renders_existing_message_viewport() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.show_welcome(
        WelcomeSnapshot(
            tool_name="debug-agent",
            version="1.2.3",
            model="fake-model",
            workspace_root="/repo",
            approval_mode="normal",
            session_id_short="sess_123",
        )
    )
    view.append_user_message("hello")
    view.append_view_event(
        ReplViewEvent(
            kind="model_markdown_final",
            payload={"model_call_id": "model_1", "text": "answer"},
        )
    )

    rendered = _render_message_viewport(view, height=16)

    assert "debug-agent 1.2.3" in rendered
    assert "> hello" in rendered
    assert "🔮 Assistant" in rendered
    assert "answer" in rendered


def test_prompt_toolkit_view_mouse_scroll_events_scroll_message_region() -> None:
    from debug_agent.cli.prompt_toolkit_view import (
        PromptToolkitReplView,
        message_scroll_step_lines,
    )

    view = _prompt_toolkit_view()

    assert view._message_region_scroll_offset() == 0

    handled = view.handle_message_region_mouse_event(
        _FakeMouseEvent(MouseEventType.SCROLL_DOWN)
    )

    assert handled is None
    assert view._message_region_scroll_offset() == message_scroll_step_lines

    view.handle_message_region_mouse_event(_FakeMouseEvent(MouseEventType.SCROLL_UP))

    assert view._message_region_scroll_offset() == 0


def test_prompt_toolkit_view_page_scroll_uses_larger_step_than_mouse_scroll() -> None:
    from debug_agent.cli.prompt_toolkit_view import (
        PromptToolkitReplView,
        message_scroll_step_lines,
        message_scroll_step_page,
    )

    view = _prompt_toolkit_view()

    assert message_scroll_step_lines == 2
    assert message_scroll_step_page == 10

    view.handle_message_region_mouse_event(_FakeMouseEvent(MouseEventType.SCROLL_DOWN))
    assert view._message_region_scroll_offset() == 2

    view.scroll_message_region_down()
    assert view._message_region_scroll_offset() == 12


def test_prompt_toolkit_view_welcome_panel_uses_ascii_border() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    view.show_welcome(
        WelcomeSnapshot(
            tool_name="debug-agent",
            version="1.2.3",
            model="fake-model",
            workspace_root="/repo",
            approval_mode="normal",
            session_id_short="sess_123",
        )
    )

    lines = view._message_region_text().splitlines()
    assert lines[0].startswith("+")
    assert set(lines[0]) <= {"+", "-"}
    assert lines[-1] == lines[0]
    assert "| debug-agent 1.2.3" in lines[1]


def test_prompt_toolkit_view_turn_status_has_spacer_above_region() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    assert view._turn_status_spacer_height() == 1
    view.set_turn_status(1, "running", 2)
    assert "\n\nturn 1: running 2s" not in view.rendered_text()


def test_prompt_toolkit_view_exit_is_idempotent_after_slash_exit() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        def __init__(self, view: PromptToolkitReplView) -> None:
            self.view = view

        def on_slash_command(self, command: str) -> bool:
            self.view.show_session_closed(
                SessionCloseSummary(
                    session_id="sess_full",
                    status="closed",
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    error_type=None,
                )
            )
            return False

    view = _prompt_toolkit_view()
    app = _RaisingExitApp()
    view._application = app
    event = _FakePromptEvent("/exit")
    event.app = app

    view.handle_prompt_enter(Controller(view), event)

    assert app.exit_calls == 1
    assert "session sess_full" not in view.rendered_text()
    assert view._terminal_summary_text() == (
        "session sess_full exit.\ntrace: debug-agent trace sess_full"
    )


def test_prompt_toolkit_view_ctrl_c_invokes_existing_interrupt_path() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        def __init__(self) -> None:
            self.interrupts = 0

        def on_interrupt(self) -> None:
            self.interrupts += 1

    controller = Controller()
    view = _prompt_toolkit_view()
    view._active_controller = controller
    event = _FakeKeyEvent(view._input_buffer)

    view.handle_interrupt_event(event)

    assert controller.interrupts == 1
    assert event.app.exit_calls == 1


def test_prompt_toolkit_view_ctrl_y_invokes_controller_mode_cycle() -> None:
    from prompt_toolkit.keys import Keys

    from debug_agent.cli.prompt_toolkit_view import _key_bindings

    calls: list[str] = []
    bindings = _key_bindings(on_approval_mode_cycle=lambda event: calls.append("cycle"))
    binding = next(
        binding
        for binding in bindings.bindings
        if tuple(binding.keys) == (Keys.ControlY,)
    )

    binding.handler(_FakeKeyEvent(buffer=None))

    assert calls == ["cycle"]


def test_prompt_toolkit_view_inline_approval_uses_input_lane_not_message_list() -> None:
    view = _prompt_toolkit_view()

    view.begin_inline_approval(
        "=== Approval Request ===\n"
        "Tool: shell_exec\n"
        "Target: git status\n"
        "\n"
        "Allow? [y]once, [a] session, [n] deny"
    )

    rendered_input = _render_input_region(view)
    assert "=== Approval Request ===" in view._approval_prompt_text()
    assert "Tool: shell_exec" in view._approval_prompt_text()
    assert "Tool: shell_exec" in rendered_input
    assert "Target: git status" in rendered_input
    assert "Allow? [y]once, [a] session, [n] deny" in rendered_input
    assert "Risk:" not in rendered_input
    assert "Grant scope:" not in rendered_input
    assert "approval>" not in view._input_prompt_fragment_text()
    assert view._input_prompt_width() == 0
    assert view._input_entry_height() == 0
    assert view._input_region_height() == view._approval_prompt_height()
    assert view._input_buffer.read_only() is True
    assert "Tool: shell_exec" not in view._message_region_text()

    view.end_inline_approval()

    assert view._approval_prompt_text() == ""
    assert view._input_prompt_fragment_text() == "> "
    assert view._input_prompt_width() == len("> ")
    assert view._input_region_height() == 1
    assert view._input_buffer.read_only() is False


def test_prompt_toolkit_view_approval_key_dispatches_without_buffering() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        def on_submit(self, text: str) -> None:
            self.submitted.append(text)

    controller = Controller()
    view = _prompt_toolkit_view()
    view._active_controller = controller
    view.begin_inline_approval("Allow? [y]once, [a] session, [n] deny")
    event = _FakeKeyEvent(view._input_buffer)

    view.handle_approval_key_event(event, "a")

    assert controller.submitted == ["a"]
    assert view._input_buffer.text == ""


def test_prompt_toolkit_view_approval_keys_type_normally_outside_approval() -> None:
    view = _prompt_toolkit_view()
    event = _FakeKeyEvent(view._input_buffer)

    view.handle_approval_key_event(event, "y")
    view.handle_approval_key_event(event, "a")
    view.handle_approval_key_event(event, "n")

    assert view._input_buffer.text == "yan"


def test_prompt_toolkit_view_streaming_redraw_preserves_prompt_buffer() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()
    view._input_buffer.text = "draft prompt"
    view._input_buffer.cursor_position = len("draft")

    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hello"},
        )
    )
    view.flush_pending_model_output(force=True)

    assert view._input_buffer.text == "draft prompt"
    assert view._input_buffer.cursor_position == len("draft")


def test_prompt_toolkit_view_has_application_layout_regions() -> None:
    from prompt_toolkit.application import Application

    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    assert isinstance(view._application, Application)
    assert view._message_region_text() == ""
    assert view._current_turn_status_text() == ""
    assert view._status_bar_text().startswith("model:")
    assert view._message_region_is_scrollable() is True
    assert view._application.full_screen is True
    assert view._application.mouse_support() is True


def test_prompt_toolkit_view_run_prints_terminal_summary_after_application_exit() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        exit_code = 0

    output = io.StringIO()
    view = _prompt_toolkit_view(output_stream=output)

    def close_session() -> None:
        view.show_session_closed(
            SessionCloseSummary(
                session_id="sess_full",
                status="closed",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                error_type=None,
            )
        )

    app = _FakeRunApp(close_session)
    view._application = app

    exit_code = view.run(Controller())

    assert exit_code == 0
    assert output.getvalue() == (
        "session sess_full exit.\ntrace: debug-agent trace sess_full\n"
    )


def test_prompt_toolkit_view_run_prints_cancelled_terminal_summary() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        exit_code = 1

    output = io.StringIO()
    view = _prompt_toolkit_view(output_stream=output)

    def cancel_session() -> None:
        view.show_session_closed(
            SessionCloseSummary(
                session_id="sess_full",
                status="cancelled",
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                error_type="cancelled",
            )
        )

    app = _FakeRunApp(cancel_session)
    view._application = app

    exit_code = view.run(Controller())

    assert exit_code == 1
    assert output.getvalue() == (
        "session sess_full cancelled.\ntrace: debug-agent trace sess_full\n"
    )


def test_prompt_toolkit_view_page_keys_scroll_message_region() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = _prompt_toolkit_view()

    assert view._message_region_scroll_offset() == 0

    view.scroll_message_region_down()
    assert view._message_region_scroll_offset() > 0

    view.scroll_message_region_up()
    assert view._message_region_scroll_offset() == 0


def test_prompt_toolkit_view_has_no_transcript_streaming_write_path() -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view

    source = inspect.getsource(prompt_toolkit_view.PromptToolkitReplView)

    assert "PromptSession" not in source
    assert "write_raw" not in source
    assert "_write_terminal_text" not in source
    assert "_write_terminal_control" not in source
    assert "print_formatted_text" not in source

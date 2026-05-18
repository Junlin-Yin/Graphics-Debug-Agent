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


def test_prompt_toolkit_view_renders_welcome_messages_status_and_close_summary() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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
            total_tokens=None,
            approval_mode="normal",
            model="fake-model",
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
    assert "you: hello" in rendered
    assert "answer" in rendered
    assert "system: status ok" in rendered
    assert "error: failed" in rendered
    assert "turn 1: completed 2s" in rendered
    assert "tokens: 999 input, 1.0k output, unavailable total" in rendered
    assert "session sess_full" not in rendered


def test_prompt_toolkit_view_normal_submit_keeps_prompt_active() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        submitted: list[str]

        def __init__(self) -> None:
            self.submitted = []

        def on_submit(self, text: str) -> None:
            self.submitted.append(text)

    view = PromptToolkitReplView()
    controller = Controller()
    event = _FakePromptEvent("hello")

    view.handle_prompt_enter(controller, event)

    assert controller.submitted == ["hello"]
    assert event.reset_called is True
    assert event.exited is False


def test_prompt_toolkit_view_keeps_turn_status_out_of_message_list() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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
    view = PromptToolkitReplView()

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

    view = PromptToolkitReplView()

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
    assert "tool: read_file" in rendered
    assert "status: ok" in rendered
    assert "> line 1" in rendered
    assert "artifacts: art_1" in rendered


def test_prompt_toolkit_view_appends_tool_completion_and_result_blocks() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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
    assert rendered.count("tool: git_status") == 1
    assert "tool: unknown" not in rendered
    assert "status: running" not in rendered
    assert "status: ok" in rendered
    assert "status: result" not in rendered
    assert "duration: 1.2s" in rendered
    assert "> M file.py" in rendered


def test_prompt_toolkit_view_truncated_tool_result_includes_detail_line() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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
    assert "> ..." in rendered
    assert "> [truncated: showing 2 of 5 lines]" in rendered


def test_prompt_toolkit_view_does_not_clear_or_replace_tool_blocks(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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

    assert "tool: git_status\nstatus: ok\nduration: 1.2s" in view.rendered_text()


def test_prompt_toolkit_view_keeps_large_model_text_plain() -> None:
    from debug_agent.cli.prompt_toolkit_view import (
        PromptToolkitReplView,
        max_markdown_render_chars,
    )

    view = PromptToolkitReplView()
    large_text = "#" * (max_markdown_render_chars + 1)

    view.append_view_event(
        ReplViewEvent(kind="model_markdown_final", payload={"text": large_text})
    )

    assert large_text in view.rendered_text()


def test_prompt_toolkit_view_updates_one_model_block_for_streaming_deltas() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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
    assert rendered.count("assistant:") == 1
    assert "partial text" not in rendered
    assert "final answer" in rendered


def test_prompt_toolkit_view_reused_model_call_id_after_user_message_starts_new_block() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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
    assert "you: first" in rendered
    assert "assistant: one" in rendered
    assert "you: second" in rendered
    assert "assistant: two" in rendered
    assert rendered.index("you: second") < rendered.index("assistant: two")


def test_prompt_toolkit_view_streaming_delta_updates_layout_message_model_only(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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

    assert "assistant: hello" in view.rendered_text()
    assert view._message_region_text().count("assistant:") == 1


def test_prompt_toolkit_view_streaming_redraw_preserves_bottom_status_region(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()
    view.set_turn_status(1, "running", 2)
    view.update_status_bar(
        StatusBarSnapshot(
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            approval_mode="normal",
            model="fake-model",
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
        "tokens: unavailable input, unavailable output, unavailable total | "
        "mode: normal | model: fake-model"
    )


def test_prompt_toolkit_view_streaming_flush_invalidates_application() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()
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

    view = PromptToolkitReplView()

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

    assert "assistant: hello" in view.rendered_text()


def test_prompt_toolkit_view_disables_prompt_buffer_edits_while_turn_runs() -> None:
    from prompt_toolkit.buffer import EditReadOnlyBuffer

    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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

    view = PromptToolkitReplView()

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

    view = PromptToolkitReplView()

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
        "assistant: | 文件 | 状态 |",
        "| --- | --- |",
        "- `a.py`",
        "- `b.py`",
    ]


def test_prompt_toolkit_view_replaces_final_markdown_in_layout_without_terminal_writes(
    monkeypatch,
) -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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

    assert "assistant: final" in view.rendered_text()


def test_prompt_toolkit_view_input_history_and_multiline_submission() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    submitted: list[str] = []
    view = PromptToolkitReplView()

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

    view = PromptToolkitReplView()

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

    view = PromptToolkitReplView()
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

    view = PromptToolkitReplView()
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

    view = PromptToolkitReplView()

    assert view._input_region_height() == 1
    for _ in range(6):
        view.insert_input_newline()

    assert view._input_buffer.text == "\n\n\n\n\n\n"
    assert view._input_region_height() == 5


def test_prompt_toolkit_view_input_region_dimension_owns_current_height() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

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

    view = PromptToolkitReplView()
    view.insert_input_newline()
    view.insert_input_newline()

    assert view._input_region_height() == 3

    view.handle_prompt_enter(Controller(), _FakePromptEvent("line 1\nline 2\nline 3"))

    assert view._input_region_height() == 1
    assert view._input_region_dimension().min == 1
    assert view._input_region_dimension().preferred == 1
    assert view._input_region_dimension().max == 1


def test_prompt_toolkit_view_input_height_changes_keep_latest_message_visible() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()
    for index in range(8):
        view.append_user_message(f"message {index}")

    assert view._message_region_following_latest() is True

    view.insert_input_newline()
    rendered = _render_message_viewport(view, height=4)

    assert view._message_region_following_latest() is True
    assert "message 7" in rendered


def test_prompt_toolkit_view_appended_messages_follow_latest_until_user_scrolls_up() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()
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

    view = PromptToolkitReplView()
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

    view = PromptToolkitReplView()

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

    rendered = _render_message_viewport(view, height=8)

    assert "debug-agent 1.2.3" in rendered
    assert "you: hello" in rendered
    assert "assistant: answer" in rendered


def test_prompt_toolkit_view_mouse_scroll_events_scroll_message_region() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

    assert view._message_region_scroll_offset() == 0

    handled = view.handle_message_region_mouse_event(
        _FakeMouseEvent(MouseEventType.SCROLL_DOWN)
    )

    assert handled is None
    assert view._message_region_scroll_offset() > 0

    view.handle_message_region_mouse_event(_FakeMouseEvent(MouseEventType.SCROLL_UP))

    assert view._message_region_scroll_offset() == 0


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

    view = PromptToolkitReplView()
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
    view = PromptToolkitReplView()
    view._active_controller = controller
    event = _FakeKeyEvent(view._input_buffer)

    view.handle_interrupt_event(event)

    assert controller.interrupts == 1
    assert event.app.exit_calls == 1


def test_prompt_toolkit_view_streaming_redraw_preserves_prompt_buffer() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()
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

    view = PromptToolkitReplView()

    assert isinstance(view._application, Application)
    assert view._message_region_text() == ""
    assert view._current_turn_status_text() == ""
    assert view._status_bar_text().startswith("tokens:")
    assert view._message_region_is_scrollable() is True
    assert view._application.full_screen is True
    assert view._application.mouse_support() is True


def test_prompt_toolkit_view_run_prints_terminal_summary_after_application_exit() -> None:
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    class Controller:
        exit_code = 0

    output = io.StringIO()
    view = PromptToolkitReplView(output_stream=output)

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
    view = PromptToolkitReplView(output_stream=output)

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

    view = PromptToolkitReplView()

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

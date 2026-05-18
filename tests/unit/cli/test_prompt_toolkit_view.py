from __future__ import annotations

import asyncio

import pytest

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
    assert "session sess_full closed." in rendered
    assert "tokens used: 1 input, 2 output, 3 total" in rendered


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
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    control_writes: list[str] = []
    printed: list[str] = []

    monkeypatch.setattr(
        prompt_toolkit_view,
        "_write_terminal_control",
        lambda value: control_writes.append(value),
    )
    monkeypatch.setattr(
        prompt_toolkit_view,
        "print_formatted_text",
        lambda value="", *args, **kwargs: printed.append(str(value)),
    )
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

    assert printed == [
        "tool: git_status\nstatus: ok\nduration: 1.2s",
    ]
    assert control_writes == []


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


def test_prompt_toolkit_view_coalesces_streaming_delta_terminal_writes(
    monkeypatch,
) -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    raw_writes: list[str] = []
    printed: list[str] = []

    def record_print(value: object = "", *args: object, **kwargs: object) -> None:
        printed.append(str(value))

    monkeypatch.setattr(
        prompt_toolkit_view,
        "_write_terminal_text",
        lambda value: raw_writes.append(value),
    )
    monkeypatch.setattr(prompt_toolkit_view, "print_formatted_text", record_print)
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

    assert printed == []

    view.flush_pending_model_output(force=True)

    assert printed == []
    assert raw_writes == [
        "assistant: hello",
    ]


def test_prompt_toolkit_view_streaming_flush_restores_bottom_status_region(
    monkeypatch,
) -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    raw_writes: list[str] = []

    monkeypatch.setattr(
        prompt_toolkit_view,
        "_write_terminal_text",
        lambda value: raw_writes.append(value),
    )
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

    assert raw_writes == [
        "assistant: hello",
    ]


def test_prompt_toolkit_view_streaming_flush_can_run_inside_prompt_application(
    monkeypatch,
) -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    calls: list[tuple[bool, bool]] = []

    async def fake_run_in_terminal(func, *, render_cli_done, in_executor=False):
        calls.append((render_cli_done, in_executor))
        return func()

    monkeypatch.setattr(
        prompt_toolkit_view,
        "run_in_terminal",
        fake_run_in_terminal,
    )
    monkeypatch.setattr(prompt_toolkit_view, "_write_terminal_text", lambda value: None)
    view = PromptToolkitReplView()
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hello"},
        )
    )

    flushed = asyncio.run(view.flush_pending_model_output_in_terminal())

    assert flushed is True
    assert calls == [(False, False)]


def test_prompt_toolkit_view_streaming_flush_writes_only_new_delta(
    monkeypatch,
) -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    raw_writes: list[str] = []
    control_writes: list[str] = []

    monkeypatch.setattr(
        prompt_toolkit_view,
        "_write_terminal_text",
        lambda value: raw_writes.append(value),
    )
    monkeypatch.setattr(
        prompt_toolkit_view,
        "_write_terminal_control",
        lambda value: control_writes.append(value),
    )
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

    assert raw_writes == [
        "assistant: hel",
        "lo",
    ]
    assert control_writes == []


def test_prompt_toolkit_view_disables_prompt_buffer_edits_while_turn_runs() -> None:
    from prompt_toolkit.buffer import EditReadOnlyBuffer

    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    view = PromptToolkitReplView()

    view.set_input_enabled(False)

    assert view._session.default_buffer.read_only() is True
    with pytest.raises(EditReadOnlyBuffer):
        view._session.default_buffer.insert_text("new prompt")

    view.set_input_enabled(True)

    assert view._session.default_buffer.read_only() is False


def test_prompt_toolkit_view_streaming_flush_reports_when_redraw_is_needed(
    monkeypatch,
) -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    monkeypatch.setattr(prompt_toolkit_view, "_write_terminal_text", lambda value: None)
    view = PromptToolkitReplView()

    assert view.flush_pending_model_output(force=True) is False
    view.append_view_event(
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hello"},
        )
    )

    assert view.flush_pending_model_output(force=True) is True


def test_prompt_toolkit_view_streaming_delta_writes_literal_markdown_text(
    monkeypatch,
) -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    raw_writes: list[str] = []
    printed: list[str] = []

    monkeypatch.setattr(
        prompt_toolkit_view,
        "_write_terminal_text",
        lambda value: raw_writes.append(value),
        raising=False,
    )
    monkeypatch.setattr(
        prompt_toolkit_view,
        "print_formatted_text",
        lambda value="", *args, **kwargs: printed.append(str(value)),
    )
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

    assert printed == []
    assert raw_writes == [
        "assistant: | 文件 | 状态 |\n| --- | --- |\n- `a.py`\n- `b.py`\n",
    ]


def test_prompt_toolkit_view_prints_final_markdown_without_visible_control_sequences(
    monkeypatch,
) -> None:
    import debug_agent.cli.prompt_toolkit_view as prompt_toolkit_view
    from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView

    printed: list[str] = []

    def record_print(value: object = "", *args: object, **kwargs: object) -> None:
        printed.append(str(value))

    monkeypatch.setattr(prompt_toolkit_view, "print_formatted_text", record_print)
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

    assert printed[-1] == "assistant: final"
    assert all("\x1b" not in item for item in printed)


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

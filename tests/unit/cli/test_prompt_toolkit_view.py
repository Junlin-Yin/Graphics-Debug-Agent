from __future__ import annotations

from debug_agent.cli.repl_view import (
    ReplViewEvent,
    SessionCloseSummary,
    StatusBarSnapshot,
    ToolResultPreview,
    WelcomeSnapshot,
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
    assert "session sess_full closed." in rendered
    assert "tokens used: 1 input, 2 output, 3 total" in rendered


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

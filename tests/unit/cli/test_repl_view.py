from __future__ import annotations

import importlib.metadata

import pytest

from debug_agent.cli.repl_view import (
    PromptHistory,
    ReplViewEvent,
    SessionCloseSummary,
    StatusBarSnapshot,
    ToolResultPreviewFormatter,
    WelcomeSnapshot,
    build_session_close_summary,
    build_welcome_snapshot,
    format_token_count,
)


def test_prompt_history_navigates_previous_and_next_entries() -> None:
    history = PromptHistory()
    history.add("first")
    history.add("second")

    assert history.previous() == "second"
    assert history.previous() == "first"
    assert history.previous() == "first"
    assert history.next() == "second"
    assert history.next() is None
    assert history.next() is None


def test_prompt_history_stores_multiline_prompts_as_one_item() -> None:
    history = PromptHistory()
    history.add("line 1\nline 2")

    assert history.previous() == "line 1\nline 2"
    assert history.next() is None


def test_prompt_history_excludes_empty_prompts_and_stores_slash_commands() -> None:
    history = PromptHistory()
    history.add("")
    history.add("   ")
    history.add("/status")

    assert history.previous() == "/status"
    assert history.next() is None


def test_prompt_history_reset_navigation_after_add() -> None:
    history = PromptHistory()
    history.add("first")
    assert history.previous() == "first"

    history.add("second")

    assert history.previous() == "second"


def test_welcome_snapshot_uses_unknown_when_version_lookup_fails(monkeypatch) -> None:
    def fail_version(_package_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", fail_version)

    snapshot = build_welcome_snapshot(
        config_snapshot={"model": "fake-model"},
        workspace_root="/tmp/work",
        approval_mode="normal",
        session_id="sess_1234567890",
    )

    assert snapshot == WelcomeSnapshot(
        tool_name="debug-agent",
        version="unknown",
        model="fake-model",
        workspace_root="/tmp/work",
        approval_mode="normal",
        session_id_short="sess-1234",
    )


def test_welcome_snapshot_uses_unknown_when_model_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda _package_name: "1.2.3")

    snapshot = build_welcome_snapshot(
        config_snapshot={},
        workspace_root="/tmp/work",
        approval_mode="yolo",
        session_id="abcdefghi",
    )

    assert snapshot.version == "1.2.3"
    assert snapshot.model == "unknown"
    assert snapshot.session_id_short == "sess-abcd"


def test_welcome_snapshot_uses_first_four_characters_of_contract_session_id(
    monkeypatch,
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda _package_name: "1.2.3")

    snapshot = build_welcome_snapshot(
        config_snapshot={"model": "fake-model"},
        workspace_root="/tmp/work",
        approval_mode="normal",
        session_id="abcd_runtime_session",
    )

    assert snapshot.session_id_short == "sess-abcd"


def test_welcome_snapshot_uses_phase_0_session_id_unique_suffix(monkeypatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda _package_name: "1.2.3")

    snapshot = build_welcome_snapshot(
        config_snapshot={"model": "fake-model"},
        workspace_root="/tmp/work",
        approval_mode="normal",
        session_id="sess_2026-05-18-09-47-59-0abc",
    )

    assert snapshot.session_id_short == "sess-0abc"


def test_status_bar_snapshot_includes_model_and_formats_tokens() -> None:
    snapshot = StatusBarSnapshot(
        input_tokens=999,
        output_tokens=1_000,
        total_tokens=None,
        approval_mode="normal",
        model="fake-model",
    )

    assert snapshot.model == "fake-model"
    assert format_token_count(snapshot.input_tokens) == "999"
    assert format_token_count(snapshot.output_tokens) == "1.0k"
    assert format_token_count(snapshot.total_tokens) == "unavailable"
    assert format_token_count(12_345) == "12.3k"


def test_session_close_summary_uses_full_session_id() -> None:
    summary = build_session_close_summary(
        session_id="sess_full_identifier",
        status="closed",
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        error_type=None,
    )

    assert summary == SessionCloseSummary(
        session_id="sess_full_identifier",
        status="closed",
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        error_type=None,
    )


def test_tool_result_preview_truncates_by_line_limit() -> None:
    preview = ToolResultPreviewFormatter().format(
        output="one\ntwo\nthree",
        redacted_output=None,
        artifact_ids=["art_1"],
        max_lines=2,
        max_chars=100,
    )

    assert preview.truncated is True
    assert preview.shown_lines == 2
    assert preview.total_lines == 3
    assert preview.artifact_ids == ["art_1"]
    assert preview.text == (
        "> one\n"
        "> two\n"
        "> ...\n"
        "> [truncated: showing 2 of 3 lines, full output saved as artifact art_1]"
    )


def test_tool_result_preview_truncates_by_character_limit() -> None:
    preview = ToolResultPreviewFormatter().format(
        output="abcdef",
        redacted_output=None,
        artifact_ids=[],
        max_lines=10,
        max_chars=3,
    )

    assert preview.truncated is True
    assert preview.shown_lines == 1
    assert preview.total_lines == 1
    assert preview.text == (
        "> abc\n"
        "> ...\n"
        "> [truncated: showing 1 of 1 lines]"
    )


def test_tool_result_preview_formats_dict_output_with_sorted_keys_and_unicode() -> None:
    preview = ToolResultPreviewFormatter().format(
        output={"z": 1, "a": "中文"},
        redacted_output=None,
        artifact_ids=[],
    )

    assert preview.truncated is False
    assert preview.text == '> {"a": "中文", "z": 1}'


def test_tool_result_preview_prefers_redacted_output_over_raw_output() -> None:
    preview = ToolResultPreviewFormatter().format(
        output="secret raw output",
        redacted_output="[redacted]",
        artifact_ids=[],
    )

    assert preview.text == "> [redacted]"


def test_tool_result_preview_does_not_create_artifacts(tmp_path) -> None:
    before = set(tmp_path.iterdir())

    preview = ToolResultPreviewFormatter().format(
        output="short",
        redacted_output=None,
        artifact_ids=[],
    )

    assert preview.text == "> short"
    assert set(tmp_path.iterdir()) == before


def test_repl_view_event_is_rendering_layer_event() -> None:
    event = ReplViewEvent(kind="system_message", payload={"message": "ok"})

    assert event.kind == "system_message"
    assert event.payload == {"message": "ok"}


def test_repl_view_protocol_accepts_cli_style_run_result() -> None:
    class FakeView:
        def run(self, controller: object) -> int:
            return 0

        def show_welcome(self, snapshot: WelcomeSnapshot) -> None:
            pass

        def set_input_enabled(self, enabled: bool) -> None:
            pass

        def append_user_message(self, message: str) -> None:
            pass

        def append_view_event(self, event: ReplViewEvent) -> None:
            pass

        def set_turn_status(
            self, turn_id: int, status: str, elapsed_seconds: int
        ) -> None:
            pass

        def update_status_bar(self, snapshot: StatusBarSnapshot) -> None:
            pass

        def show_session_closed(self, summary: SessionCloseSummary) -> None:
            pass

        def show_error(self, message: str) -> None:
            pass

    assert FakeView().run(controller=object()) == 0

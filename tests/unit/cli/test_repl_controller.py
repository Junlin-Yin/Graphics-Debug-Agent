from __future__ import annotations

import threading
from typing import Any

from debug_agent.cli.repl_view import ReplViewEvent
from debug_agent.runtime.contracts import AgentRunResult, Session
from debug_agent.runtime.stream_events import AgentStreamEvent


class FakeView:
    def __init__(self) -> None:
        self.user_messages: list[str] = []
        self.input_enabled: list[bool] = []
        self.events: list[ReplViewEvent] = []
        self.turn_statuses: list[tuple[int, str, int]] = []
        self.status_bars: list[Any] = []
        self.closed_summaries: list[Any] = []
        self.errors: list[str] = []

    def run(self, controller: object) -> int:
        return 0

    def show_welcome(self, snapshot: object) -> None:
        pass

    def set_input_enabled(self, enabled: bool) -> None:
        self.input_enabled.append(enabled)

    def append_user_message(self, message: str) -> None:
        self.user_messages.append(message)

    def append_view_event(self, event: ReplViewEvent) -> None:
        self.events.append(event)

    def set_turn_status(
        self, turn_id: int, status: str, elapsed_seconds: int
    ) -> None:
        self.turn_statuses.append((turn_id, status, elapsed_seconds))

    def update_status_bar(self, snapshot: object) -> None:
        self.status_bars.append(snapshot)

    def show_session_closed(self, summary: object) -> None:
        self.closed_summaries.append(summary)

    def show_error(self, message: str) -> None:
        self.errors.append(message)


class FakeSessions:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, session_id: str) -> Session:
        assert session_id == self.session.session_id
        return self.session


class FakeRuntime:
    def __init__(self, result: AgentRunResult | None = None) -> None:
        self.result = result or _result("completed", assistant_output="answer")
        self.run_inputs: list[str] = []
        self.status = ["session_id: sess_1", "approval_mode: normal"]
        self.completed = False
        self.closed = False
        self.failed_results: list[AgentRunResult] = []
        self.session_id = "sess_123456789"
        self.run_id = "run_1"
        self.workspace_root = "/repo"
        self.approval_mode = "normal"
        self.config_snapshot = {"model": "fake-model"}
        self.latest_context_estimate = None
        self.sessions = FakeSessions(
            Session(
                session_id=self.session_id,
                workspace_root=self.workspace_root,
                status="running",
                approval_mode=self.approval_mode,
                active_run_id=self.run_id,
                artifact_root="/repo/.sessions/different_directory_name/artifacts",
                config_snapshot=self.config_snapshot,
                latest_checkpoint_id=None,
                created_at="2026-05-18T00:00:00Z",
                updated_at="2026-05-18T00:00:00Z",
                error_summary=None,
            )
        )
        self.block_turn = False
        self.turn_started = threading.Event()
        self.release_turn = threading.Event()
        self.stream_events: list[AgentStreamEvent] = []
        self.stream_callback_seen = False
        self.frozen_skill_lines = [
            "",
            "- alpha (project) [active]",
            "Alpha skill",
            "",
            "- beta (global) [inactive]",
            "Beta skill",
        ]
        self.runtime_tool_lines = [
            "Tools:",
            "- read_file | category: native | risk: read | access: read | approval: auto-allow | enabled",
            "- activate_skill | category: runtime_control | risk: runtime_control | access: runtime_control | approval: ask | enabled",
        ]
        self.compress_result = _result(
            "completed",
            assistant_output="Context compressed: reduced from 100 to 40 tokens; retained 1 recent model calls.",
            metadata={
                "conversation_writeback": [
                    {
                        "seq": 1,
                        "role": "system",
                        "kind": "context_summary",
                        "content": "{}",
                    }
                ],
                "context_estimate": {"total_tokens": 40},
            },
        )
        self.compress_calls = 0

    def run_turn(self, user_input: str, agent_stream_callback=None) -> AgentRunResult:
        self.run_inputs.append(user_input)
        self.turn_started.set()
        if agent_stream_callback is not None:
            self.stream_callback_seen = True
            for event in self.stream_events:
                agent_stream_callback(event)
        if self.block_turn:
            self.release_turn.wait(timeout=2)
        return self.result

    def status_lines(self) -> list[str]:
        return self.status

    def skill_lines(self) -> list[str]:
        return self.frozen_skill_lines

    def tool_lines(self) -> list[str]:
        return self.runtime_tool_lines

    def manual_compress(self) -> AgentRunResult:
        self.compress_calls += 1
        estimate = self.compress_result.metadata.get("context_estimate")
        if isinstance(estimate, dict):
            self.latest_context_estimate = estimate
        return self.compress_result

    def complete(self) -> None:
        self.completed = True

    def fail(self, result: AgentRunResult) -> None:
        self.failed_results.append(result)

    def close(self) -> None:
        self.closed = True


def _result(
    status: str,
    *,
    assistant_output: str | None = None,
    usage: dict[str, Any] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        assistant_output=assistant_output,
        tool_results=tool_results or [],
        usage=usage or {},
        error=error,
        metadata=metadata or {},
    )


def test_submit_runs_turn_in_background_and_finalizes_on_ui_side() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(
        _result(
            "completed",
            assistant_output="final answer",
            usage={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        )
    )
    runtime.block_turn = True
    wakeups: list[str] = []
    controller = ReplController(
        runtime=runtime,
        view=view,
        wakeup_callback=lambda: wakeups.append("ready"),
        time_fn=lambda: 100.0,
    )

    controller.on_submit("hello")

    assert runtime.turn_started.wait(timeout=2)
    assert view.user_messages == ["hello"]
    assert view.input_enabled == [False]
    assert view.turn_statuses == [(1, "running", 0)]
    assert view.events == []

    runtime.release_turn.set()
    controller.wait_for_active_turn(timeout=2)

    assert wakeups == ["ready"]
    assert controller.drain_completed_turns() == 1
    assert view.events == [
        ReplViewEvent(kind="model_markdown_final", payload={"text": "final answer"})
    ]
    assert view.turn_statuses[-1] == (1, "completed", 0)
    assert view.input_enabled == [False, True]
    assert view.status_bars[-1].input_tokens == 3
    assert view.status_bars[-1].output_tokens == 5
    assert view.status_bars[-1].total_tokens == 8
    assert runtime.run_inputs == ["hello"]


def test_background_runtime_does_not_call_view_before_ui_drain() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(_result("completed", assistant_output="answer"))
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("hello")
    controller.wait_for_active_turn(timeout=2)

    assert view.events == []
    assert view.input_enabled == [False]
    assert controller.drain_completed_turns() == 1
    assert view.events
    assert view.input_enabled == [False, True]


def test_stream_events_are_queued_wakeup_and_drained_on_ui_side() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(_result("completed", assistant_output="hello"))
    runtime.stream_events = [
        AgentStreamEvent(
            kind="stream_model_call_started",
            payload={"model_call_id": "model_1"},
        ),
        AgentStreamEvent(
            kind="stream_text_delta",
            payload={"model_call_id": "model_1", "text": "hel"},
        ),
        AgentStreamEvent(
            kind="stream_text_delta",
            payload={"model_call_id": "model_1", "text": "lo"},
        ),
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": "model_1",
                "is_final": True,
                "usage": {},
                "duration_ms": 1,
            },
        ),
    ]
    wakeups: list[str] = []
    controller = ReplController(
        runtime=runtime,
        view=view,
        wakeup_callback=lambda: wakeups.append("ready"),
    )

    controller.on_submit("hello")
    controller.wait_for_active_turn(timeout=2)

    assert runtime.stream_callback_seen is True
    assert wakeups
    assert view.events == []
    assert controller.drain_stream_events() == 4
    assert view.events == [
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "hel"},
        ),
        ReplViewEvent(
            kind="model_text_delta",
            payload={"model_call_id": "model_1", "text": "lo"},
        ),
        ReplViewEvent(
            kind="model_markdown_final",
            payload={"model_call_id": "model_1", "text": "hello"},
        ),
    ]
    controller.drain_completed_turns()


def test_notify_event_ready_does_not_mutate_view_state() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    wakeups: list[str] = []
    controller = ReplController(
        runtime=runtime,
        view=view,
        wakeup_callback=lambda: wakeups.append("ready"),
    )

    controller.notify_event_ready()

    assert wakeups == ["ready"]
    assert view.events == []
    assert view.input_enabled == []


def test_malformed_stream_event_payload_does_not_block_queue_or_finalization() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(_result("completed", assistant_output="ok"))
    controller = ReplController(runtime=runtime, view=view)

    controller.on_agent_stream_event(
        AgentStreamEvent(kind="stream_text_delta", payload={"model_call_id": "model_1"})
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_text_delta",
            payload={"model_call_id": "model_1", "text": "ok"},
        )
    )

    assert controller.drain_stream_events() == 2
    assert view.events[0].kind == "error_message"
    assert view.events[1] == ReplViewEvent(
        kind="model_text_delta",
        payload={"model_call_id": "model_1", "text": "ok"},
    )

    controller.on_turn_finished(runtime.result)

    assert view.turn_statuses[-1][1] == "completed"
    assert view.input_enabled[-1] is True


def test_streaming_fallback_warning_is_shown_once_and_uses_final_result_rendering() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="fallback answer",
            metadata={"streaming_fallback": True},
        )
    )

    assert view.events == [
        ReplViewEvent(
            kind="system_message",
            payload={
                "message": "streaming unavailable for this model; using non-streaming response."
            },
        ),
        ReplViewEvent(
            kind="model_markdown_final",
            payload={"text": "fallback answer"},
        ),
    ]


def test_streamed_turn_finalization_does_not_duplicate_assistant_output() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_text_delta",
            payload={"model_call_id": "model_1", "text": "answer"},
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": "model_1",
                "is_final": True,
                "usage": {},
                "duration_ms": 1,
            },
        )
    )
    controller.drain_stream_events()

    controller.on_turn_finished(_result("completed", assistant_output="answer"))

    assert [
        event.kind for event in view.events if event.kind == "model_markdown_final"
    ] == ["model_markdown_final"]


def test_streamed_tool_started_is_silent_and_completed_then_result_append_blocks() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_call_started",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "name": "git_status",
                "args": {},
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_call_completed",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "ok",
                "duration_ms": 12,
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_result",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "output": "M file.py",
                "redacted_output": None,
                "artifact_ids": [],
            },
        )
    )

    assert controller.drain_stream_events() == 3
    assert [event.kind for event in view.events] == [
        "tool_block",
        "tool_block",
    ]
    assert view.events[0].payload["name"] == "git_status"
    assert view.events[0].payload["status"] == "ok"
    assert view.events[0].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "git_status",
        "duration_ms": 12,
    }
    assert view.events[0].payload.get("preview") is None
    assert view.events[1].payload["name"] == "git_status"
    assert view.events[1].payload["status"] == "result"
    assert view.events[1].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "git_status",
    }
    assert view.events[-1].payload["preview"].text == "> M file.py"


def test_interrupt_marks_runtime_cancelled_and_shows_cancel_summary() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_interrupt()

    assert controller.exit_code == 1
    assert runtime.failed_results[-1].status == "cancelled"
    assert runtime.failed_results[-1].error["error_class"] == "cancelled"
    assert view.closed_summaries[-1].status == "cancelled"
    assert view.input_enabled[-1] is False


def test_timer_updates_running_turn_elapsed_seconds() -> None:
    from debug_agent.cli.repl_controller import ReplController

    now = 10.0
    view = FakeView()
    runtime = FakeRuntime()
    runtime.block_turn = True
    controller = ReplController(runtime=runtime, view=view, time_fn=lambda: now)

    controller.on_submit("hello")
    assert runtime.turn_started.wait(timeout=2)

    now = 11.2
    controller.update_running_turn_status()
    now = 12.0
    controller.update_running_turn_status()

    runtime.release_turn.set()
    controller.wait_for_active_turn(timeout=2)

    assert (1, "running", 1) in view.turn_statuses
    assert (1, "running", 2) in view.turn_statuses
    controller.drain_completed_turns()


def test_active_prompt_is_rejected_without_runtime_call() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    runtime.block_turn = True
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("first")
    assert runtime.turn_started.wait(timeout=2)
    controller.on_submit("second")

    runtime.release_turn.set()
    controller.wait_for_active_turn(timeout=2)

    assert runtime.run_inputs == ["first"]
    assert view.events[-1].kind == "system_message"
    assert "already executing" in view.events[-1].payload["message"]
    controller.drain_completed_turns()


def test_plain_repl_turn_scoped_failure_does_not_terminalize_runtime() -> None:
    from io import StringIO

    from debug_agent.cli.repl_controller import ReplController

    runtime = FakeRuntime(
        _result(
            "failed",
            error={
                "error_class": "internal_error",
                "message": "Tool call loop exceeded Phase 0 iteration limit.",
                "source": "adapter",
                "recoverable": True,
            },
            metadata={"failure_scope": "turn"},
        )
    )
    controller = ReplController(runtime=runtime)
    output = StringIO()

    should_continue = controller.handle_line("hello", output)

    assert should_continue is True
    assert controller.exit_code == 0
    assert runtime.failed_results == []
    assert runtime.closed is False
    assert "Tool call loop exceeded Phase 0 iteration limit." in output.getvalue()


def test_status_slash_command_appends_system_message() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_slash_command("/status")

    assert view.events == [
        ReplViewEvent(
            kind="system_message",
            payload={"message": "session_id: sess_1\napproval_mode: normal"},
        )
    ]


def test_skills_slash_command_appends_local_frozen_skill_state() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    should_continue = controller.on_slash_command("/skills")

    assert should_continue is True
    assert runtime.run_inputs == []
    assert view.events == [
        ReplViewEvent(
            kind="system_message",
            payload={"message": "\n".join(runtime.frozen_skill_lines)},
        )
    ]


def test_tools_slash_command_appends_local_runtime_tool_state() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    should_continue = controller.on_slash_command("/tools")

    assert should_continue is True
    assert runtime.run_inputs == []
    assert view.events == [
        ReplViewEvent(
            kind="system_message",
            payload={"message": "\n".join(runtime.runtime_tool_lines)},
        )
    ]


def test_runtime_tool_listing_includes_policy_and_mode_context() -> None:
    from debug_agent.runtime.contracts import ToolDefinition
    from debug_agent.runtime.orchestrator import format_tool_listing

    lines = format_tool_listing(
        [
            ToolDefinition(
                name="shell_exec",
                description="Run shell.",
                input_schema={"type": "object"},
                category="shell",
                risk_level="execute",
                access=["execute"],
            )
        ],
        approval_mode="semi-auto",
        config_snapshot={
            "context": {"window_tokens": 1000},
            "policy": {
                "builtin_path_deny": [{"raw": ".sessions/"}],
                "user_path_trust": [{"raw": "../trusted/"}],
                "user_path_deny": [{"raw": "secrets/"}],
                "user_shell": {"allow": [["git"]], "deny": [["rm"]]},
            },
        },
    )

    rendered = "\n".join(lines)
    assert "Approval mode: semi-auto" in rendered
    assert "Path policy: trusted=../trusted/; denied=.sessions/, secrets/" in rendered
    assert "Shell policy: allow=git; deny=rm" in rendered
    assert (
        "- shell_exec | category: shell | risk: execute | access: execute | "
        "approval: auto-allow in trusted paths; ask outside trusted paths | "
        "enabled | note: limited by shell allowlist"
    ) in rendered


def test_compress_slash_command_uses_runtime_manual_compress() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    should_continue = controller.on_slash_command("/compress")

    assert should_continue is True
    assert runtime.compress_calls == 1
    assert runtime.run_inputs == []
    assert runtime.latest_context_estimate == {"total_tokens": 40}
    assert view.events == [
        ReplViewEvent(
            kind="system_message",
            payload={
                "message": (
                    "Context compressed: reduced from 100 to 40 tokens; "
                    "retained 1 recent model calls."
                )
            },
        )
    ]


def test_compress_slash_noop_displays_exact_message_without_turn() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    runtime.compress_result = _result(
        "completed",
        assistant_output="No compressible history.",
        metadata={"conversation_writeback": [], "context_estimate": {"total_tokens": 12}},
    )
    controller = ReplController(runtime=runtime, view=view)

    should_continue = controller.on_slash_command("/compress")

    assert should_continue is True
    assert runtime.compress_calls == 1
    assert runtime.run_inputs == []
    assert view.events == [
        ReplViewEvent(
            kind="system_message",
            payload={"message": "No compressible history."},
        )
    ]


def test_compress_slash_turn_scoped_failure_keeps_session_open() -> None:
    from debug_agent.cli.repl_controller import ReplController

    expected_message = (
        "Context compression could not fit the oldest eligible history group. "
        "The current turn was aborted. Start a new session to continue with a "
        "fresh context window."
    )
    view = FakeView()
    runtime = FakeRuntime()
    runtime.compress_result = _result(
        "failed",
        error={"error_class": "compression_failed", "message": expected_message},
        metadata={"failure_scope": "turn"},
    )
    controller = ReplController(runtime=runtime, view=view)

    should_continue = controller.on_slash_command("/compress")

    assert should_continue is True
    assert runtime.compress_calls == 1
    assert runtime.failed_results == []
    assert runtime.closed is False
    assert controller.exit_code == 0
    assert view.errors == [expected_message]
    assert view.input_enabled[-1] is True


def test_active_slash_status_is_suppressed_without_runtime_side_effects() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    controller.is_executing = True

    should_continue = controller.on_slash_command("/status")

    assert should_continue is True
    assert view.events == []
    assert runtime.completed is False
    assert runtime.failed_results == []


def test_active_slash_compress_and_tools_are_suppressed_without_runtime_side_effects() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    controller.is_executing = True

    compress_should_continue = controller.on_slash_command("/compress")
    tools_should_continue = controller.on_slash_command("/tools")

    assert compress_should_continue is True
    assert tools_should_continue is True
    assert runtime.compress_calls == 0
    assert runtime.run_inputs == []
    assert view.events == []
    assert runtime.completed is False
    assert runtime.failed_results == []


def test_active_slash_exit_and_unknown_command_are_suppressed() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    controller.is_executing = True

    should_continue = controller.on_slash_command("/exit")
    unknown_should_continue = controller.on_slash_command("/unknown")

    assert should_continue is True
    assert unknown_should_continue is True
    assert runtime.completed is False
    assert runtime.failed_results == []
    assert controller.exit_code == 0
    assert view.closed_summaries == []
    assert view.events == []


def test_unsupported_phase_1_commands_are_reported_without_model_call() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    assert controller.on_slash_command("/agents") is True
    assert controller.on_slash_command("/models") is True
    assert controller.on_slash_command("/compact") is True

    assert runtime.run_inputs == []
    assert [event.payload["message"] for event in view.events] == [
        "Unsupported Phase 1 slash command: /agents",
        "Unsupported Phase 1 slash command: /models",
        "Unsupported Phase 1 slash command: /compact",
    ]


def test_welcome_snapshot_uses_contract_session_id_not_artifact_directory() -> None:
    from debug_agent.cli.repl_controller import ReplController

    runtime = FakeRuntime()
    runtime.session_id = "sess_2026-05-18-09-47-59-0abc"
    runtime.sessions = FakeSessions(
        Session(
            session_id="sess_2026-05-18-09-47-59-0abc",
            workspace_root="/repo",
            status="running",
            approval_mode="normal",
            active_run_id="run_1",
            artifact_root="/repo/.sessions/wxyz_directory_name/artifacts",
            config_snapshot={"model": "fake-model"},
            latest_checkpoint_id=None,
            created_at="2026-05-18T00:00:00Z",
            updated_at="2026-05-18T00:00:00Z",
            error_summary=None,
        )
    )
    controller = ReplController(runtime=runtime)

    snapshot = controller.welcome_snapshot()

    assert snapshot.session_id_short == "sess-0abc"


def test_exit_slash_command_completes_runtime_and_shows_summary() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    should_continue = controller.on_slash_command("/exit")

    assert should_continue is False
    assert runtime.completed is True
    assert view.closed_summaries[-1].session_id == "sess_123456789"
    assert view.closed_summaries[-1].status == "closed"


def test_completed_turn_adapts_tool_results_into_tool_blocks() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output=None,
            tool_results=[
                {
                    "status": "ok",
                    "output": "one\ntwo",
                    "error": None,
                    "artifacts": ["art_1"],
                    "metadata": {"tool_name": "read_file"},
                    "redacted_output": None,
                }
            ],
        )
    )

    assert view.events[-1].kind == "tool_block"
    assert view.events[-1].payload["status"] == "ok"
    assert view.events[-1].payload["metadata"] == {"tool_name": "read_file"}
    assert view.events[-1].payload["preview"].text == "> one\n> two"
    assert view.events[-1].payload["preview"].artifact_ids == ["art_1"]


def test_recoverable_turn_failure_keeps_session_open_and_allows_next_prompt() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(
        _result(
            "failed",
            error={
                "error_class": "internal_error",
                "message": "Tool call loop exceeded Phase 0 iteration limit.",
                "source": "adapter",
                "recoverable": True,
            },
            metadata={"failure_scope": "turn"},
        )
    )
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(runtime.result)

    assert runtime.failed_results == []
    assert runtime.closed is False
    assert controller.exit_code == 0
    assert controller.is_executing is False
    assert view.errors == ["Tool call loop exceeded Phase 0 iteration limit."]
    assert view.turn_statuses[-1][1] == "failed"
    assert view.input_enabled[-1] is True

    runtime.result = _result("completed", assistant_output="next answer")
    controller.on_submit("next")
    controller.wait_for_active_turn(timeout=2)
    controller.drain_completed_turns()

    assert runtime.run_inputs == ["next"]
    assert view.events[-1] == ReplViewEvent(
        kind="model_markdown_final",
        payload={"text": "next answer"},
    )


def test_terminal_turn_failure_closes_session_and_keeps_input_disabled() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(
        _result(
            "failed",
            error={
                "error_class": "model_error",
                "message": "provider failed",
                "source": "model",
                "recoverable": False,
            },
        )
    )
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("break")
    controller.wait_for_active_turn(timeout=2)
    controller.drain_completed_turns()

    assert runtime.failed_results[-1].error["message"] == "provider failed"
    assert controller.exit_code == 1
    assert controller.is_executing is False
    assert view.errors == ["provider failed"]
    assert view.closed_summaries[-1].status == "failed"
    assert view.closed_summaries[-1].error_type == "model_error"
    assert view.input_enabled[-1] is False


def test_usage_preserves_last_known_counts_when_result_omits_usage() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="one",
            usage={"input_tokens": 2, "output_tokens": 4, "total_tokens": 6},
        )
    )
    controller.on_turn_finished(_result("completed", assistant_output="two", usage={}))

    assert view.status_bars[-1].input_tokens == 2
    assert view.status_bars[-1].output_tokens == 4
    assert view.status_bars[-1].total_tokens == 6


def test_provider_total_tokens_are_accumulated_across_model_calls() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="one",
            usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )
    )
    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="two",
            usage={"input_tokens": 7, "output_tokens": 11, "total_tokens": 18},
        )
    )

    assert view.status_bars[-1].total_tokens == 23


def test_usage_falls_back_to_deterministic_context_estimate_when_provider_usage_absent() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="one",
            usage={},
            metadata={"context_estimate": {"total_tokens": 11}},
        )
    )
    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="two",
            usage={},
            metadata={"context_estimate": {"total_tokens": 13}},
        )
    )

    assert view.status_bars[-1].total_tokens == 24


def test_status_bar_context_uses_runtime_model_context_estimate() -> None:
    from debug_agent.cli.repl_controller import ReplController

    runtime = FakeRuntime()
    runtime.latest_context_estimate = {
        "total_tokens": 1250,
        "estimator_version": "deterministic-char-v1",
    }
    runtime.config_snapshot = {"model": "fake-model", "context": {"window_tokens": 5000}}
    controller = ReplController(runtime=runtime)

    snapshot = controller.status_bar_snapshot()

    assert snapshot.context_used_tokens == 1250
    assert snapshot.context_window_tokens == 5000
    assert snapshot.context_percent == 25


def test_final_status_maps_cancelled_and_timeout_display_statuses() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(
        _result(
            "failed",
            error={
                "error_class": "cancelled",
                "message": "cancelled",
                "source": "model",
                "recoverable": False,
            },
        )
    )
    controller.on_turn_finished(
        _result(
            "failed",
            error={
                "error_class": "timeout",
                "message": "timeout",
                "source": "model",
                "recoverable": True,
            },
        )
    )

    assert view.turn_statuses[-2][1] == "cancelled"
    assert view.turn_statuses[-1][1] == "timeout"
    assert [result.error["error_class"] for result in runtime.failed_results] == [
        "cancelled",
        "timeout",
    ]

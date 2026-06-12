from __future__ import annotations

import threading
import io
import time
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
        self.inline_approval_started: list[str] = []
        self.inline_approval_ended = 0

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

    def begin_inline_approval(self, request: str) -> None:
        self.inline_approval_started.append(request)

    def end_inline_approval(self) -> None:
        self.inline_approval_ended += 1


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
        self.cancel_running_calls = 0
        self.cancel_idle_calls = 0
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
            "",
            "- read_file [allow]",
            "Read file contents.",
            "",
            "- activate_skill [ask-all]",
            "Activate a frozen prompt skill for this run.",
            "",
            "Path policy:",
            "- trust = /repo",
            "- deny  = .sessions/",
            "",
            "Shell policy:",
            "- allow = none",
            "- deny  = none",
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
        self.approval_mode_changes: list[tuple[str, str]] = []
        self.trace_refresh_warning: str | None = None

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

    def consume_trace_refresh_warning(self) -> str | None:
        warning = self.trace_refresh_warning
        self.trace_refresh_warning = None
        return warning

    def cycle_approval_mode(self) -> tuple[str, str]:
        order = ["normal", "semi-auto", "yolo"]
        old = self.approval_mode
        new = order[(order.index(old) + 1) % len(order)]
        self.approval_mode = new
        self.sessions.session = Session(
            session_id=self.sessions.session.session_id,
            workspace_root=self.sessions.session.workspace_root,
            status=self.sessions.session.status,
            approval_mode=new,
            active_run_id=self.sessions.session.active_run_id,
            artifact_root=self.sessions.session.artifact_root,
            config_snapshot=self.sessions.session.config_snapshot,
            latest_checkpoint_id=self.sessions.session.latest_checkpoint_id,
            created_at=self.sessions.session.created_at,
            updated_at=self.sessions.session.updated_at,
            error_summary=self.sessions.session.error_summary,
        )
        self.approval_mode_changes.append((old, new))
        return old, new

    def fail(self, result: AgentRunResult) -> None:
        self.failed_results.append(result)

    def cancel_running_turn(self) -> AgentRunResult:
        self.cancel_running_calls += 1
        self.release_turn.set()
        return _result(
            "cancelled",
            error={
                "error_class": "cancelled",
                "reason": "user_cancel_running",
                "message": "Turn cancelled.",
            },
            metadata={"failure_scope": "turn"},
        )

    def cancel_idle(self) -> None:
        self.cancel_idle_calls += 1
        self.closed = True

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


def _eventually(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


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


def test_running_interrupt_routes_to_runtime_cancellation_without_terminalizing() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(_result("completed", assistant_output="late"))
    runtime.block_turn = True
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("hello")
    assert runtime.turn_started.wait(timeout=2)

    controller.on_interrupt()
    controller.wait_for_active_turn(timeout=2)
    controller.drain_completed_turns()

    assert runtime.cancel_running_calls == 1
    assert runtime.cancel_idle_calls == 0
    assert runtime.failed_results == []
    assert view.closed_summaries == []
    assert view.input_enabled[-1] is True


def test_running_interrupt_does_not_emit_non_durable_cancellation_requested_message() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(_result("completed", assistant_output="late"))
    runtime.block_turn = True
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("hello")
    assert runtime.turn_started.wait(timeout=2)

    controller.on_interrupt()

    assert not any(
        event.kind == "system_message"
        and event.payload.get("message")
        == "Cancellation requested; ending the current turn."
        for event in view.events
    )
    assert view.turn_statuses[-1][1] == "cancelling"

    runtime.release_turn.set()
    controller.wait_for_active_turn(timeout=2)
    controller.drain_completed_turns()


def test_input_is_locked_out_while_cancelling_without_prompt_return() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    runtime.block_turn = True
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("hello")
    assert runtime.turn_started.wait(timeout=2)

    controller.on_interrupt()
    controller.on_interrupt()
    controller.on_submit("queued prompt")
    controller.on_slash_command("/status")
    controller._approval_pending = True
    controller.on_submit("a")
    assert controller._approval_decision is None
    runtime.release_turn.set()
    controller.wait_for_active_turn(timeout=2)

    assert controller.exit_code == 0
    assert runtime.cancel_running_calls == 1
    assert runtime.run_inputs == ["hello"]
    assert not any(
        event.kind == "system_message"
        and "Prompt run is already executing" in str(event.payload.get("message"))
        for event in view.events
    )
    assert controller.drain_completed_turns() == 1
    assert view.input_enabled[-1] is True
    assert controller.control_state == "idle"


def test_provider_boundary_not_closed_aborts_without_runtime_fail_or_prompt_return() -> None:
    from debug_agent.cli.exit_codes import INTERRUPTED
    from debug_agent.cli.repl_controller import ReplController
    from debug_agent.runtime.provider_execution import ProviderBoundaryNotClosed

    class Runtime(FakeRuntime):
        def run_turn(self, user_input: str, agent_stream_callback=None) -> AgentRunResult:
            self.run_inputs.append(user_input)
            self.turn_started.set()
            raise ProviderBoundaryNotClosed("provider cancellation boundary did not close")

    view = FakeView()
    runtime = Runtime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("hello")
    controller.wait_for_active_turn(timeout=2)

    assert controller.drain_completed_turns() == 0
    assert controller.exit_code == INTERRUPTED
    assert runtime.failed_results == []
    assert view.closed_summaries == []
    assert view.input_enabled[-1] is False
    assert controller.control_state == "cancelling"


def test_cancelled_running_turn_result_returns_to_input_without_terminalizing() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    controller.is_executing = True
    controller.control_state = "cancelling"

    controller.on_turn_finished(
        _result(
            "cancelled",
            error={
                "schema_version": 1,
                "error_class": "cancelled",
                "reason": "user_cancel_running",
                "message": "Turn cancelled by user.",
                "scope": "turn",
                "recoverability": "turn_recoverable",
                "metadata": {},
                "artifact_ids": [],
            },
            metadata={"failure_scope": "turn"},
        )
    )

    assert runtime.failed_results == []
    assert view.closed_summaries == []
    assert view.input_enabled[-1] is True
    assert controller.control_state == "idle"


def test_plain_controller_keyboard_interrupt_routes_running_cancellation() -> None:
    from debug_agent.cli.repl_controller import ReplController

    class Runtime(FakeRuntime):
        def run_turn(self, user_input: str, agent_stream_callback=None) -> AgentRunResult:
            self.run_inputs.append(user_input)
            self.turn_started.set()
            self.cancel_running_calls += 1
            return _result(
                "cancelled",
                error={
                    "schema_version": 1,
                    "error_class": "cancelled",
                    "reason": "user_cancel_running",
                    "message": "Turn cancelled by user.",
                    "scope": "turn",
                    "recoverability": "turn_recoverable",
                    "metadata": {},
                    "artifact_ids": [],
                },
                metadata={"failure_scope": "turn"},
            )

    runtime = Runtime()
    controller = ReplController(runtime=runtime)
    output = io.StringIO()

    should_continue = controller.handle_line("hello\n", output=output)

    assert should_continue is True
    assert controller.exit_code == 0
    assert controller.control_state == "idle"
    assert runtime.cancel_running_calls == 1
    assert runtime.cancel_idle_calls == 0
    assert runtime.failed_results == []
    assert "Turn cancelled by user." in output.getvalue()


def test_idle_interrupt_terminalizes_idle_session_without_runtime_fail() -> None:
    from debug_agent.cli.exit_codes import INTERRUPTED
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_interrupt()

    assert runtime.cancel_idle_calls == 1
    assert runtime.failed_results == []
    assert controller.exit_code == INTERRUPTED
    assert view.closed_summaries


def test_plain_controller_escape_terminalizes_idle_session_without_model_call() -> None:
    from debug_agent.cli.exit_codes import INTERRUPTED
    from debug_agent.cli.repl_controller import ReplController

    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime)
    output = io.StringIO()

    should_continue = controller.handle_line("\x1b\n", output)

    assert should_continue is False
    assert controller.exit_code == INTERRUPTED
    assert runtime.cancel_idle_calls == 1
    assert runtime.run_inputs == []
    assert output.getvalue() == ""


def test_plain_controller_blocks_input_while_cancelling_without_busy_message() -> None:
    from debug_agent.cli.repl_controller import ReplController

    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime)
    controller.is_executing = True
    controller.control_state = "cancelling"
    output = io.StringIO()

    should_continue = controller.handle_line("queued prompt\n", output)

    assert should_continue is True
    assert runtime.run_inputs == []
    assert output.getvalue() == ""


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


def test_stream_context_estimate_updates_status_bar_before_turn_finish() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    runtime.config_snapshot = {"model": "fake-model", "context": {"window_tokens": 100}}
    controller = ReplController(runtime=runtime, view=view)

    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_context_estimate_updated",
            payload={
                "context_estimate": {
                    "total_tokens": 31,
                    "estimator_version": "deterministic-char-v1",
                },
            },
        )
    )

    assert controller.drain_stream_events() == 1
    assert view.status_bars[-1].context_used_tokens == 31
    assert view.status_bars[-1].context_window_tokens == 100
    assert view.status_bars[-1].context_percent == 31


def test_stream_model_call_completion_updates_usage_before_turn_finish() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": "model_1",
                "is_final": False,
                "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
                "duration_ms": 1,
            },
        )
    )

    assert controller.drain_stream_events() == 1
    assert view.status_bars[-1].input_tokens == 3
    assert view.status_bars[-1].output_tokens == 5
    assert view.status_bars[-1].total_tokens == 8


def test_streamed_turn_finalization_does_not_double_count_aggregate_provider_usage() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": "model_1",
                "is_final": True,
                "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
                "duration_ms": 1,
            },
        )
    )
    assert controller.drain_stream_events() == 1

    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="answer",
            usage={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        )
    )

    assert view.status_bars[-1].input_tokens == 3
    assert view.status_bars[-1].output_tokens == 5
    assert view.status_bars[-1].total_tokens == 8


def test_stream_model_call_completion_uses_context_estimate_fallback_when_usage_absent() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": "model_1",
                "is_final": False,
                "usage": {},
                "context_estimate": {"total_tokens": 21},
                "duration_ms": 1,
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": "model_2",
                "is_final": True,
                "usage": {},
                "context_estimate": {"total_tokens": 34},
                "duration_ms": 1,
            },
        )
    )

    assert controller.drain_stream_events() == 2
    assert view.status_bars[-1].total_tokens == 55


def test_streamed_turn_finalization_does_not_double_count_fallback_estimate() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_model_call_completed",
            payload={
                "model_call_id": "model_1",
                "is_final": True,
                "usage": {},
                "context_estimate": {"total_tokens": 21},
                "duration_ms": 1,
            },
        )
    )
    assert controller.drain_stream_events() == 1

    controller.on_turn_finished(
        _result(
            "completed",
            assistant_output="answer",
            usage={},
            metadata={"context_estimate": {"total_tokens": 21}},
        )
    )

    assert view.status_bars[-1].total_tokens == 21


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


def test_streamed_tool_blocks_use_broker_target_execution_duration_and_shell_preview() -> None:
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
                "name": "shell_exec",
                "args": {"argv": ["pytest", "tests"]},
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_call_completed",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "name": "shell_exec",
                "status": "ok",
                "target": "pytest tests",
                "execution_duration_ms": 1400,
                "duration_ms": 9999,
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_result",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "ok",
                "output": {"stdout": "passed\n", "stderr": "warning\n", "returncode": 0},
                "redacted_output": None,
                "artifact_ids": [],
                "error": None,
            },
        )
    )

    assert controller.drain_stream_events() == 3
    assert view.events[0].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "shell_exec",
        "target": "pytest tests",
        "execution_duration_ms": 1400,
    }
    assert view.events[1].payload["preview"].text == "> passed\n> stderr: warning"


def test_streamed_user_denial_result_does_not_duplicate_tool_summary() -> None:
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
                "name": "write_file",
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_call_completed",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "denied",
                "target": "secrets.txt",
                "error": {
                    "error_class": "policy_denied",
                    "message": "Approval denied.",
                    "source": "toolbroker",
                    "recoverable": True,
                },
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_result",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "denied",
                "output": None,
                "redacted_output": None,
                "artifact_ids": [],
            },
        )
    )

    assert controller.drain_stream_events() == 3
    assert [event.kind for event in view.events] == ["tool_block"]
    assert view.events[0].payload["name"] == "write_file"
    assert view.events[0].payload["status"] == "denied"
    assert view.events[0].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "write_file",
        "target": "secrets.txt",
    }
    assert view.events[0].payload.get("preview") is None


def test_streamed_shell_policy_denial_result_does_not_duplicate_tool_summary() -> None:
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
                "name": "shell_exec",
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_call_completed",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "denied",
                "target": "rm -rf target",
                "error": {
                    "error_class": "policy_denied",
                    "message": "Command denied by builtin shell policy.",
                    "source": "toolbroker",
                    "recoverable": True,
                },
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_result",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "denied",
                "output": None,
                "redacted_output": None,
                "artifact_ids": [],
            },
        )
    )

    assert controller.drain_stream_events() == 3
    assert [event.kind for event in view.events] == ["tool_block"]
    assert view.events[0].payload["name"] == "shell_exec"
    assert view.events[0].payload["status"] == "denied"
    assert view.events[0].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "shell_exec",
        "target": "rm -rf target",
    }
    assert view.events[0].payload.get("preview") is None


def test_streamed_tool_failure_result_does_not_duplicate_tool_summary() -> None:
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
                "name": "shell_exec",
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_call_completed",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "error",
                "target": "pytest tests",
                "execution_duration_ms": 1400,
                "error": {
                    "error_class": "tool_error",
                    "message": "pytest failed",
                    "source": "toolbroker",
                    "recoverable": True,
                },
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_result",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "error",
                "output": None,
                "redacted_output": None,
                "artifact_ids": [],
            },
        )
    )

    assert controller.drain_stream_events() == 3
    assert [event.kind for event in view.events] == ["tool_block"]
    assert view.events[0].payload["name"] == "shell_exec"
    assert view.events[0].payload["status"] == "error"
    assert view.events[0].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "shell_exec",
        "target": "pytest tests",
        "execution_duration_ms": 1400,
    }
    assert view.events[0].payload.get("preview") is None


def test_streamed_error_result_with_preview_appends_preview_only_detail() -> None:
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
                "name": "shell_exec",
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_call_completed",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "error",
                "target": "pytest tests",
                "execution_duration_ms": 1400,
                "error": {
                    "error_class": "tool_error",
                    "message": "pytest failed",
                    "source": "toolbroker",
                    "recoverable": True,
                },
            },
        )
    )
    controller.on_agent_stream_event(
        AgentStreamEvent(
            kind="stream_tool_result",
            payload={
                "tool_call_id": "tool_1",
                "model_call_id": "model_1",
                "status": "error",
                "output": "failure details",
                "redacted_output": None,
                "artifact_ids": ["artifact_1"],
            },
        )
    )

    assert controller.drain_stream_events() == 3
    assert [event.kind for event in view.events] == ["tool_block", "tool_block"]
    assert view.events[0].payload["status"] == "error"
    assert view.events[0].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "shell_exec",
        "target": "pytest tests",
        "execution_duration_ms": 1400,
    }
    assert view.events[1].payload["status"] == "result"
    assert view.events[1].payload["metadata"] == {
        "tool_call_id": "tool_1",
        "model_call_id": "model_1",
        "tool_name": "shell_exec",
    }
    assert "target" not in view.events[1].payload["metadata"]
    assert "duration_ms" not in view.events[1].payload["metadata"]
    assert "execution_duration_ms" not in view.events[1].payload["metadata"]
    assert view.events[1].payload["preview"].text == "> failure details"
    assert view.events[1].payload["preview"].artifact_ids == ["artifact_1"]


def test_idle_interrupt_terminalizes_and_shows_cancel_summary() -> None:
    from debug_agent.cli.exit_codes import INTERRUPTED
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    controller.on_interrupt()

    assert controller.exit_code == INTERRUPTED
    assert runtime.cancel_idle_calls == 1
    assert runtime.failed_results == []
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


def test_timer_preserves_cancelling_status_while_turn_is_closing() -> None:
    from debug_agent.cli.repl_controller import ReplController

    now = 10.0
    view = FakeView()
    runtime = FakeRuntime()
    runtime.block_turn = True
    controller = ReplController(runtime=runtime, view=view, time_fn=lambda: now)

    controller.on_submit("hello")
    assert runtime.turn_started.wait(timeout=2)
    controller.on_interrupt()

    now = 11.2
    controller.update_running_turn_status()

    assert view.turn_statuses[-1][1] == "cancelling"
    assert (1, "running", 1) not in view.turn_statuses

    runtime.release_turn.set()
    controller.wait_for_active_turn(timeout=2)
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


def test_idle_ctrl_y_cycles_approval_mode_and_updates_status_bar() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)

    assert controller.on_approval_mode_cycle() is True
    assert controller.on_approval_mode_cycle() is True
    assert controller.on_approval_mode_cycle() is True

    assert runtime.approval_mode_changes == [
        ("normal", "semi-auto"),
        ("semi-auto", "yolo"),
        ("yolo", "normal"),
    ]
    assert [snapshot.approval_mode for snapshot in view.status_bars[-3:]] == [
        "semi-auto",
        "yolo",
        "normal",
    ]


def test_ctrl_y_is_silent_noop_during_active_execution() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    runtime.block_turn = True
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("first")
    assert runtime.turn_started.wait(timeout=2)
    assert controller.on_approval_mode_cycle() is True

    runtime.release_turn.set()
    controller.wait_for_active_turn(timeout=2)
    controller.drain_completed_turns()

    assert runtime.approval_mode_changes == []
    assert runtime.approval_mode == "normal"


def test_ctrl_y_is_silent_noop_during_inline_approval_prompt() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    controller._approval_pending = True

    assert controller.on_approval_mode_cycle() is True

    assert runtime.approval_mode_changes == []
    assert runtime.approval_mode == "normal"
    assert view.status_bars == []


def test_controller_approval_provider_uses_input_lane_for_session_approval() -> None:
    from debug_agent.cli.repl_controller import (
        ControllerApprovalProvider,
        ReplController,
    )

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    provider = ControllerApprovalProvider(controller)
    decisions: list[object] = []
    approval_request = (
        "=== Approval Request ===\n"
        "Tool: write_file\n"
        "Target: /repo/a.txt\n"
        "\n"
        "Allow? [y]once, [a] session, [n] deny"
    )
    worker = threading.Thread(
        target=lambda: decisions.append(
            provider.request_approval(
                approval_request,
                {"tool_name": "write_file"},
            )
        )
    )

    worker.start()
    assert view.input_enabled == [True]
    assert view.inline_approval_started == [approval_request]
    assert view.events == []

    controller.on_submit("a")
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert decisions[0].decision == "approved_for_session"
    assert decisions[0].grant_scope == "session"
    assert controller._approval_pending is False


def test_running_interrupt_unblocks_pending_approval_request() -> None:
    from debug_agent.cli.repl_controller import (
        ControllerApprovalProvider,
        ReplController,
    )

    view = FakeView()
    runtime = FakeRuntime()
    controller = ReplController(runtime=runtime, view=view)
    controller.is_executing = True
    controller.control_state = "running_turn"
    provider = ControllerApprovalProvider(controller)
    decisions: list[object] = []

    worker = threading.Thread(
        target=lambda: decisions.append(
            provider.request_approval(
                "approval request",
                {"tool_name": "shell_exec"},
            )
        )
    )
    worker.start()
    assert _eventually(lambda: controller._approval_pending)

    controller.on_interrupt()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert runtime.cancel_running_calls == 1
    assert decisions[0].decision == "denied"
    assert decisions[0].message == "Turn cancelled by user."
    assert controller._approval_pending is False
    assert view.input_enabled[-1] is False
    assert view.inline_approval_ended == 1


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


def test_tui_model_timeout_displays_error_and_restores_input() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(
        _result(
            "timeout",
            error={
                "error_class": "timeout",
                "message": "Model stream timed out after 30 seconds.",
                "source": "model",
                "recoverable": True,
            },
        )
    )
    controller = ReplController(runtime=runtime, view=view)

    controller.on_submit("hello")
    controller.wait_for_active_turn(timeout=2)
    controller.drain_completed_turns()

    assert view.errors == ["Model stream timed out after 30 seconds."]
    assert view.input_enabled[-1] is True
    assert view.closed_summaries == []
    assert runtime.failed_results == []
    assert controller.exit_code == 0


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
                name="load_skill_resource",
                description="Load a frozen skill resource file.",
                input_schema={"type": "object"},
                category="runtime_control",
                risk_level="runtime_control",
                access=["runtime_control"],
            ),
            ToolDefinition(
                name="read_file",
                description="Read a file.",
                input_schema={"type": "object"},
                category="native",
                risk_level="read",
                access=["read"],
            ),
            ToolDefinition(
                name="shell_exec",
                description="Run shell.",
                input_schema={"type": "object"},
                category="shell",
                risk_level="execute",
                access=["execute"],
            )
        ],
        approval_mode="normal",
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

    assert lines == [
        "Tools:",
        "",
        "- load_skill_resource [allow]",
        "Load a frozen skill resource file.",
        "",
        "- read_file [ask-distrust]",
        "Read a file.",
        "",
        "- shell_exec [ask-all]",
        "Run shell.",
        "",
        "Path policy:",
        "- trust = ../trusted/",
        "- deny  = .sessions/, secrets/",
        "",
        "Shell policy:",
        "- allow = git",
        "- deny  = rm",
    ]


def test_runtime_tool_listing_filters_view_image_by_frozen_availability() -> None:
    from debug_agent.runtime.contracts import ToolDefinition
    from debug_agent.runtime.orchestrator import format_tool_listing

    definitions = [
        ToolDefinition(
            name="load_skill_resource",
            description="Load a frozen skill resource file.",
            input_schema={"type": "object"},
            category="runtime_control",
            risk_level="read",
            access=["read"],
        ),
        ToolDefinition(
            name="todo",
            description="Replace the current Todo Plan.",
            input_schema={"type": "object"},
            category="runtime_control",
            risk_level="runtime_control",
            access=[],
        ),
        ToolDefinition(
            name="view_image",
            description="Inspect one to four local PNG or JPEG images.",
            input_schema={"type": "object"},
            category="native",
            risk_level="read",
            access=["read"],
        ),
    ]

    disabled = "\n".join(
        format_tool_listing(
            definitions,
            approval_mode="normal",
            config_snapshot={
                "multimodal": {
                    "view_image_enabled": False,
                    "view_image_disabled_reason": "missing_api_key_env",
                }
            },
        )
    )
    enabled = "\n".join(
        format_tool_listing(
            definitions,
            approval_mode="normal",
            config_snapshot={
                "multimodal": {
                    "view_image_enabled": True,
                    "view_image_disabled_reason": None,
                }
            },
        )
    )

    assert "- load_skill_resource [allow]" in disabled
    assert "- todo [allow]" in disabled
    assert "- todo " in disabled
    assert "- view_image " not in disabled
    assert "view_image disabled: missing_api_key_env" in disabled
    assert "- load_skill_resource [allow]" in enabled
    assert "- todo [allow]" in enabled
    assert "- todo " in enabled
    assert "- view_image " in enabled
    assert "view_image disabled" not in enabled


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


def test_controller_renders_restored_conversation_history_for_resumed_tui() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    runtime.conversation = [
        {
            "seq": 1,
            "role": "user",
            "kind": "current_user_input",
            "content": "show history",
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "content": "history answer",
        },
        {
            "seq": 3,
            "role": "runtime",
            "kind": "cancellation_fact",
            "content": {
                "error_class": "cancelled",
                "reason": "user_cancel_running",
                "message": "Turn cancelled by user.",
                "artifact_ids": [],
            },
        },
    ]
    controller = ReplController(runtime=runtime, view=view)

    controller.render_restored_history()

    assert view.user_messages == ["show history"]
    assert view.events == [
        ReplViewEvent(
            kind="model_markdown_final",
            payload={"text": "history answer"},
        ),
        ReplViewEvent(
            kind="system_message",
            payload={"message": "Turn cancelled by user."},
        ),
    ]


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


def test_exit_slash_command_shows_trace_refresh_warning_as_error() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime()
    runtime.trace_refresh_warning = "Trace refresh failed: ui_error/trace_render_failed"
    controller = ReplController(runtime=runtime, view=view)

    should_continue = controller.on_slash_command("/exit")

    assert should_continue is False
    assert runtime.completed is True
    assert view.errors == ["Trace refresh failed: ui_error/trace_render_failed"]
    assert view.closed_summaries[-1].status == "closed"


def test_plain_exit_slash_command_prints_trace_refresh_warning() -> None:
    from debug_agent.cli.repl_controller import ReplController

    output = io.StringIO()
    runtime = FakeRuntime()
    runtime.trace_refresh_warning = "Trace refresh failed: ui_error/trace_render_failed"
    controller = ReplController(runtime=runtime)

    should_continue = controller.handle_line("/exit", output)

    assert should_continue is False
    assert runtime.completed is True
    assert "Trace refresh failed: ui_error/trace_render_failed" in output.getvalue()


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


def test_approval_denial_turn_abort_renders_neutral_status_not_error() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(
        _result(
            "failed",
            error={
                "error_class": "policy_denied",
                "message": "Approval denied.",
                "source": "toolbroker",
                "recoverable": True,
            },
            metadata={"failure_scope": "turn", "approval_denied_abort": True},
        )
    )
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(runtime.result)

    assert runtime.failed_results == []
    assert controller.exit_code == 0
    assert view.errors == []
    assert view.events == []
    assert view.turn_statuses[-1][1] == "failed"
    assert view.input_enabled[-1] is True


def test_running_cancellation_renders_system_message_not_error() -> None:
    from debug_agent.cli.repl_controller import ReplController

    view = FakeView()
    runtime = FakeRuntime(
        _result(
            "cancelled",
            error={
                "error_class": "cancelled",
                "reason": "user_cancel_running",
                "message": "Turn cancelled by user.",
                "scope": "turn",
                "recoverability": "turn_recoverable",
            },
            metadata={"failure_scope": "turn"},
        )
    )
    controller = ReplController(runtime=runtime, view=view)

    controller.on_turn_finished(runtime.result)

    assert runtime.failed_results == []
    assert controller.exit_code == 0
    assert view.errors == []
    assert view.events == [
        ReplViewEvent(
            kind="system_message",
            payload={"message": "Turn cancelled by user."},
        )
    ]
    assert view.turn_statuses[-1][1] == "cancelled"
    assert view.input_enabled[-1] is True


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

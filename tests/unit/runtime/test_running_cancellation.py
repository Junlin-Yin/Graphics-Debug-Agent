from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

import pytest

from debug_agent.adapters.model_factory import ModelFactoryResult
from debug_agent.runtime import orchestrator as orchestrator_module
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.orchestrator import RuntimeOrchestrator
from debug_agent.runtime.provider_execution import ProviderBoundaryNotClosed
from debug_agent.runtime.stream_events import AgentStreamEvent


def _config() -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": "unused",
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": "You are debug-agent.",
    }


def test_running_cancellation_accepts_turn_fact_without_terminalizing_or_releasing(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter_started = threading.Event()

    class CancellableAdapter:
        def __init__(self, *, model: object, tool_broker: object) -> None:
            self.model = model
            self.tool_broker = tool_broker

        def run(self, request, context):
            adapter_started.set()
            while not context.cancellation_token.is_cancelled():
                time.sleep(0.001)
            return AgentRunResult(
                status="cancelled",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "schema_version": 1,
                    "error_class": "cancelled",
                    "reason": "model_call_cancelled",
                    "message": "Provider call cancelled.",
                    "scope": "provider",
                    "recoverability": "turn_recoverable",
                    "metadata": {},
                    "artifact_ids": [],
                },
                metadata={"failure_scope": "turn"},
            )

        def stream(self, request, context, on_event):
            return self.run(request, context)

    monkeypatch.setattr(
        orchestrator_module, "LangChainAgentLoopAdapter", CancellableAdapter
    )
    start = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert start.runtime is not None
    runtime = start.runtime
    result_box: dict[str, AgentRunResult] = {}
    turn_thread = threading.Thread(
        target=lambda: result_box.setdefault("result", runtime.run_turn("hello")),
    )
    turn_thread.start()
    try:
        assert adapter_started.wait(timeout=2)

        cancel_result = runtime.cancel_running_turn()
        turn_thread.join(timeout=2)

        assert cancel_result.status == "cancelled"
        assert cancel_result.error["reason"] == "user_cancel_running"
        assert result_box["result"].status == "cancelled"
        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            session_row = conn.execute(
                """
                SELECT status, active_run_id, latest_checkpoint_id
                FROM sessions
                """
            ).fetchone()
            run_row = conn.execute(
                "SELECT status, latest_checkpoint_id FROM runs"
            ).fetchone()
            checkpoints = conn.execute("SELECT kind FROM checkpoints").fetchall()
            conversation = [
                (role, kind, json.loads(content_json))
                for role, kind, content_json in conn.execute(
                    """
                    SELECT role, kind, content_json
                    FROM conversation_messages
                    ORDER BY message_index
                    """
                )
            ]

        assert session_row == ("running", runtime.run_id, None)
        assert run_row == ("running", None)
        assert checkpoints == []
        assert conversation == [
            ("user", "user_input", {"content": "hello"}),
            (
                "runtime",
                "cancellation_fact",
                {
                    "error_class": "cancelled",
                    "reason": "user_cancel_running",
                    "message": "Turn cancelled by user.",
                    "artifact_ids": [],
                },
            ),
        ]
    finally:
        runtime.close()


def test_plain_running_keyboard_interrupt_accepts_user_running_cancellation_fact(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen_token: dict[str, object] = {}

    class InterruptingAdapter:
        def __init__(self, *, model: object, tool_broker: object) -> None:
            self.model = model
            self.tool_broker = tool_broker

        def run(self, request, context):
            seen_token["token"] = context.cancellation_token
            raise KeyboardInterrupt("user interrupt")

        def stream(self, request, context, on_event):
            return self.run(request, context)

    monkeypatch.setattr(
        orchestrator_module, "LangChainAgentLoopAdapter", InterruptingAdapter
    )
    start = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert start.runtime is not None
    runtime = start.runtime

    try:
        result = runtime.run_turn("hello")

        assert result.status == "cancelled"
        assert result.error["reason"] == "user_cancel_running"
        assert seen_token["token"].is_cancelled()
        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            conversation = [
                (role, kind, json.loads(content_json))
                for role, kind, content_json in conn.execute(
                    """
                    SELECT role, kind, content_json
                    FROM conversation_messages
                    ORDER BY message_index
                    """
                )
            ]

        assert conversation == [
            ("user", "user_input", {"content": "hello"}),
            (
                "runtime",
                "cancellation_fact",
                {
                    "error_class": "cancelled",
                    "reason": "user_cancel_running",
                    "message": "Turn cancelled by user.",
                    "artifact_ids": [],
                },
            ),
        ]
    finally:
        runtime.close()


def test_plain_running_keyboard_interrupt_waits_for_provider_boundary_before_acceptance(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker_released = threading.Event()
    collect_finished = threading.Event()

    class InterruptingAdapter:
        def __init__(self, *, model: object, tool_broker: object) -> None:
            self.model = model
            self.tool_broker = tool_broker

        def run(self, request, context):
            handle = _CollectableHandle(
                collect=lambda: (
                    worker_released.wait(timeout=2),
                    collect_finished.set(),
                )
            )
            context.metadata["provider_cancellation_registry"](handle)
            raise KeyboardInterrupt("user interrupt")

        def stream(self, request, context, on_event):
            return self.run(request, context)

    monkeypatch.setattr(
        orchestrator_module, "LangChainAgentLoopAdapter", InterruptingAdapter
    )
    start = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert start.runtime is not None
    runtime = start.runtime

    try:
        result_box: dict[str, AgentRunResult] = {}
        turn_thread = threading.Thread(
            target=lambda: result_box.setdefault("result", runtime.run_turn("hello")),
            daemon=True,
        )
        turn_thread.start()
        time.sleep(0.05)

        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            before_release = conn.execute(
                """
                SELECT COUNT(*)
                FROM conversation_messages
                WHERE kind = 'cancellation_fact'
                """
            ).fetchone()[0]

        assert before_release == 0
        assert turn_thread.is_alive()
        worker_released.set()
        turn_thread.join(timeout=2)

        assert collect_finished.is_set()
        assert result_box["result"].error["reason"] == "user_cancel_running"
        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            after_release = conn.execute(
                """
                SELECT COUNT(*)
                FROM conversation_messages
                WHERE kind = 'cancellation_fact'
                """
            ).fetchone()[0]
        assert after_release == 1
    finally:
        runtime.close()


def test_plain_running_keyboard_interrupt_fail_closed_when_provider_boundary_unclosed(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class InterruptingAdapter:
        def __init__(self, *, model: object, tool_broker: object) -> None:
            self.model = model
            self.tool_broker = tool_broker

        def run(self, request, context):
            context.metadata["provider_cancellation_registry"](
                _CollectableHandle(
                    collect=lambda: (_ for _ in ()).throw(
                        ProviderBoundaryNotClosed("provider worker did not close")
                    )
                )
            )
            raise KeyboardInterrupt("user interrupt")

        def stream(self, request, context, on_event):
            return self.run(request, context)

    monkeypatch.setattr(
        orchestrator_module, "LangChainAgentLoopAdapter", InterruptingAdapter
    )
    start = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert start.runtime is not None
    runtime = start.runtime

    try:
        with pytest.raises(ProviderBoundaryNotClosed):
            runtime.run_turn("hello")

        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            conversation = [
                (role, kind)
                for role, kind in conn.execute(
                    """
                    SELECT role, kind
                    FROM conversation_messages
                    ORDER BY message_index
                    """
                )
            ]

        assert conversation == [("user", "user_input")]
    finally:
        runtime.close()


def test_repl_runtime_non_stream_turn_uses_async_provider_cancellation_boundary(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = threading.Event()
    task_cancelled = threading.Event()
    late_returned = threading.Event()

    class AsyncOnlyModel:
        def invoke(self, _messages):
            raise AssertionError("sync invoke must not be used")

        async def ainvoke(self, _messages):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise
            late_returned.set()
            return type(
                "Response",
                (),
                {"content": "late output", "tool_calls": [], "usage": {}},
            )()

    class Factory:
        def create(self, _config_snapshot):
            return ModelFactoryResult(model=AsyncOnlyModel(), error=None)

    monkeypatch.setattr(orchestrator_module, "ModelFactory", Factory)
    start = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert start.runtime is not None
    runtime = start.runtime

    try:
        result_box: dict[str, AgentRunResult] = {}
        turn_thread = threading.Thread(
            target=lambda: result_box.setdefault("result", runtime.run_turn("hello"))
        )
        turn_thread.start()
        assert started.wait(timeout=2)

        cancel_result = runtime.cancel_running_turn()
        turn_thread.join(timeout=2)

        assert cancel_result.error["reason"] == "user_cancel_running"
        assert not turn_thread.is_alive()
        assert result_box["result"].status == "cancelled"
        assert result_box["result"].error["reason"] == "user_cancel_running"
        assert task_cancelled.wait(timeout=1)
        assert not late_returned.is_set()
        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            conversation = [
                (role, kind, json.loads(content_json))
                for role, kind, content_json in conn.execute(
                    """
                    SELECT role, kind, content_json
                    FROM conversation_messages
                    ORDER BY message_index
                    """
                )
            ]

        assert conversation == [
            ("user", "user_input", {"content": "hello"}),
            (
                "runtime",
                "cancellation_fact",
                {
                    "error_class": "cancelled",
                    "reason": "user_cancel_running",
                    "message": "Turn cancelled by user.",
                    "artifact_ids": [],
                },
            ),
        ]
    finally:
        runtime.close()


def test_repl_runtime_stream_turn_uses_async_stream_cancellation_boundary(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_chunk_sent = threading.Event()
    stream_cancelled = threading.Event()
    late_chunk_sent = threading.Event()

    class AsyncStreamingModel:
        stream_chunks = ["configured"]

        def stream(self, _messages):
            raise AssertionError("sync stream must not be used")

        async def astream(self, _messages):
            yield type("Chunk", (), {"content": "partial", "tool_calls": [], "usage": {}})()
            first_chunk_sent.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                stream_cancelled.set()
                raise
            late_chunk_sent.set()
            yield type("Chunk", (), {"content": "late", "tool_calls": [], "usage": {}})()

    class Factory:
        def create(self, _config_snapshot):
            return ModelFactoryResult(model=AsyncStreamingModel(), error=None)

    monkeypatch.setattr(orchestrator_module, "ModelFactory", Factory)
    start = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert start.runtime is not None
    runtime = start.runtime
    stream_events: list[AgentStreamEvent] = []

    try:
        result_box: dict[str, AgentRunResult] = {}
        turn_thread = threading.Thread(
            target=lambda: result_box.setdefault(
                "result",
                runtime.run_turn("hello", agent_stream_callback=stream_events.append),
            ),
            daemon=True,
        )
        turn_thread.start()
        assert first_chunk_sent.wait(timeout=2)

        cancel_result = runtime.cancel_running_turn()
        turn_thread.join(timeout=2)

        assert cancel_result.error["reason"] == "user_cancel_running"
        assert not turn_thread.is_alive(), {
            "stream_cancelled": stream_cancelled.is_set(),
            "late_chunk_sent": late_chunk_sent.is_set(),
            "stream_events": [event.kind for event in stream_events],
        }
        assert result_box["result"].status == "cancelled"
        assert result_box["result"].error["reason"] == "user_cancel_running"
        assert stream_cancelled.wait(timeout=1)
        assert not late_chunk_sent.is_set()
        assert [
            event.payload["text"]
            for event in stream_events
            if event.kind == "stream_text_delta"
        ] == ["partial"]
        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            conversation = [
                (role, kind, json.loads(content_json))
                for role, kind, content_json in conn.execute(
                    """
                    SELECT role, kind, content_json
                    FROM conversation_messages
                    ORDER BY message_index
                    """
                )
            ]

        assert conversation == [
            ("user", "user_input", {"content": "hello"}),
            (
                "runtime",
                "cancellation_fact",
                {
                    "error_class": "cancelled",
                    "reason": "user_cancel_running",
                    "message": "Turn cancelled by user.",
                    "artifact_ids": [],
                },
            ),
        ]
    finally:
        runtime.close()


class _CollectableHandle:
    def __init__(self, *, collect) -> None:
        self.cancelled = False
        self.collect = collect

    def cancel(self) -> None:
        self.cancelled = True

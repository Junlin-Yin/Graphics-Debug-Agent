from __future__ import annotations

import threading
import time

import pytest

from debug_agent.runtime.provider_execution import (
    ProviderCallCancelled,
    run_provider_call,
    start_provider_call,
    stream_provider_call,
)


def test_run_provider_call_collects_worker_after_cancellation_before_return() -> None:
    cancel_requested = threading.Event()
    provider_finished = threading.Event()
    handles = []

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    def call():
        while not cancel_requested.is_set():
            time.sleep(0.001)
        provider_finished.set()
        return "late"

    def register(handle):
        handles.append(handle)
        cancel_requested.set()

    with pytest.raises(ProviderCallCancelled):
        run_provider_call(
            operation="test",
            provider="fake",
            model="fake-model",
            call=call,
            timeout_seconds=10,
            cancellation_token=Token(),
            register_cancellation_handle=register,
            cleanup_timeout_seconds=1,
        )

    assert provider_finished.is_set()
    assert handles[0].metadata["local_boundary_closed"] is True
    assert handles[0].metadata["late_result_ignored"] is True


def test_stream_provider_call_collects_worker_after_cancellation_before_return() -> None:
    cancel_requested = threading.Event()
    provider_finished = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    def stream():
        while not cancel_requested.is_set():
            time.sleep(0.001)
        yield "late"
        provider_finished.set()

    def register(handle):
        cancel_requested.set()

    with pytest.raises(ProviderCallCancelled):
        list(
            stream_provider_call(
                operation="test_stream",
                provider="fake",
                model="fake-model",
                stream=stream,
                timeout_seconds=10,
                cancellation_token=Token(),
                register_cancellation_handle=register,
                cleanup_timeout_seconds=1,
            )
        )

    assert provider_finished.is_set()


def test_start_provider_call_future_collects_worker_after_cancellation() -> None:
    cancel_requested = threading.Event()
    provider_finished = threading.Event()
    handles = []

    def call():
        while not cancel_requested.is_set():
            time.sleep(0.001)
        provider_finished.set()
        return "late"

    task = start_provider_call(
        operation="view_image",
        provider="openai",
        model="kimi-k2.5",
        call=call,
        timeout_seconds=10,
        cancellation_token=None,
        register_cancellation_handle=handles.append,
        cleanup_timeout_seconds=1,
    )
    cancel_requested.set()
    handles[0].cancel()

    with pytest.raises(ProviderCallCancelled):
        task.result(timeout=10)

    assert provider_finished.is_set()
    assert handles[0].metadata["local_boundary_closed"] is True

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from debug_agent.runtime.provider_execution import (
    ProviderBoundaryNotClosed,
    ProviderCallCancelled,
    run_provider_call,
    start_async_provider_call,
    start_provider_call,
    stream_async_provider_call,
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
    handles = []

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    def stream():
        while not cancel_requested.is_set():
            time.sleep(0.001)
        yield "late"
        provider_finished.set()

    def register(handle):
        handles.append(handle)
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

    assert not provider_finished.is_set()
    assert handles[0].metadata["local_boundary_closed"] is True
    assert handles[0].metadata["late_result_ignored"] is True


def test_stream_provider_call_enforces_cleanup_timeout_while_chunks_continue() -> None:
    cancel_requested = threading.Event()

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    def stream():
        while True:
            time.sleep(0.001)
            if False:
                yield "unreachable"

    def register(handle):
        cancel_requested.set()

    started_at = time.monotonic()
    with pytest.raises(ProviderBoundaryNotClosed):
        list(
            stream_provider_call(
                operation="test_stream",
                provider="fake",
                model="fake-model",
                stream=stream,
                timeout_seconds=10,
                cancellation_token=Token(),
                register_cancellation_handle=register,
                cleanup_timeout_seconds=0.02,
            )
        )

    assert time.monotonic() - started_at < 0.2


def test_stream_provider_call_stops_iterating_after_cancellation_boundary() -> None:
    cancel_requested = threading.Event()
    provider_finished = threading.Event()
    chunks_seen = 0
    handles = []

    class Token:
        def is_cancelled(self):
            return cancel_requested.is_set()

    def stream():
        nonlocal chunks_seen
        chunks_seen += 1
        yield f"late-{chunks_seen}"
        while not cancel_requested.is_set():
            time.sleep(0.001)
        provider_finished.set()
        while True:
            chunks_seen += 1
            yield f"ignored-{chunks_seen}"

    def register(handle):
        handles.append(handle)

    iterator = stream_provider_call(
        operation="test_stream",
        provider="fake",
        model="fake-model",
        stream=stream,
        timeout_seconds=10,
        cancellation_token=Token(),
        register_cancellation_handle=register,
        cleanup_timeout_seconds=1,
    )

    assert next(iterator) == "late-1"
    cancel_requested.set()
    handles[0].cancel()

    started_at = time.monotonic()
    with pytest.raises(ProviderCallCancelled):
        next(iterator)

    assert time.monotonic() - started_at < 0.2
    assert provider_finished.is_set()
    assert handles[0].metadata["local_boundary_closed"] is True
    assert handles[0].metadata["late_result_ignored"] is True


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


def test_run_async_provider_call_uses_cancellable_async_task() -> None:
    started = threading.Event()
    task_cancelled = threading.Event()
    handles = []

    async def call():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    def register(handle):
        handles.append(handle)

    future = start_async_provider_call(
        operation="async_test",
        provider="fake",
        model="fake-model",
        call=call,
        timeout_seconds=10,
        cancellation_token=None,
        register_cancellation_handle=register,
        cleanup_timeout_seconds=1,
    )
    assert started.wait(timeout=1)
    handles[0].cancel()

    started_at = time.monotonic()
    with pytest.raises(ProviderCallCancelled):
        future.result(timeout=10)

    assert time.monotonic() - started_at < 0.2
    assert task_cancelled.wait(timeout=1)
    assert handles[0].metadata["local_boundary_closed"] is True
    assert handles[0].metadata["late_result_ignored"] is True


def test_stream_async_provider_call_cancels_async_stream_without_durable_late_chunks() -> None:
    handles = []
    yielded_after_cancel = threading.Event()

    async def stream():
        yield "first"
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            yielded_after_cancel.set()
            raise
        yield "late"

    iterator = stream_async_provider_call(
        operation="async_stream",
        provider="fake",
        model="fake-model",
        stream=stream,
        timeout_seconds=10,
        cancellation_token=None,
        register_cancellation_handle=handles.append,
        cleanup_timeout_seconds=1,
    )

    assert next(iterator) == "first"
    handles[0].cancel()

    started_at = time.monotonic()
    with pytest.raises(ProviderCallCancelled):
        next(iterator)

    assert time.monotonic() - started_at < 0.2
    assert yielded_after_cancel.wait(timeout=1)
    assert handles[0].metadata["local_boundary_closed"] is True
    assert handles[0].metadata["late_result_ignored"] is True

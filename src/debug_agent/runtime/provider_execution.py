from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
import queue
import threading
from time import monotonic
from typing import Any


@dataclass
class ProviderCancellationHandle:
    provider: str | None
    model: str | None
    operation: str
    cancel_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    _boundary_collector: Callable[[], None] | None = None
    _cancel_requester: Callable[[], None] | None = None

    def cancel(self) -> None:
        self.cancel_requested = True
        self.metadata.update(provider_cancellation_uncertainty_metadata())
        if self._cancel_requester is not None:
            self._cancel_requester()

    def collect_boundary(self) -> None:
        if self._boundary_collector is not None:
            self._boundary_collector()


class ProviderCallCancelled(KeyboardInterrupt):
    pass


class ProviderBoundaryNotClosed(RuntimeError):
    pass


class ProviderCallTask:
    def __init__(
        self,
        *,
        operation: str,
        provider: str | None,
        model: str | None,
        call: Callable[[], Any],
        timeout_seconds: int | float | None,
        cancellation_token: object | None,
        register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
        cleanup_timeout_seconds: int | float | None,
    ) -> None:
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.cancellation_token = cancellation_token
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.handle = ProviderCancellationHandle(
            provider=provider,
            model=model,
            operation=operation,
            metadata={"operation": operation},
        )
        self.handle._boundary_collector = lambda: self._collect_boundary(ignored=True)
        if register_cancellation_handle is not None:
            register_cancellation_handle(self.handle)
        self._result_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        def invoke() -> None:
            try:
                self._result_queue.put(("ok", call()))
            except BaseException as exc:
                self._result_queue.put(("error", exc))

        self._thread = threading.Thread(target=invoke, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.handle.cancel()

    def result(self, timeout: int | float | None = None) -> Any:
        wait_timeout = self.timeout_seconds if timeout is None else timeout
        deadline = _deadline(wait_timeout)
        while True:
            if self._cancel_observed():
                self.cancel()
                self._collect_boundary(ignored=True)
                raise ProviderCallCancelled(
                    f"{self.operation} provider call cancelled."
                )
            status, value = self._wait_for_result(deadline)
            if status is None:
                self.handle.metadata.update(provider_cancellation_uncertainty_metadata())
                self._collect_boundary(ignored=True)
                raise TimeoutError(
                    f"{self.operation} provider call timed out after {wait_timeout:g} seconds."
                )
            self._mark_boundary_closed()
            if self._cancel_observed():
                self.cancel()
                self.handle.metadata["late_result_ignored"] = True
                raise ProviderCallCancelled(
                    f"{self.operation} provider call cancelled."
                )
            return _unwrap_result(status, value, "Provider call")

    def _cancel_observed(self) -> bool:
        return self.handle.cancel_requested or _token_cancelled(self.cancellation_token)

    def _wait_for_result(
        self, deadline: float | None
    ) -> tuple[str | None, object | None]:
        while True:
            try:
                return self._result_queue.get(timeout=_poll_timeout(deadline))
            except queue.Empty:
                if deadline is not None and monotonic() >= deadline:
                    return None, None

    def _collect_boundary(self, *, ignored: bool) -> None:
        deadline = _deadline(self.cleanup_timeout_seconds)
        while True:
            try:
                status, value = self._result_queue.get(timeout=_poll_timeout(deadline))
                break
            except queue.Empty:
                if deadline is not None and monotonic() >= deadline:
                    self.handle.metadata["local_boundary_closed"] = False
                    raise ProviderBoundaryNotClosed(
                        f"{self.operation} provider worker did not close locally."
                    )
        self._mark_boundary_closed()
        if ignored:
            self.handle.metadata["late_result_ignored"] = True
        if status == "error" and isinstance(value, ProviderBoundaryNotClosed):
            raise value

    def _mark_boundary_closed(self) -> None:
        self.handle.metadata["local_boundary_closed"] = True
        self.handle.metadata["local_worker_alive"] = self._thread.is_alive()


class AsyncProviderCallTask:
    def __init__(
        self,
        *,
        operation: str,
        provider: str | None,
        model: str | None,
        call: Callable[[], Any],
        timeout_seconds: int | float | None,
        cancellation_token: object | None,
        register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
        cleanup_timeout_seconds: int | float | None,
    ) -> None:
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.cancellation_token = cancellation_token
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.handle = ProviderCancellationHandle(
            provider=provider,
            model=model,
            operation=operation,
            metadata={"operation": operation, "async_provider_task": True},
        )
        self.handle._boundary_collector = lambda: self._collect_boundary(ignored=True)
        self.handle._cancel_requester = self._request_task_cancel
        self._result_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._task_ready = threading.Event()
        if register_cancellation_handle is not None:
            register_cancellation_handle(self.handle)
        self._thread = threading.Thread(target=self._run_loop, args=(call,), daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.handle.cancel()

    def result(self, timeout: int | float | None = None) -> Any:
        wait_timeout = self.timeout_seconds if timeout is None else timeout
        deadline = _deadline(wait_timeout)
        while True:
            if self._cancel_observed():
                self.cancel()
                self._collect_boundary(ignored=True)
                raise ProviderCallCancelled(
                    f"{self.operation} provider call cancelled."
                )
            status, value = self._wait_for_result(deadline)
            if status is None:
                self.handle.metadata.update(provider_cancellation_uncertainty_metadata())
                self.cancel()
                self._collect_boundary(ignored=True)
                raise TimeoutError(
                    f"{self.operation} provider call timed out after {wait_timeout:g} seconds."
                )
            self._mark_boundary_closed()
            if status == "cancelled":
                self.handle.metadata["late_result_ignored"] = True
                raise ProviderCallCancelled(
                    f"{self.operation} provider call cancelled."
                )
            if self._cancel_observed():
                self.cancel()
                self.handle.metadata["late_result_ignored"] = True
                raise ProviderCallCancelled(
                    f"{self.operation} provider call cancelled."
                )
            return _unwrap_result(status, value, "Async provider call")

    def _run_loop(self, call: Callable[[], Any]) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            task = loop.create_task(call())
            self._task = task
            self._task_ready.set()
            try:
                result = loop.run_until_complete(task)
            except asyncio.CancelledError:
                self._result_queue.put(("cancelled", None))
                return
            self._result_queue.put(("ok", result))
        except BaseException as exc:
            _mark_async_task_exception_retrieved(self._task)
            self._result_queue.put(("error", exc))
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    def _request_task_cancel(self) -> None:
        self._task_ready.wait(timeout=0.1)
        loop = self._loop
        task = self._task
        if loop is None or task is None or task.done():
            return
        loop.call_soon_threadsafe(task.cancel)

    def _cancel_observed(self) -> bool:
        return self.handle.cancel_requested or _token_cancelled(self.cancellation_token)

    def _wait_for_result(
        self, deadline: float | None
    ) -> tuple[str | None, object | None]:
        while True:
            try:
                return self._result_queue.get(timeout=_poll_timeout(deadline))
            except queue.Empty:
                if deadline is not None and monotonic() >= deadline:
                    return None, None

    def _collect_boundary(self, *, ignored: bool) -> None:
        deadline = _deadline(self.cleanup_timeout_seconds)
        self._request_task_cancel()
        while True:
            try:
                status, value = self._result_queue.get(timeout=_poll_timeout(deadline))
                break
            except queue.Empty:
                if deadline is not None and monotonic() >= deadline:
                    self.handle.metadata["local_boundary_closed"] = False
                    raise ProviderBoundaryNotClosed(
                        f"{self.operation} async provider task did not close locally."
                    )
        self._mark_boundary_closed()
        if ignored:
            self.handle.metadata["late_result_ignored"] = True
        if status == "error" and isinstance(value, ProviderBoundaryNotClosed):
            raise value

    def _mark_boundary_closed(self) -> None:
        self.handle.metadata["local_boundary_closed"] = True
        self.handle.metadata["local_worker_alive"] = self._thread.is_alive()


def provider_cancellation_uncertainty_metadata() -> dict[str, bool]:
    return {
        "local_cancel_requested": True,
        "local_wait_stopped": True,
        "remote_stop_uncertain": True,
        "billing_stop_uncertain": True,
    }


def start_provider_call(
    *,
    operation: str,
    provider: str | None,
    model: str | None,
    call: Callable[[], Any],
    timeout_seconds: int | float | None,
    cancellation_token: object | None,
    register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
    cleanup_timeout_seconds: int | float | None = 1,
) -> ProviderCallTask:
    return ProviderCallTask(
        operation=operation,
        provider=provider,
        model=model,
        call=call,
        timeout_seconds=timeout_seconds,
        cancellation_token=cancellation_token,
        register_cancellation_handle=register_cancellation_handle,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )


def start_async_provider_call(
    *,
    operation: str,
    provider: str | None,
    model: str | None,
    call: Callable[[], Any],
    timeout_seconds: int | float | None,
    cancellation_token: object | None,
    register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
    cleanup_timeout_seconds: int | float | None = 1,
) -> AsyncProviderCallTask:
    return AsyncProviderCallTask(
        operation=operation,
        provider=provider,
        model=model,
        call=call,
        timeout_seconds=timeout_seconds,
        cancellation_token=cancellation_token,
        register_cancellation_handle=register_cancellation_handle,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )


def run_async_provider_call(
    *,
    operation: str,
    provider: str | None,
    model: str | None,
    call: Callable[[], Any],
    timeout_seconds: int | float | None,
    cancellation_token: object | None,
    register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
    cleanup_timeout_seconds: int | float | None = 1,
) -> Any:
    return start_async_provider_call(
        operation=operation,
        provider=provider,
        model=model,
        call=call,
        timeout_seconds=timeout_seconds,
        cancellation_token=cancellation_token,
        register_cancellation_handle=register_cancellation_handle,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    ).result()


def run_provider_call(
    *,
    operation: str,
    provider: str | None,
    model: str | None,
    call: Callable[[], Any],
    timeout_seconds: int | float | None,
    cancellation_token: object | None,
    register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
    cleanup_timeout_seconds: int | float | None = 1,
) -> Any:
    return start_provider_call(
        operation=operation,
        provider=provider,
        model=model,
        call=call,
        timeout_seconds=timeout_seconds,
        cancellation_token=cancellation_token,
        register_cancellation_handle=register_cancellation_handle,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    ).result()


def stream_provider_call(
    *,
    operation: str,
    provider: str | None,
    model: str | None,
    stream: Callable[[], Iterator[Any]],
    timeout_seconds: int | float | None,
    cancellation_token: object | None,
    register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
    cleanup_timeout_seconds: int | float | None = 1,
) -> Iterator[Any]:
    handle = ProviderCancellationHandle(
        provider=provider,
        model=model,
        operation=operation,
        metadata={"operation": operation},
    )
    if register_cancellation_handle is not None:
        register_cancellation_handle(handle)
    result_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()

    def invoke() -> None:
        iterator: Iterator[Any] | None = None
        try:
            iterator = iter(stream())
            for chunk in iterator:
                if _stream_cancel_observed(handle, cancellation_token):
                    _close_stream_iterator(iterator, handle)
                    result_queue.put(("cancelled", None))
                    return
                result_queue.put(("chunk", chunk))
                if _stream_cancel_observed(handle, cancellation_token):
                    _close_stream_iterator(iterator, handle)
                    result_queue.put(("cancelled", None))
                    return
            result_queue.put(("ok", None))
        except BaseException as exc:
            result_queue.put(("error", exc))

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    handle._boundary_collector = lambda: _collect_stream_boundary(
        result_queue,
        handle,
        thread,
        cleanup_timeout_seconds,
        ignored=True,
    )
    while True:
        deadline = _deadline(timeout_seconds)
        while True:
            if handle.cancel_requested or _token_cancelled(cancellation_token):
                handle.cancel()
                _collect_stream_boundary(
                    result_queue,
                    handle,
                    thread,
                    cleanup_timeout_seconds,
                    ignored=True,
                )
                raise ProviderCallCancelled(f"{operation} provider call cancelled.")
            try:
                status, value = result_queue.get(timeout=_poll_timeout(deadline))
            except queue.Empty as exc:
                if deadline is not None and monotonic() >= deadline:
                    handle.metadata.update(provider_cancellation_uncertainty_metadata())
                    _collect_stream_boundary(
                        result_queue,
                        handle,
                        thread,
                        cleanup_timeout_seconds,
                        ignored=True,
                    )
                    raise TimeoutError(
                        f"{operation} provider stream timed out after {timeout_seconds:g} seconds."
                    ) from exc
                continue
            break
        if (
            status == "chunk"
            and (handle.cancel_requested or _token_cancelled(cancellation_token))
        ):
            handle.cancel()
            _collect_stream_boundary(
                result_queue,
                handle,
                thread,
                cleanup_timeout_seconds,
                ignored=True,
            )
            raise ProviderCallCancelled(f"{operation} provider call cancelled.")
        if status == "chunk":
            yield value
            continue
        if status == "ok":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            return
        if status == "cancelled":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            handle.metadata["late_result_ignored"] = True
            raise ProviderCallCancelled(f"{operation} provider call cancelled.")
        if status == "error":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            _unwrap_result(status, value, "Provider stream")
        raise RuntimeError(f"Unsupported provider stream result status: {status}")


def stream_async_provider_call(
    *,
    operation: str,
    provider: str | None,
    model: str | None,
    stream: Callable[[], AsyncIterator[Any]],
    timeout_seconds: int | float | None,
    cancellation_token: object | None,
    register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None,
    cleanup_timeout_seconds: int | float | None = 1,
) -> Iterator[Any]:
    handle = ProviderCancellationHandle(
        provider=provider,
        model=model,
        operation=operation,
        metadata={"operation": operation, "async_provider_task": True},
    )
    if register_cancellation_handle is not None:
        register_cancellation_handle(handle)
    result_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()
    task_ready = threading.Event()
    loop_holder: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}
    task_holder: dict[str, asyncio.Task[Any] | None] = {"task": None}

    def request_task_cancel() -> None:
        task_ready.wait(timeout=0.1)
        loop = loop_holder["loop"]
        task = task_holder["task"]
        if loop is None or task is None or task.done():
            return
        loop.call_soon_threadsafe(task.cancel)

    def invoke() -> None:
        loop = asyncio.new_event_loop()
        loop_holder["loop"] = loop
        asyncio.set_event_loop(loop)

        async def consume() -> None:
            try:
                async for chunk in stream():
                    if _stream_cancel_observed(handle, cancellation_token):
                        result_queue.put(("cancelled", None))
                        return
                    result_queue.put(("chunk", chunk))
                    if _stream_cancel_observed(handle, cancellation_token):
                        result_queue.put(("cancelled", None))
                        return
                result_queue.put(("ok", None))
            except asyncio.CancelledError:
                result_queue.put(("cancelled", None))
            except BaseException as exc:
                result_queue.put(("error", exc))

        try:
            task = loop.create_task(consume())
            task_holder["task"] = task
            task_ready.set()
            loop.run_until_complete(task)
        except BaseException as exc:
            _mark_async_task_exception_retrieved(task_holder["task"])
            result_queue.put(("error", exc))
        finally:
            try:
                loop.close()
            finally:
                loop_holder["loop"] = None

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    handle._cancel_requester = request_task_cancel
    handle._boundary_collector = lambda: _collect_async_stream_boundary(
        result_queue,
        handle,
        thread,
        request_task_cancel,
        cleanup_timeout_seconds,
        ignored=True,
    )
    while True:
        deadline = _deadline(timeout_seconds)
        while True:
            if handle.cancel_requested or _token_cancelled(cancellation_token):
                handle.cancel()
                _collect_async_stream_boundary(
                    result_queue,
                    handle,
                    thread,
                    request_task_cancel,
                    cleanup_timeout_seconds,
                    ignored=True,
                )
                raise ProviderCallCancelled(f"{operation} provider call cancelled.")
            try:
                status, value = result_queue.get(timeout=_poll_timeout(deadline))
            except queue.Empty as exc:
                if deadline is not None and monotonic() >= deadline:
                    handle.metadata.update(provider_cancellation_uncertainty_metadata())
                    handle.cancel()
                    _collect_async_stream_boundary(
                        result_queue,
                        handle,
                        thread,
                        request_task_cancel,
                        cleanup_timeout_seconds,
                        ignored=True,
                    )
                    raise TimeoutError(
                        f"{operation} provider stream timed out after {timeout_seconds:g} seconds."
                    ) from exc
                continue
            break
        if (
            status == "chunk"
            and (handle.cancel_requested or _token_cancelled(cancellation_token))
        ):
            handle.cancel()
            _collect_async_stream_boundary(
                result_queue,
                handle,
                thread,
                request_task_cancel,
                cleanup_timeout_seconds,
                ignored=True,
            )
            raise ProviderCallCancelled(f"{operation} provider call cancelled.")
        if status == "chunk":
            yield value
            continue
        if status == "ok":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            return
        if status == "cancelled":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            handle.metadata["late_result_ignored"] = True
            raise ProviderCallCancelled(f"{operation} provider call cancelled.")
        if status == "error":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            _unwrap_result(status, value, "Async provider stream")
        raise RuntimeError(f"Unsupported async provider stream result status: {status}")


def _collect_stream_boundary(
    result_queue: queue.Queue[tuple[str, object | None]],
    handle: ProviderCancellationHandle,
    thread: threading.Thread,
    cleanup_timeout_seconds: int | float | None,
    *,
    ignored: bool,
) -> None:
    deadline = _deadline(cleanup_timeout_seconds)
    while True:
        if deadline is not None and monotonic() >= deadline:
            handle.metadata["local_boundary_closed"] = False
            raise ProviderBoundaryNotClosed(
                f"{handle.operation} provider stream did not close locally."
            )
        try:
            status, value = result_queue.get(timeout=_poll_timeout(deadline))
        except queue.Empty:
            if deadline is not None and monotonic() >= deadline:
                handle.metadata["local_boundary_closed"] = False
                raise ProviderBoundaryNotClosed(
                    f"{handle.operation} provider stream did not close locally."
                )
            continue
        if status == "chunk":
            continue
        if status == "cancelled":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            if ignored:
                handle.metadata["late_result_ignored"] = True
            return
        handle.metadata["local_boundary_closed"] = True
        handle.metadata["local_worker_alive"] = thread.is_alive()
        if ignored:
            handle.metadata["late_result_ignored"] = True
        if status == "error" and isinstance(value, ProviderBoundaryNotClosed):
            raise value
        return


def _collect_async_stream_boundary(
    result_queue: queue.Queue[tuple[str, object | None]],
    handle: ProviderCancellationHandle,
    thread: threading.Thread,
    request_task_cancel: Callable[[], None],
    cleanup_timeout_seconds: int | float | None,
    *,
    ignored: bool,
) -> None:
    request_task_cancel()
    _collect_stream_boundary(
        result_queue,
        handle,
        thread,
        cleanup_timeout_seconds,
        ignored=ignored,
    )


def _mark_async_task_exception_retrieved(task: asyncio.Task[Any] | None) -> None:
    if task is None or not task.done() or task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


def _unwrap_result(status: str, value: object | None, source: str) -> Any:
    if status == "ok":
        return value
    if status == "error":
        if not isinstance(value, BaseException):
            raise RuntimeError(f"{source} failed without returning an exception.")
        if isinstance(value, KeyboardInterrupt):
            raise ProviderCallCancelled(str(value) or f"{source} cancelled.") from value
        raise value
    raise RuntimeError(f"Unsupported {source.lower()} result status: {status}")


def _stream_cancel_observed(
    handle: ProviderCancellationHandle,
    cancellation_token: object | None,
) -> bool:
    return handle.cancel_requested or _token_cancelled(cancellation_token)


def _close_stream_iterator(
    iterator: Iterator[Any],
    handle: ProviderCancellationHandle,
) -> None:
    close = getattr(iterator, "close", None)
    if not callable(close):
        return
    try:
        close()
        handle.metadata["local_stream_close_requested"] = True
    except RuntimeError:
        handle.metadata["local_stream_close_requested"] = False


def _deadline(timeout_seconds: int | float | None) -> float | None:
    if timeout_seconds is None or timeout_seconds <= 0:
        return None
    return monotonic() + float(timeout_seconds)


def _poll_timeout(deadline: float | None) -> float:
    if deadline is None:
        return 0.01
    return max(0.001, min(0.01, deadline - monotonic()))


def _token_cancelled(cancellation_token: object | None) -> bool:
    if cancellation_token is None:
        return False
    is_cancelled = getattr(cancellation_token, "is_cancelled", None)
    if callable(is_cancelled):
        return bool(is_cancelled())
    cancelled = getattr(cancellation_token, "cancelled", None)
    if callable(cancelled):
        return bool(cancelled())
    return bool(getattr(cancellation_token, "cancel_requested", False))

from __future__ import annotations

from collections.abc import Callable, Iterator
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

    def cancel(self) -> None:
        self.cancel_requested = True
        self.metadata.update(provider_cancellation_uncertainty_metadata())


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
        try:
            for chunk in stream():
                result_queue.put(("chunk", chunk))
            result_queue.put(("ok", None))
        except BaseException as exc:
            result_queue.put(("error", exc))

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
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
        if status == "chunk":
            yield value
            continue
        if status == "ok":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            return
        if status == "error":
            handle.metadata["local_boundary_closed"] = True
            handle.metadata["local_worker_alive"] = thread.is_alive()
            _unwrap_result(status, value, "Provider stream")
        raise RuntimeError(f"Unsupported provider stream result status: {status}")


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
        handle.metadata["local_boundary_closed"] = True
        handle.metadata["local_worker_alive"] = thread.is_alive()
        if ignored:
            handle.metadata["late_result_ignored"] = True
        if status == "error" and isinstance(value, ProviderBoundaryNotClosed):
            raise value
        return


def _unwrap_result(status: str, value: object | None, source: str) -> Any:
    if status == "ok":
        return value
    if status == "error":
        if not isinstance(value, BaseException):
            raise RuntimeError(f"{source} failed without returning an exception.")
        raise value
    raise RuntimeError(f"Unsupported {source.lower()} result status: {status}")


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

from __future__ import annotations

import asyncio
import inspect
from typing import Any


_CHILD_RESOURCE_ATTRS = ("_client", "_async_client", "client", "async_client")


def close_provider_resource(resource: Any) -> dict[str, int]:
    closed = 0
    errors = 0
    seen: set[int] = set()
    for candidate in _iter_provider_resources(resource):
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        close = getattr(candidate, "aclose", None)
        if not callable(close):
            close = getattr(candidate, "close", None)
        if not callable(close):
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                _run_awaitable(result)
            closed += 1
        except Exception:
            errors += 1
    return {
        "provider_resources_closed": closed,
        "provider_resource_close_errors": errors,
    }


async def close_provider_resource_async(resource: Any) -> dict[str, int]:
    closed = 0
    errors = 0
    seen: set[int] = set()
    for candidate in _iter_provider_resources(resource):
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        close = getattr(candidate, "aclose", None)
        if not callable(close):
            close = getattr(candidate, "close", None)
        if not callable(close):
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
            closed += 1
        except Exception:
            errors += 1
    return {
        "provider_resources_closed": closed,
        "provider_resource_close_errors": errors,
    }


def _iter_provider_resources(resource: Any):
    yield resource
    for attr in _CHILD_RESOURCE_ATTRS:
        try:
            child = getattr(resource, attr)
        except Exception:
            continue
        if child is not resource:
            yield child


def _run_awaitable(awaitable: Any) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(awaitable)
    finally:
        loop.close()

from __future__ import annotations

import pytest

from debug_agent.runtime.provider_resources import (
    close_provider_resource,
    close_provider_resource_async,
)


def test_close_provider_resource_closes_direct_and_cached_clients() -> None:
    closed: list[str] = []

    class SyncClient:
        def close(self) -> None:
            closed.append("sync")

    class AsyncClient:
        async def close(self) -> None:
            closed.append("async")

    class Model:
        def __init__(self) -> None:
            self._client = SyncClient()
            self._async_client = AsyncClient()

    metadata = close_provider_resource(Model())

    assert closed == ["sync", "async"]
    assert metadata == {
        "provider_resources_closed": 2,
        "provider_resource_close_errors": 0,
    }


def test_close_provider_resource_closes_async_aclose() -> None:
    closed: list[str] = []

    class AsyncClient:
        async def aclose(self) -> None:
            closed.append("aclose")

    metadata = close_provider_resource(AsyncClient())

    assert closed == ["aclose"]
    assert metadata["provider_resources_closed"] == 1
    assert metadata["provider_resource_close_errors"] == 0


@pytest.mark.anyio
async def test_close_provider_resource_async_closes_async_aclose_in_current_loop() -> None:
    closed: list[str] = []

    class AsyncClient:
        async def aclose(self) -> None:
            closed.append("aclose")

    metadata = await close_provider_resource_async(AsyncClient())

    assert closed == ["aclose"]
    assert metadata["provider_resources_closed"] == 1
    assert metadata["provider_resource_close_errors"] == 0

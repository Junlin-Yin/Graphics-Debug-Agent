from __future__ import annotations

import json
import threading
import time

import pytest

from debug_agent.adapters.vision_client import (
    VisionClientConfig,
    VisionImageInput,
    VisionModelClient,
    project_chat_completions_request,
)
from debug_agent.runtime.provider_execution import AsyncProviderCallTask, ProviderCallCancelled


def test_request_projection_contains_kimi_json_shape() -> None:
    images = [
        VisionImageInput(mime_type="image/png", data=b"png-bytes"),
        VisionImageInput(mime_type="image/jpeg", data=b"jpeg-bytes"),
    ]

    body = project_chat_completions_request(
        model="kimi-k2.5",
        images=images,
        instruction="Return JSON.",
        max_tokens=123,
    )

    assert body == {
        "model": "kimi-k2.5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,cG5nLWJ5dGVz"
                        },
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,anBlZy1ieXRlcw=="
                        },
                    },
                    {"type": "text", "text": "Return JSON."},
                ],
            }
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 123,
        "thinking": {"type": "disabled"},
    }
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(encoded) == len(
        b'{"model":"kimi-k2.5","messages":[{"role":"user","content":'
        b'[{"type":"image_url","image_url":{"url":"data:image/png;base64,cG5nLWJ5dGVz"}},'
        b'{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,anBlZy1ieXRlcw=="}},'
        b'{"type":"text","text":"Return JSON."}]}],"response_format":{"type":"json_object"},'
        b'"max_tokens":123,"thinking":{"type":"disabled"}}'
    )


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _Message:
            content = '{"analysis":"looks valid"}'

        class _Choice:
            message = _Message()

        class _Completion:
            choices = [_Choice()]

        return _Completion()


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeOpenAI:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.chat = _FakeChat()


def test_client_uses_single_non_streaming_request_with_timeout_and_no_retry() -> None:
    created: list[_FakeOpenAI] = []

    def factory(**kwargs):
        client = _FakeOpenAI(**kwargs)
        created.append(client)
        return client

    client = VisionModelClient(factory=factory)
    config = VisionClientConfig(
        provider="openai",
        model="kimi-k2.5",
        api_key="secret",
        base_url="https://example.test/v1",
        max_tokens=321,
    )

    response = client.analyze(
        config=config,
        images=[VisionImageInput(mime_type="image/png", data=b"abc")],
        instruction="Return JSON.",
        timeout_seconds=7.5,
    )

    assert response.text == '{"analysis":"looks valid"}'
    assert len(created) == 1
    assert created[0].kwargs == {
        "api_key": "secret",
        "base_url": "https://example.test/v1",
        "timeout": 7.5,
        "max_retries": 0,
    }
    calls = created[0].chat.completions.calls
    assert len(calls) == 1
    assert calls[0]["stream"] is False
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 321


def test_client_async_analysis_registers_cancellation_handle_and_ignores_late_result() -> None:
    started = threading.Event()
    release = threading.Event()
    registered_handles = []

    class _SlowCompletions:
        async def create(self, **_kwargs):
            started.set()
            while not release.is_set():
                await __import__("asyncio").sleep(0.001)

            class _Message:
                content = '{"analysis":"too late"}'

            class _Choice:
                message = _Message()

            class _Completion:
                choices = [_Choice()]

            return _Completion()

    class _SlowAsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.chat = type("Chat", (), {"completions": _SlowCompletions()})()

    client = VisionModelClient(async_factory=_SlowAsyncOpenAI)
    config = VisionClientConfig(
        provider="openai",
        model="kimi-k2.5",
        api_key="secret",
        base_url="https://example.test/v1",
        max_tokens=321,
    )

    future = client.analyze_async(
        config=config,
        images=[VisionImageInput(mime_type="image/png", data=b"abc")],
        instruction="Return JSON.",
        timeout_seconds=7.5,
        cleanup_timeout_seconds=2,
        register_cancellation_handle=registered_handles.append,
    )
    assert started.wait(timeout=1)
    registered_handles[0].cancel()

    with pytest.raises(ProviderCallCancelled, match="view_image provider call cancelled"):
        future.result(timeout=1)
    release.set()
    time.sleep(0.03)

    assert registered_handles[0].cancel_requested is True
    assert registered_handles[0].metadata["remote_stop_uncertain"] is True
    assert registered_handles[0].metadata["billing_stop_uncertain"] is True


def test_client_async_analysis_uses_async_openai_client_path() -> None:
    created_sync = []
    created_async = []

    def sync_factory(**kwargs):
        created_sync.append(kwargs)
        raise AssertionError("sync client must not be used by analyze_async")

    class _AsyncCompletions:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)

            class _Message:
                content = '{"analysis":"async path"}'

            class _Choice:
                message = _Message()

            class _Completion:
                choices = [_Choice()]

            return _Completion()

    class _AsyncOpenAI:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.chat = type("Chat", (), {"completions": _AsyncCompletions()})()

    def async_factory(**kwargs):
        client = _AsyncOpenAI(**kwargs)
        created_async.append(client)
        return client

    client = VisionModelClient(factory=sync_factory, async_factory=async_factory)
    config = VisionClientConfig(
        provider="openai",
        model="kimi-k2.5",
        api_key="secret",
        base_url="https://example.test/v1",
        max_tokens=321,
    )

    future = client.analyze_async(
        config=config,
        images=[VisionImageInput(mime_type="image/png", data=b"abc")],
        instruction="Return JSON.",
        timeout_seconds=7.5,
        cleanup_timeout_seconds=1,
    )

    assert isinstance(future, AsyncProviderCallTask)

    response = future.result(timeout=1)

    assert response.text == '{"analysis":"async path"}'
    assert created_sync == []
    assert len(created_async) == 1
    assert created_async[0].kwargs == {
        "api_key": "secret",
        "base_url": "https://example.test/v1",
        "timeout": 7.5,
        "max_retries": 0,
    }
    calls = created_async[0].chat.completions.calls
    assert calls[0]["stream"] is False
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_client_async_analysis_closes_async_provider_client_before_return() -> None:
    closed_clients = []

    class _AsyncCompletions:
        async def create(self, **_kwargs):
            class _Message:
                content = '{"analysis":"closed"}'

            class _Choice:
                message = _Message()

            class _Completion:
                choices = [_Choice()]

            return _Completion()

    class _AsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.chat = type("Chat", (), {"completions": _AsyncCompletions()})()
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True
            closed_clients.append(self)

    client = VisionModelClient(async_factory=_AsyncOpenAI)
    config = VisionClientConfig(
        provider="openai",
        model="kimi-k2.5",
        api_key="secret",
        base_url="https://example.test/v1",
        max_tokens=321,
    )

    response = client.analyze_async(
        config=config,
        images=[VisionImageInput(mime_type="image/png", data=b"abc")],
        instruction="Return JSON.",
        timeout_seconds=7.5,
        cleanup_timeout_seconds=1,
    ).result(timeout=1)

    assert response.text == '{"analysis":"closed"}'
    assert len(closed_clients) == 1
    assert closed_clients[0].closed is True


def test_client_async_analysis_cancels_blocked_async_provider_task_promptly() -> None:
    started = threading.Event()
    task_cancelled = threading.Event()
    registered_handles = []

    class _AsyncCompletions:
        async def create(self, **_kwargs):
            started.set()
            try:
                await __import__("asyncio").sleep(60)
            except __import__("asyncio").CancelledError:
                task_cancelled.set()
                raise

    class _AsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.chat = type("Chat", (), {"completions": _AsyncCompletions()})()

    client = VisionModelClient(async_factory=_AsyncOpenAI)
    config = VisionClientConfig(
        provider="openai",
        model="kimi-k2.5",
        api_key="secret",
        base_url="https://example.test/v1",
        max_tokens=321,
    )

    future = client.analyze_async(
        config=config,
        images=[VisionImageInput(mime_type="image/png", data=b"abc")],
        instruction="Return JSON.",
        timeout_seconds=7.5,
        cleanup_timeout_seconds=1,
        register_cancellation_handle=registered_handles.append,
    )
    assert started.wait(timeout=1)
    registered_handles[0].cancel()

    started_at = time.monotonic()
    with pytest.raises(ProviderCallCancelled):
        future.result(timeout=1)

    assert time.monotonic() - started_at < 0.2
    assert task_cancelled.wait(timeout=1)
    assert registered_handles[0].metadata["local_boundary_closed"] is True
    assert registered_handles[0].metadata["late_result_ignored"] is True


def test_client_rejects_completion_without_message_content() -> None:
    class _BadCompletions:
        def create(self, **_kwargs):
            class _Completion:
                choices = []

            return _Completion()

    class _BadOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.chat = type("Chat", (), {"completions": _BadCompletions()})()

    client = VisionModelClient(factory=_BadOpenAI)

    with pytest.raises(ValueError, match="completion.choices"):
        client.analyze(
            config=VisionClientConfig(
                provider="openai",
                model="kimi-k2.5",
                api_key="secret",
                base_url="https://example.test/v1",
                max_tokens=1,
            ),
            images=[VisionImageInput(mime_type="image/png", data=b"abc")],
            instruction="Return JSON.",
            timeout_seconds=1,
        )

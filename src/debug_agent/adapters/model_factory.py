from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelFactoryResult:
    model: object | None
    error: dict[str, Any] | None


@dataclass
class FakeModelResponse:
    content: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]


class FakeChatModel:
    def __init__(
        self,
        *,
        response: str = "fake response",
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
        stream_chunks: list[str] | None = None,
        error: Exception | None = None,
        timeout: bool = False,
        cancelled: bool = False,
    ) -> None:
        self.response = response
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.stream_chunks = stream_chunks
        self.error = error
        self.timeout = timeout
        self.cancelled = cancelled
        self.messages: list[dict[str, str]] = []

    def invoke(self, messages: list[dict[str, str]]) -> FakeModelResponse:
        self.messages = messages
        if self.timeout:
            raise TimeoutError("fake model timeout")
        if self.cancelled:
            raise KeyboardInterrupt("fake model cancelled")
        if self.error is not None:
            raise self.error
        tool_calls = list(self.tool_calls)
        self.tool_calls = []
        return FakeModelResponse(
            content=self.response,
            tool_calls=tool_calls,
            usage=self.usage,
        )

    async def ainvoke(self, messages: list[dict[str, str]]) -> FakeModelResponse:
        return self.invoke(messages)

    def stream(self, messages: list[dict[str, str]]):
        self.messages = messages
        if self.timeout:
            raise TimeoutError("fake model timeout")
        if self.cancelled:
            raise KeyboardInterrupt("fake model cancelled")
        if self.error is not None:
            raise self.error
        if self.stream_chunks is None:
            raise NotImplementedError("fake model streaming is not configured")
        last_index = len(self.stream_chunks) - 1
        for index, chunk in enumerate(self.stream_chunks):
            yield FakeModelResponse(
                content=chunk,
                tool_calls=[],
                usage=self.usage if index == last_index else {},
            )

    async def astream(self, messages: list[dict[str, str]]):
        for chunk in self.stream(messages):
            yield chunk


class ModelFactory:
    def create(self, config_snapshot: dict[str, Any]) -> ModelFactoryResult:
        provider = config_snapshot.get("provider")
        if provider == "fake":
            fake_error = config_snapshot.get("fake_error")
            return ModelFactoryResult(
                model=FakeChatModel(
                    response=config_snapshot.get("fake_response", "fake response"),
                    tool_calls=config_snapshot.get("fake_tool_calls"),
                    stream_chunks=config_snapshot.get("fake_stream_chunks"),
                    error=RuntimeError(fake_error) if fake_error else None,
                    timeout=bool(config_snapshot.get("fake_timeout", False)),
                    cancelled=bool(config_snapshot.get("fake_cancelled", False)),
                ),
                error=None,
            )
        if provider != "anthropic":
            return ModelFactoryResult(
                model=None,
                error=_config_error(f"Unsupported provider for Phase 0: {provider}"),
            )
        model_name = config_snapshot.get("model")
        if not model_name:
            return ModelFactoryResult(
                model=None,
                error=_config_error("Missing model for Anthropic provider."),
            )
        api_key_env = config_snapshot.get("auth", {}).get(
            "api_key_env", "ANTHROPIC_API_KEY"
        )
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return ModelFactoryResult(
                model=None,
                error=_config_error(f"Missing auth token in environment variable: {api_key_env}"),
            )
        base_url_env = config_snapshot.get("provider_settings", {}).get("base_url_env")
        base_url = os.environ.get(base_url_env) if base_url_env else None
        kwargs: dict[str, Any] = {
            "model_name": model_name,
            "temperature": config_snapshot.get("temperature"),
            "max_tokens_to_sample": config_snapshot.get("max_tokens"),
            "timeout": config_snapshot.get("timeout_seconds"),
            "api_key": api_key,
        }
        if base_url:
            kwargs["base_url"] = base_url
        ChatAnthropic = _load_chat_anthropic()
        return ModelFactoryResult(model=ChatAnthropic(**kwargs), error=None)


def _load_chat_anthropic():
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic


def _config_error(message: str) -> dict[str, Any]:
    return {
        "error_class": "config_error",
        "message": message,
        "source": "model_factory",
        "recoverable": True,
    }

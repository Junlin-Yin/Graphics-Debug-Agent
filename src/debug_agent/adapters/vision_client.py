from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class VisionImageInput:
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class VisionClientConfig:
    provider: str
    model: str
    api_key: str
    base_url: str
    max_tokens: int


@dataclass(frozen=True)
class VisionModelResponse:
    text: str
    provider_metadata: dict[str, Any]


def project_chat_completions_request(
    *,
    model: str,
    images: list[VisionImageInput],
    instruction: str,
    max_tokens: int,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for image in images:
        encoded = base64.b64encode(image.data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.mime_type};base64,{encoded}",
                },
            }
        )
    content.append({"type": "text", "text": instruction})
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
    }


class VisionModelClient:
    def __init__(self, *, factory: Callable[..., Any] | None = None) -> None:
        self._factory = factory

    def analyze(
        self,
        *,
        config: VisionClientConfig,
        images: list[VisionImageInput],
        instruction: str,
        timeout_seconds: float,
    ) -> VisionModelResponse:
        if config.provider != "openai" or config.model != "kimi-k2.5":
            raise ValueError("Unsupported multimodal provider or model.")
        client = self._make_client(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
        projected = project_chat_completions_request(
            model=config.model,
            images=images,
            instruction=instruction,
            max_tokens=config.max_tokens,
        )
        completion = client.chat.completions.create(
            model=projected["model"],
            messages=projected["messages"],
            response_format=projected["response_format"],
            max_tokens=projected["max_tokens"],
            extra_body={"thinking": projected["thinking"]},
            stream=False,
        )
        text = _completion_text(completion)
        return VisionModelResponse(text=text, provider_metadata={})

    def _make_client(self, **kwargs: Any) -> Any:
        if self._factory is not None:
            return self._factory(**kwargs)
        from openai import OpenAI

        return OpenAI(**kwargs)


def _completion_text(completion: Any) -> str:
    choices = getattr(completion, "choices", None)
    if not choices:
        raise ValueError("completion.choices[0].message.content is missing.")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        raise ValueError("completion.choices[0].message.content is missing.")
    return content

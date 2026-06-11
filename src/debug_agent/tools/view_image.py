from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from concurrent.futures import TimeoutError as FutureTimeoutError

from PIL import Image, UnidentifiedImageError

from debug_agent.adapters.vision_client import (
    VisionClientConfig,
    VisionImageInput,
    VisionModelClient,
    project_chat_completions_request,
)
from debug_agent.runtime.contracts import ToolDefinition, ToolResult
from debug_agent.runtime.errors import NormalizedError
from debug_agent.runtime.provider_execution import (
    ProviderBoundaryNotClosed,
    ProviderCallCancelled,
    ProviderCancellationHandle,
    provider_cancellation_uncertainty_metadata,
)
from debug_agent.tools import settings as tool_settings


@dataclass(frozen=True)
class ViewImageResult:
    status: str
    output: dict[str, Any] | None = None
    error_message: str | None = None
    error_class: str = "tool_error"
    metadata: dict[str, Any] | None = None
    redacted_output: str | None = None
    raw_provider_text: str | None = None


@dataclass(frozen=True)
class ImageFacts:
    path: Path
    display_path: str
    mime_type: str
    data: bytes
    byte_size: int
    sha256: str
    width: int
    height: int

    def display_metadata(self) -> dict[str, Any]:
        return {
            "path": self.display_path,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
        }

    def runtime_metadata(self) -> dict[str, Any]:
        return {
            **self.display_metadata(),
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }


class ViewImageTool:
    def __init__(
        self,
        *,
        vision_client: Any | None = None,
        image_reader: Callable[[Path], bytes] | None = None,
    ) -> None:
        self.vision_client = vision_client or VisionModelClient()
        self.image_reader = image_reader or Path.read_bytes

    def execute(
        self,
        context: Any,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
        cleanup_timeout_seconds: float,
        register_cancellation_handle: Callable[[ProviderCancellationHandle], None] | None = None,
    ) -> ViewImageResult:
        multimodal = _multimodal_config(context.frozen_config)
        query_result = _effective_query(
            arguments=arguments,
            max_query_chars=int(multimodal.get("max_query_chars", 8192)),
        )
        if isinstance(query_result, ViewImageResult):
            return query_result
        effective_query, effective_query_source = query_result

        if arguments.get("_view_image_symlink_escapes"):
            return ViewImageResult(
                status="error",
                error_message="view_image path resolves outside the workspace.",
                error_class="tool_error",
            )

        try:
            images = [self._load_image(Path(path), context.workspace_root) for path in arguments["paths"]]
        except OSError as exc:
            return ViewImageResult(
                status="error",
                error_message=f"Unable to read image: {exc}",
                error_class="tool_error",
            )
        except ValueError as exc:
            return ViewImageResult(
                status="error",
                error_message=str(exc),
                error_class="tool_error",
            )

        instruction = _instruction(effective_query)
        env_result = _client_config_from_env(multimodal)
        if isinstance(env_result, ViewImageResult):
            return env_result
        client_config = env_result
        projected = project_chat_completions_request(
            model=client_config.model,
            images=[
                VisionImageInput(mime_type=image.mime_type, data=image.data)
                for image in images
            ],
            instruction=instruction,
            max_tokens=client_config.max_tokens,
        )
        projected_size = len(
            json.dumps(projected, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        if projected_size > tool_settings.MAX_VIEW_IMAGE_REQUEST_BODY_BYTES:
            return ViewImageResult(
                status="error",
                error_message="Projected vision request body is too large.",
                error_class="tool_error",
                metadata={"projected_request_bytes": projected_size},
            )

        start = monotonic()
        try:
            analyze_async = getattr(self.vision_client, "analyze_async", None)
            if not callable(analyze_async):
                return ViewImageResult(
                    status="error",
                    error_message="Vision provider does not expose async cancellation.",
                    error_class="runtime_error",
                )
            future = analyze_async(
                config=client_config,
                images=[
                    VisionImageInput(mime_type=image.mime_type, data=image.data)
                    for image in images
                ],
                instruction=instruction,
                timeout_seconds=timeout_seconds,
                cleanup_timeout_seconds=cleanup_timeout_seconds,
                register_cancellation_handle=register_cancellation_handle,
                cancellation_token=getattr(context, "cancellation_token", None),
            )
            provider_response = future.result(timeout=timeout_seconds)
        except (TimeoutError, FutureTimeoutError):
            return ViewImageResult(
                status="timeout",
                error_message=f"Tool timed out after {timeout_seconds:g} seconds.",
                error_class="timeout",
            )
        except (ProviderCallCancelled, KeyboardInterrupt) as exc:
            return ViewImageResult(
                status="cancelled",
                error_message=str(exc) or "view_image provider call cancelled.",
                error_class="cancelled",
                metadata={
                    "tool_name": "view_image",
                    "provider_cancellation": provider_cancellation_uncertainty_metadata(),
                },
            )
        except ProviderBoundaryNotClosed:
            raise
        except Exception as exc:
            if _is_openai_api_timeout(exc):
                return ViewImageResult(
                    status="timeout",
                    error_message=f"Tool timed out after {timeout_seconds:g} seconds.",
                    error_class="timeout",
                )
            return ViewImageResult(
                status="error",
                error_message=str(exc),
                error_class="model_error",
            )

        duration_ms = max(0, round((monotonic() - start) * 1000))
        analysis_result = _parse_analysis(
            provider_response.text,
            max_analysis_chars=int(multimodal.get("max_analysis_chars", 8192)),
        )
        if isinstance(analysis_result, ViewImageResult):
            return analysis_result

        display_metadata = [image.display_metadata() for image in images]
        metadata = {
            "tool_name": "view_image",
            "vision_provider": client_config.provider,
            "vision_model": client_config.model,
            "duration_ms": duration_ms,
            "effective_query_source": effective_query_source,
            "projected_request_bytes": projected_size,
            "images": [image.runtime_metadata() for image in images],
        }
        output = {"analysis": analysis_result, "metadata": display_metadata}
        return ViewImageResult(
            status="ok",
            output=output,
            metadata=metadata,
            redacted_output=_redacted_output(analysis_result, display_metadata),
            raw_provider_text=provider_response.text,
        )

    def _load_image(self, path: Path, workspace_root: Path) -> ImageFacts:
        data = self.image_reader(path)
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                fmt = image.format
                width, height = image.size
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("Unsupported or corrupt image file.") from exc
        if fmt == "PNG":
            mime_type = "image/png"
        elif fmt == "JPEG":
            mime_type = "image/jpeg"
        else:
            raise ValueError("Unsupported image type.")
        if (
            width > tool_settings.MAX_VIEW_IMAGE_DIMENSION
            or height > tool_settings.MAX_VIEW_IMAGE_DIMENSION
        ):
            raise ValueError("Image dimensions exceed Phase 2 limits.")
        if width * height > tool_settings.MAX_VIEW_IMAGE_PIXELS:
            raise ValueError("Image pixel count exceeds Phase 2 limits.")
        return ImageFacts(
            path=path,
            display_path=_display_path(path, workspace_root),
            mime_type=mime_type,
            data=data,
            byte_size=len(data),
            sha256=sha256(data).hexdigest(),
            width=width,
            height=height,
        )


def tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name="view_image",
        description="Inspect one to four local PNG or JPEG images.",
        input_schema={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "description": (
                        "Local filesystem paths to PNG or JPEG images to inspect. "
                        "Paths are checked by runtime path policy before files are read."
                    ),
                    "items": {
                        "type": "string",
                        "description": "A local path to one PNG or JPEG image.",
                    },
                    "minItems": 1,
                    "maxItems": 4,
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional analysis focus for the images. If omitted, runtime "
                        "uses a default image-inspection prompt."
                    ),
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        category="native",
        risk_level="read",
        access=["read"],
    )


def tool_result_from_view_image(result: ViewImageResult, *, source: str) -> ToolResult:
    if result.status == "ok":
        return ToolResult(
            status="ok",
            output=result.output,
            error=None,
            artifacts=[],
            metadata=result.metadata or {},
            redacted_output=result.redacted_output,
        )
    error = _view_image_error(result, source=source)
    return ToolResult(
        status=result.status,
        output=None,
        error=error,
        artifacts=[],
        metadata=result.metadata or {},
        redacted_output=None,
    )


def _view_image_error(result: ViewImageResult, *, source: str) -> dict[str, Any]:
    if result.error_class == "cancelled":
        return NormalizedError.create(
            "cancelled",
            "tool_call_cancelled",
            message=result.error_message or "view_image provider call cancelled.",
            scope="tool",
            metadata=dict(result.metadata or {}),
        ).to_dict()
    return {
        "error_class": result.error_class,
        "message": result.error_message or "view_image failed.",
        "source": source,
        "recoverable": True,
    }


def _multimodal_config(frozen_config: dict[str, Any]) -> dict[str, Any]:
    multimodal = frozen_config.get("multimodal") if isinstance(frozen_config, dict) else None
    return multimodal if isinstance(multimodal, dict) else {}


def _effective_query(
    *, arguments: dict[str, Any], max_query_chars: int
) -> tuple[str, str] | ViewImageResult:
    if "query" not in arguments:
        return tool_settings.DEFAULT_VIEW_IMAGE_QUERY, "default"
    query = arguments["query"].strip()
    if not query:
        return ViewImageResult(
            status="denied",
            error_message="query must be non-empty when provided.",
            error_class="user_error",
        )
    if len(query) > max_query_chars:
        return ViewImageResult(
            status="denied",
            error_message="query exceeds max_query_chars.",
            error_class="user_error",
        )
    return query, "assistant"


def _client_config_from_env(multimodal: dict[str, Any]) -> VisionClientConfig | ViewImageResult:
    provider = multimodal.get("provider")
    model = multimodal.get("model")
    api_key_env = multimodal.get("api_key_env")
    base_url_env = multimodal.get("base_url_env")
    if provider != "openai" or model != "kimi-k2.5":
        return ViewImageResult(
            status="error",
            error_message="Unsupported multimodal provider or model.",
            error_class="config_error",
        )
    if not isinstance(api_key_env, str) or not api_key_env:
        return ViewImageResult(
            status="error",
            error_message="Missing API key environment variable name.",
            error_class="config_error",
        )
    if not isinstance(base_url_env, str) or not base_url_env:
        return ViewImageResult(
            status="error",
            error_message="Missing base URL environment variable name.",
            error_class="config_error",
        )
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return ViewImageResult(
            status="error",
            error_message=f"Missing auth token in environment variable: {api_key_env}",
            error_class="config_error",
        )
    base_url = os.environ.get(base_url_env)
    if not base_url:
        return ViewImageResult(
            status="error",
            error_message=f"Missing base URL in environment variable: {base_url_env}",
            error_class="config_error",
        )
    return VisionClientConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=int(multimodal.get("max_tokens", 4096)),
    )


def _parse_analysis(text: str, *, max_analysis_chars: int) -> str | ViewImageResult:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return ViewImageResult(
            status="error",
            error_message="Vision provider response was not valid JSON.",
            error_class="model_error",
        )
    if not isinstance(raw, dict):
        return ViewImageResult(
            status="error",
            error_message="Vision provider response must be a JSON object.",
            error_class="model_error",
        )
    analysis = raw.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        return ViewImageResult(
            status="error",
            error_message="Vision provider response missing non-empty analysis.",
            error_class="model_error",
        )
    analysis = analysis.strip()
    if len(analysis) > max_analysis_chars:
        return ViewImageResult(
            status="error",
            error_message="Vision provider analysis exceeds max_analysis_chars.",
            error_class="model_error",
        )
    return analysis


def _instruction(effective_query: str) -> str:
    return (
        "You are a runtime-owned image inspection tool. Return only a JSON object "
        'with a non-empty string field named "analysis". Report uncertainty and '
        "do not invent unseen details.\n\n"
        f"Analysis focus:\n{effective_query}"
    )


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def _redacted_output(analysis: str, metadata: list[dict[str, Any]]) -> str:
    names = ", ".join(item["path"] for item in metadata)
    return f"{analysis}\nImages: {names}"


def _is_openai_api_timeout(exc: BaseException) -> bool:
    return any(
        cls.__module__.startswith("openai") and cls.__name__ == "APITimeoutError"
        for cls in type(exc).mro()
    )

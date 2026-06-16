from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from typing import Any

from debug_agent.runtime.settings import TOKEN_ESTIMATOR_VERSION


USAGE_ESTIMATOR_VERSION = TOKEN_ESTIMATOR_VERSION


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class ModelCallTokenObservation:
    provider_usage: TokenUsage | None
    estimated_usage: TokenUsage


def normalize_provider_usage(response: object) -> dict[str, int]:
    for raw_usage in _provider_usage_candidates(response):
        usage = _normalize_usage_mapping(raw_usage)
        if usage is not None:
            return usage.to_dict()
    return {}


def estimate_model_call_usage(
    *,
    provider_messages: list[object],
    accepted_output: object,
) -> dict[str, int]:
    input_tokens = _estimate_tokens(_stable_json(provider_messages))
    output_tokens = _estimate_tokens(_stable_json(accepted_output))
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    ).to_dict()


def summarize_model_call_window(
    observations: list[ModelCallTokenObservation],
) -> dict[str, Any]:
    if not observations:
        return {
            "provider_usage_available": True,
            "token_source": "provider",
            "usage": {},
            "estimator_version": None,
        }
    if all(observation.provider_usage is not None for observation in observations):
        usage = _sum_usage(
            observation.provider_usage
            for observation in observations
            if observation.provider_usage is not None
        )
        estimated_usage = _sum_usage(
            observation.estimated_usage for observation in observations
        )
        return {
            "provider_usage_available": True,
            "token_source": "provider",
            "usage": usage.to_dict(),
            "estimated_usage": estimated_usage.to_dict(),
            "estimator_version": None,
        }
    usage = _sum_usage(observation.estimated_usage for observation in observations)
    return {
        "provider_usage_available": False,
        "token_source": "estimated",
        "usage": usage.to_dict(),
        "estimated_usage": usage.to_dict(),
        "estimator_version": USAGE_ESTIMATOR_VERSION,
    }


def token_usage_from_mapping(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    return _normalize_usage_mapping(value)


def _provider_usage_candidates(response: object) -> list[object]:
    candidates: list[object] = [getattr(response, "usage", None)]
    candidates.append(getattr(response, "usage_metadata", None))
    response_metadata = getattr(response, "response_metadata", None)
    if isinstance(response_metadata, dict):
        candidates.append(response_metadata.get("usage"))
    return candidates


def _normalize_usage_mapping(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    input_tokens = _int_value(value, "input_tokens", "prompt_tokens")
    output_tokens = _int_value(value, "output_tokens", "completion_tokens")
    total_tokens = _int_value(value, "total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return TokenUsage(
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        total_tokens=total_tokens if total_tokens is not None else 0,
    )


def _sum_usage(usages: object) -> TokenUsage:
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for usage in usages:
        if not isinstance(usage, TokenUsage):
            continue
        input_tokens += usage.input_tokens
        output_tokens += usage.output_tokens
        total_tokens += usage.total_tokens
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _int_value(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return None


def _stable_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)
    except TypeError:
        return str(value)


def _json_default(value: object) -> object:
    if hasattr(value, "to_json"):
        return value.to_json()
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "__dict__"):
        return {
            key: raw
            for key, raw in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, ceil(len(text) / 4))

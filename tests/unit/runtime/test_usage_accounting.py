from __future__ import annotations

from debug_agent.runtime.usage_accounting import (
    ModelCallTokenObservation,
    estimate_model_call_usage,
    normalize_provider_usage,
    summarize_model_call_window,
    token_usage_from_mapping,
)


def test_normalize_provider_usage_accepts_aliases_and_derives_total() -> None:
    response = type(
        "Response",
        (),
        {
            "usage_metadata": {
                "prompt_tokens": 3,
                "completion_tokens": 5,
            }
        },
    )()

    assert normalize_provider_usage(response) == {
        "input_tokens": 3,
        "output_tokens": 5,
        "total_tokens": 8,
    }


def test_summarize_window_uses_provider_only_when_every_call_has_usage() -> None:
    first = token_usage_from_mapping(
        {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    )
    second = token_usage_from_mapping(
        {"prompt_tokens": 7, "completion_tokens": 11}
    )
    assert first is not None
    assert second is not None

    summary = summarize_model_call_window(
        [
            ModelCallTokenObservation(provider_usage=first, estimated_usage=first),
            ModelCallTokenObservation(provider_usage=second, estimated_usage=second),
        ]
    )

    assert summary == {
        "provider_usage_available": True,
        "token_source": "provider",
        "usage": {"input_tokens": 9, "output_tokens": 14, "total_tokens": 23},
        "estimated_usage": {"input_tokens": 9, "output_tokens": 14, "total_tokens": 23},
        "estimator_version": None,
    }


def test_summarize_window_switches_mixed_usage_to_estimates() -> None:
    provider = token_usage_from_mapping(
        {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300}
    )
    first_estimate = token_usage_from_mapping(
        {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    )
    second_estimate = token_usage_from_mapping(
        {"input_tokens": 7, "output_tokens": 11, "total_tokens": 18}
    )
    assert provider is not None
    assert first_estimate is not None
    assert second_estimate is not None

    summary = summarize_model_call_window(
        [
            ModelCallTokenObservation(
                provider_usage=provider,
                estimated_usage=first_estimate,
            ),
            ModelCallTokenObservation(
                provider_usage=None,
                estimated_usage=second_estimate,
            ),
        ]
    )

    assert summary == {
        "provider_usage_available": False,
        "token_source": "estimated",
        "usage": {"input_tokens": 9, "output_tokens": 14, "total_tokens": 23},
        "estimated_usage": {"input_tokens": 9, "output_tokens": 14, "total_tokens": 23},
        "estimator_version": "deterministic-char-v1",
    }


def test_estimate_model_call_usage_uses_provider_visible_request_and_accepted_output() -> None:
    usage = estimate_model_call_usage(
        provider_messages=[{"role": "user", "content": "visible request"}],
        accepted_output={"content": "accepted output", "tool_calls": []},
    )

    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]

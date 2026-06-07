from __future__ import annotations

import pytest

from debug_agent.runtime.retry import (
    RetryBoundaryFacts,
    RetryController,
    RetryRuleInvalid,
    RetrySpec,
    default_retry_registry,
)


def test_default_retry_registry_matches_phase_3_rules() -> None:
    registry = default_retry_registry()

    assert registry[("model_error", "provider_timeout")] == RetrySpec(
        enabled=True,
        precondition="none",
        strategy="repeat_call",
        max_attempts=2,
        backoff="none",
        backoff_seconds=None,
        comment=(
            "Provider or SDK timeout may be transient. Repeating the model call is "
            "allowed only before a response is accepted."
        ),
    )
    assert registry[("model_error", "model_call_timeout")].max_attempts == 2
    assert registry[("model_error", "provider_rate_limited")].backoff == "fixed"
    assert registry[("model_error", "provider_rate_limited")].backoff_seconds == 2
    assert registry[("model_error", "provider_exception")].precondition == (
        "metadata_transient_true"
    )
    assert registry[("model_error", "provider_exception")].comment == (
        "Only explicitly transient provider transport failures are retryable."
    )
    assert registry[("model_error", "compression_model_failed")].precondition == (
        "metadata_transient_true"
    )
    assert registry[("model_error", "compression_model_failed")].comment == (
        "Compression model transport failures can be retried once. Deterministic "
        "compression budget, input, or output validation failures are not retryable."
    )
    assert registry[("model_error", "output_token_limit_reached")].strategy == (
        "continue_generation"
    )
    assert registry[("model_error", "output_token_limit_reached")].precondition == (
        "text_only_no_tool_fragment"
    )
    assert registry[("model_error", "output_token_limit_reached")].comment == (
        "The provider returned a successful but incomplete assistant text response due "
        "to output token limit. One continuation call may complete it only when the "
        "partial output contains no complete or partial tool-use fragment."
    )
    assert registry[("persistence_error", "sqlite_busy_timeout")].precondition == (
        "sqlite_no_partial_commit"
    )
    assert registry[("persistence_error", "sqlite_busy_timeout")].max_attempts == 3
    assert registry[("persistence_error", "sqlite_busy_timeout")].comment == (
        "SQLite busy timeouts are transient lock contention. Retry only inside the "
        "persistence transaction boundary with a short fixed wait."
    )


@pytest.mark.parametrize(
    "spec",
    [
        RetrySpec(True, "unknown", "repeat_call", 1, "none", None, "bad"),
        RetrySpec(True, "none", "unknown", 1, "none", None, "bad"),
        RetrySpec(True, "none", "repeat_call", 0, "none", None, "bad"),
        RetrySpec(True, "none", "repeat_call", 1, "fixed", None, "bad"),
        RetrySpec(True, "none", "repeat_call", 1, "none", 1, "bad"),
        RetrySpec(True, "none", "continue_generation", 1, "fixed", 1, "bad"),
    ],
)
def test_retry_spec_validation_rejects_invalid_combinations(spec: RetrySpec) -> None:
    with pytest.raises(RetryRuleInvalid):
        spec.validate()


def test_unregistered_error_is_not_retryable() -> None:
    controller = RetryController(default_retry_registry())

    decision = controller.decision(
        error_class="tool_error",
        reason="tool_execution_failed",
        metadata={},
        facts=RetryBoundaryFacts(),
    )

    assert decision.enabled is False
    assert decision.strategy is None


def test_metadata_transient_precondition_is_interpreted_by_controller() -> None:
    controller = RetryController(default_retry_registry())

    denied = controller.decision(
        error_class="model_error",
        reason="provider_exception",
        metadata={"transient": False},
        facts=RetryBoundaryFacts(),
    )
    allowed = controller.decision(
        error_class="model_error",
        reason="provider_exception",
        metadata={"transient": True},
        facts=RetryBoundaryFacts(),
    )

    assert denied.enabled is False
    assert allowed.enabled is True
    assert allowed.strategy == "repeat_call"
    assert allowed.max_attempts == 1


def test_text_only_and_sqlite_preconditions_use_boundary_facts() -> None:
    controller = RetryController(default_retry_registry())

    text_allowed = controller.decision(
        error_class="model_error",
        reason="output_token_limit_reached",
        metadata={"partial_output_kind": "text_only_no_tool_fragment"},
        facts=RetryBoundaryFacts(response_accepted=False, downstream_tool_executed=False),
    )
    text_denied_after_tool = controller.decision(
        error_class="model_error",
        reason="output_token_limit_reached",
        metadata={"partial_output_kind": "text_only_no_tool_fragment"},
        facts=RetryBoundaryFacts(response_accepted=False, downstream_tool_executed=True),
    )
    sqlite_allowed = controller.decision(
        error_class="persistence_error",
        reason="sqlite_busy_timeout",
        metadata={},
        facts=RetryBoundaryFacts(sqlite_partial_commit=False),
    )
    sqlite_denied = controller.decision(
        error_class="persistence_error",
        reason="sqlite_busy_timeout",
        metadata={},
        facts=RetryBoundaryFacts(sqlite_partial_commit=True),
    )

    assert text_allowed.enabled is True
    assert text_allowed.strategy == "continue_generation"
    assert text_denied_after_tool.enabled is False
    assert sqlite_allowed.enabled is True
    assert sqlite_denied.enabled is False

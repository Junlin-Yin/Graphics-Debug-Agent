from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RetryPrecondition = Literal[
    "none",
    "metadata_transient_true",
    "text_only_no_tool_fragment",
    "sqlite_no_partial_commit",
]
RetryStrategy = Literal["repeat_call", "continue_generation"]
RetryBackoff = Literal["none", "fixed"]

_PRECONDITIONS = frozenset(
    {
        "none",
        "metadata_transient_true",
        "text_only_no_tool_fragment",
        "sqlite_no_partial_commit",
    }
)
_STRATEGIES = frozenset({"repeat_call", "continue_generation"})
_BACKOFFS = frozenset({"none", "fixed"})


class RetryRuleInvalid(ValueError):
    pass


@dataclass(frozen=True)
class RetrySpec:
    enabled: bool
    precondition: str
    strategy: str
    max_attempts: int
    backoff: str
    backoff_seconds: int | None
    comment: str

    def validate(self) -> None:
        if self.precondition not in _PRECONDITIONS:
            raise RetryRuleInvalid(f"Unknown retry precondition: {self.precondition}")
        if self.strategy not in _STRATEGIES:
            raise RetryRuleInvalid(f"Unknown retry strategy: {self.strategy}")
        if self.backoff not in _BACKOFFS:
            raise RetryRuleInvalid(f"Unknown retry backoff: {self.backoff}")
        if self.enabled and self.max_attempts <= 0:
            raise RetryRuleInvalid("Enabled retry rules require max_attempts > 0")
        if self.backoff == "none" and self.backoff_seconds is not None:
            raise RetryRuleInvalid("backoff=none requires backoff_seconds=None")
        if self.backoff == "fixed" and (
            not isinstance(self.backoff_seconds, int) or self.backoff_seconds <= 0
        ):
            raise RetryRuleInvalid("backoff=fixed requires positive backoff_seconds")
        if self.strategy == "continue_generation" and self.backoff != "none":
            raise RetryRuleInvalid("continue_generation requires backoff=none")


@dataclass(frozen=True)
class RetryBoundaryFacts:
    response_accepted: bool = False
    downstream_tool_executed: bool = False
    sqlite_partial_commit: bool | None = None


@dataclass(frozen=True)
class RetryDecision:
    enabled: bool
    strategy: str | None = None
    max_attempts: int = 0
    backoff: str = "none"
    backoff_seconds: int | None = None
    precondition: str | None = None
    rule_key: tuple[str, str] | None = None


class RetryController:
    def __init__(self, registry: dict[tuple[str, str], RetrySpec] | None = None) -> None:
        self._registry = dict(registry or default_retry_registry())
        for spec in self._registry.values():
            spec.validate()

    def decision(
        self,
        *,
        error_class: str,
        reason: str,
        metadata: dict[str, object],
        facts: RetryBoundaryFacts,
    ) -> RetryDecision:
        key = (error_class, reason)
        spec = self._registry.get(key)
        if spec is None or not spec.enabled:
            return RetryDecision(enabled=False, rule_key=key)
        if facts.response_accepted or facts.downstream_tool_executed:
            return RetryDecision(enabled=False, rule_key=key)
        if not self._precondition_satisfied(spec, metadata=metadata, facts=facts):
            return RetryDecision(enabled=False, rule_key=key)
        return RetryDecision(
            enabled=True,
            strategy=spec.strategy,
            max_attempts=spec.max_attempts,
            backoff=spec.backoff,
            backoff_seconds=spec.backoff_seconds,
            precondition=spec.precondition,
            rule_key=key,
        )

    def _precondition_satisfied(
        self,
        spec: RetrySpec,
        *,
        metadata: dict[str, object],
        facts: RetryBoundaryFacts,
    ) -> bool:
        if spec.precondition == "none":
            return True
        if spec.precondition == "metadata_transient_true":
            return metadata.get("transient") is True
        if spec.precondition == "text_only_no_tool_fragment":
            return metadata.get("partial_output_kind") == "text_only_no_tool_fragment"
        if spec.precondition == "sqlite_no_partial_commit":
            return facts.sqlite_partial_commit is False
        raise RetryRuleInvalid(f"Unknown retry precondition: {spec.precondition}")


def default_retry_registry() -> dict[tuple[str, str], RetrySpec]:
    return {
        ("model_error", "provider_timeout"): RetrySpec(
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
        ),
        ("model_error", "model_call_timeout"): RetrySpec(
            enabled=True,
            precondition="none",
            strategy="repeat_call",
            max_attempts=2,
            backoff="none",
            backoff_seconds=None,
            comment=(
                "Runtime model-call timeout may be transient. Repeating the model call "
                "is allowed only before any response has been accepted."
            ),
        ),
        ("model_error", "provider_rate_limited"): RetrySpec(
            enabled=True,
            precondition="none",
            strategy="repeat_call",
            max_attempts=1,
            backoff="fixed",
            backoff_seconds=2,
            comment=(
                "Provider rate limits may clear after a short fixed wait. Keep the "
                "budget low to avoid provider pressure."
            ),
        ),
        ("model_error", "provider_exception"): RetrySpec(
            enabled=True,
            precondition="metadata_transient_true",
            strategy="repeat_call",
            max_attempts=1,
            backoff="none",
            backoff_seconds=None,
            comment="Only explicitly transient provider transport failures are retryable.",
        ),
        ("model_error", "compression_model_failed"): RetrySpec(
            enabled=True,
            precondition="metadata_transient_true",
            strategy="repeat_call",
            max_attempts=1,
            backoff="none",
            backoff_seconds=None,
            comment=(
                "Compression model transport failures can be retried once. "
                "Deterministic compression budget, input, or output validation failures "
                "are not retryable."
            ),
        ),
        ("model_error", "output_token_limit_reached"): RetrySpec(
            enabled=True,
            precondition="text_only_no_tool_fragment",
            strategy="continue_generation",
            max_attempts=1,
            backoff="none",
            backoff_seconds=None,
            comment=(
                "The provider returned a successful but incomplete assistant text "
                "response due to output token limit. One continuation call may complete "
                "it only when the partial output contains no complete or partial "
                "tool-use fragment."
            ),
        ),
        ("persistence_error", "sqlite_busy_timeout"): RetrySpec(
            enabled=True,
            precondition="sqlite_no_partial_commit",
            strategy="repeat_call",
            max_attempts=3,
            backoff="fixed",
            backoff_seconds=1,
            comment=(
                "SQLite busy timeouts are transient lock contention. Retry only inside "
                "the persistence transaction boundary with a short fixed wait."
            ),
        ),
    }

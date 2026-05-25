from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from debug_agent.runtime.model_context import ModelContextFrame


CONTINUATION_REASONS = frozenset(
    {
        "initial_model_call",
        "tool_result_continuation",
        "post_compression_continuation",
        "approval_denied_abort",
        "compression_failed_abort",
        "context_limit_abort",
        "final_assistant_response",
    }
)


@dataclass(frozen=True)
class QueryState:
    query_id: str
    session_id: str
    run_id: str
    turn_id: str
    continuation_reason: str
    active_skill_records: list[dict[str, Any]]
    latest_context_estimate: dict[str, Any] | None
    current_approval_mode: str
    latest_model_context_frame: ModelContextFrame | None = None


class QueryControlPlane:
    def __init__(self) -> None:
        self._states: dict[str, QueryState] = {}

    def start_query(
        self,
        *,
        session_id: str,
        run_id: str,
        turn_id: str,
        approval_mode: str,
        active_skill_records: list[dict[str, Any]],
    ) -> QueryState:
        state = QueryState(
            query_id=f"qry_{uuid4().hex}",
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            continuation_reason="initial_model_call",
            active_skill_records=[dict(record) for record in active_skill_records],
            latest_context_estimate=None,
            current_approval_mode=approval_mode,
        )
        self._states[state.query_id] = state
        return state

    def get(self, query_id: str) -> QueryState:
        return self._states[query_id]

    def record_context_estimate(
        self,
        query_id: str,
        *,
        frame: ModelContextFrame,
        estimate_total_tokens: int,
        estimator_version: str,
    ) -> QueryState:
        state = self.get(query_id)
        updated = replace(
            state,
            latest_context_estimate={
                "total_tokens": estimate_total_tokens,
                "estimator_version": estimator_version,
            },
            latest_model_context_frame=frame,
        )
        self._states[query_id] = updated
        return updated

    def record_continuation(self, query_id: str, reason: str) -> QueryState:
        if reason not in CONTINUATION_REASONS:
            raise ValueError(f"Unsupported continuation reason: {reason}")
        state = self.get(query_id)
        updated = replace(state, continuation_reason=reason)
        self._states[query_id] = updated
        return updated

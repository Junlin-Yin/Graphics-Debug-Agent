from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from debug_agent.runtime.model_context import ConversationMessage, ModelContextFrame


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


@dataclass(frozen=True)
class ModelCallGroup:
    model_call_id: str
    turn_id: str | None
    start_seq: int
    end_seq: int
    status: str
    consumed_by_later_model_call: bool
    estimated_tokens: int
    message_ids: list[int]


@dataclass(frozen=True)
class NonEvictableRawSuffix:
    message_ids: set[int]
    model_call_group_ids: set[str]
    retained_message_ids: set[int]
    current_message_ids: set[int]


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

    def derive_model_call_groups(
        self,
        messages: list[ConversationMessage],
    ) -> list[ModelCallGroup]:
        grouped: dict[str, list[ConversationMessage]] = {}
        for message in sorted(messages, key=lambda item: item.seq):
            if message.model_call_id is None:
                continue
            grouped.setdefault(message.model_call_id, []).append(message)

        later_consumed: set[str] = set()
        for message in messages:
            for consumed_id in _consumed_model_call_ids(message):
                later_consumed.add(consumed_id)

        groups: list[ModelCallGroup] = []
        for model_call_id, group_messages in grouped.items():
            start_seq = min(message.seq for message in group_messages)
            end_seq = max(message.seq for message in group_messages)
            groups.append(
                ModelCallGroup(
                    model_call_id=model_call_id,
                    turn_id=group_messages[0].turn_id,
                    start_seq=start_seq,
                    end_seq=end_seq,
                    status=_group_status(group_messages),
                    consumed_by_later_model_call=model_call_id in later_consumed,
                    estimated_tokens=sum(
                        message.estimated_tokens or 0 for message in group_messages
                    ),
                    message_ids=[message.seq for message in group_messages],
                )
            )
        return sorted(groups, key=lambda group: (group.start_seq, group.model_call_id))

    def compute_non_evictable_raw_suffix(
        self,
        messages: list[ConversationMessage],
        *,
        retain_recent_model_calls: int,
        current_messages: list[ConversationMessage] | None = None,
    ) -> NonEvictableRawSuffix:
        groups = self.derive_model_call_groups(messages)
        suffix_group_ids: set[str] = {
            group.model_call_id
            for group in groups
            if group.status == "open" or not group.consumed_by_later_model_call
        }
        completed_groups = [group for group in groups if group.status == "closed"]
        if retain_recent_model_calls > 0:
            suffix_group_ids.update(
                group.model_call_id
                for group in completed_groups[-retain_recent_model_calls:]
            )

        retained_message_ids: set[int] = set()
        for group in groups:
            if group.model_call_id in suffix_group_ids:
                retained_message_ids.update(group.message_ids)
        current_message_ids: set[int] = set()
        for message in current_messages or []:
            current_message_ids.add(message.seq)
        return NonEvictableRawSuffix(
            message_ids=retained_message_ids | current_message_ids,
            model_call_group_ids=suffix_group_ids,
            retained_message_ids=retained_message_ids,
            current_message_ids=current_message_ids,
        )


def _group_status(messages: list[ConversationMessage]) -> str:
    for message in messages:
        if message.metadata.get("streaming") is True:
            return "open"
    tool_call_ids = {
        message.tool_call_id
        for message in messages
        if message.kind == "tool_call" and message.tool_call_id is not None
    }
    terminal_tool_result_ids = {
        message.tool_call_id
        for message in messages
        if message.kind == "tool_result"
        and message.tool_call_id is not None
        and message.metadata.get("terminal", True) is not False
    }
    if tool_call_ids - terminal_tool_result_ids:
        return "open"
    if any(message.metadata.get("pending_tool_call") is True for message in messages):
        return "open"
    return "closed"


def _consumed_model_call_ids(message: ConversationMessage) -> list[str]:
    raw = message.metadata.get("consumed_model_call_ids", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]

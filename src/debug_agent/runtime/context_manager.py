from __future__ import annotations

from dataclasses import dataclass

from debug_agent.runtime.model_context import ConversationMessage
from debug_agent.runtime.query_control import QueryControlPlane


OMITTED_TOOL_RESULT_MARKER = (
    "[Earlier tool result omitted for brevity. See artifact references or trace "
    "for full details.]"
)


@dataclass(frozen=True)
class OmissionResult:
    retained_messages: list[ConversationMessage]
    snapshot_messages: list[ConversationMessage]
    omitted_tool_result_count: int
    artifact_refs: list[str]


class ContextManager:
    def __init__(self, *, query_control: QueryControlPlane | None = None) -> None:
        self._query_control = query_control or QueryControlPlane()

    def omit_old_tool_results(
        self,
        *,
        retained_messages: list[ConversationMessage],
        current_messages: list[ConversationMessage],
        retain_recent_model_calls: int,
    ) -> OmissionResult:
        groups = self._query_control.derive_model_call_groups(retained_messages)
        suffix = self._query_control.compute_non_evictable_raw_suffix(
            retained_messages,
            retain_recent_model_calls=retain_recent_model_calls,
            current_messages=current_messages,
        )
        evictable_group_ids = {
            group.model_call_id
            for group in groups
            if group.status == "closed"
            and group.consumed_by_later_model_call
            and group.model_call_id not in suffix.model_call_group_ids
        }
        omitted_count = 0
        artifact_refs: list[str] = []
        omitted_messages: list[ConversationMessage] = []
        for message in retained_messages:
            if (
                message.kind == "tool_result"
                and message.model_call_id in evictable_group_ids
                and message.seq not in suffix.retained_message_ids
                and message.content != OMITTED_TOOL_RESULT_MARKER
            ):
                omitted_count += 1
                artifact_refs.extend(message.artifact_refs)
                omitted_messages.append(
                    ConversationMessage(
                        seq=message.seq,
                        role=message.role,
                        kind=message.kind,
                        turn_id=message.turn_id,
                        model_call_id=message.model_call_id,
                        tool_call_id=message.tool_call_id,
                        content=OMITTED_TOOL_RESULT_MARKER,
                        artifact_refs=list(message.artifact_refs),
                        estimated_tokens=message.estimated_tokens,
                        metadata=dict(message.metadata),
                    )
                )
            else:
                omitted_messages.append(message)

        snapshot_messages = [
            message
            for message in omitted_messages
            if message.seq not in suffix.retained_message_ids
        ]
        return OmissionResult(
            retained_messages=omitted_messages,
            snapshot_messages=snapshot_messages,
            omitted_tool_result_count=omitted_count,
            artifact_refs=sorted(set(artifact_refs)),
        )

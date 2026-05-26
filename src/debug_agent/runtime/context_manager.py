from __future__ import annotations

import json
from dataclasses import dataclass

from debug_agent.runtime.model_context import (
    CompressionContextFrame,
    ConversationMessage,
    TokenEstimate,
    TokenEstimator,
)
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


@dataclass(frozen=True)
class CompressionPlan:
    frame: CompressionContextFrame
    selected_model_call_group_ids: list[str]
    evicted_messages: list[ConversationMessage]
    previous_summary_message: ConversationMessage | None
    budget_tokens: int
    estimate: TokenEstimate


class CompressionError(Exception):
    def __init__(self, reason: str, message: str = "compression_failed") -> None:
        super().__init__(message)
        self.reason = reason


COMPRESSION_REQUIRED_FIELDS = {
    "task_goal": str,
    "completed_work": list,
    "inspected_or_modified_files": list,
    "remaining_work": list,
    "next_plan": list,
    "key_decisions": list,
    "constraints": list,
}
COMPRESSION_OPTIONAL_VISIBLE_FIELDS = (
    "visible_artifact_refs",
    "visible_active_skills",
    "visible_loaded_skill_reference_files",
    "visible_policy_or_approval_facts",
)

COMPRESSION_INSTRUCTION_PROMPT = """You are producing a Phase 1 debug-agent continuity summary.
Return only a JSON object. Merge the previous summary and evicted history into
a complete replacement summary, not a delta. Preserve task goal, completed
work, inspected or modified files, remaining work, next plan, key decisions,
constraints, and visible artifact, skill reference, approval, or policy facts
only when already visible in the previous summary or evicted history.
Required schema:
{
  "task_goal": "string",
  "completed_work": ["string"],
  "inspected_or_modified_files": ["string"],
  "remaining_work": ["string"],
  "next_plan": ["string"],
  "key_decisions": ["string"],
  "constraints": ["string"],
  "visible_artifact_refs": ["string"],
  "visible_active_skills": ["string"],
  "visible_loaded_skill_reference_files": ["string"],
  "visible_policy_or_approval_facts": ["string"]
}
"""


class ContextManager:
    def __init__(
        self,
        *,
        query_control: QueryControlPlane | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._query_control = query_control or QueryControlPlane()
        self._token_estimator = token_estimator or TokenEstimator()

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

    def prepare_compression(
        self,
        *,
        retained_messages: list[ConversationMessage],
        current_messages: list[ConversationMessage],
        retain_recent_model_calls: int,
        window_tokens: int,
        compression_reserved_output_tokens: int,
    ) -> CompressionPlan:
        previous_summary = _latest_summary_message(retained_messages)
        groups = self._query_control.derive_model_call_groups(retained_messages)
        suffix = self._query_control.compute_non_evictable_raw_suffix(
            retained_messages,
            retain_recent_model_calls=retain_recent_model_calls,
            current_messages=current_messages,
        )
        eligible_groups = [
            group
            for group in groups
            if group.status == "closed"
            and group.consumed_by_later_model_call
            and group.model_call_id not in suffix.model_call_group_ids
        ]
        if not eligible_groups:
            raise CompressionError("no_evictable_history")
        previous_summary_estimate = 0
        if previous_summary is not None:
            previous_summary_estimate = self._token_estimator.estimate_compression_context_frame(
                CompressionContextFrame(
                    previous_summary=str(previous_summary.content),
                    evicted_messages=[],
                    instruction_segment=self._instruction_segment(),
                )
            ).total_tokens
        prompt_estimate = self._token_estimator.estimate_compression_context_frame(
            CompressionContextFrame(
                previous_summary=None,
                evicted_messages=[],
                instruction_segment=self._instruction_segment(),
            )
        ).total_tokens
        budget_tokens = (
            window_tokens
            - previous_summary_estimate
            - prompt_estimate
            - TokenEstimator.FRAME_STRUCTURAL_TOKENS
            - compression_reserved_output_tokens
        )
        if budget_tokens <= 0:
            raise CompressionError("invalid_budget")

        messages_by_seq = {message.seq: message for message in retained_messages}
        selected_group_ids: list[str] = []
        selected_messages: list[ConversationMessage] = []
        used_tokens = 0
        for group in eligible_groups:
            if group.estimated_tokens > budget_tokens and not selected_group_ids:
                raise CompressionError("oldest_group_too_large")
            if used_tokens + group.estimated_tokens > budget_tokens:
                break
            selected_group_ids.append(group.model_call_id)
            used_tokens += group.estimated_tokens
            selected_messages.extend(
                messages_by_seq[message_id]
                for message_id in group.message_ids
                if message_id in messages_by_seq
            )

        if not selected_messages:
            raise CompressionError("no_evictable_history")

        frame = CompressionContextFrame(
            previous_summary=None
            if previous_summary is None
            else str(previous_summary.content),
            evicted_messages=sorted(selected_messages, key=lambda message: message.seq),
            instruction_segment=self._instruction_segment(),
        )
        estimate = self._token_estimator.estimate_compression_context_frame(frame)
        if estimate.total_tokens + compression_reserved_output_tokens > window_tokens:
            raise CompressionError("input_too_large")
        return CompressionPlan(
            frame=frame,
            selected_model_call_group_ids=selected_group_ids,
            evicted_messages=sorted(selected_messages, key=lambda message: message.seq),
            previous_summary_message=previous_summary,
            budget_tokens=budget_tokens,
            estimate=estimate,
        )

    def parse_continuity_summary(self, output: str) -> dict[str, object]:
        if not output or not output.strip():
            raise CompressionError("invalid_output")
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            raise CompressionError("invalid_output") from exc
        if not isinstance(parsed, dict):
            raise CompressionError("invalid_output")
        summary: dict[str, object] = {}
        for field, field_type in COMPRESSION_REQUIRED_FIELDS.items():
            value = parsed.get(field)
            if not isinstance(value, field_type):
                raise CompressionError("invalid_output")
            if field_type is list and not _is_string_list(value):
                raise CompressionError("invalid_output")
            summary[field] = value
        for field in COMPRESSION_OPTIONAL_VISIBLE_FIELDS:
            value = parsed.get(field, [])
            if not isinstance(value, list) or not _is_string_list(value):
                raise CompressionError("invalid_output")
            summary[field] = value
        return summary

    def canonical_summary_json(self, summary: dict[str, object]) -> str:
        return json.dumps(summary, sort_keys=True, separators=(",", ":"))

    def _instruction_segment(self) -> ConversationMessage:
        return ConversationMessage(
            seq=1_000_000,
            role="system",
            kind="compression_instruction",
            turn_id=None,
            model_call_id=None,
            tool_call_id=None,
            content=COMPRESSION_INSTRUCTION_PROMPT,
        )


def _latest_summary_message(
    messages: list[ConversationMessage],
) -> ConversationMessage | None:
    summaries = [message for message in messages if message.kind == "context_summary"]
    if not summaries:
        return None
    return max(summaries, key=lambda message: message.seq)


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)

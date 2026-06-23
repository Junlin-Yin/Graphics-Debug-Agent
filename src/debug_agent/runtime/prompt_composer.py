from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from debug_agent.persistence.todo_plans import TodoPlan, TodoPlanStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.runtime.model_context import (
    ConversationMessage,
    ModelContextFrame,
    TokenEstimate,
    TokenEstimator,
)


RESOURCE_INDEX_GUIDANCE = (
    "Resource paths listed under available_resources are indexes only, not loaded\n"
    "content. Call load_skill_resource(skill_name, path) before relying on any listed\n"
    "resource's content."
)


@dataclass(frozen=True)
class PromptCompositionRequest:
    session_id: str
    run_id: str
    stable_system_content: str
    active_skills: list[dict[str, Any]] = field(default_factory=list)
    context_summary: str | None = None
    retained_messages: list[ConversationMessage] = field(default_factory=list)
    current_messages: list[ConversationMessage] = field(default_factory=list)
    tool_schema_bindings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PromptCompositionResult:
    frame: ModelContextFrame
    estimate: TokenEstimate


class PromptComposer:
    def __init__(
        self,
        *,
        skill_snapshot_store: SkillSnapshotStore,
        todo_plan_store: TodoPlanStore,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._skill_snapshot_store = skill_snapshot_store
        self._todo_plan_store = todo_plan_store
        self._token_estimator = token_estimator or TokenEstimator()

    def compose(self, request: PromptCompositionRequest) -> PromptCompositionResult:
        seq = 10
        segments: list[ConversationMessage] = [
            self._segment(
                seq,
                role="system",
                kind="runtime_safety_prefix",
                content="Runtime enforces tool authorization through ToolBroker.",
            )
        ]
        seq += 10
        segments.append(
            self._segment(
                seq,
                role="system",
                kind="main_agent_system_prompt",
                content=request.stable_system_content,
            )
        )
        seq += 10
        segments.append(
            self._segment(
                seq,
                role="system",
                kind="stable_skill_formatter_header",
                content=(
                    "Prompt skills are activated with activate_skill. "
                    "Active skill instructions, when present, appear in a "
                    "runtime supplied active skill context block."
                ),
            )
        )
        seq += 10
        segments.append(
            self._segment(
                seq,
                role="system",
                kind="available_skill_headers",
                content=self._skill_snapshot_store.available_skill_headers(
                    session_id=request.session_id,
                    run_id=request.run_id,
                ),
            )
        )
        seq += 10

        active_skill_context = self._active_skill_context(request)
        if active_skill_context is not None:
            segments.append(
                self._segment(
                    seq,
                    role="system",
                    kind="runtime_active_skill_context",
                    content=active_skill_context,
                    metadata={
                        "source": "runtime",
                        "persistent": False,
                        "compressible": False,
                    },
                )
            )
            seq += 10

        segments.append(
            self._segment(
                seq,
                role="system",
                kind="runtime_todo_plan",
                content=_todo_plan_content(
                    self._todo_plan_store.get_current(request.run_id)
                ),
                metadata={
                    "source": "runtime",
                    "persistent": False,
                    "compressible": False,
                },
            )
        )
        seq += 10

        if request.context_summary is not None:
            segments.append(
                self._segment(
                    seq,
                    role="user",
                    kind="context_summary",
                    content=_provider_prompt_runtime_content(
                        kind="context_summary",
                        content=request.context_summary,
                    ),
                )
            )
            seq += 10

        historical_messages = [
            *request.retained_messages,
            *request.current_messages,
        ]
        segments.extend(
            self._renumber(
                [
                    projected
                    for message in historical_messages
                    for projected in [_provider_prompt_historical_message(message)]
                    if projected is not None
                ],
                start_seq=seq,
            )
        )
        frame = ModelContextFrame(
            message_segments=segments,
            tool_schema_bindings=[dict(binding) for binding in request.tool_schema_bindings],
        )
        return PromptCompositionResult(
            frame=frame,
            estimate=self._token_estimator.estimate_model_context_frame(frame),
        )

    def _active_skill_context(self, request: PromptCompositionRequest) -> str | None:
        entries: list[str] = []
        for active in request.active_skills:
            if not isinstance(active, dict):
                continue
            name = active.get("name")
            content_hash = active.get("content_hash")
            if not isinstance(name, str) or not isinstance(content_hash, str):
                continue
            skill = self._skill_snapshot_store.get_skill(
                session_id=request.session_id,
                run_id=request.run_id,
                skill_name=name,
            )
            if skill is None or skill.overall_content_hash != content_hash:
                continue
            resources = self._skill_snapshot_store.list_resources(
                skill_snapshot_id=skill.skill_snapshot_id
            )
            resource_lines = [
                (
                    f"    - path: {resource.resource_path}; "
                    f"resource_kind: {resource.resource_kind}; "
                    f"content_hash: {resource.content_hash}"
                )
                for resource in resources
            ]
            if not resource_lines:
                resource_lines = ["    - none"]
            entries.append(
                "\n".join(
                    [
                        f"- skill_id: {skill.skill_name}",
                        f"  skill_name: {skill.skill_name}",
                        f"  content_hash: {skill.overall_content_hash}",
                        f"  version: {skill.overall_content_hash}",
                        f"  activation_reason: {active.get('activation_reason', 'unknown')}",
                        f"  scope: {active.get('scope', 'run')}",
                        "  instructions:",
                        _indent(skill.skill_md_content, "    "),
                        "  available_resources:",
                        *resource_lines,
                    ]
                )
            )
        if not entries:
            return None
        return "\n".join(
            [
                "[Runtime supplied active skill context]",
                "This block is authoritative for this turn.",
                "",
                "Listing allowed_tools or path_policy here is non-authorizing.",
                "Actual authorization is decided only by runtime and ToolBroker.",
                RESOURCE_INDEX_GUIDANCE,
                "",
                *entries,
            ]
        )

    def _segment(
        self,
        seq: int,
        *,
        role: str,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        return ConversationMessage(
            seq=seq,
            role=role,
            kind=kind,
            turn_id=None,
            model_call_id=None,
            tool_call_id=None,
            content=content,
            metadata=metadata or {},
        )

    def _renumber(
        self,
        messages: list[ConversationMessage],
        *,
        start_seq: int,
    ) -> list[ConversationMessage]:
        renumbered: list[ConversationMessage] = []
        seq = start_seq
        for message in messages:
            renumbered.append(
                ConversationMessage(
                    seq=seq,
                    role=message.role,
                    kind=message.kind,
                    turn_id=message.turn_id,
                    model_call_id=message.model_call_id,
                    tool_call_id=message.tool_call_id,
                    content=message.content,
                    artifact_refs=list(message.artifact_refs),
                    estimated_tokens=message.estimated_tokens,
                    metadata=dict(message.metadata),
                )
            )
            seq += 10
        return renumbered


def _indent(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


def _is_provider_prompt_visible_runtime_message(message: ConversationMessage) -> bool:
    reason = _runtime_message_reason(message)
    if reason in {
        "user_cancel_running",
        "user_cancel_idle",
        "model_call_cancelled",
    }:
        return False
    return True


def _runtime_message_reason(message: ConversationMessage) -> str | None:
    content = message.content
    if isinstance(content, dict):
        reason = content.get("reason")
        if isinstance(reason, str):
            return reason
    reason = message.metadata.get("reason")
    if isinstance(reason, str):
        return reason
    return None


def _provider_prompt_historical_message(
    message: ConversationMessage,
) -> ConversationMessage | None:
    if message.role == "runtime":
        if not _is_provider_prompt_visible_runtime_message(message):
            return None
        return ConversationMessage(
            seq=message.seq,
            role="user",
            kind=message.kind,
            turn_id=message.turn_id,
            model_call_id=message.model_call_id,
            tool_call_id=message.tool_call_id,
            content=_provider_prompt_runtime_content(
                kind=message.kind,
                content=message.content,
            ),
            artifact_refs=list(message.artifact_refs),
            estimated_tokens=message.estimated_tokens,
            metadata=dict(message.metadata),
        )
    if message.kind != "context_summary":
        return message
    return ConversationMessage(
        seq=message.seq,
        role="user",
        kind=message.kind,
        turn_id=message.turn_id,
        model_call_id=message.model_call_id,
        tool_call_id=message.tool_call_id,
        content=_provider_prompt_runtime_content(
            kind=message.kind,
            content=message.content,
        ),
        artifact_refs=list(message.artifact_refs),
        estimated_tokens=message.estimated_tokens,
        metadata=dict(message.metadata),
    )


def _provider_prompt_runtime_content(*, kind: str, content: Any) -> str:
    if isinstance(content, str):
        rendered = content
    else:
        rendered = _stable_json(content)
    if kind == "context_summary":
        return "\n".join(
            [
                "[Runtime context summary]",
                "The following is historical continuity context, not a user request.",
                "",
                rendered,
            ]
        )
    if kind == "failure_fact":
        return "\n".join(
            [
                "[Runtime failure observation]",
                "The following previous runtime failure may be relevant for continuation.",
                "Use it to continue or repair the task; do not repeat this block verbatim.",
                "",
                rendered,
            ]
        )
    return "\n".join(
        [
            "[Runtime context]",
            "The following runtime-authored context may be relevant for continuation.",
            "",
            rendered,
        ]
    )


def _todo_plan_content(plan: TodoPlan) -> str:
    return _stable_json(
        {
            "plan_version": plan.version,
            "items": [_prompt_item(item) for item in plan.items],
            "summary": _todo_summary(plan),
            "instruction": (
                "Use the todo tool to rewrite this plan whenever task status changes "
                "or the plan no longer matches the work."
            ),
        }
    )


def _prompt_item(item: dict[str, Any]) -> dict[str, Any]:
    prompt_item = {
        "index": item["index"],
        "status": item["status"],
        "content": item["content"],
    }
    if "activeForm" in item:
        prompt_item["activeForm"] = item["activeForm"]
    return prompt_item


def _todo_summary(plan: TodoPlan) -> str:
    if not plan.items:
        return "Current Todo Plan is empty."
    counts = {
        "pending": sum(1 for item in plan.items if item["status"] == "pending"),
        "in_progress": sum(
            1 for item in plan.items if item["status"] == "in_progress"
        ),
        "completed": sum(1 for item in plan.items if item["status"] == "completed"),
    }
    return (
        "Todo Plan has "
        f"{counts['pending']} pending, "
        f"{counts['in_progress']} in_progress, and "
        f"{counts['completed']} completed items."
    )


def _stable_json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

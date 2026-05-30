from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.runtime.model_context import (
    ConversationMessage,
    ModelContextFrame,
    TokenEstimate,
    TokenEstimator,
)


@dataclass(frozen=True)
class PromptCompositionRequest:
    session_id: str
    run_id: str
    stable_system_content: str
    active_skills: list[dict[str, Any]] = field(default_factory=list)
    context_summary: str | None = None
    retained_messages: list[ConversationMessage] = field(default_factory=list)
    live_messages: list[ConversationMessage] = field(default_factory=list)
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
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._skill_snapshot_store = skill_snapshot_store
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

        if request.context_summary is not None:
            segments.append(
                self._segment(
                    seq,
                    role="system",
                    kind="context_summary",
                    content=request.context_summary,
                )
            )
            seq += 10

        segments.extend(
            self._renumber(
                [
                    *request.retained_messages,
                    *request.live_messages,
                    *request.current_messages,
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

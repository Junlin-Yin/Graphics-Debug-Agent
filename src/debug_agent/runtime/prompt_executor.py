from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.context_snapshots import ContextSnapshotStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.runtime.contracts import (
    AgentLoopAdapter,
    AgentRunRequest,
    AgentRunResult,
    Checkpoint,
    Run,
    RunContext,
    RunEvent,
    Session,
    ToolDefinition,
    utc_now_iso,
)
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.runtime.context_manager import ContextManager
from debug_agent.runtime.model_context import ConversationMessage
from debug_agent.runtime.prompt_composer import PromptComposer, PromptCompositionRequest
from debug_agent.runtime.query_control import QueryControlPlane
from debug_agent.runtime.stream_events import AgentStreamEvent


LARGE_MODEL_CONTENT_THRESHOLD_BYTES = 16 * 1024


@dataclass(frozen=True)
class PromptAgentExecutor:
    event_writer: EventWriter
    checkpoint_store: CheckpointStore
    artifact_store: ArtifactStore
    adapter: AgentLoopAdapter
    tool_definitions: list[ToolDefinition]
    system_prompt: str
    skill_snapshot_store: SkillSnapshotStore
    run_store: RunStore | None = None
    query_control: QueryControlPlane | None = None

    def run_turn(
        self,
        *,
        session: Session,
        run: Run,
        user_input: str,
        workspace_root: str,
        conversation: list[dict[str, Any]] | None = None,
        prompt_turn_counter: int = 1,
        agent_stream_callback: Callable[[AgentStreamEvent], None] | None = None,
    ) -> AgentRunResult:
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="user_message",
            payload={"content": user_input},
        )
        query_control = self.query_control or QueryControlPlane()
        query_state_box: dict[str, Any] = {}
        context_estimate_box: dict[str, Any] = {}
        context_estimate_history: list[dict[str, Any]] = []
        continuation_history: list[str] = ["initial_model_call"]
        query_state = query_control.start_query(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=f"turn-{prompt_turn_counter}",
            approval_mode=session.approval_mode,
            active_skill_records=run.active_skills,
        )
        query_state_box["state"] = query_state
        current_messages = [
            ConversationMessage(
                seq=0,
                role="user",
                kind="current_user_input",
                turn_id=query_state.turn_id,
                model_call_id=None,
                tool_call_id=None,
                content=user_input,
            )
        ]
        retained_messages = _conversation_messages(conversation or [])
        retained_messages_box: dict[str, list[ConversationMessage]] = {
            "messages": retained_messages
        }
        optimization_box: dict[str, dict[str, Any] | None] = {"optimization": None}
        composition = self._compose_frame(
            session=session,
            run=run,
            retained_messages=retained_messages,
            current_messages=current_messages,
        )
        query_state = query_control.record_context_estimate(
            query_state.query_id,
            frame=composition.frame,
            estimate_total_tokens=composition.estimate.total_tokens,
            estimator_version=composition.estimate.estimator_version,
        )
        query_state_box["state"] = query_state
        context_estimate_box["estimate"] = composition.estimate.to_dict()
        context_estimate_history.append(composition.estimate.to_dict())
        optimization = self._maybe_omit_old_tool_results(
            session=session,
            run=run,
            query_control=query_control,
            query_state=query_state,
            retained_messages=retained_messages,
            current_messages=current_messages,
            initial_estimate=composition.estimate.to_dict(),
            prompt_turn_counter=prompt_turn_counter,
        )
        if optimization is not None:
            retained_messages = optimization["retained_messages"]
            retained_messages_box["messages"] = retained_messages
            composition = self._compose_frame(
                session=session,
                run=self._latest_run(run),
                retained_messages=retained_messages,
                current_messages=current_messages,
            )
            query_state = query_control.record_context_estimate(
                query_state.query_id,
                frame=composition.frame,
                estimate_total_tokens=composition.estimate.total_tokens,
                estimator_version=composition.estimate.estimator_version,
            )
            query_state_box["state"] = query_state
            context_estimate_box["estimate"] = composition.estimate.to_dict()
            context_estimate_history.append(composition.estimate.to_dict())
            optimization["metadata"]["reduced_to_tokens"] = (
                composition.estimate.total_tokens
            )
            optimization["metadata"]["message"] = (
                "Context optimized: reduced from "
                f"{optimization['metadata']['reduced_from_tokens']} to "
                f"{optimization['metadata']['reduced_to_tokens']} tokens by "
                "omitting earlier tool results."
            )
            self._persist_omission(
                session=session,
                run=run,
                optimization=optimization,
                after_estimate=composition.estimate.to_dict(),
                prompt_turn_counter=prompt_turn_counter,
            )
            optimization_box["optimization"] = optimization
        request = AgentRunRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            model_config=session.config_snapshot,
            timeout_seconds=session.config_snapshot.get("timeout_seconds"),
            model_context_frame=composition.frame,
            conversation=[],
            tools=[],
        )
        context = RunContext(
            workspace_root=workspace_root,
            artifact_root=session.artifact_root,
            approval_mode=session.approval_mode,
            cancellation_token=None,
            metadata={
                "skill_snapshot_store": self.skill_snapshot_store,
                "run_store": self.run_store,
                "refresh_model_context_frame": lambda tool_loop_messages: self._record_followup_composition(
                    session=session,
                    run=run,
                    query_control=query_control,
                    query_state_box=query_state_box,
                    context_estimate_box=context_estimate_box,
                    context_estimate_history=context_estimate_history,
                    continuation_history=continuation_history,
                    retained_messages_box=retained_messages_box,
                    current_messages=[
                        *current_messages,
                        *_provider_messages_to_conversation(
                            tool_loop_messages,
                            turn_id=query_state_box["state"].turn_id,
                        ),
                    ],
                    optimization_box=optimization_box,
                    prompt_turn_counter=prompt_turn_counter,
                ),
            },
            model_event_recorder=lambda kind, payload: self._append_model_event(
                session=session,
                run=run,
                kind=kind,
                payload=dict(payload),
            ),
        )
        if agent_stream_callback is None:
            result = self.adapter.run(request, context)
        else:
            result = self.adapter.stream(request, context, agent_stream_callback)
        query_state = query_control.record_continuation(
            query_state_box["state"].query_id,
            "final_assistant_response"
            if result.status == "completed"
            else query_state_box["state"].continuation_reason,
        )
        continuation_history.append(query_state.continuation_reason)
        query_state_box["state"] = query_state
        result = _with_context_metadata(
            result,
            context_estimate=context_estimate_box["estimate"],
            context_estimate_history=context_estimate_history,
            continuation_history=continuation_history,
            query_state=query_state,
            context_optimization=None
            if optimization_box["optimization"] is None
            else optimization_box["optimization"]["metadata"],
            conversation_writeback=None
            if optimization_box["optimization"] is None
            else [
                message.to_dict()
                for message in optimization_box["optimization"]["retained_messages"]
            ],
        )
        if result.status == "completed":
            self._append_event(
                session_id=session.session_id,
                run_id=run.run_id,
                kind="assistant_message",
                payload={"content": result.assistant_output},
            )
            checkpoint = self._save_checkpoint(
                session=session,
                run=run,
                kind="turn",
                state={
                    "session_status": session.status,
                    "run_status": run.status,
                    "prompt_turn_counter": prompt_turn_counter,
                    "latest_model_response_metadata": _serializable_metadata(
                        result.metadata
                    ),
                    "latest_artifact_ids": _artifact_ids(result),
                    "latest_error_summary": None,
                },
                summary=result.assistant_output,
            )
        else:
            error = result.error or {}
            checkpoint = self._save_checkpoint(
                session=session,
                run=run,
                kind="error",
                state={
                    "session_status": session.status,
                    "run_status": run.status,
                    "prompt_turn_counter": prompt_turn_counter - 1,
                    "latest_model_response_metadata": _serializable_metadata(
                        result.metadata
                    ),
                    "latest_artifact_ids": _artifact_ids(result),
                    "latest_error_summary": error.get("message"),
                },
                summary=error.get("message"),
            )
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="checkpoint_written",
            payload={"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
        )
        return result

    def _save_checkpoint(
        self,
        *,
        session: Session,
        run: Run,
        kind: str,
        state: dict[str, Any],
        summary: str | None,
    ) -> Checkpoint:
        return self.checkpoint_store.save(
            Checkpoint(
                checkpoint_id=f"chk_{uuid4().hex}",
                session_id=session.session_id,
                run_id=run.run_id,
                kind=kind,
                state=state,
                summary=summary,
                created_at=utc_now_iso(),
            )
        )

    def _compose_frame(
        self,
        *,
        session: Session,
        run: Run,
        retained_messages: list[ConversationMessage],
        current_messages: list[ConversationMessage],
    ):
        return PromptComposer(skill_snapshot_store=self.skill_snapshot_store).compose(
            PromptCompositionRequest(
                session_id=session.session_id,
                run_id=run.run_id,
                stable_system_content=self.system_prompt,
                active_skills=run.active_skills,
                retained_messages=retained_messages,
                current_messages=current_messages,
                tool_schema_bindings=[
                    definition.to_dict() for definition in self.tool_definitions
                ],
            )
        )

    def _record_followup_composition(
        self,
        *,
        session: Session,
        run: Run,
        query_control: QueryControlPlane,
        query_state_box: dict[str, Any],
        context_estimate_box: dict[str, Any],
        context_estimate_history: list[dict[str, Any]],
        continuation_history: list[str],
        retained_messages_box: dict[str, list[ConversationMessage]],
        current_messages: list[ConversationMessage],
        optimization_box: dict[str, dict[str, Any] | None],
        prompt_turn_counter: int,
    ) -> dict[str, Any]:
        state = query_control.record_continuation(
            query_state_box["state"].query_id,
            "tool_result_continuation",
        )
        continuation_history.append(state.continuation_reason)
        composition = self._compose_frame(
            session=session,
            run=self._latest_run(run),
            retained_messages=retained_messages_box["messages"],
            current_messages=current_messages,
        )
        state = query_control.record_context_estimate(
            state.query_id,
            frame=composition.frame,
            estimate_total_tokens=composition.estimate.total_tokens,
            estimator_version=composition.estimate.estimator_version,
        )
        query_state_box["state"] = state
        context_estimate_box["estimate"] = composition.estimate.to_dict()
        context_estimate_history.append(composition.estimate.to_dict())
        optimization = self._maybe_omit_old_tool_results(
            session=session,
            run=run,
            query_control=query_control,
            query_state=state,
            retained_messages=retained_messages_box["messages"],
            current_messages=current_messages,
            initial_estimate=composition.estimate.to_dict(),
            prompt_turn_counter=prompt_turn_counter,
        )
        if optimization is not None:
            retained_messages_box["messages"] = optimization["retained_messages"]
            composition = self._compose_frame(
                session=session,
                run=self._latest_run(run),
                retained_messages=retained_messages_box["messages"],
                current_messages=current_messages,
            )
            state = query_control.record_context_estimate(
                state.query_id,
                frame=composition.frame,
                estimate_total_tokens=composition.estimate.total_tokens,
                estimator_version=composition.estimate.estimator_version,
            )
            query_state_box["state"] = state
            context_estimate_box["estimate"] = composition.estimate.to_dict()
            context_estimate_history.append(composition.estimate.to_dict())
            optimization["metadata"]["reduced_to_tokens"] = (
                composition.estimate.total_tokens
            )
            optimization["metadata"]["message"] = (
                "Context optimized: reduced from "
                f"{optimization['metadata']['reduced_from_tokens']} to "
                f"{optimization['metadata']['reduced_to_tokens']} tokens by "
                "omitting earlier tool results."
            )
            self._persist_omission(
                session=session,
                run=run,
                optimization=optimization,
                after_estimate=composition.estimate.to_dict(),
                prompt_turn_counter=prompt_turn_counter,
            )
            optimization_box["optimization"] = optimization
        return {
            "frame": composition.frame,
            "estimate": composition.estimate.to_dict(),
            "query_state": state,
        }

    def _maybe_omit_old_tool_results(
        self,
        *,
        session: Session,
        run: Run,
        query_control: QueryControlPlane,
        query_state: Any,
        retained_messages: list[ConversationMessage],
        current_messages: list[ConversationMessage],
        initial_estimate: dict[str, Any],
        prompt_turn_counter: int,
    ) -> dict[str, Any] | None:
        context_settings = session.config_snapshot.get("context", {})
        window_tokens = int(context_settings.get("window_tokens", 200000))
        omit_ratio = float(context_settings.get("omit_old_tool_results_at_ratio", 0.60))
        if initial_estimate["total_tokens"] <= omit_ratio * window_tokens:
            return None
        retain_recent_model_calls = int(
            context_settings.get("retain_recent_model_calls", 4)
        )
        omission = ContextManager(query_control=query_control).omit_old_tool_results(
            retained_messages=retained_messages,
            current_messages=current_messages,
            retain_recent_model_calls=retain_recent_model_calls,
        )
        if omission.omitted_tool_result_count == 0:
            return None
        return {
            "retained_messages": omission.retained_messages,
            "snapshot_messages": omission.snapshot_messages,
            "artifact_refs": omission.artifact_refs,
            "before_estimate": dict(initial_estimate),
            "metadata": {
                "trigger": "omission",
                "omitted_tool_result_count": omission.omitted_tool_result_count,
                "reduced_from_tokens": initial_estimate["total_tokens"],
                "reduced_to_tokens": None,
                "message": "",
            },
        }

    def _persist_omission(
        self,
        *,
        session: Session,
        run: Run,
        optimization: dict[str, Any],
        after_estimate: dict[str, Any],
        prompt_turn_counter: int,
    ) -> None:
        metadata = optimization["metadata"]
        snapshot_store = ContextSnapshotStore(
            connection=self.artifact_store.connection,
            artifact_store=self.artifact_store,
        )
        context_settings = session.config_snapshot.get("context", {})
        snapshot = snapshot_store.save_omission_snapshot(
            session_id=session.session_id,
            run_id=run.run_id,
            source_checkpoint_id=run.latest_checkpoint_id,
            active_skill_records=run.active_skills,
            retained_messages=[
                message.to_dict() for message in optimization["snapshot_messages"]
            ],
            omitted_tool_result_count=metadata["omitted_tool_result_count"],
            artifact_refs=optimization["artifact_refs"],
            token_estimate={
                "before": dict(optimization["before_estimate"]),
                "after": dict(after_estimate),
                "window_tokens": int(context_settings.get("window_tokens", 200000)),
                "omit_old_tool_results_at_ratio": float(
                    context_settings.get("omit_old_tool_results_at_ratio", 0.60)
                ),
            },
        )
        if self.run_store is not None:
            self.run_store.update_context_snapshot(
                run.run_id,
                context_snapshot_id=snapshot.context_snapshot_id,
            )
        checkpoint = self._save_checkpoint(
            session=session,
            run=run,
            kind="context",
            state={
                "session_status": session.status,
                "run_status": run.status,
                "prompt_turn_counter": prompt_turn_counter,
                "context_snapshot_id": snapshot.context_snapshot_id,
                "active_skill_records": run.active_skills,
                "latest_artifact_ids": optimization["artifact_refs"],
                "latest_error_summary": None,
                "token_estimate": {
                    "before": dict(optimization["before_estimate"]),
                    "after": dict(after_estimate),
                },
            },
            summary="Old tool results omitted.",
        )
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="context_optimized",
            payload={
                "trigger": "omission",
                "context_snapshot_id": snapshot.context_snapshot_id,
                "checkpoint_id": checkpoint.checkpoint_id,
                "omitted_tool_result_count": metadata["omitted_tool_result_count"],
                "artifact_refs": optimization["artifact_refs"],
                "reduced_from_tokens": metadata["reduced_from_tokens"],
                "reduced_to_tokens": metadata["reduced_to_tokens"],
                "token_estimate": {
                    "before": dict(optimization["before_estimate"]),
                    "after": dict(after_estimate),
                },
            },
        )

    def _latest_run(self, run: Run) -> Run:
        if self.run_store is None:
            return run
        return self.run_store.get(run.run_id)

    def _append_event(
        self, *, session_id: str, run_id: str, kind: str, payload: dict[str, Any]
    ) -> None:
        self.event_writer.append(
            RunEvent(
                event_id=f"evt_{uuid4().hex}",
                timestamp=utc_now_iso(),
                session_id=session_id,
                run_id=run_id,
                step_id=None,
                kind=kind,
                payload=payload,
            )
        )

    def _append_model_event(
        self, *, session: Session, run: Run, kind: str, payload: dict[str, Any]
    ) -> None:
        if kind == "model_call_completed":
            payload = self._normalize_model_completed_payload(session, run, payload)
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind=kind,
            payload=payload,
        )

    def _normalize_model_completed_payload(
        self, session: Session, run: Run, payload: dict[str, Any]
    ) -> dict[str, Any]:
        content = payload.get("content")
        artifact_ids = list(payload.get("artifact_ids", []))
        payload["artifact_ids"] = artifact_ids
        payload.setdefault("redacted_output", None)
        payload.setdefault("tool_calls", [])
        if not isinstance(content, str):
            return payload
        content_size = len(content.encode("utf-8"))
        if content_size <= LARGE_MODEL_CONTENT_THRESHOLD_BYTES:
            return payload
        artifact = self.artifact_store.write_text(
            session_id=session.session_id,
            run_id=run.run_id,
            artifact_id=f"art_{uuid4().hex}",
            filename="model_call_completed_output.txt",
            content=content,
            metadata={
                "event_kind": "model_call_completed",
                "bytes": content_size,
            },
        )
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="artifact_registered",
            payload={
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "relative_path": artifact.relative_path,
                "metadata": artifact.metadata,
            },
        )
        payload["content"] = None
        payload["artifact_ids"] = [*artifact_ids, artifact.artifact_id]
        payload["redacted_output"] = (
            f"[model response stored as artifact: {artifact.artifact_id}]"
        )
        return payload


def _artifact_ids(result: AgentRunResult) -> list[str]:
    artifact_ids: list[str] = []
    for tool_result in result.tool_results:
        artifact_ids.extend(tool_result.get("artifacts", []))
    return artifact_ids


def _conversation_messages(conversation: list[dict[str, Any]]) -> list[ConversationMessage]:
    messages: list[ConversationMessage] = []
    for index, message in enumerate(conversation):
        role = str(message.get("role") or "user")
        content = message.get("content", "")
        messages.append(
            ConversationMessage(
                seq=int(message.get("seq", index + 1)),
                role=role,
                kind=str(message.get("kind") or "retained_raw"),
                turn_id=message.get("turn_id"),
                model_call_id=message.get("model_call_id"),
                tool_call_id=message.get("tool_call_id"),
                content=content if isinstance(content, (str, dict)) else str(content),
                artifact_refs=list(message.get("artifact_refs", [])),
                estimated_tokens=message.get("estimated_tokens"),
                metadata=dict(message.get("metadata", {})),
            )
        )
    return messages


def _provider_messages_to_conversation(
    messages: list[object],
    *,
    turn_id: str,
) -> list[ConversationMessage]:
    converted: list[ConversationMessage] = []
    for index, message in enumerate(messages, start=1):
        role = getattr(message, "type", None) or getattr(message, "role", None)
        if role == "ai":
            role = "assistant"
        if role == "tool":
            kind = "tool_result"
        elif role == "assistant":
            kind = "tool_call"
        else:
            kind = "tool_loop_message"
        content = getattr(message, "content", message)
        if not isinstance(content, (str, dict)):
            content = str(content)
        converted.append(
            ConversationMessage(
                seq=index,
                role=str(role or "assistant"),
                kind=kind,
                turn_id=turn_id,
                model_call_id=None,
                tool_call_id=getattr(message, "tool_call_id", None),
                content=content,
            )
        )
    return converted


def _with_context_metadata(
    result: AgentRunResult,
    *,
    context_estimate: dict[str, Any],
    context_estimate_history: list[dict[str, Any]],
    continuation_history: list[str],
    query_state: Any,
    context_optimization: dict[str, Any] | None = None,
    conversation_writeback: list[dict[str, Any]] | None = None,
) -> AgentRunResult:
    metadata = {
        **result.metadata,
        "context_estimate": context_estimate,
        "context_estimate_history": list(context_estimate_history),
        "continuation_history": list(continuation_history),
        "query_state": {
            "query_id": query_state.query_id,
            "turn_id": query_state.turn_id,
            "continuation_reason": query_state.continuation_reason,
            "active_skill_records": query_state.active_skill_records,
            "latest_context_estimate": query_state.latest_context_estimate,
            "current_approval_mode": query_state.current_approval_mode,
            "latest_model_context_frame": query_state.latest_model_context_frame,
        },
    }
    if context_optimization is not None:
        metadata["context_optimization"] = context_optimization
    if conversation_writeback is not None:
        metadata["conversation_writeback"] = conversation_writeback
    return AgentRunResult(
        status=result.status,
        assistant_output=result.assistant_output,
        tool_results=result.tool_results,
        usage=result.usage,
        error=result.error,
        metadata=metadata,
    )


def _serializable_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    serialized = dict(metadata)
    query_state = serialized.get("query_state")
    if isinstance(query_state, dict):
        query_state = dict(query_state)
        frame = query_state.get("latest_model_context_frame")
        if hasattr(frame, "to_dict"):
            query_state["latest_model_context_frame"] = frame.to_dict()
        serialized["query_state"] = query_state
    return serialized

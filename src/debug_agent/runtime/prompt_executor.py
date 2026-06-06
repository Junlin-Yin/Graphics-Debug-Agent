from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
from time import monotonic

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.conversation import ConversationAppend, ConversationStore
from debug_agent.persistence.context_snapshots import ContextSnapshotStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.todo_plans import TodoPlanStore
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
from debug_agent.runtime.context_manager import CompressionError, ContextManager
from debug_agent.runtime.errors import NormalizedError
from debug_agent.runtime.model_context import CompressionContextFrame, ConversationMessage
from debug_agent.runtime.policy import PermissionEvaluator, policy_facts_from_snapshot
from debug_agent.runtime.prompt_composer import PromptComposer, PromptCompositionRequest
from debug_agent.runtime.query_control import QueryControlPlane
from debug_agent.runtime.stream_events import AgentStreamEvent


LARGE_MODEL_CONTENT_THRESHOLD_BYTES = 16 * 1024
CONTEXT_LIMIT_EXCEEDED_MESSAGE = (
    "Context window still exceeds the limit after compression. "
    "The current turn was aborted."
)
NO_COMPRESSIBLE_HISTORY_MESSAGE = "No compressible history."


class CompressionFailedAbort(Exception):
    def __init__(
        self,
        *,
        message: str,
        metadata: dict[str, Any],
        conversation_writeback: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.message = message
        self.metadata = metadata
        self.conversation_writeback = conversation_writeback

    def to_result(self) -> AgentRunResult:
        return AgentRunResult(
            status="failed",
            assistant_output=None,
            tool_results=[],
            usage={},
            error={
                "error_class": "compression_failed",
                "message": self.message,
            },
            metadata={
                "failure_scope": "turn",
                "compression_failed_abort": True,
                "context_optimization": self.metadata,
                "conversation_writeback": self.conversation_writeback,
            },
        )


@dataclass(frozen=True)
class PromptAgentExecutor:
    event_writer: EventWriter
    checkpoint_store: CheckpointStore
    artifact_store: ArtifactStore
    adapter: AgentLoopAdapter
    tool_definitions: list[ToolDefinition]
    system_prompt: str
    skill_snapshot_store: SkillSnapshotStore
    todo_plan_store: TodoPlanStore
    conversation_store: ConversationStore | None = None
    run_store: RunStore | None = None
    query_control: QueryControlPlane | None = None
    compression_model: Callable[[Any], str] | None = None

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
        approval_provider: object | None = None,
    ) -> AgentRunResult:
        self._validate_durable_projection_alignment(
            run_id=run.run_id,
            conversation=conversation or [],
        )
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

        def emit_context_estimate(estimate: dict[str, Any]) -> None:
            _emit_context_estimate_update(agent_stream_callback, estimate)

        def stream_callback(event: AgentStreamEvent) -> None:
            if agent_stream_callback is None:
                return
            if (
                event.kind == "stream_model_call_completed"
                and "context_estimate" not in event.payload
            ):
                event = AgentStreamEvent(
                    kind=event.kind,
                    payload={
                        **event.payload,
                        "context_estimate": dict(context_estimate_box["estimate"]),
                    },
                )
            agent_stream_callback(event)

        query_state = query_control.start_query(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=f"turn-{prompt_turn_counter}",
            approval_mode=session.approval_mode,
            active_skill_records=run.active_skills,
        )
        query_state_box["state"] = query_state
        self._append_durable_user_input(
            session=session,
            run=run,
            turn_id=query_state.turn_id,
            user_input=user_input,
        )
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
        emit_context_estimate(composition.estimate.to_dict())
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
            emit_context_estimate(composition.estimate.to_dict())
            optimization["metadata"]["reduced_to_tokens"] = (
                composition.estimate.total_tokens
            )
            optimization["metadata"]["message"] = (
                "Context optimized: reduced from "
                f"{optimization['metadata']['reduced_from_tokens']} to "
                f"{optimization['metadata']['reduced_to_tokens']} tokens by "
                "omitting earlier tool results."
            )
            optimization_box["optimization"] = optimization
        compression_result = self._maybe_compress(
            session=session,
            run=self._latest_run(run),
            query_control=query_control,
            query_state=query_state,
            retained_messages=retained_messages,
            current_messages=current_messages,
            initial_estimate=composition.estimate.to_dict(),
            prompt_turn_counter=prompt_turn_counter,
            prior_optimization=optimization_box["optimization"],
        )
        if compression_result is not None and compression_result.get("failed"):
            query_state = query_control.record_continuation(
                query_state.query_id,
                "compression_failed_abort",
            )
            continuation_history.append(query_state.continuation_reason)
            query_state_box["state"] = query_state
            failed_result = _with_context_metadata(
                AgentRunResult(
                    status="failed",
                    assistant_output=None,
                    tool_results=[],
                    usage={},
                    error={
                        "error_class": "compression_failed",
                        "message": compression_result["message"],
                    },
                    metadata={"failure_scope": "turn"},
                ),
                context_estimate=context_estimate_box["estimate"],
                context_estimate_history=context_estimate_history,
                continuation_history=continuation_history,
                query_state=query_state,
                context_optimization=compression_result["metadata"],
                conversation_writeback=[message.to_dict() for message in retained_messages],
            )
            self._append_durable_result(
                session=session,
                run=run,
                turn_id=query_state.turn_id,
                result=failed_result,
            )
            return failed_result
        if compression_result is not None:
            retained_messages = compression_result["retained_messages"]
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
            emit_context_estimate(composition.estimate.to_dict())
            compression_result["metadata"]["reduced_to_tokens"] = (
                composition.estimate.total_tokens
            )
            compression_result["metadata"]["message"] = (
                "Context compressed: reduced from "
                f"{compression_result['metadata']['reduced_from_tokens']} to "
                f"{compression_result['metadata']['reduced_to_tokens']} tokens; "
                f"retained {compression_result['metadata']['retain_recent_model_calls']} "
                "recent model calls."
            )
            self._persist_compression(
                session=session,
                run=run,
                optimization=compression_result,
                after_estimate=composition.estimate.to_dict(),
                prompt_turn_counter=prompt_turn_counter,
            )
            optimization_box["optimization"] = compression_result
        elif optimization is not None:
            self._persist_omission(
                session=session,
                run=run,
                optimization=optimization,
                after_estimate=composition.estimate.to_dict(),
                prompt_turn_counter=prompt_turn_counter,
            )
        context_limit_result = self._maybe_record_context_limit_exceeded(
            session=session,
            run=self._latest_run(run),
            estimate=composition.estimate.to_dict(),
            prompt_turn_counter=prompt_turn_counter,
            optimization=optimization_box["optimization"],
        )
        if context_limit_result is not None:
            query_state = query_control.record_continuation(
                query_state.query_id,
                "context_limit_abort",
            )
            continuation_history.append(query_state.continuation_reason)
            query_state_box["state"] = query_state
            failed_result = _with_context_metadata(
                context_limit_result,
                context_estimate=context_estimate_box["estimate"],
                context_estimate_history=context_estimate_history,
                continuation_history=continuation_history,
                query_state=query_state,
                context_optimization=None
                if optimization_box["optimization"] is None
                else optimization_box["optimization"]["metadata"],
                conversation_writeback=None,
            )
            self._append_durable_result(
                session=session,
                run=run,
                turn_id=query_state.turn_id,
                result=failed_result,
            )
            return failed_result
        request = AgentRunRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            model_config=session.config_snapshot,
            timeout_seconds=session.config_snapshot.get("timeout_seconds"),
            model_context_frame=composition.frame,
            conversation=[],
            tools=[],
        )
        accumulated_tool_loop_messages: list[object] = []

        def refresh_model_context_frame(tool_loop_messages: list[object]) -> dict[str, Any]:
            accumulated_tool_loop_messages.extend(tool_loop_messages)
            return self._record_followup_composition(
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
                        accumulated_tool_loop_messages,
                        turn_id=query_state_box["state"].turn_id,
                    ),
                ],
                optimization_box=optimization_box,
                prompt_turn_counter=prompt_turn_counter,
                agent_stream_callback=agent_stream_callback,
            )

        tool_metadata = {
            "skill_snapshot_store": self.skill_snapshot_store,
            "todo_plan_store": self.todo_plan_store,
            "run_store": self.run_store,
            "approval_grants": ApprovalGrantStore(self.event_writer.connection),
            "approval_provider": approval_provider,
            "refresh_model_context_frame": refresh_model_context_frame,
        }
        policy_snapshot = session.config_snapshot.get("policy")
        if isinstance(policy_snapshot, dict):
            policy_facts = policy_facts_from_snapshot(policy_snapshot, workspace_root)
            tool_metadata["policy_facts"] = policy_facts
            tool_metadata["permission_evaluator"] = PermissionEvaluator(policy_facts)
        context = RunContext(
            workspace_root=workspace_root,
            artifact_root=session.artifact_root,
            approval_mode=session.approval_mode,
            cancellation_token=None,
            metadata=tool_metadata,
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
            result = self.adapter.stream(request, context, stream_callback)
        continuation_reason = (
            "approval_denied_abort"
            if result.metadata.get("approval_denied_abort") is True
            else (
                "final_assistant_response"
                if result.status == "completed"
                else query_state_box["state"].continuation_reason
            )
        )
        query_state = query_control.record_continuation(
            query_state_box["state"].query_id,
            continuation_reason,
        )
        continuation_history.append(query_state.continuation_reason)
        query_state_box["state"] = query_state
        compression_failed_abort = (
            result.metadata.get("compression_failed_abort") is True
        )
        result = _with_context_metadata(
            result,
            context_estimate=context_estimate_box["estimate"],
            context_estimate_history=context_estimate_history,
            continuation_history=continuation_history,
            query_state=query_state,
            context_optimization=None
            if compression_failed_abort or optimization_box["optimization"] is None
            else optimization_box["optimization"]["metadata"],
            conversation_writeback=None
            if compression_failed_abort or optimization_box["optimization"] is None
            else [
                message.to_dict()
                for message in optimization_box["optimization"]["retained_messages"]
            ],
            turn_tool_loop_messages=[
                message.to_dict()
                for message in _provider_messages_to_conversation(
                    accumulated_tool_loop_messages,
                    turn_id=query_state.turn_id,
                )
            ],
        )
        if result.metadata.get("compression_failed_abort") is True:
            return result
        self._append_durable_result(
            session=session,
            run=run,
            turn_id=query_state.turn_id,
            result=result,
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

    def _append_durable_user_input(
        self,
        *,
        session: Session,
        run: Run,
        turn_id: str,
        user_input: str,
    ) -> None:
        if self.conversation_store is None:
            return
        self.conversation_store.append_closed_group(
            session_id=session.session_id,
            run_id=run.run_id,
            messages=[
                ConversationAppend(
                    turn_id=turn_id,
                    message_group_id=f"{turn_id}:user",
                    model_call_id=None,
                    group_position=0,
                    group_row_count=1,
                    role="user",
                    kind="user_input",
                    content={"content": user_input},
                    metadata={},
                )
            ],
        )

    def _validate_durable_projection_alignment(
        self,
        *,
        run_id: str,
        conversation: list[dict[str, Any]],
    ) -> None:
        if self.conversation_store is None:
            return
        self.conversation_store.validate_runtime_projection_alignment(
            run_id=run_id,
            process_message_indexes=_durable_message_indexes(conversation),
            explicit_resume=False,
        )

    def _append_durable_result(
        self,
        *,
        session: Session,
        run: Run,
        turn_id: str,
        result: AgentRunResult,
    ) -> None:
        if self.conversation_store is None:
            return
        turn_tool_loop_messages = result.metadata.get("turn_tool_loop_messages")
        if isinstance(turn_tool_loop_messages, list):
            tool_loop_appends: list[ConversationAppend] = []
            for index, raw_message in enumerate(turn_tool_loop_messages, start=1):
                if not isinstance(raw_message, dict):
                    continue
                append = _durable_append_from_conversation_message(
                    raw_message,
                    turn_id=turn_id,
                    ordinal=index,
                )
                if append is None:
                    continue
                tool_loop_appends.append(append)
            if tool_loop_appends:
                self.conversation_store.append_closed_group(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    messages=[
                        _with_group_position(
                            append,
                            message_group_id=f"{turn_id}:tool-loop",
                            group_position=index,
                            group_row_count=len(tool_loop_appends),
                        )
                        for index, append in enumerate(tool_loop_appends)
                    ],
                )
        if result.status == "completed":
            self.conversation_store.append_closed_group(
                session_id=session.session_id,
                run_id=run.run_id,
                messages=[
                    ConversationAppend(
                        turn_id=turn_id,
                        message_group_id=f"{turn_id}:assistant:final",
                        model_call_id=f"{turn_id}:assistant",
                        group_position=0,
                        group_row_count=1,
                        role="assistant",
                        kind="assistant_output",
                        content={"content": result.assistant_output or ""},
                        metadata={},
                    )
                ],
            )
            return
        self.conversation_store.append_closed_group(
            session_id=session.session_id,
            run_id=run.run_id,
            messages=[
                _durable_failure_append(
                    turn_id=turn_id,
                    result=result,
                )
            ],
        )

    def manual_compress(
        self,
        *,
        session: Session,
        run: Run,
        conversation: list[dict[str, Any]] | None = None,
        prompt_turn_counter: int = 0,
    ) -> AgentRunResult:
        retained_messages = _conversation_messages(conversation or [])
        if not retained_messages:
            return AgentRunResult(
                status="completed",
                assistant_output=NO_COMPRESSIBLE_HISTORY_MESSAGE,
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )
        query_control = self.query_control or QueryControlPlane()
        query_state = query_control.start_query(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=f"manual-compress-{prompt_turn_counter}",
            approval_mode=session.approval_mode,
            active_skill_records=run.active_skills,
        )
        composition = self._compose_frame(
            session=session,
            run=run,
            retained_messages=retained_messages,
            current_messages=[],
        )
        query_state = query_control.record_context_estimate(
            query_state.query_id,
            frame=composition.frame,
            estimate_total_tokens=composition.estimate.total_tokens,
            estimator_version=composition.estimate.estimator_version,
        )
        compression_result = self._maybe_compress(
            session=session,
            run=self._latest_run(run),
            query_control=query_control,
            query_state=query_state,
            retained_messages=retained_messages,
            current_messages=[],
            initial_estimate=composition.estimate.to_dict(),
            prompt_turn_counter=prompt_turn_counter,
            prior_optimization=None,
            manual=True,
        )
        if compression_result is None:
            return AgentRunResult(
                status="completed",
                assistant_output=NO_COMPRESSIBLE_HISTORY_MESSAGE,
                tool_results=[],
                usage={},
                error=None,
                metadata={
                    "context_estimate": composition.estimate.to_dict(),
                },
            )
        if compression_result.get("failed"):
            return AgentRunResult(
                status="failed",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "error_class": "compression_failed",
                    "message": compression_result["message"],
                },
                metadata={
                    "failure_scope": "turn",
                    "context_estimate": composition.estimate.to_dict(),
                    "context_optimization": compression_result["metadata"],
                    "conversation_writeback": [message.to_dict() for message in retained_messages],
                },
            )
        retained_after = compression_result["retained_messages"]
        after_composition = self._compose_frame(
            session=session,
            run=self._latest_run(run),
            retained_messages=retained_after,
            current_messages=[],
        )
        compression_result["metadata"]["reduced_to_tokens"] = (
            after_composition.estimate.total_tokens
        )
        compression_result["metadata"]["message"] = (
            "Context compressed: reduced from "
            f"{compression_result['metadata']['reduced_from_tokens']} to "
            f"{compression_result['metadata']['reduced_to_tokens']} tokens; "
            f"retained {compression_result['metadata']['retain_recent_model_calls']} "
            "recent model calls."
        )
        self._persist_compression(
            session=session,
            run=run,
            optimization=compression_result,
            after_estimate=after_composition.estimate.to_dict(),
            prompt_turn_counter=prompt_turn_counter,
        )
        return AgentRunResult(
            status="completed",
            assistant_output=compression_result["metadata"]["message"],
            tool_results=[],
            usage={},
            error=None,
            metadata={
                "context_estimate": after_composition.estimate.to_dict(),
                "context_optimization": compression_result["metadata"],
                "conversation_writeback": [message.to_dict() for message in retained_after],
            },
        )

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
        return PromptComposer(
            skill_snapshot_store=self.skill_snapshot_store,
            todo_plan_store=self.todo_plan_store,
        ).compose(
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
        agent_stream_callback: Callable[[AgentStreamEvent], None] | None,
    ) -> dict[str, Any]:
        state = query_control.record_continuation(
            query_state_box["state"].query_id,
            "tool_result_continuation",
        )
        continuation_history.append(state.continuation_reason)
        pre_optimization_retained_messages = [
            message for message in retained_messages_box["messages"]
        ]
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
        _emit_context_estimate_update(
            agent_stream_callback,
            composition.estimate.to_dict(),
        )
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
            _emit_context_estimate_update(
                agent_stream_callback,
                composition.estimate.to_dict(),
            )
            optimization["metadata"]["reduced_to_tokens"] = (
                composition.estimate.total_tokens
            )
            optimization["metadata"]["message"] = (
                "Context optimized: reduced from "
                f"{optimization['metadata']['reduced_from_tokens']} to "
                f"{optimization['metadata']['reduced_to_tokens']} tokens by "
                "omitting earlier tool results."
            )
            optimization_box["optimization"] = optimization
        compression_result = self._maybe_compress(
            session=session,
            run=self._latest_run(run),
            query_control=query_control,
            query_state=state,
            retained_messages=retained_messages_box["messages"],
            current_messages=current_messages,
            initial_estimate=composition.estimate.to_dict(),
            prompt_turn_counter=prompt_turn_counter,
            prior_optimization=optimization_box["optimization"],
        )
        if compression_result is not None and compression_result.get("failed"):
            state = query_control.record_continuation(
                state.query_id,
                "compression_failed_abort",
            )
            continuation_history.append(state.continuation_reason)
            query_state_box["state"] = state
            raise CompressionFailedAbort(
                message=compression_result["message"],
                metadata=compression_result["metadata"],
                conversation_writeback=[
                    message.to_dict() for message in pre_optimization_retained_messages
                ],
            )
        if compression_result is not None:
            retained_messages_box["messages"] = compression_result["retained_messages"]
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
            _emit_context_estimate_update(
                agent_stream_callback,
                composition.estimate.to_dict(),
            )
            compression_result["metadata"]["reduced_to_tokens"] = (
                composition.estimate.total_tokens
            )
            compression_result["metadata"]["message"] = (
                "Context compressed: reduced from "
                f"{compression_result['metadata']['reduced_from_tokens']} to "
                f"{compression_result['metadata']['reduced_to_tokens']} tokens; "
                f"retained {compression_result['metadata']['retain_recent_model_calls']} "
                "recent model calls."
            )
            self._persist_compression(
                session=session,
                run=run,
                optimization=compression_result,
                after_estimate=composition.estimate.to_dict(),
                prompt_turn_counter=prompt_turn_counter,
            )
            optimization_box["optimization"] = compression_result
        elif optimization is not None:
            self._persist_omission(
                session=session,
                run=run,
                optimization=optimization,
                after_estimate=composition.estimate.to_dict(),
                prompt_turn_counter=prompt_turn_counter,
            )
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

    def _maybe_compress(
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
        prior_optimization: dict[str, Any] | None,
        manual: bool = False,
    ) -> dict[str, Any] | None:
        context_settings = session.config_snapshot.get("context", {})
        window_tokens = int(context_settings.get("window_tokens", 200000))
        compress_ratio = float(context_settings.get("compress_history_at_ratio", 0.80))
        retain_recent_model_calls = int(
            context_settings.get("retain_recent_model_calls", 4)
        )
        reserved_output_tokens = int(
            context_settings.get("compression_reserved_output_tokens", 10000)
        )
        if self.compression_model is None:
            return None
        if manual and not _has_evictable_history(
            retained_messages=retained_messages,
            current_messages=current_messages,
            retain_recent_model_calls=retain_recent_model_calls,
            query_control=query_control,
        ):
            return None
        manager = ContextManager(query_control=query_control)
        try:
            plan = manager.prepare_compression(
                retained_messages=retained_messages,
                current_messages=current_messages,
                retain_recent_model_calls=retain_recent_model_calls,
                window_tokens=window_tokens,
                compression_reserved_output_tokens=reserved_output_tokens,
            )
        except CompressionError as exc:
            if exc.reason == "no_evictable_history":
                return None
            if not manual and exc.reason == "invalid_budget" and (
                initial_estimate["total_tokens"] <= compress_ratio * window_tokens
            ):
                return None
            return self._record_compression_failed(
                session=session,
                run=run,
                reason=exc.reason,
                prompt_turn_counter=prompt_turn_counter,
                token_estimate={"before": dict(initial_estimate)},
            )

        eligible_tokens = sum(
            message.estimated_tokens or 0 for message in plan.evicted_messages
        )
        should_compress = (
            initial_estimate["total_tokens"] > compress_ratio * window_tokens
            or eligible_tokens > plan.budget_tokens
            or manual
        )
        if not should_compress:
            return None
        self._append_model_event(
            session=session,
            run=run,
            kind="model_call_started",
            payload={
                "provider": session.config_snapshot.get("provider"),
                "model": session.config_snapshot.get("model"),
                "purpose": "compression",
                "tool_schema_bindings": [],
            },
        )
        started_at = monotonic()
        try:
            output = self.compression_model(plan.frame)
        except Exception as exc:
            self._append_model_event(
                session=session,
                run=run,
                kind="model_call_failed",
                payload={
                    "error_class": "model_error",
                    "message": str(exc),
                    "source": "model",
                    "recoverable": True,
                    "duration": monotonic() - started_at,
                    "purpose": "compression",
                },
            )
            return self._record_compression_failed(
                session=session,
                run=run,
                reason="model_failure",
                prompt_turn_counter=prompt_turn_counter,
                token_estimate={"before": dict(initial_estimate)},
            )
        self._append_model_event(
            session=session,
            run=run,
            kind="model_call_completed",
            payload={
                "usage": {},
                "metadata": {"purpose": "compression"},
                "duration": monotonic() - started_at,
                "content": output,
                "tool_calls": [],
                "artifact_ids": [],
                "redacted_output": None,
                "purpose": "compression",
            },
        )
        try:
            summary = manager.parse_continuity_summary(output)
        except CompressionError as exc:
            return self._record_compression_failed(
                session=session,
                run=run,
                reason=exc.reason,
                prompt_turn_counter=prompt_turn_counter,
                token_estimate={"before": dict(initial_estimate)},
            )
        summary_json = manager.canonical_summary_json(summary)
        retained_after = _replace_compressed_history(
            retained_messages=retained_messages,
            plan=plan,
            summary_json=summary_json,
        )
        trigger = "manual" if manual else "compression"
        omitted_count = 0
        if prior_optimization is not None:
            prior_metadata = prior_optimization.get("metadata", {})
            if not manual and prior_metadata.get("trigger") == "omission":
                trigger = "omission | compression"
                omitted_count = int(prior_metadata.get("omitted_tool_result_count", 0))
        artifact_refs = sorted(
            {
                artifact_ref
                for message in plan.evicted_messages
                for artifact_ref in message.artifact_refs
            }
        )
        return {
            "retained_messages": retained_after,
            "snapshot_messages": retained_after,
            "summary": summary_json,
            "artifact_refs": artifact_refs,
            "before_estimate": dict(initial_estimate),
            "compression_estimate": plan.estimate.to_dict(),
            "metadata": {
                "trigger": trigger,
                "omitted_tool_result_count": omitted_count,
                "evicted_message_count": len(plan.evicted_messages),
                "evicted_model_call_group_count": len(plan.selected_model_call_group_ids),
                "selected_model_call_group_ids": list(plan.selected_model_call_group_ids),
                "retain_recent_model_calls": retain_recent_model_calls,
                "reduced_from_tokens": initial_estimate["total_tokens"],
                "reduced_to_tokens": None,
                "message": "",
            },
        }

    def _record_compression_failed(
        self,
        *,
        session: Session,
        run: Run,
        reason: str,
        prompt_turn_counter: int,
        token_estimate: dict[str, Any],
    ) -> dict[str, Any]:
        message = _compression_failure_message(reason)
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="compression_failed",
            payload={
                "error_class": "compression_failed",
                "reason": reason,
                "message": message,
                "token_estimate": token_estimate,
                "error": _normalized_error_dict(
                    "model_error",
                    "compression_failed",
                    message=message,
                    scope="turn",
                    metadata={
                        "compression_reason": reason,
                        "token_estimate": token_estimate,
                    },
                ),
            },
        )
        checkpoint = self._save_checkpoint(
            session=session,
            run=run,
            kind="context",
            state={
                "session_status": session.status,
                "run_status": run.status,
                "prompt_turn_counter": prompt_turn_counter,
                "context_snapshot_id": run.context_snapshot_id,
                "active_skill_records": run.active_skills,
                "latest_artifact_ids": [],
                "latest_error_summary": message,
                "error_class": "compression_failed",
                "reason": reason,
                "token_estimate": token_estimate,
            },
            summary=message,
        )
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="checkpoint_written",
            payload={"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
        )
        return {
            "failed": True,
            "message": message,
            "metadata": {
                "trigger": "compression",
                "failed": True,
                "reason": reason,
                "message": message,
            },
        }

    def _maybe_record_context_limit_exceeded(
        self,
        *,
        session: Session,
        run: Run,
        estimate: dict[str, Any],
        prompt_turn_counter: int,
        optimization: dict[str, Any] | None,
    ) -> AgentRunResult | None:
        context_settings = session.config_snapshot.get("context", {})
        window_tokens = int(context_settings.get("window_tokens", 200000))
        estimated_tokens = int(estimate.get("total_tokens", 0))
        if estimated_tokens <= window_tokens:
            return None
        applied: list[str] = []
        if optimization is not None:
            trigger = optimization.get("metadata", {}).get("trigger")
            if trigger == "omission | compression":
                applied = ["omission", "compression"]
            elif trigger in {"omission", "compression", "manual"}:
                applied = [str(trigger)]
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="context_limit_exceeded",
            payload={
                "error_class": "context_limit_exceeded",
                "estimated_tokens": estimated_tokens,
                "window_tokens": window_tokens,
                "optimization_applied": applied,
                "message": CONTEXT_LIMIT_EXCEEDED_MESSAGE,
                "error": _normalized_error_dict(
                    "model_error",
                    "context_limit_exceeded",
                    message=CONTEXT_LIMIT_EXCEEDED_MESSAGE,
                    scope="turn",
                    metadata={
                        "estimated_tokens": estimated_tokens,
                        "window_tokens": window_tokens,
                        "optimization_applied": applied,
                    },
                ),
            },
        )
        checkpoint = self._save_checkpoint(
            session=session,
            run=run,
            kind="context",
            state={
                "session_status": session.status,
                "run_status": run.status,
                "prompt_turn_counter": prompt_turn_counter,
                "context_snapshot_id": run.context_snapshot_id,
                "active_skill_records": run.active_skills,
                "latest_artifact_ids": [],
                "latest_error_summary": CONTEXT_LIMIT_EXCEEDED_MESSAGE,
                "error_class": "context_limit_exceeded",
                "estimated_tokens": estimated_tokens,
                "window_tokens": window_tokens,
                "token_estimate": dict(estimate),
            },
            summary=CONTEXT_LIMIT_EXCEEDED_MESSAGE,
        )
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="checkpoint_written",
            payload={"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
        )
        return AgentRunResult(
            status="failed",
            assistant_output=None,
            tool_results=[],
            usage={},
            error={
                "error_class": "context_limit_exceeded",
                "message": CONTEXT_LIMIT_EXCEEDED_MESSAGE,
            },
            metadata={"failure_scope": "turn"},
        )

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
        self._overwrite_durable_projection_from_messages(
            session=session,
            run=run,
            messages=optimization["snapshot_messages"],
            update_reason="omission",
        )

    def _persist_compression(
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
        snapshot = snapshot_store.save_compression_snapshot(
            session_id=session.session_id,
            run_id=run.run_id,
            trigger=metadata["trigger"],
            source_checkpoint_id=run.latest_checkpoint_id,
            active_skill_records=run.active_skills,
            summary=optimization["summary"],
            retained_messages=[
                message.to_dict() for message in optimization["snapshot_messages"]
            ],
            omitted_tool_result_count=metadata["omitted_tool_result_count"],
            evicted_message_count=metadata["evicted_message_count"],
            evicted_model_call_group_count=metadata["evicted_model_call_group_count"],
            artifact_refs=optimization["artifact_refs"],
            token_estimate={
                "before": dict(optimization["before_estimate"]),
                "after": dict(after_estimate),
                "compression_input": dict(optimization["compression_estimate"]),
                "window_tokens": int(context_settings.get("window_tokens", 200000)),
                "compress_history_at_ratio": float(
                    context_settings.get("compress_history_at_ratio", 0.80)
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
            summary="Context compressed.",
        )
        self._append_event(
            session_id=session.session_id,
            run_id=run.run_id,
            kind="context_optimized",
            payload={
                "trigger": metadata["trigger"],
                "context_snapshot_id": snapshot.context_snapshot_id,
                "checkpoint_id": checkpoint.checkpoint_id,
                "omitted_tool_result_count": metadata["omitted_tool_result_count"],
                "evicted_message_count": metadata["evicted_message_count"],
                "evicted_model_call_group_count": metadata[
                    "evicted_model_call_group_count"
                ],
                "artifact_refs": optimization["artifact_refs"],
                "reduced_from_tokens": metadata["reduced_from_tokens"],
                "reduced_to_tokens": metadata["reduced_to_tokens"],
                "token_estimate": {
                    "before": dict(optimization["before_estimate"]),
                    "after": dict(after_estimate),
                },
            },
        )
        summary_row_index = self._append_durable_context_summary(
            session=session,
            run=run,
            prompt_turn_counter=prompt_turn_counter,
            optimization=optimization,
        )
        self._overwrite_durable_projection_from_messages(
            session=session,
            run=run,
            messages=optimization["snapshot_messages"],
            update_reason="compression",
            summary_row_index=summary_row_index,
        )

    def _append_durable_context_summary(
        self,
        *,
        session: Session,
        run: Run,
        prompt_turn_counter: int,
        optimization: dict[str, Any],
    ) -> int | None:
        if self.conversation_store is None:
            return None
        summary = optimization.get("summary")
        if not isinstance(summary, str):
            return None
        metadata = dict(optimization.get("metadata", {}))
        trigger = str(metadata.get("trigger") or "compression")
        turn_id = (
            f"manual-compress-{prompt_turn_counter}"
            if trigger == "manual"
            else f"turn-{prompt_turn_counter}"
        )
        rows = self.conversation_store.append_closed_group(
            session_id=session.session_id,
            run_id=run.run_id,
            update_reason="compression",
            messages=[
                ConversationAppend(
                    turn_id=turn_id,
                    message_group_id=f"{turn_id}:context-summary",
                    model_call_id=None,
                    group_position=0,
                    group_row_count=1,
                    role="runtime",
                    kind="context_summary",
                    content={"content": summary},
                    metadata={
                        "trigger": trigger,
                        "evicted_message_count": int(
                            metadata.get("evicted_message_count", 0)
                        ),
                        "evicted_model_call_group_count": int(
                            metadata.get("evicted_model_call_group_count", 0)
                        ),
                    },
                )
            ],
        )
        return rows[0].message_index

    def _overwrite_durable_projection_from_messages(
        self,
        *,
        session: Session,
        run: Run,
        messages: list[ConversationMessage],
        update_reason: str,
        summary_row_index: int | None = None,
    ) -> None:
        if self.conversation_store is None:
            return
        highest = self.conversation_store.get_projection(run.run_id).source_high_watermark
        refs: list[dict[str, int]] = []
        summary_ref_used = False
        for message in messages:
            if message.kind == "context_summary" and summary_row_index is not None:
                refs.append({"index": summary_row_index})
                summary_ref_used = True
                continue
            durable_index = _conversation_message_durable_index(message)
            if durable_index is None:
                raise RuntimeError(
                    "retained projection message is missing durable_message_index"
                )
            if durable_index < 1 or durable_index > highest:
                raise RuntimeError(
                    "retained projection message durable_message_index is outside "
                    "durable projection high watermark"
                )
            refs.append({"index": durable_index})
        if summary_row_index is not None and not summary_ref_used:
            refs.insert(0, {"index": summary_row_index})
        self.conversation_store.overwrite_projection(
            session_id=session.session_id,
            run_id=run.run_id,
            message_refs=refs,
            update_reason=update_reason,
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
        if kind == "model_call_failed":
            payload = _normalize_model_call_failed_payload(payload)
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


def _normalize_model_call_failed_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if _is_normalized_error(payload.get("error")):
        return payload
    purpose = str(payload.get("purpose") or "main")
    message = _payload_error_message(payload)
    metadata = {"purpose": purpose}
    return {
        **payload,
        "error": _normalized_error_dict(
            "model_error",
            "model_call_failed",
            message=message,
            scope="provider",
            metadata=metadata,
        ),
    }


def _payload_error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(payload.get("message"), str):
        return payload["message"]
    return "Model call failed."


def _is_normalized_error(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and isinstance(value.get("error_class"), str)
        and isinstance(value.get("reason"), str)
        and isinstance(value.get("message"), str)
        and isinstance(value.get("scope"), str)
        and isinstance(value.get("recoverability"), str)
        and isinstance(value.get("metadata"), dict)
        and isinstance(value.get("artifact_ids"), list)
    )


def _normalized_error_dict(
    error_class: str,
    reason: str,
    *,
    message: str,
    scope: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return NormalizedError.create(
        error_class,
        reason,
        message=message,
        scope=scope,
        metadata=metadata,
    ).to_dict()


def _artifact_ids(result: AgentRunResult) -> list[str]:
    artifact_ids: list[str] = []
    for tool_result in result.tool_results:
        artifact_ids.extend(tool_result.get("artifacts", []))
    return artifact_ids


def _emit_context_estimate_update(
    agent_stream_callback: Callable[[AgentStreamEvent], None] | None,
    estimate: dict[str, Any],
) -> None:
    if agent_stream_callback is None:
        return
    agent_stream_callback(
        AgentStreamEvent(
            kind="stream_context_estimate_updated",
            payload={"context_estimate": dict(estimate)},
        )
    )


def _replace_compressed_history(
    *,
    retained_messages: list[ConversationMessage],
    plan: Any,
    summary_json: str,
) -> list[ConversationMessage]:
    evicted_ids = {message.seq for message in plan.evicted_messages}
    previous_summary_id = (
        None if plan.previous_summary_message is None else plan.previous_summary_message.seq
    )
    insertion_seq = min(evicted_ids)
    summary_inserted = False
    retained_after: list[ConversationMessage] = []
    for message in retained_messages:
        if message.seq == previous_summary_id or message.seq in evicted_ids:
            if not summary_inserted and message.seq >= insertion_seq:
                retained_after.append(
                    ConversationMessage(
                        seq=insertion_seq,
                        role="system",
                        kind="context_summary",
                        turn_id=None,
                        model_call_id=None,
                        tool_call_id=None,
                        content=summary_json,
                    )
                )
                summary_inserted = True
            continue
        retained_after.append(message)
    if not summary_inserted:
        retained_after.insert(
            0,
            ConversationMessage(
                seq=insertion_seq,
                role="system",
                kind="context_summary",
                turn_id=None,
                model_call_id=None,
                tool_call_id=None,
                content=summary_json,
            ),
        )
    return sorted(retained_after, key=lambda message: message.seq)


def _has_evictable_history(
    *,
    retained_messages: list[ConversationMessage],
    current_messages: list[ConversationMessage],
    retain_recent_model_calls: int,
    query_control: QueryControlPlane,
) -> bool:
    groups = query_control.derive_model_call_groups(retained_messages)
    suffix = query_control.compute_non_evictable_raw_suffix(
        retained_messages,
        retain_recent_model_calls=retain_recent_model_calls,
        current_messages=current_messages,
    )
    return any(
        group.status == "closed"
        and group.consumed_by_later_model_call
        and group.model_call_id not in suffix.model_call_group_ids
        for group in groups
    )


def _compression_failure_message(reason: str) -> str:
    if reason == "oldest_group_too_large":
        return (
            "Context compression could not fit the oldest eligible history group. "
            "The current turn was aborted. Start a new session to continue with a "
            "fresh context window."
        )
    return "Context compression failed. The current turn was aborted."


def make_compression_model_callable(model: object) -> Callable[[CompressionContextFrame], str]:
    def _call(frame: CompressionContextFrame) -> str:
        messages: list[dict[str, str]] = []
        if frame.previous_summary is not None:
            messages.append(
                {
                    "role": "system",
                    "content": frame.previous_summary,
                }
            )
        for message in frame.evicted_messages:
            messages.append(_provider_message_from_conversation(message))
        messages.append(_provider_message_from_conversation(frame.instruction_segment))
        response = model.invoke(messages)
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    return _call


def _provider_message_from_conversation(message: ConversationMessage) -> dict[str, str]:
    content = message.content
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    if message.artifact_refs:
        content = (
            f"{content}\n\nArtifact references: "
            f"{', '.join(message.artifact_refs)}"
        )
    return {"role": message.role, "content": content}


def _conversation_messages(conversation: list[dict[str, Any]]) -> list[ConversationMessage]:
    messages: list[ConversationMessage] = []
    for index, message in enumerate(conversation):
        role = str(message.get("role") or "user")
        content = message.get("content", "")
        metadata = dict(message.get("metadata", {}))
        durable_index = message.get("durable_message_index")
        if isinstance(durable_index, int):
            metadata.setdefault("durable_message_index", durable_index)
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
                metadata=metadata,
            )
        )
    return messages


def _conversation_message_durable_index(
    message: ConversationMessage,
) -> int | None:
    value = message.metadata.get("durable_message_index")
    if isinstance(value, int):
        return value
    return None


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
        tool_call_id = getattr(message, "tool_call_id", None)
        model_call_id = None
        if role == "assistant" and kind == "tool_call":
            tool_calls = _message_tool_calls(message)
            if tool_calls:
                content = {"content": content, "tool_calls": tool_calls}
                model_call_id = _model_call_id_from_tool_calls(tool_calls)
        if role == "tool" and kind == "tool_result" and isinstance(tool_call_id, str):
            content = {
                "message_type": "tool_result",
                "content": content,
                "tool_call_id": tool_call_id,
            }
            model_call_id = _model_call_id_from_tool_call_id(tool_call_id)
        if not isinstance(content, (str, dict)):
            content = str(content)
        converted.append(
            ConversationMessage(
                seq=index,
                role=str(role or "assistant"),
                kind=kind,
                turn_id=turn_id,
                model_call_id=model_call_id,
                tool_call_id=tool_call_id,
                content=content,
            )
        )
    return converted


def _message_tool_calls(message: object) -> list[dict[str, Any]]:
    tool_calls = getattr(message, "tool_calls", []) or []
    normalized: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if not isinstance(name, str) or not name:
            continue
        normalized.append(
            {
                "name": name,
                "args": call.get("args", {}),
                "id": str(call.get("id") or f"{name}_{index}"),
            }
        )
    return normalized


def _model_call_id_from_tool_calls(tool_calls: list[dict[str, Any]]) -> str | None:
    model_call_ids = {
        model_call_id
        for call in tool_calls
        if isinstance(call.get("id"), str)
        for model_call_id in [_model_call_id_from_tool_call_id(call["id"])]
        if model_call_id is not None
    }
    if len(model_call_ids) == 1:
        return next(iter(model_call_ids))
    return None


def _model_call_id_from_tool_call_id(tool_call_id: str) -> str | None:
    marker = "_tool_"
    if marker not in tool_call_id:
        return None
    model_call_id, _tool_index = tool_call_id.rsplit(marker, 1)
    return model_call_id or None


def _durable_append_from_conversation_message(
    message: dict[str, Any],
    *,
    turn_id: str,
    ordinal: int,
) -> ConversationAppend | None:
    role = str(message.get("role") or "")
    raw_kind = str(message.get("kind") or "")
    if role == "assistant" and raw_kind == "tool_call":
        kind = "assistant_tool_call"
    elif role == "tool" and raw_kind == "tool_result":
        kind = "tool_result"
    else:
        return None
    content = message.get("content", "")
    tool_call_id = message.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        tool_call_id = _tool_call_id_from_content(content)
    model_call_id = message.get("model_call_id")
    if not isinstance(model_call_id, str) or not model_call_id:
        model_call_id = (
            _model_call_id_from_tool_call_id(tool_call_id)
            if isinstance(tool_call_id, str)
            else None
        )
    return ConversationAppend(
        turn_id=turn_id,
        message_group_id=f"{turn_id}:tool-loop:{ordinal}",
        model_call_id=model_call_id,
        group_position=0,
        group_row_count=1,
        role=role,
        kind=kind,
        content=content,
        metadata=dict(message.get("metadata", {})),
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
    )


def _durable_message_indexes(conversation: list[dict[str, Any]]) -> list[int]:
    indexes: list[int] = []
    for message in conversation:
        value = message.get("durable_message_index")
        if isinstance(value, int):
            indexes.append(value)
            continue
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and isinstance(
            metadata.get("durable_message_index"),
            int,
        ):
            indexes.append(metadata["durable_message_index"])
    return indexes


def _tool_call_id_from_content(content: object) -> str | None:
    if not isinstance(content, dict):
        return None
    tool_call_id = content.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return tool_call_id
    tool_calls = content.get("tool_calls")
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        return None
    first = tool_calls[0]
    if not isinstance(first, dict):
        return None
    value = first.get("id")
    return value if isinstance(value, str) and value else None


def _durable_failure_append(
    *,
    turn_id: str,
    result: AgentRunResult,
) -> ConversationAppend:
    error = result.error if isinstance(result.error, dict) else {}
    error_class = str(error.get("error_class") or result.status)
    reason = str(error.get("reason") or error.get("error_class") or result.status)
    artifact_ids = error.get("artifact_ids")
    content = {
        "error_class": error_class,
        "reason": reason,
        "message": str(error.get("message") or "Prompt execution failed."),
        "artifact_ids": artifact_ids if isinstance(artifact_ids, list) else [],
    }
    kind = "cancellation_fact" if result.status == "cancelled" else "failure_fact"
    return ConversationAppend(
        turn_id=turn_id,
        message_group_id=f"{turn_id}:runtime:{kind}",
        model_call_id=None,
        group_position=0,
        group_row_count=1,
        role="runtime",
        kind=kind,
        content=content,
        metadata={
            "error_class": error_class,
            "reason": reason,
        },
    )


def _with_group_position(
    append: ConversationAppend,
    *,
    message_group_id: str,
    group_position: int,
    group_row_count: int,
) -> ConversationAppend:
    return ConversationAppend(
        turn_id=append.turn_id,
        message_group_id=message_group_id,
        model_call_id=append.model_call_id,
        group_position=group_position,
        group_row_count=group_row_count,
        role=append.role,
        kind=append.kind,
        content=append.content,
        artifact_id=append.artifact_id,
        metadata=append.metadata,
        source_event_id=append.source_event_id,
        tool_call_id=append.tool_call_id,
    )


def _with_context_metadata(
    result: AgentRunResult,
    *,
    context_estimate: dict[str, Any],
    context_estimate_history: list[dict[str, Any]],
    continuation_history: list[str],
    query_state: Any,
    context_optimization: dict[str, Any] | None = None,
    conversation_writeback: list[dict[str, Any]] | None = None,
    turn_tool_loop_messages: list[dict[str, Any]] | None = None,
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
    if turn_tool_loop_messages:
        metadata["turn_tool_loop_messages"] = turn_tool_loop_messages
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

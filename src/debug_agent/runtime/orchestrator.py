from __future__ import annotations

from collections.abc import Callable
import hashlib
import os
import json
import platform
import subprocess
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.adapters.model_factory import ModelFactory
from debug_agent.cli.exit_codes import (
    ERROR_ACTIVE_SESSION_CONFLICT,
    ERROR_EXECUTION_FAILED,
    ERROR_LOOKUP_NOT_FOUND,
    ERROR_STARTUP_CONFIG,
    ERROR_USAGE,
    map_error_to_exit_code,
)
from debug_agent.observability.logging import write_runtime_log
from debug_agent.observability.trace_writer import (
    TraceWriter,
    build_phase3_observability_summary,
)
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.conversation import ConversationAppend, ConversationStore
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeBootstrapError, RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.config import (
    EXECUTION_DEFAULTS,
    MULTIMODAL_LIMIT_DEFAULTS,
    PHASE_0_SYSTEM_PROMPT,
)
from debug_agent.runtime.contracts import (
    APPROVAL_MODES,
    AgentRunResult,
    RunEvent,
    utc_now_iso,
)
from debug_agent.runtime.errors import NormalizedError
from debug_agent.runtime.policy import load_main_agent_policy, policy_facts_to_snapshot
from debug_agent.runtime.prompt_executor import (
    PromptAgentExecutor,
    make_compression_model_callable,
)
from debug_agent.runtime.provider_resources import close_provider_resource
from debug_agent.runtime.stream_events import AgentStreamEvent
from debug_agent.runtime.workspace import resolve_workspace_root
from debug_agent.skills.registry import SkillRegistry, SkillRegistryError
from debug_agent.tools.broker import NonInteractiveApprovalProvider, ToolBroker
from debug_agent.tools.native import gated_user_facing_tool_definitions
from debug_agent.tools.view_image import tool_definition as view_image_tool_definition


@dataclass(frozen=True)
class OneShotResult:
    exit_code: int
    assistant_output: str | None
    error: dict[str, Any] | None
    message: str
    session_id: str | None
    run_id: str | None


@dataclass(frozen=True)
class StatusResult:
    exit_code: int
    fields: dict[str, Any]
    message: str


@dataclass(frozen=True)
class TraceResult:
    exit_code: int
    trace_path: Path
    summary: dict[str, Any]
    message: str


@dataclass(frozen=True)
class ResumeResult:
    exit_code: int
    message: str
    error: dict[str, Any] | None
    session_id: str | None


@dataclass(frozen=True)
class ReplStartError:
    exit_code: int
    message: str
    error: dict[str, Any] | None


class _RuntimeCancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class ReplRuntime:
    def __init__(
        self,
        *,
        db: RuntimeDatabase,
        sessions: SessionStore,
        runs: RunStore,
        events: EventWriter,
        checkpoints: CheckpointStore,
        executor: PromptAgentExecutor,
        session_id: str,
        run_id: str,
        workspace_root: Path,
        conversation: list[dict[str, Any]] | None = None,
        owner_token: str | None = None,
    ) -> None:
        self.db = db
        self.sessions = sessions
        self.runs = runs
        self.events = events
        self.checkpoints = checkpoints
        self.executor = executor
        self.session_id = session_id
        self.run_id = run_id
        self.workspace_root = workspace_root
        self.owner_token = owner_token
        self.conversation: list[dict[str, Any]] = (
            [] if conversation is None else [dict(message) for message in conversation]
        )
        self.turn_counter = _max_repl_turn_counter(self.conversation)
        self.latest_context_estimate: dict[str, Any] | None = None
        self.approval_provider = NonInteractiveApprovalProvider()
        self.closed = False
        self._lock = threading.RLock()
        self._active_cancellation_token: _RuntimeCancellationToken | None = None
        self._active_provider_handles: list[Any] = []
        self._active_shell_handles: list[Any] = []

    def run_turn(
        self,
        user_input: str,
        agent_stream_callback: Callable[[AgentStreamEvent], None] | None = None,
    ) -> AgentRunResult:
        with self._lock:
            self.turn_counter += 1
            session = self.sessions.get(self.session_id)
            run = self.runs.get(self.run_id)
            turn_counter = self.turn_counter
            conversation = [dict(message) for message in self.conversation]
            cancellation_token = _RuntimeCancellationToken()
            self._active_cancellation_token = cancellation_token
            self._active_provider_handles = []
            self._active_shell_handles = []

        def runtime_stream_callback(event: AgentStreamEvent) -> None:
            if event.kind == "stream_context_estimate_updated":
                estimate = event.payload.get("context_estimate")
                if isinstance(estimate, dict):
                    with self._lock:
                        self.latest_context_estimate = dict(estimate)
            if agent_stream_callback is not None:
                agent_stream_callback(event)

        try:
            result = self.executor.run_turn(
                session=session,
                run=run,
                user_input=user_input,
                workspace_root=str(self.workspace_root),
                conversation=conversation,
                prompt_turn_counter=turn_counter,
                approval_provider=self.approval_provider,
                agent_stream_callback=runtime_stream_callback
                if agent_stream_callback is not None
                else None,
                cancellation_token=cancellation_token,
                provider_cancellation_registry=self._register_provider_cancellation_handle,
                shell_process_registry=self._register_shell_process_handle,
            )
        except KeyboardInterrupt:
            self.cancel_running_turn(collect_provider_boundaries=True)
            result = _running_cancelled_result()
        finally:
            with self._lock:
                self._active_cancellation_token = None
                self._active_provider_handles = []
                self._active_shell_handles = []
        with self._lock:
            self._append_turn_conversation(user_input, result)
            return result

    def cancel_running_turn(
        self,
        *,
        collect_provider_boundaries: bool = False,
    ) -> AgentRunResult:
        with self._lock:
            token = self._active_cancellation_token
            handles = list(self._active_provider_handles)
            shell_handles = list(self._active_shell_handles)
        if token is not None:
            token.cancel()
        for handle in handles:
            cancel = getattr(handle, "cancel", None)
            if callable(cancel):
                cancel()
        if collect_provider_boundaries:
            for handle in handles:
                collect = getattr(handle, "collect_boundary", None)
                if not callable(collect):
                    collect = getattr(handle, "collect", None)
                if callable(collect):
                    collect()
        for handle in shell_handles:
            terminate = getattr(handle, "terminate", None)
            if callable(terminate):
                terminate()
        return _running_cancelled_result()

    def cancel_idle(self) -> None:
        self.fail(_idle_cancelled_result(prompt_turn_counter=self.turn_counter))

    def _register_provider_cancellation_handle(self, handle: Any) -> None:
        with self._lock:
            self._active_provider_handles.append(handle)

    def _register_shell_process_handle(self, handle: Any) -> None:
        with self._lock:
            self._active_shell_handles.append(handle)

    def _append_turn_conversation(
        self,
        user_input: str,
        result: AgentRunResult,
    ) -> None:
        estimate = result.metadata.get("context_estimate")
        if isinstance(estimate, dict):
            self.latest_context_estimate = estimate
        writeback = result.metadata.get("conversation_writeback")
        has_writeback = isinstance(writeback, list)
        if has_writeback:
            self.conversation = [dict(message) for message in writeback]
        next_seq = _next_conversation_seq(self.conversation)
        turn_id = f"turn-{self.turn_counter}"
        self.conversation.append(
            {
                "seq": next_seq,
                "role": "user",
                "kind": "current_user_input",
                "turn_id": turn_id,
                "model_call_id": None,
                "tool_call_id": None,
                "content": user_input,
                "artifact_refs": [],
                "metadata": {},
            }
        )
        next_seq += 1
        turn_tool_loop_messages = result.metadata.get("turn_tool_loop_messages")
        if isinstance(turn_tool_loop_messages, list):
            for raw_message in turn_tool_loop_messages:
                if not isinstance(raw_message, dict):
                    continue
                message = dict(raw_message)
                message["seq"] = next_seq
                message.setdefault("turn_id", turn_id)
                message.setdefault("artifact_refs", [])
                message.setdefault("metadata", {})
                self.conversation.append(message)
                next_seq += 1
        if result.status == "completed":
            consumed_ids = _consumed_model_call_ids(self.conversation)
            self.conversation.append(
                {
                    "seq": next_seq,
                    "role": "assistant",
                    "kind": "assistant_output",
                    "turn_id": turn_id,
                    "model_call_id": f"repl_turn_{self.turn_counter}_assistant",
                    "tool_call_id": None,
                    "content": result.assistant_output or "",
                    "artifact_refs": [],
                    "metadata": {
                        "consumed_model_call_ids": consumed_ids,
                    },
                }
            )
            self._sync_durable_message_indexes()
            return
        error = result.error if isinstance(result.error, dict) else {}
        if (
            result.status == "cancelled"
            and not has_writeback
            and not _durable_turn_runtime_fact_exists(
                self.db.connection,
                run_id=self.run_id,
                turn_id=turn_id,
                kind="cancellation_fact",
            )
        ):
            content = {
                "error_class": str(error.get("error_class") or "cancelled"),
                "reason": str(error.get("reason") or "user_cancel_running"),
                "message": str(error.get("message") or "Turn cancelled by user."),
                "artifact_ids": error.get("artifact_ids")
                if isinstance(error.get("artifact_ids"), list)
                else [],
            }
            self.conversation.append(
                {
                    "seq": next_seq,
                    "role": "runtime",
                    "kind": "cancellation_fact",
                    "turn_id": turn_id,
                    "model_call_id": None,
                    "tool_call_id": None,
                    "content": content,
                    "artifact_refs": [],
                    "metadata": {
                        "error_class": content["error_class"],
                        "reason": content["reason"],
                    },
                }
            )
            ConversationStore(self.db.connection).append_closed_group(
                session_id=self.session_id,
                run_id=self.run_id,
                messages=[
                    ConversationAppend(
                        turn_id=turn_id,
                        message_group_id=f"{turn_id}:runtime:cancellation_fact",
                        model_call_id=None,
                        group_position=0,
                        group_row_count=1,
                        role="runtime",
                        kind="cancellation_fact",
                        content=content,
                        metadata={
                            "error_class": content["error_class"],
                            "reason": content["reason"],
                        },
                    )
                ],
            )
            self._sync_durable_message_indexes()
            return
        self.conversation.append(
            {
                "seq": next_seq,
                "role": "assistant",
                "kind": "turn_failure_observation",
                "turn_id": turn_id,
                "model_call_id": f"repl_turn_{self.turn_counter}_failure",
                "tool_call_id": None,
                "content": {
                    "status": result.status,
                    "error_class": str(error.get("error_class") or result.status),
                    "message": str(error.get("message") or "Prompt execution failed."),
                },
                "artifact_refs": [],
                "metadata": {
                    "failure_scope": result.metadata.get("failure_scope"),
                },
            }
        )
        self._sync_durable_message_indexes()

    def _sync_durable_message_indexes(self) -> None:
        store = getattr(self.executor, "conversation_store", None)
        if store is None:
            return
        projection = store.get_projection(self.run_id)
        indexes = _projection_indexes(projection.message_refs)
        if len(indexes) != len(self.conversation):
            return
        for message, durable_index in zip(self.conversation, indexes, strict=True):
            message["durable_message_index"] = durable_index

    def set_approval_provider(self, approval_provider: object) -> None:
        with self._lock:
            self.approval_provider = approval_provider

    def manual_compress(self) -> AgentRunResult:
        with self._lock:
            session = self.sessions.get(self.session_id)
            run = self.runs.get(self.run_id)
            result = self.executor.manual_compress(
                session=session,
                run=run,
                conversation=self.conversation,
                prompt_turn_counter=self.turn_counter,
            )
            estimate = result.metadata.get("context_estimate")
            if isinstance(estimate, dict):
                self.latest_context_estimate = estimate
            if result.status == "completed":
                writeback = result.metadata.get("conversation_writeback")
                if isinstance(writeback, list):
                    self.conversation = [dict(message) for message in writeback]
                    self._sync_durable_message_indexes()
            return result

    def status_lines(self) -> list[str]:
        with self._lock:
            session = self.sessions.get(self.session_id)
            latest_run = self.runs.latest_for_session(self.session_id)
            fields = {
                "session_id": session.session_id,
                "workspace_root": session.workspace_root,
                "session_status": session.status,
                "approval_mode": session.approval_mode,
                "active_run_id": session.active_run_id,
                "latest_run_id": latest_run.run_id if latest_run else None,
                "latest_run_status": latest_run.status if latest_run else None,
                "latest_checkpoint_id": session.latest_checkpoint_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "error_summary": session.error_summary,
            }
            return [f"{key}: {value or ''}" for key, value in fields.items()]

    def skill_lines(self) -> list[str]:
        with self._lock:
            run = self.runs.get(self.run_id)
            records = SkillSnapshotStore(self.db.connection).list_for_run(
                session_id=self.session_id,
                run_id=self.run_id,
                active_skills=run.active_skills,
            )
            if not records:
                return ["Skills: none"]
            lines: list[str] = []
            for record in records:
                active = "active" if record.active else "inactive"
                lines.extend(
                    [
                        "",
                        f"- {record.name} ({record.source_scope}) [{active}]",
                        record.description,
                    ]
                )
            return lines

    def tool_lines(self) -> list[str]:
        with self._lock:
            session = self.sessions.get(self.session_id)
            return format_tool_listing(
                visible_tool_definitions(session.config_snapshot),
                approval_mode=session.approval_mode,
                config_snapshot=session.config_snapshot,
            )

    def cycle_approval_mode(self) -> tuple[str, str]:
        with self._lock:
            session = self.sessions.get(self.session_id)
            old_mode = session.approval_mode
            new_mode = _next_approval_mode(old_mode)
            session = self.sessions.update_approval_mode(session.session_id, new_mode)
            _append_event(
                self.events,
                session.session_id,
                self.run_id,
                "approval_mode_changed",
                {"old_mode": old_mode, "new_mode": new_mode},
            )
            return old_mode, new_mode

    def complete(self) -> None:
        with self._lock:
            if self.closed:
                return
            session = self.sessions.get(self.session_id)
            run = self.runs.get(self.run_id)
            checkpoint = self.checkpoints.terminalize_with_recovery_checkpoint(
                checkpoint_id=f"chk_{uuid4().hex}",
                session_id=session.session_id,
                run_id=run.run_id,
                terminal_status="completed",
                terminal_reason="user_exit",
                terminal_error=None,
                error_summary=None,
                created_at=utc_now_iso(),
            )
            _append_event(
                self.events,
                session.session_id,
                run.run_id,
                "checkpoint_written",
                {"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
            )
            run = self.runs.get(run.run_id)
            session = self.sessions.get(session.session_id)
            _append_event(self.events, session.session_id, run.run_id, "run_completed", {})
            _append_event(
                self.events, session.session_id, run.run_id, "session_completed", {}
            )
            TraceWriter(self.db.connection, self.db.path.parent).refresh_if_stale(
                session.session_id
            )
            self._release_active_ownership()
            self.close()

    def fail(self, result: AgentRunResult) -> None:
        with self._lock:
            if self.closed:
                return
            _mark_failed_terminal(
                sessions=self.sessions,
                runs=self.runs,
                events=self.events,
                checkpoints=self.checkpoints,
                trace_writer=TraceWriter(self.db.connection, self.db.path.parent),
                session_id=self.session_id,
                run_id=self.run_id,
                agent_result=result,
            )
            self._release_active_ownership()
            self.close()

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                _close_executor_provider_resources(self.executor)
                self.db.close()
                self.closed = True

    def _release_active_ownership(self) -> None:
        if self.owner_token is None:
            return
        released = self.sessions.release_ownership(
            session_id=self.session_id,
            owner_token=self.owner_token,
        )
        if released:
            self.owner_token = None
            return
        _record_ownership_release_failed_event(
            self.events,
            session_id=self.session_id,
            run_id=self.run_id,
        )
        write_runtime_log(
            self.db.path.parent,
            session_id=self.session_id,
            run_id=self.run_id,
            level="ERROR",
            event="ownership_release_failed",
            message="Active ownership release failed after terminalization.",
            metadata={"reason": "runtime_error/ownership_release_failed"},
        )


@dataclass(frozen=True)
class ReplStartResult:
    runtime: ReplRuntime | None
    error: ReplStartError | None


class RuntimeOrchestrator:
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        stale_confirmation: Callable[[dict[str, Any]], bool] | None = None,
        host_identity_provider: Any | None = None,
        process_liveness: Any | None = None,
    ) -> None:
        self.workspace_root = resolve_workspace_root(workspace_root)
        self._stale_confirmation = stale_confirmation
        self._host_identity_provider = host_identity_provider or _HostIdentityProvider()
        self._process_liveness = process_liveness or _ProcessLiveness()

    def _try_fail_close_stale_owner(
        self,
        *,
        db: RuntimeDatabase,
        sessions: SessionStore,
        checkpoints: CheckpointStore,
        active: Any | None,
    ) -> OneShotResult | None:
        if active is None:
            return None
        proof = _capture_stale_proof(
            db.connection,
            workspace_root=self.workspace_root,
            session_id=active.session_id,
            host_identity_provider=self._host_identity_provider,
            process_liveness=self._process_liveness,
        )
        if proof.error_reason is not None:
            return _ownership_conflict_one_shot(
                active,
                reason=proof.error_reason,
                message=_active_conflict_message(active.session_id),
            )
        if self._stale_confirmation is None:
            return _ownership_conflict_one_shot(
                active,
                reason="workspace_owner_confirmation_unavailable",
                message=_stale_confirmation_unavailable_message(active.session_id),
            )
        if not self._stale_confirmation(_stale_confirmation_request(proof)):
            return _ownership_conflict_one_shot(
                active,
                reason="workspace_owner_active",
                message=_active_conflict_message(active.session_id),
            )
        checkpoint = _prepare_stale_terminal_checkpoint(checkpoints, proof)
        closed = sessions.fail_close_stale_owner(
            workspace_root=self.workspace_root,
            session_id=proof.session_id,
            run_id=proof.run_id,
            owner_pid=proof.owner_pid,
            owner_host_id=proof.owner_host_id,
            owner_token=proof.owner_token,
            checkpoint_id=checkpoint.checkpoint_id if checkpoint is not None else None,
            checkpoint=checkpoint,
        )
        if not closed:
            return _ownership_conflict_one_shot(
                active,
                reason="workspace_owner_active",
                message=_active_conflict_message(active.session_id),
            )
        TraceWriter(db.connection, db.path.parent).refresh_if_stale(active.session_id)
        return OneShotResult(
            exit_code=0,
            assistant_output=None,
            error=None,
            message="Stale owner failed closed.",
            session_id=active.session_id,
            run_id=proof.run_id,
        )

    def run_one_shot(
        self,
        prompt: str,
        config_snapshot: dict[str, Any],
        *,
        approval_mode: str = "normal",
    ) -> OneShotResult:
        if approval_mode not in APPROVAL_MODES:
            return OneShotResult(
                exit_code=ERROR_USAGE,
                assistant_output=None,
                error={
                    "error_class": "config_error",
                    "message": "approval mode must be one of: normal, semi-auto, yolo",
                    "source": "orchestrator",
                    "recoverable": False,
                },
                message="approval mode must be one of: normal, semi-auto, yolo",
                session_id=None,
                run_id=None,
            )
        policy_result = self._freeze_policy(config_snapshot)
        if isinstance(policy_result, OneShotResult):
            return policy_result
        config_snapshot = _ensure_phase3_frozen_runtime_defaults(policy_result)
        provider_model = None
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return _bootstrap_one_shot_error(exc)
        sessions_root = db.path.parent
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection, sessions_root)
        artifacts = ArtifactStore(db.connection, sessions_root)
        checkpoints = CheckpointStore(db.connection, artifact_store=artifacts)
        try:
            try:
                session = sessions.create(
                    workspace_root=self.workspace_root,
                    approval_mode=approval_mode,
                    config_snapshot=config_snapshot,
                )
            except StoreError as exc:
                active = sessions.find_active_for_workspace(self.workspace_root)
                stale_result = self._try_fail_close_stale_owner(
                    db=db,
                    sessions=sessions,
                    checkpoints=checkpoints,
                    active=active,
                )
                if stale_result is not None:
                    if stale_result.exit_code != 0:
                        return stale_result
                    session = sessions.create(
                        workspace_root=self.workspace_root,
                        approval_mode=approval_mode,
                        config_snapshot=config_snapshot,
                    )
                else:
                    message = _active_conflict_message(
                        active.session_id if active else "unknown"
                    )
                    if active is not None:
                        write_runtime_log(
                            sessions_root,
                            session_id=active.session_id,
                            run_id=active.active_run_id,
                            level="ERROR",
                            event="ownership_conflict",
                            message=message,
                            metadata={"workspace_root": str(self.workspace_root)},
                        )
                    return OneShotResult(
                        exit_code=ERROR_ACTIVE_SESSION_CONFLICT,
                        assistant_output=None,
                        error={
                            "error_class": exc.error_class,
                            "message": message,
                            "source": exc.source,
                            "recoverable": exc.recoverable,
                        },
                        message=message,
                        session_id=active.session_id if active else None,
                        run_id=None,
                    )
            run = runs.create_prompt_run(session.session_id)
            session = sessions.set_active_run(session.session_id, run.run_id)
            owner_facts = _current_owner_facts()
            session = sessions.record_owner(
                session_id=session.session_id,
                owner_pid=int(owner_facts["pid"]),
                owner_host_id=str(owner_facts["host_id"]),
                owner_token=str(owner_facts["owner_token"]),
            )
            _append_event(events, session.session_id, run.run_id, "session_started", {})
            _append_event(events, session.session_id, run.run_id, "run_started", {})
            startup_error = _snapshot_skills_for_startup(
                workspace_root=self.workspace_root,
                artifacts=artifacts,
                connection=db.connection,
                events=events,
                session_id=session.session_id,
                run_id=run.run_id,
            )
            if startup_error is not None:
                failed = _mark_failed_terminal(
                    sessions=sessions,
                    runs=runs,
                    events=events,
                    checkpoints=checkpoints,
                    trace_writer=TraceWriter(db.connection, sessions_root),
                    session_id=session.session_id,
                    run_id=run.run_id,
                    agent_result=AgentRunResult(
                        status="failed",
                        assistant_output=None,
                        tool_results=[],
                        usage={},
                        error=startup_error,
                        metadata={"prompt_turn_counter": 0},
                    ),
                    startup_failure=True,
                )
                _release_ownership_after_terminalization(
                    sessions=sessions,
                    events=events,
                    sessions_root=sessions_root,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    owner_token=str(owner_facts["owner_token"]),
                )
                return OneShotResult(
                    exit_code=1,
                    assistant_output=None,
                    error=failed.error,
                    message=failed.message,
                    session_id=failed.session_id,
                    run_id=failed.run_id,
                )

            model_result = ModelFactory().create(config_snapshot)
            if model_result.error is not None:
                failed = _mark_failed_terminal(
                    sessions=sessions,
                    runs=runs,
                    events=events,
                    checkpoints=checkpoints,
                    trace_writer=TraceWriter(db.connection, sessions_root),
                    session_id=session.session_id,
                    run_id=run.run_id,
                    agent_result=AgentRunResult(
                        status="failed",
                        assistant_output=None,
                        tool_results=[],
                        usage={},
                        error=model_result.error,
                        metadata={"prompt_turn_counter": 0},
                    ),
                    startup_failure=True,
                )
                _release_ownership_after_terminalization(
                    sessions=sessions,
                    events=events,
                    sessions_root=sessions_root,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    owner_token=str(owner_facts["owner_token"]),
                )
                return OneShotResult(
                    exit_code=ERROR_STARTUP_CONFIG,
                    assistant_output=None,
                    error=failed.error,
                    message=failed.message,
                    session_id=failed.session_id,
                    run_id=failed.run_id,
                )

            broker = ToolBroker(event_writer=events, artifact_store=artifacts)
            provider_model = model_result.model
            adapter = LangChainAgentLoopAdapter(
                model=model_result.model,
                tool_broker=broker,
            )
            executor = PromptAgentExecutor(
                event_writer=events,
                checkpoint_store=checkpoints,
                artifact_store=artifacts,
                adapter=adapter,
                tool_definitions=visible_tool_definitions(config_snapshot),
                system_prompt=config_snapshot.get("system_prompt", PHASE_0_SYSTEM_PROMPT),
                skill_snapshot_store=SkillSnapshotStore(db.connection),
                todo_plan_store=TodoPlanStore(db.connection),
                conversation_store=ConversationStore(
                    db.connection,
                    artifact_store=artifacts,
                ),
                run_store=runs,
                compression_model=make_compression_model_callable(model_result.model),
            )
            agent_result = executor.run_turn(
                session=session,
                run=run,
                user_input=prompt,
                workspace_root=str(self.workspace_root),
            )
            if agent_result.status == "completed":
                terminal_checkpoint = checkpoints.terminalize_with_recovery_checkpoint(
                    checkpoint_id=f"chk_{uuid4().hex}",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    terminal_status="completed",
                    terminal_reason="terminal_completion",
                    terminal_error=None,
                    error_summary=None,
                    created_at=utc_now_iso(),
                    artifact_ids=_artifact_ids(agent_result),
                )
                _append_event(
                    events,
                    session.session_id,
                    run.run_id,
                    "checkpoint_written",
                    {
                        "checkpoint_id": terminal_checkpoint.checkpoint_id,
                        "kind": terminal_checkpoint.kind,
                    },
                )
                run = runs.get(run.run_id)
                session = sessions.get(session.session_id)
                _append_event(events, session.session_id, run.run_id, "run_completed", {})
                _append_event(events, session.session_id, run.run_id, "session_completed", {})
                TraceWriter(db.connection, sessions_root).refresh_if_stale(
                    session.session_id
                )
                _release_ownership_after_terminalization(
                    sessions=sessions,
                    events=events,
                    sessions_root=sessions_root,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    owner_token=str(owner_facts["owner_token"]),
                )
                return OneShotResult(
                    exit_code=0,
                    assistant_output=agent_result.assistant_output,
                    error=None,
                    message=_startup_success_message(
                        db,
                        agent_result.assistant_output or "",
                    ),
                    session_id=session.session_id,
                    run_id=run.run_id,
                )
            failed = _mark_failed_terminal(
                sessions=sessions,
                runs=runs,
                events=events,
                checkpoints=checkpoints,
                trace_writer=TraceWriter(db.connection, sessions_root),
                session_id=session.session_id,
                run_id=run.run_id,
                agent_result=agent_result,
            )
            _release_ownership_after_terminalization(
                sessions=sessions,
                events=events,
                sessions_root=sessions_root,
                session_id=session.session_id,
                run_id=run.run_id,
                owner_token=str(owner_facts["owner_token"]),
            )
            return failed
        finally:
            if provider_model is not None:
                close_provider_resource(provider_model)
            db.close()

    def status(self, session_id: str) -> StatusResult:
        try:
            db = RuntimeDatabase.bootstrap_read_only(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return StatusResult(
                exit_code=map_error_to_exit_code(exc.normalized_error),
                fields={},
                message=str(exc),
            )
        if db is None:
            return StatusResult(
                exit_code=0,
                fields={"runtime_database": "missing", "active_session": None},
                message="",
            )
        try:
            sessions = SessionStore(db.connection)
            runs = RunStore(db.connection)
            try:
                session = sessions.get(session_id)
            except StoreError as exc:
                return StatusResult(
                    exit_code=ERROR_LOOKUP_NOT_FOUND,
                    fields={},
                    message=exc.message,
                )
            latest_run = runs.latest_for_session(session.session_id)
            events = EventWriter(db.connection, db.path.parent).list_for_session(
                session.session_id
            )
            fields = {
                "session_id": session.session_id,
                "workspace_root": session.workspace_root,
                "session_status": session.status,
                "approval_mode": session.approval_mode,
                "active_run_id": session.active_run_id,
                "latest_run_id": latest_run.run_id if latest_run else None,
                "latest_run_status": latest_run.status if latest_run else None,
                "latest_checkpoint_id": session.latest_checkpoint_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "error_summary": session.error_summary,
            }
            fields.update(
                build_phase3_observability_summary(
                    db.connection,
                    session=session,
                    latest_run=latest_run,
                    events=events,
                )
            )
            if latest_run is not None:
                plan = TodoPlanStore(db.connection).get_current(latest_run.run_id)
                if plan.version > 0:
                    fields["todo_plan"] = {
                        "plan_version": plan.version,
                        "counts": _todo_counts(plan.items),
                    }
            return StatusResult(exit_code=0, fields=fields, message="")
        finally:
            db.close()

    def trace(self, session_id: str) -> TraceResult:
        try:
            db = RuntimeDatabase.bootstrap_read_only(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return TraceResult(
                exit_code=map_error_to_exit_code(exc.normalized_error, boundary="trace"),
                trace_path=Path(),
                summary={},
                message=str(exc),
            )
        if db is None:
            return TraceResult(
                exit_code=ERROR_LOOKUP_NOT_FOUND,
                trace_path=Path(),
                summary={},
                message=f"No session found for id: {session_id}",
            )
        sessions_root = db.path.parent
        try:
            try:
                result = TraceWriter(db.connection, sessions_root).refresh_if_stale(
                    session_id
                )
            except StoreError as exc:
                return TraceResult(
                    exit_code=ERROR_LOOKUP_NOT_FOUND,
                    trace_path=Path(),
                    summary={},
                    message=exc.message,
                )
            summary = {
                "trace_path": str(result.trace_path),
                "refreshed": result.refreshed,
                "session_id": result.session_id,
                "workspace_root": result.workspace_root,
                "run_count": result.run_count,
                "event_count": result.event_count,
                "artifact_count": result.artifact_count,
                "terminal_status": result.terminal_status,
                "error_summary": result.error_summary,
            }
            return TraceResult(
                exit_code=0,
                trace_path=result.trace_path,
                summary=summary,
                message="",
            )
        finally:
            db.close()

    def resume(self, session_id: str) -> ResumeResult:
        result = self._resume_lineage(session_id)
        if result.exit_code != 0:
            return result
        return ResumeResult(
            exit_code=0,
            message=f"Resumed session {session_id}",
            error=None,
            session_id=session_id,
        )

    def start_resumed_repl(self, session_id: str) -> ReplStartResult:
        resume = self._resume_lineage(session_id)
        if resume.exit_code != 0:
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=resume.exit_code,
                    message=resume.message,
                    error=resume.error,
                ),
            )
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=map_error_to_exit_code(exc.normalized_error),
                    message=str(exc),
                    error=exc.normalized_error.to_dict(),
                ),
            )
        try:
            runtime = _runtime_from_resumed_session(
                db=db,
                workspace_root=self.workspace_root,
                session_id=session_id,
            )
        except StoreError as exc:
            _rollback_failed_resume_handoff(db.connection, session_id=session_id)
            db.close()
            error = NormalizedError.create(
                "persistence_error",
                "persistence_read_failed",
                message=exc.message,
                scope="persistence",
            )
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=map_error_to_exit_code(error),
                    message=exc.message,
                    error=error.to_dict(),
                ),
            )
        return ReplStartResult(runtime=runtime, error=None)

    def _preflight_resumed_repl(self, session_id: str) -> ReplStartResult | None:
        try:
            db = RuntimeDatabase.bootstrap_read_only(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=map_error_to_exit_code(exc.normalized_error),
                    message=str(exc),
                    error=exc.normalized_error.to_dict(),
                ),
            )
        if db is None:
            error = NormalizedError.create(
                "user_error",
                "lookup_not_found",
                message=f"No session found for id: {session_id}",
                scope="session",
            )
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=map_error_to_exit_code(error),
                    message=error.message,
                    error=error.to_dict(),
                ),
            )
        try:
            _preflight_resumed_runtime_construction(
                db=db,
                session_id=session_id,
            )
            return None
        except StoreError as exc:
            error = NormalizedError.create(
                "persistence_error",
                "persistence_read_failed",
                message=exc.message,
                scope="persistence",
            )
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=map_error_to_exit_code(error),
                    message=exc.message,
                    error=error.to_dict(),
                ),
            )
        finally:
            db.close()

    def _resume_lineage(self, session_id: str) -> ResumeResult:
        try:
            db = RuntimeDatabase.bootstrap_read_only(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return ResumeResult(
                exit_code=map_error_to_exit_code(exc.normalized_error),
                message=str(exc),
                error=exc.normalized_error.to_dict(),
                session_id=None,
            )
        if db is None:
            return ResumeResult(
                exit_code=ERROR_LOOKUP_NOT_FOUND,
                message=f"No session found for id: {session_id}",
                error={
                    "error_class": "user_error",
                    "reason": "lookup_not_found",
                    "message": f"No session found for id: {session_id}",
                },
                session_id=None,
            )
        db.close()
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return ResumeResult(
                exit_code=map_error_to_exit_code(exc.normalized_error),
                message=str(exc),
                error=exc.normalized_error.to_dict(),
                session_id=None,
            )
        try:
            sessions = SessionStore(db.connection)
            runs = RunStore(db.connection)
            events = EventWriter(db.connection, db.path.parent)
            artifacts = ArtifactStore(db.connection, db.path.parent)
            conversation_store = ConversationStore(db.connection, artifact_store=artifacts)
            checkpoints = CheckpointStore(
                db.connection,
                conversation_store=conversation_store,
                todo_plan_store=TodoPlanStore(db.connection),
                artifact_store=artifacts,
            )
            try:
                session = sessions.get(session_id)
            except StoreError as exc:
                return ResumeResult(
                    exit_code=ERROR_LOOKUP_NOT_FOUND,
                    message=exc.message,
                    error={
                        "error_class": "user_error",
                        "reason": "lookup_not_found",
                        "message": exc.message,
                    },
                    session_id=None,
                )
            run = runs.latest_for_session(session.session_id)
            if run is None:
                return _resume_error(
                    session_id=session.session_id,
                    message=f"No prompt run found for session id: {session.session_id}",
                    error_class="user_error",
                    reason="lookup_not_found",
                )
            if (
                run.run_type != "prompt"
                or session.non_resumable_startup_failure
                or run.non_resumable_startup_failure
            ):
                return _resume_error(
                    session_id=session.session_id,
                    message=f"Session is not eligible for resume: {session.session_id}",
                    error_class="runtime_error",
                    reason="resume_not_eligible",
                )
            stale_target_closed = False
            if session.status == "running" or run.status == "running":
                active = sessions.find_active_for_workspace(self.workspace_root)
                if active is None or active.session_id != session.session_id:
                    return _resume_error(
                        session_id=session.session_id,
                        message=f"Session is not eligible for resume: {session.session_id}",
                        error_class="runtime_error",
                        reason="resume_not_eligible",
                    )
                stale_result = self._try_fail_close_stale_owner(
                    db=db,
                    sessions=sessions,
                    checkpoints=checkpoints,
                    active=active,
                )
                if stale_result is None:
                    return _resume_error(
                        session_id=session.session_id,
                        message=f"Session is not eligible for resume: {session.session_id}",
                        error_class="runtime_error",
                        reason="resume_not_eligible",
                    )
                if stale_result.exit_code != 0:
                    error = stale_result.error or {}
                    return _resume_error(
                        session_id=session.session_id,
                        message=stale_result.message,
                        error_class=str(error.get("error_class") or "policy_error"),
                        reason=str(error.get("reason") or "workspace_owner_active"),
                    )
                stale_target_closed = True
                session = sessions.get(session.session_id)
                run = runs.get(run.run_id)
            if session.status not in {"completed", "failed"} or run.status not in {
                "completed",
                "failed",
            }:
                return _resume_error(
                    session_id=session.session_id,
                    message=f"Session is not eligible for resume: {session.session_id}",
                    error_class="runtime_error",
                    reason="resume_not_eligible",
                )
            checkpoint_id = session.latest_checkpoint_id
            if not checkpoint_id or checkpoint_id != run.latest_checkpoint_id:
                message = (
                    f"Target session cannot be recovered after stale fail-close: {session.session_id}"
                    if stale_target_closed
                    else f"Session requires a terminal recovery checkpoint: {session.session_id}"
                )
                return _resume_error(
                    session_id=session.session_id,
                    message=message,
                    error_class="runtime_error",
                    reason="resume_checkpoint_required",
                )
            try:
                checkpoint = checkpoints.get(checkpoint_id)
            except StoreError:
                return _resume_error(
                    session_id=session.session_id,
                    message=f"Checkpoint not found for resume: {checkpoint_id}",
                    error_class="persistence_error",
                    reason="checkpoint_missing",
                )
            if (
                checkpoint.session_id != session.session_id
                or checkpoint.run_id != run.run_id
            ):
                return _resume_error(
                    session_id=session.session_id,
                    message="Terminal recovery checkpoint identity does not match resume target.",
                    error_class="persistence_error",
                    reason="checkpoint_invalid",
                )
            try:
                checkpoints.validate_terminal_recovery(
                    checkpoint,
                    validate_current_todo=False,
                )
            except StoreError as exc:
                return _resume_error(
                    session_id=session.session_id,
                    message=exc.message,
                    error_class="persistence_error",
                    reason="checkpoint_invalid",
                )
            try:
                conversation = _conversation_from_checkpoint_projection(
                    conversation_store=conversation_store,
                    run_id=run.run_id,
                    checkpoint_state=checkpoint.state,
                )
            except StoreError as exc:
                return _resume_error(
                    session_id=session.session_id,
                    message=exc.message,
                    error_class="persistence_error",
                    reason="conversation_cut_invalid",
                )
            active = sessions.find_active_for_workspace(self.workspace_root)
            if active is not None:
                stale_result = self._try_fail_close_stale_owner(
                    db=db,
                    sessions=sessions,
                    checkpoints=checkpoints,
                    active=active,
                )
                if stale_result is None:
                    return _resume_error(
                        session_id=session.session_id,
                        message=_active_conflict_message(active.session_id),
                        error_class="policy_error",
                        reason="workspace_owner_active",
                    )
                if stale_result.exit_code != 0:
                    error = stale_result.error or {}
                    return _resume_error(
                        session_id=session.session_id,
                        message=stale_result.message,
                        error_class=str(error.get("error_class") or "policy_error"),
                        reason=str(error.get("reason") or "workspace_owner_active"),
                    )
                session = sessions.get(session.session_id)
                run = runs.get(run.run_id)
            owner_facts = _current_owner_facts()
            try:
                _revive_same_lineage(
                    db.connection,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    owner_facts=owner_facts,
                    checkpoint_state=checkpoint.state,
                )
                _append_event(
                    events,
                    session.session_id,
                    run.run_id,
                    "session_resumed",
                    {
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "owner_pid": owner_facts["pid"],
                        "owner_host_id": owner_facts["host_id"],
                    },
                )
                _append_event(
                    events,
                    session.session_id,
                    run.run_id,
                    "run_resumed",
                    {"checkpoint_id": checkpoint.checkpoint_id},
                )
                TraceWriter(db.connection, db.path.parent).refresh_if_stale(
                    session.session_id
                )
            except StoreError as exc:
                return _resume_error(
                    session_id=session.session_id,
                    message=exc.message,
                    error_class="persistence_error",
                    reason="persistence_transition_failed",
                )
            return ResumeResult(
                exit_code=0,
                message=f"Resumed session {session.session_id}",
                error=None,
                session_id=session.session_id,
            )
        finally:
            db.close()

    def cancel_active_session(self, message: str) -> OneShotResult:
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return _bootstrap_one_shot_error(exc)
        sessions_root = db.path.parent
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection, sessions_root)
        artifacts = ArtifactStore(db.connection, sessions_root)
        checkpoints = CheckpointStore(db.connection, artifact_store=artifacts)
        try:
            session = sessions.find_active_for_workspace(self.workspace_root)
            error = {
                "error_class": "cancelled",
                "message": message,
                "source": "cli",
                "recoverable": False,
            }
            if session is None:
                return OneShotResult(
                    exit_code=1,
                    assistant_output=None,
                    error=error,
                    message=message,
                    session_id=None,
                    run_id=None,
                )
            run_id = session.active_run_id
            owner_token = _owner_token_for_session(db.connection, session.session_id)
            if run_id is None:
                session = sessions.mark_failed(session.session_id, message)
                if owner_token is not None:
                    _release_ownership_after_terminalization(
                        sessions=sessions,
                        events=events,
                        sessions_root=sessions_root,
                        session_id=session.session_id,
                        run_id=None,
                        owner_token=owner_token,
                    )
                return OneShotResult(
                    exit_code=1,
                    assistant_output=None,
                    error=error,
                    message=message,
                    session_id=session.session_id,
                    run_id=None,
                )
            failed = _mark_failed_terminal(
                sessions=sessions,
                runs=runs,
                events=events,
                checkpoints=checkpoints,
                trace_writer=TraceWriter(db.connection, sessions_root),
                session_id=session.session_id,
                run_id=run_id,
                agent_result=AgentRunResult(
                    status="cancelled",
                    assistant_output=None,
                    tool_results=[],
                    usage={},
                    error=error,
                    metadata={},
                ),
            )
            if owner_token is not None:
                _release_ownership_after_terminalization(
                    sessions=sessions,
                    events=events,
                    sessions_root=sessions_root,
                    session_id=session.session_id,
                    run_id=run_id,
                    owner_token=owner_token,
                )
            return failed
        finally:
            db.close()

    def start_repl(
        self,
        config_snapshot: dict[str, Any],
        *,
        approval_mode: str = "normal",
    ) -> ReplStartResult:
        if approval_mode not in APPROVAL_MODES:
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=ERROR_USAGE,
                    message="approval mode must be one of: normal, semi-auto, yolo",
                    error={
                        "error_class": "config_error",
                        "message": "approval mode must be one of: normal, semi-auto, yolo",
                        "source": "orchestrator",
                        "recoverable": False,
                    },
                ),
            )
        policy_result = self._freeze_policy(config_snapshot)
        if isinstance(policy_result, OneShotResult):
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=policy_result.exit_code,
                    message=policy_result.message,
                    error=policy_result.error,
                ),
            )
        config_snapshot = _ensure_phase3_frozen_runtime_defaults(policy_result)
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=map_error_to_exit_code(exc.normalized_error),
                    message=str(exc),
                    error={
                        "error_class": exc.error_class,
                        "reason": exc.reason,
                        "message": str(exc),
                        "source": exc.source,
                        "recoverable": exc.recoverable,
                    },
                ),
            )
        sessions_root = db.path.parent
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection, sessions_root)
        artifacts = ArtifactStore(db.connection, sessions_root)
        checkpoints = CheckpointStore(db.connection, artifact_store=artifacts)

        try:
            session = sessions.create(
                workspace_root=self.workspace_root,
                approval_mode=approval_mode,
                config_snapshot=config_snapshot,
            )
        except StoreError as exc:
            active = sessions.find_active_for_workspace(self.workspace_root)
            stale_result = self._try_fail_close_stale_owner(
                db=db,
                sessions=sessions,
                checkpoints=checkpoints,
                active=active,
            )
            if stale_result is not None and stale_result.exit_code == 0:
                session = sessions.create(
                    workspace_root=self.workspace_root,
                    approval_mode=approval_mode,
                    config_snapshot=config_snapshot,
                )
            elif stale_result is not None:
                db.close()
                return ReplStartResult(
                    runtime=None,
                    error=ReplStartError(
                        exit_code=stale_result.exit_code,
                        message=stale_result.message,
                        error=stale_result.error,
                    ),
                )
            else:
                message = _active_conflict_message(
                    active.session_id if active else "unknown"
                )
                if active is not None:
                    write_runtime_log(
                        sessions_root,
                        session_id=active.session_id,
                        run_id=active.active_run_id,
                        level="ERROR",
                        event="ownership_conflict",
                        message=message,
                        metadata={"workspace_root": str(self.workspace_root)},
                    )
                db.close()
                return ReplStartResult(
                    runtime=None,
                    error=ReplStartError(
                        exit_code=3,
                        message=message,
                        error={
                            "error_class": exc.error_class,
                            "message": message,
                            "source": exc.source,
                            "recoverable": exc.recoverable,
                        },
                    ),
                )

        run = runs.create_prompt_run(session.session_id)
        session = sessions.set_active_run(session.session_id, run.run_id)
        owner_facts = _current_owner_facts()
        session = sessions.record_owner(
            session_id=session.session_id,
            owner_pid=int(owner_facts["pid"]),
            owner_host_id=str(owner_facts["host_id"]),
            owner_token=str(owner_facts["owner_token"]),
        )
        _append_event(events, session.session_id, run.run_id, "session_started", {})
        _append_event(events, session.session_id, run.run_id, "run_started", {})
        startup_error = _snapshot_skills_for_startup(
            workspace_root=self.workspace_root,
            artifacts=artifacts,
            connection=db.connection,
            events=events,
            session_id=session.session_id,
            run_id=run.run_id,
        )
        if startup_error is not None:
            _mark_failed_terminal(
                sessions=sessions,
                runs=runs,
                events=events,
                checkpoints=checkpoints,
                trace_writer=TraceWriter(db.connection, sessions_root),
                session_id=session.session_id,
                run_id=run.run_id,
                agent_result=AgentRunResult(
                    status="failed",
                    assistant_output=None,
                    tool_results=[],
                    usage={},
                    error=startup_error,
                    metadata={"prompt_turn_counter": 0},
                ),
                startup_failure=True,
            )
            _release_ownership_after_terminalization(
                sessions=sessions,
                events=events,
                sessions_root=sessions_root,
                session_id=session.session_id,
                run_id=run.run_id,
                owner_token=str(owner_facts["owner_token"]),
            )
            db.close()
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=4,
                    message=startup_error["message"],
                    error=startup_error,
                ),
            )

        model_result = ModelFactory().create(config_snapshot)
        if model_result.error is not None:
            _mark_failed_terminal(
                sessions=sessions,
                runs=runs,
                events=events,
                checkpoints=checkpoints,
                trace_writer=TraceWriter(db.connection, sessions_root),
                session_id=session.session_id,
                run_id=run.run_id,
                agent_result=AgentRunResult(
                    status="failed",
                    assistant_output=None,
                    tool_results=[],
                    usage={},
                    error=model_result.error,
                    metadata={"prompt_turn_counter": 0},
                ),
                startup_failure=True,
            )
            _release_ownership_after_terminalization(
                sessions=sessions,
                events=events,
                sessions_root=sessions_root,
                session_id=session.session_id,
                run_id=run.run_id,
                owner_token=str(owner_facts["owner_token"]),
            )
            db.close()
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=4,
                    message=model_result.error["message"],
                    error=model_result.error,
                ),
            )

        broker = ToolBroker(event_writer=events, artifact_store=artifacts)
        adapter = LangChainAgentLoopAdapter(
            model=model_result.model,
            tool_broker=broker,
        )
        executor = PromptAgentExecutor(
            event_writer=events,
            checkpoint_store=checkpoints,
            artifact_store=artifacts,
            adapter=adapter,
            tool_definitions=visible_tool_definitions(config_snapshot),
            system_prompt=config_snapshot.get("system_prompt", PHASE_0_SYSTEM_PROMPT),
            skill_snapshot_store=SkillSnapshotStore(db.connection),
            todo_plan_store=TodoPlanStore(db.connection),
            conversation_store=ConversationStore(
                db.connection,
                artifact_store=artifacts,
            ),
            run_store=runs,
            compression_model=make_compression_model_callable(model_result.model),
        )
        return ReplStartResult(
            runtime=ReplRuntime(
                db=db,
                sessions=sessions,
                runs=runs,
                events=events,
                checkpoints=checkpoints,
                executor=executor,
                session_id=session.session_id,
                run_id=run.run_id,
                workspace_root=self.workspace_root,
                owner_token=str(owner_facts["owner_token"]),
            ),
            error=None,
        )

    def _freeze_policy(
        self, config_snapshot: dict[str, Any]
    ) -> dict[str, Any] | OneShotResult:
        policy = load_main_agent_policy(self.workspace_root)
        if policy.error is not None:
            return OneShotResult(
                exit_code=4,
                assistant_output=None,
                error={
                    "error_class": policy.error.error_class,
                    "message": policy.error.message,
                    "source": policy.error.source,
                    "recoverable": policy.error.recoverable,
                },
                message=policy.error.message,
                session_id=None,
                run_id=None,
            )
        frozen = dict(config_snapshot)
        frozen["policy"] = policy_facts_to_snapshot(policy.facts)
        return frozen


def _snapshot_skills_for_startup(
    *,
    workspace_root: Path,
    artifacts: ArtifactStore,
    connection,
    events: EventWriter,
    session_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    try:
        snapshots = SkillRegistry(
            workspace_root=workspace_root,
            artifact_store=artifacts,
        ).snapshot(session_id=session_id, run_id=run_id)
        store = SkillSnapshotStore(connection)
        store.save_many(snapshots)
        for snapshot in snapshots:
            _append_event(
                events,
                session_id,
                run_id,
                "skill_snapshot_created",
                {
                    "skill_name": snapshot.name,
                    "execution_mode": snapshot.execution_mode,
                    "source_scope": snapshot.source_scope,
                    "content_hash": snapshot.overall_content_hash,
                    "resource_count": len(snapshot.resources),
                },
            )
        store.available_skill_headers(session_id=session_id, run_id=run_id)
        return None
    except SkillRegistryError as exc:
        return {
            "error_class": exc.error_class,
            "message": str(exc),
            "source": exc.source,
            "recoverable": exc.recoverable,
        }


def format_tool_listing(
    tool_definitions: list[Any],
    *,
    approval_mode: str,
    config_snapshot: dict[str, Any],
) -> list[str]:
    policy = config_snapshot.get("policy")
    shell_allow: list[Any] = []
    shell_deny: list[Any] = []
    path_trust: list[str] = []
    path_deny: list[str] = []
    if isinstance(policy, dict):
        path_trust = _policy_raw_values(policy.get("user_path_trust"))
        path_deny = [
            *_policy_raw_values(policy.get("builtin_path_deny")),
            *_policy_raw_values(policy.get("user_path_deny")),
        ]
        shell = policy.get("user_shell")
        if isinstance(shell, dict):
            if isinstance(shell.get("allow"), list):
                shell_allow = shell["allow"]
            if isinstance(shell.get("deny"), list):
                shell_deny = shell["deny"]
    lines = ["Tools:"]
    for definition in tool_definitions:
        if definition.name == "view_image" and not _view_image_enabled(config_snapshot):
            continue
        approval = _tool_approval_behavior(
            name=definition.name,
            category=definition.category,
            risk_level=definition.risk_level,
            approval_mode=approval_mode,
        )
        lines.extend(
            [
                "",
                f"- {definition.name} [{_normalized_tool_approval(approval)}]",
                definition.description,
            ]
        )
    disabled_reason = _view_image_disabled_reason(config_snapshot)
    if disabled_reason is not None:
        lines.extend(["", f"view_image disabled: {disabled_reason}"])
    lines.extend(
        [
            "",
            "Path policy:",
            f"- trust = {_format_policy_values(path_trust)}",
            f"- deny  = {_format_policy_values(path_deny)}",
            "",
            "Shell policy:",
            f"- allow = {_format_shell_prefixes(shell_allow)}",
            f"- deny  = {_format_shell_prefixes(shell_deny)}",
        ]
    )
    return lines


def visible_tool_definitions(config_snapshot: dict[str, Any]) -> list[Any]:
    definitions = list(gated_user_facing_tool_definitions(config_snapshot))
    if _view_image_enabled(config_snapshot):
        definitions.append(view_image_tool_definition())
    return definitions


def _view_image_enabled(config_snapshot: dict[str, Any]) -> bool:
    multimodal = config_snapshot.get("multimodal")
    return isinstance(multimodal, dict) and multimodal.get("view_image_enabled") is True


def _view_image_disabled_reason(config_snapshot: dict[str, Any]) -> str | None:
    multimodal = config_snapshot.get("multimodal")
    if not isinstance(multimodal, dict):
        return None
    if multimodal.get("view_image_enabled") is True:
        return None
    reason = multimodal.get("view_image_disabled_reason")
    return reason if isinstance(reason, str) and reason else "missing_multimodal_config"


def _policy_raw_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("raw"), str):
            values.append(item["raw"])
    return values


def _format_policy_values(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _format_shell_prefixes(prefixes: list[Any]) -> str:
    rendered: list[str] = []
    for prefix in prefixes:
        if isinstance(prefix, list) and all(isinstance(item, str) for item in prefix):
            rendered.append(" ".join(prefix))
    return ", ".join(rendered) if rendered else "none"


def _tool_approval_behavior(
    *,
    name: str,
    category: str,
    risk_level: str,
    approval_mode: str,
) -> str:
    if name == "load_skill_resource":
        return "audit-only when target is valid"
    if name == "todo":
        return "audit-only"
    if risk_level == "runtime_control":
        return "ask" if approval_mode == "normal" else "audit-only"
    if approval_mode == "yolo":
        return "auto-allow"
    if approval_mode == "semi-auto":
        if risk_level == "read":
            return "auto-allow"
        return "auto-allow in trusted paths; ask outside trusted paths"
    if risk_level == "read":
        return "auto-allow in trusted paths; ask outside trusted paths"
    if category == "shell":
        return "ask"
    return "ask"


def _normalized_tool_approval(approval: str) -> str:
    if approval in {
        "auto-allow",
        "audit-only",
        "audit-only when target is valid",
    }:
        return "allow"
    if approval == "ask":
        return "ask-all"
    if approval == "auto-allow in trusted paths; ask outside trusted paths":
        return "ask-distrust"
    return approval


def _next_approval_mode(current: str) -> str:
    modes = ["normal", "semi-auto", "yolo"]
    try:
        index = modes.index(current)
    except ValueError:
        return "normal"
    return modes[(index + 1) % len(modes)]


def _close_executor_provider_resources(executor: PromptAgentExecutor) -> None:
    adapter = getattr(executor, "adapter", None)
    model = getattr(adapter, "model", None)
    if model is not None:
        close_provider_resource(model)


def _mark_failed_terminal(
    *,
    sessions: SessionStore,
    runs: RunStore,
    events: EventWriter,
    checkpoints: CheckpointStore | None = None,
    trace_writer: TraceWriter | None = None,
    session_id: str,
    run_id: str,
    agent_result: AgentRunResult,
    startup_failure: bool = False,
) -> OneShotResult:
    error = agent_result.error or {
        "error_class": "internal_error",
        "message": "Prompt execution failed.",
        "source": "orchestrator",
        "recoverable": False,
    }
    terminal_error = _normalize_terminal_failure_error(
        error,
        result_metadata=agent_result.metadata,
    )
    latest_checkpoint_id = None
    if startup_failure:
        run = runs.mark_startup_failure(run_id, error["message"])
        session = sessions.mark_startup_failure(session_id, error["message"])
    else:
        terminal_reason = "terminal_failure"
        if terminal_error.get("error_class") == "cancelled" and terminal_error.get(
            "reason"
        ) == "user_cancel_idle":
            terminal_reason = "user_cancel_idle"
            message_index = _next_durable_message_index(
                runs.connection,
                run_id=run_id,
            )
            ConversationStore(runs.connection).append_closed_group(
                session_id=session_id,
                run_id=run_id,
                messages=[
                    ConversationAppend(
                        turn_id=None,
                        message_group_id=f"{run_id}:user_cancel_idle:{message_index}",
                        model_call_id=None,
                        group_position=0,
                        group_row_count=1,
                        role="runtime",
                        kind="cancellation_fact",
                        content={
                            "error_class": "cancelled",
                            "reason": "user_cancel_idle",
                            "message": terminal_error.get(
                                "message", "REPL interrupted by Ctrl+C."
                            ),
                            "artifact_ids": terminal_error.get("artifact_ids", []),
                        },
                        metadata={
                            "error_class": "cancelled",
                            "reason": "user_cancel_idle",
                        },
                    )
                ],
            )
        if checkpoints is not None:
            try:
                checkpoint = checkpoints.terminalize_with_recovery_checkpoint(
                    checkpoint_id=f"chk_{uuid4().hex}",
                    session_id=session_id,
                    run_id=run_id,
                    terminal_status="failed",
                    terminal_reason=terminal_reason,
                    terminal_error=terminal_error,
                    error_summary=error["message"],
                    created_at=utc_now_iso(),
                    artifact_ids=[],
                )
                _append_event(
                    events,
                    session_id,
                    run_id,
                    "checkpoint_written",
                    {"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
                )
                latest_checkpoint_id = checkpoint.checkpoint_id
                run = runs.get(run_id)
                session = sessions.get(session_id)
            except StoreError:
                latest_checkpoint_id = None
        if latest_checkpoint_id is None:
            run = runs.mark_failed(
                run_id,
                error["message"],
                latest_checkpoint_id=None,
            )
            session = sessions.mark_failed(
                session_id,
                error["message"],
                latest_checkpoint_id=None,
            )
    _append_event(
        events,
        session.session_id,
        run.run_id,
        "run_failed",
        {**_error_payload(error), "error": terminal_error},
    )
    _append_event(
        events,
        session.session_id,
        run.run_id,
        "session_failed",
        {**_error_payload(error), "error": terminal_error},
    )
    if trace_writer is not None:
        trace_writer.refresh_if_stale(session.session_id)
    return OneShotResult(
        exit_code=1,
        assistant_output=None,
        error=error,
        message=error["message"],
        session_id=session.session_id,
        run_id=run.run_id,
    )


def _bootstrap_one_shot_error(exc: RuntimeBootstrapError) -> OneShotResult:
    return OneShotResult(
        exit_code=map_error_to_exit_code(exc.normalized_error),
        assistant_output=None,
        error={
            "error_class": exc.error_class,
            "reason": exc.reason,
            "message": str(exc),
            "source": exc.source,
            "recoverable": exc.recoverable,
        },
        message=str(exc),
        session_id=None,
        run_id=None,
    )


def _running_cancelled_result() -> AgentRunResult:
    return AgentRunResult(
        status="cancelled",
        assistant_output=None,
        tool_results=[],
        usage={},
        error={
            "schema_version": 1,
            "error_class": "cancelled",
            "reason": "user_cancel_running",
            "message": "Turn cancelled by user.",
            "scope": "turn",
            "recoverability": "turn_recoverable",
            "metadata": {},
            "artifact_ids": [],
        },
        metadata={"failure_scope": "turn"},
    )


def _durable_turn_runtime_fact_exists(
    connection,
    *,
    run_id: str,
    turn_id: str,
    kind: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM conversation_messages
        WHERE run_id = ?
          AND turn_id = ?
          AND role = 'runtime'
          AND kind = ?
        LIMIT 1
        """,
        (run_id, turn_id, kind),
    ).fetchone()
    return row is not None


def _idle_cancelled_result(*, prompt_turn_counter: int) -> AgentRunResult:
    return AgentRunResult(
        status="cancelled",
        assistant_output=None,
        tool_results=[],
        usage={},
        error={
            "schema_version": 1,
            "error_class": "cancelled",
            "reason": "user_cancel_idle",
            "message": "REPL interrupted by Ctrl+C.",
            "scope": "session",
            "recoverability": "terminal_recoverable",
            "metadata": {},
            "artifact_ids": [],
        },
        metadata={"prompt_turn_counter": prompt_turn_counter},
    )


def _next_durable_message_index(connection: sqlite3.Connection, *, run_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(message_index), 0) FROM conversation_messages WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row[0]) + 1


def _ensure_phase3_frozen_runtime_defaults(config_snapshot: dict[str, Any]) -> dict[str, Any]:
    frozen = dict(config_snapshot)
    execution = frozen.get("execution")
    if not isinstance(execution, dict):
        frozen["execution"] = dict(EXECUTION_DEFAULTS)
    else:
        frozen["execution"] = {**EXECUTION_DEFAULTS, **execution}
    multimodal = frozen.get("multimodal")
    if not isinstance(multimodal, dict):
        frozen["multimodal"] = {
            "provider": None,
            "model": None,
            **MULTIMODAL_LIMIT_DEFAULTS,
            "api_key_env": None,
            "api_key_present": False,
            "base_url_env": None,
            "base_url_present": False,
            "view_image_enabled": False,
            "view_image_disabled_reason": "missing_multimodal_config",
        }
    return frozen


def _startup_success_message(db: RuntimeDatabase, message: str) -> str:
    if not db.startup_messages:
        return message
    if not message:
        return "\n".join(db.startup_messages)
    return "\n".join([*db.startup_messages, message])


def _append_event(
    event_writer: EventWriter,
    session_id: str,
    run_id: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    event_writer.append(
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


def _resume_error(
    *,
    session_id: str | None,
    message: str,
    error_class: str,
    reason: str,
) -> ResumeResult:
    error = NormalizedError.create(
        error_class,
        reason,
        message=message,
        scope="session",
    )
    return ResumeResult(
        exit_code=map_error_to_exit_code(error),
        message=message,
        error=error.to_dict(),
        session_id=session_id,
    )


def _conversation_from_checkpoint_projection(
    *,
    conversation_store: ConversationStore,
    run_id: str,
    checkpoint_state: dict[str, Any],
) -> list[dict[str, Any]]:
    projection = checkpoint_state["conversation"]["projection_snapshot"]
    source_high_watermark = int(projection["source_high_watermark"])
    message_refs = projection["message_refs"]
    conversation_store.validate_projection_snapshot(
        run_id=run_id,
        source_high_watermark=source_high_watermark,
        message_refs=message_refs,
        checksum=projection["checksum"],
    )
    rows = {
        row.message_index: row
        for row in conversation_store.list_messages(run_id)
        if row.message_index <= source_high_watermark
    }
    restored: list[dict[str, Any]] = []
    for seq, index in enumerate(_indexes_from_message_refs(message_refs), start=1):
        row = rows[index]
        restored.append(
            {
                "seq": seq,
                "role": row.role,
                "kind": row.kind,
                "turn_id": row.turn_id,
                "model_call_id": row.model_call_id,
                "tool_call_id": row.tool_call_id,
                "content": row.content,
                "artifact_refs": [] if row.artifact_id is None else [row.artifact_id],
                "metadata": dict(row.metadata),
                "durable_message_index": row.message_index,
            }
        )
    return restored


def _indexes_from_message_refs(message_refs: list[dict[str, int]]) -> list[int]:
    indexes: list[int] = []
    for ref in message_refs:
        if "index" in ref:
            indexes.append(int(ref["index"]))
        else:
            indexes.extend(range(int(ref["start"]), int(ref["end"]) + 1))
    return indexes


@dataclass(frozen=True)
class _StaleProof:
    session_id: str = ""
    run_id: str = ""
    owner_pid: int = 0
    owner_host_id: str = ""
    owner_token: str = ""
    error_reason: str | None = None


class _HostIdentityProvider:
    def current_host_id(self) -> str | None:
        machine_id = _platform_machine_id()
        if machine_id is None:
            return None
        digest = hashlib.sha256(machine_id.encode("utf-8")).hexdigest()
        return f"host-v1:sha256({digest})"


class _ProcessLiveness:
    def pid_exists(self, pid: int) -> bool:
        if pid <= 0:
            raise OSError("invalid pid")
        if platform.system().lower() == "windows":
            return _windows_pid_exists(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def _windows_pid_exists(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        if error:
            return True
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _platform_machine_id() -> str | None:
    system = platform.system().lower()
    if system == "linux":
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                return value
        return None
    if system == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in result.stdout.splitlines():
            if "IOPlatformUUID" not in line:
                continue
            _key, _sep, value = line.partition("=")
            parsed = value.strip().strip('"')
            if parsed:
                return parsed
        return None
    if system == "windows":
        try:
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(  # type: ignore[attr-defined]
                winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "MachineGuid")  # type: ignore[attr-defined]
        except Exception:
            return None
        return str(value).strip() or None
    return None


def _capture_stale_proof(
    connection: sqlite3.Connection,
    *,
    workspace_root: Path,
    session_id: str,
    host_identity_provider: Any,
    process_liveness: Any,
) -> _StaleProof:
    row = connection.execute(
        """
        SELECT session_id, workspace_root, active_run_id, owner_pid,
               owner_host_id, owner_token
        FROM sessions
        WHERE session_id = ? AND status = 'running'
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return _StaleProof(error_reason="workspace_owner_active")
    if row[1] != str(Path(workspace_root).resolve()):
        return _StaleProof(error_reason="workspace_owner_active")
    run_id = row[2]
    owner_pid = row[3]
    owner_host_id = row[4]
    owner_token = row[5]
    if not isinstance(run_id, str) or not run_id:
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    if not isinstance(owner_host_id, str) or not owner_host_id:
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    if not isinstance(owner_token, str) or not owner_token:
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    try:
        owner_pid_int = int(owner_pid)
    except (TypeError, ValueError):
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    try:
        current_host_id = host_identity_provider.current_host_id()
    except Exception:
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    if not isinstance(current_host_id, str) or current_host_id != owner_host_id:
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    try:
        pid_exists = process_liveness.pid_exists(owner_pid_int)
    except Exception:
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    if pid_exists:
        return _StaleProof(error_reason="workspace_owner_not_proven_stale")
    return _StaleProof(
        session_id=str(row[0]),
        run_id=run_id,
        owner_pid=owner_pid_int,
        owner_host_id=owner_host_id,
        owner_token=owner_token,
    )


def _ownership_conflict_one_shot(
    active: Any,
    *,
    reason: str,
    message: str,
) -> OneShotResult:
    error = NormalizedError.create(
        "policy_error",
        reason,
        message=message,
        scope="startup",
    )
    return OneShotResult(
        exit_code=ERROR_ACTIVE_SESSION_CONFLICT,
        assistant_output=None,
        error=error.to_dict(),
        message=message,
        session_id=active.session_id,
        run_id=getattr(active, "active_run_id", None),
    )


def _stale_confirmation_unavailable_message(session_id: str) -> str:
    return (
        "An active debug-agent session already owns this workspace and appears stale, "
        "but confirmation is unavailable.\n"
        f"Session: {session_id}"
    )


def _stale_confirmation_request(proof: _StaleProof) -> dict[str, Any]:
    return {
        "session_id": proof.session_id,
        "run_id": proof.run_id,
        "evidence": {
            "host_match": True,
            "pid_absent": True,
            "owner_token_present": True,
        },
        "message": (
            "The active owner appears stale on this host. Confirm fail-close of "
            "the old session before continuing?"
        ),
    }


def _prepare_stale_terminal_checkpoint(
    checkpoints: CheckpointStore,
    proof: _StaleProof,
) -> Any | None:
    try:
        return checkpoints._new_terminal_recovery_checkpoint(
            checkpoint_id=f"chk_{uuid4().hex}",
            session_id=proof.session_id,
            run_id=proof.run_id,
            terminal_status="failed",
            terminal_reason="terminal_stale",
            terminal_error=None,
            created_at=utc_now_iso(),
            artifact_ids=[],
        )
    except StoreError:
        return None


def _current_owner_facts() -> dict[str, Any]:
    host_id = _HostIdentityProvider().current_host_id()
    if host_id is None:
        host_id = "host-v1:sha256(unavailable)"
    return {
        "pid": os.getpid(),
        "host_id": host_id,
        "owner_token": f"owner_{uuid4().hex}",
    }


def _release_ownership_after_terminalization(
    *,
    sessions: SessionStore,
    events: EventWriter,
    sessions_root: Path,
    session_id: str,
    run_id: str | None,
    owner_token: str,
) -> None:
    if sessions.release_ownership(session_id=session_id, owner_token=owner_token):
        return
    _record_ownership_release_failed_event(
        events,
        session_id=session_id,
        run_id=run_id,
    )
    write_runtime_log(
        sessions_root,
        session_id=session_id,
        run_id=run_id,
        level="ERROR",
        event="ownership_release_failed",
        message="Active ownership release failed after terminalization.",
        metadata={"reason": "runtime_error/ownership_release_failed"},
    )


def _record_ownership_release_failed_event(
    events: EventWriter,
    *,
    session_id: str,
    run_id: str | None,
) -> None:
    if run_id is None:
        return
    error = NormalizedError.create(
        "runtime_error",
        "ownership_release_failed",
        message="Active ownership release failed after terminalization.",
        scope="session",
        recoverability="non_recoverable",
    ).to_dict()
    _append_event(events, session_id, run_id, "run_failed", {"error": error})


def _owner_token_for_session(connection, session_id: str) -> str | None:
    row = connection.execute(
        "SELECT owner_token FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        return None
    return row[0]


def _revive_same_lineage(
    connection,
    *,
    session_id: str,
    run_id: str,
    owner_facts: dict[str, Any],
    checkpoint_state: dict[str, Any],
) -> None:
    try:
        with connection:
            TodoPlanStore(connection).restore_checkpoint_snapshot(
                session_id=session_id,
                run_id=run_id,
                snapshot=checkpoint_state["todo_plan"],
            )
            RunStore(connection).revive_for_explicit_resume(
                run_id=run_id,
                session_id=session_id,
            )
            SessionStore(connection).revive_for_explicit_resume(
                session_id=session_id,
                run_id=run_id,
                owner_pid=int(owner_facts["pid"]),
                owner_host_id=str(owner_facts["host_id"]),
                owner_token=str(owner_facts["owner_token"]),
            )
    except sqlite3.IntegrityError as exc:
        raise _resume_transition_error("Active workspace ownership is blocked.") from exc


def _resume_transition_error(message: str) -> StoreError:
    return StoreError(
        error_class="persistence_error",
        message=message,
        recoverable=False,
    )


def _rollback_failed_resume_handoff(connection, *, session_id: str) -> None:
    row = connection.execute(
        """
        SELECT s.active_run_id, s.latest_checkpoint_id, c.state_json
        FROM sessions s
        LEFT JOIN checkpoints c ON c.checkpoint_id = s.latest_checkpoint_id
        WHERE s.session_id = ? AND s.status = 'running'
        """,
        (session_id,),
    ).fetchone()
    if row is None or row[0] is None or row[1] is None or row[2] is None:
        return
    run_id = row[0]
    checkpoint_id = row[1]
    checkpoint_state = json.loads(row[2])
    terminal_status = checkpoint_state.get("terminal_status")
    if terminal_status not in {"completed", "failed"}:
        return
    now = utc_now_iso()
    with connection:
        connection.execute(
            """
            UPDATE runs
            SET status = ?, updated_at = ?
            WHERE run_id = ? AND session_id = ? AND status = 'running'
            """,
            (terminal_status, now, run_id, session_id),
        )
        connection.execute(
            """
            UPDATE sessions
            SET status = ?, active_run_id = NULL, owner_pid = NULL,
                owner_host_id = NULL, owner_token = NULL, updated_at = ?
            WHERE session_id = ? AND status = 'running' AND active_run_id = ?
            """,
            (terminal_status, now, session_id, run_id),
        )
        _delete_resume_events_for_checkpoint(
            connection,
            session_id=session_id,
            run_id=run_id,
            checkpoint_id=checkpoint_id,
        )


def _delete_resume_events_for_checkpoint(
    connection,
    *,
    session_id: str,
    run_id: str,
    checkpoint_id: str,
) -> None:
    rows = connection.execute(
        """
        SELECT rowid, kind, payload_json
        FROM run_events
        WHERE session_id = ? AND run_id = ?
        ORDER BY rowid DESC
        """,
        (session_id, run_id),
    ).fetchall()
    rowids: list[int] = []
    remaining = {"session_resumed", "run_resumed"}
    for rowid, kind, payload_json in rows:
        if kind not in remaining:
            continue
        payload = json.loads(payload_json)
        if payload.get("checkpoint_id") != checkpoint_id:
            continue
        rowids.append(int(rowid))
        remaining.remove(kind)
        if not remaining:
            break
    if rowids:
        connection.executemany(
            "DELETE FROM run_events WHERE rowid = ?",
            [(rowid,) for rowid in rowids],
        )


def _runtime_from_resumed_session(
    *,
    db: RuntimeDatabase,
    workspace_root: Path,
    session_id: str,
) -> ReplRuntime:
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    conversation_store = ConversationStore(db.connection, artifact_store=artifacts)
    checkpoints = CheckpointStore(
        db.connection,
        conversation_store=conversation_store,
        todo_plan_store=TodoPlanStore(db.connection),
        artifact_store=artifacts,
    )
    session = sessions.get(session_id)
    owner_token = _owner_token_for_session(db.connection, session_id)
    if session.status != "running" or session.active_run_id is None:
        raise StoreError(
            error_class="persistence_error",
            message="Resumed session is not running.",
            recoverable=False,
        )
    run = runs.get(session.active_run_id)
    if run.status != "running" or not run.latest_checkpoint_id:
        raise StoreError(
            error_class="persistence_error",
            message="Resumed run is not running.",
            recoverable=False,
        )
    checkpoint = checkpoints.get(run.latest_checkpoint_id)
    checkpoints.validate_terminal_recovery(checkpoint, validate_current_todo=True)
    conversation = _conversation_from_checkpoint_projection(
        conversation_store=conversation_store,
        run_id=run.run_id,
        checkpoint_state=checkpoint.state,
    )
    model_result = ModelFactory().create(session.config_snapshot)
    if model_result.error is not None:
        raise StoreError(
            error_class="config_error",
            message=model_result.error["message"],
            recoverable=False,
        )
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    adapter = LangChainAgentLoopAdapter(
        model=model_result.model,
        tool_broker=broker,
    )
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=adapter,
        tool_definitions=visible_tool_definitions(session.config_snapshot),
        system_prompt=session.config_snapshot.get("system_prompt", PHASE_0_SYSTEM_PROMPT),
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        todo_plan_store=TodoPlanStore(db.connection),
        conversation_store=conversation_store,
        run_store=runs,
        compression_model=make_compression_model_callable(model_result.model),
    )
    runtime = ReplRuntime(
        db=db,
        sessions=sessions,
        runs=runs,
        events=events,
        checkpoints=checkpoints,
        executor=executor,
        session_id=session.session_id,
        run_id=run.run_id,
        workspace_root=workspace_root,
        conversation=conversation,
        owner_token=owner_token,
    )
    runtime._sync_durable_message_indexes()
    return runtime


def _preflight_resumed_runtime_construction(
    *,
    db: RuntimeDatabase,
    session_id: str,
) -> None:
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    conversation_store = ConversationStore(db.connection, artifact_store=artifacts)
    checkpoints = CheckpointStore(
        db.connection,
        conversation_store=conversation_store,
        todo_plan_store=TodoPlanStore(db.connection),
        artifact_store=artifacts,
    )
    session = sessions.get(session_id)
    run = runs.latest_for_session(session.session_id)
    if (
        run is None
        or session.status not in {"completed", "failed"}
        or run.status not in {"completed", "failed"}
        or not session.latest_checkpoint_id
    ):
        return
    checkpoint = checkpoints.get(session.latest_checkpoint_id)
    checkpoints.validate_terminal_recovery(checkpoint, validate_current_todo=False)
    _conversation_from_checkpoint_projection(
        conversation_store=conversation_store,
        run_id=run.run_id,
        checkpoint_state=checkpoint.state,
    )
    model_result = ModelFactory().create(session.config_snapshot)
    if model_result.error is not None:
        raise StoreError(
            error_class="config_error",
            message=model_result.error["message"],
            recoverable=False,
        )


def _artifact_ids(result: AgentRunResult) -> list[str]:
    artifact_ids: list[str] = []
    for tool_result in result.tool_results:
        artifact_ids.extend(tool_result.get("artifacts", []))
    return artifact_ids


def _todo_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "pending": sum(1 for item in items if item.get("status") == "pending"),
        "in_progress": sum(
            1 for item in items if item.get("status") == "in_progress"
        ),
        "completed": sum(1 for item in items if item.get("status") == "completed"),
    }


def _next_conversation_seq(conversation: list[dict[str, Any]]) -> int:
    seq_values = [
        message.get("seq")
        for message in conversation
        if isinstance(message.get("seq"), int)
    ]
    if not seq_values:
        return 1
    return max(seq_values) + 1


def _max_repl_turn_counter(conversation: list[dict[str, Any]]) -> int:
    max_counter = 0
    for message in conversation:
        for key in ("turn_id", "model_call_id"):
            value = message.get(key)
            if not isinstance(value, str):
                continue
            counter = _extract_repl_turn_counter(value)
            if counter > max_counter:
                max_counter = counter
    return max_counter


def _extract_repl_turn_counter(value: str) -> int:
    for prefix in ("turn-", "repl_turn_"):
        if not value.startswith(prefix):
            continue
        suffix = value[len(prefix) :]
        digits = []
        for character in suffix:
            if not character.isdigit():
                break
            digits.append(character)
        if digits:
            return int("".join(digits))
    return 0


def _consumed_model_call_ids(conversation: list[dict[str, Any]]) -> list[str]:
    consumed: list[str] = []
    for message in conversation:
        model_call_id = message.get("model_call_id")
        if isinstance(model_call_id, str) and model_call_id not in consumed:
            consumed.append(model_call_id)
    return consumed


def _active_conflict_message(session_id: str) -> str:
    return (
        "An active debug-agent session already owns this workspace.\n"
        f"Session: {session_id}\n"
        "Use that session, wait for it to finish, or start in a separate git worktree."
    )


def _error_payload(error: dict[str, Any]) -> dict[str, Any]:
    return {
        "error_class": error.get("error_class", "runtime_error"),
        "reason": error.get("reason"),
        "message": error.get("message", "Prompt execution failed."),
        "source": error.get("source", "orchestrator"),
        "recoverable": error.get("recoverable", False),
    }


def _normalize_terminal_failure_error(
    error: dict[str, Any],
    *,
    result_metadata: dict[str, Any],
) -> dict[str, Any]:
    existing = error.get("error")
    if _is_normalized_error(existing):
        return dict(existing)
    if _is_normalized_error(error):
        return dict(error)
    error_class, reason, scope = _terminal_error_identity(error)
    metadata = _terminal_error_metadata(error, result_metadata, error_class)
    return NormalizedError.create(
        error_class,
        reason,
        message=str(error.get("message") or "Prompt execution failed."),
        scope=scope,
        metadata=metadata,
        artifact_ids=_artifact_ids_from_error(error),
    ).to_dict()


def _terminal_error_identity(error: dict[str, Any]) -> tuple[str, str, str]:
    error_class = str(error.get("error_class") or "runtime_error")
    reason = error.get("reason")
    if error_class == "compression_failed":
        return "model_error", "compression_failed", "turn"
    if error_class == "context_limit_exceeded":
        return "model_error", "context_limit_exceeded", "turn"
    if error_class == "internal_error":
        return "runtime_error", "internal_invariant_failed", "run"
    if error_class == "model_error":
        return "model_error", _valid_reason_or_default(
            "model_error", reason, "model_call_failed"
        ), "provider"
    if error_class == "config_error":
        return "config_error", _config_terminal_reason(error), "startup"
    if error_class == "policy_error":
        return "policy_error", _valid_reason_or_default(
            "policy_error", reason, "approval_denied"
        ), "tool"
    if error_class == "tool_error":
        return "tool_error", _valid_reason_or_default(
            "tool_error", reason, "tool_execution_failed"
        ), "tool"
    if error_class == "cancelled":
        return "cancelled", _valid_reason_or_default(
            "cancelled", reason, "user_cancel_running"
        ), "session"
    if error_class == "runtime_error":
        return "runtime_error", _valid_reason_or_default(
            "runtime_error", reason, "internal_invariant_failed"
        ), "run"
    if error_class == "user_error":
        return "user_error", _valid_reason_or_default(
            "user_error", reason, "invalid_command"
        ), "run"
    return "runtime_error", "internal_invariant_failed", "run"


def _config_terminal_reason(error: dict[str, Any]) -> str:
    reason = error.get("reason")
    if _is_valid_reason("config_error", reason):
        return str(reason)
    message = str(error.get("message") or "")
    source = str(error.get("source") or "")
    if source == "model_factory" and "Missing auth token" in message:
        return "provider_auth_missing"
    if source == "model_factory" and "Unsupported provider" in message:
        return "provider_config_invalid"
    if source == "model_factory":
        return "startup_model_unavailable"
    return "startup_schema_validation_failed"


def _valid_reason_or_default(
    error_class: str,
    reason: object,
    default: str,
) -> str:
    if _is_valid_reason(error_class, reason):
        return str(reason)
    return default


def _is_valid_reason(error_class: str, reason: object) -> bool:
    if not isinstance(reason, str):
        return False
    try:
        NormalizedError.create(
            error_class,
            reason,
            message="validation",
            scope="run",
        )
    except ValueError:
        return False
    return True


def _terminal_error_metadata(
    error: dict[str, Any],
    result_metadata: dict[str, Any],
    normalized_error_class: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    legacy_class = error.get("error_class")
    if isinstance(legacy_class, str) and legacy_class != normalized_error_class:
        metadata["legacy_error_class"] = legacy_class
    source = error.get("source")
    if isinstance(source, str):
        metadata["source"] = source
    if "recoverable" in error:
        metadata["legacy_recoverable"] = bool(error["recoverable"])
    failure_scope = result_metadata.get("failure_scope")
    if isinstance(failure_scope, str):
        metadata["failure_scope"] = failure_scope
    return metadata


def _artifact_ids_from_error(error: dict[str, Any]) -> list[str]:
    artifact_ids = error.get("artifact_ids")
    if isinstance(artifact_ids, list) and all(
        isinstance(artifact_id, str) for artifact_id in artifact_ids
    ):
        return list(artifact_ids)
    return []


def _projection_indexes(message_refs: list[dict[str, int]]) -> list[int]:
    indexes: list[int] = []
    for ref in message_refs:
        if "index" in ref:
            indexes.append(int(ref["index"]))
            continue
        if "start" in ref and "end" in ref:
            indexes.extend(range(int(ref["start"]), int(ref["end"]) + 1))
    return indexes


def _is_normalized_error(value: object) -> bool:
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

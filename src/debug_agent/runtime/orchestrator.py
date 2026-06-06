from __future__ import annotations

from collections.abc import Callable
import json
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
from debug_agent.observability.trace_writer import TraceWriter
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.errors import StoreError
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeBootstrapError, RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.config import PHASE_0_SYSTEM_PROMPT
from debug_agent.runtime.contracts import (
    APPROVAL_MODES,
    AgentRunResult,
    Checkpoint,
    RunEvent,
    utc_now_iso,
)
from debug_agent.runtime.errors import NormalizedError
from debug_agent.runtime.policy import load_main_agent_policy, policy_facts_to_snapshot
from debug_agent.runtime.prompt_executor import (
    PromptAgentExecutor,
    make_compression_model_callable,
)
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
        self.turn_counter = 0
        self.conversation: list[dict[str, Any]] = []
        self.latest_context_estimate: dict[str, Any] | None = None
        self.approval_provider = NonInteractiveApprovalProvider()
        self.closed = False
        self._lock = threading.RLock()

    def run_turn(
        self,
        user_input: str,
        agent_stream_callback: Callable[[AgentStreamEvent], None] | None = None,
    ) -> AgentRunResult:
        with self._lock:
            self.turn_counter += 1
            session = self.sessions.get(self.session_id)
            run = self.runs.get(self.run_id)
            def runtime_stream_callback(event: AgentStreamEvent) -> None:
                if event.kind == "stream_context_estimate_updated":
                    estimate = event.payload.get("context_estimate")
                    if isinstance(estimate, dict):
                        self.latest_context_estimate = dict(estimate)
                if agent_stream_callback is not None:
                    agent_stream_callback(event)

            result = self.executor.run_turn(
                session=session,
                run=run,
                user_input=user_input,
                workspace_root=str(self.workspace_root),
                conversation=self.conversation,
                prompt_turn_counter=self.turn_counter,
                approval_provider=self.approval_provider,
                agent_stream_callback=runtime_stream_callback
                if agent_stream_callback is not None
                else None,
            )
            self._append_turn_conversation(user_input, result)
            return result

    def _append_turn_conversation(
        self,
        user_input: str,
        result: AgentRunResult,
    ) -> None:
        estimate = result.metadata.get("context_estimate")
        if isinstance(estimate, dict):
            self.latest_context_estimate = estimate
        writeback = result.metadata.get("conversation_writeback")
        if isinstance(writeback, list):
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
            return
        error = result.error if isinstance(result.error, dict) else {}
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
            checkpoint = self.checkpoints.save(
                Checkpoint(
                    checkpoint_id=f"chk_{uuid4().hex}",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    kind="terminal",
                    state={
                        "session_status": "completed",
                        "run_status": "completed",
                        "prompt_turn_counter": self.turn_counter,
                        "latest_model_response_metadata": {},
                        "latest_artifact_ids": [],
                        "latest_error_summary": None,
                    },
                    summary="REPL exited through /exit.",
                    created_at=utc_now_iso(),
                )
            )
            _append_event(
                self.events,
                session.session_id,
                run.run_id,
                "checkpoint_written",
                {"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
            )
            run = self.runs.mark_completed(
                run.run_id, latest_checkpoint_id=checkpoint.checkpoint_id
            )
            session = self.sessions.mark_completed(
                session.session_id, latest_checkpoint_id=checkpoint.checkpoint_id
            )
            _append_event(self.events, session.session_id, run.run_id, "run_completed", {})
            _append_event(
                self.events, session.session_id, run.run_id, "session_completed", {}
            )
            TraceWriter(self.db.connection, self.db.path.parent).refresh_if_stale(
                session.session_id
            )
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
            self.close()

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                self.db.close()
                self.closed = True


@dataclass(frozen=True)
class ReplStartResult:
    runtime: ReplRuntime | None
    error: ReplStartError | None


class RuntimeOrchestrator:
    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self.workspace_root = resolve_workspace_root(workspace_root)

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
        config_snapshot = policy_result
        gate_error = _phase3_prompt_execution_gate_error(config_snapshot)
        if gate_error is not None:
            return OneShotResult(
                exit_code=ERROR_STARTUP_CONFIG,
                assistant_output=None,
                error=gate_error,
                message=gate_error["message"],
                session_id=None,
                run_id=None,
            )
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return _bootstrap_one_shot_error(exc)
        sessions_root = db.path.parent
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection, sessions_root)
        checkpoints = CheckpointStore(db.connection)
        artifacts = ArtifactStore(db.connection, sessions_root)
        try:
            try:
                session = sessions.create(
                    workspace_root=self.workspace_root,
                    approval_mode=approval_mode,
                    config_snapshot=config_snapshot,
                )
            except StoreError as exc:
                active = sessions.find_active_for_workspace(self.workspace_root)
                message = _active_conflict_message(active.session_id if active else "unknown")
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
                terminal_checkpoint = checkpoints.save(
                    Checkpoint(
                        checkpoint_id=f"chk_{uuid4().hex}",
                        session_id=session.session_id,
                        run_id=run.run_id,
                        kind="terminal",
                        state={
                            "session_status": "completed",
                            "run_status": "completed",
                            "prompt_turn_counter": 1,
                            "latest_model_response_metadata": _serializable_metadata(
                                agent_result.metadata
                            ),
                            "latest_artifact_ids": _artifact_ids(agent_result),
                            "latest_error_summary": None,
                        },
                        summary="One-shot completed successfully.",
                        created_at=utc_now_iso(),
                    )
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
                run = runs.mark_completed(
                    run.run_id,
                    latest_checkpoint_id=terminal_checkpoint.checkpoint_id,
                )
                session = sessions.mark_completed(
                    session.session_id,
                    latest_checkpoint_id=terminal_checkpoint.checkpoint_id,
                )
                _append_event(events, session.session_id, run.run_id, "run_completed", {})
                _append_event(events, session.session_id, run.run_id, "session_completed", {})
                TraceWriter(db.connection, sessions_root).refresh_if_stale(
                    session.session_id
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
            return _mark_failed_terminal(
                sessions=sessions,
                runs=runs,
                events=events,
                checkpoints=checkpoints,
                trace_writer=TraceWriter(db.connection, sessions_root),
                session_id=session.session_id,
                run_id=run.run_id,
                agent_result=agent_result,
            )
        finally:
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
        try:
            sessions = SessionStore(db.connection)
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
            message = (
                "Resume is gated until Phase 3 terminal recovery checkpoints "
                "are implemented."
            )
            return ResumeResult(
                exit_code=ERROR_EXECUTION_FAILED,
                message=message,
                error={
                    "error_class": "runtime_error",
                    "reason": "resume_checkpoint_required",
                    "message": message,
                },
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
        checkpoints = CheckpointStore(db.connection)
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
            if run_id is None:
                session = sessions.mark_failed(session.session_id, message)
                return OneShotResult(
                    exit_code=1,
                    assistant_output=None,
                    error=error,
                    message=message,
                    session_id=session.session_id,
                    run_id=None,
                )
            return _mark_failed_terminal(
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
        config_snapshot = policy_result
        gate_error = _phase3_prompt_execution_gate_error(config_snapshot)
        if gate_error is not None:
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=ERROR_STARTUP_CONFIG,
                    message=gate_error["message"],
                    error=gate_error,
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
        checkpoints = CheckpointStore(db.connection)
        artifacts = ArtifactStore(db.connection, sessions_root)

        try:
            session = sessions.create(
                workspace_root=self.workspace_root,
                approval_mode=approval_mode,
                config_snapshot=config_snapshot,
            )
        except StoreError as exc:
            active = sessions.find_active_for_workspace(self.workspace_root)
            message = _active_conflict_message(active.session_id if active else "unknown")
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
    definitions = list(gated_user_facing_tool_definitions())
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
    latest_run = runs.get(run_id)
    latest_session = sessions.get(session_id)
    latest_checkpoint_id = latest_run.latest_checkpoint_id
    if checkpoints is not None:
        latest_checkpoint = checkpoints.latest_for_run(run_id)
        if latest_checkpoint is None or latest_checkpoint.kind != "error":
            checkpoint = checkpoints.save(
                Checkpoint(
                    checkpoint_id=f"chk_{uuid4().hex}",
                    session_id=session_id,
                    run_id=run_id,
                    kind="error",
                    state={
                        "session_status": latest_session.status,
                        "run_status": latest_run.status,
                        "prompt_turn_counter": agent_result.metadata.get(
                            "prompt_turn_counter", 0
                        ),
                        "latest_model_response_metadata": _serializable_metadata(
                            agent_result.metadata
                        ),
                        "latest_artifact_ids": [],
                        "latest_error_summary": error["message"],
                    },
                    summary=error["message"],
                    created_at=utc_now_iso(),
                )
            )
            _append_event(
                events,
                session_id,
                run_id,
                "checkpoint_written",
                {"checkpoint_id": checkpoint.checkpoint_id, "kind": checkpoint.kind},
            )
            latest_checkpoint_id = checkpoint.checkpoint_id
    run = runs.mark_failed(
        run_id,
        error["message"],
        latest_checkpoint_id=latest_checkpoint_id,
    )
    session = sessions.mark_failed(
        session_id,
        error["message"],
        latest_checkpoint_id=latest_checkpoint_id,
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


def _phase3_prompt_execution_gate_error(
    config_snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    development = config_snapshot.get("development")
    allowed = (
        isinstance(development, dict)
        and development.get("allow_incomplete_phase3_prompt_execution") is True
    )
    if allowed:
        return None
    message = (
        "Phase 3 prompt execution is gated until durable conversation and terminal "
        "recovery checkpoints are implemented."
    )
    return {
        "error_class": "config_error",
        "reason": "startup_schema_validation_failed",
        "message": message,
        "scope": "startup",
        "recoverability": "recoverable",
        "source": "orchestrator",
        "recoverable": True,
    }


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

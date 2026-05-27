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
from debug_agent.runtime.config import PHASE_0_SYSTEM_PROMPT
from debug_agent.runtime.contracts import (
    APPROVAL_MODES,
    AgentRunResult,
    Checkpoint,
    RunEvent,
    utc_now_iso,
)
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
            if result.status == "completed":
                estimate = result.metadata.get("context_estimate")
                if isinstance(estimate, dict):
                    self.latest_context_estimate = estimate
                writeback = result.metadata.get("conversation_writeback")
                if isinstance(writeback, list):
                    self.conversation = [dict(message) for message in writeback]
                next_seq = _next_conversation_seq(self.conversation)
                turn_id = f"turn-{self.turn_counter}"
                model_call_id = f"repl_turn_{self.turn_counter}_assistant"
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
                consumed_ids = _consumed_model_call_ids(self.conversation)
                self.conversation.append(
                    {
                        "seq": next_seq,
                        "role": "assistant",
                        "kind": "assistant_output",
                        "turn_id": turn_id,
                        "model_call_id": model_call_id,
                        "tool_call_id": None,
                        "content": result.assistant_output or "",
                        "artifact_refs": [],
                        "metadata": {
                            "consumed_model_call_ids": consumed_ids,
                        },
                    }
                )
            elif result.metadata.get("approval_denied_abort") is True:
                self._append_denied_turn_observation(user_input, result)
            return result

    def _append_denied_turn_observation(
        self,
        user_input: str,
        result: AgentRunResult,
    ) -> None:
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
        denied_tool_calls = [
            call
            for call in result.metadata.get("denied_tool_calls", [])
            if isinstance(call, dict)
        ]
        provider_tool_calls = [
            call for call in denied_tool_calls if _non_empty_str(call.get("id"))
        ]
        if provider_tool_calls:
            self.conversation.append(
                {
                    "seq": next_seq + 1,
                    "role": "assistant",
                    "kind": "tool_call",
                    "turn_id": turn_id,
                    "model_call_id": f"repl_turn_{self.turn_counter}_denied",
                    "tool_call_id": None,
                    "content": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": str(call["id"]),
                                "name": str(call.get("name") or ""),
                                "args": call.get("args", {}),
                            }
                            for call in provider_tool_calls
                        ],
                    },
                    "artifact_refs": [],
                    "metadata": {
                        "terminal_observation": True,
                    },
                }
            )
        next_seq += 1
        if provider_tool_calls:
            next_seq += 1
        for index, tool_result in enumerate(result.tool_results):
            metadata = dict(tool_result.get("metadata", {}))
            metadata["terminal_observation"] = True
            tool_call_id = _tool_call_id_for_result(denied_tool_calls, index)
            if tool_call_id:
                self.conversation.append(
                    {
                        "seq": next_seq,
                        "role": "tool",
                        "kind": "tool_result",
                        "turn_id": turn_id,
                        "model_call_id": None,
                        "tool_call_id": tool_call_id,
                        "content": {
                            "message_type": "tool_result",
                            "content": _denied_tool_observation_content(tool_result),
                            "tool_call_id": tool_call_id,
                        },
                        "artifact_refs": list(tool_result.get("artifacts", [])),
                        "metadata": metadata,
                    }
                )
            else:
                self.conversation.append(
                    {
                        "seq": next_seq,
                        "role": "assistant",
                        "kind": "approval_denied_observation",
                        "turn_id": turn_id,
                        "model_call_id": None,
                        "tool_call_id": None,
                        "content": _plain_denied_tool_observation_content(
                            tool_result,
                            denied_tool_calls[index]
                            if index < len(denied_tool_calls)
                            else None,
                        ),
                        "artifact_refs": list(tool_result.get("artifacts", [])),
                        "metadata": metadata,
                    }
                )
            next_seq += 1

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
                gated_user_facing_tool_definitions(),
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
                exit_code=2,
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
                    exit_code=3,
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
                    exit_code=4,
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
                tool_definitions=gated_user_facing_tool_definitions(),
                system_prompt=config_snapshot.get("system_prompt", PHASE_0_SYSTEM_PROMPT),
                skill_snapshot_store=SkillSnapshotStore(db.connection),
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
                    message=agent_result.assistant_output or "",
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
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return StatusResult(exit_code=4, fields={}, message=str(exc))
        try:
            sessions = SessionStore(db.connection)
            runs = RunStore(db.connection)
            try:
                session = sessions.get(session_id)
            except StoreError as exc:
                return StatusResult(exit_code=1, fields={}, message=exc.message)
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
            return StatusResult(exit_code=0, fields=fields, message="")
        finally:
            db.close()

    def trace(self, session_id: str) -> TraceResult:
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return TraceResult(
                exit_code=4,
                trace_path=Path(),
                summary={},
                message=str(exc),
            )
        sessions_root = db.path.parent
        try:
            try:
                result = TraceWriter(db.connection, sessions_root).refresh_if_stale(
                    session_id
                )
            except StoreError as exc:
                return TraceResult(
                    exit_code=1,
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
                    exit_code=2,
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
        try:
            db = RuntimeDatabase.bootstrap(self.workspace_root)
        except RuntimeBootstrapError as exc:
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=4,
                    message=str(exc),
                    error={
                        "error_class": exc.error_class,
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
            tool_definitions=gated_user_facing_tool_definitions(),
            system_prompt=config_snapshot.get("system_prompt", PHASE_0_SYSTEM_PROMPT),
            skill_snapshot_store=SkillSnapshotStore(db.connection),
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
                    "reference_count": len(snapshot.references),
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
    if name == "load_skill_ref_file":
        return "audit-only when target is valid"
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
        {**_error_payload(error), "error": error},
    )
    _append_event(
        events,
        session.session_id,
        run.run_id,
        "session_failed",
        {**_error_payload(error), "error": error},
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
        exit_code=4,
        assistant_output=None,
        error={
            "error_class": exc.error_class,
            "message": str(exc),
            "source": exc.source,
            "recoverable": exc.recoverable,
        },
        message=str(exc),
        session_id=None,
        run_id=None,
    )


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


def _tool_call_id_for_result(tool_calls: list[dict[str, Any]], index: int) -> str:
    if 0 <= index < len(tool_calls):
        tool_call_id = tool_calls[index].get("id")
        if _non_empty_str(tool_call_id):
            return tool_call_id
    return ""


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _denied_tool_observation_content(tool_result: dict[str, Any]) -> str:
    error = tool_result.get("error")
    return json.dumps(
        {
            "status": "denied",
            "error": error
            if isinstance(error, dict)
            else {
                "error_class": "policy_denied",
                "message": "Approval denied.",
            },
        },
        sort_keys=True,
    )


def _plain_denied_tool_observation_content(
    tool_result: dict[str, Any],
    tool_call: dict[str, Any] | None,
) -> str:
    tool_name = ""
    if isinstance(tool_call, dict):
        name = tool_call.get("name")
        if isinstance(name, str):
            tool_name = name
    error = tool_result.get("error")
    message = ""
    if isinstance(error, dict):
        raw_message = error.get("message")
        if isinstance(raw_message, str):
            message = raw_message
    if not message:
        message = "Approval denied."
    if tool_name:
        return f"Tool call denied by user: {tool_name}. {message}"
    return f"Tool call denied by user. {message}"


def _active_conflict_message(session_id: str) -> str:
    return (
        "An active debug-agent session already owns this workspace.\n"
        f"Session: {session_id}\n"
        "Use that session, wait for it to finish, or start in a separate git worktree."
    )


def _error_payload(error: dict[str, Any]) -> dict[str, Any]:
    return {
        "error_class": error.get("error_class", "internal_error"),
        "message": error.get("message", "Prompt execution failed."),
        "source": error.get("source", "orchestrator"),
        "recoverable": error.get("recoverable", False),
    }


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

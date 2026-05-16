from __future__ import annotations

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
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.config import PHASE_0_SYSTEM_PROMPT
from debug_agent.runtime.contracts import AgentRunResult, Checkpoint, RunEvent, utc_now_iso
from debug_agent.runtime.prompt_executor import PromptAgentExecutor
from debug_agent.runtime.workspace import resolve_workspace_root
from debug_agent.tools.broker import ToolBroker
from debug_agent.tools.native_readonly import tool_definitions


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
        self.closed = False
        self._lock = threading.RLock()

    def run_turn(self, user_input: str) -> AgentRunResult:
        with self._lock:
            self.turn_counter += 1
            session = self.sessions.get(self.session_id)
            run = self.runs.get(self.run_id)
            result = self.executor.run_turn(
                session=session,
                run=run,
                user_input=user_input,
                workspace_root=str(self.workspace_root),
                conversation=self.conversation,
                prompt_turn_counter=self.turn_counter,
            )
            if result.status == "completed":
                self.conversation.append({"role": "user", "content": user_input})
                self.conversation.append(
                    {"role": "assistant", "content": result.assistant_output or ""}
                )
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

    def run_one_shot(self, prompt: str, config_snapshot: dict[str, Any]) -> OneShotResult:
        db = RuntimeDatabase.bootstrap(self.workspace_root)
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
                    approval_mode="yolo",
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
                tool_definitions=tool_definitions(),
                system_prompt=config_snapshot.get("system_prompt", PHASE_0_SYSTEM_PROMPT),
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
                            "latest_model_response_metadata": agent_result.metadata,
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
        db = RuntimeDatabase.bootstrap(self.workspace_root)
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
        db = RuntimeDatabase.bootstrap(self.workspace_root)
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
        db = RuntimeDatabase.bootstrap(self.workspace_root)
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

    def start_repl(self, config_snapshot: dict[str, Any]) -> ReplStartResult:
        db = RuntimeDatabase.bootstrap(self.workspace_root)
        sessions_root = db.path.parent
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection, sessions_root)
        checkpoints = CheckpointStore(db.connection)
        artifacts = ArtifactStore(db.connection, sessions_root)

        try:
            session = sessions.create(
                workspace_root=self.workspace_root,
                approval_mode="normal",
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
            tool_definitions=tool_definitions(),
            system_prompt=config_snapshot.get("system_prompt", PHASE_0_SYSTEM_PROMPT),
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
                        "latest_model_response_metadata": agent_result.metadata,
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

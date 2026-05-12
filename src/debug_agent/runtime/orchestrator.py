from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.adapters.model_factory import ModelFactory
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

    def run_turn(self, user_input: str) -> AgentRunResult:
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
        session = self.sessions.get(self.session_id)
        latest_run = self.runs.latest_for_session(self.session_id)
        return [
            f"session_id: {session.session_id}",
            f"workspace_root: {session.workspace_root}",
            f"status: {session.status}",
            f"approval_mode: {session.approval_mode}",
            f"active_run_id: {session.active_run_id or ''}",
            f"latest_run_id: {latest_run.run_id if latest_run else ''}",
            f"latest_checkpoint_id: {session.latest_checkpoint_id or ''}",
            f"created_at: {session.created_at}",
            f"updated_at: {session.updated_at}",
            f"error_summary: {session.error_summary or ''}",
        ]

    def complete(self) -> None:
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
        self.close()

    def fail(self, result: AgentRunResult) -> None:
        if self.closed:
            return
        RuntimeOrchestrator._mark_failed(
            sessions=self.sessions,
            runs=self.runs,
            events=self.events,
            session_id=self.session_id,
            run_id=self.run_id,
            agent_result=result,
        )
        self.close()

    def close(self) -> None:
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
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection)
        checkpoints = CheckpointStore(db.connection)
        artifacts = ArtifactStore(db.connection)
        try:
            model_result = ModelFactory().create(config_snapshot)
            if model_result.error is not None:
                return OneShotResult(
                    exit_code=4,
                    assistant_output=None,
                    error=model_result.error,
                    message=model_result.error["message"],
                    session_id=None,
                    run_id=None,
                )
            try:
                session = sessions.create(
                    workspace_root=self.workspace_root,
                    approval_mode="yolo",
                    config_snapshot=config_snapshot,
                )
            except StoreError as exc:
                active = sessions.find_active_for_workspace(self.workspace_root)
                message = _active_conflict_message(active.session_id if active else "unknown")
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

            broker = ToolBroker(event_writer=events, artifact_store=artifacts)
            adapter = LangChainAgentLoopAdapter(
                model=model_result.model,
                tool_broker=broker,
            )
            executor = PromptAgentExecutor(
                event_writer=events,
                checkpoint_store=checkpoints,
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
            latest_run = runs.get(run.run_id)
            latest_session = sessions.get(session.session_id)
            if agent_result.status == "completed":
                run = runs.mark_completed(
                    run.run_id, latest_checkpoint_id=latest_run.latest_checkpoint_id
                )
                session = sessions.mark_completed(
                    session.session_id,
                    latest_checkpoint_id=latest_session.latest_checkpoint_id,
                )
                _append_event(events, session.session_id, run.run_id, "run_completed", {})
                _append_event(events, session.session_id, run.run_id, "session_completed", {})
                return OneShotResult(
                    exit_code=0,
                    assistant_output=agent_result.assistant_output,
                    error=None,
                    message=agent_result.assistant_output or "",
                    session_id=session.session_id,
                    run_id=run.run_id,
                )
            return self._mark_failed(
                sessions=sessions,
                runs=runs,
                events=events,
                session_id=session.session_id,
                run_id=run.run_id,
                agent_result=agent_result,
            )
        finally:
            db.close()

    def start_repl(self, config_snapshot: dict[str, Any]) -> ReplStartResult:
        db = RuntimeDatabase.bootstrap(self.workspace_root)
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        events = EventWriter(db.connection)
        checkpoints = CheckpointStore(db.connection)
        artifacts = ArtifactStore(db.connection)

        model_result = ModelFactory().create(config_snapshot)
        if model_result.error is not None:
            db.close()
            return ReplStartResult(
                runtime=None,
                error=ReplStartError(
                    exit_code=4,
                    message=model_result.error["message"],
                    error=model_result.error,
                ),
            )

        try:
            session = sessions.create(
                workspace_root=self.workspace_root,
                approval_mode="normal",
                config_snapshot=config_snapshot,
            )
        except StoreError as exc:
            active = sessions.find_active_for_workspace(self.workspace_root)
            db.close()
            message = _active_conflict_message(active.session_id if active else "unknown")
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

        broker = ToolBroker(event_writer=events, artifact_store=artifacts)
        adapter = LangChainAgentLoopAdapter(
            model=model_result.model,
            tool_broker=broker,
        )
        executor = PromptAgentExecutor(
            event_writer=events,
            checkpoint_store=checkpoints,
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

    @staticmethod
    def _mark_failed(
        *,
        sessions: SessionStore,
        runs: RunStore,
        events: EventWriter,
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
        run = runs.mark_failed(
            run_id,
            error["message"],
            latest_checkpoint_id=latest_run.latest_checkpoint_id,
        )
        session = sessions.mark_failed(
            session_id,
            error["message"],
            latest_checkpoint_id=latest_session.latest_checkpoint_id,
        )
        _append_event(
            events,
            session.session_id,
            run.run_id,
            "run_failed",
            {"error": error},
        )
        _append_event(
            events,
            session.session_id,
            run.run_id,
            "session_failed",
            {"error": error},
        )
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
            event_id=f"evt_{utc_now_iso().replace(':', '').replace('-', '')}_{kind}",
            timestamp=utc_now_iso(),
            session_id=session_id,
            run_id=run_id,
            step_id=None,
            kind=kind,
            payload=payload,
        )
    )


def _active_conflict_message(session_id: str) -> str:
    return (
        "An active debug-agent session already owns this workspace.\n"
        f"Session: {session_id}\n"
        "Use that session, wait for it to finish, or start in a separate git worktree."
    )

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from debug_agent.runtime.contracts import AgentRunResult, RunEvent, utc_now_iso
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

    def _mark_failed(
        self,
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

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
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
        request = AgentRunRequest(
            session_id=session.session_id,
            run_id=run.run_id,
            user_input=user_input,
            system_prompt=self.system_prompt,
            conversation=conversation or [],
            tools=[definition.to_dict() for definition in self.tool_definitions],
            model_config=session.config_snapshot,
            timeout_seconds=session.config_snapshot.get("timeout_seconds"),
        )
        context = RunContext(
            workspace_root=workspace_root,
            artifact_root=session.artifact_root,
            approval_mode=session.approval_mode,
            cancellation_token=None,
            metadata={},
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
                    "latest_model_response_metadata": result.metadata,
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
                    "latest_model_response_metadata": result.metadata,
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

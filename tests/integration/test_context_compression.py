from __future__ import annotations

import json

from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.adapters.model_factory import FakeChatModel
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.prompt_executor import PromptAgentExecutor
from debug_agent.tools.broker import ToolBroker
from debug_agent.tools.native import tool_definitions


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "model": "fake-model", "timeout_seconds": 30},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    return {
        "workspace": workspace,
        "db": db,
        "runs": runs,
        "events": events,
        "checkpoints": checkpoints,
        "artifacts": artifacts,
        "session": session,
        "run": run,
        "skill_store": SkillSnapshotStore(db.connection),
    }


def _summary_output(completed: str) -> str:
    return json.dumps(
        {
            "task_goal": "continue debugging",
            "completed_work": [completed],
            "inspected_or_modified_files": [],
            "remaining_work": [],
            "next_plan": [],
            "key_decisions": [],
            "constraints": [],
        }
    )


def test_automatic_compression_runs_before_initial_model_call(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult("completed", "answer", [], {}, None, {})

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    session = _with_context(runtime["session"], window_tokens=420, ratio=0.1)
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=runtime["events"],
        checkpoint_store=runtime["checkpoints"],
        artifact_store=runtime["artifacts"],
        adapter=adapter,
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=runtime["skill_store"],
        run_store=runtime["runs"],
        compression_model=lambda _frame: _summary_output("compressed initial history"),
    )

    result = executor.run_turn(
        session=session,
        run=runtime["run"],
        user_input="continue",
        workspace_root=str(runtime["workspace"]),
        conversation=_compressible_conversation(),
    )

    sent_text = "\n".join(
        str(segment.content)
        for segment in adapter.request.model_context_frame.ordered_message_segments()
    )
    assert result.status == "completed"
    assert "compressed initial history" in sent_text
    assert "old output old output" not in sent_text
    assert runtime["runs"].get(runtime["run"].run_id).context_snapshot_id is not None
    runtime["db"].close()


def test_automatic_compression_runs_before_tool_loop_followup(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    (runtime["workspace"] / "notes.txt").write_text(
        "fresh tool result " * 220,
        encoding="utf-8",
    )

    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def invoke(self, messages):
            self.calls += 1
            self.messages_by_call.append(messages)
            if self.calls == 1:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_file_0",
                                "name": "read_file",
                                "args": {"path": "notes.txt"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type("Response", (), {"content": "done", "tool_calls": [], "usage": {}})()

    model = ToolLoopModel()
    session = _with_context(runtime["session"], window_tokens=1000, ratio=0.7)
    executor = PromptAgentExecutor(
        event_writer=runtime["events"],
        checkpoint_store=runtime["checkpoints"],
        artifact_store=runtime["artifacts"],
        adapter=LangChainAgentLoopAdapter(
            model=model,
            tool_broker=ToolBroker(
                event_writer=runtime["events"],
                artifact_store=runtime["artifacts"],
            ),
        ),
        tool_definitions=tool_definitions(),
        system_prompt="system",
        skill_snapshot_store=runtime["skill_store"],
        run_store=runtime["runs"],
        compression_model=lambda _frame: _summary_output("compressed follow-up history"),
    )

    result = executor.run_turn(
        session=session,
        run=runtime["run"],
        user_input="read notes",
        workspace_root=str(runtime["workspace"]),
        conversation=_compressible_conversation(old_tokens=40, old_repeats=12),
    )

    second_call_text = "\n".join(message["content"] for message in model.messages_by_call[1])
    assert result.status == "completed"
    assert "compressed follow-up history" in second_call_text
    assert "old output old output" not in second_call_text
    runtime["db"].close()


def test_automatic_compression_failure_preserves_conversation(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    session = _with_context(runtime["session"], window_tokens=420, ratio=0.1)
    conversation = _compressible_conversation()
    executor = PromptAgentExecutor(
        event_writer=runtime["events"],
        checkpoint_store=runtime["checkpoints"],
        artifact_store=runtime["artifacts"],
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=runtime["skill_store"],
        run_store=runtime["runs"],
        compression_model=lambda _frame: "",
    )

    result = executor.run_turn(
        session=session,
        run=runtime["run"],
        user_input="continue",
        workspace_root=str(runtime["workspace"]),
        conversation=conversation,
    )

    assert result.status == "failed"
    assert result.error["error_class"] == "compression_failed"
    assert [message["content"] for message in result.metadata["conversation_writeback"]] == [
        message["content"] for message in conversation
    ]
    assert runtime["db"].connection.execute(
        "SELECT COUNT(*) FROM context_snapshots"
    ).fetchone()[0] == 0
    assert "compression_failed" in {
        event.kind for event in runtime["events"].list_for_run(runtime["run"].run_id)
    }
    runtime["db"].close()


def _with_context(session, *, window_tokens: int, ratio: float):
    return type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": window_tokens,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": ratio,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )


def _compressible_conversation(*, old_tokens: int = 120, old_repeats: int = 40):
    return [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output " * old_repeats,
            "estimated_tokens": old_tokens,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

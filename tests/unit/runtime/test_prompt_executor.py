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
from debug_agent.runtime.config import PHASE_0_SYSTEM_PROMPT
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.model_context import TokenEstimator
from debug_agent.runtime.orchestrator import ReplRuntime
from debug_agent.runtime.prompt_executor import PromptAgentExecutor
from debug_agent.runtime.stream_events import AgentStreamEvent
from debug_agent.tools.broker import ToolBroker
from debug_agent.tools.native import tool_definitions


def _runtime(tmp_path, model):
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
        config_snapshot={"provider": "fake", "timeout_seconds": 30},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    adapter = LangChainAgentLoopAdapter(model=model, tool_broker=broker)
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
    )
    return (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        executor,
    )


def test_prompt_executor_writes_model_events_assistant_event_and_turn_checkpoint(
    tmp_path,
) -> None:
    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response="assistant answer"))

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    persisted_events = events.list_for_run(run.run_id)
    latest_checkpoint = checkpoints.latest_for_run(run.run_id)
    assert result.assistant_output == "assistant answer"
    assert [event.kind for event in persisted_events] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "assistant_message",
        "checkpoint_written",
    ]
    assert persisted_events[2].payload["duration"] >= 0
    assert persisted_events[2].payload["content"] == "assistant answer"
    assert persisted_events[2].payload["tool_calls"] == []
    assert persisted_events[2].payload["artifact_ids"] == []
    assert persisted_events[2].payload["redacted_output"] is None
    assert latest_checkpoint.kind == "turn"
    assert latest_checkpoint.state["session_status"] == "running"
    assert latest_checkpoint.state["run_status"] == "running"
    assert latest_checkpoint.state["prompt_turn_counter"] == 1
    checkpoint_metadata = latest_checkpoint.state["latest_model_response_metadata"]
    assert checkpoint_metadata["context_estimate"] == result.metadata["context_estimate"]
    assert checkpoint_metadata["query_state"]["continuation_reason"] == (
        "final_assistant_response"
    )
    assert sessions.get(session.session_id).status == "running"
    assert runs.get(run.run_id).status == "running"
    assert sessions.get(session.session_id).latest_checkpoint_id == latest_checkpoint.checkpoint_id
    assert runs.get(run.run_id).latest_checkpoint_id == latest_checkpoint.checkpoint_id
    db.close()


def test_prompt_executor_records_model_completion_before_react_tool_events(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
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
            return type(
                "Response",
                (),
                {"content": "notes say hello", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
    )

    assert result.assistant_output == "notes say hello"
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "tool_call_started",
        "tool_call_completed",
        "model_call_started",
        "model_call_completed",
        "assistant_message",
        "checkpoint_written",
    ]
    first_model_completed = events.list_for_run(run.run_id)[2]
    assert first_model_completed.payload["content"] == ""
    assert first_model_completed.payload["tool_calls"] == [
        {"id": "read_file_0", "name": "read_file", "args": {"path": "notes.txt"}}
    ]
    db.close()


def test_tool_loop_followup_records_new_context_estimate_before_second_call(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0
            self.message_counts = []

        def invoke(self, messages):
            self.calls += 1
            self.message_counts.append(len(messages))
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
            return type(
                "Response",
                (),
                {"content": "notes say hello", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
    )

    query_state = result.metadata["query_state"]
    initial_estimate = result.metadata["context_estimate_history"][0]
    followup_estimate = result.metadata["context_estimate"]
    assert result.status == "completed"
    assert result.metadata["continuation_history"] == [
        "initial_model_call",
        "tool_result_continuation",
        "final_assistant_response",
    ]
    assert followup_estimate["total_tokens"] > initial_estimate["total_tokens"]
    assert query_state["latest_context_estimate"]["total_tokens"] == (
        followup_estimate["total_tokens"]
    )
    assert query_state["continuation_reason"] == "final_assistant_response"
    db.close()


def test_tool_loop_followup_runs_omission_before_second_model_call(tmp_path) -> None:
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
            return type(
                "Response",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1000,
                    "omit_old_tool_results_at_ratio": 0.9,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "Inspect old output.",
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "tool_call",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": "read_file",
        },
        {
            "seq": 3,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": "full old tool output " * 20,
            "artifact_refs": ["art_old"],
        },
        {
            "seq": 4,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    first_call_text = "\n".join(
        message["content"] for message in executor.adapter.model.messages_by_call[0]
    )
    second_call_text = "\n".join(
        message["content"] for message in executor.adapter.model.messages_by_call[1]
    )
    assert result.status == "completed"
    assert "full old tool output" in first_call_text
    assert marker not in first_call_text
    assert marker in second_call_text
    assert "full old tool output" not in second_call_text
    assert result.metadata["context_optimization"]["trigger"] == "omission"
    assert result.metadata["conversation_writeback"][2]["content"] == marker
    assert runs.get(run.run_id).context_snapshot_id is not None
    assert any(checkpoint.kind == "context" for checkpoint in checkpoints.list_for_session(session.session_id))
    assert [event.kind for event in events.list_for_run(run.run_id)].count(
        "context_optimized"
    ) == 1
    db.close()


def test_tool_loop_followup_runs_compression_before_second_model_call(tmp_path) -> None:
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
            return type(
                "Response",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    compression_calls = 0

    def compression_model(_frame):
        nonlocal compression_calls
        compression_calls += 1
        return json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["compressed before tool follow-up"],
                "inspected_or_modified_files": [],
                "remaining_work": [],
                "next_plan": [],
                "key_decisions": [],
                "constraints": [],
            }
        )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        _checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1000,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.7,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=CheckpointStore(db.connection),
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(
            model=ToolLoopModel(),
            tool_broker=ToolBroker(event_writer=events, artifact_store=artifacts),
        ),
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "old output " * 12,
            "estimated_tokens": 40,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    second_call_text = "\n".join(
        message["content"] for message in executor.adapter.model.messages_by_call[1]
    )
    assert result.status == "completed"
    assert compression_calls == 1
    assert "old output " * 4 not in second_call_text
    assert "compressed before tool follow-up" in second_call_text
    assert result.metadata["context_optimization"]["trigger"] == "compression"
    assert [event.kind for event in events.list_for_run(run.run_id)].count(
        "context_optimized"
    ) == 1
    db.close()


def test_tool_loop_followup_compression_failure_preserves_boundary(tmp_path) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
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
            raise AssertionError("ordinary follow-up model call should not run")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1000,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.7,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(
            model=ToolLoopModel(),
            tool_broker=ToolBroker(event_writer=events, artifact_store=artifacts),
        ),
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: "",
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "old output " * 12,
            "estimated_tokens": 40,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    assert result.status == "failed"
    assert result.error["error_class"] == "compression_failed"
    assert result.metadata["conversation_writeback"] is not None
    assert [message["content"] for message in result.metadata["conversation_writeback"]] == [
        message["content"] for message in conversation
    ]
    assert all(
        message["kind"] != "context_summary"
        for message in result.metadata["conversation_writeback"]
    )
    persisted = events.list_for_run(run.run_id)
    assert "compression_failed" in [event.kind for event in persisted]
    assert "assistant_message" not in [event.kind for event in persisted]
    assert [checkpoint.kind for checkpoint in checkpoints.list_for_session(session.session_id)] == [
        "context"
    ]
    assert runs.get(run.run_id).status == "running"
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    db.close()


def test_tool_loop_followup_omission_then_compression_failure_does_not_write_back_omission(
    tmp_path,
) -> None:
    class ToolLoopModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
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
            raise AssertionError("ordinary follow-up model call should not run")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1000,
                    "omit_old_tool_results_at_ratio": 0.95,
                    "compress_history_at_ratio": 0.7,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(
            model=ToolLoopModel(),
            tool_broker=ToolBroker(event_writer=events, artifact_store=artifacts),
        ),
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: "",
    )
    long_tool_output = "full old tool output " * 20
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "content": "old output",
            "estimated_tokens": 10,
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": long_tool_output,
            "artifact_refs": ["art_old"],
            "estimated_tokens": 60,
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    assert result.status == "failed"
    assert result.error["error_class"] == "compression_failed"
    writeback_contents = [
        message["content"] for message in result.metadata["conversation_writeback"]
    ]
    assert marker not in writeback_contents
    assert long_tool_output in writeback_contents
    assert [checkpoint.kind for checkpoint in checkpoints.list_for_session(session.session_id)] == [
        "context"
    ]
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    assert "compression_failed" in [
        event.kind for event in events.list_for_run(run.run_id)
    ]
    db.close()


def test_tool_loop_current_buffer_seq_does_not_protect_retained_tool_result(
    tmp_path,
) -> None:
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
            return type(
                "Response",
                (),
                {"content": "done", "tool_calls": [], "usage": {}},
            )()

    (
        workspace,
        db,
        _sessions,
        _runs,
        _events,
        _checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, ToolLoopModel())
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 1000,
                    "omit_old_tool_results_at_ratio": 0.9,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    (workspace / "notes.txt").write_text("fresh tool result " * 220, encoding="utf-8")
    conversation = [
        {
            "seq": 1,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-old",
            "model_call_id": "call-old",
            "tool_call_id": "tool-old",
            "content": "full old tool output " * 20,
            "artifact_refs": ["art_old"],
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-consumed",
            "model_call_id": "call-consumed",
            "content": "Consumed old output.",
            "metadata": {"consumed_model_call_ids": ["call-old"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="read notes",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    second_call_text = "\n".join(
        message["content"] for message in executor.adapter.model.messages_by_call[1]
    )
    assert result.status == "completed"
    assert marker in second_call_text
    assert "full old tool output" not in second_call_text
    assert result.metadata["conversation_writeback"][0]["content"] == marker
    db.close()


def test_prompt_executor_writes_large_model_response_to_text_artifact(
    tmp_path,
) -> None:
    large_response = "x" * (16 * 1024 + 1)
    (
        workspace,
        db,
        _sessions,
        _runs,
        events,
        _checkpoints,
        artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(response=large_response))

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    persisted_events = events.list_for_run(run.run_id)
    model_completed = next(
        event for event in persisted_events if event.kind == "model_call_completed"
    )
    artifact_id = model_completed.payload["artifact_ids"][0]
    assert result.assistant_output == large_response
    assert [event.kind for event in persisted_events[:4]] == [
        "user_message",
        "model_call_started",
        "artifact_registered",
        "model_call_completed",
    ]
    assert model_completed.payload["content"] is None
    assert model_completed.payload["redacted_output"].startswith(
        "[model response stored as artifact:"
    )
    assert artifacts.resolve_path(artifact_id).read_text(encoding="utf-8") == large_response
    assert artifacts.get(artifact_id).metadata == {
        "bytes": 16 * 1024 + 1,
        "event_kind": "model_call_completed",
    }
    db.close()


def test_prompt_executor_passes_session_timeout_to_adapter_request(tmp_path) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.timeout_seconds = None

        def run(self, request, context):
            self.timeout_seconds = request.timeout_seconds
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    assert result.status == "completed"
    assert adapter.timeout_seconds == 7
    db.close()


def test_prompt_executor_passes_estimated_model_context_frame_identity_to_adapter(
    tmp_path,
) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="semi-auto",
        config_snapshot={"provider": "fake", "model": "fake-model", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    assert result.status == "completed"
    frame = adapter.request.model_context_frame
    assert frame is not None
    assert adapter.request.system_prompt == ""
    assert adapter.request.conversation == []
    assert adapter.request.user_input == ""
    assert adapter.request.tools == []
    estimate = TokenEstimator().estimate_model_context_frame(frame)
    assert result.metadata["context_estimate"] == estimate.to_dict()
    assert result.metadata["query_state"]["latest_context_estimate"] == {
        "total_tokens": estimate.total_tokens,
        "estimator_version": estimate.estimator_version,
    }
    assert result.metadata["query_state"]["continuation_reason"] == "final_assistant_response"
    assert result.metadata["query_state"]["latest_model_context_frame"] is frame
    db.close()


def test_prompt_executor_uses_stream_path_when_callback_is_supplied(tmp_path) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.run_called = False
            self.stream_called = False

        def run(self, request, context):
            self.run_called = True
            raise AssertionError("run should not be called when stream callback is supplied")

        def stream(self, request, context, on_event):
            self.stream_called = True
            on_event(
                AgentStreamEvent(
                    kind="stream_text_delta",
                    payload={"model_call_id": "model_1", "text": "answer"},
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
    )
    stream_events = []

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
        agent_stream_callback=stream_events.append,
    )

    assert result.status == "completed"
    assert adapter.stream_called is True
    assert adapter.run_called is False
    assert stream_events[0].kind == "stream_context_estimate_updated"
    assert stream_events[0].payload["context_estimate"] == result.metadata[
        "context_estimate"
    ]
    assert stream_events[1:] == [
        AgentStreamEvent(
            kind="stream_text_delta",
            payload={"model_call_id": "model_1", "text": "answer"},
        )
    ]
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "user_message",
        "assistant_message",
        "checkpoint_written",
    ]
    db.close()


def test_prompt_executor_streams_context_estimate_before_adapter_call(tmp_path) -> None:
    class RecordingAdapter:
        def run(self, request, context):
            raise AssertionError("run should not be called")

        def stream(self, request, context, on_event):
            assert stream_events
            assert stream_events[0].kind == "stream_context_estimate_updated"
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_completed",
                    payload={
                        "model_call_id": "model_1",
                        "is_final": True,
                        "usage": {},
                        "duration_ms": 1,
                    },
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
    )
    stream_events = []

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
        agent_stream_callback=stream_events.append,
    )

    assert result.status == "completed"
    estimate = result.metadata["context_estimate"]
    assert stream_events[0].payload["context_estimate"] == estimate
    assert stream_events[1].payload["context_estimate"] == estimate
    db.close()


def test_prompt_executor_does_not_persist_agent_stream_events(tmp_path) -> None:
    class StreamingAdapter:
        def run(self, request, context):
            raise AssertionError("run should not be called")

        def stream(self, request, context, on_event):
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_started",
                    payload={"model_call_id": "model_1"},
                )
            )
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_completed",
                    payload={
                        "model_call_id": "model_1",
                        "is_final": True,
                        "usage": {},
                        "duration_ms": 1,
                    },
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    events = EventWriter(db.connection, db.path.parent)
    checkpoints = CheckpointStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake", "timeout_seconds": 7},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=StreamingAdapter(),
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
    )

    executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
        agent_stream_callback=lambda _event: None,
    )

    persisted_kinds = [event.kind for event in events.list_for_run(run.run_id)]
    assert "stream_model_call_started" not in persisted_kinds
    assert "stream_model_call_completed" not in persisted_kinds
    assert persisted_kinds == [
        "user_message",
        "assistant_message",
        "checkpoint_written",
    ]
    db.close()


def test_prompt_executor_writes_failed_model_event_and_error_checkpoint(tmp_path) -> None:
    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        executor,
    ) = _runtime(tmp_path, FakeChatModel(error=RuntimeError("provider failed")))

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="hello",
        workspace_root=str(workspace),
    )

    latest_checkpoint = checkpoints.latest_for_run(run.run_id)
    assert result.status == "failed"
    assert result.error["error_class"] == "model_error"
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "user_message",
        "model_call_started",
        "model_call_failed",
        "checkpoint_written",
    ]
    failed_event = events.list_for_run(run.run_id)[2]
    assert failed_event.payload["error_class"] == "model_error"
    assert failed_event.payload["message"] == "provider failed"
    assert failed_event.payload["source"] == "model"
    assert failed_event.payload["recoverable"] is True
    assert failed_event.payload["duration"] >= 0
    assert latest_checkpoint.kind == "error"
    assert latest_checkpoint.state["latest_error_summary"] == "provider failed"
    assert sessions.get(session.session_id).status == "running"
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_prompt_executor_omits_old_tool_results_and_persists_context_snapshot(
    tmp_path,
) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        _artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
        adapter=adapter,
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 500,
                    "omit_old_tool_results_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    long_output = "full old tool output " * 80
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "I will inspect",
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "tool_call",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "read_file",
        },
        {
            "seq": 3,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": long_output,
            "artifact_refs": ["art_full"],
            "metadata": {"path": "old.log"},
        },
        {
            "seq": 4,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed older result.",
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    sent_contents = [
        segment.content
        for segment in adapter.request.model_context_frame.ordered_message_segments()
    ]
    assert result.status == "completed"
    assert marker in sent_contents
    assert long_output not in sent_contents
    assert result.metadata["context_optimization"] == {
        "message": "Context optimized: reduced from "
        + f"{result.metadata['context_optimization']['reduced_from_tokens']} to "
        + f"{result.metadata['context_optimization']['reduced_to_tokens']} tokens "
        + "by omitting earlier tool results.",
        "omitted_tool_result_count": 1,
        "reduced_from_tokens": result.metadata["context_estimate_history"][0]["total_tokens"],
        "reduced_to_tokens": result.metadata["context_estimate"]["total_tokens"],
        "trigger": "omission",
    }
    assert result.metadata["context_optimization"]["reduced_to_tokens"] < (
        result.metadata["context_optimization"]["reduced_from_tokens"]
    )

    row = db.connection.execute(
        """
        SELECT context_snapshot_id, trigger, summary, retained_messages_json,
               omitted_tool_result_count, artifact_refs_json, token_estimate_json,
               payload_artifact_id
        FROM context_snapshots
        WHERE run_id = ?
        """,
        (run.run_id,),
    ).fetchone()
    assert row is not None
    assert row[1] == "omission"
    assert row[2] == ""
    assert row[4] == 1
    assert row[5] == '["art_full"]'
    token_estimate = json.loads(row[6])
    assert token_estimate["before"] == result.metadata["context_estimate_history"][0]
    assert token_estimate["after"] == result.metadata["context_estimate"]
    assert row[7] is None
    assert runs.get(run.run_id).context_snapshot_id == row[0]

    persisted_checkpoints = checkpoints.list_for_session(session.session_id)
    context_checkpoint = next(
        checkpoint for checkpoint in persisted_checkpoints if checkpoint.kind == "context"
    )
    assert context_checkpoint.state == {
        "session_status": "running",
        "run_status": "running",
        "prompt_turn_counter": 1,
        "context_snapshot_id": row[0],
        "active_skill_records": [],
        "latest_artifact_ids": ["art_full"],
        "latest_error_summary": None,
        "token_estimate": {
            "before": result.metadata["context_estimate_history"][0],
            "after": result.metadata["context_estimate"],
        },
    }
    assert checkpoints.latest_for_run(run.run_id).kind == "turn"
    context_events = [event for event in events.list_for_run(run.run_id) if event.kind == "context_optimized"]
    assert context_events[0].payload["context_snapshot_id"] == row[0]
    assert context_events[0].payload["reduced_to_tokens"] == (
        result.metadata["context_estimate"]["total_tokens"]
    )
    db.close()


def test_prompt_executor_automatically_compresses_before_initial_model_call(
    tmp_path,
) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    compression_frames = []

    def compression_model(frame):
        compression_frames.append(frame)
        return json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["read old output"],
                "inspected_or_modified_files": ["old.py"],
                "remaining_work": ["finish fix"],
                "next_plan": ["run unit tests"],
                "key_decisions": ["keep snapshots non-authoritative"],
                "constraints": ["no manual compression"],
                "visible_artifact_refs": ["art_old"],
            }
        )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=adapter,
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 420,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output " * 40,
            "estimated_tokens": 120,
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "artifact art_old",
            "artifact_refs": ["art_old"],
            "estimated_tokens": 8,
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "consumed old call",
            "estimated_tokens": 8,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    sent_contents = [
        segment.content
        for segment in adapter.request.model_context_frame.ordered_message_segments()
    ]
    assert result.status == "completed"
    assert len(compression_frames) == 1
    assert [message.seq for message in compression_frames[0].evicted_messages] == [1, 2]
    assert "old output " * 4 not in sent_contents
    assert any('"task_goal":"continue debugging"' in content for content in sent_contents)
    assert result.metadata["context_optimization"]["trigger"] == "compression"
    assert result.metadata["context_optimization"]["evicted_model_call_group_count"] == 1
    assert result.metadata["conversation_writeback"][0]["kind"] == "context_summary"

    persisted_kinds = [event.kind for event in events.list_for_run(run.run_id)]
    assert persisted_kinds[:4] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "context_optimized",
    ]
    assert events.list_for_run(run.run_id)[1].payload["purpose"] == "compression"
    assert events.list_for_run(run.run_id)[1].payload["tool_schema_bindings"] == []
    row = db.connection.execute(
        "SELECT trigger, summary, evicted_message_count, evicted_model_call_group_count FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()
    assert row[0] == "compression"
    assert '"visible_artifact_refs":["art_old"]' in row[1]
    assert row[2] == 2
    assert row[3] == 1
    assert any(checkpoint.kind == "context" for checkpoint in checkpoints.list_for_session(session.session_id))
    db.close()


def test_prompt_executor_compression_failure_aborts_without_conversation_mutation(
    tmp_path,
) -> None:
    class FailingAdapter:
        def run(self, request, context):
            raise AssertionError("ordinary model call should not run after compression failure")

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=FailingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: "",
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 420,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output " * 40,
            "estimated_tokens": 120,
        },
        {
            "seq": 2,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "consumed old",
            "estimated_tokens": 8,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    assert result.status == "failed"
    assert result.error["error_class"] == "compression_failed"
    assert [message["content"] for message in result.metadata["conversation_writeback"]] == [
        message["content"] for message in conversation
    ]
    assert all(
        message["kind"] != "context_summary"
        for message in result.metadata["conversation_writeback"]
    )
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    persisted = events.list_for_run(run.run_id)
    assert [event.kind for event in persisted] == [
        "user_message",
        "model_call_started",
        "model_call_completed",
        "compression_failed",
        "checkpoint_written",
    ]
    assert persisted[1].payload["purpose"] == "compression"
    assert persisted[3].payload["error_class"] == "compression_failed"
    context_checkpoint = next(
        checkpoint for checkpoint in checkpoints.list_for_session(session.session_id)
        if checkpoint.kind == "context"
    )
    assert context_checkpoint.state["latest_error_summary"] == (
        "Context compression failed. The current turn was aborted."
    )
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_prompt_executor_context_limit_exceeded_aborts_without_model_call(
    tmp_path,
) -> None:
    class FailingAdapter:
        def run(self, request, context):
            raise AssertionError("ordinary model call should not run over context limit")

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=FailingAdapter(),
        tool_definitions=[],
        system_prompt="system " * 100,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=None,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 40,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 1.0,
                    "retain_recent_model_calls": 4,
                    "compression_reserved_output_tokens": 10,
                },
            },
        }
    )

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=[],
    )

    expected_message = (
        "Context window still exceeds the limit after compression. "
        "The current turn was aborted."
    )
    assert result.status == "failed"
    assert result.error == {
        "error_class": "context_limit_exceeded",
        "message": expected_message,
    }
    assert result.metadata["failure_scope"] == "turn"
    assert result.metadata["context_estimate"]["total_tokens"] > 40
    persisted = events.list_for_run(run.run_id)
    assert [event.kind for event in persisted] == [
        "user_message",
        "context_limit_exceeded",
        "checkpoint_written",
    ]
    assert persisted[1].payload["message"] == expected_message
    assert persisted[1].payload["error_class"] == "context_limit_exceeded"
    context_checkpoint = checkpoints.latest_for_run(run.run_id)
    assert context_checkpoint.kind == "context"
    assert context_checkpoint.state["error_class"] == "context_limit_exceeded"
    assert context_checkpoint.state["session_status"] == "running"
    assert context_checkpoint.state["run_status"] == "running"
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_prompt_executor_manual_compress_noop_does_not_call_model_or_snapshot(
    tmp_path,
) -> None:
    compression_calls = 0

    def compression_model(_frame):
        nonlocal compression_calls
        compression_calls += 1
        return "{}"

    (
        _workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )

    result = executor.manual_compress(
        session=session,
        run=run,
        conversation=[],
        prompt_turn_counter=1,
    )

    assert result.status == "completed"
    assert result.assistant_output == "No compressible history."
    assert compression_calls == 0
    assert "conversation_writeback" not in result.metadata
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    assert events.list_for_run(run.run_id) == []
    assert checkpoints.latest_for_run(run.run_id) is None
    db.close()


def test_prompt_executor_manual_compress_success_writes_snapshot_and_message(
    tmp_path,
) -> None:
    (
        _workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=lambda _frame: json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["manual compression ran"],
                "inspected_or_modified_files": [],
                "remaining_work": [],
                "next_plan": [],
                "key_decisions": [],
                "constraints": [],
            }
        ),
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 420,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 1.0,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )

    result = executor.manual_compress(
        session=session,
        run=run,
        conversation=[
            {
                "seq": 1,
                "role": "assistant",
                "kind": "assistant_output",
                "turn_id": "turn-1",
                "model_call_id": "call-1",
                "content": "old output " * 40,
                "estimated_tokens": 120,
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
        ],
        prompt_turn_counter=1,
    )

    assert result.status == "completed"
    assert result.assistant_output.startswith("Context compressed: reduced from ")
    assert " to " in result.assistant_output
    assert result.metadata["context_optimization"]["trigger"] == "manual"
    assert result.metadata["conversation_writeback"][0]["kind"] == "context_summary"
    assert db.connection.execute(
        "SELECT trigger FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == "manual"
    assert [event.kind for event in events.list_for_run(run.run_id)] == [
        "model_call_started",
        "model_call_completed",
        "context_optimized",
    ]
    assert checkpoints.latest_for_run(run.run_id).kind == "context"
    db.close()


def test_prompt_executor_manual_compress_oldest_group_failure_preserves_boundary(
    tmp_path,
) -> None:
    compression_calls = 0

    def compression_model(_frame):
        nonlocal compression_calls
        compression_calls += 1
        return "{}"

    (
        _workspace,
        db,
        _sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=LangChainAgentLoopAdapter(model=FakeChatModel(response="unused")),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 800,
                    "omit_old_tool_results_at_ratio": 1.0,
                    "compress_history_at_ratio": 1.0,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old output",
            "estimated_tokens": 1000,
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

    result = executor.manual_compress(
        session=session,
        run=run,
        conversation=conversation,
        prompt_turn_counter=1,
    )

    expected_message = (
        "Context compression could not fit the oldest eligible history group. "
        "The current turn was aborted. Start a new session to continue with a "
        "fresh context window."
    )
    assert result.status == "failed"
    assert result.error == {
        "error_class": "compression_failed",
        "message": expected_message,
    }
    assert result.metadata["failure_scope"] == "turn"
    assert [message["content"] for message in result.metadata["conversation_writeback"]] == [
        message["content"] for message in conversation
    ]
    assert all(
        message["kind"] != "context_summary"
        for message in result.metadata["conversation_writeback"]
    )
    assert compression_calls == 0
    assert db.connection.execute(
        "SELECT COUNT(*) FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()[0] == 0
    persisted = events.list_for_run(run.run_id)
    assert [event.kind for event in persisted] == [
        "compression_failed",
        "checkpoint_written",
    ]
    assert persisted[0].payload["message"] == expected_message
    assert checkpoints.latest_for_run(run.run_id).state["latest_error_summary"] == (
        expected_message
    )
    assert runs.get(run.run_id).status == "running"
    db.close()


def test_omission_plus_compression_writes_only_final_snapshot(tmp_path) -> None:
    class RecordingAdapter:
        def run(self, request, context):
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    def compression_model(_frame):
        return json.dumps(
            {
                "task_goal": "continue debugging",
                "completed_work": ["omitted and compressed"],
                "inspected_or_modified_files": [],
                "remaining_work": [],
                "next_plan": [],
                "key_decisions": [],
                "constraints": [],
            }
        )

    (
        workspace,
        db,
        _sessions,
        runs,
        events,
        _checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=CheckpointStore(db.connection),
        artifact_store=artifacts,
        adapter=RecordingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
        compression_model=compression_model,
    )
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                    "window_tokens": 500,
                    "omit_old_tool_results_at_ratio": 0.1,
                    "compress_history_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                    "compression_reserved_output_tokens": 40,
                },
            },
        }
    )
    conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "content": "old",
            "estimated_tokens": 10,
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "full old tool output " * 80,
            "artifact_refs": ["art_full"],
            "estimated_tokens": 60,
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed older result.",
            "estimated_tokens": 10,
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = executor.run_turn(
        session=session,
        run=run,
        user_input="continue",
        workspace_root=str(workspace),
        conversation=conversation,
    )

    rows = db.connection.execute(
        "SELECT trigger FROM context_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchall()
    context_events = [
        event for event in events.list_for_run(run.run_id)
        if event.kind == "context_optimized"
    ]
    assert result.status == "completed"
    assert rows == [("omission | compression",)]
    assert [event.payload["trigger"] for event in context_events] == [
        "omission | compression"
    ]
    assert result.metadata["context_optimization"]["omitted_tool_result_count"] == 1
    db.close()


def test_repl_runtime_writes_back_omitted_conversation_and_metadata(tmp_path) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, context):
            self.request = request
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    session = type(session)(
        **{
            **session.to_dict(),
            "config_snapshot": {
                **session.config_snapshot,
                "context": {
                        "window_tokens": 500,
                    "omit_old_tool_results_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    db.connection.execute(
        "UPDATE sessions SET config_snapshot_json = ? WHERE session_id = ?",
        (json.dumps(session.config_snapshot, sort_keys=True), session.session_id),
    )
    db.connection.commit()
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=RecordingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
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
        workspace_root=workspace,
    )
    runtime.conversation = [
        {
            "seq": 1,
            "role": "assistant",
            "kind": "tool_call",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "shell_exec",
        },
        {
            "seq": 2,
            "role": "tool",
            "kind": "tool_result",
            "turn_id": "turn-1",
            "model_call_id": "call-1",
            "tool_call_id": "tool-1",
            "content": "full old tool output " * 80,
            "artifact_refs": ["art_full"],
        },
        {
            "seq": 3,
            "role": "assistant",
            "kind": "assistant_output",
            "turn_id": "turn-2",
            "model_call_id": "call-2",
            "content": "Consumed older result.",
            "metadata": {"consumed_model_call_ids": ["call-1"]},
        },
    ]

    result = runtime.run_turn("continue")

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    assert result.status == "completed"
    assert runtime.conversation[1]["content"] == marker
    assert runtime.conversation[1]["artifact_refs"] == ["art_full"]
    assert runtime.conversation[-2]["kind"] == "current_user_input"
    assert runtime.conversation[-2]["turn_id"] == "turn-1"
    assert runtime.conversation[-2]["seq"] > 3
    assert runtime.conversation[-1]["kind"] == "assistant_output"
    assert runtime.conversation[-1]["model_call_id"].startswith("repl_turn_1")
    assert runtime.conversation[-1]["metadata"]["consumed_model_call_ids"] == [
        "call-1",
        "call-2",
    ]
    db.close()


def test_repl_runtime_updates_latest_context_estimate_from_stream_event(tmp_path) -> None:
    class StreamingAdapter:
        def run(self, request, context):
            raise AssertionError("run should not be called")

        def stream(self, request, context, on_event):
            on_event(
                AgentStreamEvent(
                    kind="stream_model_call_completed",
                    payload={
                        "model_call_id": "model_1",
                        "is_final": True,
                        "usage": {},
                        "duration_ms": 1,
                    },
                )
            )
            return AgentRunResult(
                status="completed",
                assistant_output="answer",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

        def cancel(self, run_id: str) -> None:
            raise AssertionError("cancel should not be called")

    (
        workspace,
        db,
        sessions,
        runs,
        events,
        checkpoints,
        artifacts,
        session,
        run,
        _executor,
    ) = _runtime(tmp_path, FakeChatModel(response="unused"))
    executor = PromptAgentExecutor(
        event_writer=events,
        checkpoint_store=checkpoints,
        artifact_store=artifacts,
        adapter=StreamingAdapter(),
        tool_definitions=tool_definitions(),
        system_prompt=PHASE_0_SYSTEM_PROMPT,
        skill_snapshot_store=SkillSnapshotStore(db.connection),
        run_store=runs,
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
        workspace_root=workspace,
    )
    stream_events: list[AgentStreamEvent] = []

    result = runtime.run_turn("hello", agent_stream_callback=stream_events.append)

    assert result.status == "completed"
    assert stream_events[0].kind == "stream_context_estimate_updated"
    assert runtime.latest_context_estimate == stream_events[0].payload[
        "context_estimate"
    ]
    db.close()

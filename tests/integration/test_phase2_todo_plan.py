from __future__ import annotations

import json

from debug_agent.observability.trace_writer import TraceWriter
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


def _config_with_todo_call() -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": "todo recorded",
        "fake_tool_calls": [
            {
                "id": "todo_call_1",
                "name": "todo",
                "args": {
                    "items": [
                        {
                            "content": "Inspect exported image",
                            "status": "in_progress",
                            "activeForm": "Inspecting exported image",
                        },
                        {
                            "content": "Run verification",
                            "status": "pending",
                        },
                    ]
                },
            }
        ],
        "timeout_seconds": 30,
        "system_prompt": "Use runtime tools when needed.",
    }


def test_one_shot_todo_call_persists_and_next_model_call_sees_plan(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "make a plan",
        _config_with_todo_call(),
        approval_mode="yolo",
    )

    assert result.exit_code == 0
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        plan = TodoPlanStore(db.connection).get_current(result.run_id)
        assert plan.version == 1
        assert [item["content"] for item in plan.items] == [
            "Inspect exported image",
            "Run verification",
        ]
        checkpoint = CheckpointStore(db.connection).latest_for_run(result.run_id)
        assert checkpoint is not None
        frame = checkpoint.state["latest_model_response_metadata"]["query_state"][
            "latest_model_context_frame"
        ]
        tool_names = [binding["name"] for binding in frame["tool_schema_bindings"]]
        assert "todo" in tool_names
        assert "read_file" in tool_names
        assert "view_image" not in tool_names
        todo_segments = [
            segment
            for segment in frame["message_segments"]
            if segment["kind"] == "runtime_todo_plan"
        ]
        assert len(todo_segments) == 1
        assert "Inspect exported image" in todo_segments[0]["content"]
        assert "Current Todo Plan is empty." not in todo_segments[0]["content"]
        status = RuntimeOrchestrator(workspace_root=workspace).status(result.session_id)
        assert status.fields["todo_plan"] == {
            "plan_version": 1,
            "counts": {
                "pending": 1,
                "in_progress": 1,
                "completed": 0,
            },
        }
    finally:
        db.close()


def test_manual_compress_leaves_todo_plan_unchanged_and_trace_observes_store(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        session = db.connection.execute(
            "SELECT session_id FROM sessions LIMIT 1"
        ).fetchone()
        assert session is None
    finally:
        db.close()

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "make a plan",
        _config_with_todo_call(),
        approval_mode="yolo",
    )
    assert result.exit_code == 0

    db = RuntimeDatabase.bootstrap(workspace)
    try:
        before = TodoPlanStore(db.connection).get_current(result.run_id)
        events_before = [
            event.kind
            for event in EventWriter(db.connection, db.path.parent).list_for_run(
                result.run_id
            )
        ]
        # Simulate a compression summary mentioning a conflicting plan. Todo Plan
        # truth must remain store-owned and must not be rebuilt from summary text.
        runs = RunStore(db.connection)
        run = runs.get(result.run_id)
        db.connection.execute(
            """
            INSERT INTO context_snapshots (
                context_snapshot_id, session_id, run_id, trigger,
                source_checkpoint_id, active_skill_records_json, summary,
                retained_messages_json, omitted_tool_result_count,
                evicted_message_count, evicted_model_call_group_count,
                artifact_refs_json, token_estimate_json, payload_artifact_id,
                created_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ctx_conflicting_summary",
                result.session_id,
                result.run_id,
                "manual",
                run.latest_checkpoint_id,
                "[]",
                json.dumps(
                    {
                        "task_goal": "continue",
                        "completed_work": [],
                        "inspected_or_modified_files": [],
                        "remaining_work": [],
                        "next_plan": ["Conflicting summary-only plan"],
                        "key_decisions": [],
                        "constraints": [],
                    },
                    sort_keys=True,
                ),
                "[]",
                0,
                0,
                0,
                "[]",
                "{}",
                None,
                "2026-06-01T00:00:00Z",
                1,
            ),
        )
        db.connection.commit()
        after = TodoPlanStore(db.connection).get_current(result.run_id)
        assert after == before
        assert [
            event.kind
            for event in EventWriter(db.connection, db.path.parent).list_for_run(
                result.run_id
            )
        ] == events_before

        trace = TraceWriter(db.connection, db.path.parent).refresh_if_stale(
            result.session_id
        )
        trace_text = trace.trace_path.read_text(encoding="utf-8")
        assert "Inspect exported image" in trace_text
        assert "Conflicting summary-only plan" not in trace_text
    finally:
        db.close()

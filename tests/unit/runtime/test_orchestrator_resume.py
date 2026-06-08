from __future__ import annotations

import json
import sqlite3

from langchain_core.messages import ToolMessage, convert_to_messages

from debug_agent.cli.exit_codes import (
    ERROR_ACTIVE_SESSION_CONFLICT,
    ERROR_EXECUTION_FAILED,
    ERROR_PERSISTENCE_READ,
)
from debug_agent.adapters.model_factory import ModelFactoryResult
from debug_agent.persistence.conversation import ConversationAppend, ConversationStore
from debug_agent.persistence.errors import StoreError
from debug_agent.runtime import orchestrator as orchestrator_module
from debug_agent.runtime.orchestrator import RuntimeOrchestrator, visible_tool_definitions


def _config(response: str = "fake answer") -> dict:
    return {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": response,
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer concisely "
            "and use only tools exposed by the runtime."
        ),
    }


def test_resume_revives_one_shot_same_lineage_without_conversation_append(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    assert one_shot.exit_code == 0

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == 0
    assert resume.session_id == one_shot.session_id
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        sessions = conn.execute(
            """
            SELECT session_id, status, active_run_id, latest_checkpoint_id
            FROM sessions
            """
        ).fetchall()
        runs = conn.execute(
            """
            SELECT run_id, session_id, status, latest_checkpoint_id
            FROM runs
            """
        ).fetchall()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        checkpoint_ids = [
            row[0] for row in conn.execute("SELECT checkpoint_id FROM checkpoints")
        ]
        durable_rows = conn.execute(
            "SELECT role, kind, content_json FROM conversation_messages ORDER BY message_index"
        ).fetchall()
        checkpoint_id = checkpoint_ids[0]

    assert sessions == [(one_shot.session_id, "running", one_shot.run_id, checkpoint_id)]
    assert runs == [(one_shot.run_id, one_shot.session_id, "running", checkpoint_id)]
    assert event_kinds[-2:] == ["session_resumed", "run_resumed"]
    assert len(runs) == 1
    assert len(checkpoint_ids) == 1
    assert [(row[0], row[1], json.loads(row[2])) for row in durable_rows] == [
        ("user", "user_input", {"content": "hello"}),
        ("assistant", "assistant_output", {"content": "one shot answer"}),
    ]


def test_resumed_repl_with_runtime_cancellation_fact_runs_next_prompt(
    tmp_path,
    monkeypatch,
) -> None:
    captured_messages: list[object] = []

    class ValidatingProviderMessageModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, messages):
            captured_messages[:] = list(messages)
            seen_non_system = False
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "runtime":
                    raise ValueError("Unexpected message type: 'runtime'.")
                if isinstance(message, dict) and message.get("role") == "system":
                    if seen_non_system:
                        raise ValueError("Received multiple non-consecutive system messages.")
                else:
                    seen_non_system = True
            return type(
                "Response",
                (),
                {"content": "post resume answer", "tool_calls": [], "usage": {}},
            )()

    class ValidatingModelFactory:
        def create(self, _config):
            return ModelFactoryResult(model=ValidatingProviderMessageModel(), error=None)

    monkeypatch.setattr(orchestrator_module, "ModelFactory", ValidatingModelFactory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = RuntimeOrchestrator(workspace_root=workspace).start_repl(
        _config("post resume answer")
    )
    assert started.runtime is not None
    runtime = started.runtime
    try:
        content = {
            "error_class": "cancelled",
            "reason": "user_cancel_running",
            "message": "Turn cancelled by user.",
            "artifact_ids": [],
        }
        store = ConversationStore(runtime.db.connection)
        user_rows = store.append_closed_group(
            session_id=runtime.session_id,
            run_id=runtime.run_id,
            messages=[
                ConversationAppend(
                    turn_id="turn-cancelled",
                    message_group_id="turn-cancelled:user",
                    model_call_id=None,
                    group_position=0,
                    group_row_count=1,
                    role="user",
                    kind="user_input",
                    content={"content": "cancelled user prompt"},
                    metadata={},
                ),
            ],
        )
        fact_rows = store.append_closed_group(
            session_id=runtime.session_id,
            run_id=runtime.run_id,
            messages=[
                ConversationAppend(
                    turn_id="turn-cancelled",
                    message_group_id="turn-cancelled:runtime:cancellation_fact",
                    model_call_id=None,
                    group_position=0,
                    group_row_count=1,
                    role="runtime",
                    kind="cancellation_fact",
                    content=content,
                    metadata={
                        "error_class": "cancelled",
                        "reason": "user_cancel_running",
                    },
                )
            ],
        )
        runtime.conversation = [
            {
                "seq": 1,
                "role": "user",
                "kind": "current_user_input",
                "turn_id": "turn-cancelled",
                "model_call_id": None,
                "tool_call_id": None,
                "content": "cancelled user prompt",
                "artifact_refs": [],
                "metadata": {},
                "durable_message_index": user_rows[0].message_index,
            },
            {
                "seq": 2,
                "role": "runtime",
                "kind": "cancellation_fact",
                "turn_id": "turn-cancelled",
                "model_call_id": None,
                "tool_call_id": None,
                "content": content,
                "artifact_refs": [],
                "metadata": {
                    "error_class": "cancelled",
                    "reason": "user_cancel_running",
                },
                "durable_message_index": fact_rows[0].message_index,
            }
        ]
        runtime.cancel_idle()
    finally:
        runtime.close()

    resumed = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        runtime.session_id
    )

    assert resumed.runtime is not None
    try:
        result = resumed.runtime.run_turn("continue")
        assert result.status == "completed"
        assert result.assistant_output == "post resume answer"
        rendered_messages = [
            message.get("content", "")
            for message in captured_messages
            if isinstance(message, dict)
        ]
        assert "Turn cancelled by user." not in "\n".join(rendered_messages)
    finally:
        resumed.runtime.close()


def test_resumed_repl_projects_repeated_tool_history_for_next_prompt(
    tmp_path,
    monkeypatch,
) -> None:
    class ValidatingToolHistoryModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, messages):
            converted = convert_to_messages(messages)
            tool_pair_ids = []
            for index, message in enumerate(converted):
                tool_calls = getattr(message, "tool_calls", None)
                if not tool_calls:
                    continue
                next_message = converted[index + 1]
                if not isinstance(next_message, ToolMessage):
                    raise AssertionError("tool call must be followed by tool result")
                if not next_message.tool_call_id:
                    raise AssertionError("tool result id must be non-empty")
                if next_message.tool_call_id != tool_calls[0]["id"]:
                    raise AssertionError("tool result id must match assistant tool call")
                tool_pair_ids.append(next_message.tool_call_id)
            assert len(tool_pair_ids) == 2
            assert len(set(tool_pair_ids)) == 2
            return type(
                "Response",
                (),
                {"content": "post resume answer", "tool_calls": [], "usage": {}},
            )()

    class ValidatingModelFactory:
        def create(self, _config):
            return ModelFactoryResult(model=ValidatingToolHistoryModel(), error=None)

    monkeypatch.setattr(orchestrator_module, "ModelFactory", ValidatingModelFactory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = RuntimeOrchestrator(workspace_root=workspace).start_repl(
        _config("post resume answer")
    )
    assert started.runtime is not None
    runtime = started.runtime
    try:
        store = ConversationStore(runtime.db.connection)
        rows = []
        for turn_number, tool_name, output in [
            (1, "view_image", "cancelled"),
            (2, "shell_exec", "completed"),
        ]:
            turn_id = f"turn-tool-{turn_number}"
            rows.extend(
                store.append_closed_group(
                    session_id=runtime.session_id,
                    run_id=runtime.run_id,
                    messages=[
                        ConversationAppend(
                            turn_id=turn_id,
                            message_group_id=f"{turn_id}:user",
                            model_call_id=None,
                            group_position=0,
                            group_row_count=1,
                            role="user",
                            kind="user_input",
                            content={"content": f"tool prompt {turn_number}"},
                            metadata={},
                        ),
                    ],
                )
            )
            rows.extend(
                store.append_closed_group(
                    session_id=runtime.session_id,
                    run_id=runtime.run_id,
                    messages=[
                        ConversationAppend(
                            turn_id=turn_id,
                            message_group_id=f"{turn_id}:assistant_tool_call",
                            model_call_id="model_call_1",
                            group_position=0,
                            group_row_count=2,
                            role="assistant",
                            kind="assistant_tool_call",
                            content={
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "model_call_1_tool_1",
                                        "name": tool_name,
                                        "args": {},
                                    }
                                ],
                            },
                            metadata={},
                            tool_call_id="model_call_1_tool_1",
                        ),
                        ConversationAppend(
                            turn_id=turn_id,
                            message_group_id=f"{turn_id}:assistant_tool_call",
                            model_call_id="model_call_1",
                            group_position=1,
                            group_row_count=2,
                            role="tool",
                            kind="tool_result",
                            content={
                                "message_type": "tool_result",
                                "content": output,
                                "tool_call_id": "model_call_1_tool_1",
                            },
                            metadata={},
                            tool_call_id="model_call_1_tool_1",
                        ),
                    ],
                )
            )
        runtime.conversation = [
            {
                "seq": index + 1,
                "role": row.role,
                "kind": row.kind,
                "turn_id": row.turn_id,
                "model_call_id": row.model_call_id,
                "tool_call_id": row.tool_call_id,
                "content": row.content,
                "artifact_refs": [],
                "metadata": {},
                "durable_message_index": row.message_index,
            }
            for index, row in enumerate(rows)
        ]
        runtime.cancel_idle()
    finally:
        runtime.close()

    resumed = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        runtime.session_id
    )

    assert resumed.runtime is not None
    try:
        result = resumed.runtime.run_turn("continue")
        assert result.status == "completed"
        assert result.assistant_output == "post resume answer"
    finally:
        resumed.runtime.close()


def test_resumed_repl_idle_cancel_after_new_turn_writes_new_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    class PromptAnswerModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            return type(
                "Response",
                (),
                {"content": "post resume answer", "tool_calls": [], "usage": {}},
            )()

    class PromptAnswerModelFactory:
        def create(self, _config):
            return ModelFactoryResult(model=PromptAnswerModel(), error=None)

    monkeypatch.setattr(orchestrator_module, "ModelFactory", PromptAnswerModelFactory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = RuntimeOrchestrator(workspace_root=workspace).start_repl(
        _config("post resume answer")
    )
    assert started.runtime is not None
    session_id = started.runtime.session_id
    try:
        started.runtime.run_turn("first prompt")
        started.runtime.cancel_idle()
    finally:
        started.runtime.close()

    resumed = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(session_id)
    assert resumed.runtime is not None
    try:
        result = resumed.runtime.run_turn("second prompt")
        assert result.status == "completed"
        resumed.runtime.cancel_idle()
    finally:
        resumed.runtime.close()

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_checkpoint, run_checkpoint = conn.execute(
            """
            SELECT s.latest_checkpoint_id, r.latest_checkpoint_id
            FROM sessions s
            JOIN runs r ON r.run_id = s.active_run_id OR r.session_id = s.session_id
            WHERE s.session_id = ?
            ORDER BY r.updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        checkpoint_count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]

    assert session_checkpoint
    assert session_checkpoint == run_checkpoint
    assert checkpoint_count == 2

    second_resume = RuntimeOrchestrator(workspace_root=workspace).resume(session_id)

    assert second_resume.exit_code == 0


def test_resume_rejects_non_terminal_target(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = RuntimeOrchestrator(workspace_root=workspace).start_repl(_config())
    assert controller.runtime is not None
    session_id = controller.runtime.session_id
    try:
        resume = RuntimeOrchestrator(workspace_root=workspace).resume(session_id)
    finally:
        controller.runtime.close()

    assert resume.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert resume.error["error_class"] == "policy_error"
    assert resume.error["reason"] == "workspace_owner_not_proven_stale"


def test_resume_rejects_missing_checkpoint(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        conn.execute("UPDATE sessions SET latest_checkpoint_id = NULL")
        conn.commit()

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == ERROR_EXECUTION_FAILED
    assert resume.error["error_class"] == "runtime_error"
    assert resume.error["reason"] == "resume_checkpoint_required"


def test_resume_rejects_invalid_checkpoint_payload(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        state = json.loads(conn.execute("SELECT state_json FROM checkpoints").fetchone()[0])
        state["terminal_status"] = "failed"
        conn.execute(
            "UPDATE checkpoints SET state_json = ?",
            (json.dumps(state, ensure_ascii=False, sort_keys=True),),
        )
        conn.commit()

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == ERROR_PERSISTENCE_READ
    assert resume.error["error_class"] == "persistence_error"
    assert resume.error["reason"] == "checkpoint_invalid"


def test_resume_rejects_active_ownership_conflict(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    active = RuntimeOrchestrator(workspace_root=other_workspace).start_repl(_config())
    assert active.runtime is not None
    try:
        with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
            active_run_id = active.runtime.run_id
            active_session_id = active.runtime.session_id
            # Insert a conflicting active owner directly into the target DB.
            config_json = json.dumps(_config(), ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace_root, status, approval_mode, active_run_id,
                    artifact_root, config_snapshot_json, latest_checkpoint_id,
                    created_at, updated_at, error_summary, terminal_reason,
                    terminal_error_json, non_resumable_startup_failure, version
                )
                VALUES (?, ?, 'running', 'normal', ?, ?, ?, NULL,
                        '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z',
                        NULL, NULL, NULL, 0, 1)
                """,
                (
                    active_session_id,
                    str(workspace.resolve()),
                    active_run_id,
                    str(workspace / ".sessions" / active_session_id / "artifacts"),
                    config_json,
                ),
            )
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, session_id, parent_run_id, run_type, status,
                    active_skills_json, latest_checkpoint_id, context_snapshot_id,
                    created_at, updated_at, error_summary, terminal_reason,
                    terminal_error_json, non_resumable_startup_failure, version
                )
                VALUES (?, ?, NULL, 'prompt', 'running', '[]', NULL, NULL,
                        '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z',
                        NULL, NULL, NULL, 0, 1)
                """,
                (active_run_id, active_session_id),
            )
            conn.commit()

        resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)
    finally:
        active.runtime.close()

    assert resume.exit_code == ERROR_ACTIVE_SESSION_CONFLICT
    assert resume.error["error_class"] == "policy_error"
    assert resume.error["reason"] == "workspace_owner_not_proven_stale"


def test_resume_restores_drifted_current_todo_without_update_event(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "make a plan",
        _config("planned"),
    )
    assert one_shot.exit_code == 0
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        conn.execute(
            """
            INSERT INTO todo_plans (
                run_id, session_id, plan_version, items_json, created_at,
                updated_at, version
            )
            VALUES (?, ?, 99,
                    '[{"index":1,"content":"drift","status":"pending","metadata":{}}]',
                    '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z', 1)
            """,
            (one_shot.run_id, one_shot.session_id),
        )
        before_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE kind = 'todo_updated'"
        ).fetchone()[0]
        conn.commit()

    resume = RuntimeOrchestrator(workspace_root=workspace).resume(one_shot.session_id)

    assert resume.exit_code == 0
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        plan_version, items_json = conn.execute(
            "SELECT plan_version, items_json FROM todo_plans WHERE run_id = ?",
            (one_shot.run_id,),
        ).fetchone()
        after_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE kind = 'todo_updated'"
        ).fetchone()[0]

    assert plan_version == 0
    assert json.loads(items_json) == []
    assert after_events == before_events


def test_resume_preserves_approval_grants_active_skills_and_frozen_tools(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha skill\n---\nUse alpha.\n",
        encoding="utf-8",
    )
    config = _config("skill activated")
    config["execution"] = {
        "default_shell_timeout_seconds": 11,
        "max_shell_timeout_seconds": 22,
    }
    config["fake_tool_calls"] = [
        {"name": "activate_skill", "args": {"name": "alpha"}, "id": "call_alpha"}
    ]
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "activate alpha",
        config,
        approval_mode="semi-auto",
    )
    assert one_shot.exit_code == 0
    resumed = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        one_shot.session_id
    )
    assert resumed.runtime is not None
    try:
        session = resumed.runtime.sessions.get(one_shot.session_id)
        run = resumed.runtime.runs.get(one_shot.run_id)
        approval_state = json.loads(
            resumed.runtime.db.connection.execute(
                "SELECT state_json FROM checkpoints WHERE checkpoint_id = ?",
                (run.latest_checkpoint_id,),
            ).fetchone()[0]
        )["approval_state"]
        tools = resumed.runtime.tool_lines()
        tool_definitions = {
            definition.name: definition
            for definition in visible_tool_definitions(session.config_snapshot)
        }
        skills = resumed.runtime.skill_lines()
    finally:
        resumed.runtime.close()

    assert session.approval_mode == "semi-auto"
    assert approval_state["approval_mode"] == "semi-auto"
    assert approval_state["grant_count"] == 0
    assert run.active_skills[0]["name"] == "alpha"
    assert any("alpha" in line for line in skills)
    assert any("shell_exec" in line for line in tools)
    assert (
        tool_definitions["shell_exec"]
        .input_schema["properties"]["timeout_seconds"]["maximum"]
        == 22
    )
    assert any("view_image" in line and "disabled" in line for line in tools)


def test_start_resumed_repl_runtime_construction_failure_does_not_revive(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    assert one_shot.exit_code == 0

    class FailingModelFactory:
        def create(self, _config_snapshot):
            return ModelFactoryResult(
                model=None,
                error={
                    "schema_version": 1,
                    "error_class": "config_error",
                    "reason": "provider_config_invalid",
                    "message": "model construction failed",
                    "scope": "startup",
                    "recoverability": "terminal_non_resumable",
                    "metadata": {},
                    "artifact_ids": [],
                },
            )

    monkeypatch.setattr(orchestrator_module, "ModelFactory", FailingModelFactory)

    result = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        one_shot.session_id
    )

    assert result.runtime is None
    assert result.error is not None
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_row = conn.execute(
            """
            SELECT status, active_run_id, owner_pid, owner_host_id, owner_token
            FROM sessions
            WHERE session_id = ?
            """,
            (one_shot.session_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?",
            (one_shot.run_id,),
        ).fetchone()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]

    assert session_row == ("completed", None, None, None, None)
    assert run_row == ("completed",)
    assert "session_resumed" not in event_kinds
    assert "run_resumed" not in event_kinds


def test_start_resumed_repl_post_revival_failure_rolls_back_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    one_shot = RuntimeOrchestrator(workspace_root=workspace).run_one_shot(
        "hello",
        _config("one shot answer"),
    )
    assert one_shot.exit_code == 0

    def fail_after_revival(**_kwargs):
        raise StoreError(
            error_class="persistence_error",
            message="runtime construction failed after revival",
            recoverable=False,
        )

    monkeypatch.setattr(
        orchestrator_module,
        "_runtime_from_resumed_session",
        fail_after_revival,
    )

    result = RuntimeOrchestrator(workspace_root=workspace).start_resumed_repl(
        one_shot.session_id
    )

    assert result.runtime is None
    assert result.error is not None
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_row = conn.execute(
            """
            SELECT status, active_run_id, owner_pid, owner_host_id, owner_token
            FROM sessions
            WHERE session_id = ?
            """,
            (one_shot.session_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?",
            (one_shot.run_id,),
        ).fetchone()
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]

    assert session_row == ("completed", None, None, None, None)
    assert run_row == ("completed",)
    assert "session_resumed" not in event_kinds
    assert "run_resumed" not in event_kinds

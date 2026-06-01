from __future__ import annotations

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore
from debug_agent.adapters.langchain_adapter import LangChainAgentLoopAdapter
from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.policy import build_builtin_policy
from debug_agent.runtime.prompt_executor import PromptAgentExecutor
from debug_agent.skills.registry import SkillRegistry
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker
from debug_agent.tools.native import gated_user_facing_tool_definitions


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n\nDo it.\n"


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="semi-auto",
        config_snapshot={"provider": "fake", "model": "fake-model"},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    skill_store = SkillSnapshotStore(db.connection)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("alpha"), encoding="utf-8")
    (skill_dir / "references" / "guide.txt").write_text("guide", encoding="utf-8")
    snapshots = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=artifacts,
    ).snapshot(session_id=session.session_id, run_id=run.run_id)
    skill_store.save_many(snapshots)
    return {
        "workspace": workspace,
        "db": db,
        "session": session,
        "run": run,
        "runs": runs,
        "events": events,
        "artifacts": artifacts,
        "skill_store": skill_store,
    }


def _provider_message_content(message: object) -> str:
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", ""))


def test_brokered_skill_activation_and_resource_load_in_fake_harness(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    broker = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
    )
    context = {
        "workspace_root": str(runtime["workspace"]),
        "approval_mode": "semi-auto",
        "policy_facts": build_builtin_policy(runtime["workspace"]),
        "approval_grants": ApprovalGrantStore(runtime["db"].connection),
        "approval_provider": FakeApprovalProvider("denied"),
        "skill_snapshot_store": runtime["skill_store"],
        "run_store": runtime["runs"],
    }

    activation = broker.invoke(
        runtime["session"].session_id,
        runtime["run"].run_id,
        "activate_skill",
        {"name": "alpha"},
        context,
    )
    resource = broker.invoke(
        runtime["session"].session_id,
        runtime["run"].run_id,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/guide.txt"},
        context,
    )

    assert activation.status == "ok"
    assert resource.status == "ok"
    assert resource.output["content"] == "guide"
    assert resource.output["resource_kind"] == "reference"
    assert runtime["runs"].get("run_1").active_skills == [
        {
            "name": "alpha",
            "content_hash": activation.metadata["content_hash"],
            "activation_reason": "model_requested",
            "scope": "run",
        }
    ]
    assert [event.kind for event in runtime["events"].list_for_run("run_1")] == [
        "tool_call_started",
        "tool_call_completed",
        "skill_activated",
        "tool_call_started",
        "tool_call_completed",
        "skill_resource_loaded",
    ]
    runtime["db"].close()


def test_active_skill_injection_shares_adapter_model_context_frame(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    class ActivatingModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def bind_tools(self, tools):
            self.bound_tool_names = [tool.name for tool in tools]
            return self

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
                                "id": "activate_alpha",
                                "name": "activate_skill",
                                "args": {"name": "alpha"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            return type(
                "Response",
                (),
                {"content": "alpha active", "tool_calls": [], "usage": {}},
            )()

    model = ActivatingModel()
    broker = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
    )
    adapter = LangChainAgentLoopAdapter(model=model, tool_broker=broker)
    executor = PromptAgentExecutor(
        event_writer=runtime["events"],
        checkpoint_store=CheckpointStore(runtime["db"].connection),
        artifact_store=runtime["artifacts"],
        adapter=adapter,
        tool_definitions=gated_user_facing_tool_definitions(),
        system_prompt="system",
        skill_snapshot_store=runtime["skill_store"],
        todo_plan_store=TodoPlanStore(runtime["db"].connection),
        run_store=runtime["runs"],
    )

    result = executor.run_turn(
        session=runtime["session"],
        run=runtime["run"],
        user_input="activate alpha",
        workspace_root=str(runtime["workspace"]),
    )

    assert result.status == "completed"
    assert "activate_skill" in model.bound_tool_names
    second_call_messages = [
        _provider_message_content(message) for message in model.messages_by_call[1]
    ]
    assert any(
        "Skill activated: alpha" in message for message in second_call_messages
    )
    second_call_text = "\n".join(
        second_call_messages
    )
    assert "[Runtime supplied active skill context]" in second_call_text
    assert "skill_id: alpha" in second_call_text
    assert "Do it." in second_call_text
    durable_text = runtime["db"].connection.execute(
        "SELECT active_skills_json FROM runs WHERE run_id = 'run_1'"
    ).fetchone()[0]
    assert "Do it." not in durable_text
    runtime["db"].close()


def test_tool_loop_context_refresh_preserves_prior_skill_activation_result(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    class ActivatingThenLoadingModel:
        def __init__(self) -> None:
            self.calls = 0
            self.messages_by_call = []

        def bind_tools(self, tools):
            self.bound_tool_names = [tool.name for tool in tools]
            return self

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
                                "id": "activate_alpha",
                                "name": "activate_skill",
                                "args": {"name": "alpha"},
                            }
                        ],
                        "usage": {},
                    },
                )()
            if self.calls == 2:
                return type(
                    "Response",
                    (),
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "load_alpha_guide",
                                "name": "load_skill_resource",
                                "args": {
                                    "skill_name": "alpha",
                                    "path": "references/guide.txt",
                                },
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

    model = ActivatingThenLoadingModel()
    broker = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
    )
    adapter = LangChainAgentLoopAdapter(model=model, tool_broker=broker)
    executor = PromptAgentExecutor(
        event_writer=runtime["events"],
        checkpoint_store=CheckpointStore(runtime["db"].connection),
        artifact_store=runtime["artifacts"],
        adapter=adapter,
        tool_definitions=gated_user_facing_tool_definitions(),
        system_prompt="system",
        skill_snapshot_store=runtime["skill_store"],
        todo_plan_store=TodoPlanStore(runtime["db"].connection),
        run_store=runtime["runs"],
    )

    result = executor.run_turn(
        session=runtime["session"],
        run=runtime["run"],
        user_input="activate alpha and read guide",
        workspace_root=str(runtime["workspace"]),
    )

    assert result.status == "completed"
    third_call_messages = [
        _provider_message_content(message) for message in model.messages_by_call[2]
    ]
    third_call_text = "\n".join(third_call_messages)
    assert "Skill activated: alpha" in third_call_text
    assert "guide" in third_call_text
    assert "[Runtime supplied active skill context]" in third_call_text
    runtime["db"].close()


def test_omitted_tool_output_remains_recoverable_from_artifact(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    full_output = "full recoverable tool output\n" * 900
    artifact = runtime["artifacts"].write_text(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        filename="tool-output.txt",
        content=full_output,
        metadata={"source": "integration-test"},
        artifact_id="art_tool_full",
    )

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

    session = type(runtime["session"])(
        **{
            **runtime["session"].to_dict(),
            "config_snapshot": {
                **runtime["session"].config_snapshot,
                "context": {
                        "window_tokens": 500,
                    "omit_old_tool_results_at_ratio": 0.1,
                    "retain_recent_model_calls": 1,
                },
            },
        }
    )
    executor = PromptAgentExecutor(
        event_writer=runtime["events"],
        checkpoint_store=CheckpointStore(runtime["db"].connection),
        artifact_store=runtime["artifacts"],
        adapter=RecordingAdapter(),
        tool_definitions=[],
        system_prompt="system",
        skill_snapshot_store=runtime["skill_store"],
        todo_plan_store=TodoPlanStore(runtime["db"].connection),
        run_store=runtime["runs"],
    )

    result = executor.run_turn(
        session=session,
        run=runtime["run"],
        user_input="continue",
        workspace_root=str(runtime["workspace"]),
        conversation=[
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
                "content": "[Tool output stored as artifact: art_tool_full]",
                "artifact_refs": [artifact.artifact_id],
            },
            {
                "seq": 3,
                "role": "assistant",
                "kind": "assistant_output",
                "turn_id": "turn-2",
                "model_call_id": "call-2",
                "content": "Consumed tool output.",
                "metadata": {"consumed_model_call_ids": ["call-1"]},
            },
        ],
    )

    marker = "[Earlier tool result omitted for brevity. See artifact references or trace for full details.]"
    sent_segments = executor.adapter.request.model_context_frame.ordered_message_segments()
    sent_text = "\n".join(str(segment.content) for segment in sent_segments)
    assert result.status == "completed"
    assert marker in sent_text
    omitted_segment = next(segment for segment in sent_segments if segment.content == marker)
    assert omitted_segment.artifact_refs == ["art_tool_full"]
    assert runtime["artifacts"].resolve_path("art_tool_full").read_text(
        encoding="utf-8"
    ) == full_output
    runtime["db"].close()

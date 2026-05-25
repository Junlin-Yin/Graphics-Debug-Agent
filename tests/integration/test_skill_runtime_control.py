from __future__ import annotations

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
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


def test_brokered_skill_activation_and_reference_load_in_fake_harness(tmp_path) -> None:
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
    reference = broker.invoke(
        runtime["session"].session_id,
        runtime["run"].run_id,
        "load_skill_ref_file",
        {"skill_name": "alpha", "path": "references/guide.txt"},
        context,
    )

    assert activation.status == "ok"
    assert reference.status == "ok"
    assert reference.output["content"] == "guide"
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
        "tool_call_started",
        "tool_call_completed",
    ]
    runtime["db"].close()


def test_real_provider_tool_surface_remains_gated_until_model_context_frame(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    class RecordingAdapter:
        def __init__(self) -> None:
            self.tools = None

        def run(self, request, context):
            self.tools = request.tools
            return AgentRunResult(
                status="completed",
                assistant_output="ok",
                tool_results=[],
                usage={},
                error=None,
                metadata={},
            )

    adapter = RecordingAdapter()
    executor = PromptAgentExecutor(
        event_writer=runtime["events"],
        checkpoint_store=CheckpointStore(runtime["db"].connection),
        artifact_store=runtime["artifacts"],
        adapter=adapter,
        tool_definitions=gated_user_facing_tool_definitions(),
        system_prompt="system",
    )

    result = executor.run_turn(
        session=runtime["session"],
        run=runtime["run"],
        user_input="activate alpha",
        workspace_root=str(runtime["workspace"]),
    )

    assert result.status == "completed"
    assert adapter.tools == []
    runtime["db"].close()

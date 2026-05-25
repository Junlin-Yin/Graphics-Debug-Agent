from __future__ import annotations

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.policy import ShellPolicy, build_builtin_policy
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker
from debug_agent.tools.shell import FakeShellRunner


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    facts = build_builtin_policy(workspace)
    facts.user_shell = ShellPolicy(deny=[("git",)])
    return {
        "workspace": workspace,
        "db": db,
        "broker": ToolBroker(event_writer=events, artifact_store=artifacts),
        "session": session,
        "run": run,
        "context": {
            "workspace_root": str(workspace),
            "approval_mode": "yolo",
            "policy_facts": facts,
            "approval_grants": ApprovalGrantStore(db.connection),
            "approval_provider": FakeApprovalProvider("denied"),
            "shell_runner": FakeShellRunner(),
        },
    }


def _invoke(runtime, argv):
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="shell_exec",
        arguments={"argv": argv},
        context=runtime["context"],
    )


def test_shell_deny_git_blocks_direct_path_windows_suffix_and_env_wrapper(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    direct = _invoke(runtime, ["git", "status"])
    path_qualified = _invoke(runtime, ["/usr/bin/git", "status"])
    windows_suffix = _invoke(runtime, ["git.exe", "status"])
    transparent_wrapper = _invoke(runtime, ["env", "FOO=1", "git.cmd", "status"])

    assert direct.status == "denied"
    assert path_qualified.status == "denied"
    assert windows_suffix.status == "denied"
    assert transparent_wrapper.status == "denied"
    assert direct.error["message"] == "Command denied by user shell policy."
    assert path_qualified.error["message"] == "Command denied by user shell policy."
    assert windows_suffix.error["message"] == "Command denied by user shell policy."
    assert transparent_wrapper.error["message"] == "Command denied by user shell policy."
    runtime["db"].close()

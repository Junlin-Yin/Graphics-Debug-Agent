from __future__ import annotations

from concurrent.futures import TimeoutError
from pathlib import Path

import pytest

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.policy import (
    PathPolicyEntry,
    PermissionEvaluator,
    ShellPolicy,
    build_builtin_policy,
)
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker
from debug_agent.tools.shell import FakeShellRunner, ShellTimeout, tool_definitions


def _runtime(
    tmp_path,
    *,
    approval_mode: str = "semi-auto",
    default_timeout: int | None = 300,
    policy_facts=None,
    runner: FakeShellRunner | None = None,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode=approval_mode,
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    context = {
        "workspace_root": str(workspace),
        "approval_mode": approval_mode,
        "policy_facts": policy_facts or build_builtin_policy(workspace),
        "approval_grants": ApprovalGrantStore(db.connection),
        "approval_provider": FakeApprovalProvider("denied"),
        "shell_runner": runner or FakeShellRunner(),
    }
    if default_timeout is not None:
        context["frozen_config"] = {
            "execution": {"default_shell_timeout_seconds": default_timeout}
        }
    return {
        "workspace": workspace,
        "db": db,
        "broker": broker,
        "session": session,
        "run": run,
        "events": events,
        "artifacts": artifacts,
        "context": context,
    }


def _invoke(runtime, arguments, **context):
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="shell_exec",
        arguments=arguments,
        context={**runtime["context"], **context},
    )


def _event_kinds(runtime) -> list[str]:
    return [event.kind for event in runtime["events"].list_for_run("run_1")]


def test_shell_tool_definition_schema_is_structured_argv_only() -> None:
    definitions = {definition.name: definition for definition in tool_definitions()}

    definition = definitions["shell_exec"]
    assert definition.category == "shell"
    assert definition.risk_level == "execute"
    assert definition.access == ["execute"]
    assert definition.input_schema == {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1},
        },
        "required": ["argv"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": []},
        {"argv": ["echo", 1]},
        {"argv": "echo hi"},
        {"argv": ["echo"], "timeout_seconds": 0},
        {"argv": ["echo"], "extra": True},
        {"command": "echo hi"},
        "echo hi",
    ],
)
def test_shell_schema_rejects_raw_strings_empty_argv_and_unknown_fields(
    tmp_path, arguments
) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, arguments)

    assert result.status == "denied"
    assert result.error["error_class"] == "user_error"
    assert _event_kinds(runtime) == ["tool_call_denied"]
    runtime["db"].close()


def test_shell_resolves_cwd_timeout_and_runs_fake_runner(tmp_path) -> None:
    runner = FakeShellRunner(stdout="out", stderr="err", returncode=7)
    runtime = _runtime(tmp_path, default_timeout=10, runner=runner)

    result = _invoke(runtime, {"argv": ["echo", "ok"], "timeout_seconds": 99})

    assert result.status == "ok"
    assert result.output == {"stdout": "out", "stderr": "err", "returncode": 7}
    assert result.metadata["effective_timeout_seconds"] == 10
    assert runner.calls == [
        {
            "argv": ["echo", "ok"],
            "cwd": runtime["workspace"],
            "timeout_seconds": 10,
        }
    ]
    assert _event_kinds(runtime) == ["tool_call_started", "tool_call_completed"]
    runtime["db"].close()


@pytest.mark.parametrize(
    ("provided_cwd", "policy_marker"),
    [
        (r"C:\Users\me\repo", "__debug_agent_windows_drive__"),
        (r"\\server\share\repo", "__debug_agent_windows_unc__"),
    ],
)
def test_shell_preserves_windows_cwd_for_runner_while_using_policy_facts(
    tmp_path, provided_cwd, policy_marker
) -> None:
    runner = FakeShellRunner(stdout="ok")
    runtime = _runtime(
        tmp_path,
        approval_mode="normal",
        runner=runner,
    )
    approval_provider = FakeApprovalProvider("approved_once")

    result = _invoke(
        runtime,
        {"argv": ["echo", "ok"], "cwd": provided_cwd},
        approval_provider=approval_provider,
    )

    assert result.status == "ok"
    assert str(runner.calls[0]["cwd"]) == provided_cwd

    events = runtime["events"].list_for_run("run_1")
    started = next(event for event in events if event.kind == "tool_call_started")
    classified_cwd = started.payload["arguments"]["cwd"]
    assert policy_marker in classified_cwd
    assert classified_cwd != str(runner.calls[0]["cwd"])
    assert approval_provider.requests
    scope_signature = approval_provider.requests[0][1]["scope_signature"]
    assert policy_marker in scope_signature
    runtime["db"].close()


def test_shell_timeout_returns_tool_result_through_broker(tmp_path) -> None:
    runner = FakeShellRunner(exc=ShellTimeout("timed out"))
    runtime = _runtime(tmp_path, default_timeout=5, runner=runner)

    result = _invoke(runtime, {"argv": ["sleep", "10"]})

    assert result.status == "timeout"
    assert result.error["error_class"] == "timeout"
    assert result.metadata["effective_timeout_seconds"] == 5
    assert _event_kinds(runtime) == ["tool_call_started", "tool_call_failed"]
    runtime["db"].close()


def test_shell_outer_broker_timeout_uses_effective_shell_timeout(tmp_path) -> None:
    class RecordingFuture:
        def __init__(self, output):
            self.output = output
            self.timeout_seen = None

        def result(self, *, timeout=None):
            self.timeout_seen = timeout
            if timeout < 300:
                raise TimeoutError
            return self.output

        def cancel(self):
            return False

    class RecordingExecutor:
        last_future = None

        def __init__(self, *args, **kwargs):
            pass

        def submit(self, fn, *args, **kwargs):
            output = fn(*args, **kwargs)
            self.__class__.last_future = RecordingFuture(output)
            return self.__class__.last_future

        def shutdown(self, *, wait=True, cancel_futures=False):
            pass

    runner = FakeShellRunner(stdout="ok")
    runtime = _runtime(tmp_path, default_timeout=300, runner=runner)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("debug_agent.tools.broker.ThreadPoolExecutor", RecordingExecutor)
        result = _invoke(runtime, {"argv": ["echo", "ok"]}, timeout_seconds=1)

    assert result.status == "ok"
    assert result.metadata["effective_timeout_seconds"] == 300
    assert runner.calls[0]["timeout_seconds"] == 300
    assert RecordingExecutor.last_future.timeout_seen == 300
    runtime["db"].close()


def test_shell_policy_allow_deny_builtin_and_allowlist_miss(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    facts = runtime["context"]["policy_facts"]
    facts.user_shell = ShellPolicy(allow=[("uv",)], deny=[("git",)])

    allowed = _invoke(runtime, {"argv": ["uv", "run", "pytest"]})
    user_denied = _invoke(runtime, {"argv": ["git", "status"]})
    builtin_denied = _invoke(runtime, {"argv": ["rm", "-rf", "target"]})
    allow_miss = _invoke(runtime, {"argv": ["python", "-m", "pytest"]})

    assert allowed.status == "ok"
    assert user_denied.status == "denied"
    assert user_denied.error["message"] == "Command denied by user shell policy."
    assert builtin_denied.status == "denied"
    assert builtin_denied.error["message"] == "Command denied by builtin shell policy."
    assert allow_miss.status == "denied"
    assert allow_miss.error["message"] == "Command does not match shell allowlist."
    runtime["db"].close()


def test_empty_shell_allowlist_defaults_to_allowed_after_approval_policy(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")

    result = _invoke(runtime, {"argv": ["python", "-m", "pytest"]})

    assert result.status == "ok"
    runtime["db"].close()


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/git.exe", "status"],
        ["env", "FOO=1", "git.cmd", "status"],
    ],
)
def test_shell_normalizes_path_qualified_windows_suffix_and_env_wrapped_denies(
    tmp_path, argv
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    runtime["context"]["policy_facts"].user_shell = ShellPolicy(deny=[("git",)])

    result = _invoke(runtime, {"argv": argv})

    assert result.status == "denied"
    assert result.error["message"] == "Command denied by user shell policy."
    runtime["db"].close()


@pytest.mark.parametrize(
    "argv",
    [
        ["npm", "run", "git-status"],
        ["make", "git-status"],
        ["uv", "run", "git-status"],
        ["python", "scripts/run_git.py"],
        ["./scripts/run_git"],
    ],
)
def test_shell_documents_opaque_wrapper_behavior(tmp_path, argv) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    runtime["context"]["policy_facts"].user_shell = ShellPolicy(deny=[("git",)])
    (runtime["workspace"] / "scripts").mkdir()

    result = _invoke(runtime, {"argv": argv})

    assert result.status == "ok"
    runtime["db"].close()


@pytest.mark.parametrize(
    "token",
    [
        "src/file.py",
        "../outside/file.py",
        "/tmp/file",
        r"C:\\tmp\\file",
        r"\\\\server\\share\\file",
    ],
)
def test_bare_path_like_argv_tokens_are_classified_for_path_policy(
    tmp_path, token
) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")

    result = _invoke(runtime, {"argv": ["python", token]})

    if token == "src/file.py":
        assert result.status == "ok"
    else:
        assert result.status == "denied"
        assert result.error["message"] == "Approval denied."
    runtime["db"].close()


def test_shell_path_policy_denies_cwd_executable_and_classified_paths(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    (workspace / "build").mkdir()
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "tool").write_text("", encoding="utf-8")

    cwd_denied = _invoke(runtime, {"argv": ["echo"], "cwd": "build"})
    exe_denied = _invoke(runtime, {"argv": ["./build/tool"]})
    arg_denied = _invoke(runtime, {"argv": ["python", "--config", "build/config.toml"]})

    assert cwd_denied.status == "denied"
    assert exe_denied.status == "denied"
    assert arg_denied.status == "denied"
    runtime["db"].close()


def test_untrusted_shell_argv_path_requires_approval_under_semi_auto(tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="semi-auto")

    denied = _invoke(runtime, {"argv": ["python", str(outside)]})
    approved = _invoke(
        runtime,
        {"argv": ["python", str(outside)]},
        approval_provider=FakeApprovalProvider("approved_once"),
    )

    assert denied.status == "denied"
    assert denied.error["message"] == "Approval denied."
    assert approved.status == "ok"
    runtime["db"].close()


def test_shell_large_stdout_stderr_are_artifacted_separately(tmp_path) -> None:
    stdout = "o" * (16 * 1024 + 1)
    stderr = "e" * (16 * 1024 + 1)
    runtime = _runtime(
        tmp_path,
        approval_mode="semi-auto",
        runner=FakeShellRunner(stdout=stdout, stderr=stderr),
    )

    result = _invoke(runtime, {"argv": ["echo", "large"]})

    assert result.status == "ok"
    assert result.output == {"stdout": None, "stderr": None, "returncode": 0}
    assert len(result.artifacts) == 2
    artifact_texts = [
        runtime["artifacts"].resolve_path(artifact_id).read_text(encoding="utf-8")
        for artifact_id in result.artifacts
    ]
    assert sorted(artifact_texts) == [stderr, stdout]
    runtime["db"].close()

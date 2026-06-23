from __future__ import annotations

import threading
import time
import io
from concurrent.futures import TimeoutError as FutureTimeoutError
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
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
    policy_facts_to_snapshot,
)
from debug_agent.tools.broker import (
    FakeApprovalProvider,
    NonInteractiveApprovalProvider,
    ToolBroker,
    _normalized_error,
)
import debug_agent.tools.native as native_tools
from debug_agent.tools.native import NativeHandlerResult
from debug_agent.tools.shell import FakeShellRunner


def _rg_json_match(path: Path, line_number: int, line: str) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": str(path)},
                "lines": {"text": line},
                "line_number": line_number,
            },
        }
    )


def test_broker_normalized_error_maps_legacy_classes_only_as_compatibility() -> None:
    legacy_policy = _normalized_error(
        error_class="policy_denied",
        reason=None,
        message="Legacy policy denial.",
        scope="tool",
        metadata={},
    )
    legacy_internal = _normalized_error(
        error_class="internal_error",
        reason=None,
        message="Legacy internal error.",
        scope="tool",
        metadata={},
    )
    timeout_status = _normalized_error(
        error_class="tool_error",
        reason="tool_execution_timeout",
        message="Timed out.",
        scope="tool",
        metadata={"status": "timeout"},
    )

    assert legacy_policy.error_class == "policy_error"
    assert legacy_policy.reason == "approval_denied"
    assert legacy_internal.error_class == "runtime_error"
    assert legacy_internal.reason == "internal_invariant_failed"
    assert timeout_status.error_class == "tool_error"
    assert timeout_status.reason == "tool_execution_timeout"


def _install_basic_rg_stub(monkeypatch) -> None:
    def fake_run(argv, **_kwargs):
        if "--version" in argv:
            return SimpleNamespace(returncode=0, stdout="ripgrep 14\n", stderr="")
        if str(argv[-1]).endswith("regex-check.txt"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")

    def fake_matches(_rg, common_args, candidate, _timeout_seconds):
        pattern = common_args[common_args.index("--regexp") + 1]
        ignore_case = "-i" in common_args
        candidate = Path(candidate)
        try:
            for line_number, line in enumerate(candidate.read_text(encoding="utf-8").splitlines(True), start=1):
                haystack = line.casefold() if ignore_case else line
                needle = pattern.casefold() if ignore_case else pattern
                if needle in haystack:
                    preview = line.rstrip("\r\n")
                    yield {
                        "path": str(candidate),
                        "line_number": line_number,
                        "line": preview,
                        "is_context": False,
                        "line_truncated": False,
                    }
        except UnicodeDecodeError:
            return

    monkeypatch.setattr(
        native_tools,
        "shutil",
        SimpleNamespace(which=lambda name: "/usr/bin/rg"),
        raising=False,
    )
    monkeypatch.setattr(
        native_tools,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=TimeoutError),
        raising=False,
    )
    monkeypatch.setattr(native_tools, "_iter_ripgrep_matches", fake_matches)


class _FakeRgProcess:
    def __init__(self, *, stdout: str, returncode: int) -> None:
        self.stdout = io.BytesIO(stdout.encode("utf-8"))
        self.stderr = io.BytesIO(b"")
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _install_popen_rg_stub(monkeypatch, *, calls, stdout_for_candidate) -> None:
    def fake_run(argv, **kwargs):
        calls.append({"kind": "run", "argv": list(argv), **kwargs})
        if "--version" in argv:
            return SimpleNamespace(returncode=0, stdout="ripgrep 14\n", stderr="")
        if str(argv[-1]).endswith("regex-check.txt"):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    def fake_popen(argv, **kwargs):
        calls.append({"kind": "popen", "argv": list(argv), **kwargs})
        stdout = stdout_for_candidate(Path(argv[-1]))
        return _FakeRgProcess(stdout=stdout, returncode=0 if stdout else 1)

    monkeypatch.setattr(
        native_tools,
        "shutil",
        SimpleNamespace(which=lambda name: "/usr/bin/rg"),
        raising=False,
    )
    monkeypatch.setattr(
        native_tools,
        "subprocess",
        SimpleNamespace(
            run=fake_run,
            Popen=fake_popen,
            PIPE=object(),
            TimeoutExpired=TimeoutError,
        ),
        raising=False,
    )


class _RecordingTimeoutRouter:
    def __init__(self) -> None:
        self.effective_timeout_seconds = None

    def route(self, context, arguments):
        self.effective_timeout_seconds = context.effective_timeout_seconds
        return "ok"


class _NativeResultRouter:
    def __init__(self, result):
        self.result = result

    def route(self, context, arguments):
        return self.result


class _FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def monotonic(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class _StartedEventAdvancingWriter:
    def __init__(self, delegate, clock: _FakeClock, seconds: float) -> None:
        self.delegate = delegate
        self.clock = clock
        self.seconds = seconds

    def append(self, event):
        result = self.delegate.append(event)
        if event.kind == "tool_call_started":
            self.clock.advance(self.seconds)
        return result

    def list_for_run(self, run_id):
        return self.delegate.list_for_run(run_id)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class _FailingArtifactWriteStore:
    def __init__(self, delegate):
        self.delegate = delegate
        self.sessions_root = delegate.sessions_root

    def write_text(self, **_kwargs):
        raise OSError("artifact write failed")

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class _FailingArtifactRegistrationStore:
    def __init__(self, delegate):
        self.delegate = delegate
        self.sessions_root = delegate.sessions_root

    def write_text(self, **kwargs):
        artifact_id = kwargs.get("artifact_id")
        if isinstance(artifact_id, str):
            content = kwargs.get("content", "")
            session_id = kwargs.get("session_id", "")
            filename = kwargs.get("filename", "orphan.txt")
            path = self.sessions_root / session_id / "artifacts" / Path(filename).name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
        raise RuntimeError("artifact registration failed")

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class _SlowArtifactStore:
    def __init__(self, delegate, delay_seconds: float) -> None:
        self.delegate = delegate
        self.delay_seconds = delay_seconds
        self.sessions_root = delegate.sessions_root

    def write_text(self, **kwargs):
        time.sleep(self.delay_seconds)
        return self.delegate.write_text(**kwargs)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class _DeadlineCrossingArtifactStore(ArtifactStore):
    def __init__(
        self,
        connection,
        sessions_root,
        *,
        inserted: threading.Event,
        release_commit: threading.Event,
        cleaned_up: threading.Event,
    ) -> None:
        super().__init__(connection, sessions_root)
        self.inserted = inserted
        self.release_commit = release_commit
        self.cleaned_up = cleaned_up
        self.cleanup_exception: BaseException | None = None

    def _insert(self, **kwargs):
        artifact = super()._insert(**kwargs)
        self.inserted.set()
        self.release_commit.wait(timeout=5)
        return artifact

    def _delete_accepted_artifact(self, *, artifact_id: str, relative_path: str) -> None:
        try:
            super()._delete_accepted_artifact(
                artifact_id=artifact_id,
                relative_path=relative_path,
            )
        except BaseException as exc:
            self.cleanup_exception = exc
            raise
        finally:
            self.cleaned_up.set()


class _CacheObservingSlowRouter:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release

    def route(self, context, arguments):
        context.record_file_metadata(arguments["path"], source_tool="read_file")
        self.entered.set()
        self.release.wait(timeout=1)
        return NativeHandlerResult(
            status="ok",
            output={"path": arguments["path"], "content": "ok"},
        )


class _CacheRecordingAfterTimeoutRouter:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release

    def route(self, context, arguments):
        self.entered.set()
        self.release.wait(timeout=1)
        context.record_file_metadata(arguments["path"], source_tool="read_file")
        return NativeHandlerResult(
            status="ok",
            output={"path": arguments["path"], "content": "late"},
        )


def _runtime(tmp_path, *, approval_mode: str = "normal", policy_facts=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="normal",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    events = EventWriter(db.connection, db.path.parent)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    return {
        "workspace": workspace,
        "db": db,
        "broker": broker,
        "session": session,
        "run": run,
        "events": events,
        "artifacts": artifacts,
        "approval_mode": approval_mode,
        "policy_facts": policy_facts or build_builtin_policy(workspace),
    }


def _invoke(runtime, tool_name, arguments, **context):
    merged_context = {
        "workspace_root": str(runtime["workspace"]),
        "approval_mode": runtime["approval_mode"],
        "policy_facts": runtime["policy_facts"],
        "approval_grants": ApprovalGrantStore(runtime["db"].connection),
        "approval_provider": FakeApprovalProvider("denied"),
        **context,
    }
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name=tool_name,
        arguments=arguments,
        context=merged_context,
    )


def _event_kinds(runtime) -> list[str]:
    return [event.kind for event in runtime["events"].list_for_run("run_1")]


def test_broker_keyboard_interrupt_shuts_down_executor(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    submitted = {"called": False}
    shutdown_calls: list[dict] = []

    class InterruptingFuture:
        def result(self, timeout=None):
            raise KeyboardInterrupt

    class InterruptingExecutor:
        def __init__(self, *, max_workers):
            assert max_workers == 1

        def submit(self, fn, *args):
            submitted["called"] = True
            return InterruptingFuture()

        def shutdown(self, *, wait=True, cancel_futures=False):
            shutdown_calls.append(
                {"wait": wait, "cancel_futures": cancel_futures}
            )

    monkeypatch.setattr(
        "debug_agent.tools.broker.ThreadPoolExecutor",
        InterruptingExecutor,
    )

    with pytest.raises(KeyboardInterrupt):
        _invoke(runtime, "read_file", {"path": "notes.txt"})

    assert submitted == {"called": True}
    assert shutdown_calls == [{"wait": False, "cancel_futures": True}]
    runtime["db"].close()


def test_broker_uses_frozen_generic_tool_timeout_for_unspecified_native_tool(
    tmp_path, monkeypatch
) -> None:
    class RecordingFuture:
        timeout_seen = None

        def __init__(self, result):
            self._result = result

        def result(self, timeout=None):
            self.__class__.timeout_seen = timeout
            return self._result

        def cancel(self):
            return False

    class RecordingExecutor:
        def __init__(self, *, max_workers):
            assert max_workers == 1

        def submit(self, fn, *args):
            return RecordingFuture(fn(*args))

        def shutdown(self, *, wait=True, cancel_futures=False):
            pass

    runtime = _runtime(tmp_path)
    router = _RecordingTimeoutRouter()
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
        router=router,
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("debug_agent.tools.broker.ThreadPoolExecutor", RecordingExecutor)
        result = _invoke(
            runtime,
            "read_file",
            {"path": "notes.txt"},
            approval_mode="yolo",
            frozen_config={"execution": {"default_tool_timeout_seconds": 7}},
        )

    assert result.status == "ok"
    assert router.effective_timeout_seconds == 7
    assert RecordingFuture.timeout_seen == 7
    runtime["db"].close()


def test_schema_validation_rejects_unknown_fields_and_invalid_limits(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    (runtime["workspace"] / "notes.txt").write_text("hello", encoding="utf-8")

    extra = _invoke(runtime, "read_file", {"path": "notes.txt", "extra": True})
    zero = _invoke(runtime, "read_file", {"path": "notes.txt", "limit": 0})
    too_many_lines = _invoke(runtime, "read_file", {"path": "notes.txt", "limit": 2001})
    too_many_entries = _invoke(runtime, "list_dir", {"path": ".", "limit": 1001})
    missing = _invoke(runtime, "search_text", {"path": "."})
    old_query = _invoke(runtime, "search_text", {"query": "hello"})
    boolean_limit = _invoke(runtime, "read_file", {"path": "notes.txt", "limit": True})

    assert extra.status == "error"
    assert zero.status == "error"
    assert too_many_lines.status == "error"
    assert too_many_entries.status == "error"
    assert missing.status == "error"
    assert old_query.status == "error"
    assert boolean_limit.status == "error"
    assert extra.error["error_class"] == "tool_error"
    assert extra.error["reason"] == "tool_schema_invalid"
    assert zero.error["message"] == "limit must be a positive integer."
    assert too_many_lines.error["reason"] == "tool_schema_invalid"
    assert too_many_entries.error["reason"] == "tool_schema_invalid"
    assert old_query.error["message"] == "Unknown field: query"
    assert boolean_limit.error["message"] == "limit must be an integer."
    assert _event_kinds(runtime) == [
        "tool_call_failed",
        "tool_call_failed",
        "tool_call_failed",
        "tool_call_failed",
        "tool_call_failed",
        "tool_call_failed",
        "tool_call_failed",
    ]
    runtime["db"].close()


def test_schema_defaults_and_nested_validation_are_injected_before_handler_and_audit(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    (workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    result = _invoke(runtime, "read_file", {"path": "notes.txt"})

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ][0]
    assert result.status == "ok"
    assert completed["arguments"]["offset"] == 0
    assert completed["arguments"]["limit"] == 2000


def test_present_path_strings_are_trimmed_and_empty_paths_are_schema_invalid(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")

    trimmed = _invoke(runtime, "read_file", {"path": " notes.txt "})
    empty = _invoke(runtime, "read_file", {"path": "   "})
    shell_empty = _invoke(runtime, "shell_exec", {"argv": ["pwd"], "cwd": "   "})

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ][0]
    assert trimmed.status == "ok"
    assert completed["arguments"]["path"] == str((workspace / "notes.txt").resolve())
    assert empty.status == "error"
    assert empty.error["reason"] == "tool_schema_invalid"
    assert shell_empty.status == "error"
    assert shell_empty.error["reason"] == "tool_schema_invalid"


def test_read_file_auto_allows_trusted_workspace_under_normal(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    (runtime["workspace"] / "notes.txt").write_text(
        "a\nb\nc\n", encoding="utf-8", newline="\n"
    )

    result = _invoke(runtime, "read_file", {"path": "notes.txt", "limit": 2})

    assert result.status == "ok"
    assert result.output["path"] == str((runtime["workspace"] / "notes.txt").resolve())
    assert result.output["content"] == "a\nb\n"
    assert result.output["offset"] == 0
    assert result.output["limit"] == 2
    assert result.output["total_returned"] == 2
    assert result.output["truncated"] is True
    assert result.output["next_offset"] == 2
    assert result.output["sha256"] == sha256(b"a\nb\nc\n").hexdigest()
    assert result.output["bytes"] == 6
    assert _event_kinds(runtime) == ["tool_call_started", "tool_call_completed"]
    runtime["db"].close()


def test_read_outside_trusted_workspace_requires_approval_under_normal(tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="normal")

    denied = _invoke(runtime, "read_file", {"path": str(outside)})
    approved = _invoke(
        runtime,
        "read_file",
        {"path": str(outside)},
        approval_provider=FakeApprovalProvider("approved_once"),
    )

    assert denied.status == "denied"
    assert denied.error["message"] == "Approval denied."
    assert approved.status == "ok"
    assert approved.output["content"] == "secret"
    assert approved.output["path"] == str(outside.resolve())
    runtime["db"].close()


def test_interactive_approval_writes_requested_and_decision_audit_events(
    tmp_path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="normal")

    result = _invoke(
        runtime,
        "read_file",
        {"path": str(outside)},
        approval_provider=FakeApprovalProvider("approved_once"),
    )

    events = runtime["events"].list_for_run("run_1")
    assert result.status == "ok"
    assert [event.kind for event in events] == [
        "approval_requested",
        "approval_decision_recorded",
        "tool_call_started",
        "tool_call_completed",
    ]
    assert events[0].payload["tool_name"] == "read_file"
    assert events[1].payload["decision"] == "approved_once"
    assert events[1].payload["grant_scope"] == "once"
    runtime["db"].close()


def test_interactive_approval_prompt_renders_required_facts_and_denial_aborts_turn(
    tmp_path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="normal")
    provider = FakeApprovalProvider("denied")

    result = _invoke(
        runtime,
        "read_file",
        {"path": str(outside)},
        approval_provider=provider,
    )
    rows = runtime["db"].connection.execute(
        """
        SELECT decision, grant_scope, approval_request
        FROM approval_grants
        ORDER BY rowid
        """
    ).fetchall()

    assert result.status == "denied"
    assert result.error["error_class"] == "policy_error"
    assert result.error["reason"] == "approval_denied"
    assert result.metadata["turn_aborted"] is True
    assert provider.requests
    request_text, facts = provider.requests[0]
    assert request_text == (
        "=== Approval Request ===\n"
        "Tool: read_file\n"
        f"Target: {outside.resolve()}\n"
        "\n"
        "Allow? [y]once, [a] session, [n] deny"
    )
    assert "Tool: read_file" in request_text
    assert f"Target: {outside.resolve()}" in request_text
    assert "Risk:" not in request_text
    assert "Grant scope:" not in request_text
    assert facts["grant_scope"] == "once or session"
    assert rows == [("denied", "none", request_text)]
    runtime["db"].close()


def test_non_interactive_approval_required_uses_specific_policy_reason(tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="normal")

    result = _invoke(
        runtime,
        "read_file",
        {"path": str(outside)},
        approval_provider=NonInteractiveApprovalProvider(),
    )

    assert result.status == "denied"
    assert result.error["error_class"] == "policy_error"
    assert result.error["reason"] == "approval_required_non_interactive"
    assert result.error["message"] == "Interactive approval is unavailable."
    runtime["db"].close()


def test_runtime_control_invalid_activate_skill_target_is_user_error(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    skill_store = SimpleNamespace(get_skill=lambda **_kwargs: None)

    result = _invoke(
        runtime,
        "activate_skill",
        {"name": "missing"},
        skill_snapshot_store=skill_store,
    )

    assert result.status == "error"
    assert result.error["error_class"] == "user_error"
    assert result.error["reason"] == "invalid_runtime_control_target"
    runtime["db"].close()


def test_runtime_control_invalid_load_skill_resource_target_is_user_error(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    skill_store = SimpleNamespace(get_skill=lambda **_kwargs: None)

    result = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "missing", "path": "README.md"},
        skill_snapshot_store=skill_store,
        run_store=SimpleNamespace(),
    )

    assert result.status == "error"
    assert result.error["error_class"] == "user_error"
    assert result.error["reason"] == "invalid_runtime_control_target"
    runtime["db"].close()


def test_runtime_control_missing_skill_snapshot_store_is_not_user_target_error(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")

    result = _invoke(runtime, "activate_skill", {"name": "missing"})

    assert result.status == "error"
    assert result.error["error_class"] == "config_error"
    assert result.error["reason"] == "invalid_runtime_config"
    runtime["db"].close()


def test_runtime_control_missing_skill_resource_state_is_not_user_target_error(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")

    result = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "missing", "path": "README.md"},
    )

    assert result.status == "error"
    assert result.error["error_class"] == "config_error"
    assert result.error["reason"] == "invalid_runtime_config"
    runtime["db"].close()


def test_policy_auto_allow_does_not_write_approval_audit_or_grants(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    (runtime["workspace"] / "notes.txt").write_text("hello", encoding="utf-8")

    result = _invoke(runtime, "read_file", {"path": "notes.txt"})
    grant_count = runtime["db"].connection.execute(
        "SELECT COUNT(*) FROM approval_grants"
    ).fetchone()[0]

    assert result.status == "ok"
    assert grant_count == 0
    assert "approval_requested" not in _event_kinds(runtime)
    assert "approval_decision_recorded" not in _event_kinds(runtime)
    runtime["db"].close()


def test_write_approval_matrix_for_normal_and_semi_auto(tmp_path) -> None:
    normal = _runtime(tmp_path / "normal", approval_mode="normal")
    semi = _runtime(tmp_path / "semi", approval_mode="semi-auto")
    untrusted_path = tmp_path / "outside.txt"

    normal_denied = _invoke(normal, "write_file", {"path": "x.txt", "content": "x"})
    normal_approved = _invoke(
        normal,
        "write_file",
        {"path": "x.txt", "content": "x"},
        approval_provider=FakeApprovalProvider("approved_once"),
    )
    semi_trusted = _invoke(semi, "write_file", {"path": "x.txt", "content": "x"})
    semi_untrusted = _invoke(
        semi, "write_file", {"path": str(untrusted_path), "content": "x"}
    )

    assert normal_denied.status == "denied"
    assert normal_approved.status == "ok"
    assert semi_trusted.status == "ok"
    assert semi_untrusted.status == "denied"
    assert (normal["workspace"] / "x.txt").read_text(encoding="utf-8") == "x"
    assert (semi["workspace"] / "x.txt").read_text(encoding="utf-8") == "x"
    normal["db"].close()
    semi["db"].close()


def test_write_file_creates_missing_parents_only_after_authorization(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")

    result = _invoke(
        runtime,
        "write_file",
        {"path": "nested/new/file.txt", "content": "created"},
    )
    denied = _invoke(
        runtime,
        "write_file",
        {"path": "build/new/file.txt", "content": "blocked"},
    )

    assert result.status == "ok"
    assert (runtime["workspace"] / "nested/new/file.txt").read_text(
        encoding="utf-8"
    ) == "created"
    assert denied.status == "denied"
    assert not (runtime["workspace"] / "build").exists()
    runtime["db"].close()


def test_edit_file_replaces_first_exact_match_on_normalized_lf_view(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    target = runtime["workspace"] / "mixed.txt"
    target.write_bytes(b"first\r\nold\r\nsecond\r\nold\r\n")
    _invoke(runtime, "read_file", {"path": "mixed.txt"})

    result = _invoke(
        runtime,
        "edit_file",
        {"path": "mixed.txt", "old_text": "old\nsecond", "new_text": "NEW\nsecond"},
    )

    assert result.status == "ok"
    assert target.read_bytes() == b"first\r\nNEW\r\nsecond\r\nold\r\n"
    runtime["db"].close()


def test_edit_file_returns_tool_error_when_old_text_absent_or_not_found(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    (runtime["workspace"] / "notes.txt").write_text("hello", encoding="utf-8")

    absent = _invoke(
        runtime,
        "edit_file",
        {"path": "notes.txt", "old_text": "", "new_text": "x"},
    )
    missing = _invoke(
        runtime,
        "edit_file",
        {"path": "notes.txt", "old_text": "absent", "new_text": "x"},
    )

    assert absent.status == "error"
    assert missing.status == "error"
    assert absent.error["error_class"] == "tool_error"
    assert absent.error["reason"] == "tool_schema_invalid"
    assert missing.error["error_class"] == "tool_error"
    runtime["db"].close()


def test_edit_file_lf_fallback_when_no_dominant_line_ending(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    target = runtime["workspace"] / "single.txt"
    target.write_text("old", encoding="utf-8")
    _invoke(runtime, "read_file", {"path": "single.txt"})

    result = _invoke(
        runtime,
        "edit_file",
        {"path": "single.txt", "old_text": "old", "new_text": "new\nline"},
    )

    assert result.status == "ok"
    assert target.read_bytes() == b"new\nline"
    runtime["db"].close()


def test_edit_file_requires_fresh_cache_and_replace_all_for_multiple_matches(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("old\nold\n", encoding="utf-8")

    missing_cache = _invoke(
        runtime,
        "edit_file",
        {"path": "notes.txt", "old_text": "old", "new_text": "new"},
    )
    _invoke(runtime, "read_file", {"path": "notes.txt"})
    multiple = _invoke(
        runtime,
        "edit_file",
        {"path": "notes.txt", "old_text": "old", "new_text": "new"},
    )
    all_matches = _invoke(
        runtime,
        "edit_file",
        {
            "path": "notes.txt",
            "old_text": "old",
            "new_text": "new",
            "replace_all": True,
        },
    )
    assert target.read_text(encoding="utf-8") == "new\nnew\n"
    target.write_text("external\n", encoding="utf-8")
    stale = _invoke(
        runtime,
        "edit_file",
        {"path": "notes.txt", "old_text": "external", "new_text": "guarded"},
    )

    assert missing_cache.status == "error"
    assert "read_file first" in missing_cache.error["message"]
    assert multiple.status == "error"
    assert "exactly one match" in multiple.error["message"]
    assert all_matches.status == "ok"
    assert all_matches.output["replacements"] == 2
    assert all_matches.output["guard"] == {"used": True, "cache_source": "read_file"}
    assert stale.status == "error"
    assert "read_file again" in stale.error["message"]
    runtime["db"].close()


def test_write_file_overwrite_requires_cache_and_updates_guarded_cache(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    target = runtime["workspace"] / "empty.txt"
    target.write_text("", encoding="utf-8")

    missing_cache = _invoke(
        runtime,
        "write_file",
        {"path": "empty.txt", "content": "first"},
    )
    _invoke(runtime, "read_file", {"path": "empty.txt"})
    overwrite = _invoke(
        runtime,
        "write_file",
        {"path": "empty.txt", "content": "first"},
    )
    target.write_text("external", encoding="utf-8")
    stale = _invoke(
        runtime,
        "write_file",
        {"path": "empty.txt", "content": "second"},
    )

    assert missing_cache.status == "error"
    assert "read_file first" in missing_cache.error["message"]
    assert overwrite.status == "ok"
    assert overwrite.output["created"] is False
    assert overwrite.output["overwritten"] is True
    assert overwrite.output["sha256_before"] == sha256(b"").hexdigest()
    assert overwrite.output["sha256_after"] == sha256(b"first").hexdigest()
    assert overwrite.output["guard"] == {"used": True, "cache_source": "read_file"}
    assert stale.status == "error"
    assert "read_file again" in stale.error["message"]
    cache = runtime["broker"].file_metadata_cache_snapshot()
    assert cache[str(target.resolve())]["sha256"] == sha256(b"first").hexdigest()
    runtime["db"].close()


def test_write_file_parent_directory_approval_scope_and_audit(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    provider = FakeApprovalProvider("approved_once")
    grants = ApprovalGrantStore(runtime["db"].connection)

    result = _invoke(
        runtime,
        "write_file",
        {"path": "nested/new/file.txt", "content": "created"},
        approval_provider=provider,
        approval_grants=grants,
    )

    planned = [
        str((runtime["workspace"] / "nested").resolve()),
        str((runtime["workspace"] / "nested" / "new").resolve()),
    ]
    events = runtime["events"].list_for_run("run_1")
    completed = [event.payload for event in events if event.kind == "tool_call_completed"][0]
    approval_rows = runtime["db"].connection.execute(
        "SELECT scope_signature, approval_request FROM approval_grants"
    ).fetchall()

    assert result.status == "ok"
    assert result.output["created"] is True
    assert result.output["overwritten"] is False
    assert result.output["guard"] == {"used": False, "cache_source": None}
    assert provider.requests[0][1]["planned_parent_directories"] == planned
    assert "Planned parent directories:" in provider.requests[0][0]
    assert all(directory in provider.requests[0][0] for directory in planned)
    assert "planned_parents:" + ",".join(planned) in approval_rows[0][0]
    assert all(directory in approval_rows[0][1] for directory in planned)
    assert completed["arguments"]["planned_parent_directories"] == planned
    runtime["db"].close()


def test_write_file_create_new_target_race_fails_without_overwrite(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    target = runtime["workspace"] / "race.txt"
    original_open = Path.open
    armed = {"value": True}

    def racing_open(self, mode="r", *args, **kwargs):
        if self.resolve() == target.resolve() and "x" in mode and armed["value"]:
            armed["value"] = False
            target.write_text("raced", encoding="utf-8")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)

    result = _invoke(
        runtime,
        "write_file",
        {"path": "race.txt", "content": "new"},
    )

    assert result.status == "error"
    assert result.error["reason"] == "tool_execution_failed"
    assert target.read_text(encoding="utf-8") == "raced"
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_write_file_timeout_after_parent_creation_audits_side_effects_without_cache(
    tmp_path,
) -> None:
    class _TimeoutAfterFirstParentRouter:
        def route(self, context, arguments):
            parent = Path(arguments["planned_parent_directories"][0])
            parent.mkdir()
            context.record_created_directory(parent)
            raise FutureTimeoutError

    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
        timeout_seconds=0.01,
        router=_TimeoutAfterFirstParentRouter(),
    )

    result = _invoke(
        runtime,
        "write_file",
        {"path": "nested/file.txt", "content": "late"},
        timeout_seconds=1,
    )
    failed_events = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_failed"
    ]

    assert result.status == "timeout"
    assert failed_events
    failed = failed_events[-1]
    assert (runtime["workspace"] / "nested").is_dir()
    assert not (runtime["workspace"] / "nested" / "file.txt").exists()
    assert failed["side_effects"]["created_directories"] == [
        str((runtime["workspace"] / "nested").resolve())
    ]
    assert failed["file_write_completed"] is False
    assert failed["cache_updated"] is False
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_builtin_user_symlink_sessions_and_skill_source_denies(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    facts = runtime["policy_facts"]
    facts.user_path_deny.append(
        PathPolicyEntry.from_raw("deny", "secret/", workspace, facts.home)
    )
    (workspace / ".sessions" / "secret.txt").write_text("runtime", encoding="utf-8")
    (workspace / ".debug-agent" / "skills" / "s" ).mkdir(parents=True)
    (workspace / ".debug-agent" / "skills" / "s" / "SKILL.md").write_text(
        "skill", encoding="utf-8"
    )
    (workspace / "secret").mkdir()
    (workspace / "secret" / "data.txt").write_text("secret", encoding="utf-8")
    (workspace / "build").mkdir()
    (workspace / "build" / "x.txt").write_text("built", encoding="utf-8")
    (workspace / "link.txt").symlink_to(workspace / ".sessions" / "secret.txt")

    assert _invoke(runtime, "read_file", {"path": "build/x.txt"}).status == "denied"
    assert _invoke(runtime, "read_file", {"path": "secret/data.txt"}).status == "denied"
    assert _invoke(runtime, "read_file", {"path": "link.txt"}).status == "denied"
    assert _invoke(runtime, "read_file", {"path": ".sessions/secret.txt"}).status == "denied"
    assert _invoke(
        runtime,
        "read_file",
        {"path": ".debug-agent/skills/s/SKILL.md"},
    ).status == "denied"
    runtime["db"].close()


def test_artifact_ids_or_runtime_references_do_not_bypass_sessions_deny(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    artifact = runtime["artifacts"].write_text(
        session_id="sess_1",
        run_id="run_1",
        filename="secret.txt",
        content="secret",
        metadata={},
        artifact_id="art_secret",
    )
    (runtime["workspace"] / ".sessions" / "sess_1" / "artifacts").mkdir(
        parents=True,
        exist_ok=True,
    )
    runtime_reference = ".sessions/sess_1/artifacts/secret.txt"
    (runtime["workspace"] / runtime_reference).write_text("secret", encoding="utf-8")

    by_id = _invoke(runtime, "read_file", {"path": "art_secret"})
    by_artifact_store_path = _invoke(runtime, "read_file", {"path": artifact.relative_path})
    by_runtime_reference = _invoke(runtime, "read_file", {"path": runtime_reference})

    assert by_id.status == "error"
    assert by_artifact_store_path.status == "error"
    assert by_runtime_reference.status == "denied"
    runtime["db"].close()


def test_search_text_skips_denied_dirs_and_has_no_explicit_denied_dir_exception(
    tmp_path, monkeypatch
) -> None:
    _install_basic_rg_stub(monkeypatch)
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    facts = runtime["policy_facts"]
    facts.user_path_deny.append(
        PathPolicyEntry.from_raw("deny", "secret/", workspace, facts.home)
    )
    (workspace / "src").mkdir()
    (workspace / "src" / "app.txt").write_text("needle app", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("needle git", encoding="utf-8")
    (workspace / "secret").mkdir()
    (workspace / "secret" / "data.txt").write_text("needle secret", encoding="utf-8")

    default_search = _invoke(
        runtime,
        "search_text",
        {"pattern": "needle", "path": ".", "maxResults": 5},
        permission_evaluator=PermissionEvaluator(facts),
    )
    explicit_denied = _invoke(
        runtime,
        "search_text",
        {"pattern": "needle", "path": "secret"},
        permission_evaluator=PermissionEvaluator(facts),
    )

    assert default_search.status == "ok"
    assert default_search.output["matches"] == [
        {
            "path": str((workspace / "src" / "app.txt").resolve()),
            "line_number": 1,
            "line": "needle app",
            "is_context": False,
            "line_truncated": False,
        }
    ]
    assert default_search.output["skipped_files"]["denied"] == 0
    assert default_search.output["skipped_files"]["hidden"] == 0
    assert explicit_denied.status == "denied"
    runtime["db"].close()


def test_search_text_outside_workspace_returns_absolute_paths_when_allowed(
    tmp_path, monkeypatch
) -> None:
    _install_basic_rg_stub(monkeypatch)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "notes.txt"
    outside_file.write_text("needle outside", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="yolo")

    result = _invoke(
        runtime,
        "search_text",
        {"path": str(outside_dir), "pattern": "needle"},
    )

    assert result.status == "ok"
    assert result.output == {
        "matches": [
            {
                "path": str(outside_file.resolve()),
                "line_number": 1,
                "line": "needle outside",
                "is_context": False,
                "line_truncated": False,
            }
        ],
        "root": str(outside_dir.resolve()),
        "pattern": "needle",
        "output_mode": "content",
        "offset": 0,
        "maxResults": 100,
        "total_returned": 1,
        "truncated": False,
        "next_offset": None,
        "skipped_files": {"denied": 0, "hidden": 0, "decode_error": 0, "other": 0},
    }
    runtime["db"].close()


def test_search_text_missing_rg_fails_after_approval_without_cache_update(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    (runtime["workspace"] / "notes.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(
        native_tools,
        "shutil",
        SimpleNamespace(which=lambda name: None),
        raising=False,
    )

    result = _invoke(runtime, "search_text", {"pattern": "needle"})

    assert result.status == "error"
    assert result.error["reason"] == "tool_execution_failed"
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_search_text_invalid_glob_wins_before_denied_path_policy(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")

    result = _invoke(
        runtime,
        "search_text",
        {"path": ".sessions", "pattern": "needle", "glob": "a**.py"},
    )

    assert result.status == "error"
    assert result.error["reason"] == "tool_schema_invalid"
    assert _event_kinds(runtime) == ["tool_call_failed"]
    runtime["db"].close()


def test_search_text_invokes_controlled_rg_and_returns_output_modes(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    src = workspace / "src"
    src.mkdir()
    first = src / "a.py"
    second = src / "b.py"
    first.write_text("before\nneedle one\nneedle two\n", encoding="utf-8")
    second.write_text("NEEDLE upper\nneedle again\n", encoding="utf-8")
    calls = []

    def stdout_for_candidate(path: Path) -> str:
        lines = []
        if path == first:
            lines = [
                _rg_json_match(first, 2, "needle one\n"),
                _rg_json_match(first, 3, "needle two\n"),
            ]
        elif path == second:
            lines = [_rg_json_match(second, 2, "needle again\n")]
        return "\n".join(lines)

    _install_popen_rg_stub(
        monkeypatch,
        calls=calls,
        stdout_for_candidate=stdout_for_candidate,
    )

    content = _invoke(
        runtime,
        "search_text",
        {"path": "src", "pattern": "needle", "maxResults": 1, "after_context": 1},
    )
    files = _invoke(
        runtime,
        "search_text",
        {"path": "src", "pattern": "needle", "output_mode": "files_with_matches"},
    )
    counts = _invoke(
        runtime,
        "search_text",
        {"path": "src", "pattern": "needle", "output_mode": "count"},
    )

    search_calls = [call for call in calls if call["kind"] == "popen"]
    assert content.status == "ok"
    assert content.output["matches"] == [
        {
            "path": str(first.resolve()),
            "line_number": 2,
            "line": "needle one",
            "is_context": False,
            "line_truncated": False,
        },
        {
            "path": str(first.resolve()),
            "line_number": 3,
            "line": "needle two",
            "is_context": True,
            "line_truncated": False,
        },
    ]
    assert content.output["total_returned"] == 1
    assert content.output["truncated"] is True
    assert content.output["next_offset"] == 1
    assert "paths" not in content.output
    assert "counts" not in content.output
    assert files.output["paths"] == [str(first.resolve()), str(second.resolve())]
    assert "matches" not in files.output
    assert counts.output["counts"] == [
        {"path": str(first.resolve()), "count": 2},
        {"path": str(second.resolve()), "count": 1},
    ]
    assert "matches" not in counts.output
    assert all(call["argv"][0] == "/usr/bin/rg" for call in calls)
    assert all("--no-config" in call["argv"] for call in calls)
    assert all("--context" not in call["argv"] for call in search_calls)
    assert all("--type" not in call["argv"] for call in search_calls)
    assert all(call["env"].get("RIPGREP_CONFIG_PATH") is None for call in calls)
    run_calls = [call for call in calls if call["kind"] == "run"]
    assert all(call["capture_output"] and call["text"] and not call["check"] for call in run_calls)
    assert all(call["stdout"] is native_tools.subprocess.PIPE for call in search_calls)
    assert all(call["stderr"] is native_tools.subprocess.PIPE for call in search_calls)
    assert all("text" not in call and not call["shell"] for call in search_calls)
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_search_text_preserves_symlink_candidate_identity_for_all_output_modes(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    src = workspace / "src"
    src.mkdir()
    target = src / "target.txt"
    link_a = src / "link-a.txt"
    link_b = src / "link-b.txt"
    target.write_text("needle through symlink\n", encoding="utf-8")
    link_a.symlink_to(target)
    link_b.symlink_to(target)
    calls = []

    _install_popen_rg_stub(
        monkeypatch,
        calls=calls,
        stdout_for_candidate=lambda _candidate: _rg_json_match(target, 1, "needle through symlink\n"),
    )

    content = _invoke(
        runtime,
        "search_text",
        {"path": "src", "pattern": "needle", "glob": "link-*.txt"},
    )
    files = _invoke(
        runtime,
        "search_text",
        {
            "path": "src",
            "pattern": "needle",
            "glob": "link-*.txt",
            "output_mode": "files_with_matches",
        },
    )
    counts = _invoke(
        runtime,
        "search_text",
        {
            "path": "src",
            "pattern": "needle",
            "glob": "link-*.txt",
            "output_mode": "count",
        },
    )

    expected = sorted([str(link_a.absolute()), str(link_b.absolute())])
    assert content.status == "ok"
    assert sorted(match["path"] for match in content.output["matches"]) == expected
    assert files.status == "ok"
    assert files.output["paths"] == expected
    assert counts.status == "ok"
    assert counts.output["counts"] == [
        {"path": expected[0], "count": 1},
        {"path": expected[1], "count": 1},
    ]
    assert str(target.resolve()) not in files.output["paths"]
    search_argvs = [call["argv"] for call in calls if call["kind"] == "popen"]
    assert sorted(argv[-1] for argv in search_argvs) == sorted(expected * 3)
    runtime["db"].close()


def test_search_text_content_mode_retains_only_page_plus_truncation_probe(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("\n".join(f"needle {index}" for index in range(20)), encoding="utf-8")
    max_retained = 0

    class TrackingContentPager(native_tools._SearchContentPager):
        def add(self, item):
            nonlocal max_retained
            super().add(item)
            max_retained = max(max_retained, len(self.page) + int(self.truncated))

    def fake_matches(_rg, _common_args, candidate, _timeout_seconds):
        for index in range(20):
            yield {
                "path": str(candidate),
                "line_number": index + 1,
                "line": f"needle {index}",
                "is_context": False,
                "line_truncated": False,
            }

    _install_basic_rg_stub(monkeypatch)
    monkeypatch.setattr(native_tools, "_SearchContentPager", TrackingContentPager)
    monkeypatch.setattr(native_tools, "_iter_ripgrep_matches", fake_matches)

    result = _invoke(
        runtime,
        "search_text",
        {"path": "notes.txt", "pattern": "needle", "offset": 3, "maxResults": 2},
    )

    assert result.status == "ok"
    assert [match["line_number"] for match in result.output["matches"]] == [4, 5]
    assert result.output["total_returned"] == 2
    assert result.output["truncated"] is True
    assert result.output["next_offset"] == 5
    assert max_retained <= 3
    runtime["db"].close()


def test_search_text_context_attachment_does_not_read_whole_file(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("before\nneedle\nmiddle\nafter\n", encoding="utf-8")

    def fail_read_text(self, *args, **kwargs):
        raise AssertionError("context attachment must not read the whole file")

    _install_basic_rg_stub(monkeypatch)
    monkeypatch.setattr(
        native_tools,
        "_iter_ripgrep_matches",
        lambda _rg, _common_args, candidate, _timeout_seconds: iter(
            [
                {
                    "path": str(candidate),
                    "line_number": 2,
                    "line": "needle",
                    "is_context": False,
                    "line_truncated": False,
                }
            ]
        ),
        raising=False,
    )
    monkeypatch.setattr(Path, "read_text", fail_read_text)
    result = _invoke(
        runtime,
        "search_text",
        {"path": "notes.txt", "pattern": "needle", "before_context": 1, "after_context": 1},
    )

    assert result.status == "ok"
    assert result.output["matches"] == [
        {
            "path": str(target.resolve()),
            "line_number": 1,
            "line": "before",
            "is_context": True,
            "line_truncated": False,
        },
        {
            "path": str(target.resolve()),
            "line_number": 2,
            "line": "needle",
            "is_context": False,
            "line_truncated": False,
        },
        {
            "path": str(target.resolve()),
            "line_number": 3,
            "line": "middle",
            "is_context": True,
            "line_truncated": False,
        },
    ]
    runtime["db"].close()


def test_search_text_streams_candidates_before_exhausting_candidate_iterator(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("needle first\n", encoding="utf-8")
    second.write_text("needle second\n", encoding="utf-8")
    searched_first = False

    def fake_candidates(_context, _root, *, include_hidden, skipped):
        yield first
        if not searched_first:
            raise AssertionError("candidate iterator was exhausted before first search")
        yield second

    def fake_matches(_rg, _common_args, candidate, _timeout_seconds):
        nonlocal searched_first
        if candidate == first:
            searched_first = True
        yield {
            "path": str(candidate),
            "line_number": 1,
            "line": candidate.read_text(encoding="utf-8").strip(),
            "is_context": False,
            "line_truncated": False,
        }

    _install_basic_rg_stub(monkeypatch)
    monkeypatch.setattr(native_tools, "_iter_search_candidate_files", fake_candidates)
    monkeypatch.setattr(native_tools, "_iter_ripgrep_matches", fake_matches)

    result = _invoke(
        runtime,
        "search_text",
        {"path": ".", "pattern": "needle", "maxResults": 1},
    )

    assert result.status == "ok"
    assert result.output["matches"] == [
        {
            "path": str(first),
            "line_number": 1,
            "line": "needle first",
            "is_context": False,
            "line_truncated": False,
        }
    ]
    assert result.output["truncated"] is True
    runtime["db"].close()


def test_search_text_unknown_type_wins_before_denied_path_policy(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    sessions = runtime["workspace"] / ".sessions"
    sessions.mkdir(exist_ok=True)
    _install_basic_rg_stub(monkeypatch)

    result = _invoke(
        runtime,
        "search_text",
        {"path": ".sessions", "pattern": "needle", "type": "unknown"},
    )

    assert result.status == "error"
    assert result.error["reason"] == "tool_schema_invalid"
    runtime["db"].close()


def test_search_text_oversized_ripgrep_record_skips_candidate(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("needle\n", encoding="utf-8")
    calls = []
    old_limit = native_tools.SEARCH_TEXT_RG_JSON_RECORD_BYTES
    monkeypatch.setattr(native_tools, "SEARCH_TEXT_RG_JSON_RECORD_BYTES", 64)

    def stdout_for_candidate(_path: Path) -> str:
        return _rg_json_match(target, 1, "needle " + ("x" * old_limit) + "\n")

    _install_popen_rg_stub(
        monkeypatch,
        calls=calls,
        stdout_for_candidate=stdout_for_candidate,
    )

    result = _invoke(runtime, "search_text", {"path": "notes.txt", "pattern": "needle"})

    assert result.status == "ok"
    assert result.output["matches"] == []
    assert result.output["skipped_files"] == {
        "denied": 0,
        "hidden": 0,
        "decode_error": 0,
        "other": 1,
    }
    runtime["db"].close()


def test_search_text_ripgrep_bytes_decode_error_skips_record(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("needle\n", encoding="utf-8")
    calls = []

    def stdout_for_candidate(_path: Path) -> str:
        return json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(target)},
                    "lines": {"bytes": [255]},
                    "line_number": 1,
                },
            }
        )

    _install_popen_rg_stub(
        monkeypatch,
        calls=calls,
        stdout_for_candidate=stdout_for_candidate,
    )

    result = _invoke(runtime, "search_text", {"path": "notes.txt", "pattern": "needle"})

    assert result.status == "ok"
    assert result.output["matches"] == []
    assert result.output["skipped_files"] == {
        "denied": 0,
        "hidden": 0,
        "decode_error": 1,
        "other": 0,
    }
    runtime["db"].close()


def test_search_text_filters_glob_type_hidden_decode_and_fixed_strings(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    (workspace / "src").mkdir()
    py = workspace / "src" / "app.PY"
    txt = workspace / "src" / "notes.txt"
    hidden = workspace / ".hidden.txt"
    bad = workspace / "src" / "bad.py"
    py.write_text("needle py\n", encoding="utf-8")
    txt.write_text("needle text\n", encoding="utf-8")
    hidden.write_text("needle hidden\n", encoding="utf-8")
    bad.write_bytes(b"\xff")
    calls = []

    def stdout_for_candidate(path: Path) -> str:
        if path == py:
            return _rg_json_match(py, 1, "needle py\n")
        return ""

    _install_popen_rg_stub(
        monkeypatch,
        calls=calls,
        stdout_for_candidate=stdout_for_candidate,
    )

    result = _invoke(
        runtime,
        "search_text",
        {
            "pattern": "needle",
            "glob": "src/**",
            "type": "python",
            "fixed_strings": True,
        },
    )

    search_calls = [call["argv"] for call in calls if call["kind"] == "popen"]
    assert result.status == "ok"
    assert result.output["matches"][0]["path"] == str(py.resolve())
    assert result.output["skipped_files"] == {
        "denied": 0,
        "hidden": 1,
        "decode_error": 1,
        "other": 0,
    }
    assert len(search_calls) == 1
    assert "-F" in search_calls[0]
    assert Path(search_calls[0][-1]) == py.resolve()
    assert all("--regexp" in argv for argv in search_calls)
    assert not any(str(txt.resolve()) in item for argv in search_calls for item in argv)
    assert not any(str(hidden.resolve()) in item for argv in search_calls for item in argv)
    assert not any(str(bad.resolve()) in item for argv in search_calls for item in argv)
    runtime["db"].close()


def test_list_dir_lists_immediate_entries_sorted_with_limit(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    (runtime["workspace"] / "b.txt").write_text("b", encoding="utf-8")
    (runtime["workspace"] / "a").mkdir()
    (runtime["workspace"] / "c.txt").write_text("c", encoding="utf-8")

    result = _invoke(runtime, "list_dir", {"path": ".", "limit": 2, "offset": 1})

    assert result.status == "ok"
    assert result.output == {
        "path": str(runtime["workspace"].resolve()),
        "entries": [
            {"name": "b.txt", "type": "file"},
            {"name": "c.txt", "type": "file"},
        ],
        "offset": 1,
        "limit": 2,
        "total_returned": 2,
        "truncated": False,
        "next_offset": None,
    }
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_list_dir_filters_hidden_denied_and_ignore_patterns(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    workspace = runtime["workspace"]
    (workspace / ".hidden").write_text("hidden", encoding="utf-8")
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")
    (workspace / "skip.log").write_text("skip", encoding="utf-8")
    (workspace / "build").mkdir()
    (workspace / "build" / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "sub").mkdir()

    hidden_excluded = _invoke(
        runtime,
        "list_dir",
        {"path": ".", "ignore": ["*.log", "sub/"], "include_hidden": False},
    )
    hidden_included = _invoke(
        runtime,
        "list_dir",
        {"path": ".", "ignore": ["*.log", "sub/**"], "include_hidden": True},
    )
    bad_ignore = _invoke(runtime, "list_dir", {"path": ".", "ignore": ["a/b"]})

    assert hidden_excluded.status == "ok"
    assert hidden_excluded.output["entries"] == [{"name": "keep.txt", "type": "file"}]
    assert hidden_included.status == "ok"
    assert hidden_included.output["entries"] == [
        {"name": ".hidden", "type": "file"},
        {"name": "keep.txt", "type": "file"},
    ]
    assert "build" not in str(hidden_included.output)
    assert bad_ignore.status == "error"
    assert bad_ignore.error["reason"] == "tool_schema_invalid"
    runtime["db"].close()


def test_read_file_pagination_utf8_and_cache_update(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("one\ntwo\nthree", encoding="utf-8", newline="\n")
    bad = runtime["workspace"] / "bad.bin"
    bad.write_bytes(b"\xff")

    page = _invoke(runtime, "read_file", {"path": "notes.txt", "offset": 1, "limit": 1})
    beyond = _invoke(runtime, "read_file", {"path": "notes.txt", "offset": 99})
    decode_error = _invoke(runtime, "read_file", {"path": "bad.bin"})

    assert page.status == "ok"
    assert page.output == {
        "path": str(target.resolve()),
        "content": "two\n",
        "offset": 1,
        "limit": 1,
        "total_returned": 1,
        "truncated": True,
        "next_offset": 2,
        "sha256": sha256(target.read_bytes()).hexdigest(),
        "bytes": len(target.read_bytes()),
    }
    assert beyond.status == "ok"
    assert beyond.output["content"] == ""
    assert beyond.output["total_returned"] == 0
    assert beyond.output["truncated"] is False
    assert beyond.output["next_offset"] is None
    assert decode_error.status == "error"
    assert decode_error.error["reason"] == "tool_execution_failed"
    cache = runtime["broker"].file_metadata_cache_snapshot()
    assert cache[str(target.resolve())]["sha256"] == sha256(target.read_bytes()).hexdigest()
    assert str(bad.resolve()) not in cache
    runtime["db"].close()


def test_read_file_phase35_tool_result_keeps_structured_output(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("one\ntwo\nthree", encoding="utf-8", newline="\n")

    result = _invoke(
        runtime,
        "read_file",
        {"path": "notes.txt", "offset": 1, "limit": 1},
        phase3_compatible_tool_results=True,
    )

    assert result.status == "ok"
    assert result.output == {
        "path": str(target.resolve()),
        "content": "two\n",
        "offset": 1,
        "limit": 1,
        "total_returned": 1,
        "truncated": True,
        "next_offset": 2,
        "sha256": sha256(target.read_bytes()).hexdigest(),
        "bytes": len(target.read_bytes()),
    }
    assert result.metadata["phase3_compatible_tool_result"] is True
    runtime["db"].close()


def test_find_file_defaults_to_workspace_and_returns_sorted_files_only(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    workspace = runtime["workspace"]
    (workspace / "src").mkdir()
    (workspace / "src" / "b.py").write_text("b", encoding="utf-8")
    (workspace / "src" / "A.PY").write_text("a", encoding="utf-8")
    (workspace / "src" / "nested").mkdir()
    (workspace / "src" / "nested" / "c.py").write_text("c", encoding="utf-8")
    (workspace / ".hidden.py").write_text("hidden", encoding="utf-8")

    result = _invoke(runtime, "find_file", {"pattern": "**/*.py", "maxResults": 2})

    expected = sorted(
        [
            str((workspace / "src" / "A.PY").resolve()),
            str((workspace / "src" / "b.py").resolve()),
            str((workspace / "src" / "nested" / "c.py").resolve()),
        ]
    )
    assert result.status == "ok"
    assert result.output == {
        "root": str(workspace.resolve()),
        "pattern": "**/*.py",
        "matches": expected[:2],
        "offset": 0,
        "maxResults": 2,
        "total_returned": 2,
        "truncated": True,
        "next_offset": 2,
    }
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_find_file_glob_subset_character_classes_casefold_and_hidden(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    workspace = runtime["workspace"]
    (workspace / "docs").mkdir()
    (workspace / "docs" / "Alpha.TXT").write_text("a", encoding="utf-8")
    (workspace / "docs" / "beta.txt").write_text("b", encoding="utf-8")
    (workspace / ".secret.txt").write_text("hidden", encoding="utf-8")

    class_match = _invoke(runtime, "find_file", {"pattern": "docs/[A]*.txt"})
    single_char = _invoke(
        runtime,
        "find_file",
        {"pattern": "docs/beta.tx?", "case_sensitive": True},
    )
    hidden = _invoke(
        runtime,
        "find_file",
        {"pattern": "*.txt", "include_hidden": True},
    )

    assert class_match.status == "ok"
    assert class_match.output["matches"] == [
        str((workspace / "docs" / "Alpha.TXT").resolve())
    ]
    assert single_char.status == "ok"
    assert single_char.output["matches"] == [
        str((workspace / "docs" / "beta.txt").resolve())
    ]
    assert hidden.status == "ok"
    assert hidden.output["matches"] == [str((workspace / ".secret.txt").resolve())]
    runtime["db"].close()


def test_find_file_rejects_unsupported_glob_before_policy(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")

    bad_backslash = _invoke(runtime, "find_file", {"path": ".sessions", "pattern": r"\*.py"})
    bad_glob = _invoke(runtime, "find_file", {"path": ".sessions", "pattern": "a**.py"})
    bad_class = _invoke(runtime, "find_file", {"path": ".sessions", "pattern": "[!a].py"})
    malformed_class = _invoke(runtime, "find_file", {"path": ".sessions", "pattern": "[abc"})
    brace = _invoke(runtime, "find_file", {"path": ".sessions", "pattern": "{a,b}.py"})
    empty = _invoke(runtime, "find_file", {"path": ".sessions", "pattern": "   "})

    assert bad_backslash.status == "error"
    assert bad_backslash.error["reason"] == "tool_schema_invalid"
    assert bad_glob.status == "error"
    assert bad_glob.error["reason"] == "tool_schema_invalid"
    assert bad_class.status == "error"
    assert bad_class.error["reason"] == "tool_schema_invalid"
    assert malformed_class.status == "error"
    assert malformed_class.error["reason"] == "tool_schema_invalid"
    assert brace.status == "error"
    assert brace.error["reason"] == "tool_schema_invalid"
    assert empty.status == "error"
    assert empty.error["reason"] == "tool_schema_invalid"
    runtime["db"].close()


def test_find_file_symlink_directory_and_file_target_policy(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    workspace = runtime["workspace"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "real").mkdir()
    (workspace / "real" / "ok.txt").write_text("ok", encoding="utf-8")
    (workspace / "file_link.txt").symlink_to(workspace / "real" / "ok.txt")
    (workspace / "dir_link").symlink_to(workspace / "real", target_is_directory=True)
    (workspace / "escape.txt").symlink_to(outside / "secret.txt")

    result = _invoke(runtime, "find_file", {"pattern": "**/*.txt"})

    assert result.status == "ok"
    assert result.output["matches"] == sorted(
        [
            str((workspace.resolve() / "file_link.txt")),
            str((workspace / "real" / "ok.txt").resolve()),
        ]
    )
    assert all("dir_link" not in path for path in result.output["matches"])
    assert all("escape" not in path for path in result.output["matches"])
    runtime["db"].close()


def test_structured_native_large_field_is_artifacted_without_row_fallback(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    content = "x" * (16 * 1024 + 1)
    target = runtime["workspace"] / "large.txt"
    target.write_text("small", encoding="utf-8")
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
        router=_NativeResultRouter(
            NativeHandlerResult(
                status="ok",
                output={
                    "path": str(target.resolve()),
                    "content": content,
                    "offset": 0,
                    "limit": 2000,
                    "total_returned": 1,
                    "truncated": False,
                    "next_offset": None,
                    "sha256": "0" * 64,
                    "bytes": len(content.encode("utf-8")),
                },
                metadata={"tool_name": "read_file"},
            )
        ),
    )

    result = _invoke(runtime, "read_file", {"path": "large.txt"})

    assert result.status == "ok"
    assert result.output["path"] == str(target.resolve())
    assert result.output["sha256"] == "0" * 64
    assert result.output["bytes"] == len(content.encode("utf-8"))
    assert len(result.artifacts) == 1
    assert result.output["content"]["artifact_id"] == result.artifacts[0]
    assert result.output["content"]["relative_path"].startswith("sess_1/artifacts/")
    assert result.output["content"]["sha256"].startswith("sha256:")
    assert runtime["artifacts"].resolve_path(result.artifacts[0]).read_text(
        encoding="utf-8"
    ) == content
    assert _event_kinds(runtime) == [
        "tool_call_started",
        "artifact_registered",
        "tool_call_completed",
    ]
    runtime["db"].close()


def test_structured_native_oversize_after_field_artifacting_returns_error_without_exposed_artifact_ids(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    target = runtime["workspace"] / "large.txt"
    target.write_text("small", encoding="utf-8")
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
        router=_NativeResultRouter(
            NativeHandlerResult(
                status="ok",
                output={
                    "path": str(target.resolve()),
                    "content": "x" * (16 * 1024 + 1),
                    "unartifactable_metadata": "y" * (16 * 1024 + 1),
                },
                metadata={"tool_name": "read_file"},
            )
        ),
    )

    result = _invoke(runtime, "read_file", {"path": "large.txt"})

    assert result.status == "error"
    assert result.output is None
    assert result.artifacts == []
    assert result.error["reason"] == "tool_execution_failed"
    assert _event_kinds(runtime) == ["tool_call_started", "tool_call_failed"]
    runtime["db"].close()


@pytest.mark.parametrize(
    "store_factory",
    [_FailingArtifactWriteStore, _FailingArtifactRegistrationStore],
)
def test_structured_native_field_artifact_failure_returns_error_without_exposed_artifact_ids(
    tmp_path,
    store_factory,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    content = "x" * (16 * 1024 + 1)
    target = runtime["workspace"] / "large.txt"
    target.write_text("small", encoding="utf-8")
    artifact_store = store_factory(runtime["artifacts"])
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=artifact_store,
        router=_NativeResultRouter(
            NativeHandlerResult(
                status="ok",
                output={
                    "path": str(target.resolve()),
                    "content": content,
                    "offset": 0,
                    "limit": 2000,
                    "total_returned": 1,
                    "truncated": False,
                    "next_offset": None,
                    "sha256": "0" * 64,
                    "bytes": len(content.encode("utf-8")),
                },
                metadata={"tool_name": "read_file"},
            )
        ),
    )

    result = _invoke(runtime, "read_file", {"path": "large.txt"})

    assert result.status == "error"
    assert result.output is None
    assert result.artifacts == []
    assert result.error["reason"] == "tool_execution_failed"
    assert _event_kinds(runtime) == ["tool_call_started", "tool_call_failed"]
    failed_event = runtime["events"].list_for_run("run_1")[-1]
    assert failed_event.payload["artifact_ids"] == []
    assert runtime["artifacts"].list_for_session(runtime["session"].session_id) == []
    runtime["db"].close()


def test_structured_native_field_artifact_write_is_inside_timeout_envelope(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    target = runtime["workspace"] / "large.txt"
    target.write_text("small", encoding="utf-8")
    artifact_store = _SlowArtifactStore(runtime["artifacts"], delay_seconds=0.05)
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=artifact_store,
        timeout_seconds=0.01,
        router=_NativeResultRouter(
            NativeHandlerResult(
                status="ok",
                output={
                    "path": str(target.resolve()),
                    "content": "x" * (16 * 1024 + 1),
                    "sha256": "0" * 64,
                    "bytes": 16 * 1024 + 1,
                },
                metadata={"tool_name": "read_file"},
            )
        ),
    )

    result = _invoke(runtime, "read_file", {"path": "large.txt"})

    assert result.status == "timeout"
    assert result.output is None
    assert result.artifacts == []
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "tool_execution_timeout"
    failed = runtime["events"].list_for_run("run_1")[-1].payload
    assert failed["arguments"]["path"] == str(target.resolve())
    assert failed["artifact_ids"] == []
    time.sleep(0.1)
    assert runtime["artifacts"].list_for_session(runtime["session"].session_id) == []
    runtime["db"].close()


def test_started_audit_emission_does_not_consume_handler_timeout_envelope(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("small", encoding="utf-8")
    clock = _FakeClock()
    event_writer = _StartedEventAdvancingWriter(
        runtime["events"],
        clock,
        seconds=1.0,
    )
    runtime["broker"] = ToolBroker(
        event_writer=event_writer,
        artifact_store=runtime["artifacts"],
        timeout_seconds=0.5,
        router=_NativeResultRouter(
            NativeHandlerResult(
                status="ok",
                output={"path": str(target.resolve()), "content": "ok"},
                metadata={"tool_name": "read_file"},
            )
        ),
    )

    monkeypatch.setattr("debug_agent.tools.broker.monotonic", clock.monotonic)

    result = _invoke(runtime, "read_file", {"path": "notes.txt"})

    assert result.status == "ok"
    assert result.error is None
    assert _event_kinds(runtime) == ["tool_call_started", "tool_call_completed"]
    completed = runtime["events"].list_for_run("run_1")[-1].payload
    assert completed["execution_duration_ms"] == 0
    runtime["db"].close()


def test_structured_native_field_artifact_commit_after_timeout_is_cleaned_up(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    target = runtime["workspace"] / "large.txt"
    target.write_text("small", encoding="utf-8")
    inserted = threading.Event()
    release_commit = threading.Event()
    cleaned_up = threading.Event()
    artifact_store = _DeadlineCrossingArtifactStore(
        runtime["db"].connection,
        runtime["artifacts"].sessions_root,
        inserted=inserted,
        release_commit=release_commit,
        cleaned_up=cleaned_up,
    )
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=artifact_store,
        timeout_seconds=1,
        router=_NativeResultRouter(
            NativeHandlerResult(
                status="ok",
                output={
                    "path": str(target.resolve()),
                    "content": "x" * (16 * 1024 + 1),
                    "sha256": "0" * 64,
                    "bytes": 16 * 1024 + 1,
                },
                metadata={"tool_name": "read_file"},
            )
        ),
    )

    result_holder: dict[str, object] = {}

    def invoke_tool() -> None:
        try:
            result_holder["result"] = _invoke(runtime, "read_file", {"path": "large.txt"})
        except BaseException as exc:  # pragma: no cover - surfaced below
            result_holder["exception"] = exc

    invoke_thread = threading.Thread(target=invoke_tool)
    invoke_thread.start()
    try:
        assert inserted.wait(timeout=1)
        invoke_thread.join(timeout=2)
        assert not invoke_thread.is_alive()
    finally:
        release_commit.set()
        invoke_thread.join(timeout=2)
    if "exception" in result_holder:
        raise result_holder["exception"]
    result = result_holder["result"]

    assert result.status == "timeout"
    assert result.artifacts == []
    assert cleaned_up.wait(timeout=2)
    if artifact_store.cleanup_exception is not None:
        raise artifact_store.cleanup_exception
    assert runtime["artifacts"].list_for_session(runtime["session"].session_id) == []
    artifacts_dir = runtime["artifacts"].sessions_root / "sess_1" / "artifacts"
    assert not any(artifacts_dir.iterdir())
    runtime["db"].close()


def test_final_tool_result_formatting_is_outside_timeout_envelope(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("small", encoding="utf-8")
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
        timeout_seconds=0.01,
        router=_NativeResultRouter(
            NativeHandlerResult(
                status="ok",
                output={"path": str(target.resolve()), "content": "ok"},
                metadata={"tool_name": "read_file"},
            )
        ),
    )
    original = runtime["broker"]._tool_result_from_prepared

    def slow_format(*args, **kwargs):
        time.sleep(0.05)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime["broker"], "_tool_result_from_prepared", slow_format)

    result = _invoke(runtime, "read_file", {"path": "notes.txt"})

    assert result.status == "ok"
    assert result.error is None
    runtime["db"].close()


def test_timeout_does_not_commit_staged_file_metadata_cache_updates(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
        timeout_seconds=0.01,
        router=_CacheObservingSlowRouter(entered, release),
    )

    result = _invoke(runtime, "read_file", {"path": "notes.txt"})
    release.set()

    assert entered.is_set()
    assert result.status == "timeout"
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_worker_continuing_after_timeout_cannot_advance_file_metadata_cache(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    runtime["broker"] = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
        timeout_seconds=0.1,
        router=_CacheRecordingAfterTimeoutRouter(entered, release),
    )

    result_holder: dict[str, object] = {}

    def invoke_tool() -> None:
        try:
            result_holder["result"] = _invoke(runtime, "read_file", {"path": "notes.txt"})
        except BaseException as exc:  # pragma: no cover - surfaced below
            result_holder["exception"] = exc

    invoke_thread = threading.Thread(target=invoke_tool)
    invoke_thread.start()
    try:
        assert entered.wait(timeout=1)
        invoke_thread.join(timeout=1)
        assert not invoke_thread.is_alive()
    finally:
        release.set()
        invoke_thread.join(timeout=1)
    if "exception" in result_holder:
        raise result_holder["exception"]
    result = result_holder["result"]

    assert result.status == "timeout"
    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_file_metadata_cache_is_process_local_and_starts_empty_for_new_broker(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    stage = runtime["broker"]._stage_file_metadata_for_test(
        target,
        source_tool="read_file",
    )
    runtime["broker"]._commit_file_metadata_stage_for_test(stage)

    fresh_broker = ToolBroker(
        event_writer=runtime["events"],
        artifact_store=runtime["artifacts"],
    )

    cache = runtime["broker"].file_metadata_cache_snapshot()
    entry = cache[str(target.resolve())]
    assert entry["sha256"] == sha256(b"hello").hexdigest()
    assert entry["size"] == 5
    assert isinstance(entry["mtime_ns"], int)
    assert isinstance(entry["observed_at"], str)
    assert entry["source_tool"] == "read_file"
    assert fresh_broker.file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_file_metadata_cache_rejects_sources_that_do_not_advance_write_guard(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    target = runtime["workspace"] / "notes.txt"
    target.write_text("hello", encoding="utf-8")

    with pytest.raises(ValueError):
        runtime["broker"]._stage_file_metadata_for_test(target, source_tool="list_dir")

    assert runtime["broker"].file_metadata_cache_snapshot() == {}
    runtime["db"].close()


def test_same_canonical_path_write_locks_serialize_in_process(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    first_entered = threading.Event()
    release_first = threading.Event()
    active = 0
    max_active = 0
    calls = 0
    state_lock = threading.Lock()

    def locked_section(path: str) -> None:
        nonlocal active, calls, max_active
        with runtime["broker"]._write_lock_for_path_for_test(path):
            with state_lock:
                calls += 1
                call_number = calls
                active += 1
                max_active = max(max_active, active)
            if call_number == 1:
                first_entered.set()
                release_first.wait(timeout=1)
            try:
                time.sleep(0.01)
            finally:
                with state_lock:
                    active -= 1

    first = threading.Thread(target=locked_section, args=("locked.txt",))
    second = threading.Thread(target=locked_section, args=("./locked.txt",))
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    time.sleep(0.02)
    assert max_active == 1
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert calls == 2
    assert max_active == 1
    runtime["db"].close()


def test_native_handlers_do_not_write_audit_events_directly(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    (runtime["workspace"] / "notes.txt").write_text("hello", encoding="utf-8")

    result = _invoke(runtime, "read_file", {"path": "notes.txt"})
    events = runtime["events"].list_for_run("run_1")

    assert result.status == "ok"
    assert [event.kind for event in events] == [
        "tool_call_started",
        "tool_call_completed",
    ]
    assert events[-1].payload["result"] == result.to_dict()
    assert json.dumps(events[-1].payload)


def test_tool_audit_payload_includes_broker_normalized_target_for_native_search_and_shell(
    tmp_path, monkeypatch
) -> None:
    _install_basic_rg_stub(monkeypatch)
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("needle\n", encoding="utf-8")

    read = _invoke(runtime, "read_file", {"path": "src/app.py"})
    search = _invoke(runtime, "search_text", {"path": "src", "pattern": "needle"})
    shell = _invoke(
        runtime,
        "shell_exec",
        {"argv": ["pytest", "tests"], "cwd": "."},
        shell_runner=FakeShellRunner(stdout="ok\n"),
    )

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ]
    assert read.status == "ok"
    assert search.status == "ok"
    assert shell.status == "ok"
    assert completed[0]["target"] == str((workspace / "src/app.py").resolve())
    assert completed[1]["target"] == f"needle in {(workspace / 'src').resolve()}"
    assert completed[2]["target"] == "pytest tests"
    runtime["db"].close()


def test_phase_3_5_search_defaults_participate_in_scope_and_audit_but_pagination_does_not(
    tmp_path, monkeypatch
) -> None:
    _install_basic_rg_stub(monkeypatch)
    runtime = _runtime(tmp_path, approval_mode="normal")
    search_root = tmp_path / "outside-src"
    search_root.mkdir()
    (search_root / "app.txt").write_text("needle\n", encoding="utf-8")
    provider = FakeApprovalProvider("approved_for_session")

    result = _invoke(
        runtime,
        "search_text",
        {"path": str(search_root), "pattern": "needle", "maxResults": 1},
        approval_provider=provider,
    )

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ][0]
    request_facts = provider.requests[0][1]
    assert result.status == "ok"
    assert f"read:{search_root.resolve()}" in request_facts["scope_signature"]
    assert "pattern:needle" in request_facts["scope_signature"]
    assert "glob:**" in request_facts["scope_signature"]
    assert "before_context_effective:0" in request_facts["scope_signature"]
    assert "maxResults" not in request_facts["scope_signature"]
    assert completed["arguments"] == {
        "path": str(search_root.resolve()),
        "pattern": "needle",
        "glob": "**",
        "offset": 0,
        "maxResults": 1,
        "output_mode": "content",
        "fixed_strings": False,
        "case_sensitive": True,
        "include_hidden": False,
        "before_context_effective": 0,
        "after_context_effective": 0,
    }


def test_search_text_context_presence_is_checked_before_default_injection(
    tmp_path, monkeypatch
) -> None:
    _install_basic_rg_stub(monkeypatch)
    runtime = _runtime(tmp_path, approval_mode="yolo")
    (runtime["workspace"] / "notes.txt").write_text("needle\n", encoding="utf-8")

    only_context = _invoke(runtime, "search_text", {"pattern": "needle", "context": 2})
    conflict = _invoke(
        runtime,
        "search_text",
        {"pattern": "needle", "context": 2, "before_context": 1},
    )

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ][0]
    assert only_context.status == "ok"
    assert completed["arguments"]["before_context_effective"] == 2
    assert completed["arguments"]["after_context_effective"] == 2
    assert conflict.status == "error"
    assert conflict.error["reason"] == "tool_schema_invalid"


def test_write_and_edit_audit_arguments_are_redacted_without_changing_approval_scope(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    workspace = runtime["workspace"]
    target = workspace / "notes.txt"
    target.write_text("old", encoding="utf-8")

    write = _invoke(runtime, "write_file", {"path": "new.txt", "content": "secret"})
    read = _invoke(runtime, "read_file", {"path": "notes.txt"})
    edit = _invoke(
        runtime,
        "edit_file",
        {"path": "notes.txt", "old_text": "old", "new_text": "new", "replace_all": True},
    )

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ]
    assert write.status == "ok"
    assert read.status == "ok"
    assert edit.status == "ok"
    write_args = [
        payload for payload in completed if payload["tool_name"] == "write_file"
    ][0]["arguments"]
    edit_args = [
        payload for payload in completed if payload["tool_name"] == "edit_file"
    ][0]["arguments"]
    assert "content" not in write_args
    assert write_args["content_sha256"] == sha256(b"secret").hexdigest()
    assert write_args["content_bytes"] == len(b"secret")
    assert "old_text" not in edit_args
    assert "new_text" not in edit_args
    assert edit_args["old_text_sha256"] == sha256(b"old").hexdigest()
    assert edit_args["old_text_bytes"] == len(b"old")
    assert edit_args["new_text_sha256"] == sha256(b"new").hexdigest()
    assert edit_args["new_text_bytes"] == len(b"new")


def test_broker_restores_policy_from_frozen_config_when_policy_facts_are_absent(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    workspace = runtime["workspace"]
    facts = build_builtin_policy(workspace)
    facts.user_path_deny.append(
        PathPolicyEntry.from_raw("deny", "README.md", workspace, facts.home)
    )
    facts.user_shell = ShellPolicy(deny=[("git",)])
    (workspace / "README.md").write_text("secret", encoding="utf-8")
    base_context = {
        "workspace_root": str(workspace),
        "approval_mode": "yolo",
        "frozen_config": {"policy": policy_facts_to_snapshot(facts)},
        "approval_grants": ApprovalGrantStore(runtime["db"].connection),
        "approval_provider": FakeApprovalProvider("denied"),
    }

    denied_read = runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="read_file",
        arguments={"path": "README.md"},
        context=base_context,
    )
    denied_shell = runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name="shell_exec",
        arguments={"argv": ["git", "status"]},
        context=base_context,
    )

    assert denied_read.status == "denied"
    assert denied_read.error["message"].startswith("Path denied by policy:")
    assert denied_read.error["error_class"] == "policy_error"
    assert denied_read.error["reason"] == "path_policy_denied"
    assert denied_shell.status == "denied"
    assert denied_shell.error["message"] == "Command denied by user shell policy."
    assert denied_shell.error["error_class"] == "policy_error"
    assert denied_shell.error["reason"] == "shell_policy_denied"
    assert _event_kinds(runtime) == ["tool_call_denied", "tool_call_denied"]
    runtime["db"].close()


def test_approval_wait_duration_is_persisted_and_excluded_from_execution_duration(
    tmp_path,
    monkeypatch,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="normal")
    ticks = iter([10.0, 11.25, 11.5, 11.5, 11.5, 11.5, 11.75])
    monkeypatch.setattr("debug_agent.tools.broker.monotonic", lambda: next(ticks))

    result = _invoke(
        runtime,
        "read_file",
        {"path": str(outside)},
        approval_provider=FakeApprovalProvider("approved_once"),
    )

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ][0]
    assert result.status == "ok"
    assert completed["approval_wait_duration_ms"] == 250
    assert completed["execution_duration_ms"] == 250
    assert completed["duration"] == 0.25
    runtime["db"].close()


def test_non_executed_denials_record_zero_approval_wait_and_no_execution_duration(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")

    result = _invoke(runtime, "shell_exec", {"argv": ["rm", "-rf", "target"]})

    denied = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_denied"
    ][0]
    assert result.status == "denied"
    assert result.error["error_class"] == "policy_error"
    assert result.error["reason"] == "shell_policy_denied"
    assert denied["target"] == "rm -rf target"
    assert denied["approval_wait_duration_ms"] == 0
    assert "execution_duration_ms" not in denied
    runtime["db"].close()

from __future__ import annotations

import json
from pathlib import Path

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.policy import (
    PathPolicyEntry,
    PermissionEvaluator,
    build_builtin_policy,
)
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker


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


def test_schema_validation_rejects_unknown_fields_and_invalid_limits(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    (runtime["workspace"] / "notes.txt").write_text("hello", encoding="utf-8")

    extra = _invoke(runtime, "read_file", {"path": "notes.txt", "extra": True})
    zero = _invoke(runtime, "read_file", {"path": "notes.txt", "limit": 0})
    missing = _invoke(runtime, "search_text", {"query": "hello"})

    assert extra.status == "denied"
    assert zero.status == "denied"
    assert missing.status == "denied"
    assert extra.error["error_class"] == "user_error"
    assert zero.error["message"] == "limit must be a positive integer."
    assert _event_kinds(runtime) == [
        "tool_call_denied",
        "tool_call_denied",
        "tool_call_denied",
    ]
    runtime["db"].close()


def test_read_file_auto_allows_trusted_workspace_under_normal(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    (runtime["workspace"] / "notes.txt").write_text("a\nb\nc\n", encoding="utf-8")

    result = _invoke(runtime, "read_file", {"path": "notes.txt", "limit": 2})

    assert result.status == "ok"
    assert result.output == "a\nb\n"
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
    assert approved.output == "secret"
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
    assert missing.error["error_class"] == "tool_error"
    runtime["db"].close()


def test_edit_file_lf_fallback_when_no_dominant_line_ending(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    target = runtime["workspace"] / "single.txt"
    target.write_text("old", encoding="utf-8")

    result = _invoke(
        runtime,
        "edit_file",
        {"path": "single.txt", "old_text": "old", "new_text": "new\nline"},
    )

    assert result.status == "ok"
    assert target.read_bytes() == b"new\nline"
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
    tmp_path,
) -> None:
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
        {"query": "needle", "path": ".", "limit": 5},
        permission_evaluator=PermissionEvaluator(facts),
    )
    explicit_denied = _invoke(
        runtime,
        "search_text",
        {"query": "needle", "path": "secret"},
        permission_evaluator=PermissionEvaluator(facts),
    )

    assert default_search.status == "ok"
    assert default_search.output == {
        "matches": [{"path": "src/app.txt", "line": 1, "text": "needle app"}]
    }
    assert explicit_denied.status == "denied"
    runtime["db"].close()


def test_search_text_outside_workspace_returns_absolute_paths_when_allowed(
    tmp_path,
) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "notes.txt"
    outside_file.write_text("needle outside", encoding="utf-8")
    runtime = _runtime(tmp_path, approval_mode="yolo")

    result = _invoke(
        runtime,
        "search_text",
        {"path": str(outside_dir), "query": "needle"},
    )

    assert result.status == "ok"
    assert result.output == {
        "matches": [
            {
                "path": str(outside_file.resolve()),
                "line": 1,
                "text": "needle outside",
            }
        ]
    }
    runtime["db"].close()


def test_list_dir_lists_immediate_entries_sorted_with_limit(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    (runtime["workspace"] / "b.txt").write_text("b", encoding="utf-8")
    (runtime["workspace"] / "a").mkdir()

    result = _invoke(runtime, "list_dir", {"path": ".", "limit": 2})

    assert result.status == "ok"
    assert result.output == {
        "entries": [
            {"name": "a", "type": "directory"},
            {"name": "b.txt", "type": "file"},
        ]
    }
    runtime["db"].close()


def test_large_output_is_written_to_text_artifact_by_broker(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    content = "x" * (16 * 1024 + 1)
    (runtime["workspace"] / "large.txt").write_text(content, encoding="utf-8")

    result = _invoke(runtime, "read_file", {"path": "large.txt"})

    assert result.status == "ok"
    assert result.output is None
    assert len(result.artifacts) == 1
    assert runtime["artifacts"].resolve_path(result.artifacts[0]).read_text(
        encoding="utf-8"
    ) == content
    assert _event_kinds(runtime) == [
        "tool_call_started",
        "artifact_registered",
        "tool_call_completed",
    ]
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

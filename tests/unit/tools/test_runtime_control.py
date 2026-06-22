from __future__ import annotations

import sqlite3

import pytest

from debug_agent.persistence.approval_grants import ApprovalGrantStore
from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.runtime.policy import build_builtin_policy
from debug_agent.skills.registry import SkillRegistry
from debug_agent.tools.broker import FakeApprovalProvider, ToolBroker
from debug_agent.tools.runtime_control import tool_definitions


def _skill_md(name: str, body: str = "Use this skill.") -> str:
    return f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n\n{body}\n"


def _runtime(tmp_path, *, approval_mode: str = "semi-auto"):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(parents=True)
    home.mkdir()
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
    store = SkillSnapshotStore(db.connection)
    broker = ToolBroker(event_writer=events, artifact_store=artifacts)
    context = {
        "workspace_root": str(workspace),
        "approval_mode": approval_mode,
        "policy_facts": build_builtin_policy(workspace),
        "approval_grants": ApprovalGrantStore(db.connection),
        "approval_provider": FakeApprovalProvider("denied"),
        "skill_snapshot_store": store,
        "run_store": runs,
    }
    return {
        "workspace": workspace,
        "home": home,
        "db": db,
        "session": session,
        "run": run,
        "runs": runs,
        "events": events,
        "artifacts": artifacts,
        "store": store,
        "broker": broker,
        "context": context,
    }


def _snapshot_skill(runtime, *, name: str = "alpha", reference_text: str = "guide") -> None:
    skill_dir = runtime["workspace"] / ".debug-agent" / "skills" / name
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md(name, body="ORIGINAL"), encoding="utf-8")
    (skill_dir / "references" / "guide.txt").write_text(reference_text, encoding="utf-8")
    (skill_dir / "references" / "large.txt").write_text("x" * (17 * 1024), encoding="utf-8")
    (skill_dir / "references" / "blob.bin").write_bytes(b"\xff\x00")
    (skill_dir / "assets" / "sprite.txt").write_text("asset text", encoding="utf-8")
    (skill_dir / "scripts" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    snapshots = SkillRegistry(
        workspace_root=runtime["workspace"],
        home_dir=runtime["home"],
        artifact_store=runtime["artifacts"],
    ).snapshot(session_id=runtime["session"].session_id, run_id=runtime["run"].run_id)
    runtime["store"].save_many(snapshots)


def _invoke(runtime, tool_name: str, arguments: dict, **context):
    return runtime["broker"].invoke(
        session_id=runtime["session"].session_id,
        run_id=runtime["run"].run_id,
        tool_name=tool_name,
        arguments=arguments,
        context={**runtime["context"], **context},
    )


def _event_kinds(runtime) -> list[str]:
    return [event.kind for event in runtime["events"].list_for_run("run_1")]


def test_runtime_control_tool_definitions_are_strict() -> None:
    definitions = {definition.name: definition for definition in tool_definitions()}

    assert definitions["activate_skill"].category == "runtime_control"
    assert definitions["activate_skill"].risk_level == "runtime_control"
    assert definitions["activate_skill"].input_schema == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    assert "load_skill_ref_file" not in definitions
    assert definitions["load_skill_resource"].category == "runtime_control"
    assert definitions["load_skill_resource"].risk_level == "read"
    assert definitions["load_skill_resource"].description == (
        "Load one frozen resource file for an active skill. Use this when active skill\n"
        "instructions or available_resources reference a file whose contents are needed."
    )
    assert definitions["load_skill_resource"].input_schema == {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["skill_name", "path"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("activate_skill", {}),
        ("activate_skill", {"name": "alpha", "extra": True}),
        ("load_skill_resource", {"skill_name": "alpha"}),
        ("load_skill_resource", {"skill_name": "alpha", "path": "references/guide.txt", "extra": True}),
    ],
)
def test_runtime_control_schema_rejects_missing_and_unknown_fields(
    tmp_path, tool_name, arguments
) -> None:
    runtime = _runtime(tmp_path)

    result = _invoke(runtime, tool_name, arguments)

    assert result.status == "error"
    assert result.error["error_class"] == "tool_error"
    assert result.error["reason"] == "tool_schema_invalid"
    assert _event_kinds(runtime) == ["tool_call_failed"]
    runtime["db"].close()


def test_activate_skill_requires_approval_in_normal_and_is_idempotent(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    _snapshot_skill(runtime, name="alpha")

    denied = _invoke(runtime, "activate_skill", {"name": "alpha"})
    approved = _invoke(
        runtime,
        "activate_skill",
        {"name": "alpha"},
        approval_provider=FakeApprovalProvider("approved_once"),
    )
    repeated_provider = FakeApprovalProvider("denied")
    again = _invoke(
        runtime,
        "activate_skill",
        {"name": "alpha"},
        approval_provider=repeated_provider,
    )

    active = runtime["runs"].get("run_1").active_skills
    assert denied.status == "denied"
    assert approved.status == "ok"
    assert again.status == "ok"
    assert "ORIGINAL" not in str(approved.output)
    assert active == [
        {
            "name": "alpha",
            "content_hash": approved.metadata["content_hash"],
            "activation_reason": "model_requested",
            "scope": "run",
        }
    ]
    assert not repeated_provider.requests
    assert _event_kinds(runtime).count("approval_requested") == 2
    assert _event_kinds(runtime).count("skill_activated") == 1
    runtime["db"].close()


def test_activate_skill_is_audit_only_in_semi_auto_and_uses_frozen_snapshot(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    _snapshot_skill(runtime, name="alpha")
    (runtime["workspace"] / ".debug-agent" / "skills" / "alpha" / "SKILL.md").write_text(
        _skill_md("alpha", body="MUTATED"), encoding="utf-8"
    )

    result = _invoke(runtime, "activate_skill", {"name": "alpha"})

    assert result.status == "ok"
    assert result.output == f"Skill activated: alpha ({result.metadata['content_hash']})"
    assert not runtime["context"]["approval_provider"].requests
    assert _event_kinds(runtime) == [
        "tool_call_started",
        "tool_call_completed",
        "skill_activated",
    ]
    runtime["db"].close()


def test_activate_skill_denies_unknown_and_corrupt_snapshot_before_approval(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    _snapshot_skill(runtime, name="alpha")
    provider = FakeApprovalProvider("approved_once")

    unknown = _invoke(runtime, "activate_skill", {"name": "missing"}, approval_provider=provider)
    runtime["db"].connection.execute(
        "UPDATE skill_snapshots SET skill_md_content = 'corrupt' WHERE skill_name = 'alpha'"
    )
    runtime["db"].connection.commit()
    corrupt = _invoke(runtime, "activate_skill", {"name": "alpha"}, approval_provider=provider)

    assert unknown.status == "error"
    assert corrupt.status == "error"
    assert unknown.error["error_class"] == "config_error"
    assert corrupt.error["error_class"] == "config_error"
    assert provider.requests == []
    runtime["db"].close()


def test_load_skill_resource_requires_active_skill_and_valid_relative_resource(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="yolo")
    _snapshot_skill(runtime, name="alpha", reference_text="guide text")

    inactive = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/guide.txt"},
    )
    _invoke(runtime, "activate_skill", {"name": "alpha"})
    loaded = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/guide.txt"},
    )
    loaded_asset = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "assets/sprite.txt"},
    )
    loaded_script = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "scripts/helper.sh"},
    )
    traversal = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "../SKILL.md"},
    )
    absolute = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": str(runtime["workspace"] / "x.txt")},
    )

    assert inactive.status == "error"
    assert inactive.error["error_class"] == "config_error"
    assert loaded.status == "ok"
    assert loaded.output["content"] == "guide text"
    assert loaded.output["resource_path"] == "references/guide.txt"
    assert loaded.output["resource_kind"] == "reference"
    assert loaded.output["content_hash"].startswith("sha256:")
    assert loaded_asset.output["resource_kind"] == "asset"
    assert loaded_script.output["resource_kind"] == "script"
    assert traversal.status == "error"
    assert absolute.status == "error"
    assert not runtime["context"]["approval_provider"].requests
    assert _event_kinds(runtime) == [
        "tool_call_failed",
        "tool_call_started",
        "tool_call_completed",
        "skill_activated",
        "tool_call_started",
        "tool_call_completed",
        "skill_resource_loaded",
        "tool_call_started",
        "tool_call_completed",
        "skill_resource_loaded",
        "tool_call_started",
        "tool_call_completed",
        "skill_resource_loaded",
        "tool_call_failed",
        "tool_call_failed",
    ]
    runtime["db"].close()


def test_load_skill_resource_returns_markers_for_large_text_and_binary(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    _snapshot_skill(runtime, name="alpha")
    _invoke(runtime, "activate_skill", {"name": "alpha"})

    large = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/large.txt"},
    )
    binary = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/blob.bin"},
    )

    assert large.status == "ok"
    assert binary.status == "ok"
    assert large.output["content"] is None
    assert binary.output["content"] is None
    assert large.output["artifact_id"]
    assert binary.output["artifact_id"]
    assert large.output["resource_marker"].startswith("[skill resource stored as artifact:")
    assert binary.output["resource_marker"].startswith("[skill resource stored as artifact:")
    runtime["db"].close()


def test_load_skill_resource_denies_missing_and_hash_mismatch_before_approval(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="normal")
    _snapshot_skill(runtime, name="alpha")
    _invoke(
        runtime,
        "activate_skill",
        {"name": "alpha"},
        approval_provider=FakeApprovalProvider("approved_once"),
    )
    provider = FakeApprovalProvider("approved_once")

    missing = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/missing.txt"},
        approval_provider=provider,
    )
    runtime["db"].connection.execute(
        """
        UPDATE skill_resource_snapshots
        SET inline_text_payload = 'corrupt'
        WHERE resource_path = 'references/guide.txt'
        """
    )
    runtime["db"].connection.commit()
    corrupt = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/guide.txt"},
        approval_provider=provider,
    )

    assert missing.status == "error"
    assert corrupt.status == "error"
    assert missing.error["error_class"] == "config_error"
    assert corrupt.error["error_class"] == "config_error"
    assert provider.requests == []
    runtime["db"].close()


def test_runtime_control_audit_payload_uses_broker_normalized_targets(tmp_path) -> None:
    runtime = _runtime(tmp_path, approval_mode="semi-auto")
    _snapshot_skill(runtime, name="alpha")

    activated = _invoke(runtime, "activate_skill", {"name": "alpha"})
    loaded = _invoke(
        runtime,
        "load_skill_resource",
        {"skill_name": "alpha", "path": "references/guide.txt"},
    )

    completed = [
        event.payload
        for event in runtime["events"].list_for_run("run_1")
        if event.kind == "tool_call_completed"
    ]
    assert activated.status == "ok"
    assert loaded.status == "ok"
    assert completed[0]["target"] == "skill alpha"
    assert completed[1]["target"] == "skill resource alpha:references/guide.txt (reference)"
    assert completed[0]["approval_wait_duration_ms"] == 0
    assert completed[1]["approval_wait_duration_ms"] == 0
    runtime["db"].close()

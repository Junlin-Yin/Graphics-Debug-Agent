from __future__ import annotations

import sys
from pathlib import Path

import pytest

from debug_agent.runtime.config import ConfigError, load_config_snapshot
from debug_agent.runtime.policy import (
    ApprovalGrant,
    NormalizedToolCall,
    PathPolicyEntry,
    PermissionEvaluator,
    ShellPolicy,
    build_builtin_policy,
    canonicalize_path,
    classify_argv_paths,
    load_main_agent_policy,
    policy_facts_from_snapshot,
    policy_facts_to_snapshot,
    normalize_shell_argv,
    scope_signature_for_tool,
)


def test_phase1_context_and_execution_defaults_are_frozen(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.error is None
    assert result.snapshot["context"] == {
        "window_tokens": 200000,
        "omit_old_tool_results_at_ratio": 0.60,
        "compress_history_at_ratio": 0.80,
        "retain_recent_model_calls": 4,
        "compression_reserved_output_tokens": 10000,
    }
    assert result.snapshot["execution"] == {"default_shell_timeout_seconds": 300}


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[context]\nwindow_tokens = 0", "window_tokens"),
        (
            "[context]\nomit_old_tool_results_at_ratio = 0.9\ncompress_history_at_ratio = 0.8",
            "omit_old_tool_results_at_ratio",
        ),
        (
            "[context]\nwindow_tokens = 100\ncompression_reserved_output_tokens = 100",
            "compression_reserved_output_tokens",
        ),
        ("[execution]\ndefault_shell_timeout_seconds = 0", "default_shell_timeout_seconds"),
    ],
)
def test_invalid_context_or_execution_settings_fail_with_config_error(
    tmp_path, monkeypatch, toml, message
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        f"""
[defaults]
provider = "fake"
model = "fake-model"

{toml}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    result = load_config_snapshot()

    assert result.snapshot is None
    assert result.error is not None
    assert result.error.error_class == "config_error"
    assert message in result.error.message


def test_absent_agent_policy_uses_documented_defaults(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))

    policy = load_main_agent_policy(workspace)

    assert policy.error is None
    assert [entry.path for entry in policy.facts.user_path_trust] == [workspace.resolve()]
    assert policy.facts.user_path_deny == []
    assert policy.facts.user_shell.allow == []
    assert policy.facts.user_shell.deny == []
    assert any(entry.raw == ".git/" for entry in policy.facts.builtin_path_deny)
    assert any(entry.raw == "~/.debug-agent/skills/" for entry in policy.facts.builtin_path_deny)


def test_agent_policy_parses_path_and_shell_prefixes(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    agent_dir = home / ".debug-agent"
    agent_dir.mkdir(parents=True)
    workspace.mkdir()
    (agent_dir / "agent.toml").write_text(
        """
[[path_policies]]
scope = "trust"
paths = ["../shared/"]

[[path_policies]]
scope = "deny"
paths = ["secrets/", ".env"]

[shell_policy]
allow = [["uv"], ["python", "-m", "pytest"]]
deny = [["git"]]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    policy = load_main_agent_policy(workspace)

    assert policy.error is None
    assert [entry.raw for entry in policy.facts.user_path_deny] == ["secrets/", ".env"]
    assert policy.facts.user_shell.allow == [("uv",), ("python", "-m", "pytest")]
    assert policy.facts.user_shell.deny == [("git",)]


def test_policy_snapshot_restores_user_path_and_shell_policy(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    facts = build_builtin_policy(workspace, home)
    facts.user_path_deny.append(
        PathPolicyEntry.from_raw("deny", "README.md", workspace, home)
    )
    facts.user_shell = ShellPolicy(deny=[("git",)])
    snapshot = policy_facts_to_snapshot(facts)

    restored = policy_facts_from_snapshot(snapshot, workspace, home)
    evaluator = PermissionEvaluator(restored)

    assert evaluator.classify_path(workspace / "README.md").classification == "denied"
    decision = evaluator.evaluate(
        NormalizedToolCall(
            tool_name="shell_exec",
            category="shell",
            risk_level="execute",
            access=("execute",),
            paths=(workspace,),
            shell_argv=("git", "status"),
            approval_scope_signature="shell_exec|execute|git",
        ),
        approval_mode="yolo",
    )
    assert decision.decision == "deny"
    assert decision.reason == "user_shell_denied"


def test_policy_rejects_invalid_scope_and_regex_shapes(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    agent_dir = home / ".debug-agent"
    agent_dir.mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (agent_dir / "agent.toml").write_text(
        """
[[path_policies]]
scope = "allow"
paths = ["src/"]
""".strip(),
        encoding="utf-8",
    )

    invalid_scope = load_main_agent_policy(workspace)
    assert invalid_scope.error == ConfigError(
        error_class="config_error",
        message="Invalid path policy scope: allow",
        source="config",
        recoverable=True,
    )

    (agent_dir / "agent.toml").write_text(
        """
[shell_policy]
allow_regex = ["git .*"]
""".strip(),
        encoding="utf-8",
    )
    regex_policy = load_main_agent_policy(workspace)
    assert regex_policy.error is not None
    assert regex_policy.error.error_class == "config_error"
    assert "regex" in regex_policy.error.message


def test_path_classification_handles_missing_files_exact_entries_and_symlink_escape(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (workspace / "allowed").mkdir()
    (workspace / "allowed" / "file.txt").write_text("ok", encoding="utf-8")
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "token.txt").write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(workspace / "secrets")

    facts = build_builtin_policy(workspace, home)
    facts.user_path_trust.append(
        PathPolicyEntry.from_raw("trust", str(workspace / "allowed") + "/", workspace, home)
    )
    facts.user_path_deny.append(
        PathPolicyEntry.from_raw("deny", "secrets/", workspace, home)
    )
    facts.user_path_deny.append(
        PathPolicyEntry.from_raw("deny", "allowed/file.txt", workspace, home)
    )
    evaluator = PermissionEvaluator(facts)

    assert evaluator.classify_path(workspace / "allowed" / "new.txt").classification == "trusted"
    assert evaluator.classify_path(workspace / "allowed" / "file.txt").classification == "denied"
    assert evaluator.classify_path(workspace / "link" / "token.txt").classification == "denied"
    assert evaluator.classify_path(workspace / ".git" / "config").classification == "denied"
    assert (
        evaluator.classify_path(workspace / "missing" / ".." / "secrets" / "new.txt").classification
        == "denied"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows path behavior")
def test_windows_absolute_string_paths_use_real_host_paths(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    facts = build_builtin_policy(workspace, tmp_path / "home")
    evaluator = PermissionEvaluator(facts)

    canonical = canonicalize_path(str(workspace), workspace)

    assert canonical == workspace.resolve()
    assert "__debug_agent_windows_drive__" not in str(canonical)
    assert evaluator.classify_path(str(workspace)).classification == "trusted"


def test_shell_normalization_matching_and_argv_path_classification(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert normalize_shell_argv(["/usr/bin/git.exe", "status"]) == ("git", "status")
    assert normalize_shell_argv(["env", "FOO=1", "git.cmd", "status"]) == (
        "git",
        "status",
    )
    shell_policy = ShellPolicy(allow=[("uv",)], deny=[("git",)])
    assert shell_policy.matches_deny(("git", "status"))
    assert shell_policy.matches_allow(("uv", "run", "pytest"))
    assert not shell_policy.matches_allow(("python", "-m", "pytest"))

    classified = classify_argv_paths(
        [
            "./.sessions/tool",
            "--output=dist/out.txt",
            "-I",
            "include",
            "--flag",
            "not/a/path",
        ],
        workspace,
    )
    assert [item.original for item in classified] == [
        "./.sessions/tool",
        "dist/out.txt",
        "include",
        "not/a/path",
    ]
    facts = build_builtin_policy(workspace, tmp_path / "home")
    assert (
        PermissionEvaluator(facts).classify_path(classified[0].path).classification
        == "denied"
    )


@pytest.mark.parametrize(
    ("mode", "risk", "trusted", "expected"),
    [
        ("normal", "read", True, "allow"),
        ("normal", "read", False, "ask"),
        ("normal", "write", True, "ask"),
        ("semi-auto", "write", True, "allow"),
        ("semi-auto", "write", False, "ask"),
        ("yolo", "execute", False, "allow"),
        ("normal", "runtime_control", True, "ask"),
        ("semi-auto", "runtime_control", True, "allow"),
    ],
)
def test_permission_evaluator_mode_matrix(mode, risk, trusted, expected, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    facts = build_builtin_policy(workspace, tmp_path / "home")
    call = NormalizedToolCall(
        tool_name="read_file",
        category="native" if risk != "runtime_control" else "runtime_control",
        risk_level=risk,
        access=(risk,),
        paths=(workspace / "file.txt",) if trusted else (tmp_path / "other.txt",),
        shell_argv=(),
        approval_scope_signature=f"{risk}:scope",
    )

    decision = PermissionEvaluator(facts).evaluate(call, approval_mode=mode)

    assert decision.decision == expected


def test_permission_evaluator_hard_denies_and_reusable_grants(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    facts = build_builtin_policy(workspace, tmp_path / "home")
    call = NormalizedToolCall(
        tool_name="write_file",
        category="native",
        risk_level="write",
        access=("write",),
        paths=(workspace / ".sessions" / "state.txt",),
        shell_argv=(),
        approval_scope_signature="write:/repo/.sessions/state.txt",
    )

    denied = PermissionEvaluator(facts).evaluate(call, approval_mode="yolo")
    assert denied.decision == "deny"
    assert denied.error_class == "policy_denied"

    approved_call = NormalizedToolCall(
        tool_name="write_file",
        category="native",
        risk_level="write",
        access=("write",),
        paths=(workspace / "state.txt",),
        shell_argv=(),
        approval_scope_signature="write:/repo/state.txt",
    )
    grant = ApprovalGrant(
        session_id="sess_1",
        tool_name="write_file",
        risk_level="write",
        scope_signature="write:/repo/state.txt",
    )
    approved = PermissionEvaluator(facts).evaluate(
        approved_call,
        approval_mode="normal",
        reusable_grants=[grant],
        session_id="sess_1",
    )
    assert approved.decision == "allow"
    assert approved.reason == "approval_grant"


def test_scope_signatures_are_narrow_and_deterministic(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "src" / "app.py"
    shell_cwd = workspace

    write_signature = scope_signature_for_tool(
        "write_file",
        risk_level="write",
        paths=[file_path],
    )
    edit_signature = scope_signature_for_tool(
        "edit_file",
        risk_level="write",
        paths=[file_path],
    )
    assert write_signature == f"write_file|write|write:{file_path.resolve()}"
    assert edit_signature == f"edit_file|write|write:{file_path.resolve()}"
    assert write_signature != f"write_file|write|write:{file_path.parent.resolve()}"

    shell_signature = scope_signature_for_tool(
        "shell_exec",
        risk_level="execute",
        shell_argv=("uv", "run", "pytest"),
        cwd=shell_cwd,
        effective_timeout_seconds=300,
        classified_paths=[workspace / "tests"],
    )
    assert shell_signature == (
        f"shell_exec|execute|argv:uv\\x00run\\x00pytest|cwd:{shell_cwd.resolve()}|"
        f"timeout:300|paths:{(workspace / 'tests').resolve()}"
    )

    assert scope_signature_for_tool(
        "activate_skill",
        risk_level="runtime_control",
        skill_name="debugging",
        skill_content_hash="sha256:abc",
    ) == "activate_skill|runtime_control|skill:debugging|skill_hash:sha256:abc"
    assert scope_signature_for_tool(
        "load_skill_ref_file",
        risk_level="read",
        skill_name="debugging",
        skill_content_hash="sha256:abc",
        reference_path="references/a.md",
        reference_content_hash="sha256:def",
    ) == (
        "load_skill_ref_file|read|skill:debugging|skill_hash:sha256:abc|"
        "ref:references/a.md|ref_hash:sha256:def"
    )

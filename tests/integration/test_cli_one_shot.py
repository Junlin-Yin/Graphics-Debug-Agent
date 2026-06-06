from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _subprocess_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home)}


def test_debug_agent_one_shot_completes_with_fake_model(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    workspace.mkdir()
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "integration answer"
""".strip(),
        encoding="utf-8",
    )

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable, "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "integration answer\n"
    db_path = workspace / ".sessions" / "runtime.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] >= 1
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "completed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "completed"
        terminal_reason, terminal_error = conn.execute(
            "SELECT terminal_reason, terminal_error_json FROM runs"
        ).fetchone()
        assert (terminal_reason, terminal_error) == ("terminal_completion", None)
        durable_rows = conn.execute(
            """
            SELECT message_index, role, kind, content_json
            FROM conversation_messages
            ORDER BY message_index
            """
        ).fetchall()
        assert [
            (index, role, kind, content)
            for index, role, kind, content in durable_rows
        ] == [
            (1, "user", "user_input", '{"content":"hello"}'),
            (2, "assistant", "assistant_output", '{"content":"integration answer"}'),
        ]
        checkpoint_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM checkpoints ORDER BY rowid")
        ]
        assert checkpoint_kinds == ["terminal_recovery"]


def test_debug_agent_one_shot_semi_auto_skill_activation_is_audit_only(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    workspace.mkdir()
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: alpha
description: Alpha prompt skill
---

Use alpha.
""",
        encoding="utf-8",
    )
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "skill activated"
fake_tool_calls = [
  {name = "activate_skill", args = {name = "alpha"}, id = "call_alpha"}
]
""".strip(),
        encoding="utf-8",
    )

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable, "--approval-mode", "semi-auto", "-p", "activate alpha"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "skill activated\n"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert (
            conn.execute("SELECT approval_mode FROM sessions").fetchone()[0]
            == "semi-auto"
        )
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
        active_skills_json = conn.execute(
            "SELECT active_skills_json FROM runs"
        ).fetchone()[0]
    assert "skill_activated" in event_kinds
    assert "approval_requested" not in event_kinds
    assert "approval_decision_recorded" not in event_kinds
    assert "alpha" in active_skills_json


def test_debug_agent_one_shot_non_interactive_approval_denial_terminalizes(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    workspace.mkdir()
    outside.write_text("outside", encoding="utf-8")
    (config_dir / "config.toml").write_text(
        f"""
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "must not be printed"
fake_tool_calls = [
  {{name = "read_file", args = {{path = "{outside.as_posix()}", limit = 10}}, id = "call_read"}}
]
""".strip(),
        encoding="utf-8",
    )

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable, "-p", "read outside"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        assert conn.execute("SELECT active_run_id FROM sessions").fetchone()[0] is None
        failed_error = conn.execute(
            """
            SELECT
              json_extract(payload_json, '$.error_class'),
              json_extract(payload_json, '$.reason')
            FROM run_events
            WHERE kind = 'run_failed'
            """
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM approval_grants").fetchone()[0] == 0
        event_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM run_events ORDER BY rowid")
        ]
    assert failed_error == ("policy_error", "approval_required_non_interactive")
    assert "approval_requested" not in event_kinds
    assert "approval_decision_recorded" not in event_kinds


def test_debug_agent_one_shot_model_cancellation_records_terminal_failure(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    workspace.mkdir()
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_cancelled = true
""".strip(),
        encoding="utf-8",
    )

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable, "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "fake model cancelled" in result.stderr
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        assert conn.execute("SELECT active_run_id FROM sessions").fetchone()[0] is None
        assert conn.execute("SELECT kind FROM checkpoints").fetchone()[0] == "terminal_recovery"


def test_debug_agent_one_shot_model_timeout_records_terminal_failure(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    workspace.mkdir()
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_timeout = true
""".strip(),
        encoding="utf-8",
    )

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable, "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "fake model timeout" in result.stderr
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        assert conn.execute("SELECT active_run_id FROM sessions").fetchone()[0] is None
        assert conn.execute("SELECT kind FROM checkpoints").fetchone()[0] == "terminal_recovery"

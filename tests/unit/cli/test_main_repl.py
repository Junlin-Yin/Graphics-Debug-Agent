from __future__ import annotations

import io
import sqlite3

import pytest
import debug_agent.cli.main as cli_main
from debug_agent.cli.main import main
from debug_agent.cli.exit_codes import INTERRUPTED
from debug_agent.cli.repl import PlainApprovalProvider, run_repl
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_plain_approval_provider_maps_y_a_n_and_renders_prompt() -> None:
    rendered = io.StringIO()
    once = PlainApprovalProvider(
        input_stream=io.StringIO("y\n"),
        output_stream=rendered,
    ).request_approval("Tool: read_file", {})
    session = PlainApprovalProvider(
        input_stream=io.StringIO("a\n"),
        output_stream=io.StringIO(),
    ).request_approval("Tool: write_file", {})
    denied = PlainApprovalProvider(
        input_stream=io.StringIO("n\n"),
        output_stream=io.StringIO(),
    ).request_approval("Tool: shell_exec", {})

    assert rendered.getvalue() == "Tool: read_file\n"
    assert (once.decision, once.grant_scope) == ("approved_once", "once")
    assert (session.decision, session.grant_scope) == (
        "approved_for_session",
        "session",
    )
    assert (denied.decision, denied.grant_scope) == ("denied", "none")


def _write_fake_config(
    home,
    response: str = "repl answer",
    *,
    error: str | None = None,
) -> None:
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    fake_error = f'\nfake_error = "{error}"' if error else ""
    (config_dir / "config.toml").write_text(
        f"""
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "{response}"{fake_error}
""".strip(),
        encoding="utf-8",
    )


def test_main_repl_accepts_two_turns_status_and_exit(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "turn answer")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("hello\n/status\ntell me one more thing\n/exit\n"),
    )

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.count("turn answer\n") == 2
    assert "session_id:" in captured.out
    assert "approval_mode: normal" in captured.out

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, approval_mode, active_run_id = conn.execute(
            "SELECT status, approval_mode, active_run_id FROM sessions"
        ).fetchone()
        run_status = conn.execute("SELECT status FROM runs").fetchone()[0]
        user_messages = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE kind = 'user_message'"
        ).fetchone()[0]
        checkpoint_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM checkpoints ORDER BY rowid")
        ]

    assert (session_status, approval_mode, active_run_id) == (
        "completed",
        "normal",
        None,
    )
    assert run_status == "completed"
    assert user_messages == 2
    assert checkpoint_kinds == ["terminal_recovery"]


def test_main_repl_fresh_phase3_startup_no_longer_requires_development_gate(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "fresh repl")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n"))

    exit_code = main([])

    assert exit_code == 0
    assert capsys.readouterr().out == "fresh repl\n"
    assert (workspace / ".sessions" / "runtime.db").exists()


def test_main_repl_accepts_explicit_initial_approval_mode(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "unused")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("sys.stdin", io.StringIO("/status\n/exit\n"))

    exit_code = main(["--approval-mode", "semi-auto"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "approval_mode: semi-auto" in captured.out
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert (
            conn.execute("SELECT approval_mode FROM sessions").fetchone()[0]
            == "semi-auto"
        )


def test_main_repl_rejects_invalid_initial_approval_mode(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "unused")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    exit_code = main(["--approval-mode", "auto"])

    assert exit_code == 2
    assert "approval mode must be one of: normal, semi-auto, yolo" in capsys.readouterr().err
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_repl_status_and_exit_do_not_call_model(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, error="model should not be invoked")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("sys.stdin", io.StringIO("/status\n/exit\n"))

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "session_id:" in captured.out
    assert "model should not be invoked" not in captured.err
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        model_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE kind LIKE 'model_call_%'"
        ).fetchone()[0]
    assert model_events == 0


def test_repl_model_failure_returns_runtime_error(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, error="provider failed")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n"))

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "provider failed" in captured.out
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "failed"


def test_repl_rejects_ordinary_input_while_execution_is_active(
    tmp_path, monkeypatch
) -> None:
    from debug_agent.cli.repl import ReplController

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = ReplController.start(
        config_snapshot={
            "provider": "fake",
            "model": "fake-model",
            "fake_response": "unused",
            "temperature": 0.2,
            "max_tokens": 8192,
            "timeout_seconds": 120,
            "system_prompt": (
                "You are debug-agent, a local debugging assistant. Answer concisely "
                "and use only tools exposed by the runtime."
            ),
            "development": {
                "allow_incomplete_phase3_prompt_execution": True,
            },
        },
        workspace_root=workspace,
    )
    controller.is_executing = True
    output = io.StringIO()

    try:
        should_continue = controller.handle_line("hello", output)
    finally:
        controller.close()

    assert should_continue is True
    assert "Prompt run is already executing." in output.getvalue()


def test_repl_ctrl_c_after_session_creation_marks_failed_and_releases_ownership(
    tmp_path,
) -> None:
    class InterruptingInput:
        def __iter__(self):
            return self

        def __next__(self) -> str:
            raise KeyboardInterrupt

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_snapshot = {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": "unused",
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer concisely "
            "and use only tools exposed by the runtime."
        ),
        "development": {
            "allow_incomplete_phase3_prompt_execution": True,
        },
    }

    try:
        exit_code = run_repl(
            config_snapshot,
            input_stream=InterruptingInput(),
            output_stream=io.StringIO(),
            error_stream=io.StringIO(),
            workspace_root=workspace,
        )
    except KeyboardInterrupt:
        pytest.fail("REPL Ctrl+C must be recorded as terminal failed state")

    assert exit_code == INTERRUPTED
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id, session_error = conn.execute(
            "SELECT status, active_run_id, error_summary FROM sessions"
        ).fetchone()
        run_status, run_error = conn.execute(
            "SELECT status, error_summary FROM runs"
        ).fetchone()
        checkpoint_kind, checkpoint_state = conn.execute(
            "SELECT kind, state_json FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        failed_error_class = conn.execute(
            """
            SELECT json_extract(payload_json, '$.error_class')
            FROM run_events
            WHERE kind = 'session_failed'
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()[0]

    assert (session_status, active_run_id, run_status) == ("failed", None, "failed")
    assert session_error == "REPL interrupted by Ctrl+C."
    assert run_error == "REPL interrupted by Ctrl+C."
    assert checkpoint_kind == "terminal_recovery"
    assert '"terminal_reason": "user_cancel_idle"' in checkpoint_state
    assert '"reason": "user_cancel_idle"' in checkpoint_state
    assert failed_error_class == "cancelled"

    second_exit = run_repl(
        config_snapshot,
        input_stream=io.StringIO("/exit\n"),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
        workspace_root=workspace,
    )
    assert second_exit == 0


def test_main_ctrl_c_fallback_is_raw_process_interrupt_without_terminalizing_active_session(
    tmp_path, monkeypatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    db = RuntimeDatabase.bootstrap(workspace)
    try:
        sessions = SessionStore(db.connection)
        runs = RunStore(db.connection)
        session = sessions.create(
            workspace_root=workspace,
            approval_mode="yolo",
            config_snapshot={"provider": "fake"},
            session_id="sess_interrupt",
        )
        run = runs.create_prompt_run(session.session_id, run_id="run_interrupt")
        sessions.set_active_run(session.session_id, run.run_id)
    finally:
        db.close()

    def interrupt(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "_main", interrupt)

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == INTERRUPTED
    assert captured.err == "Interrupted by Ctrl+C.\n"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id, session_error = conn.execute(
            "SELECT status, active_run_id, error_summary FROM sessions"
        ).fetchone()
        run_status, run_error = conn.execute(
            "SELECT status, error_summary FROM runs"
        ).fetchone()
        session_failed = conn.execute(
            """
            SELECT 1
            FROM run_events
            WHERE kind = 'session_failed'
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()

    assert (session_status, active_run_id, run_status) == (
        "running",
        "run_interrupt",
        "running",
    )
    assert session_error is None
    assert run_error is None
    assert session_failed is None


def test_tty_repl_ctrl_c_marks_failed_and_releases_ownership(
    tmp_path, monkeypatch
) -> None:
    from debug_agent.cli import repl as repl_module

    class InterruptingPromptToolkitView:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, controller) -> int:
            controller.on_interrupt()
            return controller.exit_code

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_snapshot = {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": "unused",
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer concisely "
            "and use only tools exposed by the runtime."
        ),
        "development": {
            "allow_incomplete_phase3_prompt_execution": True,
        },
    }
    monkeypatch.setattr(
        repl_module, "PromptToolkitReplView", InterruptingPromptToolkitView
    )
    monkeypatch.setattr("sys.stdin", TtyStringIO(""))
    monkeypatch.setattr("sys.stdout", TtyStringIO())

    exit_code = run_repl(
        config_snapshot,
        error_stream=io.StringIO(),
        workspace_root=workspace,
    )

    assert exit_code == INTERRUPTED
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        session_status, active_run_id, session_error = conn.execute(
            "SELECT status, active_run_id, error_summary FROM sessions"
        ).fetchone()
        run_status, run_error = conn.execute(
            "SELECT status, error_summary FROM runs"
        ).fetchone()
        failed_error_class = conn.execute(
            """
            SELECT json_extract(payload_json, '$.error_class')
            FROM run_events
            WHERE kind = 'session_failed'
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()[0]

    assert (session_status, active_run_id, run_status) == ("failed", None, "failed")
    assert session_error == "REPL interrupted by Ctrl+C."
    assert run_error == "REPL interrupted by Ctrl+C."
    assert failed_error_class == "cancelled"

    second_exit = run_repl(
        config_snapshot,
        input_stream=io.StringIO("/exit\n"),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
        workspace_root=workspace,
    )
    assert second_exit == 0

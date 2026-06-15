from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from debug_agent.cli import main as main_module
from debug_agent.cli.main import main


class _TTYStringIO(StringIO):
    def isatty(self) -> bool:
        return True


class _NonTTYStringIO(StringIO):
    def isatty(self) -> bool:
        return False


def _write_fake_config(home, response: str = "cli answer") -> None:
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        f"""
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "{response}"
""".strip(),
        encoding="utf-8",
    )


def test_main_one_shot_prints_fake_answer_and_returns_zero(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "hello from cli")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    exit_code = main(["-p", "hello"])

    assert exit_code == 0
    assert capsys.readouterr().out == "hello from cli\n"
    assert (workspace / ".sessions" / "runtime.db").is_file()


def test_main_one_shot_accepts_explicit_approval_mode(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "hello from semi-auto")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    exit_code = main(["--approval-mode", "semi-auto", "-p", "hello"])

    assert exit_code == 0
    assert capsys.readouterr().out == "hello from semi-auto\n"


def test_main_rejects_invalid_one_shot_approval_mode(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    exit_code = main(["--approval-mode", "auto", "-p", "hello"])

    assert exit_code == 2
    assert "approval mode must be one of: normal, semi-auto, yolo" in capsys.readouterr().err
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_main_returns_usage_error_for_missing_or_unsupported_args(capsys) -> None:
    assert main(["status"]) == 2
    assert main(["unknown", "sess_1"]) == 2
    assert main(["--unknown"]) == 2
    assert main(["-p"]) == 2
    usage = capsys.readouterr().err
    assert "Usage:" in usage
    assert "debug-agent [--approval-mode normal|semi-auto|yolo]  # REPL" in usage


def test_main_help_prints_multiline_usage_to_stdout(capsys) -> None:
    assert main(["--help"]) == 0
    assert main(["--approval-mode", "semi-auto", "-h"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("Usage:\n") == 2
    for usage in captured.out.split("Usage:\n")[1:]:
        assert "  debug-agent [--approval-mode normal|semi-auto|yolo]  # REPL\n" in usage
        assert '  debug-agent [--approval-mode normal|semi-auto|yolo] -p "prompt"\n' in usage
        assert "  debug-agent status <session_id>\n" in usage
        assert "  debug-agent trace <session_id>\n" in usage
        assert "  debug-agent resume <session_id>\n" in usage


def test_main_returns_config_error_without_creating_session(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    exit_code = main(["-p", "hello"])

    assert exit_code == 4
    assert "Provider and model must be configured" in capsys.readouterr().err
    assert not (workspace / ".sessions" / "runtime.db").exists()


def test_main_invalid_phase35_config_does_not_touch_existing_runtime_db(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    db_dir = workspace / ".sessions"
    db_dir.mkdir()
    db_path = db_dir / "runtime.db"
    db_path.write_text("legacy-db-sentinel", encoding="utf-8")
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"

[agent_loop]
max_tool_call_iterations = true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    exit_code = main(["-p", "hello"])

    assert exit_code == 4
    assert "agent_loop.max_tool_call_iterations" in capsys.readouterr().err
    assert db_path.read_text(encoding="utf-8") == "legacy-db-sentinel"


def test_main_one_shot_fresh_phase3_startup_no_longer_requires_development_gate(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "fresh startup")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    exit_code = main(["-p", "hello"])

    assert exit_code == 0
    assert capsys.readouterr().out == "fresh startup\n"
    assert (workspace / ".sessions" / "runtime.db").exists()


def test_main_one_shot_interactive_stale_confirmation_is_passed_to_orchestrator(
    monkeypatch,
) -> None:
    seen = {}

    class FakeOrchestrator:
        def __init__(self, *, stale_confirmation=None, **_kwargs):
            self.stale_confirmation = stale_confirmation
            seen["has_confirmation"] = stale_confirmation is not None

        def run_one_shot(self, prompt, config_snapshot, *, approval_mode="normal"):
            approved = self.stale_confirmation(
                {
                    "session_id": "sess_old",
                    "run_id": "run_old",
                    "evidence": {
                        "host_match": True,
                        "pid_absent": True,
                        "owner_token_present": True,
                    },
                }
            )
            return SimpleNamespace(
                exit_code=0 if approved else 3,
                message="accepted" if approved else "rejected",
                error=None,
                session_id=None,
            )

    monkeypatch.setattr(main_module, "RuntimeOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        main_module,
        "load_config_snapshot",
        lambda: SimpleNamespace(error=None, snapshot={"provider": "fake"}),
    )
    stdin = _TTYStringIO("y\n")
    stdout = _TTYStringIO()
    monkeypatch.setattr(main_module.sys, "stdin", stdin)
    monkeypatch.setattr(main_module.sys, "stdout", stdout)

    exit_code = main(["-p", "hello"])

    assert exit_code == 0
    assert seen == {"has_confirmation": True}
    output = stdout.getvalue()
    assert "Stale session is still taking the ownership: sess_old." in output
    assert "run: run_old" not in output
    assert "stale evidence:" not in output
    assert "accepted" in output


def test_main_one_shot_non_interactive_stale_confirmation_is_unavailable(
    monkeypatch,
) -> None:
    seen = {}

    class FakeOrchestrator:
        def __init__(self, *, stale_confirmation=None, **_kwargs):
            seen["has_confirmation"] = stale_confirmation is not None

        def run_one_shot(self, prompt, config_snapshot, *, approval_mode="normal"):
            return SimpleNamespace(
                exit_code=3,
                message="confirmation unavailable",
                error=None,
                session_id=None,
            )

    monkeypatch.setattr(main_module, "RuntimeOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        main_module,
        "load_config_snapshot",
        lambda: SimpleNamespace(error=None, snapshot={"provider": "fake"}),
    )
    monkeypatch.setattr(main_module.sys, "stdin", _NonTTYStringIO(""))
    monkeypatch.setattr(main_module.sys, "stdout", _NonTTYStringIO())

    exit_code = main(["-p", "hello"])

    assert exit_code == 3
    assert seen == {"has_confirmation": False}


def test_main_keyboard_interrupt_uses_raw_process_interrupt_fallback(
    monkeypatch, capsys
) -> None:
    def raise_keyboard_interrupt(_args):
        raise KeyboardInterrupt

    class ForbiddenOrchestrator:
        def __init__(self, **_kwargs):
            raise AssertionError("top-level KeyboardInterrupt must not create orchestrator")

    monkeypatch.setattr(main_module, "_main", raise_keyboard_interrupt)
    monkeypatch.setattr(main_module, "RuntimeOrchestrator", ForbiddenOrchestrator)

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == "Interrupted by Ctrl+C.\n"


def test_main_one_shot_failure_without_session_uses_existing_error_path(
    monkeypatch, capsys
) -> None:
    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def run_one_shot(self, prompt, config_snapshot, *, approval_mode="normal"):
            return SimpleNamespace(
                exit_code=4,
                message="startup failed",
                error={
                    "error_class": "config_error",
                    "reason": "startup_schema_validation_failed",
                    "message": "startup failed",
                },
                session_id=None,
            )

    monkeypatch.setattr(main_module, "RuntimeOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        main_module,
        "load_config_snapshot",
        lambda: SimpleNamespace(error=None, snapshot={"provider": "fake"}),
    )

    exit_code = main(["-p", "hello"])

    captured = capsys.readouterr()
    assert exit_code == 4
    assert captured.out == ""
    assert captured.err == "startup failed\n"


def test_main_one_shot_failure_with_session_formats_summary(monkeypatch, capsys) -> None:
    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def run_one_shot(self, prompt, config_snapshot, *, approval_mode="normal"):
            return SimpleNamespace(
                exit_code=1,
                message="legacy message must not be used",
                error={
                    "schema_version": 1,
                    "error_class": "model_error",
                    "reason": "model_call_timeout",
                    "message": "model call timed out",
                    "scope": "provider",
                    "recoverability": "terminal_recoverable",
                    "metadata": {},
                    "artifact_ids": [],
                },
                session_id="sess_123",
                terminal_failure_summary=True,
            )

    monkeypatch.setattr(main_module, "RuntimeOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        main_module,
        "load_config_snapshot",
        lambda: SimpleNamespace(error=None, snapshot={"provider": "fake"}),
    )

    exit_code = main(["-p", "hello"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        "\n"
        "One-shot session sess_123 failed.\n"
        "model_error/model_call_timeout: model call timed out\n"
        "trace: .sessions/sess_123/logs/trace.md\n"
        "resume: debug-agent resume sess_123\n"
    )


def test_main_one_shot_active_session_conflict_with_session_uses_raw_error_path(
    monkeypatch, capsys
) -> None:
    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def run_one_shot(self, prompt, config_snapshot, *, approval_mode="normal"):
            return SimpleNamespace(
                exit_code=3,
                message="active session conflict for sess_active",
                error={
                    "schema_version": 1,
                    "error_class": "policy_error",
                    "reason": "workspace_owner_active",
                    "message": "active session conflict for sess_active",
                    "scope": "startup",
                    "recoverability": "non_recoverable",
                    "metadata": {},
                    "artifact_ids": [],
                },
                session_id="sess_active",
                terminal_failure_summary=False,
            )

    monkeypatch.setattr(main_module, "RuntimeOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        main_module,
        "load_config_snapshot",
        lambda: SimpleNamespace(error=None, snapshot={"provider": "fake"}),
    )

    exit_code = main(["-p", "hello"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.out == ""
    assert captured.err == "active session conflict for sess_active\n"


def test_main_one_shot_summary_requires_complete_normalized_error_fields(
    monkeypatch, capsys
) -> None:
    class FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def run_one_shot(self, prompt, config_snapshot, *, approval_mode="normal"):
            return SimpleNamespace(
                exit_code=1,
                message="raw failure message",
                error={"error_class": "model_error", "message": "incomplete"},
                session_id="sess_incomplete",
                terminal_failure_summary=True,
            )

    monkeypatch.setattr(main_module, "RuntimeOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        main_module,
        "load_config_snapshot",
        lambda: SimpleNamespace(error=None, snapshot={"provider": "fake"}),
    )

    exit_code = main(["-p", "hello"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "raw failure message\n"

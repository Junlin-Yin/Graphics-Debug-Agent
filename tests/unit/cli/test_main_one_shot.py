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
    usage = capsys.readouterr().err
    assert "Usage:" in usage
    assert "debug-agent [--approval-mode normal|semi-auto|yolo]  # REPL" in usage


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
    assert "Active owner appears stale." in output
    assert "session: sess_old" in output
    assert "stale evidence: same host, owner pid absent, owner token captured" in output
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

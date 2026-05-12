from __future__ import annotations

from debug_agent.cli.main import main


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


def test_main_returns_usage_error_for_missing_or_unsupported_args(capsys) -> None:
    assert main(["status"]) == 2
    assert main(["unknown", "sess_1"]) == 2
    assert "Usage:" in capsys.readouterr().err


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

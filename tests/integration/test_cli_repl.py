from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import debug_agent.cli.repl as repl_module
from debug_agent.cli.main import main
from debug_agent.cli.repl_controller import ReplController
from debug_agent.cli.repl_view import ReplViewEvent
from debug_agent.cli.repl import run_repl
from debug_agent.cli.plain_repl_view import PlainReplView


def _subprocess_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home)}


def _write_fake_config(home: Path, response: str = "integration repl answer") -> None:
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


def _fake_config_snapshot(
    response: str = "integration repl answer",
    *,
    stream_chunks: list[str] | None = None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "provider": "fake",
        "model": "fake-model",
        "fake_response": response,
        "temperature": 0.2,
        "max_tokens": 8192,
        "timeout_seconds": 120,
        "system_prompt": (
            "You are debug-agent, a local debugging assistant. Answer concisely "
            "and use only tools exposed by the runtime."
        ),
    }
    if stream_chunks is not None:
        snapshot["fake_stream_chunks"] = stream_chunks
    return snapshot


class FakeControllerView:
    def __init__(self) -> None:
        self.user_messages: list[str] = []
        self.input_enabled: list[bool] = []
        self.events: list[ReplViewEvent] = []
        self.turn_statuses: list[tuple[int, str, int]] = []
        self.status_bars: list[object] = []
        self.closed_summaries: list[object] = []
        self.errors: list[str] = []

    def run(self, controller: object) -> int:
        return 0

    def show_welcome(self, snapshot: object) -> None:
        pass

    def set_input_enabled(self, enabled: bool) -> None:
        self.input_enabled.append(enabled)

    def append_user_message(self, message: str) -> None:
        self.user_messages.append(message)

    def append_view_event(self, event: ReplViewEvent) -> None:
        self.events.append(event)

    def set_turn_status(
        self, turn_id: int, status: str, elapsed_seconds: int
    ) -> None:
        self.turn_statuses.append((turn_id, status, elapsed_seconds))

    def update_status_bar(self, snapshot: object) -> None:
        self.status_bars.append(snapshot)

    def show_session_closed(self, summary: object) -> None:
        self.closed_summaries.append(summary)

    def show_error(self, message: str) -> None:
        self.errors.append(message)


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_debug_agent_repl_accepts_two_turns_status_and_exit(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_fake_config(home)

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable],
        cwd=workspace,
        env=_subprocess_env(home),
        input="hello\n/status\ntell me one more thing\n/exit\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.count("integration repl answer\n") == 2
    assert "session_id:" in result.stdout
    db_path = workspace / ".sessions" / "runtime.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "completed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "completed"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE kind = 'user_message'"
            ).fetchone()[0]
            == 2
        )
        checkpoint_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM checkpoints ORDER BY rowid")
        ]
    assert checkpoint_kinds == ["turn", "turn", "terminal"]


def test_non_streaming_repl_controller_completes_fake_model_turn(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    view = FakeControllerView()
    controller = ReplController.start(
        config_snapshot=_fake_config_snapshot("controller answer"),
        workspace_root=workspace,
        view=view,
    )

    try:
        controller.on_submit("hello")
        controller.wait_for_active_turn(timeout=2)
        assert controller.drain_completed_turns() == 1
    finally:
        controller.close()

    assert view.user_messages == ["hello"]
    assert view.input_enabled == [False, True]
    assert ReplViewEvent(
        kind="model_markdown_final",
        payload={"text": "controller answer"},
    ) in view.events
    assert view.turn_statuses[-1][1] == "completed"
    assert view.status_bars[-1].model == "fake-model"
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE kind = 'assistant_message'"
            ).fetchone()[0]
            == 1
        )


def test_streaming_repl_controller_renders_fake_model_deltas(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    view = FakeControllerView()
    controller = ReplController.start(
        config_snapshot=_fake_config_snapshot(
            "unused",
            stream_chunks=["hel", "lo"],
        ),
        workspace_root=workspace,
        view=view,
    )

    try:
        controller.on_submit("hello")
        controller.wait_for_active_turn(timeout=2)
        controller.drain_stream_events()
        assert controller.drain_completed_turns() == 1
    finally:
        controller.close()

    assert ReplViewEvent(
        kind="model_text_delta",
        payload={"model_call_id": "model_call_1", "text": "hel"},
    ) in view.events
    assert ReplViewEvent(
        kind="model_text_delta",
        payload={"model_call_id": "model_call_1", "text": "lo"},
    ) in view.events
    assert ReplViewEvent(
        kind="model_markdown_final",
        payload={"model_call_id": "model_call_1", "text": "hello"},
    ) in view.events
    assert view.turn_statuses[-1][1] == "completed"


def test_streaming_repl_controller_warns_on_non_streaming_fallback(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    view = FakeControllerView()
    controller = ReplController.start(
        config_snapshot=_fake_config_snapshot("fallback answer"),
        workspace_root=workspace,
        view=view,
    )

    try:
        controller.on_submit("hello")
        controller.wait_for_active_turn(timeout=2)
        controller.drain_stream_events()
        assert controller.drain_completed_turns() == 1
    finally:
        controller.close()

    assert ReplViewEvent(
        kind="system_message",
        payload={
            "message": "streaming unavailable for this model; using non-streaming response."
        },
    ) in view.events
    assert ReplViewEvent(
        kind="model_markdown_final",
        payload={"text": "fallback answer"},
    ) in view.events


def test_streaming_repl_controller_rejects_active_prompt(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    view = FakeControllerView()
    controller = ReplController.start(
        config_snapshot=_fake_config_snapshot(
            "unused",
            stream_chunks=["slow answer"],
        ),
        workspace_root=workspace,
        view=view,
    )

    try:
        controller.is_executing = True
        controller.on_submit("second")
    finally:
        controller.close()

    assert view.events[-1].kind == "system_message"
    assert "already executing" in view.events[-1].payload["message"]


def test_injected_io_repl_uses_plain_repl_view(tmp_path, monkeypatch) -> None:
    constructed: list[type[PlainReplView]] = []

    class RecordingPlainReplView(PlainReplView):
        def __init__(self, **kwargs) -> None:
            constructed.append(type(self))
            super().__init__(**kwargs)

    monkeypatch.setattr(repl_module, "PlainReplView", RecordingPlainReplView)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = io.StringIO()

    exit_code = run_repl(
        _fake_config_snapshot("injected answer"),
        input_stream=io.StringIO("hello\n/status\n/exit\n"),
        output_stream=output,
        error_stream=io.StringIO(),
        workspace_root=workspace,
    )

    assert exit_code == 0
    assert constructed == [RecordingPlainReplView]
    assert "injected answer\n" in output.getvalue()
    assert "session_id:" in output.getvalue()
    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "completed"


def test_tty_repl_selects_prompt_toolkit_view(tmp_path, monkeypatch) -> None:
    constructed: list[type[object]] = []

    class RecordingPromptToolkitReplView:
        def __init__(self, **kwargs) -> None:
            constructed.append(type(self))

        def run(self, controller) -> int:
            controller.runtime.complete()
            return 0

    monkeypatch.setattr(
        repl_module, "PromptToolkitReplView", RecordingPromptToolkitReplView
    )
    monkeypatch.setattr("sys.stdin", TtyStringIO("/exit\n"))
    monkeypatch.setattr("sys.stdout", TtyStringIO())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    exit_code = run_repl(
        _fake_config_snapshot("tty answer"),
        error_stream=io.StringIO(),
        workspace_root=workspace,
    )

    assert exit_code == 0
    assert constructed == [RecordingPromptToolkitReplView]


def test_prompt_toolkit_initialization_failure_falls_back_to_plain_view(
    tmp_path, monkeypatch
) -> None:
    constructed_plain: list[type[PlainReplView]] = []

    class FailingPromptToolkitReplView:
        def __init__(self, **kwargs) -> None:
            raise RuntimeError("terminal unavailable")

    class RecordingPlainReplView(PlainReplView):
        def __init__(self, **kwargs) -> None:
            constructed_plain.append(type(self))
            super().__init__(**kwargs)

    monkeypatch.setattr(repl_module, "PromptToolkitReplView", FailingPromptToolkitReplView)
    monkeypatch.setattr(repl_module, "PlainReplView", RecordingPlainReplView)
    monkeypatch.setattr("sys.stdin", TtyStringIO("/exit\n"))
    monkeypatch.setattr("sys.stdout", TtyStringIO())
    error_stream = io.StringIO()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    exit_code = run_repl(
        _fake_config_snapshot("fallback answer"),
        error_stream=error_stream,
        workspace_root=workspace,
    )

    assert exit_code == 0
    assert constructed_plain == [RecordingPlainReplView]
    assert error_stream.getvalue().count("falling back to plain REPL") == 1


def test_non_tty_repl_uses_plain_repl_view(tmp_path, monkeypatch, capsys) -> None:
    constructed: list[type[PlainReplView]] = []

    class RecordingPlainReplView(PlainReplView):
        def __init__(self, **kwargs) -> None:
            constructed.append(type(self))
            super().__init__(**kwargs)

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "non tty answer")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n/status\n/exit\n"))
    monkeypatch.setattr(repl_module, "PlainReplView", RecordingPlainReplView)

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert constructed == [RecordingPlainReplView]
    assert "non tty answer\n" in captured.out
    assert "session_id:" in captured.out


def test_one_shot_does_not_construct_plain_repl_view(
    tmp_path, monkeypatch, capsys
) -> None:
    class FailingPlainReplView(PlainReplView):
        def __init__(self, **kwargs) -> None:
            raise AssertionError("one-shot must not construct a ReplView")

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_fake_config(home, "one shot still plain")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(repl_module, "PlainReplView", FailingPlainReplView)

    exit_code = main(["-p", "hello"])

    assert exit_code == 0
    assert capsys.readouterr().out == "one shot still plain\n"

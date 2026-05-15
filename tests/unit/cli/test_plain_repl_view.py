from __future__ import annotations

import io

from debug_agent.cli.plain_repl_view import PlainReplView


class FakeRuntime:
    def __init__(self) -> None:
        self.completed = False

    def complete(self) -> None:
        self.completed = True


class FakeController:
    def __init__(self, *, stop_after: int | None = None, exit_code: int = 0) -> None:
        self.runtime = FakeRuntime()
        self.exit_code = exit_code
        self.lines: list[str] = []
        self.outputs: list[io.StringIO] = []
        self._stop_after = stop_after

    def handle_line(self, line: str, output: io.StringIO) -> bool:
        self.lines.append(line)
        self.outputs.append(output)
        output.write(f"handled: {line}")
        if self._stop_after is not None and len(self.lines) >= self._stop_after:
            return False
        return True


def test_plain_repl_view_run_returns_zero_for_normal_close() -> None:
    input_stream = io.StringIO("/exit\n")
    output_stream = io.StringIO()
    controller = FakeController(stop_after=1, exit_code=0)

    exit_code = PlainReplView(
        input_stream=input_stream,
        output_stream=output_stream,
    ).run(controller)

    assert exit_code == 0
    assert controller.lines == ["/exit\n"]
    assert output_stream.getvalue() == "handled: /exit\n"
    assert controller.runtime.completed is False


def test_plain_repl_view_eof_completes_runtime_and_returns_zero() -> None:
    input_stream = io.StringIO("hello\n")
    output_stream = io.StringIO()
    controller = FakeController()

    exit_code = PlainReplView(
        input_stream=input_stream,
        output_stream=output_stream,
    ).run(controller)

    assert exit_code == 0
    assert controller.lines == ["hello\n"]
    assert controller.runtime.completed is True


def test_plain_repl_view_uses_injected_output_stream_for_each_line() -> None:
    input_stream = io.StringIO("one\ntwo\n")
    output_stream = io.StringIO()
    controller = FakeController(stop_after=2)

    PlainReplView(input_stream=input_stream, output_stream=output_stream).run(controller)

    assert controller.lines == ["one\n", "two\n"]
    assert controller.outputs == [output_stream, output_stream]
    assert output_stream.getvalue() == "handled: one\nhandled: two\n"


def test_plain_repl_view_returns_controller_exit_code_when_controller_stops() -> None:
    controller = FakeController(stop_after=1, exit_code=7)

    exit_code = PlainReplView(
        input_stream=io.StringIO("hello\n"),
        output_stream=io.StringIO(),
    ).run(controller)

    assert exit_code == 7
    assert controller.runtime.completed is False

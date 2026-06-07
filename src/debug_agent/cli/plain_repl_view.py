from __future__ import annotations

import sys
from typing import Protocol, TextIO


class PlainReplController(Protocol):
    exit_code: int
    runtime: object

    def handle_line(self, line: str, output: TextIO) -> bool: ...


class PlainReplRuntime(Protocol):
    def complete(self) -> None: ...


class PlainReplView:
    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout

    def run(self, controller: PlainReplController) -> int:
        for line in self.input_stream:
            try:
                should_continue = controller.handle_line(line, self.output_stream)
            except KeyboardInterrupt:
                if _is_active_turn(controller):
                    interrupt = getattr(controller, "on_interrupt", None)
                    if callable(interrupt):
                        interrupt()
                    return int(getattr(controller, "exit_code", 130))
                raise
            if not should_continue:
                return controller.exit_code
        runtime = controller.runtime
        complete = getattr(runtime, "complete")
        complete()
        return 0


def _is_active_turn(controller: PlainReplController) -> bool:
    return bool(getattr(controller, "is_executing", False)) or str(
        getattr(controller, "control_state", "")
    ) in {"running_turn", "cancelling"}

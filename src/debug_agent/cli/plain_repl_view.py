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
            if not controller.handle_line(line, self.output_stream):
                return controller.exit_code
        runtime = controller.runtime
        complete = getattr(runtime, "complete")
        complete()
        return 0

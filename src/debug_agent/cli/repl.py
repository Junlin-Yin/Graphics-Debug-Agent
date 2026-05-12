from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from debug_agent.runtime.contracts import AgentRunResult
from debug_agent.runtime.orchestrator import ReplRuntime, RuntimeOrchestrator


BUSY_MESSAGE = "Prompt run is already executing. Use /status or /exit."


@dataclass
class ReplController:
    runtime: ReplRuntime
    is_executing: bool = False
    exit_code: int = 0

    @classmethod
    def start(
        cls,
        *,
        config_snapshot: dict[str, Any],
        workspace_root: str | Path | None = None,
    ) -> ReplController:
        result = RuntimeOrchestrator(workspace_root=workspace_root).start_repl(
            config_snapshot
        )
        if result.error is not None or result.runtime is None:
            raise ReplStartFailed(result.error.exit_code, result.error.message)
        return cls(runtime=result.runtime)

    def handle_line(self, line: str, output: TextIO) -> bool:
        command = line.strip()
        if not command:
            return True
        if command.startswith("/"):
            return self._handle_slash_command(command, output)
        if self.is_executing:
            print(BUSY_MESSAGE, file=output)
            return True
        self.is_executing = True
        try:
            result = self.runtime.run_turn(command)
        finally:
            self.is_executing = False
        if result.status == "completed":
            print(result.assistant_output or "", file=output)
            return True
        self.runtime.fail(result)
        self.exit_code = 1
        message = result.error["message"] if result.error else "Prompt execution failed."
        print(message, file=output)
        return False

    def close(self) -> None:
        self.runtime.close()

    def _handle_slash_command(self, command: str, output: TextIO) -> bool:
        if command == "/status":
            print("\n".join(self.runtime.status_lines()), file=output)
            return True
        if command == "/exit":
            self.runtime.complete()
            return False
        print(f"Unsupported Phase 0 slash command: {command}", file=output)
        return True


class ReplStartFailed(RuntimeError):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


def run_repl(
    config_snapshot: dict[str, Any],
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    workspace_root: str | Path | None = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    try:
        controller = ReplController.start(
            config_snapshot=config_snapshot,
            workspace_root=workspace_root,
        )
    except ReplStartFailed as exc:
        print(exc.message, file=error_stream)
        return exc.exit_code

    try:
        for line in input_stream:
            if not controller.handle_line(line, output_stream):
                return controller.exit_code
        controller.runtime.complete()
        return 0
    except KeyboardInterrupt:
        controller.runtime.fail(
            AgentRunResult(
                status="cancelled",
                assistant_output=None,
                tool_results=[],
                usage={},
                error={
                    "error_class": "cancelled",
                    "message": "REPL interrupted by Ctrl+C.",
                    "source": "cli",
                    "recoverable": False,
                },
                metadata={"prompt_turn_counter": controller.runtime.turn_counter},
            )
        )
        return 1
    finally:
        controller.close()

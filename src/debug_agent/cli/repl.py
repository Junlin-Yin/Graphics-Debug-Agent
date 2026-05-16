from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from debug_agent.cli.plain_repl_view import PlainReplView
from debug_agent.cli.repl_controller import ReplController, ReplStartFailed
from debug_agent.runtime.contracts import AgentRunResult


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
        return PlainReplView(
            input_stream=input_stream,
            output_stream=output_stream,
        ).run(controller)
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

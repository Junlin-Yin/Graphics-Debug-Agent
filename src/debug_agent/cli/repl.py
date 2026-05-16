from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TextIO

from debug_agent.cli.plain_repl_view import PlainReplView
from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView
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
    injected_input = input_stream is not None
    injected_output = output_stream is not None
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
        view = _select_repl_view(
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=error_stream,
            injected_input=injected_input,
            injected_output=injected_output,
        )
        return view.run(controller)
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


def _select_repl_view(
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    injected_input: bool,
    injected_output: bool,
) -> object:
    if (
        not injected_input
        and not injected_output
        and input_stream.isatty()
        and output_stream.isatty()
    ):
        try:
            return PromptToolkitReplView()
        except Exception as exc:
            print(
                f"prompt_toolkit initialization failed; falling back to plain REPL: {exc}",
                file=error_stream,
            )
    return PlainReplView(
        input_stream=input_stream,
        output_stream=output_stream,
    )

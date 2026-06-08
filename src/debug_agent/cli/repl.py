from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TextIO

from debug_agent.cli.plain_repl_view import PlainReplView
from debug_agent.cli.prompt_toolkit_view import PromptToolkitReplView
from debug_agent.cli.repl_controller import (
    ControllerApprovalProvider,
    ReplController,
    ReplStartFailed,
)
from debug_agent.cli.exit_codes import INTERRUPTED
from debug_agent.tools.broker import ApprovalDecision, NonInteractiveApprovalProvider


def run_repl(
    config_snapshot: dict[str, Any],
    *,
    approval_mode: str = "normal",
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
            approval_mode=approval_mode,
            workspace_root=workspace_root,
            stale_confirmation=_stale_confirmation_provider(
                input_stream=input_stream,
                output_stream=output_stream,
            ),
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
        if isinstance(view, PromptToolkitReplView):
            controller.runtime.set_approval_provider(
                ControllerApprovalProvider(controller)
            )
        elif _stream_isatty(input_stream) and _stream_isatty(output_stream):
            controller.runtime.set_approval_provider(
                PlainApprovalProvider(
                    input_stream=input_stream,
                    output_stream=output_stream,
                )
            )
        else:
            controller.runtime.set_approval_provider(NonInteractiveApprovalProvider())
        return view.run(controller)
    except KeyboardInterrupt:
        controller.runtime.cancel_idle()
        return INTERRUPTED
    finally:
        controller.close()


def run_resumed_repl(
    session_id: str,
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
        controller = ReplController.resume(
            session_id=session_id,
            workspace_root=workspace_root,
            stale_confirmation=_stale_confirmation_provider(
                input_stream=input_stream,
                output_stream=output_stream,
            ),
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
        if isinstance(view, PromptToolkitReplView):
            controller.runtime.set_approval_provider(
                ControllerApprovalProvider(controller)
            )
        elif _stream_isatty(input_stream) and _stream_isatty(output_stream):
            controller.runtime.set_approval_provider(
                PlainApprovalProvider(
                    input_stream=input_stream,
                    output_stream=output_stream,
                )
            )
        else:
            controller.runtime.set_approval_provider(NonInteractiveApprovalProvider())
        return view.run(controller)
    except KeyboardInterrupt:
        controller.runtime.cancel_idle()
        return INTERRUPTED
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
            return PromptToolkitReplView(output_stream=output_stream)
        except Exception as exc:
            print(
                f"prompt_toolkit initialization failed; falling back to plain REPL: {exc}",
                file=error_stream,
            )
    return PlainReplView(
        input_stream=input_stream,
        output_stream=output_stream,
    )


def _stream_isatty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _stale_confirmation_provider(
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> Any | None:
    if not (_stream_isatty(input_stream) and _stream_isatty(output_stream)):
        return None

    def confirm(request: dict[str, Any]) -> bool:
        print(
            "Stale session is still taking the ownership: "
            f"{request.get('session_id', '')}.",
            file=output_stream,
        )
        print("Fail-close the stale owner and continue? [y/N] ", end="", file=output_stream)
        output_stream.flush()
        response = input_stream.readline()
        return response.strip().lower() in {"y", "yes"}

    return confirm


class PlainApprovalProvider:
    is_interactive = True

    def __init__(self, *, input_stream: TextIO, output_stream: TextIO) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream

    def request_approval(self, request: str, facts: dict[str, Any]) -> ApprovalDecision:
        print(request, file=self.output_stream)
        response = self.input_stream.readline()
        if response == "":
            return ApprovalDecision(
                "denied",
                "none",
                "Interactive approval is unavailable.",
            )
        normalized = response.strip().lower()
        if normalized == "y":
            return ApprovalDecision("approved_once", "once")
        if normalized == "a":
            return ApprovalDecision("approved_for_session", "session")
        return ApprovalDecision("denied", "none")

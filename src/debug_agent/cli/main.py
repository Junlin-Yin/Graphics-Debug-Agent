from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import MutableMapping, TextIO

from debug_agent.cli.exit_codes import ERROR_USAGE, INTERRUPTED, OK
from debug_agent.cli.repl import (
    _stale_confirmation_provider,
    run_repl,
    run_resumed_repl,
)
from debug_agent.cli.settings import APPROVAL_MODES, USAGE
from debug_agent.persistence.sqlite import RuntimeBootstrapError
from debug_agent.runtime.config import load_config_snapshot
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


_UTF8_REEXEC_MARKER = "DEBUG_AGENT_UTF8_REEXECED"


@dataclass(frozen=True)
class CliArgs:
    command: str
    session_id: str | None = None
    approval_mode: str = "normal"
    prompt: str | None = None


class CliArgumentParser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise argparse.ArgumentError(None, message or USAGE)

    def error(self, message: str) -> None:
        raise argparse.ArgumentError(None, message)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except KeyboardInterrupt:
        print("Interrupted by Ctrl+C.", file=sys.stderr)
        return INTERRUPTED
    except RuntimeBootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _main(args: list[str]) -> int:
    parse_result = _parse_cli_args(args)
    if parse_result == "help":
        print(USAGE)
        return OK
    if isinstance(parse_result, str):
        print(parse_result, file=sys.stderr)
        return ERROR_USAGE
    parsed = parse_result
    if _starts_repl(parsed):
        _ensure_tty_repl_utf8_mode(args)

    if parsed.command in {"status", "trace", "resume"}:
        session_id = parsed.session_id
        if session_id is None:
            print(USAGE, file=sys.stderr)
            return ERROR_USAGE
        orchestrator = RuntimeOrchestrator()
        if parsed.command == "status":
            result = orchestrator.status(session_id)
            if result.exit_code != 0:
                print(result.message, file=sys.stderr)
                return result.exit_code
            print(_format_fields(result.fields))
            return 0
        if parsed.command == "trace":
            result = orchestrator.trace(session_id)
            if result.exit_code != 0:
                print(result.message, file=sys.stderr)
                return result.exit_code
            print(_format_fields(result.summary))
            return 0
        return run_resumed_repl(session_id)

    config = load_config_snapshot()
    if config.error is not None or config.snapshot is None:
        message = config.error.message if config.error else "Invalid configuration."
        print(message, file=sys.stderr)
        return 4

    if parsed.prompt is None:
        return run_repl(config.snapshot, approval_mode=parsed.approval_mode)

    result = RuntimeOrchestrator(
        stale_confirmation=_stale_confirmation_provider(
            input_stream=sys.stdin,
            output_stream=sys.stdout,
        )
    ).run_one_shot(
        parsed.prompt or "",
        config.snapshot,
        approval_mode=parsed.approval_mode,
    )
    if result.exit_code == 0:
        print(result.message)
    else:
        summary = _format_one_shot_terminal_failure(result)
        print(summary if summary is not None else result.message, file=sys.stderr)
    return result.exit_code


def _format_one_shot_terminal_failure(result) -> str | None:
    if not getattr(result, "terminal_failure_summary", False):
        return None
    session_id = getattr(result, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return None
    error = getattr(result, "error", None)
    if not _has_complete_normalized_error_fields(error):
        return None
    return "\n" + "\n".join(
        [
            f"One-shot session {session_id} failed.",
            f"{error['error_class']}/{error['reason']}: {error['message']}",
            f"trace: {_trace_file_path(session_id)}",
            f"resume: debug-agent resume {session_id}",
        ]
    )


def _has_complete_normalized_error_fields(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    required = {
        "schema_version": int,
        "error_class": str,
        "reason": str,
        "message": str,
        "scope": str,
        "recoverability": str,
        "metadata": dict,
        "artifact_ids": list,
    }
    for field, expected_type in required.items():
        value = error.get(field)
        if not isinstance(value, expected_type):
            return False
        if expected_type is str and not value:
            return False
    return True


def _trace_file_path(session_id: str) -> str:
    return f".sessions/{session_id}/logs/trace.md"


def _parse_cli_args(args: list[str]) -> CliArgs | str:
    parser = _build_parser()
    try:
        namespace = parser.parse_args(args)
    except argparse.ArgumentError:
        return USAGE

    if namespace.help:
        return "help"
    if namespace.approval_mode not in APPROVAL_MODES:
        return "approval mode must be one of: normal, semi-auto, yolo"

    if namespace.command in {"status", "trace", "resume"}:
        if namespace.prompt is not None:
            return USAGE
        return CliArgs(command=namespace.command, session_id=namespace.session_id)

    if namespace.prompt == "":
        return USAGE
    return CliArgs(
        command="prompt",
        approval_mode=namespace.approval_mode,
        prompt=namespace.prompt,
    )


def _starts_repl(parsed: CliArgs) -> bool:
    return parsed.command == "resume" or (
        parsed.command == "prompt" and parsed.prompt is None
    )


def _ensure_tty_repl_utf8_mode(
    args: list[str],
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    environ: MutableMapping[str, str] | None = None,
    utf8_mode: int | None = None,
    executable: str | None = None,
    execve=os.execve,
) -> None:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    environ = os.environ if environ is None else environ
    utf8_mode = sys.flags.utf8_mode if utf8_mode is None else utf8_mode
    executable = executable or sys.executable
    if utf8_mode == 1:
        return
    if environ.get(_UTF8_REEXEC_MARKER) == "1":
        return
    if not (_stream_isatty(input_stream) and _stream_isatty(output_stream)):
        return
    environ["PYTHONUTF8"] = "1"
    environ[_UTF8_REEXEC_MARKER] = "1"
    execve(
        executable,
        [executable, "-X", "utf8", "-m", "debug_agent.cli.main", *args],
        environ,
    )


def _stream_isatty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="debug-agent", add_help=False, exit_on_error=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--approval-mode", default="normal")
    parser.add_argument("-p", dest="prompt")
    subparsers = parser.add_subparsers(dest="command")
    for command in ("status", "trace", "resume"):
        subparser = subparsers.add_parser(command, add_help=False, exit_on_error=False)
        subparser.add_argument("session_id")
    return parser


def _format_fields(fields: dict) -> str:
    return "\n".join(f"{key}: {_format_value(value)}" for key, value in fields.items())


def _format_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())

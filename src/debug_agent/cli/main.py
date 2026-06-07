from __future__ import annotations

import sys
from collections.abc import Sequence

from debug_agent.cli.exit_codes import ERROR_USAGE, INTERRUPTED
from debug_agent.cli.repl import (
    _stale_confirmation_provider,
    run_repl,
    run_resumed_repl,
)
from debug_agent.persistence.sqlite import RuntimeBootstrapError
from debug_agent.runtime.config import load_config_snapshot
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


USAGE = (
    'Usage: debug-agent [--approval-mode normal|semi-auto|yolo]  # REPL | '
    'debug-agent [--approval-mode normal|semi-auto|yolo] -p "prompt" | '
    "debug-agent status <session_id> | debug-agent trace <session_id> | "
    "debug-agent resume <session_id>"
)
APPROVAL_MODES = {"normal", "semi-auto", "yolo"}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except KeyboardInterrupt:
        result = RuntimeOrchestrator().cancel_active_session("Interrupted by Ctrl+C.")
        print(result.message, file=sys.stderr)
        return INTERRUPTED
    except RuntimeBootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _main(args: list[str]) -> int:
    if args and args[0] in {"status", "trace", "resume"}:
        if len(args) != 2 or not args[1]:
            print(USAGE, file=sys.stderr)
            return ERROR_USAGE
        orchestrator = RuntimeOrchestrator()
        if args[0] == "status":
            result = orchestrator.status(args[1])
            if result.exit_code != 0:
                print(result.message, file=sys.stderr)
                return result.exit_code
            print(_format_fields(result.fields))
            return 0
        if args[0] == "trace":
            result = orchestrator.trace(args[1])
            if result.exit_code != 0:
                print(result.message, file=sys.stderr)
                return result.exit_code
            print(_format_fields(result.summary))
            return 0
        return run_resumed_repl(args[1])

    parse_result = _parse_prompt_args(args)
    if isinstance(parse_result, str):
        print(parse_result, file=sys.stderr)
        return ERROR_USAGE
    approval_mode, prompt = parse_result

    config = load_config_snapshot()
    if config.error is not None or config.snapshot is None:
        message = config.error.message if config.error else "Invalid configuration."
        print(message, file=sys.stderr)
        return 4

    if prompt is None:
        return run_repl(config.snapshot, approval_mode=approval_mode)

    result = RuntimeOrchestrator(
        stale_confirmation=_stale_confirmation_provider(
            input_stream=sys.stdin,
            output_stream=sys.stdout,
        )
    ).run_one_shot(
        prompt or "",
        config.snapshot,
        approval_mode=approval_mode,
    )
    if result.exit_code == 0:
        print(result.message)
    else:
        print(result.message, file=sys.stderr)
    return result.exit_code


def _parse_prompt_args(args: list[str]) -> tuple[str, str | None] | str:
    approval_mode = "normal"
    remaining = list(args)
    if remaining[:1] == ["--approval-mode"]:
        if len(remaining) < 2 or remaining[1] not in APPROVAL_MODES:
            return "approval mode must be one of: normal, semi-auto, yolo"
        approval_mode = remaining[1]
        remaining = remaining[2:]
    if not remaining:
        return approval_mode, None
    if len(remaining) == 2 and remaining[0] == "-p" and remaining[1]:
        return approval_mode, remaining[1]
    return USAGE


def _format_fields(fields: dict) -> str:
    return "\n".join(f"{key}: {_format_value(value)}" for key, value in fields.items())


def _format_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())

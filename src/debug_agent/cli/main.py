from __future__ import annotations

import sys
from collections.abc import Sequence

from debug_agent.cli.exit_codes import ERROR_USAGE, INTERRUPTED
from debug_agent.cli.repl import (
    _stale_confirmation_provider,
    run_repl,
    run_resumed_repl,
)
from debug_agent.cli.settings import APPROVAL_MODES, USAGE
from debug_agent.persistence.sqlite import RuntimeBootstrapError
from debug_agent.runtime.config import load_config_snapshot
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


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
            f"trace: debug-agent trace {session_id}",
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

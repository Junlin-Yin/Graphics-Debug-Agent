from __future__ import annotations

import sys
from collections.abc import Sequence

from debug_agent.cli.repl import run_repl
from debug_agent.persistence.sqlite import RuntimeBootstrapError
from debug_agent.runtime.config import load_config_snapshot
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


USAGE = 'Usage: debug-agent [-p "prompt"] | debug-agent status <session_id> | debug-agent trace <session_id>'


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except RuntimeBootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _main(args: list[str]) -> int:
    if args and args[0] in {"status", "trace"}:
        if len(args) != 2 or not args[1]:
            print(USAGE, file=sys.stderr)
            return 2
        orchestrator = RuntimeOrchestrator()
        if args[0] == "status":
            result = orchestrator.status(args[1])
            if result.exit_code != 0:
                print(result.message, file=sys.stderr)
                return result.exit_code
            print(_format_fields(result.fields))
            return 0
        result = orchestrator.trace(args[1])
        if result.exit_code != 0:
            print(result.message, file=sys.stderr)
            return result.exit_code
        print(_format_fields(result.summary))
        return 0

    if args and (len(args) != 2 or args[0] != "-p" or not args[1]):
        print(USAGE, file=sys.stderr)
        return 2

    config = load_config_snapshot()
    if config.error is not None or config.snapshot is None:
        message = config.error.message if config.error else "Invalid configuration."
        print(message, file=sys.stderr)
        return 4

    if not args:
        return run_repl(config.snapshot)

    result = RuntimeOrchestrator().run_one_shot(args[1], config.snapshot)
    if result.exit_code == 0:
        print(result.assistant_output or "")
    else:
        print(result.message, file=sys.stderr)
    return result.exit_code


def _format_fields(fields: dict) -> str:
    return "\n".join(f"{key}: {_format_value(value)}" for key, value in fields.items())


def _format_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())

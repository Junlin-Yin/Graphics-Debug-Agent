from __future__ import annotations

import sys
from collections.abc import Sequence

from debug_agent.runtime.config import load_config_snapshot
from debug_agent.runtime.orchestrator import RuntimeOrchestrator


USAGE = 'Usage: debug-agent -p "prompt"'


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "-p" or not args[1]:
        print(USAGE, file=sys.stderr)
        return 2

    config = load_config_snapshot()
    if config.error is not None or config.snapshot is None:
        message = config.error.message if config.error else "Invalid configuration."
        print(message, file=sys.stderr)
        return 4

    result = RuntimeOrchestrator().run_one_shot(args[1], config.snapshot)
    if result.exit_code == 0:
        print(result.assistant_output or "")
    else:
        print(result.message, file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

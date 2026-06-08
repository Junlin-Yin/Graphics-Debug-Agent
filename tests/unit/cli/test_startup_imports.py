from __future__ import annotations

import subprocess
import sys


def test_cli_import_does_not_load_openai_sdk() -> None:
    script = (
        "import sys\n"
        "import debug_agent.cli.main\n"
        "raise SystemExit(1 if 'openai' in sys.modules else 0)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
    )

    assert result.returncode == 0

from __future__ import annotations

import subprocess
from pathlib import Path


def resolve_workspace_root(start: str | Path | None = None) -> Path:
    candidate = Path.cwd() if start is None else Path(start)
    candidate = candidate.resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=candidate,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        root = result.stdout.strip()
        if root:
            return Path(root).resolve()
    return candidate

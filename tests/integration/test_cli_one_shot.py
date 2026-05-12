from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _subprocess_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home)}


def test_debug_agent_one_shot_completes_with_fake_model(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config_dir = home / ".debug-agent"
    config_dir.mkdir(parents=True)
    workspace.mkdir()
    (config_dir / "config.toml").write_text(
        """
[defaults]
provider = "fake"
model = "fake-model"
fake_response = "integration answer"
""".strip(),
        encoding="utf-8",
    )

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable, "-p", "hello"],
        cwd=workspace,
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "integration answer\n"
    db_path = workspace / ".sessions" / "runtime.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1

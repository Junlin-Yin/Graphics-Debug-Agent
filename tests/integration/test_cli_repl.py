from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _subprocess_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home)}


def test_debug_agent_repl_accepts_two_turns_status_and_exit(tmp_path) -> None:
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
fake_response = "integration repl answer"
""".strip(),
        encoding="utf-8",
    )

    executable = str(Path(sys.executable).parent / "debug-agent")
    result = subprocess.run(
        [executable],
        cwd=workspace,
        env=_subprocess_env(home),
        input="hello\n/status\ntell me one more thing\n/exit\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.count("integration repl answer\n") == 2
    assert "session_id:" in result.stdout
    db_path = workspace / ".sessions" / "runtime.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "completed"
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "completed"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE kind = 'user_message'"
            ).fetchone()[0]
            == 2
        )
        checkpoint_kinds = [
            row[0] for row in conn.execute("SELECT kind FROM checkpoints ORDER BY rowid")
        ]
    assert checkpoint_kinds == ["turn", "turn", "terminal"]

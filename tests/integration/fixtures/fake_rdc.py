from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path


def materialize_fake_rdc(tmp_path: Path, workspace: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = _script(workspace)
    if os.name == "nt":
        script_path = bin_dir / "fake_rdc.py"
        script_path.write_text(script, encoding="utf-8")
        executable = bin_dir / "rdc.cmd"
        python_executable = str(Path(os.sys.executable))
        executable.write_text(
            f'@echo off\n"{python_executable}" "%~dp0fake_rdc.py" %*\nexit /b %ERRORLEVEL%\n',
            encoding="utf-8",
        )
    else:
        executable = bin_dir / "rdc"
        executable.write_text(script, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _script(workspace: Path) -> str:
    return textwrap.dedent(
        f"""\
        #!{Path(os.sys.executable).as_posix()}
        from __future__ import annotations

        import base64
        import json
        import os
        import sys
        from pathlib import Path

        WORKSPACE = Path({str(workspace)!r})
        STATE_DIR = WORKSPACE / ".fake_rdc"
        STATE_FILE = STATE_DIR / "state.json"
        LOG_FILE = STATE_DIR / "commands.jsonl"
        PNG_BYTES = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMUqbjDwMDAxAAGABBCAWz41fthAAAAAElFTkSuQmCC"
        )

        def _record(argv: list[str]) -> None:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({{"argv": argv, "cwd": os.getcwd()}}, sort_keys=True) + "\\n")

        def _load_state() -> dict:
            if not STATE_FILE.exists():
                raise SystemExit("no capture is open")
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))

        def _write_state(payload: dict) -> None:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        def main(argv: list[str]) -> int:
            _record(argv)
            if argv == ["doctor"]:
                print("fake rdc doctor ok")
                return 0
            if len(argv) == 2 and argv[0] == "open":
                capture = Path(argv[1])
                if not capture.is_absolute():
                    capture = Path.cwd() / capture
                if not capture.exists():
                    print(f"capture not found: {{argv[1]}}", file=sys.stderr)
                    return 2
                _write_state({{"capture": str(capture.resolve()), "cwd": os.getcwd(), "open": True}})
                print(json.dumps({{"opened": argv[1], "cwd": os.getcwd()}}, sort_keys=True))
                return 0
            if argv == ["info", "--json"]:
                state = _load_state()
                print(json.dumps({{
                    "capture": Path(state["capture"]).name,
                    "driver": "fake-renderdoc",
                    "frames": 1,
                    "target": "phase4-readiness",
                }}, sort_keys=True))
                return 0
            if argv == ["draws", "--limit", "20"]:
                _load_state()
                print("eid=42 name=DrawIndexed vertices=3 pipeline=graphics")
                print("eid=77 name=Dispatch groups=1,1,1 pipeline=compute")
                return 0
            if len(argv) == 4 and argv[0] == "rt" and argv[2] == "-o":
                _load_state()
                output = Path(argv[3])
                if not output.is_absolute():
                    output = Path.cwd() / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(PNG_BYTES)
                print(json.dumps({{"eid": argv[1], "output": str(output.resolve())}}, sort_keys=True))
                return 0
            if argv == ["close"]:
                if STATE_FILE.exists():
                    STATE_FILE.unlink()
                print("fake rdc closed")
                return 0
            print("unsupported fake rdc command: " + " ".join(argv), file=sys.stderr)
            return 64

        if __name__ == "__main__":
            raise SystemExit(main(sys.argv[1:]))
        """
    )

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from debug_agent.runtime.contracts import ToolDefinition


@dataclass(frozen=True)
class ShellRunResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class ShellHandlerResult:
    status: str
    output: dict[str, Any] | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None


class ShellTimeout(Exception):
    pass


class ShellRunner(Protocol):
    def run(
        self, argv: list[str], *, cwd: Path, timeout_seconds: int
    ) -> ShellRunResult:
        ...


class FakeShellRunner:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        exc: Exception | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def run(
        self, argv: list[str], *, cwd: Path, timeout_seconds: int
    ) -> ShellRunResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.exc is not None:
            raise self.exc
        return ShellRunResult(
            stdout=self.stdout,
            stderr=self.stderr,
            returncode=self.returncode,
        )


class SubprocessShellRunner:
    def run(
        self, argv: list[str], *, cwd: Path, timeout_seconds: int
    ) -> ShellRunResult:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                timeout=timeout_seconds,
                shell=False,
                text=True,
                capture_output=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ShellTimeout(str(exc)) from exc
        return ShellRunResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="shell_exec",
            description="Run a structured argv command.",
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            category="shell",
            risk_level="execute",
            access=["execute"],
        )
    ]


def shell_exec(context: Any, arguments: dict[str, Any]) -> ShellHandlerResult:
    runner = getattr(context, "shell_runner", None) or SubprocessShellRunner()
    timeout_seconds = int(arguments["effective_timeout_seconds"])
    try:
        result = runner.run(
            list(arguments["argv"]),
            cwd=Path(arguments["execution_cwd"]),
            timeout_seconds=timeout_seconds,
        )
    except ShellTimeout as exc:
        return ShellHandlerResult(
            status="timeout",
            error_message=str(exc),
            metadata={"effective_timeout_seconds": timeout_seconds},
        )
    return ShellHandlerResult(
        status="ok",
        output={
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        },
        metadata={"effective_timeout_seconds": timeout_seconds},
    )


def tool_handlers() -> dict[str, Any]:
    return {"shell_exec": shell_exec}

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
    reason: str | None = None


class ShellTimeout(Exception):
    pass


class ShellCancelled(Exception):
    pass


class ShellRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        register_process_handle: Any = None,
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
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        register_process_handle: Any = None,
    ) -> ShellRunResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "register_process_handle": register_process_handle,
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
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        register_process_handle: Any = None,
    ) -> ShellRunResult:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        handle = ActiveShellProcessHandle(process)
        if callable(register_process_handle):
            register_process_handle(handle)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            handle.terminate()
            stdout, stderr = process.communicate()
            raise ShellTimeout(str(exc)) from exc
        if handle.terminate_requested:
            raise ShellCancelled("shell_exec was cancelled.")
        return ShellRunResult(
            stdout=stdout,
            stderr=stderr,
            returncode=process.returncode,
        )


@dataclass
class ActiveShellProcessHandle:
    process: subprocess.Popen
    terminate_requested: bool = False

    def terminate(self) -> None:
        self.terminate_requested = True
        if self.process.poll() is None:
            self.process.terminate()


def tool_definitions(*, max_timeout_seconds: int = 3600) -> list[ToolDefinition]:
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
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": max_timeout_seconds,
                    },
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
            register_process_handle=getattr(context, "shell_process_registry", None),
        )
    except ShellTimeout as exc:
        return ShellHandlerResult(
            status="timeout",
            error_message=str(exc),
            metadata={"effective_timeout_seconds": timeout_seconds},
        )
    except ShellCancelled as exc:
        return ShellHandlerResult(
            status="cancelled",
            error_message=str(exc),
            metadata={"effective_timeout_seconds": timeout_seconds},
        )
    if result.returncode != 0:
        return ShellHandlerResult(
            status="error",
            error_message=_nonzero_exit_message(result),
            metadata={"effective_timeout_seconds": timeout_seconds},
            reason="shell_nonzero_exit",
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


def _nonzero_exit_message(result: ShellRunResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    if detail:
        return f"{detail} (exit code {result.returncode})"
    return f"Command exited with exit code {result.returncode}."

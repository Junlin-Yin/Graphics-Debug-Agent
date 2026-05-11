from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.runtime.contracts import RunEvent, ToolResult, utc_now_iso
from debug_agent.tools.native_readonly import tool_handlers


LARGE_OUTPUT_THRESHOLD_BYTES = 16 * 1024
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0


class ToolBroker:
    def __init__(
        self,
        *,
        event_writer: EventWriter,
        artifact_store: ArtifactStore,
        timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> None:
        self.event_writer = event_writer
        self.artifact_store = artifact_store
        self.timeout_seconds = timeout_seconds
        self._tool_handlers = tool_handlers()

    def invoke(
        self,
        session_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict,
        context: dict,
    ) -> ToolResult:
        start = monotonic()
        normalized_arguments = dict(arguments)
        workspace_root = Path(context["workspace_root"]).resolve()
        timeout_seconds = float(context.get("timeout_seconds", self.timeout_seconds))

        denial = self._validate(tool_name, normalized_arguments, workspace_root)
        if denial is not None:
            result = _denied_result(denial)
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_denied",
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                ),
            )
            return result

        self._write_event(
            session_id=session_id,
            run_id=run_id,
            kind="tool_call_started",
            payload={
                "tool_name": tool_name,
                "arguments": normalized_arguments,
                "status": "started",
            },
        )
        handler = self._tool_handlers[tool_name]
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(handler, workspace_root, normalized_arguments)
                output = future.result(timeout=timeout_seconds)
        except TimeoutError:
            result = ToolResult(
                status="timeout",
                output=None,
                error={
                    "error_class": "timeout",
                    "message": f"Tool timed out after {timeout_seconds:g} seconds.",
                    "source": "toolbroker",
                    "recoverable": True,
                },
                artifacts=[],
                metadata={},
                redacted_output=None,
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                ),
            )
            return result
        except Exception as exc:
            result = ToolResult(
                status="error",
                output=None,
                error={
                    "error_class": "tool_error",
                    "message": str(exc),
                    "source": tool_name,
                    "recoverable": True,
                },
                artifacts=[],
                metadata={},
                redacted_output=None,
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                ),
            )
            return result

        result = self._ok_result(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            output=output,
        )
        self._write_event(
            session_id=session_id,
            run_id=run_id,
            kind="tool_call_completed",
            payload=_audit_payload(
                tool_name=tool_name,
                arguments=normalized_arguments,
                result=result,
                duration_seconds=monotonic() - start,
            ),
        )
        return result

    def _validate(
        self, tool_name: str, arguments: dict[str, Any], workspace_root: Path
    ) -> str | None:
        if tool_name not in self._tool_handlers:
            return f"Unknown tool: {tool_name}"
        if arguments.get("write") is True or arguments.get("intent") == "write":
            return "Write intent is denied in Phase 0."
        if tool_name in {"read_file", "list_dir"} and not isinstance(
            arguments.get("path"), str
        ):
            return "Missing required string argument: path"
        if tool_name == "search_text" and not isinstance(arguments.get("query"), str):
            return "Missing required string argument: query"
        if tool_name == "search_text" and "path" in arguments and not isinstance(
            arguments.get("path"), str
        ):
            return "Optional argument path must be a string"
        for key in ("path",):
            if key not in arguments:
                continue
            target = (workspace_root / arguments[key]).resolve()
            try:
                target.relative_to(workspace_root)
            except ValueError:
                return "Path is outside the workspace root."
        return None

    def _ok_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
            output: str | dict[str, Any],
    ) -> ToolResult:
        if isinstance(output, str):
            output_size = len(output.encode("utf-8"))
            if output_size > LARGE_OUTPUT_THRESHOLD_BYTES:
                artifact = self.artifact_store.write_text(
                    session_id=session_id,
                    run_id=run_id,
                    artifact_id=f"art_{uuid4().hex}",
                    filename=f"{tool_name}_output.txt",
                    content=output,
                    metadata={
                        "tool_name": tool_name,
                        "bytes": output_size,
                    },
                )
                return ToolResult(
                    status="ok",
                    output=None,
                    error=None,
                    artifacts=[artifact.artifact_id],
                    metadata={"bytes": output_size},
                    redacted_output=f"[output stored as artifact: {artifact.artifact_id}]",
                )
        return ToolResult(
            status="ok",
            output=output,
            error=None,
            artifacts=[],
            metadata={},
            redacted_output=None,
        )

    def _write_event(
        self, *, session_id: str, run_id: str, kind: str, payload: dict[str, Any]
    ) -> None:
        self.event_writer.append(
            RunEvent(
                event_id=f"evt_{uuid4().hex}",
                timestamp=utc_now_iso(),
                session_id=session_id,
                run_id=run_id,
                step_id=None,
                kind=kind,
                payload=payload,
            )
        )


def _denied_result(message: str) -> ToolResult:
    return ToolResult(
        status="denied",
        output=None,
        error={
            "error_class": "policy_denied",
            "message": message,
            "source": "toolbroker",
            "recoverable": True,
        },
        artifacts=[],
        metadata={},
        redacted_output=None,
    )


def _audit_payload(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    duration_seconds: float,
) -> dict[str, Any]:
    payload = {
        "tool_name": tool_name,
        "arguments": arguments,
        "status": result.status,
        "duration": duration_seconds,
        "artifact_ids": result.artifacts,
    }
    if result.error is not None:
        payload["error_class"] = result.error["error_class"]
    return payload

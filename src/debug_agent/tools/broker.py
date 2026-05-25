from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.runtime.contracts import RunEvent, ToolDefinition, ToolResult, utc_now_iso
from debug_agent.runtime.policy import (
    ApprovalGrant,
    NormalizedToolCall,
    PermissionEvaluator,
    PolicyFacts,
    build_builtin_policy,
    canonicalize_path,
    classify_argv_paths,
    normalize_shell_argv,
    scope_signature_for_tool,
)
from debug_agent.tools.native import NativeHandlerResult, tool_definitions, tool_error_result, tool_handlers
from debug_agent.tools import shell as shell_tools
from debug_agent.tools.shell import ShellHandlerResult


LARGE_OUTPUT_THRESHOLD_BYTES = 16 * 1024
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ApprovalDecision:
    decision: str
    grant_scope: str = "none"
    message: str | None = None


class ApprovalProvider(Protocol):
    def request_approval(self, request: str, facts: dict[str, Any]) -> ApprovalDecision:
        ...


class FakeApprovalProvider:
    def __init__(self, decision: str = "denied") -> None:
        self.decision = decision
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def request_approval(self, request: str, facts: dict[str, Any]) -> ApprovalDecision:
        self.requests.append((request, facts))
        if self.decision == "approved_for_session":
            return ApprovalDecision("approved_for_session", "session")
        if self.decision == "approved_once":
            return ApprovalDecision("approved_once", "once")
        return ApprovalDecision("denied", "none")


@dataclass(frozen=True)
class NormalizedBrokerArguments:
    arguments: dict[str, Any]
    paths: tuple[Path, ...]
    shell_argv: tuple[str, ...]
    scope_signature: str
    approval_target: str


@dataclass(frozen=True)
class ToolUseContext:
    session_id: str
    run_id: str
    workspace_root: Path
    artifact_root: Path
    approval_mode: str
    frozen_config: dict[str, Any]
    tool_definition: ToolDefinition
    frozen_policy: PolicyFacts
    permission_evaluator: PermissionEvaluator
    approval_grants: Any
    approval_provider: ApprovalProvider
    event_writer: EventWriter
    artifact_store: ArtifactStore
    skill_snapshot_store: Any = None
    shell_runner: Any = None


class ToolRouter:
    CATEGORIES = frozenset({"native", "shell", "runtime_control"})

    def __init__(self) -> None:
        self._native_handlers = tool_handlers()
        self._shell_handlers = shell_tools.tool_handlers()

    def route(
        self, context: ToolUseContext, arguments: dict[str, Any]
    ) -> str | dict[str, Any] | NativeHandlerResult:
        if context.tool_definition.category == "native":
            return self._native_handlers[context.tool_definition.name](context, arguments)
        if context.tool_definition.category == "shell":
            return self._shell_handlers[context.tool_definition.name](context, arguments)
        raise RuntimeError("Tool category is not enabled in this milestone.")


class ToolBroker:
    def __init__(
        self,
        *,
        event_writer: EventWriter,
        artifact_store: ArtifactStore,
        timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        router: ToolRouter | None = None,
    ) -> None:
        self.event_writer = event_writer
        self.artifact_store = artifact_store
        self.timeout_seconds = timeout_seconds
        definitions = [*tool_definitions(), *shell_tools.tool_definitions()]
        self._definitions = {definition.name: definition for definition in definitions}
        self._router = router or ToolRouter()

    def invoke(
        self,
        session_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict,
        context: dict,
    ) -> ToolResult:
        start = monotonic()
        if not isinstance(arguments, dict):
            result = _denied_result("Tool arguments must be an object.", error_class="user_error")
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_denied",
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments={"invalid_arguments": repr(arguments)},
                    result=result,
                    duration_seconds=monotonic() - start,
                ),
            )
            return result
        normalized_arguments = dict(arguments)
        workspace_root = Path(context["workspace_root"]).resolve()
        timeout_seconds = float(context.get("timeout_seconds", self.timeout_seconds))

        definition = self._definitions.get(tool_name)
        if definition is None or not tool_name.strip():
            result = _denied_result(
                "Invalid tool name." if not tool_name.strip() else f"Unknown tool: {tool_name}",
                error_class="policy_denied",
            )
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

        schema_error = _validate_schema(definition, normalized_arguments)
        if schema_error is not None:
            result = _denied_result(schema_error, error_class="user_error")
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

        policy_facts = context.get("policy_facts") or build_builtin_policy(workspace_root)
        evaluator = context.get("permission_evaluator") or PermissionEvaluator(policy_facts)
        normalized = _normalize_tool_arguments(
            definition=definition,
            arguments=normalized_arguments,
            workspace_root=workspace_root,
            frozen_config=context.get("frozen_config", {}),
        )
        normalized_arguments = normalized.arguments
        route_timeout_seconds = timeout_seconds
        if definition.name == "shell_exec":
            route_timeout_seconds = float(normalized_arguments["effective_timeout_seconds"])
        scope_signature = normalized.scope_signature
        call = NormalizedToolCall(
            tool_name=tool_name,
            category=definition.category,
            risk_level=definition.risk_level,
            access=tuple(definition.access or ()),
            paths=normalized.paths,
            shell_argv=normalized.shell_argv,
            approval_scope_signature=scope_signature,
        )
        approval_grants = context.get("approval_grants")
        reusable = _load_reusable_grant(
            approval_grants=approval_grants,
            session_id=session_id,
            tool_name=tool_name,
            risk_level=definition.risk_level,
            scope_signature=scope_signature,
        )
        decision = evaluator.evaluate(
            call,
            approval_mode=context.get("approval_mode", "normal"),
            reusable_grants=[reusable] if reusable is not None else [],
            session_id=session_id,
        )
        if decision.decision == "deny":
            result = _denied_result(
                decision.message or "Tool call denied by policy.",
                error_class=decision.error_class or "policy_denied",
            )
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
        if decision.decision == "ask":
            approval_provider = context.get("approval_provider") or FakeApprovalProvider("denied")
            request = _approval_request(tool_name, normalized.approval_target, definition.risk_level)
            approval_facts = {
                "tool_name": tool_name,
                "risk_level": definition.risk_level,
                "scope_signature": scope_signature,
                "target": normalized.approval_target,
            }
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="approval_requested",
                payload={
                    **approval_facts,
                    "approval_request": request,
                },
            )
            approval = approval_provider.request_approval(
                request,
                approval_facts,
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="approval_decision_recorded",
                payload={
                    **approval_facts,
                    "decision": approval.decision,
                    "grant_scope": approval.grant_scope,
                    "message": approval.message,
                },
            )
            _record_approval(
                approval_grants=approval_grants,
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                risk_level=definition.risk_level,
                scope_signature=scope_signature,
                approval_request=request,
                approval=approval,
            )
            if approval.decision not in {"approved_once", "approved_for_session"}:
                result = _denied_result("Approval denied.", error_class="policy_denied")
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

        tool_context = ToolUseContext(
            session_id=session_id,
            run_id=run_id,
            workspace_root=workspace_root,
            artifact_root=Path(context.get("artifact_root", self.artifact_store.sessions_root)),
            approval_mode=context.get("approval_mode", "normal"),
            frozen_config=context.get("frozen_config", {}),
            tool_definition=definition,
            frozen_policy=policy_facts,
            permission_evaluator=evaluator,
            approval_grants=approval_grants,
            approval_provider=context.get("approval_provider") or FakeApprovalProvider("denied"),
            event_writer=self.event_writer,
            artifact_store=self.artifact_store,
            skill_snapshot_store=context.get("skill_snapshot_store"),
            shell_runner=context.get("shell_runner"),
        )
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

        executor: ThreadPoolExecutor | None = None
        try:
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(self._router.route, tool_context, normalized_arguments)
            handler_output = future.result(timeout=route_timeout_seconds)
        except TimeoutError:
            future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            metadata = {}
            if definition.name == "shell_exec":
                metadata["effective_timeout_seconds"] = route_timeout_seconds
            result = _timeout_result(route_timeout_seconds, metadata=metadata)
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
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            result = tool_error_result(str(exc), source=tool_name)
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
        else:
            if executor is not None:
                executor.shutdown(wait=True)

        result = self._handler_result(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            output=handler_output,
        )
        event_kind = "tool_call_completed" if result.status == "ok" else "tool_call_failed"
        self._write_event(
            session_id=session_id,
            run_id=run_id,
            kind=event_kind,
            payload=_audit_payload(
                tool_name=tool_name,
                arguments=normalized_arguments,
                result=result,
                duration_seconds=monotonic() - start,
            ),
        )
        return result

    def _handler_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: str | dict[str, Any] | NativeHandlerResult,
    ) -> ToolResult:
        if isinstance(output, NativeHandlerResult):
            if output.status == "error":
                return tool_error_result(output.error_message or "Tool failed.", source=tool_name)
            return self._ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output.output or {},
                metadata=output.metadata or {},
            )
        if isinstance(output, ShellHandlerResult):
            if output.status == "timeout":
                return _timeout_result(
                    float((output.metadata or {}).get("effective_timeout_seconds", 0)),
                    metadata=output.metadata or {},
                )
            if output.status == "error":
                return tool_error_result(output.error_message or "Tool failed.", source=tool_name)
            return self._shell_ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output.output or {"stdout": "", "stderr": "", "returncode": 0},
                metadata=output.metadata or {},
            )
        return self._ok_result(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            output=output,
            metadata={},
        )

    def _ok_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: str | dict[str, Any],
        metadata: dict[str, Any],
    ) -> ToolResult:
        artifact_text = _artifact_text(output)
        output_size = len(artifact_text.encode("utf-8"))
        if output_size > LARGE_OUTPUT_THRESHOLD_BYTES:
            artifact = self.artifact_store.write_text(
                session_id=session_id,
                run_id=run_id,
                artifact_id=f"art_{uuid4().hex}",
                filename=f"{tool_name}_output.txt",
                content=artifact_text,
                metadata={
                    "tool_name": tool_name,
                    "bytes": output_size,
                },
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="artifact_registered",
                payload={
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "relative_path": artifact.relative_path,
                    "metadata": artifact.metadata,
                },
            )
            return ToolResult(
                status="ok",
                output=None,
                error=None,
                artifacts=[artifact.artifact_id],
                metadata={"bytes": output_size, **metadata},
                redacted_output=f"[output stored as artifact: {artifact.artifact_id}]",
            )
        return ToolResult(
            status="ok",
            output=output,
            error=None,
            artifacts=[],
            metadata=metadata,
            redacted_output=None,
        )

    def _shell_ok_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ToolResult:
        normalized = dict(output)
        artifacts: list[str] = []
        for stream_name in ("stdout", "stderr"):
            content = str(normalized.get(stream_name, ""))
            output_size = len(content.encode("utf-8"))
            if output_size <= LARGE_OUTPUT_THRESHOLD_BYTES:
                continue
            artifact = self.artifact_store.write_text(
                session_id=session_id,
                run_id=run_id,
                artifact_id=f"art_{uuid4().hex}",
                filename=f"{tool_name}_{stream_name}.txt",
                content=content,
                metadata={
                    "tool_name": tool_name,
                    "stream": stream_name,
                    "bytes": output_size,
                },
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="artifact_registered",
                payload={
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "relative_path": artifact.relative_path,
                    "metadata": artifact.metadata,
                },
            )
            artifacts.append(artifact.artifact_id)
            normalized[stream_name] = None
        return ToolResult(
            status="ok",
            output=normalized,
            error=None,
            artifacts=artifacts,
            metadata=metadata,
            redacted_output=None if not artifacts else "[shell stream output stored as artifact]",
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


def _validate_schema(definition: ToolDefinition, arguments: dict[str, Any]) -> str | None:
    schema = definition.input_schema
    allowed = set(schema.get("properties", {}))
    for key in arguments:
        if key not in allowed:
            return f"Unknown field: {key}"
    for key in schema.get("required", []):
        if key not in arguments:
            return f"Missing required field: {key}"
    for key, field_schema in schema.get("properties", {}).items():
        if key not in arguments:
            continue
        value = arguments[key]
        expected_type = field_schema.get("type")
        if expected_type == "string" and not isinstance(value, str):
            return f"{key} must be a string."
        if expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return f"{key} must be an integer."
            if field_schema.get("minimum") == 1 and value < 1:
                return f"{key} must be a positive integer."
        if expected_type == "array":
            if not isinstance(value, list):
                return f"{key} must be an array."
            min_items = field_schema.get("minItems")
            if min_items is not None and len(value) < min_items:
                return f"{key} must contain at least {min_items} item."
            item_schema = field_schema.get("items", {})
            if item_schema.get("type") == "string" and not all(
                isinstance(item, str) for item in value
            ):
                return f"{key} items must be strings."
    return None


def _normalize_tool_arguments(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    workspace_root: Path,
    frozen_config: dict[str, Any],
) -> NormalizedBrokerArguments:
    if definition.name == "shell_exec":
        return _normalize_shell_arguments(
            definition=definition,
            arguments=arguments,
            workspace_root=workspace_root,
            frozen_config=frozen_config,
        )
    canonical_path = canonicalize_path(arguments["path"], workspace_root)
    normalized_arguments = dict(arguments)
    normalized_arguments["path"] = str(canonical_path)
    scope_signature = scope_signature_for_tool(
        definition.name,
        risk_level=definition.risk_level,
        paths=[canonical_path],
    )
    return NormalizedBrokerArguments(
        arguments=normalized_arguments,
        paths=(canonical_path,),
        shell_argv=(),
        scope_signature=scope_signature,
        approval_target=str(canonical_path),
    )


def _normalize_shell_arguments(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    workspace_root: Path,
    frozen_config: dict[str, Any],
) -> NormalizedBrokerArguments:
    normalized_arguments = dict(arguments)
    cwd_argument = normalized_arguments.get("cwd")
    policy_cwd = canonicalize_path(
        cwd_argument if cwd_argument is not None else str(workspace_root),
        workspace_root,
    )
    execution_cwd = _execution_cwd_for_shell(cwd_argument, workspace_root)
    effective_timeout = _effective_shell_timeout(
        requested=normalized_arguments.get("timeout_seconds"),
        frozen_config=frozen_config,
    )
    classified = classify_argv_paths(normalized_arguments["argv"], workspace_root)
    classified_paths = [item.path for item in classified]
    paths = tuple([policy_cwd, *classified_paths])
    shell_argv = normalize_shell_argv(normalized_arguments["argv"])
    normalized_arguments["cwd"] = str(policy_cwd)
    normalized_arguments["execution_cwd"] = str(execution_cwd)
    normalized_arguments["effective_timeout_seconds"] = effective_timeout
    normalized_arguments["classified_paths"] = [
        {"original": item.original, "path": str(item.path)} for item in classified
    ]
    scope_signature = scope_signature_for_tool(
        definition.name,
        risk_level=definition.risk_level,
        shell_argv=shell_argv,
        cwd=policy_cwd,
        effective_timeout_seconds=effective_timeout,
        classified_paths=classified_paths,
    )
    return NormalizedBrokerArguments(
        arguments=normalized_arguments,
        paths=paths,
        shell_argv=shell_argv,
        scope_signature=scope_signature,
        approval_target=" ".join(normalized_arguments["argv"]),
    )


def _execution_cwd_for_shell(cwd: str | None, workspace_root: Path) -> Path:
    if cwd is None:
        return workspace_root
    if _is_windows_absolute_or_unc(cwd):
        return Path(cwd)
    candidate = Path(cwd)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return canonicalize_path(candidate, workspace_root)


def _is_windows_absolute_or_unc(path: str) -> bool:
    return (
        len(path) >= 3
        and path[1] == ":"
        and path[0].isalpha()
        and path[2] in {"\\", "/"}
    ) or path.startswith("\\\\")


def _effective_shell_timeout(
    *, requested: int | None, frozen_config: dict[str, Any]
) -> int:
    configured = (
        frozen_config.get("execution", {}).get("default_shell_timeout_seconds")
        if isinstance(frozen_config, dict)
        else None
    )
    default = configured if isinstance(configured, int) and configured > 0 else 300
    return min(requested, default) if requested is not None else default


def _load_reusable_grant(
    *,
    approval_grants: Any,
    session_id: str,
    tool_name: str,
    risk_level: str,
    scope_signature: str,
) -> ApprovalGrant | None:
    if approval_grants is None or not hasattr(approval_grants, "find_reusable"):
        return None
    grant = approval_grants.find_reusable(
        session_id=session_id,
        tool_name=tool_name,
        risk_level=risk_level,
        scope_signature=scope_signature,
    )
    if grant is None:
        return None
    return ApprovalGrant(
        session_id=grant.session_id,
        tool_name=grant.tool_name,
        risk_level=grant.risk_level,
        scope_signature=grant.scope_signature,
    )


def _record_approval(
    *,
    approval_grants: Any,
    session_id: str,
    run_id: str,
    tool_name: str,
    risk_level: str,
    scope_signature: str,
    approval_request: str,
    approval: ApprovalDecision,
) -> None:
    if approval_grants is None or not hasattr(approval_grants, "record"):
        return
    approval_grants.record(
        grant_id=f"grant_{uuid4().hex}",
        session_id=session_id,
        run_id=run_id,
        tool_name=tool_name,
        risk_level=risk_level,
        scope_signature=scope_signature,
        decision=approval.decision,
        grant_scope=approval.grant_scope,
        approval_request=approval_request,
    )


def _approval_request(tool_name: str, target: str, risk_level: str) -> str:
    return f"Allow {tool_name} {risk_level} access to {target}?"


def _artifact_text(output: str | dict[str, Any]) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, sort_keys=True)


def _denied_result(message: str, *, error_class: str) -> ToolResult:
    return ToolResult(
        status="denied",
        output=None,
        error={
            "error_class": error_class,
            "message": message,
            "source": "toolbroker",
            "recoverable": True,
        },
        artifacts=[],
        metadata={},
        redacted_output=None,
    )


def _timeout_result(timeout_seconds: float, *, metadata: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(
        status="timeout",
        output=None,
        error={
            "error_class": "timeout",
            "message": f"Tool timed out after {timeout_seconds:g} seconds.",
            "source": "toolbroker",
            "recoverable": True,
        },
        artifacts=[],
        metadata=metadata or {},
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
        payload["message"] = result.error["message"]
        payload["source"] = result.error["source"]
        payload["recoverable"] = result.error["recoverable"]
    if result.status == "ok":
        payload["result"] = result.to_dict()
    return payload

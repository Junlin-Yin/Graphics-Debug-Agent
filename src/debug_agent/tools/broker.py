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
    scope_signature_for_tool,
)
from debug_agent.tools.native import NativeHandlerResult, tool_definitions, tool_error_result, tool_handlers


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


class ToolRouter:
    CATEGORIES = frozenset({"native", "shell", "runtime_control"})

    def __init__(self) -> None:
        self._native_handlers = tool_handlers()

    def route(
        self, context: ToolUseContext, arguments: dict[str, Any]
    ) -> str | dict[str, Any] | NativeHandlerResult:
        if context.tool_definition.category != "native":
            raise RuntimeError("Only native handlers are enabled in Milestone 2A.")
        return self._native_handlers[context.tool_definition.name](context, arguments)


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
        self._definitions = {definition.name: definition for definition in tool_definitions()}
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
        canonical_path = canonicalize_path(normalized_arguments["path"], workspace_root)
        normalized_arguments["path"] = str(canonical_path)
        scope_signature = scope_signature_for_tool(
            tool_name,
            risk_level=definition.risk_level,
            paths=[canonical_path],
        )
        call = NormalizedToolCall(
            tool_name=tool_name,
            category=definition.category,
            risk_level=definition.risk_level,
            access=tuple(definition.access or ()),
            paths=(canonical_path,),
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
            request = _approval_request(tool_name, canonical_path, definition.risk_level)
            approval_facts = {
                "tool_name": tool_name,
                "risk_level": definition.risk_level,
                "scope_signature": scope_signature,
                "path": str(canonical_path),
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
            handler_output = future.result(timeout=timeout_seconds)
        except TimeoutError:
            future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            result = _timeout_result(timeout_seconds)
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
    return None


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


def _approval_request(tool_name: str, path: Path, risk_level: str) -> str:
    return f"Allow {tool_name} {risk_level} access to {path}?"


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


def _timeout_result(timeout_seconds: float) -> ToolResult:
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
        payload["message"] = result.error["message"]
        payload["source"] = result.error["source"]
        payload["recoverable"] = result.error["recoverable"]
    if result.status == "ok":
        payload["result"] = result.to_dict()
    return payload

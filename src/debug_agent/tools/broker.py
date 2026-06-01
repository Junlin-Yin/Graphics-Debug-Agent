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
    PermissionDecision,
    PermissionEvaluator,
    PolicyFacts,
    build_builtin_policy,
    canonicalize_path,
    classify_argv_paths,
    normalize_shell_argv,
    policy_facts_from_snapshot,
    scope_signature_for_tool,
)
from debug_agent.tools import runtime_control as runtime_control_tools
from debug_agent.tools import shell as shell_tools
from debug_agent.tools import view_image as view_image_tools
from debug_agent.tools.native import NativeHandlerResult, tool_definitions, tool_error_result, tool_handlers
from debug_agent.tools.runtime_control import RuntimeControlHandlerResult
from debug_agent.tools.shell import ShellHandlerResult
from debug_agent.tools.view_image import ViewImageResult


LARGE_OUTPUT_THRESHOLD_BYTES = 16 * 1024
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ApprovalDecision:
    decision: str
    grant_scope: str = "none"
    message: str | None = None


class ApprovalProvider(Protocol):
    is_interactive: bool

    def request_approval(self, request: str, facts: dict[str, Any]) -> ApprovalDecision:
        ...


class FakeApprovalProvider:
    is_interactive = True

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


class NonInteractiveApprovalProvider:
    is_interactive = False

    def request_approval(self, request: str, facts: dict[str, Any]) -> ApprovalDecision:
        return ApprovalDecision(
            "denied",
            "none",
            "Interactive approval is unavailable.",
        )


@dataclass(frozen=True)
class NormalizedBrokerArguments:
    arguments: dict[str, Any]
    paths: tuple[Path, ...]
    shell_argv: tuple[str, ...]
    scope_signature: str
    target: str
    runtime_control_valid: bool = True
    runtime_control_error_message: str | None = None
    runtime_control_error_class: str = "config_error"
    runtime_control_already_active: bool = False


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
    run_store: Any = None
    shell_runner: Any = None
    todo_plan_store: Any = None
    vision_client: Any = None
    view_image_reader: Any = None
    effective_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS


class ToolRouter:
    CATEGORIES = frozenset({"native", "shell", "runtime_control"})

    def __init__(self) -> None:
        self._native_handlers = tool_handlers()
        self._shell_handlers = shell_tools.tool_handlers()
        self._runtime_control_handlers = runtime_control_tools.tool_handlers()

    def route(
        self, context: ToolUseContext, arguments: dict[str, Any]
    ) -> str | dict[str, Any] | NativeHandlerResult:
        if context.tool_definition.category == "native":
            if context.tool_definition.name == "view_image":
                tool = view_image_tools.ViewImageTool(
                    vision_client=context.vision_client,
                    image_reader=context.view_image_reader,
                )
                return tool.execute(
                    context,
                    arguments,
                    timeout_seconds=context.effective_timeout_seconds,
                )
            return self._native_handlers[context.tool_definition.name](context, arguments)
        if context.tool_definition.category == "shell":
            return self._shell_handlers[context.tool_definition.name](context, arguments)
        if context.tool_definition.category == "runtime_control":
            return self._runtime_control_handlers[context.tool_definition.name](
                context, arguments
            )
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
        definitions = [
            *tool_definitions(),
            view_image_tools.tool_definition(),
            *shell_tools.tool_definitions(),
            *runtime_control_tools.tool_definitions(),
        ]
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
        approval_wait_duration_ms = 0
        tool_audit_recorder = context.get("tool_audit_recorder")
        if not isinstance(arguments, dict):
            result = _denied_result("Tool arguments must be an object.", error_class="user_error")
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_denied",
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments={"invalid_arguments": repr(arguments)},
                    result=result,
                    duration_seconds=monotonic() - start,
                    target="",
                    approval_wait_duration_ms=0,
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
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                    target="",
                    approval_wait_duration_ms=0,
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
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                    target="",
                    approval_wait_duration_ms=0,
                ),
            )
            return result
        semantic_error = _validate_view_image_semantics(
            definition=definition,
            arguments=normalized_arguments,
            frozen_config=context.get("frozen_config", {}),
        )
        if semantic_error is not None:
            result = _denied_result(semantic_error, error_class="user_error")
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_denied",
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                    target="",
                    approval_wait_duration_ms=0,
                ),
            )
            return result
        view_image_denial = _view_image_availability_denial(
            tool_name=tool_name,
            frozen_config=context.get("frozen_config", {}),
        )
        if view_image_denial is not None:
            result = _denied_result(view_image_denial, error_class="config_error")
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_denied",
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                    target="",
                    approval_wait_duration_ms=0,
                ),
            )
            return result

        policy_facts = _policy_facts_from_context(context, workspace_root)
        evaluator = context.get("permission_evaluator") or PermissionEvaluator(policy_facts)
        normalized = _normalize_tool_arguments(
            definition=definition,
            arguments=normalized_arguments,
            workspace_root=workspace_root,
            frozen_config=context.get("frozen_config", {}),
            runtime_context=context,
            session_id=session_id,
            run_id=run_id,
            artifact_store=self.artifact_store,
        )
        normalized_arguments = normalized.arguments
        if not normalized.runtime_control_valid:
            result = _denied_result(
                normalized.runtime_control_error_message
                or "Invalid runtime-control target.",
                error_class=normalized.runtime_control_error_class,
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_denied",
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                    target=normalized.target,
                    approval_wait_duration_ms=0,
                ),
            )
            return result
        route_timeout_seconds = timeout_seconds
        if definition.name == "shell_exec":
            route_timeout_seconds = float(normalized_arguments["effective_timeout_seconds"])
        if definition.name == "view_image":
            route_timeout_seconds = _effective_view_image_timeout(
                frozen_config=context.get("frozen_config", {}),
                fallback=timeout_seconds,
            )
        scope_signature = normalized.scope_signature
        call = NormalizedToolCall(
            tool_name=tool_name,
            category=definition.category,
            risk_level=definition.risk_level,
            access=tuple(definition.access or ()),
            paths=normalized.paths,
            shell_argv=normalized.shell_argv,
            approval_scope_signature=scope_signature,
            runtime_control_valid=normalized.runtime_control_valid,
            runtime_control_already_active=normalized.runtime_control_already_active,
        )
        approval_grants = context.get("approval_grants")
        if tool_name == "todo":
            decision = PermissionDecision("allow", "todo_audit_only")
        else:
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
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - start,
                    target=normalized.target,
                    approval_wait_duration_ms=0,
                ),
            )
            return result
        if decision.decision == "ask":
            approval_provider = context.get("approval_provider") or NonInteractiveApprovalProvider()
            request = _approval_request(
                tool_name=tool_name,
                target=normalized.target,
                risk_level=definition.risk_level,
                grant_scope="once or session",
            )
            is_interactive_prompt = bool(
                getattr(approval_provider, "is_interactive", False)
            )
            approval_facts = {
                "tool_name": tool_name,
                "risk_level": definition.risk_level,
                "scope_signature": scope_signature,
                "target": normalized.target,
                "grant_scope": "once or session",
            }
            if is_interactive_prompt:
                self._write_event(
                    session_id=session_id,
                    run_id=run_id,
                    kind="approval_requested",
                    payload={
                        **approval_facts,
                        "approval_request": request,
                    },
                )
            approval_start = monotonic()
            approval = approval_provider.request_approval(
                request,
                approval_facts,
            )
            if is_interactive_prompt:
                approval_wait_duration_ms = max(
                    0, round((monotonic() - approval_start) * 1000)
                )
            if is_interactive_prompt:
                self._write_event(
                    session_id=session_id,
                    run_id=run_id,
                    kind="approval_decision_recorded",
                    payload={
                        **approval_facts,
                        "decision": approval.decision,
                        "grant_scope": approval.grant_scope,
                        "message": approval.message,
                        "approval_wait_duration_ms": approval_wait_duration_ms,
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
                result = _denied_result(
                    approval.message or "Approval denied.",
                    error_class="policy_denied",
                    metadata={"turn_aborted": True},
                )
                self._write_event(
                    session_id=session_id,
                    run_id=run_id,
                    kind="tool_call_denied",
                    audit_recorder=tool_audit_recorder,
                    payload=_audit_payload(
                        tool_name=tool_name,
                        arguments=normalized_arguments,
                        result=result,
                        duration_seconds=monotonic() - start,
                        target=normalized.target,
                        approval_wait_duration_ms=approval_wait_duration_ms,
                    ),
                )
                return result

        execution_start = monotonic()
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
            approval_provider=context.get("approval_provider")
            or NonInteractiveApprovalProvider(),
            event_writer=self.event_writer,
            artifact_store=self.artifact_store,
            skill_snapshot_store=context.get("skill_snapshot_store"),
            run_store=context.get("run_store"),
            shell_runner=context.get("shell_runner"),
            todo_plan_store=context.get("todo_plan_store"),
            vision_client=context.get("vision_client"),
            view_image_reader=context.get("view_image_reader"),
            effective_timeout_seconds=route_timeout_seconds,
        )
        self._write_event(
            session_id=session_id,
            run_id=run_id,
            kind="tool_call_started",
            audit_recorder=tool_audit_recorder,
            payload={
                "tool_name": tool_name,
                "arguments": _audit_arguments(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                ),
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
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - execution_start,
                    target=normalized.target,
                    approval_wait_duration_ms=approval_wait_duration_ms,
                    include_execution_duration=True,
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
                audit_recorder=tool_audit_recorder,
                payload=_audit_payload(
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    result=result,
                    duration_seconds=monotonic() - execution_start,
                    target=normalized.target,
                    approval_wait_duration_ms=approval_wait_duration_ms,
                    include_execution_duration=True,
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
            audit_recorder=tool_audit_recorder,
            payload=_audit_payload(
                tool_name=tool_name,
                arguments=normalized_arguments,
                result=result,
                duration_seconds=monotonic() - execution_start,
                target=normalized.target,
                approval_wait_duration_ms=approval_wait_duration_ms,
                include_execution_duration=True,
            ),
        )
        self._write_runtime_control_observability_event(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            result=result,
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
                return tool_error_result(
                    output.error_message or "Tool failed.",
                    source=tool_name,
                    metadata=output.metadata or {},
                )
            return self._ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output.output or {},
                metadata=output.metadata or {},
            )
        if isinstance(output, ViewImageResult):
            result = view_image_tools.tool_result_from_view_image(output, source=tool_name)
            if (
                result.status == "ok"
                and output.raw_provider_text is not None
                and len(output.raw_provider_text.encode("utf-8"))
                > LARGE_OUTPUT_THRESHOLD_BYTES
            ):
                artifact = self.artifact_store.write_text(
                    session_id=session_id,
                    run_id=run_id,
                    artifact_id=f"art_{uuid4().hex}",
                    filename=f"{tool_name}_raw_provider_output.txt",
                    content=output.raw_provider_text,
                    metadata={
                        "tool_name": tool_name,
                        "bytes": len(output.raw_provider_text.encode("utf-8")),
                        "source": "raw_provider_output",
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
                    status=result.status,
                    output=result.output,
                    error=result.error,
                    artifacts=[artifact.artifact_id],
                    metadata=result.metadata,
                    redacted_output=result.redacted_output,
                )
            return result
        if isinstance(output, ShellHandlerResult):
            if output.status == "timeout":
                return _timeout_result(
                    float((output.metadata or {}).get("effective_timeout_seconds", 0)),
                    metadata=output.metadata or {},
                )
            if output.status == "error":
                return tool_error_result(
                    output.error_message or "Tool failed.",
                    source=tool_name,
                    metadata=output.metadata or {},
                )
            return self._shell_ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output.output or {"stdout": "", "stderr": "", "returncode": 0},
                metadata=output.metadata or {},
            )
        if isinstance(output, RuntimeControlHandlerResult):
            if output.status == "denied":
                return _denied_result(
                    output.error_message or "Runtime-control target denied.",
                    error_class=output.error_class,
                )
            if output.status == "error":
                return tool_error_result(output.error_message or "Tool failed.", source=tool_name)
            result = self._ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output.output or {},
                metadata=output.metadata or {},
            )
            if (
                tool_name == "todo"
                and output.metadata is not None
                and "redacted_output" in output.metadata
            ):
                metadata = dict(result.metadata)
                redacted_output = metadata.pop("redacted_output")
                return ToolResult(
                    status=result.status,
                    output=result.output,
                    error=result.error,
                    artifacts=result.artifacts,
                    metadata=metadata,
                    redacted_output=redacted_output,
                )
            return result
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
        self,
        *,
        session_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        audit_recorder: Any = None,
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
        if callable(audit_recorder):
            audit_recorder(kind, payload)

    def _write_runtime_control_observability_event(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        result: ToolResult,
    ) -> None:
        if result.status != "ok":
            return
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        if tool_name == "activate_skill":
            if metadata.get("already_active") is True:
                return
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="skill_activated",
                payload={
                    "skill_name": metadata.get("skill_name", ""),
                    "content_hash": metadata.get("content_hash", ""),
                    "activation_reason": metadata.get("activation_reason", ""),
                    "scope": metadata.get("scope", ""),
                },
            )
            return
        if tool_name == "load_skill_resource":
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="skill_resource_loaded",
                payload={
                    "skill_name": metadata.get("skill_name", ""),
                    "skill_content_hash": metadata.get("skill_content_hash", ""),
                    "resource_path": metadata.get("resource_path", ""),
                    "resource_kind": metadata.get("resource_kind", ""),
                    "resource_content_hash": metadata.get(
                        "resource_content_hash", ""
                    ),
                    "media_kind": metadata.get("media_kind", ""),
                    "size_bytes": metadata.get("size_bytes", 0),
                    "artifact_id": metadata.get("artifact_id"),
                },
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
            max_items = field_schema.get("maxItems")
            if max_items is not None and len(value) > max_items:
                return f"{key} must contain at most {max_items} items."
            if item_schema.get("type") == "object":
                item_error = _validate_object_array_items(key, value, item_schema)
                if item_error is not None:
                    return item_error
    return None


def _validate_object_array_items(
    field_name: str, values: list[Any], item_schema: dict[str, Any]
) -> str | None:
    allowed = set(item_schema.get("properties", {}))
    for index, item in enumerate(values, start=1):
        if not isinstance(item, dict):
            return f"{field_name}[{index}] must be an object."
        for key in item:
            if key not in allowed:
                return f"Unknown field: {field_name}[{index}].{key}"
        for key in item_schema.get("required", []):
            if key not in item:
                return f"Missing required field: {field_name}[{index}].{key}"
        for key, field_schema in item_schema.get("properties", {}).items():
            if key not in item:
                continue
            value = item[key]
            expected_type = field_schema.get("type")
            if expected_type == "string" and not isinstance(value, str):
                return f"{field_name}[{index}].{key} must be a string."
            enum = field_schema.get("enum")
            if enum is not None and value not in enum:
                return f"{field_name}[{index}].{key} must be one of: {', '.join(enum)}."
    return None


def _validate_view_image_semantics(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    frozen_config: dict[str, Any],
) -> str | None:
    if definition.name != "view_image":
        return None
    paths = arguments.get("paths")
    if isinstance(paths, list) and any(not path.strip() for path in paths):
        return "paths items must be non-empty strings."
    query = arguments.get("query")
    if query is None:
        return None
    trimmed = query.strip()
    if not trimmed:
        return "query must be non-empty when provided."
    multimodal = frozen_config.get("multimodal") if isinstance(frozen_config, dict) else None
    max_query_chars = (
        multimodal.get("max_query_chars")
        if isinstance(multimodal, dict)
        else 8192
    )
    if not isinstance(max_query_chars, int) or isinstance(max_query_chars, bool):
        max_query_chars = 8192
    if len(trimmed) > max_query_chars:
        return "query exceeds max_query_chars."
    return None


def _policy_facts_from_context(context: dict[str, Any], workspace_root: Path) -> PolicyFacts:
    policy_facts = context.get("policy_facts")
    if isinstance(policy_facts, PolicyFacts):
        return policy_facts
    frozen_config = context.get("frozen_config")
    policy_snapshot = frozen_config.get("policy") if isinstance(frozen_config, dict) else None
    if isinstance(policy_snapshot, dict):
        return policy_facts_from_snapshot(policy_snapshot, workspace_root)
    return build_builtin_policy(workspace_root)


def _view_image_availability_denial(
    *, tool_name: str, frozen_config: dict[str, Any]
) -> str | None:
    if tool_name != "view_image" or not isinstance(frozen_config, dict):
        return None
    multimodal = frozen_config.get("multimodal")
    if not isinstance(multimodal, dict):
        return "view_image is disabled: missing_multimodal_config"
    if multimodal.get("view_image_enabled") is True:
        return None
    reason = multimodal.get("view_image_disabled_reason")
    if not isinstance(reason, str) or not reason:
        reason = "missing_multimodal_config"
    return f"view_image is disabled: {reason}"


def _normalize_tool_arguments(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    workspace_root: Path,
    frozen_config: dict[str, Any],
    runtime_context: dict[str, Any],
    session_id: str,
    run_id: str,
    artifact_store: ArtifactStore,
) -> NormalizedBrokerArguments:
    if definition.name == "shell_exec":
        return _normalize_shell_arguments(
            definition=definition,
            arguments=arguments,
            workspace_root=workspace_root,
            frozen_config=frozen_config,
        )
    if definition.category == "runtime_control":
        return _normalize_runtime_control_arguments(
            definition=definition,
            arguments=arguments,
            workspace_root=workspace_root,
            runtime_context=runtime_context,
            session_id=session_id,
            run_id=run_id,
            artifact_store=artifact_store,
        )
    if definition.name == "view_image":
        return _normalize_view_image_arguments(
            definition=definition,
            arguments=arguments,
            workspace_root=workspace_root,
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
        target=_target_for_path_tool(definition.name, normalized_arguments, canonical_path),
    )


def _normalize_view_image_arguments(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    workspace_root: Path,
) -> NormalizedBrokerArguments:
    canonical_paths = [canonicalize_path(path, workspace_root) for path in arguments["paths"]]
    normalized_arguments = dict(arguments)
    normalized_arguments["paths"] = [str(path) for path in canonical_paths]
    symlink_escapes: list[str] = []
    for raw_path, canonical_path in zip(arguments["paths"], canonical_paths, strict=True):
        lexical_path = Path(raw_path)
        if not lexical_path.is_absolute():
            lexical_path = workspace_root / lexical_path
        try:
            lexical_path.relative_to(workspace_root)
            canonical_path.relative_to(workspace_root)
        except ValueError:
            if _path_lexically_under_workspace(lexical_path, workspace_root):
                symlink_escapes.append(str(raw_path))
    if symlink_escapes:
        normalized_arguments["_view_image_symlink_escapes"] = symlink_escapes
    scope_signature = scope_signature_for_tool(
        definition.name,
        risk_level=definition.risk_level,
        paths=canonical_paths,
    )
    return NormalizedBrokerArguments(
        arguments=normalized_arguments,
        paths=tuple(canonical_paths),
        shell_argv=(),
        scope_signature=scope_signature,
        target=", ".join(str(path) for path in canonical_paths),
    )


def _path_lexically_under_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.relative_to(workspace_root)
        return True
    except ValueError:
        return False


def _normalize_runtime_control_arguments(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    workspace_root: Path,
    runtime_context: dict[str, Any],
    session_id: str,
    run_id: str,
    artifact_store: ArtifactStore,
) -> NormalizedBrokerArguments:
    pseudo_context = ToolUseContext(
        session_id=session_id,
        run_id=run_id,
        workspace_root=workspace_root,
        artifact_root=Path(runtime_context.get("artifact_root", artifact_store.sessions_root)),
        approval_mode=runtime_context.get("approval_mode", "normal"),
        frozen_config=runtime_context.get("frozen_config", {}),
        tool_definition=definition,
        frozen_policy=_policy_facts_from_context(runtime_context, workspace_root),
        permission_evaluator=runtime_context.get("permission_evaluator")
        or PermissionEvaluator(_policy_facts_from_context(runtime_context, workspace_root)),
        approval_grants=runtime_context.get("approval_grants"),
        approval_provider=runtime_context.get("approval_provider")
        or NonInteractiveApprovalProvider(),
        event_writer=runtime_context["event_writer"]
        if "event_writer" in runtime_context
        else None,
        artifact_store=artifact_store,
        skill_snapshot_store=runtime_context.get("skill_snapshot_store"),
        run_store=runtime_context.get("run_store"),
        shell_runner=runtime_context.get("shell_runner"),
        todo_plan_store=runtime_context.get("todo_plan_store"),
    )
    target = runtime_control_tools.validate_target(
        pseudo_context,
        definition.name,
        arguments,
    )
    normalized_arguments = dict(arguments)
    if definition.name == "todo":
        return NormalizedBrokerArguments(
            arguments=normalized_arguments,
            paths=(),
            shell_argv=(),
            scope_signature=scope_signature_for_tool(
                definition.name,
                risk_level=definition.risk_level,
            ),
            target="todo plan",
            runtime_control_valid=target.valid,
            runtime_control_error_message=target.error_message,
            runtime_control_error_class=target.error_class,
        )
    if definition.name == "activate_skill":
        skill_name = arguments["name"]
        skill_hash = target.skill.overall_content_hash if target.skill is not None else None
        scope_signature = scope_signature_for_tool(
            definition.name,
            risk_level=definition.risk_level,
            skill_name=skill_name,
            skill_content_hash=skill_hash,
        )
        return NormalizedBrokerArguments(
            arguments=normalized_arguments,
            paths=(),
            shell_argv=(),
            scope_signature=scope_signature,
            target=f"skill {skill_name}",
            runtime_control_valid=target.valid,
            runtime_control_error_message=target.error_message,
            runtime_control_error_class=target.error_class,
            runtime_control_already_active=target.already_active,
        )
    skill_name = arguments["skill_name"]
    resource_path = target.normalized_path or arguments["path"]
    if target.normalized_path is not None:
        normalized_arguments["path"] = target.normalized_path
    scope_signature = scope_signature_for_tool(
        definition.name,
        risk_level=definition.risk_level,
        skill_name=skill_name,
        skill_content_hash=target.skill.overall_content_hash if target.skill else None,
        resource_path=resource_path,
        resource_kind=target.resource.resource_kind if target.resource else None,
        resource_content_hash=target.resource.content_hash if target.resource else None,
    )
    resource_kind = target.resource.resource_kind if target.resource else "unknown"
    return NormalizedBrokerArguments(
        arguments=normalized_arguments,
        paths=(),
        shell_argv=(),
        scope_signature=scope_signature,
        target=f"skill resource {skill_name}:{resource_path} ({resource_kind})",
        runtime_control_valid=target.valid,
        runtime_control_error_message=target.error_message,
        runtime_control_error_class=target.error_class,
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
        target=_target_for_shell(normalized_arguments["argv"], policy_cwd, workspace_root),
    )


def _target_for_path_tool(
    tool_name: str, arguments: dict[str, Any], canonical_path: Path
) -> str:
    if tool_name == "search_text":
        return f"{arguments['query']} in {canonical_path}"
    return str(canonical_path)


def _target_for_shell(argv: list[str], policy_cwd: Path, workspace_root: Path) -> str:
    command_preview = " ".join(argv)
    if policy_cwd == workspace_root:
        return command_preview
    return f"{command_preview} (cwd: {policy_cwd})"


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


def _effective_view_image_timeout(
    *, frozen_config: dict[str, Any], fallback: float
) -> float:
    multimodal = frozen_config.get("multimodal") if isinstance(frozen_config, dict) else None
    timeout = multimodal.get("timeout_seconds") if isinstance(multimodal, dict) else None
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout)
    return fallback


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


def _approval_request(
    *,
    tool_name: str,
    target: str,
    risk_level: str,
    grant_scope: str,
) -> str:
    return "\n".join(
        [
            "=== Approval Request ===",
            f"Tool: {tool_name}",
            f"Target: {target}",
            "",
            "Allow? [y]once, [a] session, [n] deny",
        ]
    )


def _artifact_text(output: str | dict[str, Any]) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, sort_keys=True)


def _denied_result(
    message: str,
    *,
    error_class: str,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
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
        metadata=metadata or {},
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
    target: str,
    approval_wait_duration_ms: int,
    include_execution_duration: bool = False,
) -> dict[str, Any]:
    audit_arguments = _audit_arguments(tool_name=tool_name, arguments=arguments)
    payload = {
        "tool_name": tool_name,
        "arguments": audit_arguments,
        "target": target,
        "status": result.status,
        "duration": duration_seconds,
        "approval_wait_duration_ms": approval_wait_duration_ms,
        "artifact_ids": result.artifacts,
    }
    if include_execution_duration:
        payload["execution_duration_ms"] = max(0, round(duration_seconds * 1000))
    if result.error is not None:
        payload["error_class"] = result.error["error_class"]
        payload["message"] = result.error["message"]
        payload["source"] = result.error["source"]
        payload["recoverable"] = result.error["recoverable"]
    if result.status == "ok":
        payload["result"] = result.to_dict()
    if tool_name == "view_image":
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        source = metadata.get("effective_query_source")
        if isinstance(source, str):
            payload["effective_query_source"] = source
        images = metadata.get("images")
        if isinstance(images, list):
            payload["images"] = images
        if "vision_provider" in metadata:
            payload["vision_provider"] = metadata["vision_provider"]
        if "vision_model" in metadata:
            payload["vision_model"] = metadata["vision_model"]
        if "duration_ms" in metadata:
            payload["duration_ms"] = metadata["duration_ms"]
        if "projected_request_bytes" in metadata:
            payload["projected_request_bytes"] = metadata["projected_request_bytes"]
    return payload


def _audit_arguments(*, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name != "view_image":
        return arguments
    redacted: dict[str, Any] = {}
    paths = arguments.get("paths")
    if isinstance(paths, list):
        redacted["paths"] = [str(path) for path in paths]
    symlink_escapes = arguments.get("_view_image_symlink_escapes")
    if isinstance(symlink_escapes, list):
        redacted["_view_image_symlink_escapes"] = [str(path) for path in symlink_escapes]
    redacted["effective_query_source"] = (
        "assistant" if isinstance(arguments.get("query"), str) else "default"
    )
    return redacted

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.runtime.contracts import RunEvent, ToolDefinition, ToolResult, utc_now_iso
from debug_agent.runtime.errors import NormalizedError
from debug_agent.runtime.provider_execution import ProviderBoundaryNotClosed
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
from debug_agent.tools.native import (
    NativeHandlerResult,
    SEARCH_TEXT_TYPES,
    is_search_text_type_allowed,
    tool_definitions,
    tool_error_result,
    tool_handlers,
    validate_list_dir_ignore_patterns,
    validate_portable_glob_pattern,
)
from debug_agent.tools.runtime_control import RuntimeControlHandlerResult
from debug_agent.tools.settings import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    FIND_FILE_DEFAULT_MAX_RESULTS,
    FIND_FILE_MAX_RESULTS,
    LARGE_OUTPUT_THRESHOLD_BYTES,
    LIST_DIR_DEFAULT_LIMIT,
    LIST_DIR_MAX_IGNORE_PATTERNS,
    LIST_DIR_MAX_LIMIT,
    READ_FILE_DEFAULT_LIMIT,
    READ_FILE_MAX_LIMIT,
)
from debug_agent.tools.shell import ShellHandlerResult
from debug_agent.tools.view_image import ViewImageResult


_FIELD_ARTIFACT_ORDER = (
    ("read_file", "content"),
    ("search_text", "matches"),
    ("search_text", "paths"),
    ("search_text", "counts"),
    ("shell_exec", "stdout"),
    ("shell_exec", "stderr"),
)

_CACHE_ADVANCING_SOURCE_TOOLS = frozenset(
    {
        "read_file",
        "edit_file",
        "write_file",
    }
)


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
class FileMetadataCacheEntry:
    sha256: str
    size: int
    mtime_ns: int
    observed_at: str
    source_tool: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "observed_at": self.observed_at,
            "source_tool": self.source_tool,
        }


@dataclass(frozen=True)
class PreparedToolResult:
    status: str
    output: Any = None
    error_message: str | None = None
    error_class: str = "tool_error"
    reason: str | None = None
    artifacts: tuple[Any, ...] = ()
    metadata: dict[str, Any] | None = None
    redacted_output: Any = None


class _ToolDeadlineExceeded(Exception):
    pass


@dataclass(frozen=True)
class _ToolDeadline:
    expires_at: float

    def check(self) -> None:
        if monotonic() >= self.expires_at:
            raise _ToolDeadlineExceeded


class FileMetadataCache:
    def __init__(self) -> None:
        self._entries: dict[str, FileMetadataCacheEntry] = {}
        self._lock = threading.RLock()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {path: entry.to_dict() for path, entry in self._entries.items()}

    def get(self, path: str | Path) -> FileMetadataCacheEntry | None:
        canonical = str(Path(path).resolve())
        with self._lock:
            return self._entries.get(canonical)

    def commit(self, updates: list[tuple[str, FileMetadataCacheEntry]]) -> None:
        if not updates:
            return
        with self._lock:
            for path, entry in updates:
                self._entries[path] = entry


class FileMetadataUpdateStage:
    def __init__(self, cache: FileMetadataCache) -> None:
        self._cache = cache
        self._updates: list[tuple[str, FileMetadataCacheEntry]] = []
        self._lock = threading.Lock()
        self._abandoned = False

    def record(self, path: str | Path, *, source_tool: str) -> None:
        update = _file_metadata_update(path, source_tool=source_tool)
        with self._lock:
            if self._abandoned:
                return
            self._updates.append(update)

    def take_updates(self) -> list[tuple[str, FileMetadataCacheEntry]]:
        with self._lock:
            if self._abandoned:
                self._updates.clear()
                return []
            updates = list(self._updates)
            self._updates.clear()
            return updates

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            self._updates.clear()


class WriteLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.Lock()

    @contextmanager
    def lock_for_path(self, path: str | Path):
        canonical = str(Path(path).resolve())
        with self._registry_lock:
            lock = self._locks.setdefault(canonical, threading.RLock())
        with lock:
            yield


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
    shell_process_registry: Any = None
    todo_plan_store: Any = None
    vision_client: Any = None
    view_image_reader: Any = None
    provider_cancellation_registry: Any = None
    effective_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    cancellation_timeout_seconds: float = 10
    file_metadata_cache_stage: FileMetadataUpdateStage | None = None
    file_metadata_cache: FileMetadataCache | None = None
    write_lock_registry: WriteLockRegistry | None = None
    tool_deadline: _ToolDeadline | None = None
    created_directories: list[str] = field(default_factory=list)
    side_effect_lock: threading.Lock = field(default_factory=threading.Lock)

    def record_file_metadata(self, path: str | Path, *, source_tool: str) -> None:
        if self.file_metadata_cache_stage is None:
            raise RuntimeError("File metadata cache stage is unavailable.")
        self.file_metadata_cache_stage.record(path, source_tool=source_tool)

    def guard_existing_file(self, path: str | Path) -> FileMetadataCacheEntry:
        if self.file_metadata_cache is None:
            raise RuntimeError("File metadata cache is unavailable.")
        canonical = Path(path).resolve()
        entry = self.file_metadata_cache.get(canonical)
        if entry is None:
            raise RuntimeError("read_file first before modifying an existing file.")
        current_sha = _file_sha256(canonical)
        if current_sha != entry.sha256:
            raise RuntimeError("File changed since last observation; run read_file again.")
        return entry

    def check_deadline(self) -> None:
        if self.tool_deadline is not None:
            self.tool_deadline.check()

    def record_created_directory(self, path: str | Path) -> None:
        canonical = str(Path(path).resolve())
        with self.side_effect_lock:
            if canonical not in self.created_directories:
                self.created_directories.append(canonical)

    def write_side_effect_metadata(self) -> dict[str, Any]:
        with self.side_effect_lock:
            created = list(self.created_directories)
        if not created:
            return {}
        return {
            "side_effects": {"created_directories": created},
            "file_write_completed": False,
            "cache_updated": False,
        }

    def write_lock_for_path(self, path: str | Path):
        if self.write_lock_registry is None:
            raise RuntimeError("Write lock registry is unavailable.")
        return self.write_lock_registry.lock_for_path(path)


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
                    cleanup_timeout_seconds=context.cancellation_timeout_seconds,
                    register_cancellation_handle=context.provider_cancellation_registry,
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
        self._file_metadata_cache = FileMetadataCache()
        self._write_locks = WriteLockRegistry()
        definitions = [
            *tool_definitions(),
            view_image_tools.tool_definition(),
            *shell_tools.tool_definitions(),
            *runtime_control_tools.tool_definitions(),
        ]
        self._definitions = {definition.name: definition for definition in definitions}
        self._router = router or ToolRouter()

    def file_metadata_cache_snapshot(self) -> dict[str, dict[str, Any]]:
        return self._file_metadata_cache.snapshot()

    def _stage_file_metadata_for_test(
        self, path: str | Path, *, source_tool: str
    ) -> FileMetadataUpdateStage:
        stage = FileMetadataUpdateStage(self._file_metadata_cache)
        stage.record(path, source_tool=source_tool)
        return stage

    def _commit_file_metadata_stage_for_test(
        self, stage: FileMetadataUpdateStage
    ) -> None:
        self._file_metadata_cache.commit(stage.take_updates())

    def _write_lock_for_path_for_test(self, path: str | Path):
        return self._write_locks.lock_for_path(path)

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
            result = _error_result(
                "Tool arguments must be an object.",
                error_class="tool_error",
                reason="tool_schema_invalid",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
        timeout_seconds = _generic_tool_timeout_seconds(context, self.timeout_seconds)

        definition = self._definitions.get(tool_name)
        if definition is None or not tool_name.strip():
            result = _error_result(
                "Invalid tool name." if not tool_name.strip() else f"Unknown tool: {tool_name}",
                error_class="tool_error",
                reason="unknown_tool",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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

        raw_argument_keys = frozenset(normalized_arguments)
        schema_error = _validate_schema(definition, normalized_arguments)
        if schema_error is not None:
            result = _error_result(
                schema_error,
                error_class="tool_error",
                reason="tool_schema_invalid",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
        search_semantic_error = _validate_search_text_semantics(
            definition=definition,
            arguments=normalized_arguments,
            raw_argument_keys=raw_argument_keys,
        )
        if search_semantic_error is not None:
            result = _error_result(
                search_semantic_error,
                error_class="tool_error",
                reason="tool_schema_invalid",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
        native_semantic_error = _validate_phase_3_5_native_semantics(
            definition=definition,
            arguments=normalized_arguments,
        )
        if native_semantic_error is not None:
            result = _error_result(
                native_semantic_error,
                error_class="tool_error",
                reason="tool_schema_invalid",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
            result = _error_result(
                semantic_error,
                error_class="tool_error",
                reason="tool_schema_invalid",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
        shell_timeout_error = _validate_shell_timeout_semantics(
            definition=definition,
            arguments=normalized_arguments,
            frozen_config=context.get("frozen_config", {}),
        )
        if shell_timeout_error is not None:
            result = _error_result(
                shell_timeout_error,
                error_class="tool_error",
                reason="tool_schema_invalid",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
            result = _error_result(
                view_image_denial,
                error_class="config_error",
                reason="tool_unavailable",
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
            result = _error_result(
                normalized.runtime_control_error_message
                or "Invalid runtime-control target.",
                error_class=normalized.runtime_control_error_class,
                reason="tool_schema_invalid"
                if normalized.runtime_control_error_class == "tool_error"
                else None,
                metadata={"tool_name": tool_name},
            )
            self._write_event(
                session_id=session_id,
                run_id=run_id,
                kind="tool_call_failed",
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
                error_class="policy_error",
                reason=_policy_denial_reason(decision),
                metadata={"tool_name": tool_name},
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
                planned_parent_directories=normalized_arguments.get(
                    "planned_parent_directories", []
                )
                if tool_name == "write_file"
                else [],
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
            if tool_name == "write_file":
                approval_facts["planned_parent_directories"] = list(
                    normalized_arguments.get("planned_parent_directories", [])
                )
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
                    error_class="policy_error",
                    reason=_approval_denial_reason(is_interactive_prompt),
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

        execution_start = monotonic()
        deadline = _ToolDeadline(execution_start + route_timeout_seconds)
        cache_stage = FileMetadataUpdateStage(self._file_metadata_cache)
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
            shell_process_registry=context.get("shell_process_registry"),
            todo_plan_store=context.get("todo_plan_store"),
            vision_client=context.get("vision_client"),
            view_image_reader=context.get("view_image_reader"),
            provider_cancellation_registry=context.get("provider_cancellation_registry"),
            effective_timeout_seconds=route_timeout_seconds,
            cancellation_timeout_seconds=_effective_cancellation_timeout(
                frozen_config=context.get("frozen_config", {})
            ),
            file_metadata_cache_stage=cache_stage,
            file_metadata_cache=self._file_metadata_cache,
            write_lock_registry=self._write_locks,
            tool_deadline=deadline,
        )

        executor: ThreadPoolExecutor | None = None
        try:
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                self._execute_tool_call_inside_timeout,
                session_id,
                run_id,
                tool_name,
                tool_context,
                normalized_arguments,
                deadline,
            )
            prepared = future.result(timeout=route_timeout_seconds)
        except (TimeoutError, _ToolDeadlineExceeded):
            cache_stage.abandon()
            future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            metadata = {}
            if definition.name == "shell_exec":
                metadata["effective_timeout_seconds"] = route_timeout_seconds
            if definition.name == "write_file":
                metadata.update(tool_context.write_side_effect_metadata())
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
        except ProviderBoundaryNotClosed:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            raise
        except KeyboardInterrupt:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception as exc:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            metadata = {}
            if definition.name == "write_file":
                metadata.update(tool_context.write_side_effect_metadata())
            result = tool_error_result(str(exc), source=tool_name, metadata=metadata)
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
        try:
            result = self._tool_result_from_prepared(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                prepared=prepared,
            )
        except Exception as exc:
            metadata = {}
            if definition.name == "write_file":
                metadata.update(tool_context.write_side_effect_metadata())
            result = tool_error_result(str(exc), source=tool_name, metadata=metadata)
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
        if result.status == "ok":
            self._file_metadata_cache.commit(cache_stage.take_updates())
        else:
            cache_stage.abandon()
            if definition.name == "write_file":
                result = _with_metadata(
                    result,
                    tool_context.write_side_effect_metadata(),
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

    def _execute_tool_call_inside_timeout(
        self,
        session_id: str,
        run_id: str,
        tool_name: str,
        tool_context: ToolUseContext,
        normalized_arguments: dict[str, Any],
        deadline: _ToolDeadline,
    ) -> PreparedToolResult:
        deadline.check()
        handler_output = self._router.route(tool_context, normalized_arguments)
        deadline.check()
        return self._prepare_handler_result(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            output=handler_output,
            deadline_check=deadline.check,
        )

    def _prepare_handler_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: str | dict[str, Any] | NativeHandlerResult,
        deadline_check: Any = None,
    ) -> PreparedToolResult:
        if isinstance(output, NativeHandlerResult):
            if output.status == "error":
                return PreparedToolResult(
                    status="error",
                    error_message=output.error_message or "Tool failed.",
                    reason=output.reason or "tool_execution_failed",
                    metadata=output.metadata or {},
                )
            return self._prepare_native_ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output.output or {},
                metadata=output.metadata or {},
                deadline_check=deadline_check,
            )
        if isinstance(output, ViewImageResult):
            if output.status == "ok":
                artifacts: tuple[Any, ...] = ()
                if (
                    output.raw_provider_text is not None
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
                        deadline_check=deadline_check,
                    )
                    artifacts = (artifact,)
                return PreparedToolResult(
                    status="view_image",
                    output=output,
                    artifacts=artifacts,
                )
            return PreparedToolResult(
                status="view_image",
                output=output,
            )
        if isinstance(output, ShellHandlerResult):
            if output.status == "cancelled":
                return PreparedToolResult(
                    status="cancelled",
                    error_message=output.error_message or "Tool call cancelled.",
                    metadata={"tool_name": tool_name, **(output.metadata or {})},
                )
            if output.status == "timeout":
                return PreparedToolResult(
                    status="timeout",
                    metadata=output.metadata or {},
                )
            if output.status == "error":
                return PreparedToolResult(
                    status="error",
                    error_message=output.error_message or "Tool failed.",
                    reason=output.reason or "tool_execution_failed",
                    metadata=output.metadata or {},
                )
            return self._prepare_native_ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output.output or {"stdout": "", "stderr": "", "returncode": 0},
                metadata=output.metadata or {},
                deadline_check=deadline_check,
            )
        if isinstance(output, RuntimeControlHandlerResult):
            if output.status == "denied":
                return PreparedToolResult(
                    status="denied",
                    error_message=output.error_message
                    or "Runtime-control target denied.",
                    error_class=output.error_class,
                    reason="tool_schema_invalid"
                    if output.error_class == "tool_error"
                    else None,
                )
            if output.status == "error":
                return PreparedToolResult(
                    status="error",
                    error_message=output.error_message or "Tool failed.",
                    metadata=output.metadata or {},
                )
            return PreparedToolResult(
                status="ok",
                output=output.output or {},
                metadata=output.metadata or {},
                redacted_output=(output.metadata or {}).get("redacted_output")
                if tool_name == "todo" and output.metadata is not None
                else None,
            )
        return self._prepare_ok_result(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            output=output,
            metadata={},
            deadline_check=deadline_check,
        )

    def _tool_result_from_prepared(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        prepared: PreparedToolResult,
    ) -> ToolResult:
        metadata = prepared.metadata or {}
        if prepared.status == "error":
            return tool_error_result(
                prepared.error_message or "Tool failed.",
                source=tool_name,
                metadata=metadata,
                reason=prepared.reason or "tool_execution_failed",
            )
        if prepared.status == "timeout":
            return _timeout_result(
                float(metadata.get("effective_timeout_seconds", 0)),
                metadata=metadata,
            )
        if prepared.status == "cancelled":
            return _cancelled_tool_result(
                prepared.error_message or "Tool call cancelled.",
                metadata=metadata,
            )
        if prepared.status == "denied":
            return _denied_result(
                prepared.error_message or "Runtime-control target denied.",
                error_class=prepared.error_class,
                reason=prepared.reason,
            )
        if prepared.status == "view_image":
            view_output = prepared.output
            result = view_image_tools.tool_result_from_view_image(
                view_output, source=tool_name
            )
            if result.status == "ok" and prepared.artifacts:
                for artifact in prepared.artifacts:
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
                    artifacts=[artifact.artifact_id for artifact in prepared.artifacts],
                    metadata=result.metadata,
                    redacted_output=result.redacted_output,
                )
            return result
        if prepared.status == "ok":
            if prepared.redacted_output is not None and tool_name == "todo":
                return ToolResult(
                    status="ok",
                    output=prepared.output,
                    error=None,
                    artifacts=[artifact.artifact_id for artifact in prepared.artifacts],
                    metadata={
                        key: value
                        for key, value in metadata.items()
                        if key != "redacted_output"
                    },
                    redacted_output=prepared.redacted_output,
                )
            for artifact in prepared.artifacts:
                self._write_artifact_registered_event(
                    session_id=session_id,
                    run_id=run_id,
                    artifact=artifact,
                )
            return ToolResult(
                status="ok",
                output=prepared.output,
                error=None,
                artifacts=[artifact.artifact_id for artifact in prepared.artifacts],
                metadata=metadata,
                redacted_output=prepared.redacted_output,
            )
        return tool_error_result("Tool failed.", source=tool_name)

    def _handler_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: str | dict[str, Any] | NativeHandlerResult,
    ) -> ToolResult:
        return self._tool_result_from_prepared(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            prepared=self._prepare_handler_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output,
            ),
        )

    def _prepare_ok_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: str | dict[str, Any],
        metadata: dict[str, Any],
        deadline_check: Any = None,
    ) -> PreparedToolResult:
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
                deadline_check=deadline_check,
            )
            return PreparedToolResult(
                status="ok",
                output=None,
                artifacts=(artifact,),
                metadata={"bytes": output_size, **metadata},
                redacted_output=f"[output stored as artifact: {artifact.artifact_id}]",
            )
        return PreparedToolResult(
            status="ok",
            output=output,
            metadata=metadata,
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

    def _native_ok_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ToolResult:
        return self._tool_result_from_prepared(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            prepared=self._prepare_native_ok_result(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                output=output,
                metadata=metadata,
            ),
        )

    def _prepare_native_ok_result(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: dict[str, Any],
        metadata: dict[str, Any],
        deadline_check: Any = None,
    ) -> PreparedToolResult:
        artifacted_output, artifacts = self._field_artifacted_native_output(
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            output=output,
            deadline_check=deadline_check,
        )
        if artifacted_output is None:
            return PreparedToolResult(
                status="error",
                error_message=(
                    "Native tool output exceeded the durable inline threshold after "
                    "field-level artifacting; narrow the request or reduce pagination."
                ),
                reason="tool_execution_failed",
                metadata=metadata,
            )
        return PreparedToolResult(
            status="ok",
            output=artifacted_output,
            artifacts=tuple(artifacts),
            metadata=metadata,
        )

    def _field_artifacted_native_output(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        output: dict[str, Any],
        deadline_check: Any = None,
    ) -> tuple[dict[str, Any] | None, list[Any]]:
        normalized = deepcopy(output)
        if _observation_size(normalized) <= LARGE_OUTPUT_THRESHOLD_BYTES:
            return normalized, []
        artifacts: list[Any] = []
        for field_tool, field_name in _FIELD_ARTIFACT_ORDER:
            if field_tool != tool_name or field_name not in normalized:
                continue
            artifact = self._write_field_artifact(
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                field_name=field_name,
                value=normalized[field_name],
                deadline_check=deadline_check,
            )
            normalized[field_name] = _artifact_reference(artifact)
            artifacts.append(artifact)
            if _observation_size(normalized) <= LARGE_OUTPUT_THRESHOLD_BYTES:
                return normalized, artifacts
        if _observation_size(normalized) > LARGE_OUTPUT_THRESHOLD_BYTES:
            return None, []
        return normalized, artifacts

    def _write_field_artifact(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        field_name: str,
        value: Any,
        deadline_check: Any = None,
    ):
        content = _artifact_text(value)
        content_bytes = content.encode("utf-8")
        artifact = self.artifact_store.write_text(
            session_id=session_id,
            run_id=run_id,
            artifact_id=f"art_{uuid4().hex}",
            filename=f"{tool_name}_{field_name}.txt",
            content=content,
            metadata={
                "tool_name": tool_name,
                "field": field_name,
                "bytes": len(content_bytes),
            },
            deadline_check=deadline_check,
        )
        return artifact

    def _write_artifact_registered_event(
        self,
        *,
        session_id: str,
        run_id: str,
        artifact: Any,
    ) -> None:
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
    return _validate_object_schema(
        definition.name,
        arguments,
        _schema_for_validation(definition),
        path="",
    )


def _validate_object_schema(
    tool_name: str,
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
) -> str | None:
    if schema.get("type") == "object" and not isinstance(value, dict):
        return f"{path or 'arguments'} must be an object."
    properties = schema.get("properties", {})
    allowed = set(properties)
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in allowed:
                return f"Unknown field: {_join_schema_path(path, key)}"
    for key in schema.get("required", []):
        if key not in value:
            return f"Missing required field: {_join_schema_path(path, key)}"
    for key, field_schema in properties.items():
        child_path = _join_schema_path(path, key)
        if key not in value:
            if "default" in field_schema:
                value[key] = _copy_schema_default(field_schema["default"])
            continue
        error = _validate_schema_value(tool_name, value[key], field_schema, path=child_path)
        if error is not None:
            return error
    return None


def _validate_schema_value(
    tool_name: str,
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
) -> str | None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object."
        return _validate_object_schema(tool_name, value, schema, path=path)
    if expected_type == "string":
        if not isinstance(value, str):
            return f"{path} must be a string."
        if _is_trimmed_path_field(tool_name, path):
            trimmed = value.strip()
            if not trimmed:
                return f"{path} must be a non-empty string."
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            return f"{path} must be one of: {', '.join(str(item) for item in enum)}."
        return None
    if expected_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path} must be an integer."
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            if minimum == 1:
                return f"{path} must be a positive integer."
            return f"{path} must be greater than or equal to {minimum}."
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            return f"{path} must be less than or equal to {maximum}."
        return None
    if expected_type == "boolean":
        if not isinstance(value, bool):
            return f"{path} must be a boolean."
        return None
    if expected_type == "array":
        if not isinstance(value, list):
            return f"{path} must be an array."
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            return f"{path} must contain at least {min_items} item."
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            return f"{path} must contain at most {max_items} items."
        item_schema = schema.get("items", {})
        for index, item in enumerate(value, start=1):
            item_path = f"{path}[{index}]"
            error = _validate_schema_value(tool_name, item, item_schema, path=item_path)
            if error is not None:
                return error
        return None
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        return f"{path} must be one of: {', '.join(str(item) for item in enum)}."
    return None


def _copy_schema_default(default: Any) -> Any:
    if isinstance(default, (dict, list)):
        return json.loads(json.dumps(default))
    return default


def _file_metadata_update(
    path: str | Path, *, source_tool: str
) -> tuple[str, FileMetadataCacheEntry]:
    if source_tool not in _CACHE_ADVANCING_SOURCE_TOOLS:
        raise ValueError(f"{source_tool} is not allowed to update file metadata cache.")
    canonical = Path(path).resolve()
    stat_result = canonical.stat()
    digest = sha256()
    with canonical.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return (
        str(canonical),
        FileMetadataCacheEntry(
            sha256=digest.hexdigest(),
            size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            observed_at=utc_now_iso(),
            source_tool=source_tool,
        ),
    )


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _join_schema_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _schema_for_validation(definition: ToolDefinition) -> dict[str, Any]:
    if definition.name == "read_file":
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": READ_FILE_MAX_LIMIT,
                    "default": READ_FILE_DEFAULT_LIMIT,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }
    if definition.name == "list_dir":
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": LIST_DIR_MAX_IGNORE_PATTERNS,
                    "default": [],
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": LIST_DIR_MAX_LIMIT,
                    "default": LIST_DIR_DEFAULT_LIMIT,
                },
                "include_hidden": {"type": "boolean", "default": False},
            },
            "required": ["path"],
            "additionalProperties": False,
        }
    if definition.name == "find_file":
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "pattern": {"type": "string"},
                "case_sensitive": {"type": "boolean", "default": False},
                "include_hidden": {"type": "boolean", "default": False},
                "maxResults": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": FIND_FILE_MAX_RESULTS,
                    "default": FIND_FILE_DEFAULT_MAX_RESULTS,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }
    if definition.name == "search_text":
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "pattern": {"type": "string"},
                "glob": {"type": "string", "default": "**"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "maxResults": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "default": 100,
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "default": "content",
                },
                "fixed_strings": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": True},
                "type": {
                    "type": "string",
                    "enum": list(SEARCH_TEXT_TYPES),
                },
                "include_hidden": {"type": "boolean", "default": False},
                "before_context": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10,
                    "default": 0,
                },
                "after_context": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10,
                    "default": 0,
                },
                "context": {"type": "integer", "minimum": 0, "maximum": 10},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }
    if definition.name == "edit_file":
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        }
    return definition.input_schema


def _is_trimmed_path_field(tool_name: str, path: str) -> bool:
    if tool_name == "view_image" and path.startswith("paths["):
        return True
    if path == "cwd" and tool_name == "shell_exec":
        return True
    return path == "path" and tool_name in {
        "read_file",
        "list_dir",
        "find_file",
        "search_text",
        "write_file",
        "edit_file",
    }


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


def _validate_shell_timeout_semantics(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    frozen_config: dict[str, Any],
) -> str | None:
    if definition.name != "shell_exec" or "timeout_seconds" not in arguments:
        return None
    requested = arguments["timeout_seconds"]
    max_timeout = _shell_timeout_limits(frozen_config)[1]
    if requested > max_timeout:
        return (
            "timeout_seconds must be less than or equal to the configured "
            f"maximum of {max_timeout}."
        )
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


def _validate_search_text_semantics(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    raw_argument_keys: frozenset[str],
) -> str | None:
    if definition.name != "search_text":
        return None
    pattern = arguments.get("pattern")
    if isinstance(pattern, str):
        if not pattern.strip():
            return "pattern must be non-empty."
        if "\r" in pattern or "\n" in pattern:
            return "pattern must not contain CR or LF characters."
    if "context" in raw_argument_keys and (
        "before_context" in raw_argument_keys or "after_context" in raw_argument_keys
    ):
        return "context is mutually exclusive with before_context and after_context."
    glob = arguments.get("glob")
    if isinstance(glob, str):
        glob_error = validate_portable_glob_pattern(glob)
        if glob_error is not None:
            return glob_error
    type_name = arguments.get("type")
    if isinstance(type_name, str) and not is_search_text_type_allowed(type_name):
        return f"Unsupported search_text type: {type_name}."
    context = arguments.get("context")
    if context is not None:
        arguments["before_context_effective"] = context
        arguments["after_context_effective"] = context
    else:
        arguments["before_context_effective"] = arguments.get("before_context", 0)
        arguments["after_context_effective"] = arguments.get("after_context", 0)
    return None


def _validate_phase_3_5_native_semantics(
    *,
    definition: ToolDefinition,
    arguments: dict[str, Any],
) -> str | None:
    if definition.name == "find_file":
        pattern = arguments.get("pattern")
        if isinstance(pattern, str):
            if not pattern.strip():
                return "pattern must be non-empty."
            return validate_portable_glob_pattern(pattern)
    if definition.name == "list_dir":
        ignore = arguments.get("ignore")
        if isinstance(ignore, list):
            return validate_list_dir_ignore_patterns(ignore)
    if definition.name == "edit_file":
        old_text = arguments.get("old_text")
        if isinstance(old_text, str) and old_text == "":
            return "old_text must be non-empty."
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
    normalized_arguments = dict(arguments)
    if "path" in normalized_arguments:
        normalized_arguments["path"] = normalized_arguments["path"].strip()
    canonical_path = canonicalize_path(normalized_arguments["path"], workspace_root)
    normalized_arguments["path"] = str(canonical_path)
    planned_parent_directories: list[Path] = []
    if definition.name == "write_file":
        planned_parent_directories = _planned_parent_directories(canonical_path)
        normalized_arguments["planned_parent_directories"] = [
            str(path) for path in planned_parent_directories
        ]
    if definition.name == "search_text":
        scope_signature = _phase_3_5_scope_signature(
            definition.name,
            risk_level=definition.risk_level,
            canonical_path=canonical_path,
            arguments=normalized_arguments,
        )
    elif definition.name == "find_file":
        scope_signature = _phase_3_5_scope_signature(
            definition.name,
            risk_level=definition.risk_level,
            canonical_path=canonical_path,
            arguments=normalized_arguments,
        )
    elif definition.name == "list_dir":
        scope_signature = _phase_3_5_scope_signature(
            definition.name,
            risk_level=definition.risk_level,
            canonical_path=canonical_path,
            arguments=normalized_arguments,
        )
    elif definition.name == "edit_file":
        scope_signature = _phase_3_5_scope_signature(
            definition.name,
            risk_level=definition.risk_level,
            canonical_path=canonical_path,
            arguments=normalized_arguments,
        )
    elif definition.name == "write_file":
        scope_signature = _phase_3_5_scope_signature(
            definition.name,
            risk_level=definition.risk_level,
            canonical_path=canonical_path,
            arguments=normalized_arguments,
        )
    else:
        scope_signature = scope_signature_for_tool(
            definition.name,
            risk_level=definition.risk_level,
            paths=[canonical_path],
        )
    return NormalizedBrokerArguments(
        arguments=normalized_arguments,
        paths=(canonical_path, *planned_parent_directories),
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
    trimmed_paths = [path.strip() for path in arguments["paths"]]
    canonical_paths = [canonicalize_path(path, workspace_root) for path in trimmed_paths]
    normalized_arguments = dict(arguments)
    normalized_arguments["paths"] = [str(path) for path in canonical_paths]
    symlink_escapes: list[str] = []
    for raw_path, canonical_path in zip(trimmed_paths, canonical_paths, strict=True):
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
    if isinstance(cwd_argument, str):
        normalized_arguments["cwd"] = cwd_argument.strip()
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
        return f"{arguments['pattern']} in {canonical_path}"
    if tool_name == "find_file":
        return f"{arguments['pattern']} under {canonical_path}"
    return str(canonical_path)


def _phase_3_5_scope_signature(
    tool_name: str,
    *,
    risk_level: str,
    canonical_path: Path,
    arguments: dict[str, Any],
) -> str:
    access = "read" if risk_level == "read" else "write"
    path = str(canonical_path.resolve())
    if tool_name == "list_dir":
        ignore = ",".join(arguments.get("ignore") or [])
        return (
            f"list_dir|read|read:{path}|ignore:{ignore}|"
            f"include_hidden:{bool(arguments.get('include_hidden'))}"
        )
    if tool_name == "search_text":
        return (
            f"search_text|read|read:{path}|pattern:{arguments.get('pattern', '')}|"
            f"glob:{arguments.get('glob', '')}|"
            f"case_sensitive:{bool(arguments.get('case_sensitive'))}|"
            f"fixed_strings:{bool(arguments.get('fixed_strings'))}|"
            f"type:{arguments.get('type', '')}|"
            f"output_mode:{arguments.get('output_mode', '')}|"
            f"before_context_effective:{arguments.get('before_context_effective', 0)}|"
            f"after_context_effective:{arguments.get('after_context_effective', 0)}|"
            f"include_hidden:{bool(arguments.get('include_hidden'))}"
        )
    if tool_name == "find_file":
        return (
            f"find_file|read|read:{path}|pattern:{arguments.get('pattern', '')}|"
            f"case_sensitive:{bool(arguments.get('case_sensitive'))}|"
            f"include_hidden:{bool(arguments.get('include_hidden'))}"
        )
    if tool_name == "edit_file":
        return (
            f"edit_file|write|write:{path}|"
            f"replace_all:{bool(arguments.get('replace_all'))}"
        )
    if tool_name == "write_file":
        planned = arguments.get("planned_parent_directories") or []
        planned_part = ",".join(str(path) for path in planned)
        return f"write_file|write|write:{path}|planned_parents:{planned_part}"
    return f"{tool_name}|{risk_level}|{access}:{path}"


def _planned_parent_directories(canonical_path: Path) -> list[Path]:
    planned: list[Path] = []
    cursor = canonical_path.parent
    while not cursor.exists():
        planned.append(cursor.resolve())
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return list(reversed(planned))


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
    default, _max_timeout = _shell_timeout_limits(frozen_config)
    return requested if requested is not None else default


def _shell_timeout_limits(frozen_config: dict[str, Any]) -> tuple[int, int]:
    execution = frozen_config.get("execution") if isinstance(frozen_config, dict) else None
    default_value = (
        execution.get("default_shell_timeout_seconds")
        if isinstance(execution, dict)
        else None
    )
    max_value = (
        execution.get("max_shell_timeout_seconds")
        if isinstance(execution, dict)
        else None
    )
    default = (
        default_value
        if isinstance(default_value, int)
        and not isinstance(default_value, bool)
        and default_value > 0
        else 300
    )
    max_timeout = (
        max_value
        if isinstance(max_value, int)
        and not isinstance(max_value, bool)
        and max_value >= default
        else 3600
    )
    return default, max_timeout


def _generic_tool_timeout_seconds(context: dict[str, Any], fallback: float) -> float:
    explicit = context.get("timeout_seconds")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and explicit > 0:
        return float(explicit)
    frozen_config = context.get("frozen_config")
    execution = frozen_config.get("execution") if isinstance(frozen_config, dict) else None
    timeout = (
        execution.get("default_tool_timeout_seconds")
        if isinstance(execution, dict)
        else None
    )
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout)
    return float(fallback)


def _effective_view_image_timeout(
    *, frozen_config: dict[str, Any], fallback: float
) -> float:
    multimodal = frozen_config.get("multimodal") if isinstance(frozen_config, dict) else None
    timeout = multimodal.get("timeout_seconds") if isinstance(multimodal, dict) else None
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout)
    return fallback


def _effective_cancellation_timeout(*, frozen_config: dict[str, Any]) -> float:
    execution = frozen_config.get("execution") if isinstance(frozen_config, dict) else None
    timeout = execution.get("cancellation_timeout_seconds") if isinstance(execution, dict) else None
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        return float(timeout)
    return 10


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
    planned_parent_directories: list[str] | tuple[str, ...] = (),
) -> str:
    lines = [
        "=== Approval Request ===",
        f"Tool: {tool_name}",
        f"Target: {target}",
    ]
    if planned_parent_directories:
        lines.append("Planned parent directories:")
        lines.extend(f"- {path}" for path in planned_parent_directories)
    return "\n".join([*lines, "", "Allow? [y]once, [a] session, [n] deny"])


def _observation_size(output: dict[str, Any]) -> int:
    return len(_artifact_text(output).encode("utf-8"))


def _artifact_reference(artifact: Any) -> dict[str, Any]:
    metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
    payload_sha256 = metadata.get("payload_sha256")
    return {
        "artifact_id": artifact.artifact_id,
        "relative_path": artifact.relative_path,
        "preview": metadata.get("preview"),
        "truncated": True,
        "bytes": int(metadata.get("bytes", 0)),
        "sha256": payload_sha256 if isinstance(payload_sha256, str) else "",
    }


def _artifact_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, sort_keys=True)


def _error_result(
    message: str,
    *,
    error_class: str,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    normalized = _normalized_error(
        error_class=error_class,
        reason=reason,
        message=message,
        scope="tool",
        metadata=metadata or {},
    )
    return ToolResult(
        status="error",
        output=None,
        error=normalized.to_dict(),
        artifacts=[],
        metadata=metadata or {},
        redacted_output=None,
    )


def _denied_result(
    message: str,
    *,
    error_class: str,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    normalized = _normalized_error(
        error_class=error_class,
        reason=reason,
        message=message,
        scope="tool",
        metadata=metadata or {},
    )
    return ToolResult(
        status="denied",
        output=None,
        error=normalized.to_dict(),
        artifacts=[],
        metadata=metadata or {},
        redacted_output=None,
    )


def _timeout_result(timeout_seconds: float, *, metadata: dict[str, Any] | None = None) -> ToolResult:
    normalized = NormalizedError.create(
        "tool_error",
        "tool_execution_timeout",
        message=f"Tool timed out after {timeout_seconds:g} seconds.",
        scope="tool",
        metadata=metadata or {},
    )
    return ToolResult(
        status="timeout",
        output=None,
        error=normalized.to_dict(),
        artifacts=[],
        metadata=metadata or {},
        redacted_output=None,
    )


def _with_metadata(result: ToolResult, metadata: dict[str, Any]) -> ToolResult:
    if not metadata:
        return result
    merged_metadata = {**(result.metadata or {}), **metadata}
    error = result.error
    if isinstance(error, dict):
        error = dict(error)
        error_metadata = error.get("metadata")
        if isinstance(error_metadata, dict):
            error["metadata"] = {**error_metadata, **metadata}
    return ToolResult(
        status=result.status,
        output=result.output,
        error=error,
        artifacts=result.artifacts,
        metadata=merged_metadata,
        redacted_output=result.redacted_output,
    )


def _cancelled_tool_result(
    message: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    normalized = NormalizedError.create(
        "cancelled",
        "tool_call_cancelled",
        message=message,
        scope="tool",
        metadata=metadata or {},
    )
    return ToolResult(
        status="cancelled",
        output=None,
        error=normalized.to_dict(),
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
        payload["error"] = result.error
        payload["error_class"] = result.error["error_class"]
        payload["message"] = result.error["message"]
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    for key in ("side_effects", "file_write_completed", "cache_updated"):
        if key in metadata:
            payload[key] = metadata[key]
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


def _normalized_error(
    *,
    error_class: str,
    reason: str | None,
    message: str,
    scope: str,
    metadata: dict[str, Any],
) -> NormalizedError:
    normalized_class = error_class
    normalized_reason = reason
    if normalized_class == "tool_error":
        normalized_reason = normalized_reason or "tool_execution_failed"
    elif normalized_class == "config_error":
        normalized_reason = normalized_reason or "invalid_runtime_config"
    elif normalized_class == "policy_error":
        normalized_reason = normalized_reason or "approval_denied"
    elif normalized_class == "user_error":
        normalized_reason = normalized_reason or "invalid_runtime_control_target"
    elif normalized_class == "internal_error":
        normalized_class = "runtime_error"
        normalized_reason = "internal_invariant_failed"
    elif normalized_class == "policy_denied":
        normalized_class = "policy_error"
        normalized_reason = "approval_denied"
    else:
        normalized_reason = normalized_reason or "tool_execution_failed"
        normalized_class = "tool_error"
    return NormalizedError.create(
        normalized_class,
        normalized_reason,
        message=message,
        scope=scope,
        metadata=metadata,
    )


def _policy_denial_reason(decision: PermissionDecision) -> str:
    if decision.reason == "path_denied":
        return "path_policy_denied"
    if decision.reason in {
        "builtin_shell_denied",
        "user_shell_denied",
        "shell_allowlist_miss",
    }:
        return "shell_policy_denied"
    if decision.reason == "approval_required_non_interactive":
        return "approval_required_non_interactive"
    return "approval_denied"


def _approval_denial_reason(is_interactive_prompt: bool) -> str:
    if is_interactive_prompt:
        return "approval_denied"
    return "approval_required_non_interactive"


def _audit_arguments(*, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "write_file":
        redacted = {key: value for key, value in arguments.items() if key != "content"}
        content = arguments.get("content", "")
        if isinstance(content, str):
            encoded = content.encode("utf-8")
            redacted["content_sha256"] = sha256(encoded).hexdigest()
            redacted["content_bytes"] = len(encoded)
        return redacted
    if tool_name == "edit_file":
        redacted = {
            key: value
            for key, value in arguments.items()
            if key not in {"old_text", "new_text"}
        }
        for field in ("old_text", "new_text"):
            text = arguments.get(field, "")
            if isinstance(text, str):
                encoded = text.encode("utf-8")
                redacted[f"{field}_sha256"] = sha256(encoded).hexdigest()
                redacted[f"{field}_bytes"] = len(encoded)
        return redacted
    if tool_name == "search_text":
        return {
            key: value
            for key, value in arguments.items()
            if key
            in {
                "path",
                "pattern",
                "glob",
                "offset",
                "maxResults",
                "output_mode",
                "fixed_strings",
                "case_sensitive",
                "type",
                "include_hidden",
                "before_context_effective",
                "after_context_effective",
            }
        }
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

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from debug_agent.persistence.skills import (
    FrozenResourceSnapshot,
    FrozenSkillSnapshot,
)
from debug_agent.runtime.contracts import ToolDefinition


TODO_STATUSES = ("pending", "in_progress", "completed")


@dataclass(frozen=True)
class RuntimeControlTarget:
    valid: bool
    error_message: str | None = None
    error_class: str = "config_error"
    skill: FrozenSkillSnapshot | None = None
    resource: FrozenResourceSnapshot | None = None
    normalized_path: str | None = None
    already_active: bool = False


@dataclass(frozen=True)
class RuntimeControlHandlerResult:
    status: str
    output: str | dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    error_message: str | None = None
    error_class: str = "tool_error"


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="activate_skill",
            description="Activate a frozen prompt skill for this run.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            category="runtime_control",
            risk_level="runtime_control",
            access=["runtime_control"],
        ),
        ToolDefinition(
            name="load_skill_resource",
            description=(
                "Load one frozen resource file for an active skill. Use this when active skill\n"
                "instructions or available_resources reference a file whose contents are needed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["skill_name", "path"],
                "additionalProperties": False,
            },
            category="runtime_control",
            risk_level="read",
            access=["read"],
        ),
        ToolDefinition(
            name="todo",
            description="Replace the current run Todo Plan.",
            input_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": list(TODO_STATUSES),
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "Optional present-continuous label.",
                                },
                            },
                            "required": ["content", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            category="runtime_control",
            risk_level="runtime_control",
            access=[],
        ),
    ]


def tool_handlers() -> dict[str, Any]:
    return {
        "activate_skill": activate_skill,
        "load_skill_resource": load_skill_resource,
        "todo": todo,
    }


def validate_target(context: Any, tool_name: str, arguments: dict[str, Any]) -> RuntimeControlTarget:
    if tool_name == "activate_skill":
        return _validate_activation(context, arguments)
    if tool_name == "load_skill_resource":
        return _validate_resource(context, arguments)
    if tool_name == "todo":
        return _validate_todo(arguments)
    return RuntimeControlTarget(False, f"Unknown runtime-control tool: {tool_name}")


def activate_skill(context: Any, arguments: dict[str, Any]) -> RuntimeControlHandlerResult:
    target = validate_target(context, "activate_skill", arguments)
    if not target.valid or target.skill is None:
        return RuntimeControlHandlerResult(
            "denied",
            error_message=target.error_message or "Invalid runtime-control target.",
            error_class=target.error_class,
        )
    run_store = getattr(context, "run_store", None)
    if run_store is None:
        return RuntimeControlHandlerResult(
            "error",
            error_message="Run store is required for skill activation.",
            error_class="internal_error",
        )
    if not target.already_active:
        run_store.activate_skill(
            context.run_id,
            name=target.skill.skill_name,
            content_hash=target.skill.overall_content_hash,
            activation_reason="model_requested",
            scope="run",
        )
    message = (
        "Skill already active"
        if target.already_active
        else "Skill activated"
    )
    return RuntimeControlHandlerResult(
        "ok",
        output=f"{message}: {target.skill.skill_name} ({target.skill.overall_content_hash})",
        metadata={
            "skill_name": target.skill.skill_name,
            "content_hash": target.skill.overall_content_hash,
            "activation_reason": "model_requested",
            "scope": "run",
            "already_active": target.already_active,
        },
    )


def load_skill_resource(context: Any, arguments: dict[str, Any]) -> RuntimeControlHandlerResult:
    target = validate_target(context, "load_skill_resource", arguments)
    if not target.valid or target.skill is None or target.resource is None:
        return RuntimeControlHandlerResult(
            "denied",
            error_message=target.error_message or "Invalid runtime-control target.",
            error_class=target.error_class,
        )
    resource = target.resource
    output = {
        "skill_name": resource.skill_name,
        "resource_path": resource.resource_path,
        "resource_kind": resource.resource_kind,
        "content_hash": resource.content_hash,
        "size_bytes": resource.size_bytes,
        "media_kind": resource.media_kind,
        "content": resource.inline_text_payload,
        "artifact_id": resource.payload_artifact_id,
        "resource_marker": None,
    }
    if resource.inline_text_payload is None:
        output["resource_marker"] = (
            f"[skill resource stored as artifact: {resource.payload_artifact_id}]"
        )
    return RuntimeControlHandlerResult(
        "ok",
        output=output,
        metadata={
            "skill_name": resource.skill_name,
            "skill_content_hash": target.skill.overall_content_hash,
            "resource_path": resource.resource_path,
            "resource_kind": resource.resource_kind,
            "resource_content_hash": resource.content_hash,
            "media_kind": resource.media_kind,
            "size_bytes": resource.size_bytes,
            "artifact_id": resource.payload_artifact_id,
        },
    )


def todo(context: Any, arguments: dict[str, Any]) -> RuntimeControlHandlerResult:
    store = getattr(context, "todo_plan_store", None)
    if store is None:
        return RuntimeControlHandlerResult(
            "error",
            error_message="Todo Plan store is required.",
            error_class="internal_error",
        )
    normalized_items: list[dict[str, Any]] = []
    in_progress_count = 0
    for item in arguments["items"]:
        content = item["content"].strip()
        if not content:
            return RuntimeControlHandlerResult(
                "denied",
                error_message="Todo item content must not be empty.",
                error_class="user_error",
            )
        if len(content) > 240:
            return RuntimeControlHandlerResult(
                "denied",
                error_message="Todo item content must be at most 240 characters.",
                error_class="user_error",
            )
        normalized = {"content": content, "status": item["status"]}
        active_form = item.get("activeForm")
        if active_form is not None:
            active_form = active_form.strip()
            if not active_form:
                return RuntimeControlHandlerResult(
                    "denied",
                    error_message="Todo item activeForm must not be empty when provided.",
                    error_class="user_error",
                )
            if len(active_form) > 120:
                return RuntimeControlHandlerResult(
                    "denied",
                    error_message="Todo item activeForm must be at most 120 characters.",
                    error_class="user_error",
                )
            if item["status"] == "in_progress":
                normalized["activeForm"] = active_form
        if item["status"] == "in_progress":
            in_progress_count += 1
        normalized_items.append(normalized)
    if in_progress_count > 1:
        return RuntimeControlHandlerResult(
            "denied",
            error_message="Todo Plan may contain at most one in_progress item.",
            error_class="user_error",
        )
    replacement = store.replace_plan(
        context.session_id,
        context.run_id,
        normalized_items,
        context.event_writer,
    )
    payload = replacement.event.payload
    output_items = [
        {
            key: value
            for key, value in item.items()
            if key in {"index", "content", "status", "activeForm"}
        }
        for item in payload["items"]
    ]
    output = {
        "plan_version": payload["plan_version"],
        "item_count": payload["item_count"],
        "counts": payload["counts"],
        "items": output_items,
    }
    return RuntimeControlHandlerResult(
        "ok",
        output=output,
        metadata={
            "tool_name": "todo",
            "previous_plan_version": replacement.previous_plan_version,
            "plan_version": payload["plan_version"],
            "mutation": "replace",
            "item_count": payload["item_count"],
            "counts": payload["counts"],
            "redacted_output": _render_todo_plan(payload["plan_version"], output_items, payload["counts"]),
        },
    )


def _validate_activation(context: Any, arguments: dict[str, Any]) -> RuntimeControlTarget:
    store = getattr(context, "skill_snapshot_store", None)
    if store is None:
        return RuntimeControlTarget(False, "Skill snapshot store is not available.")
    skill = store.get_skill(
        session_id=context.session_id,
        run_id=context.run_id,
        skill_name=arguments["name"],
    )
    if skill is None:
        return RuntimeControlTarget(False, f"Unknown skill: {arguments['name']}")
    target = _validated_skill(store, skill)
    if not target.valid:
        return target
    return RuntimeControlTarget(
        True,
        skill=target.skill,
        already_active=_skill_is_active(context, skill),
    )


def _validate_todo(arguments: dict[str, Any]) -> RuntimeControlTarget:
    in_progress_count = 0
    for item in arguments["items"]:
        content = item["content"].strip()
        if not content:
            return RuntimeControlTarget(
                False,
                "Todo item content must not be empty.",
                error_class="tool_error",
            )
        if len(content) > 240:
            return RuntimeControlTarget(
                False,
                "Todo item content must be at most 240 characters.",
                error_class="tool_error",
            )
        active_form = item.get("activeForm")
        if active_form is not None:
            active_form = active_form.strip()
            if not active_form:
                return RuntimeControlTarget(
                    False,
                    "Todo item activeForm must not be empty when provided.",
                    error_class="tool_error",
                )
            if len(active_form) > 120:
                return RuntimeControlTarget(
                    False,
                    "Todo item activeForm must be at most 120 characters.",
                    error_class="tool_error",
                )
        if item["status"] == "in_progress":
            in_progress_count += 1
    if in_progress_count > 1:
        return RuntimeControlTarget(
            False,
            "Todo Plan may contain at most one in_progress item.",
            error_class="tool_error",
        )
    return RuntimeControlTarget(True)


def _validate_resource(context: Any, arguments: dict[str, Any]) -> RuntimeControlTarget:
    store = getattr(context, "skill_snapshot_store", None)
    run_store = getattr(context, "run_store", None)
    if store is None or run_store is None:
        return RuntimeControlTarget(False, "Skill runtime state is not available.")
    normalized = _normalize_resource_path(arguments["path"])
    if normalized is None:
        return RuntimeControlTarget(False, "Invalid skill resource path.")
    skill = store.get_skill(
        session_id=context.session_id,
        run_id=context.run_id,
        skill_name=arguments["skill_name"],
    )
    if skill is None:
        return RuntimeControlTarget(False, f"Unknown skill: {arguments['skill_name']}")
    skill_target = _validated_skill(store, skill)
    if not skill_target.valid:
        return skill_target
    active = run_store.get(context.run_id).active_skills
    if not any(
        isinstance(record, dict)
        and record.get("name") == skill.skill_name
        and record.get("content_hash") == skill.overall_content_hash
        for record in active
    ):
        return RuntimeControlTarget(False, f"Skill is not active: {skill.skill_name}")
    resource = store.get_resource(
        skill_snapshot_id=skill.skill_snapshot_id,
        resource_path=normalized,
    )
    if resource is None:
        return RuntimeControlTarget(False, f"Unknown skill resource: {normalized}")
    if not _resource_hash_valid(context, resource):
        return RuntimeControlTarget(False, f"Corrupt frozen skill resource: {normalized}")
    return RuntimeControlTarget(
        True,
        skill=skill,
        resource=resource,
        normalized_path=normalized,
    )


def _validated_skill(store: Any, skill: FrozenSkillSnapshot) -> RuntimeControlTarget:
    if _sha256_text(skill.skill_md_content) != skill.skill_md_content_hash:
        return RuntimeControlTarget(False, f"Corrupt frozen skill snapshot: {skill.skill_name}")
    expected = _overall_hash(
        manifest=skill.manifest,
        skill_md_text=skill.skill_md_content,
        resources=store.list_resources(skill_snapshot_id=skill.skill_snapshot_id),
    )
    if expected != skill.overall_content_hash:
        return RuntimeControlTarget(False, f"Frozen skill hash mismatch: {skill.skill_name}")
    return RuntimeControlTarget(True, skill=skill)


def _skill_is_active(context: Any, skill: FrozenSkillSnapshot) -> bool:
    run_store = getattr(context, "run_store", None)
    if run_store is None:
        return False
    active = run_store.get(context.run_id).active_skills
    return any(
        isinstance(record, dict)
        and record.get("name") == skill.skill_name
        and record.get("content_hash") == skill.overall_content_hash
        for record in active
    )


def _resource_hash_valid(context: Any, resource: FrozenResourceSnapshot) -> bool:
    if resource.inline_text_payload is not None:
        return _sha256_text(resource.inline_text_payload) == resource.content_hash
    if resource.payload_artifact_id is None:
        return False
    try:
        payload = context.artifact_store.resolve_path(resource.payload_artifact_id).read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    if resource.media_kind == "text":
        return _sha256_text(payload) == resource.content_hash
    try:
        return _sha256_bytes(bytes.fromhex(payload)) == resource.content_hash
    except ValueError:
        return False


def _normalize_resource_path(path: str) -> str | None:
    if not path or path.startswith("/") or "\\" in path:
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    normalized = posixpath.normpath(path)
    if not normalized.startswith(("references/", "assets/", "scripts/")):
        return None
    return normalized


def _overall_hash(
    *,
    manifest: dict[str, Any],
    skill_md_text: str,
    resources: list[FrozenResourceSnapshot],
) -> str:
    payload = {
        "manifest": manifest,
        "skill_md_text": _normalize_text(skill_md_text),
        "resources": [
            {
                "resource_path": resource.resource_path,
                "resource_kind": resource.resource_kind,
                "media_kind": resource.media_kind,
                "size_bytes": resource.size_bytes,
                "content_hash": resource.content_hash,
            }
            for resource in resources
        ],
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _sha256_text(text: str) -> str:
    return _sha256_bytes(_normalize_text(text).encode("utf-8"))


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _render_todo_plan(
    plan_version: int, items: list[dict[str, Any]], counts: dict[str, int]
) -> str:
    if not items:
        return f"Todo Plan v{plan_version}: empty"
    lines = [
        (
            f"Todo Plan v{plan_version}: {counts['pending']} pending, "
            f"{counts['in_progress']} in_progress, {counts['completed']} completed"
        )
    ]
    markers = {"completed": "[o]", "in_progress": "[>]", "pending": "[ ]"}
    completed_steps = [
        int(item["index"])
        for item in items
        if item["status"] == "completed"
    ]
    if len(completed_steps) > 1:
        lines.append(f"[o] (steps {_format_step_ranges(completed_steps)} done)")

    pending_seen = 0
    aggregated_pending_steps: list[int] = []
    for item in items:
        status = item["status"]
        if status == "completed" and len(completed_steps) > 1:
            continue
        if status == "pending":
            pending_seen += 1
            if counts["pending"] > 4 and pending_seen >= 4:
                aggregated_pending_steps.append(int(item["index"]))
                continue
        lines.append(f"{markers[status]} {item['index']}. {item['content']}")
    if aggregated_pending_steps:
        lines.append(
            f"[ ] (steps {_format_step_ranges(aggregated_pending_steps)} pending)"
        )
    return "\n".join(lines)


def _format_step_ranges(steps: list[int]) -> str:
    ranges: list[str] = []
    start: int | None = None
    previous: int | None = None
    for step in steps:
        if start is None or previous is None:
            start = previous = step
            continue
        if step == previous + 1:
            previous = step
            continue
        ranges.append(_format_step_range(start, previous))
        start = previous = step
    if start is not None and previous is not None:
        ranges.append(_format_step_range(start, previous))
    return ", ".join(ranges)


def _format_step_range(start: int, end: int) -> str:
    if start == end:
        return str(start)
    return f"{start}-{end}"

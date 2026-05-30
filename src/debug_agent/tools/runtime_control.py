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
            description="Load one frozen resource file for an active skill.",
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
    ]


def tool_handlers() -> dict[str, Any]:
    return {
        "activate_skill": activate_skill,
        "load_skill_resource": load_skill_resource,
    }


def validate_target(context: Any, tool_name: str, arguments: dict[str, Any]) -> RuntimeControlTarget:
    if tool_name == "activate_skill":
        return _validate_activation(context, arguments)
    if tool_name == "load_skill_resource":
        return _validate_resource(context, arguments)
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

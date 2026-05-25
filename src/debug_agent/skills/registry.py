from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.runtime.contracts import utc_now_iso


INLINE_PAYLOAD_THRESHOLD_BYTES = 16 * 1024
MANIFEST_FIELDS = frozenset(
    {"name", "description", "execution_mode", "triggers", "metadata"}
)
SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class SkillRegistryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str = "config_error",
        source: str = "skill_registry",
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.source = source
        self.recoverable = recoverable


@dataclass(frozen=True)
class ReferenceSnapshot:
    reference_snapshot_id: str
    reference_path: str
    media_kind: str
    size_bytes: int
    content_hash: str
    inline_text_payload: str | None
    payload_artifact_id: str | None
    created_at: str
    version: int = 1


@dataclass(frozen=True)
class SkillSnapshot:
    skill_snapshot_id: str
    session_id: str
    run_id: str
    name: str
    execution_mode: str
    source_scope: str
    source_path: str
    manifest: dict[str, Any]
    skill_md_content: str
    skill_md_content_hash: str
    overall_content_hash: str
    payload_artifact_id: str | None
    references: list[ReferenceSnapshot] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    version: int = 1


@dataclass(frozen=True)
class _DiscoveredSkill:
    scope: str
    directory: Path
    skill_md_path: Path
    manifest: dict[str, Any]
    skill_md_text: str
    body: str


class SkillRegistry:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        artifact_store: ArtifactStore,
        home_dir: str | Path | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home_dir = Path(home_dir or _default_home()).resolve()
        self.artifact_store = artifact_store

    def snapshot(self, *, session_id: str, run_id: str) -> list[SkillSnapshot]:
        selected: dict[str, _DiscoveredSkill] = {}
        for scope, root in self._roots_by_precedence():
            discovered = self._discover_scope(scope, root)
            for name, skill in discovered.items():
                if name not in selected:
                    selected[name] = skill
        snapshots = [
            self._snapshot_skill(session_id=session_id, run_id=run_id, skill=skill)
            for skill in sorted(selected.values(), key=lambda item: item.manifest["name"])
        ]
        return snapshots

    def _roots_by_precedence(self) -> list[tuple[str, Path]]:
        return [
            ("project", self.workspace_root / ".debug-agent" / "skills"),
            ("global", self.home_dir / ".debug-agent" / "skills"),
        ]

    def _discover_scope(self, scope: str, root: Path) -> dict[str, _DiscoveredSkill]:
        if not root.exists():
            return {}
        if not root.is_dir():
            return {}
        by_name: dict[str, _DiscoveredSkill] = {}
        for child in sorted(root.iterdir(), key=lambda path: _normalized_path(path)):
            if child.is_symlink() or not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                raise SkillRegistryError(f"Missing root SKILL.md in skill directory: {child}")
            text = _read_utf8(skill_md, "SKILL.md")
            manifest, body = _parse_skill_md(text, skill_md)
            name = manifest["name"]
            if name in by_name:
                raise SkillRegistryError(f"Duplicate skill name in {scope} scope: {name}")
            by_name[name] = _DiscoveredSkill(
                scope=scope,
                directory=child.resolve(),
                skill_md_path=skill_md.resolve(),
                manifest=manifest,
                skill_md_text=_normalize_text(text),
                body=_normalize_text(body),
            )
        return by_name

    def _snapshot_skill(
        self, *, session_id: str, run_id: str, skill: _DiscoveredSkill
    ) -> SkillSnapshot:
        created_at = utc_now_iso()
        references = self._snapshot_references(
            session_id=session_id,
            run_id=run_id,
            skill_name=skill.manifest["name"],
            skill_dir=skill.directory,
            created_at=created_at,
        )
        skill_md_hash = _sha256_text(skill.skill_md_text)
        overall_hash = _overall_hash(
            manifest=skill.manifest,
            skill_md_text=skill.skill_md_text,
            references=references,
        )
        payload_artifact_id = self._artifact_skill_payload_if_needed(
            session_id=session_id,
            run_id=run_id,
            skill=skill,
            skill_md_hash=skill_md_hash,
            overall_hash=overall_hash,
            references=references,
        )
        return SkillSnapshot(
            skill_snapshot_id=f"skill_{uuid4().hex}",
            session_id=session_id,
            run_id=run_id,
            name=skill.manifest["name"],
            execution_mode=skill.manifest["execution_mode"],
            source_scope=skill.scope,
            source_path=_normalized_path(skill.directory),
            manifest=skill.manifest,
            skill_md_content=skill.skill_md_text,
            skill_md_content_hash=skill_md_hash,
            overall_content_hash=overall_hash,
            payload_artifact_id=payload_artifact_id,
            references=references,
            created_at=created_at,
        )

    def _snapshot_references(
        self,
        *,
        session_id: str,
        run_id: str,
        skill_name: str,
        skill_dir: Path,
        created_at: str,
    ) -> list[ReferenceSnapshot]:
        references_root = skill_dir / "references"
        if not references_root.exists():
            return []
        references: list[ReferenceSnapshot] = []
        for path in sorted(
            (item for item in references_root.rglob("*") if item.is_file()),
            key=lambda item: _relative_reference_path(references_root, item),
        ):
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise SkillRegistryError(f"Unreadable reference file: {path}") from exc
            reference_path = _relative_reference_path(references_root, path)
            text_payload: str | None
            media_kind: str
            artifact_id: str | None = None
            try:
                text_payload = _normalize_text(payload.decode("utf-8"))
                media_kind = "text"
                content_hash = _sha256_text(text_payload)
            except UnicodeDecodeError:
                text_payload = None
                media_kind = "binary"
                content_hash = _sha256_bytes(payload)
            if (
                media_kind == "binary"
                or len((text_payload or "").encode("utf-8"))
                > INLINE_PAYLOAD_THRESHOLD_BYTES
            ):
                content = text_payload if text_payload is not None else payload.hex()
                artifact = self.artifact_store.write_text(
                    session_id=session_id,
                    run_id=run_id,
                    filename=f"{skill_name}_{path.name}.snapshot.txt",
                    content=content,
                    metadata={
                        "kind": "skill_reference_snapshot",
                        "skill_name": skill_name,
                        "reference_path": reference_path,
                        "media_kind": media_kind,
                        "bytes": len(payload),
                        "content_hash": content_hash,
                    },
                )
                artifact_id = artifact.artifact_id
                text_payload = None
            references.append(
                ReferenceSnapshot(
                    reference_snapshot_id=f"skill_ref_{uuid4().hex}",
                    reference_path=reference_path,
                    media_kind=media_kind,
                    size_bytes=len(payload),
                    content_hash=content_hash,
                    inline_text_payload=text_payload,
                    payload_artifact_id=artifact_id,
                    created_at=created_at,
                )
            )
        return references

    def _artifact_skill_payload_if_needed(
        self,
        *,
        session_id: str,
        run_id: str,
        skill: _DiscoveredSkill,
        skill_md_hash: str,
        overall_hash: str,
        references: list[ReferenceSnapshot],
    ) -> str | None:
        payload = {
            "manifest": skill.manifest,
            "skill_md_content": skill.skill_md_text,
            "skill_md_content_hash": skill_md_hash,
            "overall_content_hash": overall_hash,
            "references": [
                {
                    "reference_path": ref.reference_path,
                    "media_kind": ref.media_kind,
                    "size_bytes": ref.size_bytes,
                    "content_hash": ref.content_hash,
                    "inline_text_payload": ref.inline_text_payload,
                    "payload_artifact_id": ref.payload_artifact_id,
                }
                for ref in references
            ],
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) <= INLINE_PAYLOAD_THRESHOLD_BYTES:
            return None
        artifact = self.artifact_store.write_text(
            session_id=session_id,
            run_id=run_id,
            filename=f"{skill.manifest['name']}_skill_snapshot.json",
            content=serialized,
            metadata={
                "kind": "skill_snapshot_payload",
                "skill_name": skill.manifest["name"],
                "bytes": len(serialized.encode("utf-8")),
                "overall_content_hash": overall_hash,
            },
        )
        return artifact.artifact_id


def _default_home() -> Path:
    home = os.environ.get("DEBUG_AGENT_HOME") or os.environ.get("HOME")
    return Path(home) if home else Path.home()


def _parse_skill_md(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise SkillRegistryError(f"SKILL.md must start with YAML front matter: {path}")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise SkillRegistryError(f"SKILL.md front matter is not closed: {path}")
    raw_manifest = text[4:end]
    body = text[end + len("\n---\n") :]
    try:
        loaded = yaml.safe_load(raw_manifest)
    except yaml.YAMLError as exc:
        raise SkillRegistryError(f"Invalid SKILL.md front matter: {path}") from exc
    if not isinstance(loaded, dict):
        raise SkillRegistryError("Skill manifest must be a mapping.")
    manifest = _validate_manifest(loaded)
    return manifest, body


def _validate_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(raw) - MANIFEST_FIELDS)
    if unknown:
        raise SkillRegistryError(f"Unknown skill manifest field: {unknown[0]}")
    name = raw.get("name")
    description = raw.get("description")
    if not isinstance(name, str):
        raise SkillRegistryError("Skill manifest name must be a string.")
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillRegistryError("Invalid skill name.")
    if not isinstance(description, str):
        raise SkillRegistryError("Skill manifest description must be a string.")
    execution_mode = raw.get("execution_mode", "prompt")
    if not isinstance(execution_mode, str):
        raise SkillRegistryError("Skill manifest execution_mode must be a string.")
    if execution_mode != "prompt":
        raise SkillRegistryError("Only prompt skills are supported in Phase 1.")
    triggers = raw.get("triggers", [])
    if not isinstance(triggers, list) or any(
        not isinstance(item, str) for item in triggers
    ):
        raise SkillRegistryError("Skill manifest triggers must be a list of strings.")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SkillRegistryError("Skill manifest metadata must be a mapping.")
    return {
        "name": name,
        "description": description,
        "execution_mode": execution_mode,
        "triggers": triggers,
        "metadata": metadata,
    }


def _read_utf8(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillRegistryError(f"Unreadable UTF-8 {label}: {path}") from exc


def _overall_hash(
    *,
    manifest: dict[str, Any],
    skill_md_text: str,
    references: list[ReferenceSnapshot],
) -> str:
    payload = {
        "manifest": manifest,
        "skill_md_text": skill_md_text,
        "references": [
            {
                "reference_path": ref.reference_path,
                "media_kind": ref.media_kind,
                "size_bytes": ref.size_bytes,
                "content_hash": ref.content_hash,
            }
            for ref in references
        ],
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _sha256_text(text: str) -> str:
    return _sha256_bytes(_normalize_text(text).encode("utf-8"))


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalized_path(path: Path) -> str:
    return path.resolve().as_posix()


def _relative_reference_path(references_root: Path, path: Path) -> str:
    return ("references" / path.relative_to(references_root)).as_posix()

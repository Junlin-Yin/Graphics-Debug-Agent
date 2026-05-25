from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from debug_agent.runtime.config import ConfigError


BUILTIN_DIRECTORY_DENIES = (
    ".git",
    "node_modules",
    "build",
    "dist",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".sessions",
)
PATH_OPTION_NAMES = frozenset(
    {
        "--output",
        "--out",
        "--config",
        "--file",
        "--path",
        "--cwd",
        "--directory",
        "--root",
        "--input",
        "--src",
        "--source",
        "--dest",
        "--destination",
        "-o",
        "-c",
        "-f",
        "-C",
        "-I",
    }
)
WINDOWS_SUFFIXES = (".exe", ".cmd", ".bat")
RAW_SHELL_TRAMPOLINES = {
    ("sh", "-c"),
    ("bash", "-c"),
    ("zsh", "-c"),
    ("cmd", "/c"),
    ("powershell", "-command"),
    ("pwsh", "-command"),
}


@dataclass
class PathPolicyEntry:
    scope: str
    raw: str
    path: Path
    subtree: bool
    component_name: str | None = None

    @classmethod
    def from_raw(
        cls, scope: str, raw: str, workspace_root: str | Path, home: str | Path
    ) -> "PathPolicyEntry":
        if scope not in {"trust", "deny"}:
            raise ValueError(f"Invalid path policy scope: {scope}")
        if not isinstance(raw, str) or not raw:
            raise ValueError("Path policy paths must be non-empty strings.")
        subtree = raw.endswith(("/", "\\"))
        expanded = raw
        if raw.startswith("~/"):
            expanded = str(Path(home) / raw[2:])
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = Path(workspace_root) / candidate
        return cls(
            scope=scope,
            raw=raw,
            path=_canonicalize_candidate(candidate),
            subtree=subtree,
        )


@dataclass
class ShellPolicy:
    allow: list[tuple[str, ...]] = field(default_factory=list)
    deny: list[tuple[str, ...]] = field(default_factory=list)

    def matches_allow(self, normalized_argv: tuple[str, ...]) -> bool:
        return any(_prefix_matches(normalized_argv, prefix) for prefix in self.allow)

    def matches_deny(self, normalized_argv: tuple[str, ...]) -> bool:
        return any(_prefix_matches(normalized_argv, prefix) for prefix in self.deny)


@dataclass
class PolicyFacts:
    workspace_root: Path
    home: Path
    builtin_path_deny: list[PathPolicyEntry]
    user_path_trust: list[PathPolicyEntry]
    user_path_deny: list[PathPolicyEntry]
    builtin_shell_deny: ShellPolicy
    user_shell: ShellPolicy


@dataclass(frozen=True)
class PolicyLoadResult:
    facts: PolicyFacts | None
    error: ConfigError | None


@dataclass(frozen=True)
class PathClassification:
    path: Path
    classification: str
    matched_policy: str | None = None


@dataclass(frozen=True)
class ClassifiedArgvPath:
    original: str
    path: Path


@dataclass(frozen=True)
class NormalizedToolCall:
    tool_name: str
    category: str
    risk_level: str
    access: tuple[str, ...]
    paths: tuple[Path, ...] = ()
    shell_argv: tuple[str, ...] = ()
    approval_scope_signature: str = ""
    runtime_control_valid: bool = True


@dataclass(frozen=True)
class ApprovalGrant:
    session_id: str
    tool_name: str
    risk_level: str
    scope_signature: str


@dataclass(frozen=True)
class PermissionDecision:
    decision: str
    reason: str
    error_class: str | None = None
    message: str | None = None
    path_classification: str | None = None


class PermissionEvaluator:
    def __init__(self, policy_facts: PolicyFacts) -> None:
        self.policy_facts = policy_facts

    def classify_path(self, path: str | Path) -> PathClassification:
        canonical = canonicalize_path(path, self.policy_facts.workspace_root)
        for entry in self.policy_facts.builtin_path_deny:
            if _entry_matches(entry, canonical):
                return PathClassification(canonical, "denied", entry.raw)
        for entry in self.policy_facts.user_path_deny:
            if _entry_matches(entry, canonical):
                return PathClassification(canonical, "denied", entry.raw)
        for entry in self.policy_facts.user_path_trust:
            if _entry_matches(entry, canonical):
                return PathClassification(canonical, "trusted", entry.raw)
        return PathClassification(canonical, "untrusted", None)

    def evaluate(
        self,
        call: NormalizedToolCall,
        *,
        approval_mode: str,
        reusable_grants: list[ApprovalGrant] | None = None,
        session_id: str | None = None,
    ) -> PermissionDecision:
        if approval_mode not in {"normal", "semi-auto", "yolo"}:
            return PermissionDecision(
                "deny",
                "invalid_approval_mode",
                "config_error",
                f"Invalid approval mode: {approval_mode}",
            )
        if not call.runtime_control_valid:
            return PermissionDecision(
                "deny",
                "invalid_runtime_control_target",
                "policy_denied",
                "Invalid runtime-control target.",
            )
        path_classes = [self.classify_path(path) for path in call.paths]
        denied_path = next(
            (item for item in path_classes if item.classification == "denied"), None
        )
        if denied_path is not None:
            return PermissionDecision(
                "deny",
                "path_denied",
                "policy_denied",
                f"Path denied by policy: {denied_path.path}",
                "denied",
            )
        normalized_shell = call.shell_argv
        if call.category == "shell":
            if is_builtin_shell_denied(normalized_shell):
                return PermissionDecision(
                    "deny",
                    "builtin_shell_denied",
                    "policy_denied",
                    "Command denied by builtin shell policy.",
                )
            if self.policy_facts.user_shell.matches_deny(normalized_shell):
                return PermissionDecision(
                    "deny",
                    "user_shell_denied",
                    "policy_denied",
                    "Command denied by user shell policy.",
                )
            if (
                self.policy_facts.user_shell.allow
                and not self.policy_facts.user_shell.matches_allow(normalized_shell)
            ):
                return PermissionDecision(
                    "deny",
                    "shell_allowlist_miss",
                    "policy_denied",
                    "Command does not match shell allowlist.",
                )

        trusted = bool(path_classes) and all(
            item.classification == "trusted" for item in path_classes
        )
        if not path_classes:
            trusted = True
        mode_decision = _approval_mode_decision(
            approval_mode=approval_mode,
            risk_level=call.risk_level,
            trusted=trusted,
            category=call.category,
            tool_name=call.tool_name,
        )
        if mode_decision == "allow":
            return PermissionDecision("allow", "approval_mode", path_classification=_trust_label(trusted))
        grant = _matching_grant(
            call,
            reusable_grants or [],
            session_id=session_id,
        )
        if grant is not None:
            return PermissionDecision("allow", "approval_grant", path_classification=_trust_label(trusted))
        return PermissionDecision("ask", "approval_required", path_classification=_trust_label(trusted))


def load_main_agent_policy(workspace_root: str | Path) -> PolicyLoadResult:
    workspace = Path(workspace_root).resolve()
    home = _home_path()
    facts = build_builtin_policy(workspace, home)
    path = home / ".debug-agent" / "agent.toml"
    if not path.exists():
        return PolicyLoadResult(facts=facts, error=None)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return PolicyLoadResult(facts=None, error=_config_error(f"Invalid agent.toml: {exc}"))

    if any(key.endswith("regex") or "regex" in key for key in raw.get("shell_policy", {})):
        return PolicyLoadResult(
            facts=None,
            error=_config_error("regex shell policy shapes are not supported in Phase 1."),
        )
    try:
        for entry in raw.get("path_policies", []):
            if not isinstance(entry, dict):
                raise ValueError("path policy entries must be tables.")
            scope = entry.get("scope")
            if scope not in {"trust", "deny"}:
                raise ValueError(f"Invalid path policy scope: {scope}")
            paths = entry.get("paths")
            if not isinstance(paths, list) or not all(
                isinstance(item, str) for item in paths
            ):
                raise ValueError("path policy paths must be a list of strings.")
            target = facts.user_path_trust if scope == "trust" else facts.user_path_deny
            for raw_path in paths:
                target.append(PathPolicyEntry.from_raw(scope, raw_path, workspace, home))
        shell_policy = raw.get("shell_policy", {})
        if shell_policy is None:
            shell_policy = {}
        if not isinstance(shell_policy, dict):
            raise ValueError("shell_policy must be a table.")
        facts.user_shell = ShellPolicy(
            allow=_parse_shell_prefixes(shell_policy.get("allow", [])),
            deny=_parse_shell_prefixes(shell_policy.get("deny", [])),
        )
    except ValueError as exc:
        return PolicyLoadResult(facts=None, error=_config_error(str(exc)))
    return PolicyLoadResult(facts=facts, error=None)


def build_builtin_policy(
    workspace_root: str | Path, home: str | Path | None = None
) -> PolicyFacts:
    workspace = Path(workspace_root).resolve()
    home_path = Path(home).resolve() if home is not None else _home_path()
    builtin_entries = [
        PathPolicyEntry(
            scope="deny",
            raw=f"{name}/",
            path=workspace / name,
            subtree=True,
            component_name=name,
        )
        for name in BUILTIN_DIRECTORY_DENIES
    ]
    builtin_entries.append(
        PathPolicyEntry.from_raw("deny", "~/.debug-agent/skills/", workspace, home_path)
    )
    builtin_entries.append(
        PathPolicyEntry.from_raw(
            "deny", ".debug-agent/skills/", workspace, home_path
        )
    )
    return PolicyFacts(
        workspace_root=workspace,
        home=home_path,
        builtin_path_deny=builtin_entries,
        user_path_trust=[
            PathPolicyEntry(
                scope="trust",
                raw=str(workspace),
                path=workspace,
                subtree=True,
            )
        ],
        user_path_deny=[],
        builtin_shell_deny=ShellPolicy(),
        user_shell=ShellPolicy(),
    )


def policy_facts_to_snapshot(facts: PolicyFacts) -> dict[str, Any]:
    return {
        "builtin_path_deny": [_entry_snapshot(entry) for entry in facts.builtin_path_deny],
        "user_path_trust": [_entry_snapshot(entry) for entry in facts.user_path_trust],
        "user_path_deny": [_entry_snapshot(entry) for entry in facts.user_path_deny],
        "user_shell": {
            "allow": [list(prefix) for prefix in facts.user_shell.allow],
            "deny": [list(prefix) for prefix in facts.user_shell.deny],
        },
        "builtin_shell_denies": {
            "privilege_escalation": ["sudo", "su", "doas"],
            "recursive_rm": True,
            "raw_shell_trampolines": [list(prefix) for prefix in sorted(RAW_SHELL_TRAMPOLINES)],
        },
    }


def canonicalize_path(path: str | Path, workspace_root: str | Path) -> Path:
    if isinstance(path, str):
        windows_absolute = _windows_absolute_path(path)
        if windows_absolute is not None:
            return windows_absolute
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(workspace_root) / candidate
    return _canonicalize_candidate(candidate)


def normalize_shell_argv(argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    tokens = list(argv)
    if tokens and _command_identity(tokens[0]) == "env":
        index = 1
        while index < len(tokens) and _is_env_assignment(tokens[index]):
            index += 1
        tokens = tokens[index:]
    if not tokens:
        return ()
    return tuple([_command_identity(tokens[0]), *[_normalize_shell_token(t) for t in tokens[1:]]])


def classify_argv_paths(
    argv: list[str] | tuple[str, ...], workspace_root: str | Path
) -> list[ClassifiedArgvPath]:
    classified: list[ClassifiedArgvPath] = []
    tokens = list(argv)
    if tokens and _is_path_qualified(tokens[0]):
        classified.append(
            ClassifiedArgvPath(tokens[0], canonicalize_path(tokens[0], workspace_root))
        )
    skip_next = False
    for index, token in enumerate(tokens[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        option, sep, value = token.partition("=")
        if sep and option in PATH_OPTION_NAMES and _is_path_like(value):
            classified.append(
                ClassifiedArgvPath(value, canonicalize_path(value, workspace_root))
            )
            continue
        if token in PATH_OPTION_NAMES and index + 1 < len(tokens):
            value = tokens[index + 1]
            if not value.startswith("-") and "://" not in value:
                classified.append(
                    ClassifiedArgvPath(value, canonicalize_path(value, workspace_root))
                )
            skip_next = True
            continue
        if not token.startswith("-") and _is_path_like(token):
            classified.append(
                ClassifiedArgvPath(token, canonicalize_path(token, workspace_root))
            )
    return classified


def is_builtin_shell_denied(normalized_argv: tuple[str, ...]) -> bool:
    if not normalized_argv:
        return True
    command = normalized_argv[0]
    if command in {"sudo", "su", "doas"}:
        return True
    if command == "rm" and any(_is_recursive_rm_option(token) for token in normalized_argv[1:]):
        return True
    lowered = tuple(token.lower() for token in normalized_argv[:2])
    return lowered in RAW_SHELL_TRAMPOLINES


def scope_signature_for_tool(
    tool_name: str,
    *,
    risk_level: str,
    paths: list[Path] | None = None,
    shell_argv: tuple[str, ...] | None = None,
    cwd: Path | None = None,
    effective_timeout_seconds: int | None = None,
    classified_paths: list[Path] | None = None,
    skill_name: str | None = None,
    skill_content_hash: str | None = None,
    reference_path: str | None = None,
    reference_content_hash: str | None = None,
) -> str:
    if tool_name in {"read_file", "list_dir", "search_text", "write_file", "edit_file"}:
        access = "read" if risk_level == "read" else "write"
        canonical_paths = [str(Path(path).resolve()) for path in paths or []]
        return f"{tool_name}|{risk_level}|" + "|".join(
            f"{access}:{path}" for path in sorted(canonical_paths)
        )
    if tool_name == "shell_exec":
        path_part = "|".join(str(Path(path).resolve()) for path in sorted(classified_paths or []))
        return (
            f"shell_exec|{risk_level}|argv:{'\\x00'.join(shell_argv or ())}|"
            f"cwd:{Path(cwd).resolve() if cwd is not None else ''}|"
            f"timeout:{effective_timeout_seconds}|paths:{path_part}"
        )
    if tool_name == "activate_skill":
        return (
            f"activate_skill|{risk_level}|skill:{skill_name}|"
            f"skill_hash:{skill_content_hash}"
        )
    if tool_name == "load_skill_ref_file":
        return (
            f"load_skill_ref_file|{risk_level}|skill:{skill_name}|"
            f"skill_hash:{skill_content_hash}|ref:{reference_path}|"
            f"ref_hash:{reference_content_hash}"
        )
    return f"{tool_name}|{risk_level}"


def _canonicalize_candidate(candidate: Path) -> Path:
    if candidate.exists():
        return candidate.resolve()
    parts = candidate.parts
    existing = candidate
    missing: list[str] = []
    while not existing.exists() and existing != existing.parent:
        missing.append(existing.name)
        existing = existing.parent
    base = existing.resolve() if existing.exists() else candidate.anchor
    if not isinstance(base, Path):
        base = Path(base)
    for part in reversed(missing):
        base = base / part
    return Path(os.path.normpath(base))


def _entry_matches(entry: PathPolicyEntry, candidate: Path) -> bool:
    if entry.component_name and entry.component_name in candidate.parts:
        return True
    if entry.subtree:
        try:
            candidate.relative_to(entry.path)
            return True
        except ValueError:
            return False
    return candidate == entry.path


def _entry_snapshot(entry: PathPolicyEntry) -> dict[str, Any]:
    return {
        "scope": entry.scope,
        "raw": entry.raw,
        "path": str(entry.path),
        "subtree": entry.subtree,
        "component_name": entry.component_name,
    }


def _command_identity(token: str) -> str:
    name = Path(token).name if _is_path_qualified(token) else token
    lowered = name.lower()
    for suffix in WINDOWS_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return lowered


def _normalize_shell_token(token: str) -> str:
    return token.lower() if token.lower() in {"-command", "/c"} else token


def _is_path_qualified(token: str) -> bool:
    return (
        "/" in token
        or "\\" in token
        or token.startswith(".")
        or bool(re.match(r"^[A-Za-z]:[\\/]", token))
    )


def _is_path_like(token: str) -> bool:
    if "://" in token or token.startswith("$"):
        return False
    return (
        "/" in token
        or "\\" in token
        or token.startswith(".")
        or bool(re.match(r"^[A-Za-z]:[\\/]", token))
    )


def _is_env_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", token))


def _windows_absolute_path(token: str) -> Path | None:
    if re.match(r"^[A-Za-z]:[\\/]", token):
        return Path("/__debug_agent_windows_drive__") / token[0].lower() / token[3:].replace("\\", "/")
    if token.startswith("\\\\"):
        return Path("/__debug_agent_windows_unc__") / token.lstrip("\\").replace("\\", "/")
    return None


def _is_recursive_rm_option(token: str) -> bool:
    if token == "--recursive":
        return True
    return token.startswith("-") and not token.startswith("--") and any(
        char in token[1:] for char in {"r", "R"}
    )


def _prefix_matches(argv: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    if len(prefix) > len(argv):
        return False
    normalized_prefix = tuple(
        _command_identity(token) if index == 0 else token
        for index, token in enumerate(prefix)
    )
    return argv[: len(normalized_prefix)] == normalized_prefix


def _parse_shell_prefixes(raw: Any) -> list[tuple[str, ...]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("shell policy prefixes must be lists of argv lists.")
    prefixes: list[tuple[str, ...]] = []
    for prefix in raw:
        if not isinstance(prefix, list) or not prefix or not all(
            isinstance(token, str) and token for token in prefix
        ):
            raise ValueError("shell policy prefixes must be non-empty argv lists.")
        prefixes.append(tuple(normalize_shell_argv(prefix)))
    return prefixes


def _approval_mode_decision(
    *,
    approval_mode: str,
    risk_level: str,
    trusted: bool,
    category: str,
    tool_name: str,
) -> str:
    if tool_name == "load_skill_ref_file":
        return "allow"
    if risk_level == "runtime_control":
        return "ask" if approval_mode == "normal" else "allow"
    if approval_mode == "yolo":
        return "allow"
    if approval_mode == "normal":
        return "allow" if risk_level == "read" and trusted else "ask"
    if risk_level == "read":
        return "allow"
    return "allow" if trusted else "ask"


def _matching_grant(
    call: NormalizedToolCall,
    grants: list[ApprovalGrant],
    *,
    session_id: str | None,
) -> ApprovalGrant | None:
    for grant in grants:
        if (
            (session_id is None or grant.session_id == session_id)
            and grant.tool_name == call.tool_name
            and grant.risk_level == call.risk_level
            and grant.scope_signature == call.approval_scope_signature
        ):
            return grant
    return None


def _trust_label(trusted: bool) -> str:
    return "trusted" if trusted else "untrusted"


def _home_path() -> Path:
    home = os.environ.get("DEBUG_AGENT_HOME") or os.environ.get("HOME")
    return Path(home).resolve() if home else Path.home().resolve()


def _config_error(message: str) -> ConfigError:
    return ConfigError(
        error_class="config_error",
        message=message,
        source="config",
        recoverable=True,
    )

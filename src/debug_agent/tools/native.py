from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from debug_agent.runtime.contracts import ToolDefinition, ToolResult
from debug_agent.runtime.policy import PermissionEvaluator
from debug_agent.tools.settings import DEFAULT_NATIVE_TOOL_LIMIT


class NativeToolContext(Protocol):
    workspace_root: Path
    permission_evaluator: PermissionEvaluator


@dataclass(frozen=True)
class NativeHandlerResult:
    status: str
    output: str | dict[str, Any] | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="read_file",
            description="Read file contents.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            category="native",
            risk_level="read",
            access=["read"],
        ),
        ToolDefinition(
            name="list_dir",
            description="List immediate directory entries.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            category="native",
            risk_level="read",
            access=["read"],
        ),
        ToolDefinition(
            name="search_text",
            description="Search UTF-8 text files with ripgrep-compatible pattern matching under authorized paths.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "default": "content",
                    },
                    "maxResults": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 100,
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "case_sensitive": {"type": "boolean", "default": True},
                    "fixed_strings": {"type": "boolean", "default": False},
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
                    "include_hidden": {"type": "boolean", "default": False},
                    "type": {"type": "string"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            category="native",
            risk_level="read",
            access=["read"],
        ),
        ToolDefinition(
            name="write_file",
            description="Write content to file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            category="native",
            risk_level="write",
            access=["write"],
        ),
        ToolDefinition(
            name="edit_file",
            description="Replace exact text in file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            category="native",
            risk_level="write",
            access=["write"],
        ),
    ]


def gated_user_facing_tool_definitions(
    config_snapshot: dict[str, Any] | None = None,
) -> list[ToolDefinition]:
    from debug_agent.tools import runtime_control, shell

    max_timeout_seconds = 3600
    execution = (config_snapshot or {}).get("execution")
    if isinstance(execution, dict) and isinstance(
        execution.get("max_shell_timeout_seconds"), int
    ):
        max_timeout_seconds = execution["max_shell_timeout_seconds"]
    return [
        *tool_definitions(),
        *shell.tool_definitions(max_timeout_seconds=max_timeout_seconds),
        *runtime_control.tool_definitions(),
    ]


def tool_handlers() -> dict[str, Any]:
    return {
        "read_file": read_file,
        "list_dir": list_dir,
        "search_text": search_text,
        "write_file": write_file,
        "edit_file": edit_file,
    }


def read_file(context: NativeToolContext, arguments: dict[str, Any]) -> str:
    text = Path(arguments["path"]).read_text(encoding="utf-8")
    limit = arguments.get("limit")
    if limit is None:
        return text
    lines = text.splitlines(keepends=True)
    return "".join(lines[:limit])


def list_dir(_context: NativeToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    target = Path(arguments["path"])
    entries: list[dict[str, Any]] = []
    limit = arguments.get("limit", DEFAULT_NATIVE_TOOL_LIMIT)
    for child in sorted(target.iterdir(), key=lambda path: path.name):
        if _context.permission_evaluator.classify_path(child).classification == "denied":
            continue
        entries.append(
            {
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
            }
        )
        if len(entries) >= limit:
            break
    return {"entries": entries}


def search_text(context: NativeToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments["query"]
    start = Path(arguments["path"])
    limit = arguments.get("limit", DEFAULT_NATIVE_TOOL_LIMIT)
    matches: list[dict[str, Any]] = []
    for path in _iter_search_files(context, start):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle, start=1):
                    text = line.rstrip("\n")
                    if query in text:
                        matches.append(
                            {
                                "path": _display_path(path, context.workspace_root),
                                "line": index,
                                "text": text,
                            }
                        )
                        if len(matches) >= limit:
                            return {"matches": matches}
        except UnicodeDecodeError:
            continue
    return {"matches": matches}


def write_file(_context: NativeToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    target = Path(arguments["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(arguments["content"], encoding="utf-8")
    return {"path": target.as_posix(), "bytes": len(arguments["content"].encode("utf-8"))}


def edit_file(_context: NativeToolContext, arguments: dict[str, Any]) -> NativeHandlerResult:
    old_text = arguments["old_text"]
    if old_text == "":
        return NativeHandlerResult(
            status="error",
            error_message="old_text must be non-empty.",
        )
    target = Path(arguments["path"])
    raw = target.read_bytes()
    text = raw.decode("utf-8")
    line_ending = _dominant_line_ending(raw)
    normalized = _normalize_lf(text)
    old_normalized = _normalize_lf(old_text)
    new_normalized = _normalize_lf(arguments["new_text"])
    if old_normalized not in normalized:
        return NativeHandlerResult(
            status="error",
            error_message="old_text was not found.",
        )
    replaced = normalized.replace(old_normalized, new_normalized, 1)
    output_text = replaced.replace("\n", line_ending)
    target.write_text(output_text, encoding="utf-8", newline="")
    return NativeHandlerResult(
        status="ok",
        output={
            "path": target.as_posix(),
            "replacements": 1,
        },
    )


def _iter_search_files(context: NativeToolContext, start: Path):
    if start.is_file():
        yield start
        return
    for root, dirnames, filenames in os.walk(start):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            child = root_path / dirname
            if context.permission_evaluator.classify_path(child).classification == "denied":
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            child = root_path / filename
            if context.permission_evaluator.classify_path(child).classification == "denied":
                continue
            yield child


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path.resolve())


def _dominant_line_ending(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    without_crlf = raw.replace(b"\r\n", b"")
    lf = without_crlf.count(b"\n")
    cr = without_crlf.count(b"\r")
    counts = [("\r\n", crlf), ("\n", lf), ("\r", cr)]
    winner, count = max(counts, key=lambda item: item[1])
    return winner if count > 0 else "\n"


def tool_error_result(
    message: str,
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
    reason: str = "tool_execution_failed",
) -> ToolResult:
    return ToolResult(
        status="error",
        output=None,
        error={
            "schema_version": 1,
            "error_class": "tool_error",
            "reason": reason,
            "message": message,
            "scope": "tool",
            "source": source,
            "recoverable": True,
            "recoverability": "turn_recoverable",
            "metadata": metadata or {},
            "artifact_ids": [],
        },
        artifacts=[],
        metadata=metadata or {},
        redacted_output=None,
    )

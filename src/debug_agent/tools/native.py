from __future__ import annotations

import os
import fnmatch
import hashlib
import codecs
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from debug_agent.runtime.contracts import ToolDefinition, ToolResult
from debug_agent.runtime.policy import PermissionEvaluator
from debug_agent.tools.settings import (
    DEFAULT_NATIVE_TOOL_LIMIT,
    FIND_FILE_DEFAULT_MAX_RESULTS,
    FIND_FILE_MAX_RESULTS,
    LIST_DIR_DEFAULT_LIMIT,
    LIST_DIR_MAX_IGNORE_PATTERNS,
    LIST_DIR_MAX_LIMIT,
    READ_FILE_DEFAULT_LIMIT,
    READ_FILE_MAX_LIMIT,
)


class NativeToolContext(Protocol):
    workspace_root: Path
    permission_evaluator: PermissionEvaluator

    def write_lock_for_path(self, path: str | Path): ...


@dataclass(frozen=True)
class NativeHandlerResult:
    status: str
    output: str | dict[str, Any] | None = None
    error_message: str | None = None
    reason: str = "tool_execution_failed"
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
                    "ignore": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": LIST_DIR_MAX_IGNORE_PATTERNS,
                        "default": [],
                    },
                    "include_hidden": {"type": "boolean", "default": False},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": LIST_DIR_MAX_LIMIT,
                        "default": LIST_DIR_DEFAULT_LIMIT,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            category="native",
            risk_level="read",
            access=["read"],
        ),
        ToolDefinition(
            name="find_file",
            description="Find files by glob pattern under an authorized path.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
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
        "find_file": find_file,
        "search_text": search_text,
        "write_file": write_file,
        "edit_file": edit_file,
    }


def read_file(context: NativeToolContext, arguments: dict[str, Any]) -> NativeHandlerResult:
    target = Path(arguments["path"])
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", READ_FILE_DEFAULT_LIMIT))
    digest = hashlib.sha256()
    raw_size = 0
    page_lines: list[str] = []
    line_index = 0
    total_seen_for_page = 0
    current_line: list[str] = []
    current_has_content = False
    pending_cr = False
    line_scan_complete = False

    def capture_current_line() -> bool:
        return line_index >= offset and len(page_lines) < limit

    def append_char(char: str) -> None:
        nonlocal current_has_content
        current_has_content = True
        if capture_current_line():
            current_line.append(char)

    def finish_line() -> None:
        nonlocal current_has_content, line_index, total_seen_for_page, line_scan_complete
        if line_index >= offset:
            total_seen_for_page += 1
            if len(page_lines) < limit:
                page_lines.append("".join(current_line))
            else:
                line_scan_complete = True
        line_index += 1
        current_line.clear()
        current_has_content = False

    def process_text(text: str) -> None:
        nonlocal pending_cr
        for char in text:
            if line_scan_complete:
                return
            if pending_cr:
                if char == "\n":
                    append_char(char)
                    pending_cr = False
                    finish_line()
                    continue
                pending_cr = False
                finish_line()
            if char == "\r":
                append_char(char)
                pending_cr = True
                continue
            if char == "\n":
                append_char(char)
                finish_line()
                continue
            append_char(char)

    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                raw_size += len(chunk)
                digest.update(chunk)
                process_text(decoder.decode(chunk, final=False))
            process_text(decoder.decode(b"", final=True))
            if pending_cr:
                pending_cr = False
                finish_line()
            elif current_has_content:
                finish_line()
    except UnicodeDecodeError:
        return NativeHandlerResult(
            status="error",
            error_message="File is not valid UTF-8 text.",
        )
    returned = len(page_lines)
    truncated = line_scan_complete or total_seen_for_page > returned
    context.record_file_metadata(target, source_tool="read_file")
    return NativeHandlerResult(
        status="ok",
        output={
            "path": str(target.resolve()),
            "content": "".join(page_lines),
            "offset": offset,
            "limit": limit,
            "total_returned": returned,
            "truncated": truncated,
            "next_offset": offset + returned if truncated else None,
            "sha256": digest.hexdigest(),
            "bytes": raw_size,
        },
    )


def list_dir(context: NativeToolContext, arguments: dict[str, Any]) -> NativeHandlerResult:
    target = Path(arguments["path"])
    ignore_patterns = arguments.get("ignore", [])
    ignore_error = _validate_ignore_patterns(ignore_patterns)
    if ignore_error is not None:
        return NativeHandlerResult(
            status="error",
            error_message=ignore_error,
            reason="tool_schema_invalid",
        )
    entries: list[dict[str, Any]] = []
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("limit", LIST_DIR_DEFAULT_LIMIT))
    include_hidden = bool(arguments.get("include_hidden", False))
    for child in sorted(target.iterdir(), key=lambda path: path.name):
        if _is_denied(context, child):
            continue
        if not include_hidden and _is_hidden_relative(child.relative_to(target)):
            continue
        if _ignore_matches(child, ignore_patterns):
            continue
        entries.append(
            {
                "name": child.name,
                "type": _entry_type(child),
            }
        )
    page, truncated, next_offset = _paginate(entries, offset=offset, limit=limit)
    return NativeHandlerResult(
        status="ok",
        output={
            "path": str(target.resolve()),
            "entries": page,
            "offset": offset,
            "limit": limit,
            "total_returned": len(page),
            "truncated": truncated,
            "next_offset": next_offset,
        },
    )


def find_file(context: NativeToolContext, arguments: dict[str, Any]) -> NativeHandlerResult:
    pattern = arguments["pattern"]
    if not pattern.strip():
        return NativeHandlerResult(
            status="error",
            error_message="pattern must be non-empty.",
            reason="tool_schema_invalid",
        )
    try:
        glob = PortableGlob(pattern, case_sensitive=bool(arguments.get("case_sensitive", False)))
    except ValueError as exc:
        return NativeHandlerResult(
            status="error",
            error_message=str(exc),
            reason="tool_schema_invalid",
        )
    root = Path(arguments["path"])
    include_hidden = bool(arguments.get("include_hidden", False))
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("maxResults", FIND_FILE_DEFAULT_MAX_RESULTS))
    matches: list[str] = []
    for candidate in _iter_candidate_files(context, root, include_hidden=include_hidden):
        relative = candidate.relative_to(root).as_posix()
        if glob.matches(relative):
            matches.append(str(candidate))
    matches = sorted(dict.fromkeys(matches))
    page, truncated, next_offset = _paginate(matches, offset=offset, limit=limit)
    return NativeHandlerResult(
        status="ok",
        output={
            "root": str(root.resolve()),
            "pattern": pattern,
            "matches": page,
            "offset": offset,
            "maxResults": limit,
            "total_returned": len(page),
            "truncated": truncated,
            "next_offset": next_offset,
        },
    )


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


def write_file(context: NativeToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    target = Path(arguments["path"])
    with context.write_lock_for_path(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(arguments["content"], encoding="utf-8")
    return {"path": target.as_posix(), "bytes": len(arguments["content"].encode("utf-8"))}


def edit_file(context: NativeToolContext, arguments: dict[str, Any]) -> NativeHandlerResult:
    old_text = arguments["old_text"]
    if old_text == "":
        return NativeHandlerResult(
            status="error",
            error_message="old_text must be non-empty.",
        )
    target = Path(arguments["path"])
    with context.write_lock_for_path(target):
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


def _bytes_end_with_line_boundary(value: bytes) -> bool:
    return value.endswith(b"\n") or value.endswith(b"\r")


def _paginate(items: list[Any], *, offset: int, limit: int) -> tuple[list[Any], bool, int | None]:
    page = items[offset : offset + limit]
    truncated = offset + len(page) < len(items)
    return page, truncated, offset + len(page) if truncated else None


def _entry_type(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def _is_denied(context: NativeToolContext, path: Path) -> bool:
    return context.permission_evaluator.classify_path(path).classification == "denied"


def _is_hidden_relative(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _validate_ignore_patterns(patterns: list[str]) -> str | None:
    for pattern in patterns:
        if "\\" in pattern:
            return "ignore patterns must not contain backslash."
        if any(token in pattern for token in ("{", "}", "[", "]")):
            return "ignore pattern uses unsupported syntax."
        if any(token in pattern for token in ("!(", "?(", "+(", "*(", "@(")):
            return "ignore pattern uses unsupported extglob syntax."
        if pattern == "**":
            return "ignore pattern uses unsupported recursive syntax."
        if "/" in pattern:
            if pattern.endswith("/"):
                name = pattern[:-1]
            elif pattern.endswith("/**"):
                name = pattern[:-3]
            else:
                return "ignore patterns may only use foo/ or foo/** directory aliases."
            if not name or "/" in name or any(char in name for char in "*?[]{}()!"):
                return "ignore directory aliases must be literal child directory names."
    return None


def validate_list_dir_ignore_patterns(patterns: list[str]) -> str | None:
    return _validate_ignore_patterns(patterns)


def _ignore_matches(path: Path, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/"):
            if path.is_dir() and path.name == pattern[:-1]:
                return True
            continue
        if pattern.endswith("/**"):
            if path.is_dir() and path.name == pattern[:-3]:
                return True
            continue
        if fnmatch.fnmatchcase(path.name, pattern):
            return True
    return False


def _iter_candidate_files(
    context: NativeToolContext, root: Path, *, include_hidden: bool
):
    root = Path(root)
    if root.is_file() or root.is_symlink():
        if _candidate_file_allowed(context, root, root, include_hidden=include_hidden):
            yield _normalized_candidate_path(root)
        return
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            child = current_path / dirname
            try:
                relative = child.relative_to(root)
            except ValueError:
                continue
            if _is_denied(context, child):
                continue
            if not include_hidden and _is_hidden_relative(relative):
                continue
            if child.is_symlink():
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            child = current_path / filename
            if _candidate_file_allowed(context, child, root, include_hidden=include_hidden):
                yield _normalized_candidate_path(child)


def _candidate_file_allowed(
    context: NativeToolContext, candidate: Path, root: Path, *, include_hidden: bool
) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    if not include_hidden and _is_hidden_relative(relative):
        return False
    if _is_denied(context, candidate):
        return False
    if candidate.is_dir() and not candidate.is_symlink():
        return False
    if not candidate.is_file() and not candidate.is_symlink():
        return False
    if candidate.is_symlink():
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        if context.permission_evaluator.classify_path(resolved).classification == "denied":
            return False
        if not resolved.is_file():
            return False
    return True


def _normalized_candidate_path(path: Path) -> Path:
    if path.is_symlink():
        return Path(os.path.normpath(path.absolute()))
    return path.resolve()


class PortableGlob:
    def __init__(self, pattern: str, *, case_sensitive: bool) -> None:
        _validate_portable_glob(pattern)
        self.pattern = pattern
        self.case_sensitive = case_sensitive
        self.segments = pattern.split("/")

    def matches(self, relative_path: str) -> bool:
        candidate_segments = relative_path.split("/")
        pattern_segments = self.segments
        if not self.case_sensitive:
            candidate_segments = [segment.casefold() for segment in candidate_segments]
            pattern_segments = [segment.casefold() for segment in pattern_segments]
        return _match_segments(pattern_segments, candidate_segments)


def _match_segments(pattern_segments: list[str], candidate_segments: list[str]) -> bool:
    if not pattern_segments:
        return not candidate_segments
    head = pattern_segments[0]
    if head == "**":
        if _match_segments(pattern_segments[1:], candidate_segments):
            return True
        return bool(candidate_segments) and _match_segments(pattern_segments, candidate_segments[1:])
    if not candidate_segments:
        return False
    if not fnmatch.fnmatchcase(candidate_segments[0], head):
        return False
    return _match_segments(pattern_segments[1:], candidate_segments[1:])


def _validate_portable_glob(pattern: str) -> None:
    if "\\" in pattern:
        raise ValueError("glob patterns must not contain backslash.")
    if any(token in pattern for token in ("{", "}")):
        raise ValueError("glob pattern uses unsupported brace syntax.")
    for token in ("!(", "?(", "+(", "*(", "@("):
        if token in pattern:
            raise ValueError("glob pattern uses unsupported extglob syntax.")
    for segment in pattern.split("/"):
        if "**" in segment and segment != "**":
            raise ValueError("** is supported only as a complete path segment.")
        _validate_glob_segment(segment)


def validate_portable_glob_pattern(pattern: str) -> str | None:
    try:
        _validate_portable_glob(pattern)
    except ValueError as exc:
        return str(exc)
    return None


def _validate_glob_segment(segment: str) -> None:
    index = 0
    while index < len(segment):
        if segment[index] != "[":
            index += 1
            continue
        end = segment.find("]", index + 1)
        if end == -1:
            raise ValueError("glob pattern has malformed character class.")
        body = segment[index + 1 : end]
        if not body:
            raise ValueError("glob pattern has malformed character class.")
        if body.startswith("!") or body.startswith("^"):
            raise ValueError("glob pattern uses unsupported negated character class.")
        index = end + 1


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

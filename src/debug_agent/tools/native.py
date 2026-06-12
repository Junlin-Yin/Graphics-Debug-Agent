from __future__ import annotations

import fnmatch
import hashlib
import codecs
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import TimeoutError as ToolTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

from debug_agent.runtime.contracts import ToolDefinition, ToolResult
from debug_agent.runtime.policy import PermissionEvaluator
from debug_agent.tools.settings import (
    FIND_FILE_DEFAULT_MAX_RESULTS,
    FIND_FILE_MAX_RESULTS,
    LIST_DIR_DEFAULT_LIMIT,
    LIST_DIR_MAX_IGNORE_PATTERNS,
    LIST_DIR_MAX_LIMIT,
    READ_FILE_DEFAULT_LIMIT,
    READ_FILE_MAX_LIMIT,
)


SEARCH_TEXT_TYPES = {
    "c": ("**/*.c", "**/*.h"),
    "cpp": ("**/*.cc", "**/*.cpp", "**/*.cxx", "**/*.hh", "**/*.hpp", "**/*.hxx"),
    "csharp": ("**/*.cs",),
    "css": ("**/*.css",),
    "go": ("**/*.go",),
    "html": ("**/*.html", "**/*.htm"),
    "java": ("**/*.java",),
    "javascript": ("**/*.js", "**/*.mjs", "**/*.cjs", "**/*.jsx"),
    "json": ("**/*.json", "**/*.jsonl"),
    "markdown": ("**/*.md", "**/*.markdown"),
    "python": ("**/*.py", "**/*.pyi"),
    "rust": ("**/*.rs",),
    "shell": ("**/*.sh", "**/*.bash", "**/*.zsh"),
    "text": ("**/*.txt",),
    "toml": ("**/*.toml",),
    "typescript": ("**/*.ts", "**/*.tsx"),
    "yaml": ("**/*.yaml", "**/*.yml"),
}

SEARCH_TEXT_LINE_PREVIEW_CODEPOINTS = 4000
SEARCH_TEXT_STDERR_PREVIEW_CODEPOINTS = 4000
SEARCH_TEXT_RG_JSON_RECORD_BYTES = 1024 * 1024
SEARCH_TEXT_RG_STDOUT_CHUNK_BYTES = 8192


def is_search_text_type_allowed(type_name: str) -> bool:
    return type_name in SEARCH_TEXT_TYPES


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


class _RipgrepExecutionError(Exception):
    pass


class _RipgrepCandidateSkipped(Exception):
    def __init__(self, counter: str) -> None:
        super().__init__(counter)
        self.counter = counter


class _RipgrepRecordSkipped(Exception):
    def __init__(self, counter: str) -> None:
        super().__init__(counter)
        self.counter = counter


class _SearchPageCollector:
    def __init__(self, *, offset: int, limit: int) -> None:
        self.offset = offset
        self.limit = limit
        self.seen = 0
        self.page: list[Any] = []
        self.truncated = False

    def add(self, item: Any) -> None:
        if self.seen < self.offset:
            self.seen += 1
            return
        if len(self.page) < self.limit:
            self.page.append(item)
        else:
            self.truncated = True
        self.seen += 1

    @property
    def next_offset(self) -> int | None:
        return self.offset + len(self.page) if self.truncated else None

    def snapshot(self) -> tuple[int, list[Any], bool]:
        return self.seen, list(self.page), self.truncated

    def restore(self, snapshot: tuple[int, list[Any], bool]) -> None:
        self.seen, self.page, self.truncated = snapshot


class _SearchContentPager(_SearchPageCollector):
    pass


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


def search_text(context: NativeToolContext, arguments: dict[str, Any]) -> NativeHandlerResult:
    root = Path(arguments["path"])
    pattern = arguments["pattern"]
    output_mode = arguments.get("output_mode", "content")
    offset = int(arguments.get("offset", 0))
    limit = int(arguments.get("maxResults", 100))
    before_context = int(arguments.get("before_context_effective", 0))
    after_context = int(arguments.get("after_context_effective", 0))
    skipped = {"denied": 0, "hidden": 0, "decode_error": 0, "other": 0}

    rg = shutil.which("rg")
    if rg is None:
        return NativeHandlerResult(
            status="error",
            error_message="rg executable is required for search_text.",
        )
    common_args = _ripgrep_common_args(
        pattern,
        fixed_strings=bool(arguments.get("fixed_strings", False)),
        case_sensitive=bool(arguments.get("case_sensitive", True)),
    )
    validation = _validate_ripgrep_regex(
        rg,
        common_args,
        fixed_strings=bool(arguments.get("fixed_strings", False)),
        timeout_seconds=context.effective_timeout_seconds,
    )
    if validation is not None:
        return validation
    try:
        glob = PortableGlob(arguments.get("glob", "**"), case_sensitive=True)
    except ValueError as exc:
        return NativeHandlerResult(
            status="error",
            error_message=str(exc),
            reason="tool_schema_invalid",
        )
    type_globs = _search_type_globs(arguments.get("type"))
    candidates = _iter_search_text_candidates(
        context,
        root,
        include_hidden=bool(arguments.get("include_hidden", False)),
        glob=glob,
        type_globs=type_globs,
        skipped=skipped,
    )

    if output_mode == "content":
        pager = _SearchContentPager(offset=offset, limit=limit)
        for candidate in candidates:
            seen_lines: set[int] = set()
            snapshot = pager.snapshot()
            try:
                for item in _iter_ripgrep_matches(
                    rg, common_args, candidate, context.effective_timeout_seconds
                ):
                    skip_counter = item.get("_skip_counter")
                    if skip_counter in skipped:
                        skipped[skip_counter] += 1
                        continue
                    line_number = item["line_number"]
                    if line_number in seen_lines:
                        continue
                    seen_lines.add(line_number)
                    pager.add(item)
            except _RipgrepCandidateSkipped as exc:
                pager.restore(snapshot)
                skipped[exc.counter] += 1
                continue
            except _RipgrepExecutionError as exc:
                return NativeHandlerResult(status="error", error_message=str(exc))
        context_result = _attach_search_context(
            pager.page, before=before_context, after=after_context
        )
        if context_result.status == "error":
            return context_result
        output = _search_common_output(
            root=root,
            pattern=pattern,
            output_mode=output_mode,
            offset=offset,
            limit=limit,
            total_returned=len(pager.page),
            truncated=pager.truncated,
            next_offset=pager.next_offset,
            skipped=skipped,
        )
        output["matches"] = context_result.output
        return NativeHandlerResult(status="ok", output=output)

    if output_mode == "files_with_matches":
        pager = _SearchPageCollector(offset=offset, limit=limit)
        for candidate in candidates:
            seen_lines: set[int] = set()
            matched = False
            try:
                for item in _iter_ripgrep_matches(
                    rg, common_args, candidate, context.effective_timeout_seconds
                ):
                    skip_counter = item.get("_skip_counter")
                    if skip_counter in skipped:
                        skipped[skip_counter] += 1
                        continue
                    line_number = item["line_number"]
                    if line_number in seen_lines:
                        continue
                    seen_lines.add(line_number)
                    matched = True
            except _RipgrepCandidateSkipped as exc:
                skipped[exc.counter] += 1
                continue
            except _RipgrepExecutionError as exc:
                return NativeHandlerResult(status="error", error_message=str(exc))
            if matched:
                pager.add(str(candidate))
        output = _search_common_output(
            root=root,
            pattern=pattern,
            output_mode=output_mode,
            offset=offset,
            limit=limit,
            total_returned=len(pager.page),
            truncated=pager.truncated,
            next_offset=pager.next_offset,
            skipped=skipped,
        )
        output["paths"] = pager.page
        return NativeHandlerResult(status="ok", output=output)
    if output_mode == "count":
        pager = _SearchPageCollector(offset=offset, limit=limit)
        for candidate in candidates:
            seen_lines: set[int] = set()
            count = 0
            try:
                for item in _iter_ripgrep_matches(
                    rg, common_args, candidate, context.effective_timeout_seconds
                ):
                    skip_counter = item.get("_skip_counter")
                    if skip_counter in skipped:
                        skipped[skip_counter] += 1
                        continue
                    line_number = item["line_number"]
                    if line_number in seen_lines:
                        continue
                    seen_lines.add(line_number)
                    count += 1
            except _RipgrepCandidateSkipped as exc:
                skipped[exc.counter] += 1
                continue
            except _RipgrepExecutionError as exc:
                return NativeHandlerResult(status="error", error_message=str(exc))
            if count > 0:
                pager.add({"path": str(candidate), "count": count})
        output = _search_common_output(
            root=root,
            pattern=pattern,
            output_mode=output_mode,
            offset=offset,
            limit=limit,
            total_returned=len(pager.page),
            truncated=pager.truncated,
            next_offset=pager.next_offset,
            skipped=skipped,
        )
        output["counts"] = pager.page
        return NativeHandlerResult(status="ok", output=output)

    return NativeHandlerResult(status="error", error_message="Unsupported output_mode.")


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


def _search_common_output(
    *,
    root: Path,
    pattern: str,
    output_mode: str,
    offset: int,
    limit: int,
    total_returned: int,
    truncated: bool,
    next_offset: int | None,
    skipped: dict[str, int],
) -> dict[str, Any]:
    return {
        "root": str(root.resolve()),
        "pattern": pattern,
        "output_mode": output_mode,
        "offset": offset,
        "maxResults": limit,
        "total_returned": total_returned,
        "truncated": truncated,
        "next_offset": next_offset,
        "skipped_files": dict(skipped),
    }


def _iter_search_text_candidates(
    context: NativeToolContext,
    root: Path,
    *,
    include_hidden: bool,
    glob: PortableGlob,
    type_globs: tuple[PortableGlob, ...] | None,
    skipped: dict[str, int],
):
    for candidate in _iter_search_candidate_files(
        context, root, include_hidden=include_hidden, skipped=skipped
    ):
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            relative = candidate.name
        if relative == ".":
            relative = candidate.name
        if not glob.matches(relative):
            continue
        if type_globs is not None and not any(type_glob.matches(relative) for type_glob in type_globs):
            continue
        try:
            _prescreen_utf8(candidate)
        except UnicodeDecodeError:
            skipped["decode_error"] += 1
            continue
        except OSError:
            skipped["other"] += 1
            continue
        yield candidate


def _iter_search_candidate_files(
    context: NativeToolContext,
    root: Path,
    *,
    include_hidden: bool,
    skipped: dict[str, int],
):
    if root.is_file() or root.is_symlink():
        allowed = _search_candidate_allowed(
            context, root, root, include_hidden=include_hidden, skipped=skipped
        )
        if allowed:
            yield _normalized_candidate_path(root)
        return
    yield from _iter_search_candidate_directory(
        context, root, root, include_hidden=include_hidden, skipped=skipped
    )


def _iter_search_candidate_directory(
    context: NativeToolContext,
    current_path: Path,
    root: Path,
    *,
    include_hidden: bool,
    skipped: dict[str, int],
):
    try:
        entries = sorted(
            os.scandir(current_path),
            key=lambda entry: str(Path(os.path.normpath(Path(entry.path).absolute()))),
        )
    except OSError:
        skipped["other"] += 1
        return
    for entry in entries:
        child = Path(entry.path)
        try:
            relative = child.relative_to(root)
        except ValueError:
            skipped["other"] += 1
            continue
        if child.is_dir() and not child.is_symlink():
            if _is_denied(context, child):
                continue
            if not include_hidden and _is_hidden_relative(relative):
                continue
            yield from _iter_search_candidate_directory(
                context,
                child,
                root,
                include_hidden=include_hidden,
                skipped=skipped,
            )
            continue
        allowed = _search_candidate_allowed(
            context, child, root, include_hidden=include_hidden, skipped=skipped
        )
        if allowed:
            yield _normalized_candidate_path(child)


def _search_candidate_allowed(
    context: NativeToolContext,
    candidate: Path,
    root: Path,
    *,
    include_hidden: bool,
    skipped: dict[str, int],
) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        skipped["other"] += 1
        return False
    if _is_denied(context, candidate):
        skipped["denied"] += 1
        return False
    if not include_hidden and _is_hidden_relative(relative):
        skipped["hidden"] += 1
        return False
    if candidate.is_dir() and not candidate.is_symlink():
        return False
    if not candidate.is_file() and not candidate.is_symlink():
        skipped["other"] += 1
        return False
    if candidate.is_symlink():
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            skipped["other"] += 1
            return False
        if context.permission_evaluator.classify_path(resolved).classification == "denied":
            skipped["other"] += 1
            return False
        if not resolved.is_file():
            skipped["other"] += 1
            return False
    return True


def _prescreen_utf8(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)


def _search_type_globs(type_name: str | None) -> tuple[PortableGlob, ...] | None:
    if not type_name:
        return None
    if not is_search_text_type_allowed(type_name):
        raise ValueError(f"Unsupported search_text type: {type_name}.")
    patterns = SEARCH_TEXT_TYPES[type_name]
    return tuple(PortableGlob(pattern, case_sensitive=False) for pattern in patterns)


def _ripgrep_common_args(
    pattern: str, *, fixed_strings: bool, case_sensitive: bool
) -> list[str]:
    args = ["--json", "--no-config"]
    if fixed_strings:
        args.append("-F")
    if not case_sensitive:
        args.append("-i")
    args.extend(["--regexp", pattern])
    return args


def _ripgrep_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


def _run_rg(argv: list[str], *, timeout_seconds: float) -> Any:
    try:
        return subprocess.run(
            argv,
            cwd="/",
            env=_ripgrep_env(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeoutError from exc


def _validate_ripgrep_regex(
    rg: str,
    common_args: list[str],
    *,
    fixed_strings: bool,
    timeout_seconds: float,
) -> NativeHandlerResult | None:
    version = _run_rg([rg, "--no-config", "--version"], timeout_seconds=timeout_seconds)
    if version.returncode != 0:
        return NativeHandlerResult(
            status="error",
            error_message=_rg_failure_message(version.stderr or version.stdout),
        )
    if fixed_strings:
        return None
    with tempfile.TemporaryDirectory(prefix="debug-agent-rg-") as directory:
        empty_file = Path(directory) / "regex-check.txt"
        empty_file.write_text("", encoding="utf-8")
        result = _run_rg(
            [rg, *common_args, "--", str(empty_file)],
            timeout_seconds=timeout_seconds,
        )
    if result.returncode in (0, 1):
        return None
    return NativeHandlerResult(
        status="error",
        error_message=_rg_failure_message(result.stderr or result.stdout),
    )


def _iter_ripgrep_matches(
    rg: str, common_args: list[str], candidate: Path, timeout_seconds: float
) -> Iterator[dict[str, Any]]:
    candidate_identity = str(candidate)
    process = subprocess.Popen(
        [rg, *common_args, "--", candidate_identity],
        cwd="/",
        env=_ripgrep_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        assert process.stdout is not None
        for line in _iter_bounded_ripgrep_stdout_records(process.stdout):
            item = _parse_ripgrep_match_line(line, candidate_identity)
            if item is not None:
                yield item
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise ToolTimeoutError from exc
        stderr = _read_bounded_process_stderr(process)
    finally:
        if process.poll() is None:
            process.kill()
    if returncode == 1:
        return
    if returncode != 0:
        raise _RipgrepExecutionError(_rg_failure_message(stderr))


def _iter_bounded_ripgrep_stdout_records(stream: Any) -> Iterator[bytes]:
    buffer = bytearray()
    while True:
        chunk = stream.read(SEARCH_TEXT_RG_STDOUT_CHUNK_BYTES)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        buffer.extend(chunk)
        if len(buffer) > SEARCH_TEXT_RG_JSON_RECORD_BYTES:
            raise _RipgrepCandidateSkipped("other")
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            record = bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
            if len(record) > SEARCH_TEXT_RG_JSON_RECORD_BYTES:
                raise _RipgrepCandidateSkipped("other")
            yield record
    if buffer:
        if len(buffer) > SEARCH_TEXT_RG_JSON_RECORD_BYTES:
            raise _RipgrepCandidateSkipped("other")
        yield bytes(buffer)


def _parse_ripgrep_match_line(
    line: bytes | str, candidate_identity: str
) -> dict[str, Any] | None:
    if not line:
        return None
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return {"_skip_counter": "decode_error"}
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return {"_skip_counter": "other"}
    if record.get("type") != "match":
        return None
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    line_number = data.get("line_number")
    try:
        text = _rg_text_field(data.get("lines"))
    except _RipgrepRecordSkipped as exc:
        return {"_skip_counter": exc.counter}
    if text is None:
        return None
    if len(text.encode("utf-8")) > SEARCH_TEXT_RG_JSON_RECORD_BYTES:
        raise _RipgrepCandidateSkipped("other")
    if not isinstance(line_number, int):
        return None
    preview, truncated = _line_preview(text.rstrip("\r\n"))
    return {
        "path": candidate_identity,
        "line_number": line_number,
        "line": preview,
        "is_context": False,
        "line_truncated": truncated,
    }


def _read_bounded_process_stderr(process: Any) -> str:
    stream = process.stderr
    if stream is None:
        return ""
    try:
        data = stream.read(SEARCH_TEXT_STDERR_PREVIEW_CODEPOINTS + 1)
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return data[:SEARCH_TEXT_STDERR_PREVIEW_CODEPOINTS]
    except Exception:
        return ""


def _rg_text_field(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    if isinstance(text, str):
        return text
    raw_bytes = value.get("bytes")
    if isinstance(raw_bytes, list):
        try:
            return bytes(raw_bytes).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise _RipgrepRecordSkipped("decode_error")
    return None


def _rg_failure_message(diagnostic: str) -> str:
    short = " ".join((diagnostic or "unknown rg failure").split())
    return f"rg execution failure: {short}"


def _line_preview(line: str) -> tuple[str, bool]:
    if len(line) <= SEARCH_TEXT_LINE_PREVIEW_CODEPOINTS:
        return line, False
    return line[:SEARCH_TEXT_LINE_PREVIEW_CODEPOINTS], True


def _attach_search_context(
    matches: list[dict[str, Any]], *, before: int, after: int
) -> NativeHandlerResult:
    if not matches or (before == 0 and after == 0):
        return NativeHandlerResult(status="ok", output=matches)
    by_path: dict[str, set[int]] = {}
    match_lines: dict[tuple[str, int], dict[str, Any]] = {}
    for match in matches:
        path = match["path"]
        line_number = int(match["line_number"])
        match_lines[(path, line_number)] = match
        lines = by_path.setdefault(path, set())
        start = max(1, line_number - before)
        end = line_number + after
        lines.update(range(start, end + 1))
    rows: list[dict[str, Any]] = []
    for path, requested_lines in sorted(by_path.items()):
        try:
            text_lines = _read_context_lines(Path(path), requested_lines)
        except (OSError, UnicodeDecodeError) as exc:
            return NativeHandlerResult(
                status="error",
                error_message=f"Unable to attach search context: {exc}",
            )
        for line_number in sorted(requested_lines):
            key = (path, line_number)
            if key in match_lines:
                rows.append(match_lines[key])
                continue
            if line_number in text_lines:
                preview, truncated = _line_preview(text_lines[line_number])
                rows.append(
                    {
                        "path": path,
                        "line_number": line_number,
                        "line": preview,
                        "is_context": True,
                        "line_truncated": truncated,
                    }
                )
    return NativeHandlerResult(
        status="ok",
        output=sorted(rows, key=lambda item: (item["path"], item["line_number"], item["is_context"])),
    )


def _read_context_lines(path: Path, requested_lines: set[int]) -> dict[int, str]:
    if not requested_lines:
        return {}
    wanted = set(requested_lines)
    max_line = max(wanted)
    found: dict[int, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number in wanted:
                found[line_number] = line.rstrip("\r\n")
                if len(found) == len(wanted):
                    break
            if line_number >= max_line:
                break
    return found


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

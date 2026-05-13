from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from debug_agent.runtime.contracts import ToolDefinition


ToolHandler = Callable[[Path, dict[str, Any]], str | dict[str, Any]]
SEARCH_DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".sessions",
        ".git",
        "node_modules",
        "build",
        "dist",
        ".venv",
        "__pycache__",
        ".pytest_cache",
    }
)


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file under the workspace root.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path",
                    }
                },
                "required": ["path"],
            },
        ),
        ToolDefinition(
            name="list_dir",
            description="List immediate directory entries under the workspace root.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory path",
                    }
                },
                "required": ["path"],
            },
        ),
        ToolDefinition(
            name="search_text",
            description="Search UTF-8 text files under the workspace root.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search for"},
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory path",
                    },
                },
                "required": ["query"],
            },
        ),
        ToolDefinition(
            name="git_status",
            description="Return git status for the workspace root.",
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
    ]


def tool_handlers() -> dict[str, ToolHandler]:
    return {
        "read_file": read_file,
        "list_dir": list_dir,
        "search_text": search_text,
        "git_status": git_status,
    }


def read_file(workspace_root: Path, arguments: dict[str, Any]) -> str:
    return (workspace_root / arguments["path"]).read_text(encoding="utf-8")


def list_dir(workspace_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    target = workspace_root / arguments["path"]
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda path: path.name):
        entries.append(
            {
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
            }
        )
    return {"entries": entries}


def search_text(workspace_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments["query"]
    start = workspace_root / arguments.get("path", ".")
    matches: list[dict[str, Any]] = []
    explicit_path = "path" in arguments and arguments["path"] not in {"", "."}
    for path in _iter_search_files(workspace_root, start, explicit_path=explicit_path):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle, start=1):
                    text = line.rstrip("\n")
                    if query in text:
                        matches.append(
                            {
                                "path": path.relative_to(workspace_root).as_posix(),
                                "line": index,
                                "text": text,
                            }
                        )
        except UnicodeDecodeError:
            continue
    return {"matches": matches}


def _iter_search_files(
    workspace_root: Path,
    start: Path,
    *,
    explicit_path: bool,
):
    if start.is_file():
        yield start
        return

    start = start.resolve()
    for root, dirnames, filenames in os.walk(start):
        root_path = Path(root)
        if not explicit_path and _is_excluded_search_dir(workspace_root, root_path):
            dirnames[:] = []
            continue
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in SEARCH_DEFAULT_EXCLUDED_DIRS
        ]
        for filename in filenames:
            yield root_path / filename


def _is_excluded_search_dir(workspace_root: Path, path: Path) -> bool:
    relative_parts = path.relative_to(workspace_root).parts
    return any(part in SEARCH_DEFAULT_EXCLUDED_DIRS for part in relative_parts)


def git_status(workspace_root: Path, _arguments: dict[str, Any]) -> str:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "git status failed"
        raise RuntimeError(message)
    return result.stdout

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from debug_agent.runtime.contracts import ToolDefinition


ToolHandler = Callable[[Path, dict[str, Any]], str | dict[str, Any]]


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
    files = [start] if start.is_file() else sorted(path for path in start.rglob("*"))
    for path in files:
        if not path.is_file() or ".sessions" in path.relative_to(workspace_root).parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(lines, start=1):
            if query in line:
                matches.append(
                    {
                        "path": path.relative_to(workspace_root).as_posix(),
                        "line": index,
                        "text": line,
                    }
                )
    return {"matches": matches}


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

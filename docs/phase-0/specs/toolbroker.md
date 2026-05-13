# Phase 0 ToolBroker Specification

## Purpose

ToolBroker is the only runtime-approved tool boundary. Phase 0 uses it to prove native read-only tools are policy checked, audited, and returned through standard `ToolResult`.

## Phase 0 Native Read-Only Tools

Phase 0 supports exactly these tools:

- `read_file`: read a UTF-8 text file under `workspace_root`.
- `list_dir`: list immediate directory entries under `workspace_root`.
- `search_text`: search text under `workspace_root` using the local search implementation.
- `git_status`: return repository status for `workspace_root`.

No writable native tool is part of Phase 0. Writable native tools begin in Phase 1 after path policy and approval grants exist.

`search_text` default workspace searches skip common large generated or dependency
directories: `.sessions`, `.git`, `node_modules`, `build`, `dist`, `.venv`,
`__pycache__`, and `.pytest_cache`. If the user explicitly supplies one of those
directories as the `path` argument, Phase 0 searches that requested subtree.
`search_text` streams files line by line and skips files that cannot be decoded
as UTF-8.

## Tool Definition Schema

Runtime exposes tools to adapters through a framework-neutral schema:

```python
ToolDefinition(
    name="read_file",
    description="Read a UTF-8 text file under the workspace root.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"}
        },
        "required": ["path"],
    },
)
```

The supported JSON Schema subset is `type`, `properties`, `required`, `description`, `enum`, scalar types, arrays, and objects. LangChain-specific tool objects are adapter output, not runtime contract.

## Invocation

```python
class ToolBroker:
    def invoke(
        self,
        session_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict,
        context: dict,
    ) -> ToolResult: ...
```

Every invocation must:

1. Validate `tool_name`.
2. Validate arguments.
3. Validate path stays inside `workspace_root`.
4. Apply allow/deny policy.
5. Execute the tool with timeout.
6. Normalize result to `ToolResult`.
7. Write audit event.

## Allow And Deny Rules

- Unknown tool returns `denied`.
- Any write intent returns `denied`.
- Any path outside `workspace_root` returns `denied`.
- Symlink traversal outside `workspace_root` returns `denied`.
- `git_status` may only run status-equivalent read operation.

## ToolResult

```python
ToolResult(
    status="ok" | "error" | "denied" | "timeout" | "cancelled",
    output=str_or_dict_or_none,
    error=error_dict_or_none,
    artifacts=list_of_artifact_ids,
    metadata=dict,
    redacted_output=str_or_none,
)
```

Outputs larger than 16 KiB must be written as artifacts and summarized in `redacted_output`.

## Audit Events

ToolBroker writes:

- `tool_call_started`
- `tool_call_completed`
- `tool_call_denied`
- `tool_call_failed`

Audit payload includes:

- `tool_name`
- normalized arguments
- status
- duration
- artifact ids
- error class if any

## Timeout

Each tool has a default timeout. Phase 0 default is 30 seconds unless overridden by runtime config snapshot.

Timeout returns `ToolResult(status="timeout")` and writes `tool_call_failed` with error class `timeout`.

## Yolo Mode

`yolo` does not bypass ToolBroker. In Phase 0, yolo only means one-shot does not ask for interactive approval for read-only tools. Policy, path validation, timeout, and audit still apply.

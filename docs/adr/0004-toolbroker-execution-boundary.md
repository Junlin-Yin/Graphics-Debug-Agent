# ADR 0004: ToolBroker As Mandatory Execution Boundary

## Status

Accepted after Phase 0 implementation.

## Context

`debug-agent` operates on real local workspaces. Even in Phase 0, read-only
tools need path validation, timeout handling, audit events, artifact handling,
and standardized results.

Future phases add higher-risk execution surfaces: writable native tools, shell
and git commands, subagent tools, workflow step tools, and optional MCP tools.
If these surfaces each implement their own policy and audit behavior, runtime
safety becomes fragmented and hard to review.

## Decision

All tool behavior must pass through `ToolBroker`.

`ToolBroker` owns:

- tool allow/deny decisions
- argument validation
- workspace path validation
- timeout handling
- standardized `ToolResult` creation
- tool audit events
- large output artifact registration

Adapters may expose framework-specific tool callables, but those callables must
delegate execution to `ToolBroker`. Native tool handlers must not write audit
events directly, and model frameworks must not bypass runtime policy.

Approval modes can affect whether user confirmation is needed, but they cannot
bypass `ToolBroker`. In particular, `yolo` mode still uses the same policy,
path validation, timeout, and audit boundary.

## Alternatives Considered

### Expose native tools directly to the agent framework

This is simpler for Phase 0, but it lets framework tool callables become the
effective safety boundary. That would couple runtime policy to LangChain and
make later MCP or subagent integration unsafe.

### Let each executor enforce its own policy

Prompt agent, subagent, workflow, shell, git, and MCP executors could each check
their own permissions. This keeps local code small, but produces duplicated
policy logic and inconsistent audit behavior.

### Use system prompts as the safety boundary

Prompt instructions are useful context, but they are not enforceable runtime
policy. They cannot protect the workspace from malformed tool calls, model
mistakes, or future external tool integrations.

## Consequences

- Every new tool surface must integrate through `ToolBroker`.
- Tool tests can focus on one policy and audit boundary.
- Runtime traces can rely on consistent tool audit event shapes.
- Adapters remain replaceable because framework-specific tool objects are not
  runtime contracts.
- `ToolBroker` becomes a critical module and must stay small, explicit, and
  heavily tested.

# ADR 0001: LangChain As Adapter

## Status

Accepted for Phase 0.

## Context

`debug-agent` needs an agent loop quickly, but its core value is reliable local runtime behavior: session/run lifecycle, ToolBroker safety, checkpoint recovery, artifacts, trace, and later workflow execution.

If LangChain owns runtime state, future changes to safety, checkpoint, workflow, or provider strategy become coupled to LangChain internals.

## Decision

Use LangChain only behind `AgentLoopAdapter`.

Runtime Core owns:

- `Session`
- `Run`
- `RunEvent`
- `Checkpoint`
- `Artifact`
- `ToolResult`
- ToolBroker policy
- trace generation

LangChain owns only the immediate model interaction inside adapter calls.

## Alternatives Considered

### LangChain-first runtime

Initial implementation would be faster, but session truth, checkpoints, tools, and workflow would be constrained by external framework semantics.

### Fully custom agent loop in Phase 0

This gives maximum control but slows the minimal runtime slice. Phase 0 needs to prove runtime persistence and CLI behavior first.

### Provider-specific executors

This avoids a framework dependency but fragments tool and event behavior across providers.

## Consequences

- Switching from LangChain to another framework later requires a new adapter, not a runtime rewrite.
- Phase 0 must define adapter request/result contracts early.
- Some LangChain conveniences may be intentionally ignored if they conflict with runtime ownership.


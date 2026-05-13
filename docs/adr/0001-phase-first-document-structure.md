# ADR 0001: Phase-First Document Structure

## Status

Accepted before Phase 0 implementation.

## Context

The project has a broad long-term roadmap: skills, tools, subagents, workflow, shader-debug readiness, MCP, and plugin packaging. Starting implementation directly from a single large spec risks mixing future-phase features into Phase 0.

Phase 0 needs tight documents that answer what to build now, what not to build, and how to prove completion.

## Decision

Organize development documentation by phase:

```text
docs/
  project-contract.md
  phase-0/
    scope.md
    architecture.md
    implementation-plan.md
    specs/
    tests.md
  adr/
```

`project-contract.md` defines project-level constraints. `phase-0/*` defines the immediate coding contract. `adr/*` records durable architecture decisions and alternatives.

## Alternatives Considered

### Single large spec

Keeps everything in one place, but makes it easy for implementation agents to pull Phase 1+ features into Phase 0.

### Module-first docs

Useful after the codebase grows, but early implementation is driven by vertical runtime slices rather than mature module ownership.

### ADR-only docs

Captures rationale, but does not provide enough coding guidance or acceptance criteria.

## Consequences

- Implementation agents can use `docs/phase-0/` as the immediate scope boundary.
- Future phases can add their own scope, architecture, specs, and tests without rewriting Phase 0 docs.
- ADRs remain cross-phase and stable.
- Root historical docs can be retired or treated as source input after the phase docs become authoritative.

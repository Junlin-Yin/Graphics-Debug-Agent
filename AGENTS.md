# Agent Instructions

## Authority

For implementation work, `docs/` is the authoritative source for product behavior, architecture, phase scope, and technical contracts.

The active phase is determined by the human instruction or current task. If the active phase is unclear, stop and ask. The active phase directory under `docs/` is the daily coding contract.

Use these documents this way:

1. `docs/<active-phase>/` is the daily coding contract.
2. `docs/project-contract.md` provides top-level constraints and arbitrates true conflicts with project-level law.
3. `docs/adr/` records durable architecture decisions.

`docs/project-plan.md` is historical planning context only. Do not use it to expand active phase scope.

Specific phase documents may narrow or detail higher-level roadmap language. Treat more specific active-phase contracts as controlling implementation details unless they conflict with a top-level project constraint. If phase docs, project contract, and ADRs appear to conflict, stop and ask for contract clarification.

Accepted ADRs override earlier architectural descriptions for the decision they cover.

## Scope Discipline

Implement only what is required by the current task, active phase docs, and active milestone.

Do not add architecture, commands, tools, runtime states, provider abstractions, registries, workflows, subagents, MCP, plugins, writable tools, or convenience features unless explicitly required by the active phase docs.

Follow the active phase `implementation-plan.md` milestone order. Do not skip ahead until the current milestone reaches its stated Runnable state.

Code for future phases must not appear in the active phase unless explicitly required. Avoid dormant code paths, unused abstractions, placeholder registries, feature flags, and inactive future-phase scaffolding.

Every milestone must leave the repository in a runnable and testable state. Do not land architectural scaffolding without the corresponding executable behavior required by the milestone.

## Task And Diff Discipline

Implement only the behavior required for the current task and milestone.

Do not expand implementation scope because adjacent functionality appears related or convenient.

Unrelated cleanup, optimization, abstraction, or restructuring is a separate task unless explicitly requested.

Prefer the smallest coherent diff that satisfies the contract.

Avoid broad rewrites, mass renames, mass formatting changes, and directory reorganizations unless explicitly required.

Minimize unrelated refactors when modifying a module. Do not rewrite stable modules for stylistic consistency.

## Contract-First Development

Repository documents define intended behavior. Existing code does not redefine the contract.

If implementation and documentation diverge:

1. Treat documentation as authoritative.
2. Report the divergence explicitly.
3. Do not silently change behavior to match existing code.
4. Do not edit the contract to match implementation drift without human approval.

## No Guessing

Do not extend architecture based on assumptions.

If a contract is missing, conflicting, ambiguous, or if a feature, optimization, or convenience behavior seems useful but is not explicitly required:

1. Stop coding.
2. Describe the gap.
3. Explain why it blocks implementation.
4. Propose the smallest contract patch needed.
5. Wait for human approval before continuing.

Human approval is required for contract changes, architecture changes, phase scope expansion, new persistence semantics, state machine changes, and new tool risk categories.

## Implementation Rules

Prefer the smallest implementation that satisfies the documented contract.

Do not introduce speculative extensibility. Avoid abstractions, interfaces, extension points, hooks, registries, strategy layers, generic base classes, or configuration systems for hypothetical future phases unless explicitly required.

Prefer concrete implementations over premature generalization.

Temporary debugging code, commented-out logic, compatibility shims, migration paths, and transitional behavior must not remain unless explicitly required by the active phase contract.

Module organization should follow the active phase architecture document. Keep module responsibilities separated.

## Runtime Integrity

Preserve ownership boundaries between runtime orchestration, persistence, tool execution, workflow execution, agent loop integration, observability, and configuration.

Do not collapse boundaries for convenience.

Keep runtime truth in structured state, not natural language summaries.

All persistence must go through the documented Store abstractions. Do not bypass stores to write SQLite or session files directly.

Runtime persistence is append-oriented and audit-oriented. Do not mutate or overwrite historical runtime records unless explicitly required by the contract.

State transitions must follow the active phase runtime contracts. Do not invent new states or transitions.

Use documented error classes. Do not invent new error classes.

Do not bypass `ToolBroker` for any tool behavior.

Do not make LangChain own session, run, checkpoint, artifact, trace, or ToolBroker policy state.

## Observability And Failure

Do not reduce observability for convenience.

New execution paths, persistence behavior, interrupts, failures, approvals, and state transitions must remain observable through logs, events, checkpoints, or traces as required by the contract.

Failures must be explicit and classified.

Do not silently swallow exceptions, invent fallback behavior, or continue execution after contract violations unless explicitly required by the specification.

## Verification

Before claiming completion, run the relevant tests or checks from the active phase test plan.

Operational commands must come from the active phase `operations.md`. If no canonical command exists, discover candidates from repository files, report evidence, and wait for human approval before treating a command as standard.

If verification cannot be run, state exactly what was not verified and why.

Acceptance criteria are defined by the active phase `tests.md` and `scope.md`.

Do not mark placeholders, fake implementations, unimplemented branches, or unverified paths as completed functionality.

## Exception Handling

If strict adherence to the current contract would cause unreasonable implementation complexity, architectural damage, or blocked progress:

1. Stop implementation.
2. Describe the constraint.
3. Explain why the current contract is insufficient.
4. Propose the minimal exception or contract patch.
5. Wait for human approval.

Do not silently violate repository rules for convenience.

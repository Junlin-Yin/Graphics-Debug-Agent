# Agent Instructions

## Authority

For implementation work, `docs/` is the source of truth for product behavior, architecture, phase scope, and technical contracts.

If the active phase is unclear, stop and ask.

Source order:

1. `docs/project-contract.md`
2. `docs/<active-phase>/`
3. accepted `docs/adr/`

`docs/project-plan.md` is historical context only. Do not use it to expand active phase scope.

Active phase docs refine and narrow the project contract for the active phase. They must not expand or contradict the project contract.

If active phase docs, project contract, and ADRs conflict, stop and ask for clarification. Do not silently resolve conflicts by priority order.

## Scope And Diff Discipline

Implement only what is required by the current task, active phase docs, active milestone, and documented `implementation-plan.md` order.

Do not add future-phase behavior, dormant scaffolding, convenience features, unrelated cleanup, broad refactors, mass renames, or formatting churn unless explicitly required.

Prefer the smallest coherent diff that satisfies the contract.

Every milestone must remain runnable and testable; do not land scaffolding without the executable behavior required by the milestone.

## Contract Discipline

Documentation defines intended behavior. Existing code does not redefine the contract.

If implementation and documentation diverge, treat documentation as authoritative, report the divergence, and do not edit the contract to match implementation drift without human approval.

Do not extend architecture or behavior from assumptions. If a required contract is missing, ambiguous, conflicting, or insufficient, stop, describe the gap, propose the smallest contract patch or exception, and wait for approval.

Human approval is required for contract changes, architecture changes, phase scope expansion, new persistence semantics, state machine changes, and new tool risk categories.

## Implementation Rules

Prefer concrete, minimal implementations that follow documented module responsibilities.

Do not introduce speculative extensibility, unused abstractions, placeholder paths, compatibility shims, commented-out logic, or temporary debugging code unless explicitly required.

Runtime behavior, observability, and failure handling must follow the project contract, active phase docs, and accepted ADRs. Do not bypass, weaken, or invent documented contracts for implementation convenience.

## Verification

Before claiming completion, run the relevant checks from the active phase test plan.

Operational commands must come from the active phase `operations.md`. If no canonical command exists, discover candidates from repository files, report evidence, and wait for human approval before treating a command as standard.

If verification cannot be run, state exactly what was not verified and why.

Acceptance criteria are defined by the active phase `tests.md` and `scope.md`. Do not mark placeholders, fake implementations, unimplemented branches, or unverified paths as completed functionality.

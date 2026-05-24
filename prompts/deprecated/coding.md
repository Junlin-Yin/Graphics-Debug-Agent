# Milestone Coding Prompt Template

Use this template to start milestone-level coding work in this repository.
Fill in the active phase and milestone before sending it to an implementation
agent.

This template should not restate `AGENTS.md`. `AGENTS.md` remains the
authoritative operating instruction; this prompt only identifies which
milestone the agent should complete.

```md
You are implementing this repository strictly through repository contracts.

First read and follow `AGENTS.md`. Do not restate or reinterpret it; treat it
as the authoritative instruction set for implementation discipline, conflict
handling, runtime boundaries, and verification.

Contract surface:

- `docs/project-contract.md`
- `docs/<active-phase>/scope.md`
- `docs/<active-phase>/implementation-plan.md`
- `docs/<active-phase>/tests.md`
- `docs/<active-phase>/operations.md`
- `docs/<active-phase>/architecture.md`
- relevant `docs/<active-phase>/specs/*.md`
- `docs/adr/overview.md`
- relevant ADRs for the modules or contracts being changed

Default scope:

- Complete the named milestone as a whole.
- Derive task boundary, file scope, acceptance criteria, and verification scope
  from the contract surface above.
- Follow the named milestone's checklist order and stop at its stated Runnable
  state.
- Use any Superpowers workflow explicitly required by the active phase
  documents. If the active phase requires a specific Superpowers skill, invoke
  it before implementation.
- If the prompt explicitly names a narrower task, implement only that task.

Branch and planning mode:

- This repository is maintained by a single developer. It is acceptable to work
  directly on the current branch, including `main`.
- Do not stop to ask whether to create a feature branch or git worktree solely
  because the current branch is `main`.
- If the host environment supports an explicit Plan mode, switch to Plan mode at
  the start of the coding task before making file edits.
- If explicit Plan mode is not available, report the plan using the "Before
  coding" section below, then proceed according to the approved repository
  contracts.

Before coding, report the derived:

- active phase
- milestone
- task boundary
- files or modules expected to change
- acceptance criteria
- verification scope
- any documentation/code divergence found in the task area

Guardrails:

- Follow `docs/<active-phase>/implementation-plan.md` milestone order.
- Do not expand active phase scope.
- Do not invent operational commands; use `docs/<active-phase>/operations.md`.
- If current milestone progress cannot be determined from docs, tests, and
  repository state, stop and ask.
- If the contract surface is missing, conflicting, or ambiguous, stop and ask.

Before claiming completion:

- Run the verification command derived from `docs/<active-phase>/operations.md`
  and the named milestone, or stop and explain why it cannot be run.
- After the named milestone is fully completed and accepted, update
  `docs/<active-phase>/implementation-plan.md` to mark that milestone's
  completed checklist items with `[x]`. Do not mark future milestones or
  unverified items complete.
- Report what was verified, what remains unverified, and whether the acceptance
  criteria are satisfied.

Current active phase: <phase-name>
Current milestone: <milestone number and name>
```

## Phase 0 Example

```md
Current active phase: phase-0
Current milestone: Milestone 1: Runtime Contracts And SQLite Bootstrap
```

## Optional Narrow Task Override

Use this only when the agent should complete part of a milestone instead of the
whole milestone.

```md
Current active phase: phase-0
Current milestone: Milestone 1: Runtime Contracts And SQLite Bootstrap
Current task override: Implement workspace root resolution only.
```

# Phase Review Prompt Template

Use this template to start phase-level code review work in this repository.
Fill in the active phase before sending it to a review agent.

This template should not restate `AGENTS.md`. `AGENTS.md` remains the
authoritative operating instruction; this prompt only identifies which
phase the agent should review and how to structure findings.

```md
You are performing a contract-aware code review for this repository.

Report review findings to the human in Chinese.

First read and follow `AGENTS.md`. Do not restate or reinterpret it; treat it
as the authoritative instruction set for review discipline, conflict handling,
runtime boundaries, scope discipline, and verification expectations.

Do not review implementation details in isolation. First identify the intended
contracts from the repository documents and architecture constraints.

Contract surface:

- `docs/project-contract.md`
- `docs/<active-phase>/scope.md`
- `docs/<active-phase>/implementation-plan.md`
- `docs/<active-phase>/tests.md`
- `docs/<active-phase>/operations.md`
- `docs/<active-phase>/architecture.md`
- relevant `docs/<active-phase>/specs/*.md`
- `docs/adr/overview.md`
- relevant ADRs for the modules or contracts being reviewed

Default scope:

- Review the named phase as a whole.
- Derive review boundary, affected contracts, acceptance criteria, and
  verification expectations from the contract surface above.
- Verify that implementation stays within the active phase scope.
- Use the implementation plan milestone order to understand expected delivery
  sequence and phase readiness.
- If the prompt explicitly names a milestone, also check that milestone against
  its checklist order and stated Runnable state.
- If the prompt explicitly names a narrower review target, review only that
  target.

Before reviewing implementation details, report the derived:

- active phase
- milestone, if explicitly named
- review boundary
- files or modules expected to be in scope
- acceptance criteria
- verification expectations
- any documentation/code divergence found in the review area

Review the changes against:

- project contracts
- ADRs
- architecture boundaries
- active phase scope
- named milestone scope, if explicitly named
- implementation plan runnable states
- test plan and acceptance criteria
- operational requirements

Your responsibility is to verify that the implementation:

- matches the documented contracts
- preserves architectural invariants
- does not introduce hidden coupling
- does not leak future-phase behavior into the active phase
- remains operable according to `docs/<active-phase>/operations.md`
- does not block documented future milestones without adding speculative
  scaffolding

Prioritize finding:

- contract violations
- active phase scope violations
- named milestone scope violations, if explicitly named
- future-phase leakage
- architectural drift
- hidden state coupling
- unsafe operational behavior
- partial implementations
- migration hazards
- rollback or recovery risks
- missing failure-path handling
- missing observability or audit behavior
- missing or incorrect tests

Avoid:

- style nitpicks
- speculative redesigns
- requiring abstractions not demanded by the active phase
- subjective preferences
- expanding scope based on future roadmap language
- using `docs/project-plan.md` to expand active phase scope

For every issue include:

- severity
- violated contract or invariant, with document reference
- affected files/modules/functions
- concrete risk
- recommended correction
- whether it blocks phase acceptance
- whether it blocks the named milestone Runnable state, if explicitly named

Guardrails:

- Follow `docs/<active-phase>/implementation-plan.md` milestone order when
  judging phase readiness.
- Do not expand active phase scope.
- Do not invent operational requirements; use `docs/<active-phase>/operations.md`.
- If active phase progress cannot be determined from docs, tests, and repository
  state, stop and ask.
- If the contract surface is missing, conflicting, or ambiguous in a way that
  affects review judgment, stop and ask.
- If a section has no findings, write "No findings." Do not invent concerns.

Output sections:

## 合同违背

## 范围与 Phase 泄漏

## 架构问题

## 可靠性风险

## 测试缺口

## 运维风险

## 次要建议

## 最终评估

- 合同符合度
- active phase 就绪度
- phase acceptance 风险
- 部署或运维风险
- 可维护性展望

Current active phase: <phase-name>
```

## Phase 0 Example

```md
Current active phase: phase-0
```

## Optional Milestone Focus

Use this only when the agent should focus on one milestone while still judging
it against the active phase contract.

```md
Current active phase: phase-0
Current milestone focus: Milestone 14: Strict Phase 0 Acceptance Pass
```

## Optional Narrow Review Override

Use this only when the agent should review part of a phase instead of the whole
phase.

```md
Current active phase: phase-0
Current review override: Review persistence and observability only.
```

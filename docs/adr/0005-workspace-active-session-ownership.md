# ADR 0005: Workspace Active Session Ownership

## Status

Accepted after Phase 0 implementation.

## Context

`debug-agent` is designed for long-running local debugging tasks. These tasks
can read files, later write patches, run shell and git commands, collect
artifacts, and resume from persisted state.

Multiple active sessions in the same workspace would compete over filesystem
state, git state, artifacts, checkpoints, and future approval grants. Fine
grained locking cannot reliably model global side effects from build systems,
shell commands, or git operations.

## Decision

A workspace root can have at most one active session.

The workspace root is the git worktree root when available, otherwise the
current working directory. Session creation claims active ownership for that
workspace. A second session for the same workspace is rejected while the first
session is active.

SQLite is the authoritative ownership store. Phase 0 enforces this with a
partial unique index over running sessions for a workspace. Local files may be
used later as fast anchors or diagnostics, but they do not replace persisted
runtime ownership.

Ownership is released when the session reaches a terminal state such as
`completed` or `failed`. Future interrupted/resume behavior must explicitly
define whether interrupted sessions continue to own the workspace.

## Alternatives Considered

### Allow multiple active sessions per workspace

This gives users more flexibility, but creates unclear behavior when sessions
modify the same files, run tests simultaneously, or write conflicting artifacts
and checkpoints.

### Automatically copy the workspace per session

This provides stronger isolation, but it is expensive for large repositories and
does not map well to debugging tasks that need the original worktree, local
build cache, or user environment.

### Use fine grained file locks

File locks are too narrow for commands with broad side effects, such as git,
build systems, generated files, and future writable tools.

### Require users to create git worktrees for parallelism

This remains the preferred parallelism model. It makes isolation explicit and
keeps runtime ownership simple.

## Consequences

- Runtime avoids hidden cross-session state coupling in one workspace.
- Active ownership errors are explicit user errors.
- Recovery and trace inspection can reason about one active execution context
  per workspace.
- Users who need parallel runs should use separate git worktrees or separate
  workspace directories.
- Future `/resume` and interruption work must preserve or revise this ownership
  rule deliberately.

## Phase 3 Refinement

Phase 3 preserves the one-active-session-per-workspace rule and adds
`owner_token` fencing for ownership release and user-confirmed stale fail-close.
Active ownership blockage remains a user-facing startup/resume conflict, but the
Phase 3 normalized error taxonomy records it as
`policy_error/workspace_owner_active` rather than `user_error`.

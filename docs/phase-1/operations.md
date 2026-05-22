# Phase 1 Operations

## Purpose

This document is the Phase 1 operational contract registry. It defines how
build, test, lint, and verification commands are discovered, approved, promoted,
and deprecated.

It is not a tutorial. Keep it short, contract-oriented, and append-oriented.

## Known Commands

Phase 1 uses `uv` for package management and command execution.

Phase 1 does not standardize new lint, type-check, build, or formatting
commands unless repository evidence and human approval promote them here.

Implementation acceptance requires the project lockfile to reflect any changed
dependency declarations.

Agents must not guess commands such as `ruff`, `mypy`, `npm`, `make`, or `just`
unless repository files or human instructions prove they are valid for this
project.

## Discovery Protocol

Before running or standardizing an operational command, inspect repository
evidence first:

- `pyproject.toml`
- `pytest.ini`
- `tox.ini`
- `noxfile.py`
- `Makefile`
- `justfile`
- `.github/workflows/*`
- `README.md`
- relevant files under `docs/`

Do not assume global tools, activated virtual environments, environment
variables, external services, or local infrastructure unless repository files or
human instructions explicitly define them.

If multiple candidate commands exist, report each candidate with its evidence
and wait for human approval before treating any command as canonical.

## Verification Scope Rule

Prefer the narrowest verification that meaningfully validates the modified
behavior.

Do not default to full test suite execution unless:

- required by the active phase contract.
- requested by the human.
- necessary due to broad architectural impact.

For Phase 1, narrow verification should prefer tests for:

- skill discovery, snapshot, hash, reference file loading, and activation
  behavior.
- prompt composition and `ModelContextFrame` construction.
- context estimation, omission, compression, and status-bar updates.
- ToolBroker shell/path/approval policy.
- approval grant persistence and audit.
- REPL slash commands `/skills`, `/tools`, and `/compress`.
- removal of model-visible `git_status`.
- one-shot, plain REPL, and TTY fallback behavior affected by approval or
  context changes.

If only partial verification is possible:

- state exactly what was verified.
- state what remains unverified.
- explain why.

## Command Promotion Rule

Discovered commands are not canonical commands.

A command becomes canonical only after human approval and an update to this
document.

After a command is promoted, future agents must prefer it over rediscovering or
inventing alternatives.

## Deprecation Rule

If a canonical command becomes obsolete, broken, or superseded:

1. Report the issue.
2. Propose the replacement command with evidence.
3. Wait for human approval.
4. Update this document after approval.

Do not silently migrate operational workflows.

## Canonical Commands

These commands are canonical for Phase 1 verification:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use the narrowest command that meaningfully validates the modified behavior.
Use `uv run pytest -v` for Phase 1 acceptance or broad cross-module changes.

This command is canonical for dependency lockfile updates:

```bash
uv lock
```

Run `uv lock` whenever Phase 1 dependency declarations change.

## Manual Verification

Manual verification is required for TTY behaviors that are not reliably covered
by automated tests:

- inline approval prompt in the prompt_toolkit application.
- denial returning to the prompt input area without terminalizing the session.
- status bar display order and context/token updates.
- `/skills`, `/tools`, and `/compress` visibility in TTY REPL.

Manual verification must record:

- terminal application used.
- command sequence.
- expected result.
- observed result.
- any known limitation.

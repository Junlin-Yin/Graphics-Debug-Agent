# Phase 3 Operations

## Purpose

This document is the Phase 3 operational contract registry. It defines
canonical verification commands for Phase 3 implementation and documentation
work.

It is not a tutorial. Keep it short, contract-oriented, and append-oriented.

## Known Commands

Repository evidence from earlier phase operations shows this project uses `uv`
for package management and command execution, with `pytest` configured in
`pyproject.toml`.

Phase 3 does not standardize new lint, type-check, build, formatting, or
external service commands unless repository evidence and human approval promote
them here.

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

If multiple candidate commands exist, report each candidate with its evidence
and wait for human approval before treating any new command as canonical.

## Verification Scope Rule

Prefer the narrowest verification that meaningfully validates the modified
behavior.

For Phase 3, narrow verification should prefer tests for:

- schema-version fail-closed behavior.
- normalized error payload and reason registry.
- durable conversation append/projection/cut validation.
- terminal recovery checkpoint creation and validation.
- startup/config/schema failure non-resumability.
- running turn cancellation and idle terminalization.
- explicit same-lineage resume.
- stale fail-close policy.
- provider and shell cancellation.
- output-token-limit continuation.
- retry registry behavior.
- shell timeout config cleanup.

Use the full suite for Phase 3 acceptance or broad cross-module changes.

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

These commands are canonical for Phase 3 verification:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use the narrowest command that meaningfully validates the modified behavior.
Use `uv run pytest -v` for Phase 3 acceptance or broad cross-module changes.

This command is canonical for dependency lockfile updates:

```bash
uv lock
```

Run `uv lock` whenever Phase 3 dependency declarations change.

## External Provider Verification

Automated Phase 3 tests must use fake or stubbed provider behavior. They must
not require network access, live API keys, or a real model provider.

Manual provider cancellation smoke may be recorded when a human supplies
environment variables, but it is not a canonical acceptance command for Phase 3.

Manual provider cancellation smoke must record:

- command sequence.
- configured provider and model.
- expected cancellation behavior.
- observed local runtime behavior.
- whether runtime avoided claiming remote provider stop/billing stop.

## Manual Verification

Manual verification is required for TTY behaviors that are not reliably covered
by automated tests:

- running `Ctrl+C` in REPL/TUI.
- idle `Ctrl+C` in REPL/TUI.
- `debug-agent resume <session_id>` interactive flow.
- stale fail-close confirmation.
- double interrupt while `cancelling`.

Manual verification must record:

- terminal application used.
- command sequence.
- expected result.
- observed result.
- any known limitation.

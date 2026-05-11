# Phase 0 Operations

## Purpose

This document is the Phase 0 operational contract registry. It defines how build, test, lint, and verification commands are discovered, approved, promoted, and deprecated.

It is not a tutorial. Keep it short, contract-oriented, and append-oriented.

## Known Commands

Phase 0 uses `uv` for package management and command execution.

Agents must not guess commands such as `ruff`, `mypy`, `npm`, `make`, or `just` unless repository files or human instructions prove they are valid for this project.

## Discovery Protocol

Before running or standardizing an operational command, inspect repository evidence first:

- `pyproject.toml`
- `pytest.ini`
- `tox.ini`
- `noxfile.py`
- `Makefile`
- `justfile`
- `.github/workflows/*`
- `README.md`
- relevant files under `docs/`

Do not assume global tools, activated virtual environments, environment variables, external services, or local infrastructure unless repository files or human instructions explicitly define them.

If multiple candidate commands exist, report each candidate with its evidence and wait for human approval before treating any command as canonical.

## Verification Scope Rule

Prefer the narrowest verification that meaningfully validates the modified behavior.

Do not default to full test suite execution unless:

- required by the active phase contract
- requested by the human
- necessary due to broad architectural impact

If only partial verification is possible:

- state exactly what was verified
- state what remains unverified
- explain why

## Command Promotion Rule

Discovered commands are not canonical commands.

A command becomes canonical only after human approval and an update to this document.

After a command is promoted, future agents must prefer it over rediscovering or inventing alternatives.

## Deprecation Rule

If a canonical command becomes obsolete, broken, or superseded:

1. Report the issue.
2. Propose the replacement command with evidence.
3. Wait for human approval.
4. Update this document after approval.

Do not silently migrate operational workflows.

## Canonical Commands

These commands are canonical for Phase 0 verification:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use the narrowest command that meaningfully validates the modified behavior. Use `uv run pytest -v` for Phase 0 acceptance or broad cross-module changes.

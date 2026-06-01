# Phase 2 Operations

## Purpose

This document is the Phase 2 operational contract registry. It defines
canonical verification commands for Phase 2 implementation and documentation
work.

It is not a tutorial. Keep it short, contract-oriented, and append-oriented.

## Known Commands

Repository evidence shows this project uses `uv` for package management and
command execution, with `pytest` configured in `pyproject.toml`.

Phase 2 does not standardize new lint, type-check, build, formatting, or
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

For Phase 2, narrow verification should prefer tests for:

- `view_image` tool schema, path policy, multi-image PNG/JPEG input, metadata,
  provider boundary, audit, and no-base64 guarantees.
- `todo` tool schema, persistence, events, `ModelContextFrame` injection,
  token estimation, and compression survival.
- Phase 2 schema version and legacy database fail-closed behavior.
- `status` and `trace` rendering for Todo Plan and `view_image`.

Use the full suite for Phase 2 acceptance or broad cross-module changes.

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

These commands are canonical for Phase 2 verification:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use the narrowest command that meaningfully validates the modified behavior.
Use `uv run pytest -v` for Phase 2 acceptance or broad cross-module changes.

This command is canonical for dependency lockfile updates:

```bash
uv lock
```

Run `uv lock` whenever Phase 2 dependency declarations change.

Phase 2 real multimodal execution uses the `openai` Python SDK, and image
metadata parsing uses Pillow. Adding either dependency is a Phase 2 dependency
declaration change and therefore requires `uv lock`.

## External Provider Verification

Automated Phase 2 tests must use fake or stubbed `VisionModelClient` behavior.
They must not require network access, live API keys, or a real multimodal
provider.

Manual provider smoke may be recorded when a human supplies environment
variables, but it is not a canonical acceptance command for Phase 2.

Manual provider smoke must record:

- command sequence.
- configured provider and model.
- image source.
- expected result.
- observed result.
- whether image base64 was absent from trace output.

## Manual Verification

Manual verification is required for TTY behaviors that are not reliably covered
by automated tests:

- `/tools` visibility for Phase 2 tools.
- inline approval prompt for `view_image`.
- denial returning to prompt input without terminalizing the session.
- optional Todo Plan summary presentation, if implemented.

Manual verification must record:

- terminal application used.
- command sequence.
- expected result.
- observed result.
- any known limitation.

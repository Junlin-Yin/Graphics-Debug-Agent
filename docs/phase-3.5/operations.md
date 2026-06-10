# Phase 3.5 Operations

## Purpose

This document defines canonical verification commands for Phase 3.5
documentation and future implementation work.

Phase 3.5 does not standardize new lint, type-check, build, formatting, or
external service commands unless repository evidence and human approval promote
them here.

## Discovery Protocol

Before running or standardizing a new operational command, inspect repository
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

Discovered commands are not canonical until human approval updates this file.

## Canonical Commands

These commands are canonical for Phase 3.5 verification:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use the narrowest command that meaningfully validates the modified behavior.
Use `uv run pytest -v` for Phase 3.5 acceptance or broad cross-module changes.

This command is canonical for dependency lockfile updates:

```bash
uv lock
```

Run `uv lock` whenever dependency declarations change.

## Native Tool Verification Scope

Prefer targeted tests for:

- schema version 4 compatibility and startup legacy reset.
- ToolBroker schema validation and default injection.
- approval scope signatures.
- audit event normalized/redacted arguments.
- portable glob subset.
- `find_file` traversal, hidden, deny, symlink, sort, and pagination.
- `read_file` pagination and file metadata cache updates.
- `list_dir` filtering and pagination.
- `search_text` controlled ripgrep boundary and output modes.
- stale-write guard for `edit_file` and `write_file`.
- `shell_exec` successful output shape and nonzero failure mapping.
- `view_image` unchanged ordinary output and query redaction.
- terminal recovery checkpoint `tool_availability`.

## External Dependencies

Phase 3.5 does not add Python dependencies for glob matching.

`search_text` depends on the `rg` executable at runtime. Automated tests should
cover both available and missing `rg` behavior without requiring network
access.

The absence of `rg` is not an installation failure for `debug-agent`; it is a
runtime `search_text` tool failure normalized as
`tool_error/tool_execution_failed`.

## Manual Verification

Manual verification should record:

- command sequence.
- expected result.
- observed result.
- session id and run id when applicable.
- trace/status excerpts for observability changes.
- known limitations.

Manual checks are useful for:

- conversation transcript readability in `logs/trace.md`.
- JSONL diagnostic readability in `logs/events.jsonl`.
- REPL/TUI rendering of tool pagination and guard failures.
- user-facing legacy reset messages.
- `shell_exec` output presentation.

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
- startup legacy reset fail-closed behavior when fresh Phase 3.5 runtime paths
  collide with orphaned legacy files or directories.
- ToolBroker schema validation and default injection.
- empty and whitespace-only path rejection, plus trim-before-canonicalization
  behavior for non-empty path strings.
- `load_skill_resource.path` remaining skill-local and excluded from the
  Phase 3.5 native filesystem workspace-path canonicalization rule.
- approval scope signatures.
- `write_file` reusable approval signatures that include planned parent
  directories to create.
- audit event normalized/redacted arguments.
- portable glob subset.
- inherited builtin denies, including `.sessions/`, global skill sources, and
  project skill sources.
- `find_file` traversal, hidden, deny, symlink, sort, and pagination.
- `read_file` pagination, streaming whole-file hash calculation, and file
  metadata cache updates.
- `list_dir` filtering and pagination.
- `search_text` controlled ripgrep boundary and output modes.
- `search_text` line-oriented pattern validation, including CR/LF rejection.
- `search_text` skipped-file counters as file-leaf aggregates, including denied
  subtree non-traversal, hidden subtree non-traversal, symlink escape in `other`,
  and UTF-8 decode pre-screening in `decode_error`.
- `search_text` content-mode pagination before context attachment, including
  matching-line result items, same-line repeated match counting, `next_offset`,
  and repeated context rows across adjacent pages.
- `search_text` context attachment by bounded runtime reads after matching-line
  pagination, including no ripgrep context flags and failure without partial
  successful pages when context attachment cannot read/stat/decode a selected
  page file.
- `search_text` ripgrep argv construction with `shell=False`, `--regexp`, `--`,
  `--no-config`, special-character paths, controlled `RIPGREP_CONFIG_PATH`
  behavior, runtime-side type filtering for explicit candidate files, fixed
  runtime-owned type allowlist behavior with case-insensitive file-family
  matching, required `rg` availability and regex compile checks before empty
  success using a runtime-owned empty temporary file, optional chunking,
  deterministic canonical-path result ordering, empty candidate handling, custom
  ripgrep type/config isolation, and no Python regex fallback.
- generic text tool streaming or bounded-memory behavior under the ToolBroker
  timeout envelope, including no partial successful pages on timeout.
- ToolResult envelope and durable `tool_result` serialization for structured
  native tool outputs, including deterministic field-level artifact references
  triggered by full-observation size in stable field order for documented large
  fields and `tool_error/tool_execution_failed` when the full native-tool
  observation still exceeds the inline threshold afterward.
- atomic artifact finalization for large tool output: temporary artifact files
  are committed to accepted ArtifactStore truth only after successful write and
  metadata calculation; timeout, cancellation, or registration failure must not
  expose artifact ids or accepted conversation references to incomplete
  artifacts.
- stale-write guard for `edit_file` and `write_file`.
- same-directory temporary-file atomic replace behavior for `edit_file` and
  overwrite `write_file`, without requiring crash-consistency or fsync-grade
  durability.
- `write_file` create-parent-directory side effects on failure or timeout,
  including cooperative deadline checks, no reported file write success, no file
  metadata cache update, and the documented minimal side-effect audit fields.
- trace argument redaction for `write_file.content`, `edit_file.old_text`, and
  `edit_file.new_text`.
- `shell_exec` default workspace-root `cwd`, successful output shape, and
  nonzero failure mapping, including dynamic frozen maximum timeout schema and
  completed-only optional diagnostic artifact exposure.
- `view_image` unchanged ordinary output and query redaction.
- terminal recovery checkpoint tool-availability facts.

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

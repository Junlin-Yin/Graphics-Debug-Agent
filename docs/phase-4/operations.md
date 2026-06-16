# Phase 4 Operations

## Purpose

This document defines canonical verification commands and manual smoke records
for Phase 4 documentation and implementation work.

Phase 4 adds RenderDoc readiness, main-agent thinking, run metrics, and package
deployment smoke. Use the narrowest command that validates the modified
behavior, then run broader acceptance commands before declaring Phase 4 ready.

## Discovery Protocol

Before standardizing additional operational commands, inspect repository
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

## Canonical Test Commands

These commands are canonical for Phase 4 automated verification:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v
uv run pytest -v
```

Use targeted tests for focused changes. Use `uv run pytest -v` for Phase 4
acceptance or broad cross-module changes.

This command is canonical for dependency lockfile updates:

```bash
uv lock
```

Run `uv lock` whenever dependency declarations change.

## Phase 4 Targeted Verification Scope

Prefer targeted tests for:

- schema version `5` bootstrap and Phase 3.5 `4` forward compatibility.
- fail-closed handling for missing, `< 4`, corrupt, and future schema versions.
- read-only `status`, `trace`, and `resume` schema gates.
- `[thinking]` config parsing, defaults, validation, frozen snapshot, and
  resume behavior.
- main-agent thinking request projection.
- thinking-enabled projection requiring an explicit provider thinking-enable
  option, not only `effort`.
- stripping `thinking` blocks from all accepted/durable/subsequent paths.
- `view_image` remaining thinking-disabled.
- run metrics collection, JSON shape, atomic writing, and best-effort failure.
- run metrics UTC millisecond filename format and deterministic collision
  suffix behavior.
- run metrics `token_source` shape for provider and estimated usage.
- provider usage normalization from direct `usage`, `usage_metadata`, and
  `response_metadata.usage`.
- cumulative provider `input_tokens`, `output_tokens`, and `total_tokens` in
  run metrics and existing REPL/TUI token accounting.
- token accounting boundaries: main-agent and context-compression calls count,
  brokered `view_image` provider usage does not count, and mixed provider/
  estimated windows use whole-window deterministic estimates.
- cumulative estimated `input_tokens`, `output_tokens`, and `total_tokens` when
  provider usage is unavailable.
- absence of `estimated_context_tokens` from run metrics.
- absence of separate reasoning/thinking-token fields derived from thinking
  content.
- fake `rdc` readiness through `shell_exec` and `view_image`.
- deployment smoke when package metadata or entrypoint behavior changes.

## Package Deployment Smoke

Canonical package smoke:

```bash
uv build
uv tool install --force dist/debug_agent-0.1.0-py3-none-any.whl
mkdir -p /tmp/debug-agent-smoke
cd /tmp/debug-agent-smoke
debug-agent --help
```

If the generated wheel filename differs from
`dist/debug_agent-0.1.0-py3-none-any.whl`, use the single generated wheel path
from `dist/`.

Expected result:

- `uv build` succeeds.
- `uv tool install --force ...` succeeds.
- installed `debug-agent --help` runs outside the source checkout.

Optional runtime smoke when valid local model config is available:

```bash
cd /tmp/debug-agent-smoke
debug-agent -p "hello"
debug-agent status <session_id>
debug-agent trace <session_id>
```

Record the session id, run id, trace path, metrics path, and observed result.

## Fake `rdc` CI Scenario

The fake `rdc` readiness scenario is canonical automated verification and should
run under the integration test suite.

This scenario verifies the runtime readiness path. It does not require the
external adapted `renderdoc-gpu-debug` skill.

Expected high-level command sequence inside the prompt session:

```bash
rdc doctor
rdc open sample.rdc
rdc info --json
rdc draws --limit 20
rdc rt <eid> -o <output.png>
rdc close
```

The exported PNG must be inspected by `view_image` before the final assistant
answer.

Expected result:

- fake `rdc` commands execute through brokered `shell_exec`.
- `view_image` successfully inspects the generated PNG.
- session terminalizes.
- `logs/trace.md` is generated.
- `logs/run_metrics_*.json` is generated.

## Adapted `renderdoc-gpu-debug` Skill Smoke

The adapted skill smoke is a manual canonical operation because the
`renderdoc-gpu-debug` adaptation lives outside this repository.

Record:

- external adapted skill location/version or content hash.
- workspace path used for the run.
- how the skill was installed or exposed under project skill discovery.
- evidence that `renderdoc-gpu-debug` was discoverable and activated.
- whether fake `rdc` or real `rdc` was used.
- command sequence observed through brokered `shell_exec`.
- `view_image` result for exported PNG output when applicable.
- session id and run id.
- trace path.
- metrics path.
- observed result.
- known limitations.

This manual smoke may be combined with the Windows + real `rdc` smoke when the
same run uses the external adapted skill and records all required fields.

## Windows + Real `rdc` Smoke

Windows + real `rdc` smoke is a manual canonical operation or optional
self-hosted automation gate. It is not required for ordinary PR CI.

Prefer an existing sample `.rdc` capture over live capture automation.

Suggested command sequence:

```bash
rdc doctor
rdc open <sample.rdc>
rdc info --json
rdc draws --limit 20
rdc rt <eid> -o <output.png>
rdc close
```

Run the corresponding `debug-agent` prompt flow so that the PNG is inspected
through `view_image`.

Manual record must include:

- Windows version.
- RenderDoc and `rdc` versions.
- whether the external adapted `renderdoc-gpu-debug` skill was used.
- relevant environment variables and PATH notes.
- sample `.rdc` path.
- command sequence.
- expected result.
- observed result.
- session id and run id.
- trace path.
- metrics path.
- known limitations.

Phase 4 v1 completion requires a recorded passing Windows + real `rdc` smoke.

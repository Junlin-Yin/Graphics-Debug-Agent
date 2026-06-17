# Phase 4 Milestone 6 Evidence

Date: 2026-06-17
Host: macOS/Darwin local development machine
Workspace: `/Users/xinzhu/Workspace/MyAgent`

## Automated Verification

Commands:

- `uv run pytest tests/unit -v`
  - Result: passed.
  - Summary: `792 passed, 1 skipped in 21.82s`.
- `uv run pytest tests/integration -v`
  - Result: passed.
  - Summary: `54 passed in 14.32s`.
  - Fake `rdc` readiness coverage: included
    `tests/integration/test_phase4_fake_rdc_readiness.py::test_phase4_fake_rdc_readiness_uses_brokered_shell_and_view_image`.
- `uv run pytest -v`
  - Result: passed.
  - Summary: `846 passed, 1 skipped in 33.95s`.

## Package Deployment Smoke

Commands:

- `uv build`
  - First sandboxed attempt: failed because the managed sandbox could not open
    `/Users/xinzhu/.cache/uv/sdists-v9/.git`.
  - Escalated retry result: passed.
  - Output summary:
    - `Successfully built dist/debug_agent-0.1.0.tar.gz`
    - `Successfully built dist/debug_agent-0.1.0-py3-none-any.whl`
- `uv tool install --force dist/debug_agent-0.1.0-py3-none-any.whl`
  - First sandboxed attempt: failed because the managed sandbox could not open
    `/Users/xinzhu/.cache/uv/sdists-v9/.git`.
  - Escalated retry result: passed.
  - Output summary:
    - installed `debug-agent==0.1.0` from the generated wheel.
    - installed executable: `debug-agent`.
- `mkdir -p /tmp/debug-agent-smoke`
  - Result: passed.
- `debug-agent --help`
  - Working directory: `/tmp/debug-agent-smoke`.
  - Result: passed.
  - Output summary:

    ```text
    Usage:
      debug-agent [--approval-mode normal|semi-auto|yolo]  # REPL
      debug-agent [--approval-mode normal|semi-auto|yolo] -p "prompt"
      debug-agent status <session_id>
      debug-agent trace <session_id>
      debug-agent resume <session_id>
    ```

Generated artifacts:

- `dist/debug_agent-0.1.0.tar.gz`
- `dist/debug_agent-0.1.0-py3-none-any.whl`

The `dist/` directory is ignored by `.gitignore` and must not be committed.

## Fake `rdc` Readiness Confirmation

Ordinary integration automation covers fake `rdc` readiness.

Evidence:

- `uv run pytest tests/integration -v` passed.
- `uv run pytest -v` passed.
- Both runs included
  `tests/integration/test_phase4_fake_rdc_readiness.py::test_phase4_fake_rdc_readiness_uses_brokered_shell_and_view_image`.

This confirms the automated fake `rdc` scenario is part of ordinary integration
automation. It does not verify the external adapted `renderdoc-gpu-debug` skill.

## Manual Adapted `renderdoc-gpu-debug` Skill Smoke

No real local evidence was available for this milestone pass.

Missing required fields:

- external adapted skill location/version or content hash.
- workspace path used for the run.
- how the adapted skill was installed or exposed under project skill discovery.
- evidence that `renderdoc-gpu-debug` was discoverable and activated.
- whether fake `rdc` or real `rdc` was used.
- brokered `shell_exec` command sequence.
- brokered `view_image` result for exported PNG output when applicable.
- session id and run id.
- trace path.
- metrics path.
- observed result.
- known limitations.

Milestone 6 adapted-skill smoke gates remain incomplete.

## Windows + Real `rdc` Smoke

No real local evidence was available for this milestone pass.

Missing required fields:

- Windows version.
- runner or machine details.
- RenderDoc and `rdc` versions.
- whether the external adapted `renderdoc-gpu-debug` skill was used.
- relevant environment variables and PATH notes.
- sample `.rdc` path.
- real `rdc` command sequence.
- expected result.
- observed result.
- session id and run id.
- trace path.
- metrics path.
- known limitations.

Milestone 6 Windows + real `rdc` smoke gates remain incomplete, so v1 completion
remains blocked on this manual evidence.

## Scope Review Summary

Package metadata changes were not required. `pyproject.toml` already declares
the documented console script:

```toml
[project.scripts]
debug-agent = "debug_agent.cli.main:main"
```

No runtime code changes were made for Milestone 6.

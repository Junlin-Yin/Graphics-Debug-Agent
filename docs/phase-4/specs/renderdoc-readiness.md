# Phase 4 RenderDoc Readiness Specification

## Boundary

Phase 4 validates RenderDoc readiness in two layers:

- automated runtime readiness using a fake `rdc` scenario.
- manual smoke of the externally adapted `renderdoc-gpu-debug` prompt skill.

Runtime core must not encode RenderDoc business procedures. The prompt skill
owns how to use RenderDoc and `rdc`.

The `renderdoc-gpu-debug` skill adaptation is external to this repository and
is considered a Phase 4 input. This repository verifies runtime support through
fake `rdc` automation, and verifies the external adapted skill through a manual
canonical smoke record.

## Runtime Requirements

The readiness flow must use existing brokered tools:

- `shell_exec` for short structured `rdc` commands.
- `view_image` for local PNG/JPEG inspection after `rdc rt` exports an image.

The flow must not require:

- PTY shell.
- interactive shell.
- long-running shell runtime.
- background shell.
- workflow runtime.
- subagents.
- MCP tools.
- tool-call cache.
- RenderDoc command allowlists in runtime core.
- runtime-owned RenderDoc daemon state.

`rdc open/status/close` manage `rdc`'s own daemon-backed inspection state. That
state is not a `debug-agent` session, run, checkpoint, or recovery truth.

## Fake `rdc` Automated Scenario

Fake `rdc` is an automated acceptance fixture. It must live under tests, not
under `src/debug_agent`, and must not be packaged with `debug-agent`.

Recommended layout:

```text
tests/integration/test_phase4_fake_rdc_readiness.py
tests/integration/fixtures/fake_rdc.py
```

Tests may dynamically generate a temporary executable:

```text
<tmp>/bin/rdc
```

and prepend `<tmp>/bin` to `PATH`.

The fake executable must support:

```bash
rdc doctor
rdc open sample.rdc
rdc info --json
rdc draws --limit 20
rdc rt <eid> -o <output.png>
rdc close
```

Expected behavior:

- `doctor` exits `0`.
- `open` accepts an existing sample `.rdc` path and records per-test state.
- `info --json` writes deterministic capture metadata JSON.
- `draws --limit 20` writes deterministic draw-call output.
- `rt ... -o <output.png>` writes a valid PNG file.
- `close` clears per-test state and exits `0`.

The fake must be deterministic and isolated per test. It may use a state file
under `tmp_path`; it must not use global machine state.

## Automated Readiness Test Shape

The fake scenario must run a complete prompt session with a fake or scripted
model path. It must not rely on real provider behavior or on availability of
the external `renderdoc-gpu-debug` skill.

The model script should drive the minimal tool sequence:

1. `shell_exec`: `rdc doctor`
2. `shell_exec`: `rdc open sample.rdc`
3. `shell_exec`: `rdc info --json`
4. `shell_exec`: `rdc draws --limit 20`
5. `shell_exec`: `rdc rt <eid> -o <output.png>`
6. `view_image`: inspect `<output.png>`
7. `shell_exec`: `rdc close`
8. final assistant answer

The test must verify:

- every command is executed through ToolBroker `shell_exec`.
- fake `rdc` is found through the configured process environment `PATH`.
- command `cwd` behavior matches the workspace expectation.
- the exported PNG exists and is valid for `view_image`.
- `view_image` is brokered and returns an ordinary tool result.
- the prompt session terminalizes.
- `.sessions/<session_id>/logs/trace.md` exists.
- `.sessions/<session_id>/logs/run_metrics_*.json` exists.
- the metrics file is valid JSON.

This test is the ordinary CI readiness guard for RenderDoc runtime support.

The automated fake scenario may use a local test skill fixture or direct
scripted model messages to exercise the runtime path. It is not the acceptance
gate for the external `renderdoc-gpu-debug` skill content.

## Manual Adapted Skill Smoke

The externally adapted `renderdoc-gpu-debug` skill must be verified by a manual
canonical smoke record before Phase 4 is accepted.

The manual smoke must run `debug-agent` with the adapted skill available as a
project skill and must verify:

- the `renderdoc-gpu-debug` skill is discoverable and activated for the session.
- the session freezes the adapted skill snapshot.
- the skill-guided run executes `rdc` commands through brokered `shell_exec`.
- any exported PNG used by the run is inspected through brokered `view_image`.
- the prompt session terminalizes.
- trace and run metrics paths are recorded.

This manual adapted-skill smoke may use fake `rdc` or real `rdc`. When it uses
Windows + real `rdc`, it may also satisfy the Windows + real `rdc` smoke gate if
all required real-smoke fields are recorded.

## Windows + Real `rdc` Smoke

Windows + real `rdc` smoke is the v1 completion gate, but it is not required for
ordinary PR CI.

Allowed execution forms:

- manual canonical operation.
- optional self-hosted Windows CI job.
- optional release/nightly gate.

The smoke should prefer an existing sample `.rdc` file over live capture
automation. Live capture depends on GPU/driver/windowing state and is outside
the ordinary Phase 4 automation requirement.

The real smoke command sequence should mirror the fake scenario:

```bash
rdc doctor
rdc open <sample.rdc>
rdc info --json
rdc draws --limit 20
rdc rt <eid> -o <output.png>
debug-agent one-shot flow inspects <output.png> with view_image
rdc close
```

The manual record must include:

- OS and environment summary.
- whether the run used the external adapted `renderdoc-gpu-debug` skill.
- `rdc doctor` result.
- command sequence.
- expected result.
- observed result.
- session id and run id.
- trace path.
- metrics path.
- known limitations.

Real smoke does not require RenderDoc GUI automation.

## Non-Goals

Phase 4 RenderDoc readiness does not implement:

- real capture automation as a required CI test.
- RenderDoc GUI smoke.
- runtime-owned RenderDoc session state machine.
- `rdc` command allowlist in runtime core.
- shader report validation in runtime core.
- Ralph Loop.
- shader patch loop.

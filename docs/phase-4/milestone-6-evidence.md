# Phase 4 Milestone 6 Evidence

Date: 2026-06-17
Automated verification host: macOS/Darwin local development machine
Automated verification workspace: `/Users/xinzhu/Workspace/MyAgent`

Manual RenderDoc smoke host: Windows 11 Pro, build 26100.3323
Manual RenderDoc smoke workspace: `D:\Projects\MyWorkspace`

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

Manual smoke was executed in `D:\Projects\MyWorkspace` against the project-local
adapted skill.

Adapted skill location:

- `D:\Projects\MyWorkspace\.debug-agent\skills\renderdoc-gpu-debug\SKILL.md`
- References:
  - `D:\Projects\MyWorkspace\.debug-agent\skills\renderdoc-gpu-debug\references\commands-quick-ref.md`
  - `D:\Projects\MyWorkspace\.debug-agent\skills\renderdoc-gpu-debug\references\rdc-debugging-primitives.md`
  - `D:\Projects\MyWorkspace\.debug-agent\skills\renderdoc-gpu-debug\references\report-template.md`
  - `D:\Projects\MyWorkspace\.debug-agent\skills\renderdoc-gpu-debug\references\unity-tuanjie-recipes.md`
  - `D:\Projects\MyWorkspace\.debug-agent\skills\renderdoc-gpu-debug\references\windows-rdc-setup.md`

Skill evidence:

- The session artifact contains
  `D:\Projects\MyWorkspace\.sessions\sess_2026-06-17-23-29-19-e736\artifacts\renderdoc-gpu-debug_skill_snapshot.json`.
- The skill snapshot file SHA256 observed during evidence review was
  `D7B9F245CFD950660AC4B62DF75540DC53378DBF6D50D49EBEF1280060921023`.
- The trace records `Skill activated: renderdoc-gpu-debug
  (sha256:008680e561f69fa5b6d3120633754bc73f5e5950708116ab7e198741247b6b4b)`.
- The trace records loading `references/unity-tuanjie-recipes.md` through the
  `renderdoc-gpu-debug` skill.

Primary session:

- Session id: `sess_2026-06-17-23-29-19-e736`.
- Run id: `run_31ac17f929e340658e0542ff102ed919`.
- Invocation kind: `start`.
- Runtime result: completed.
- `rdc` mode: real `rdc`, not fake `rdc`.
- Trace path:
  `D:\Projects\MyWorkspace\.sessions\sess_2026-06-17-23-29-19-e736\logs\trace.md`.
- Metrics path:
  `D:\Projects\MyWorkspace\.sessions\sess_2026-06-17-23-29-19-e736\logs\run_metrics_20260617T154134.475Z.json`.

Brokered command and image evidence:

- The trace records brokered `shell_exec` calls for:
  - `rdc doctor`
  - `rdc open capture.rdc`
  - `rdc status`
  - `rdc info --json`
  - `rdc stats`
  - `rdc passes`
  - `rdc draws --limit 50`
  - multiple `rdc rt <eid> -o <png>` calls, including EIDs `47`, `85`,
    `121`, `158`, `197`, `232`, `277`, `322`, `425`, and `472`.
  - texture exports used during diagnosis.
  - `rdc close`
- The trace records brokered `view_image` calls for:
  - `D:\renderdoc\034\rt.png`
  - `D:\renderdoc\034\expected.rt.png`
  - exported render targets such as `rt_eid47.png`, `rt_eid85.png`,
    `rt_eid121.png`, `rt_eid158.png`, `rt_eid197.png`, `rt_eid232.png`,
    `rt_eid277.png`, `rt_eid322.png`, `rt_eid425.png`, and `rt_eid472.png`.
  - exported textures such as `texture_14775_CameraColorAttachment.png`,
    `texture_14801_CameraOpaque.png`, `texture_14826_FinalOutput.png`,
    `texture_14302_swapchain.png`, `texture_14306_render.png`,
    `tex_12921_IndustrialGlass_basecolor.png`, and
    `tex_14035_Hole_mesh_basecolor.png`.

Observed result:

- The adapted skill was discoverable and activated.
- The run used real `rdc` against `D:\renderdoc\034\capture.rdc`.
- Render targets and textures were exported and inspected through brokered
  `view_image`.
- The `rdc` session closed successfully with `session closed`.
- The final assistant response produced a RenderDoc diagnostic report.
- Metrics recorded `118` total tool calls, `103` successful tool calls, and
  `15` failed exploratory tool calls.

Known limitations:

- Some exploratory commands failed because the live `rdc` CLI did not support
  the attempted option or argument shape, for example `shader --disasm`,
  `pixel-history`, `pixel --eid`, and some `texture`/`cbuffer` forms. The run
  recovered and completed using supported commands.
- The manual record intentionally captures only RenderDoc/`rdc`-relevant
  environment details rather than a full environment-variable dump.

Auxiliary session:

- Session id: `sess_2026-06-17-22-46-41-126a`.
- Run id: `run_0e26a4f87a624287b3b8129d777a00ab`.
- Invocation kind: resumed prompt session.
- Runtime result: completed.
- Trace path:
  `D:\Projects\MyWorkspace\.sessions\sess_2026-06-17-22-46-41-126a\logs\trace.md`.
- Metrics path:
  `D:\Projects\MyWorkspace\.sessions\sess_2026-06-17-22-46-41-126a\logs\run_metrics_20260617T145359.083Z.json`.
- This session also activated `renderdoc-gpu-debug`, used real `rdc`, inspected
  image outputs, and closed the `rdc` session. It is retained as supporting
  evidence; the primary acceptance evidence is
  `sess_2026-06-17-23-29-19-e736`.

## Windows + Real `rdc` Smoke

Windows + real `rdc` smoke was executed and recorded through the primary
manual session above.

Machine details:

- OS: Windows 11 Pro, build `26100.3323`.
- CPU: 12th Gen Intel(R) Core(TM) i7-12800HX 2.00 GHz.
- RAM: 32.0 GB.

RenderDoc and `rdc` details:

- `rdc`: `C:\Users\xinzhu\.local\bin\rdc.exe`.
- `rdc --version`: `rdc, version 0.5.5`.
- RenderDoc command-line tool:
  `C:\Program Files\RenderDoc\renderdoccmd.exe`.
- `renderdoccmd --version`:
  `renderdoccmd x64 v1.41 built from c9e72e3d706c18601de874bcdb875b0ec977f952`.
- `rdc doctor` also reported:
  - Python `3.13.3`.
  - platform `windows`.
  - `renderdoc-module: version=1.41`.
  - replay-support found.
  - Visual Studio Build Tools `17.13.35825.156`.
  - RenderDoc found at `C:\Program Files\RenderDoc\renderdoc.dll`.
  - Vulkan layer registered at
    `C:\Users\xinzhu\AppData\Local\rdc\renderdoc\renderdoc.json`.

Relevant environment and PATH notes:

- Relevant PATH entries observed in the current shell:
  - `C:\Users\xinzhu\.local\bin`
  - `C:\Users\xinzhu\AppData\Local\Microsoft\WindowsApps`
  - `C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.2.0_x64__8wekyb3d8bbwe`
  - `c:\Users\xinzhu\.vscode\extensions\ms-python.debugpy-2026.6.0-win32-x64\bundled\scripts\noConfigScripts`
- No non-empty explicit process environment variables were found for
  `RENDERDOC`, `RENDERDOC_HOME`, `RENDERDOC_PATH`, `PYTHONPATH`,
  `PYTHONHOME`, `VK_LAYER_PATH`, `VK_INSTANCE_LAYERS`, or `RDC_HOME`.

Sample capture and input files:

- Workspace root: `D:\Projects\MyWorkspace`.
- `rdc` working directory during the primary run: `D:\renderdoc\034`.
- Sample capture: `D:\renderdoc\034\capture.rdc`.
- Input comparison images:
  - `D:\renderdoc\034\rt.png`
  - `D:\renderdoc\034\expected.rt.png`

Command sequence:

- `rdc doctor`
- `rdc open capture.rdc`
- `rdc status`
- `rdc info --json`
- `rdc stats`
- `rdc passes`
- `rdc draws --limit 50`
- multiple `rdc rt <eid> -o <output.png>` exports.
- texture export commands used for diagnosis.
- `rdc close`

Expected result:

- The adapted `renderdoc-gpu-debug` skill is discovered and activated.
- Real `rdc` opens the sample capture.
- The prompt flow uses brokered `shell_exec` for `rdc` commands.
- Exported PNGs are inspected through brokered `view_image`.
- The prompt session terminalizes, writes trace and metrics, and closes the
  `rdc` session.

Observed result:

- Expected result was met.
- Primary session id: `sess_2026-06-17-23-29-19-e736`.
- Primary run id: `run_31ac17f929e340658e0542ff102ed919`.
- Trace path:
  `D:\Projects\MyWorkspace\.sessions\sess_2026-06-17-23-29-19-e736\logs\trace.md`.
- Metrics path:
  `D:\Projects\MyWorkspace\.sessions\sess_2026-06-17-23-29-19-e736\logs\run_metrics_20260617T154134.475Z.json`.
- `rdc close` completed with `session closed`.
- The prompt session completed and produced a diagnostic report.

Known limitations:

- Some exploratory `rdc` calls failed because of unsupported command-line
  shapes, as documented in the adapted skill smoke section. These failures did
  not block the completed smoke result.
- The smoke used an existing sample capture rather than live capture
  automation, which is the preferred Phase 4 manual operation.

## Scope Review Summary

Package metadata changes were not required. `pyproject.toml` already declares
the documented console script:

```toml
[project.scripts]
debug-agent = "debug_agent.cli.main:main"
```

No runtime code changes were made for Milestone 6.

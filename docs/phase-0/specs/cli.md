# Phase 0 CLI Specification

## Commands

### `debug-agent`

Starts REPL mode.

Defaults:

- approval mode: `normal`
- run type: `prompt`
- session lifetime: until `/exit` or fatal error
- workspace root: git worktree root when inside a git worktree; otherwise current working directory

### `debug-agent -p "..."`

Runs one-shot prompt mode.

Defaults:

- approval mode: `yolo`
- run type: `prompt`
- session lifetime: single prompt run
- workspace root: git worktree root when inside a git worktree; otherwise current working directory

### `debug-agent status <session_id>`

Prints persisted session status.

Minimum fields:

- `session_id`
- `workspace_root`
- `status`
- `approval_mode`
- `active_run_id`
- `latest_run_id`
- `latest_checkpoint_id`
- `created_at`
- `updated_at`
- `error_summary`

### `debug-agent trace <session_id>`

Renders or prints session trace from run events and artifact metadata.

Minimum fields:

- session header
- run list
- event timeline
- checkpoints
- artifacts
- terminal status or error

## REPL Slash Commands

### `/status`

Prints the same logical status as `debug-agent status <session_id>` for the current session.

### `/exit`

Stops the REPL. If a run is active, runtime completes the prompt run at the nearest safe boundary and releases workspace ownership.

## Commands Not In Phase 0

These commands are reserved for later phases and must not be required for Phase 0 acceptance:

- `resume`
- `/resume`
- `/compress`
- `/skills`
- `/agents`
- `/models`
- `Ctrl+Y` mode switching
- `plugins list`
- `--workspace`

## Output Rules

- User-facing command errors must be concise and actionable.
- Machine-readable JSON output is not required in Phase 0.
- `debug-agent trace <session_id>` refreshes `.sessions/<session_id>/trace.md` if missing or stale, then prints the trace path plus a short summary. Stale detection uses rendered `event_count` and `latest_event_id` metadata compared with the current persisted event set.
- Slash commands are handled locally and are never sent to the model.

## Exit Codes

- `0`: command completed successfully.
- `1`: runtime or model execution failed.
- `2`: CLI usage error.
- `3`: policy denied or active workspace ownership conflict.
- `4`: configuration error.

Configuration errors return exit code `4` even when no session could be created. A `config_error` event is written only if a session already exists.

Missing provider/model configuration is a configuration error. Phase 0 does not choose a built-in provider/model.

## Running Input Rules

- In REPL, ordinary user input is accepted only when the prompt run is ready for the next turn.
- While an execution is actively running, only local slash commands that do not mutate model context are allowed.
- Phase 0 supports `/status` and `/exit` during active execution.
- Ctrl+C or mid-call cancellation is recorded as `failed` with error class `cancelled`, and workspace ownership is released.

## Error Messages

Active workspace conflict message must include:

```text
An active debug-agent session already owns this workspace.
Session: <session_id>
Use that session, wait for it to finish, or start in a separate git worktree.
```

Missing session message must include:

```text
No session found for id: <session_id>
```

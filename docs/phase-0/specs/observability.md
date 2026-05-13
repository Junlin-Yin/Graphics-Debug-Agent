# Phase 0 Observability Specification

## Outputs

```text
.sessions/<session_id>/logs/engine.log
.sessions/<session_id>/trace.md
.sessions/runtime.db
```

`runtime.db` is truth. `engine.log` and `trace.md` are derived observability surfaces.

## engine.log

Log format is JSON Lines. Each entry contains:

- `timestamp`
- `session_id`
- `run_id`
- `step_id`
- `level`
- `event`
- `message`
- `metadata`

Allowed `level` values:

- `DEBUG`
- `INFO`
- `WARN`
- `ERROR`

Minimum events:

- session start/end/failure
- run start/end/failure
- model call start/end/failure
- tool call allow/deny/failure
- checkpoint written
- artifact registered
- ownership conflict

## trace.md

Trace is generated from run events, checkpoints, and artifact metadata.

Trace generation is not performed after every event. Runtime refreshes `trace.md` when a session reaches terminal state, and `debug-agent trace <session_id>` refreshes it on demand if missing or stale.

Stale means the rendered trace no longer reflects the latest persisted event set. Phase 0 should determine this with low-cost metadata:

- Store the latest rendered `event_count` and `latest_event_id` in trace metadata.
- Treat `trace.md` as stale when the current persisted event count differs from the rendered event count.
- If event count is equal, treat `trace.md` as stale when the latest persisted event id differs from the rendered latest event id.
- Do not checksum event payloads or artifact contents in Phase 0.

Minimum sections:

- Session summary
- Runs
- Timeline
- Checkpoints
- Artifacts
- Errors

Trace must be readable by humans and useful to future agent runs. It is not a state recovery source.

## Event To Trace Mapping

- `session_started`: session header and workspace.
- `run_started`: run section.
- `user_message`: timeline item with redacted or summarized user input.
- `assistant_message`: timeline item with assistant output summary.
- `model_call_started`: timeline item with provider/model metadata.
- `model_call_completed`: timeline item with usage, duration, response summary,
  tool-call summary, and artifact ids.
- `tool_call_*`: timeline item with tool name, status, duration, result summary,
  and artifact ids.
- `checkpoint_written`: checkpoint section entry.
- `artifact_registered`: artifact section entry.
- `*_failed`: errors section entry.

## Status View Fields

Status command shows:

- `session_id`
- `workspace_root`
- `session_status`
- `approval_mode`
- `active_run_id`
- `latest_run_id`
- `latest_run_status`
- `latest_checkpoint_id`
- `created_at`
- `updated_at`
- `error_summary`

## Trace View Fields

Trace command shows or references:

- `trace_path`
- refreshed/stale status
- `session_id`
- `workspace_root`
- run count
- event count
- artifact count
- terminal status
- error summary

## Error Recording

Every error event payload includes:

- `error_class`
- `message`
- `source`
- `recoverable`
- `details_artifact_id` when details are large

Large stack traces or external outputs go to artifacts, not inline event payloads.

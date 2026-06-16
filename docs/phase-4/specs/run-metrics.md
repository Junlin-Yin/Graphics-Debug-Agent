# Phase 4 Run Metrics Specification

## Boundary

Phase 4 writes a non-authoritative per-invocation metrics file when a prompt
session terminalizes.

Metrics files support Phase 4 readiness evaluation and manual review. They are
not runtime truth, audit truth, checkpoint input, resume input, recovery input,
or business-quality scoring.

Runtime must not use metrics files to answer `status`, rebuild `trace`, validate
recovery, resume a session, or aggregate historical invocation state.

## File Layout

Metrics files are written under the session logs directory:

```text
.sessions/<session_id>/logs/run_metrics_<timestamp>.json
```

Timestamp format:

```text
YYYYMMDDTHHMMSS.SSSZ
```

The timestamp uses UTC and millisecond precision.

Each process invocation writes a new metrics file. Resume does not overwrite or
read previous metrics files.

If the target filename already exists for the same millisecond timestamp, the
writer must create a deterministic suffixed filename instead of overwriting:

```text
run_metrics_<timestamp>_1.json
run_metrics_<timestamp>_2.json
```

## Invocation Window

Each process invocation owns an independent in-memory metrics window.

Fresh session start:

```text
metrics_started_at = invocation start time
```

Explicit resume:

```text
metrics_started_at = resume invocation start time
```

`metrics_ended_at` is captured when the current invocation has reached a
terminal prompt-session outcome and observable model/tool completion data for
the invocation has been collected.

Resume starts a new collector and does not merge earlier invocation metrics. If
case-level aggregation is needed, an external eval harness may merge multiple
metrics files offline.

## Write Timing

Metrics are written during terminalization, near the existing automatic terminal
trace refresh path.

Metrics writing happens after the runtime knows the terminal outcome for the
current invocation.

Metrics write failure must not:

- roll back terminalization.
- affect terminal checkpoint creation.
- affect ownership release.
- affect the process exit code.
- write runtime truth.
- write audit events.
- write run events.
- write `events.jsonl` diagnostics as authoritative facts.

CLI/UI may show a best-effort warning.

## Data Sources

Allowed current-invocation data sources:

- model-call completion observations.
- tool-call completion observations.
- provider usage payloads when available.
- existing tool result status and timing metadata.
- invocation start/end wall-clock time.

Disallowed data sources:

- previous `run_metrics_*.json` files.
- `trace.md`.
- `events.jsonl`.
- checkpoint payloads.
- business report files.
- RenderDoc report schemas.

## JSON Shape

Metrics files use schema version `1`:

```json
{
  "schema_version": 1,
  "session_id": "sess_...",
  "run_id": "run_...",
  "metrics_started_at": "2026-06-16T09:10:00Z",
  "metrics_ended_at": "2026-06-16T09:15:30Z",
  "invocation_kind": "start",
  "timing": {
    "wall_time_ms": 330000,
    "llm_time_ms_observed": 120000,
    "tool_time_ms_observed": 85000,
    "tool_time_coverage": {
      "timed_tool_calls": 16,
      "total_tool_calls": 18
    }
  },
  "tokens": {
    "provider_usage_available": true,
    "token_source": "provider",
    "input_tokens": 85000,
    "output_tokens": 12000,
    "total_tokens": 97000,
    "estimator_version": null
  },
  "tools": {
    "total_tool_calls": 18,
    "successful_tool_calls": 17,
    "failed_tool_calls": 1,
    "tool_success_rate": 0.9444,
    "tool_failure_rate": 0.0556,
    "failure_breakdown": {
      "error": 1,
      "timeout": 0,
      "denied": 0,
      "cancelled": 0
    }
  }
}
```

`invocation_kind` is one of:

- `"start"`
- `"resume"`

## Timing Metrics

`wall_time_ms` is elapsed wall-clock time from `metrics_started_at` to
`metrics_ended_at`.

`llm_time_ms_observed` is the sum of observed LLM-call duration in the current
invocation. It includes main agent model calls and context compression model
calls. It does not include `view_image` provider time.

`tool_time_ms_observed` is the sum of observed brokered tool-call duration for
tool calls that expose timing data. `view_image` duration belongs here because
runtime observes it as a brokered tool call even though it internally calls a
vision provider.

`tool_time_coverage` explains whether all tool calls provided timing data.
Unless `timed_tool_calls == total_tool_calls`, `tool_time_ms_observed` must not
be interpreted as all tool time.

## Token Metrics

Runtime normalizes provider usage from the existing Kimi/Anthropic-compatible
response path before metrics or REPL/TUI token accounting consume it. Supported
provider response sources include direct `usage`, `usage_metadata`, and
`response_metadata.usage` when those objects are available through the current
LangChain response type.

`token_source` is one of:

- `"provider"`
- `"estimated"`

Counted model calls for token metrics include main-agent model calls and
context compression model calls in the current invocation. They exclude provider
usage internal to brokered `view_image` calls; `view_image` remains accounted as
a brokered tool for timing and tool metrics.

When every counted model call in the invocation has provider usage,
`provider_usage_available` is `true`, `token_source` is `"provider"`, and
`input_tokens`, `output_tokens`, and `total_tokens` are cumulative
provider-usage counters for the current metrics invocation. They are sums across
counted model calls in the invocation and must not be the last-known per-call
values. `estimator_version` is `null`.

If a provider usage payload contains `input_tokens`/`prompt_tokens` and
`output_tokens`/`completion_tokens` but omits `total_tokens`, runtime may derive
that call's `total_tokens` as input plus output for cumulative accounting.

When provider usage is unavailable for any counted model call in the invocation,
including a mixed window where only some counted calls have provider usage,
`provider_usage_available` is `false`, `token_source` is `"estimated"`, and the
whole counted model-call window uses cumulative deterministic token estimates.
Runtime must not mix provider totals for some calls with estimates for others in
one metrics file. Estimated input tokens are derived from the provider-visible
request for each counted model call. Estimated output tokens are derived from
the accepted provider output for each completed counted model call. Estimated
`total_tokens` is the sum of estimated input and output tokens.
`estimator_version` identifies the deterministic estimator used.

Run metrics must not contain an `estimated_context_tokens` field. A latest model
call context estimate may remain a separate runtime/UI diagnostic, but it is not
a run metrics token field and must not be substituted for estimated
`input_tokens`, `output_tokens`, or `total_tokens`.

Existing REPL/TUI token surfaces follow the same cumulative provider-usage
semantics for `input_tokens`, `output_tokens`, and `total_tokens`. If provider
usage is absent, existing REPL/TUI token surfaces must show cumulative estimated
`input_tokens`, `output_tokens`, and `total_tokens` rather than latest-context
token counts.

Thinking tokens are counted only if the provider usage payload includes them in
its returned usage values. Runtime must not persist thinking content to compute
token usage.

Phase 4 run metrics must not add separate `reasoning_tokens`, `thinking_tokens`,
or equivalent reasoning-token fields. If the provider includes thinking cost in
ordinary `output_tokens` or `total_tokens`, those provider values are used as
returned. Runtime must not estimate reasoning tokens from stripped thinking
blocks.

Runtime must not compute business metrics such as token per valid report or
issue-location accuracy.

## Tool Stability Metrics

`successful_tool_calls` counts tool results with:

```text
status == "ok"
```

`failed_tool_calls` counts tool results with:

```text
status in ["error", "timeout", "denied", "cancelled"]
```

`failure_breakdown` groups failed calls by status.

`tool_success_rate` and `tool_failure_rate` are derived from total tool calls.
When there are no tool calls, both rates are `0.0`.

## Write Semantics

Metrics output must be valid UTF-8 JSON.

The writer must use same-directory temporary file plus atomic finalization or
atomic replace so the final path never exposes a partial JSON file.

Because filenames contain timestamps, normal operation should not overwrite an
existing metrics file. If a timestamp collision occurs, the writer must use the
deterministic suffix rule defined above. Atomic writing is still required.

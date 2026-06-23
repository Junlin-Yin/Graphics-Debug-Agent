# Run Metrics Failed Model Call Token Design

## Context

Phase 4 run metrics require cumulative token accounting for the current
invocation. When provider usage is unavailable, metrics must use deterministic
estimated input, output, and total token counts.

Current failed model-call paths can terminalize a prompt session after retries
without recording any model-call usage observation. The metrics writer then sees
an empty model-call window and writes zero token totals.

## Decision

Record failed model calls for metrics with deterministic estimated usage.

The implementation will add a metrics-only observation identifier to model-call
run events. This identifier is separate from durable conversation
`model_call_id` and provider tool-call ids.

## Event Fields

`model_call_started`, `model_call_failed`, and `model_call_completed` may carry:

- `purpose`: `main` or `compression`
- `turn_id`: current runtime turn id when available
- `model_call_observation_id`: invocation-local id for metrics correlation
- `estimated_usage`: deterministic estimate for the provider-visible request

For failed calls, estimated output tokens are `0`; estimated input tokens come
from the provider-visible request. The total is input plus output.

## Boundaries

This change must not alter:

- provider-visible tool call ids
- adapter-local tool-loop ids such as `model_call_1_tool_1`
- durable conversation `model_call_id` or `tool_call_id`
- resume projection
- checkpoint truth
- status or trace truth semantics

Metrics may consume the new event fields. Durable conversation and tool-loop
logic must ignore them.

## Retry Semantics

Each retry attempt gets a distinct `model_call_observation_id`, so failed
attempts accumulate rather than overwrite each other. A completed call consumes
or clears any pending estimate without double-counting.

## Testing

Add focused tests for:

- retry-exhausted model timeout terminalization writes estimated token metrics
  with `provider_usage_available=false`.
- token totals are greater than zero for failed provider-visible requests.
- output tokens are zero for failed calls with no accepted output.
- repeated retry failures accumulate estimated input usage.
- completed model calls are not double-counted by started/completed event pairs.
- durable conversation and tool-loop ids remain unchanged in existing tests.

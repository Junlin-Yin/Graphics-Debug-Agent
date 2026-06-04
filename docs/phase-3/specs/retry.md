# Phase 3 Retry Spec

## Purpose

Phase 3 adds narrow runtime-owned retry for explicitly retry-safe transient
failures and `output_token_limit_reached` continuation.

Retry is not generic step-level retry. It does not own final failure handling.

## Retry Rule Registry

Retry decisions must come from a central retry rule registry.

Each rule defines:

- normalized `error_class`.
- normalized `reason`.
- eligible source/module.
- strategy.
- maximum attempts.
- backoff, if any.
- whether the retry is safe with respect to side effects.
- audit metadata fields.

Call sites must not implement ad hoc retry loops with local reason strings.

## Strategies

Allowed Phase 3 strategies:

- `repeat_call`
- `continue_generation`

No other strategy is allowed in Phase 3.

## `repeat_call`

`repeat_call` may be used only for explicitly retry-safe runtime-owned
transient failures.

Requirements:

- the operation has no external side effect or is proven idempotent by the
  runtime-owned boundary.
- retry rule is registered centrally.
- attempts are bounded.
- each attempt is audited.
- final exhaustion returns to ordinary error handling.

`repeat_call` must not apply by default to:

- ordinary model-visible tools.
- shell commands.
- file writes.
- approval requests.
- accepted model-call results.
- completed tool results.

## `continue_generation`

`continue_generation` handles `model_error/output_token_limit_reached`.

Requirements:

- provider returned a completed response with a stop/finish reason that
  indicates output token limit.
- partial assistant output is not accepted as final.
- incomplete tool calls or incomplete tool arguments are not executed.
- continuation prompt/input is runtime-owned and based on the partial response
  plus durable context.
- successful continuation produces one accepted final assistant output or
  complete accepted assistant tool-call message.
- attempts are bounded and audited.

`continue_generation` is not:

- token-level resume.
- replay of an accepted/completed model-call result.
- tool-mid-flight resume.
- acceptance of partial output.

If continuation is exhausted or fails, ordinary error handling decides whether
the turn fails, session terminalizes, or a terminal checkpoint is written at a
later eligible terminalization boundary.

## Retry Metadata

Retry audit facts should include:

- rule id.
- strategy.
- attempt number.
- maximum attempts.
- source error class/reason.
- final exhausted flag.
- resulting error class/reason when failed.

Retry metadata may be visible in trace/status. It must not be included in the
model-visible error projection except as a concise message if needed.

## Disallowed Retry

Phase 3 forbids:

- generic step-level retry.
- default runtime-level automatic tool retry.
- shell command retry.
- tool replay after side effects.
- accepted or completed model-call result replay.
- token-level resume.
- tool-mid-flight resume.
- retry policy owned by prompt skills.

Skills may decide business behavior after seeing a model-visible failure fact,
but that is ordinary model/tool loop behavior, not runtime retry.

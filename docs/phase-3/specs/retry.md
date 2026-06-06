# Phase 3 Retry Spec

## Purpose

Phase 3 adds narrow runtime-owned retry for explicitly retry-safe transient
failures and `output_token_limit_reached` continuation.

Retry is not generic step-level retry. It does not own final failure handling.

## Retry Rule Registry

Retry decisions must come from a central retry rule registry.

Each rule is keyed by normalized `error_class/reason` and defines exactly:

- `enabled`.
- precondition.
- strategy.
- maximum attempts.
- backoff policy.
- backoff seconds when the policy is fixed.
- comment.

The Phase 3 `RetrySpec` shape is:

```text
RetrySpec:
  enabled: bool
  precondition: none | metadata_transient_true | text_only_no_tool_fragment | sqlite_no_partial_commit
  strategy: repeat_call | continue_generation
  max_attempts: int
  backoff: none | fixed
  backoff_seconds: int | null
  comment: str
```

`max_attempts` counts retry attempts after the initial failed or truncated
attempt. When `enabled = true`, `max_attempts` must be a positive integer and
the rule's `precondition` must be satisfied before the strategy is applicable.
When `enabled = false`, the rule is disabled regardless of any stored numeric
default. When `backoff = "none"`, `backoff_seconds` must be `null`. When
`backoff = "fixed"`, `backoff_seconds` must be a positive integer. Retry
budgets, backoff durations, and conditional applicability must be centralized in
this registry. Call sites may ask the registry for policy, provide the
normalized error metadata and operation-boundary facts needed to evaluate the
registered precondition, record attempt metadata, wait according to the
registered backoff, and execute the selected strategy; they must not define
independent retry counts, sleep durations, local rule ids, local boundary
fields, local metadata predicates, or ad hoc retry loops.

Allowed Phase 3 preconditions:

- `none`: the enabled rule is applicable when ordinary strategy safety
  requirements hold.
- `metadata_transient_true`: applicable only when normalized error metadata has
  `transient = true`.
- `text_only_no_tool_fragment`: applicable only when normalized error metadata
  has `partial_output_kind = "text_only_no_tool_fragment"`.
- `sqlite_no_partial_commit`: applicable only when the persistence boundary
  proves no partial commit occurred and the complete operation can be retried
  from the beginning inside the documented transaction boundary.

`RetryController`, not individual call sites, interprets these preconditions.
Call sites may report facts to the controller but must not independently decide
that a conditional rule is applicable.

Any normalized error without an explicit enabled rule defaults to
`enabled = false`. Disabled retry immediately returns to ordinary error
handling; retry policy does not decide final terminalization, tool-loop,
checkpoint, ownership, or user-guidance behavior.

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
- for persistence transaction retries, no partial commit has occurred and the
  complete store operation can be retried from the beginning inside the same
  documented transaction boundary.
- retry rule is registered centrally.
- attempts are bounded.
- each attempt is audited.
- final exhaustion returns to ordinary error handling.
- no model response has been accepted for the failed attempt.
- no successful model-call result has been written for the failed attempt.
- no downstream tool call has been executed based on the failed attempt.

If the failed attempt later produces a provider result after runtime has already
classified it as timed out, failed, or retry-abandoned, that late result belongs
to the old attempt only. Runtime must ignore it for accepted execution truth: it
must not append it to durable conversation, must not execute any tool call from
it, must not write it as accepted assistant output, and must not overwrite a
successful retry result.

`repeat_call` must not apply by default to:

- ordinary model-visible tools.
- shell commands.
- file writes.
- approval requests.
- accepted model-call results.
- completed tool results.

Phase 3 enables `repeat_call` only for the explicit default rules listed in
`Default Retry Rules`.

## `continue_generation`

`continue_generation` handles `model_error/output_token_limit_reached`.

Requirements:

- provider returned a completed response with a stop/finish reason that
  indicates output token limit.
- partial assistant output is not accepted as final.
- incomplete tool calls or incomplete tool arguments are not executed.
- continuation prompt/input is runtime-owned and based on the partial response
  plus durable context.
- the partial response may exist only as retry-attempt transient input or audit
  metadata.
- the partial response must not be appended to `conversation_messages`.
- successful continuation produces exactly one accepted final
  `assistant_output` message.
- attempts are bounded and audited.

Phase 3 continuation is text-only. Runtime may use `continue_generation` only
when the partial provider response contains plain assistant text and contains no
complete or partial tool-call fragment. If the partial response contains a tool
call name, tool call id, partial tool arguments, structured function-call data,
or any provider-specific tool-use fragment, runtime must not run continuation;
it must route the `model_error/output_token_limit_reached` failure to ordinary
error handling.

The continuation request is a runtime-owned ordinary model call built from the
same durable context cut plus transient retry input. The partial text may be
provided to that call only as transient retry input or audit metadata; it must
not be appended to `conversation_messages` before continuation succeeds. The
continuation instruction must be explicit that the model should continue
directly from the stopping point, without restarting, summarizing, or repeating
already produced text. The Phase 3 contract is the bounded text-only behavior in
this spec.

On success, runtime creates exactly one accepted `assistant_output`
conversation message by concatenating the partial text and continuation text in
order, after normalization. Intermediate partial text, continuation prompts,
and failed continuation outputs remain audit/retry facts only and are not
accepted durable conversation messages.

If the continuation response contains a tool call name, tool call id, partial
tool arguments, structured function-call data, or any provider-specific
tool-use fragment, runtime must not accept it as a successful continuation and
must route the failure to ordinary error handling.

Continuation normalization must be deterministic:

- preserve the partial text bytes after provider text normalization already used
  for ordinary accepted assistant text.
- normalize the continuation text through the same ordinary assistant-text
  normalization path.
- concatenate `partial_text + continuation_text` exactly once in that order.
- do not trim, summarize, deduplicate, or insert separator text unless the
  provider-normalized continuation text itself contains it.
- compute durable conversation `content_sha256` from the same canonical
  accepted assistant content representation used for ordinary assistant output.

`continue_generation` is not:

- token-level resume.
- replay of an accepted/completed model-call result.
- tool-mid-flight resume.
- acceptance of partial output.

If continuation is exhausted or fails, ordinary error handling decides whether
the turn fails, session terminalizes, or a terminal checkpoint is written at a
later eligible terminalization boundary.

## Default Retry Rules

Initial Phase 3 retry rules are:

```text
model_error/provider_timeout:
  enabled=true
  precondition=none
  strategy=repeat_call
  max_attempts=2
  backoff=none
  backoff_seconds=null
  comment="Provider or SDK timeout may be transient. Repeating the model call is allowed only before a response is accepted."

model_error/model_call_timeout:
  enabled=true
  precondition=none
  strategy=repeat_call
  max_attempts=2
  backoff=none
  backoff_seconds=null
  comment="Runtime model-call timeout may be transient. Repeating the model call is allowed only before any response has been accepted."

model_error/provider_rate_limited:
  enabled=true
  precondition=none
  strategy=repeat_call
  max_attempts=1
  backoff=fixed
  backoff_seconds=2
  comment="Provider rate limits may clear after a short fixed wait. Keep the budget low to avoid provider pressure."

model_error/provider_exception:
  enabled=true
  precondition=metadata_transient_true
  strategy=repeat_call
  max_attempts=1
  backoff=none
  backoff_seconds=null
  comment="Only explicitly transient provider transport failures are retryable."

model_error/compression_model_failed:
  enabled=true
  precondition=metadata_transient_true
  strategy=repeat_call
  max_attempts=1
  backoff=none
  backoff_seconds=null
  comment="Compression model transport failures can be retried once. Deterministic compression budget, input, or output validation failures are not retryable."

model_error/output_token_limit_reached:
  enabled=true
  precondition=text_only_no_tool_fragment
  strategy=continue_generation
  max_attempts=1
  backoff=none
  backoff_seconds=null
  comment="The provider returned a successful but incomplete assistant text response due to output token limit. One continuation call may complete it only when the partial output contains no complete or partial tool-use fragment."

persistence_error/sqlite_busy_timeout:
  enabled=true
  precondition=sqlite_no_partial_commit
  strategy=repeat_call
  max_attempts=3
  backoff=fixed
  backoff_seconds=1
  comment="SQLite busy timeouts are transient lock contention. Retry only inside the persistence transaction boundary with a short fixed wait."
```

These defaults are runtime truth for Phase 3 retry behavior. Tests must assert
the registry values directly, and runtime call sites must not duplicate numeric
budgets.

For `model_error/output_token_limit_reached`, the enabled registry rule is
applicable only when runtime metadata proves the partial provider response is
plain assistant text with no complete or partial tool-use fragment. If that
precondition is absent, false, or invalid, the retry controller must treat
continuation as disabled for that failure and return it to ordinary error
handling without accepting partial output.

## Retry Metadata

Retry audit facts must include:

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
- prompt skills registering, modifying, or extending runtime-owned retry
  rules.

Skills may decide business behavior after seeing a model-visible failure fact,
but that is ordinary model/tool loop behavior, not runtime retry.

# Phase 3 Test Plan

## Acceptance Criteria

Phase 3 acceptance requires:

- Phase 2 runtime databases are deleted and replaced with a fresh Phase 3
  database on startup before any legacy rows are interpreted.
- startup, `status`, `trace`, active ownership checks, and `resume` validate
  schema version before interpreting runtime truth.
- `status`, `trace`, and `resume` fail closed for legacy databases before
  interpreting runtime truth.
- normalized error payloads use fixed `error_class` and `reason` symbols.
- failure-class events carry normalized error objects at `payload.error`.
- model-visible errors expose only the narrow projection.
- semantic CLI exit codes are used at command boundaries.
- `conversation_messages` is append-only durable conversation truth.
- durable conversation rows carry explicit message group and model-call group
  fields needed for recovery and compression validation.
- durable conversation rows declare a single canonical content source and
  multi-row groups have a deterministic completeness source.
- process-local conversation is rebuilt as a projection.
- pending stream/model/tool/shell state is never appended as durable
  conversation.
- terminal recovery checkpoints are the only resume entrypoints.
- Phase 3 prompt execution does not write ordinary turn, context, error, or
  other non-terminal checkpoint/provenance records.
- `latest_checkpoint_id` points only to terminal recovery checkpoints.
- startup/config/schema failure after session/run creation writes audit facts
  but no terminal recovery checkpoint.
- resume rejects startup/config/schema failure sessions from a structured
  lifecycle or terminal metadata marker, not event replay.
- running `Ctrl+C` or `Esc` cancels only the active turn and returns REPL/TUI to
  input.
- running `Ctrl+C` or `Esc` does not terminalize session/run or release
  ownership.
- idle `Ctrl+C` or `Esc` terminalizes session/run, writes terminal recovery
  checkpoint, releases ownership, and exits.
- graceful `/exit` writes terminal recovery checkpoint when eligible and releases
  ownership.
- zero-message `/exit` and normal graceful shutdown use a canonical empty
  conversation fact cut and empty projection snapshot.
- explicit `debug-agent resume <session_id>` restores eligible terminalized
  REPL prompt session using the same session/run lineage.
- explicit `debug-agent resume <session_id>` restores eligible one-shot terminal
  prompt session into REPL using the same eligibility rules.
- no path other than explicit resume revives terminalized session/run lifecycle
  to `running`.
- provider cancellation preserves public adapter contract and reports uncertain
  remote-stop/billing status only as metadata.
- active `shell_exec` receives best-effort termination on running cancellation.
- shell mid-flight state is never resumable.
- stale running fail-close requires proven-stale evidence and user
  confirmation.
- stale fail-close fails closed for live owner, insufficient evidence, or
  missing confirmation.
- stale fail-close and normal ownership release are fenced by `owner_token`.
- retry decisions come from a central registry.
- `repeat_call` applies only to registered retry-safe runtime-owned transient
  failures.
- `output_token_limit_reached` continuation does not accept partial assistant
  output or execute incomplete tool calls.
- `output_token_limit_reached` continuation applies only to text-only partial
  output with no complete or partial tool-use fragment.
- shell timeout config distinguishes default and maximum timeout.
- `execution.cancellation_timeout_seconds` is frozen and validated as the local
  cleanup envelope for accepted running interruption.
- explicit `shell_exec.timeout_seconds` is validated against maximum and is not
  silently capped by default.

## Unit Tests

### Schema Compatibility

- fresh workspace initializes Phase 3 schema version.
- fresh workspace initializes `PRAGMA user_version = 3`.
- fresh workspace creation writes the Phase 3 schema user version before
  startup or active ownership code interprets any runtime truth rows.
- startup with missing schema version deletes the old runtime DB before session
  truth reads and creates a fresh Phase 3 schema.
- startup with Phase 0/0.5/1/2 schema versions deletes the old runtime DB
  before session truth reads and creates a fresh Phase 3 schema.
- startup does not migrate, reinterpret, preserve, or rewrite legacy rows into
  the fresh Phase 3 database.
- startup legacy reset deletes only `.sessions/runtime.db`; legacy artifact,
  log, trace, checkpoint-payload, or session subdirectories may remain on disk
  but are not interpreted as Phase 3 runtime truth.
- fresh Phase 3 database created after startup legacy reset contains no
  references to orphaned legacy artifact, log, trace, checkpoint-payload, or
  session files left under `.sessions/`.
- startup with unknown future schema version fails closed and does not delete
  the database.
- `status`, `trace`, and `resume` are never treated as startup legacy-reset
  paths and never delete a legacy database during CLI process initialization.
- `status`, `trace`, and `resume` do not create `.sessions/runtime.db` when it is
  missing.
- `status` with no runtime database returns a read-only no-session observation.
- `trace <session_id>` and `resume <session_id>` with no runtime database return
  lookup-not-found.
- `status`, `trace`, and `resume` with missing schema version fail closed before
  reading legacy rows.
- `status` fails closed before reading legacy rows.
- `trace` fails closed before reading legacy rows.
- `resume` fails closed before reading legacy rows.
- active ownership check during startup runs only after legacy DB reset or
  Phase 3 schema validation, and never interprets legacy owner rows.
- startup legacy reset message says the old runtime database was deleted and a
  fresh Phase 3 database was created.
- startup legacy reset message says legacy artifact/log/trace files may remain
  on disk but are not interpreted by fresh Phase 3 runtime.
- `status`, `trace`, and `resume` legacy failure messages instruct the user to
  start a new session or use a fresh workspace.

### Normalized Errors

- every constructed error validates against the central class/reason registry.
- unknown error class is rejected in tests/helpers.
- unknown reason for a class is rejected.
- `user_error/active_session_conflict` is rejected; active workspace ownership
  blockage uses `policy_error/workspace_owner_active`.
- deprecated cancellation reasons such as `tool_error/shell_cancelled` and
  `cancelled/shell_cancel_requested` are rejected.
- default `recoverability` values are asserted for representative startup,
  ownership, REPL turn, one-shot terminal, retryable, and cancellation errors.
- one-shot `compression_failed` and `context_limit_exceeded` recoverability
  depends on terminal checkpoint eligibility: eligible failures are
  `terminal_recoverable`, while failures before the first closed durable
  conversation cut are `terminal_non_resumable`.
- retryable model/persistence reasons required by the Phase 3 retry registry are
  present in the centralized reason registry and map to the documented default
  recoverability when their preconditions hold.
- `provider_timeout` is used for provider SDK/client/service timeout reports,
  while `model_call_timeout` is used for runtime-owned worker or call-budget
  timeout; tests must not use the two reasons interchangeably.
- model-visible invalid tool arguments and Todo Plan semantic validation map to
  `tool_error/tool_schema_invalid`, not `user_error`.
- malformed or locally invalid `view_image` calls, such as missing `paths`,
  empty `paths`, too many paths, invalid `query`, unsupported local image input,
  or validation failure before provider execution, map to
  `tool_error/tool_schema_invalid`, not `user_error`.
- `view_image` provider timeout maps to brokered tool timeout
  `tool_error/tool_execution_timeout`, with provider-layer details only in
  metadata.
- failure events write `payload.error`.
- model-visible projection omits `scope`, `recoverability`, `metadata`, retry
  policy, and provider internals.
- startup config failure maps to semantic exit code.
- startup policy failure maps to semantic exit code.
- startup persistence/schema failure maps to semantic exit code.
- CLI exit-code dispatch covers Phase 3 reason families for usage, lookup,
  startup config, startup policy, startup persistence, startup skill snapshot,
  startup model, active ownership, policy denial, context/model/tool,
  persistence read/write/transition, runtime invariant, resume ineligibility,
  UI/trace rendering, process interrupt, and generic execution failure.
- startup and resume active ownership blockage persist
  `policy_error/workspace_owner_active` and map to
  `ERROR_ACTIVE_SESSION_CONFLICT`.
- startup and resume stale fail-close confirmation unavailable persists
  `policy_error/workspace_owner_confirmation_unavailable` and maps to
  `ERROR_ACTIVE_SESSION_CONFLICT`.
- missing or invalid startup multimodal `view_image` config freezes
  `view_image` disabled and does not fail session startup.
- stale or direct valid `view_image` calls against frozen disabled tool
  availability return `config_error/tool_unavailable` and are not routed to the
  vision provider.
- malformed disabled `view_image` calls fail validation first with
  `tool_error/tool_schema_invalid`, while unknown tool names remain
  `tool_error/unknown_tool`.
- lookup missing maps to `ERROR_LOOKUP_NOT_FOUND`.
- process-level interrupt maps to `INTERRUPTED`.

### Durable Conversation

- accepted user input appends one durable conversation row.
- accepted assistant output appends durable conversation row only after complete
  authoritative result.
- accepted tool result appends durable conversation row only after ToolBroker
  normalization.
- durable conversation rows persist explicit `message_group_id`,
  `model_call_id`, `group_position`, and `group_status` fields.
- durable conversation schema includes a deterministic group completeness source
  such as `group_row_count` or an accepted closed-group marker.
- each durable conversation row declares exactly one canonical content source:
  inline `content_json` or artifact-backed `artifact_id`.
- accepted `conversation_messages` rows with `group_status = "open"` are
  invalid Phase 3 runtime truth and make fact-cut validation fail closed.
- every accepted durable `conversation_messages` row in a closed group carries
  `group_status = "closed"`; any implementation-internal open staging state is
  invisible to projection reads, fact-cut validation, terminal checkpoint
  creation, resume validation, compression grouping, status, and trace.
- closed multi-row groups validate by contiguous `group_position` plus the
  implementation's deterministic group row count metadata or accepted
  closed-group marker, never by exposing accepted `open` rows.
- terminal fact-cut validation rejects truncated, duplicate-position, or open
  message groups.
- accepted durable conversation appends commit only closed groups to
  `conversation_messages`; any implementation-internal open group staging is
  not visible as accepted durable conversation truth.
- accepted assistant tool calls and accepted tool results validate by
  `tool_call_id` pairing inside a closed model-call/tool-loop sequence.
- failure fact appends only after recovery boundary.
- cancellation fact appends only after recovery boundary.
- stream delta does not append durable conversation.
- stream-delta equality with final assistant output is required only for
  completed uncancelled accepted stream results.
- partial output from token-limit response does not append final assistant row.
- incomplete tool call does not append accepted assistant tool-call row.
- process-local conversation can be rebuilt from durable rows during explicit
  resume.
- process-local conversation is rebuilt from durable rows only during explicit
  resume.
- ordinary runtime drift between process-local conversation, projection state,
  and durable rows fails closed instead of silently rebuilding from durable
  rows.
- durable conversation fact cut checksum validates.
- empty durable conversation fact cut for eligible zero-message `/exit` or
  normal graceful shutdown validates with `highest_message_index = 0`,
  `message_count = 0`, and the canonical empty checksum.
- durable conversation row `content_sha256` uses canonical JSON bytes for inline
  content and verified artifact payload checksums for artifact-backed content.
- durable conversation fact-cut checksum uses only the documented canonical row
  fields ordered by `message_index`, and excludes physical row id,
  `accepted_at`, trace/status presentation fields, and implementation insertion
  order.
- checksum canonicalization preserves non-ASCII UTF-8 text, sorts JSON object
  keys, uses no insignificant whitespace, rejects unsupported scalar types such
  as NaN/Infinity, and fails closed for missing checksum inputs.
- conversation append commits message index allocation, message rows, and
  projection-state update atomically.
- failed conversation append leaves no `message_index` gap or projection
  reference to uncommitted messages.
- current conversation projection state is persisted and can be overwritten.
- terminal checkpoint freezes a projection snapshot copied from current
  projection state.
- projection snapshot checksum validates.
- projection snapshot checksum uses ordered message refs plus referenced
  `content_sha256` values, and excludes `projection_state_id`, `updated_at`,
  and `update_reason`.
- missing row invalidates fact cut or projection snapshot.
- artifact-backed row validates artifact reference and checksum.
- terminal checkpoint `payload_sha256` validates against canonical
  `payload_json` bytes with embedded checksum fields included as ordinary
  strings.
- Todo Plan checkpoint checksum uses `run_id`, `plan_version`, ordered item
  fields, and item metadata, and excludes `updated_at`.
- approval grant checksum validates canonical grant rows up to
  `grant_high_watermark`, and excludes wall-clock timestamps, approval prompt
  text, unsubmitted approval input, reusable grant secrets or tokens, and UI
  presentation fields.
- active skill and frozen snapshot references validate by stored snapshot ids
  and stored content hashes without re-reading current source/config/policy
  files.
- Phase 3 conversation and checkpoint JSON surfaces preserve non-ASCII text as
  UTF-8 outside documented checksum canonicalization inputs.

### Terminal Checkpoints

- idle terminalization writes `terminal_recovery` checkpoint.
- zero-message `/exit` writes a `terminal_recovery` checkpoint with an empty
  durable conversation fact cut and empty projection snapshot.
- zero-message normal graceful shutdown writes a `terminal_recovery` checkpoint
  with an empty durable conversation fact cut and empty projection snapshot.
- idle `Ctrl+C` or `Esc` before any user prompt appends a session-scoped
  `cancellation_fact`, then writes a `terminal_recovery` checkpoint with
  terminal status `failed`, terminal reason `user_cancel_idle`, and terminal
  error `cancelled/user_cancel_idle`.
- one-shot normal completion writes `terminal_recovery` checkpoint with terminal
  status `completed`, terminal reason `terminal_completion`, and no terminal
  error.
- `/exit` and normal graceful shutdown write terminal status `completed`,
  terminal reason `user_exit`, and no terminal error.
- idle `Ctrl+C` or `Esc` writes terminal status `failed`, terminal reason
  `user_cancel_idle`, and terminal error `cancelled/user_cancel_idle`.
- terminal prompt failure writes terminal status `failed`, terminal reason
  `terminal_failure`, and the normalized terminal failure fact.
- terminal prompt failure before the first closed accepted durable conversation
  group does not write a terminal recovery checkpoint.
- terminal prompt failure must not use the zero-message terminal checkpoint
  exception.
- terminal prompt failure after a closed accepted durable conversation cut
  writes a terminal recovery checkpoint when projection and runtime-owned state
  validation succeeds.
- stale fail-close writes terminal status `failed`, terminal reason
  `terminal_stale`, and no terminal error.
- terminal checkpoint creation failure does not set `latest_checkpoint_id` or
  present the session/run as resumable.
- terminal failure after accepted facts writes `terminal_recovery` checkpoint
  when eligible.
- running cancellation does not write terminal recovery checkpoint.
- turn-scoped failure does not write terminal recovery checkpoint by itself.
- context/compression failure does not write terminal recovery checkpoint by
  itself.
- ordinary turn success does not write a non-terminal `turn` checkpoint.
- turn-scoped failure does not write a non-terminal `error` checkpoint.
- context/compression success or failure does not write a non-terminal
  `context` checkpoint or `context_snapshot`.
- startup/config/schema failure writes no terminal recovery checkpoint.
- Phase 3 prompt execution never writes non-terminal checkpoint/provenance
  records.
- `latest_checkpoint_id` rejects non-terminal checkpoint kind.
- terminal manifest includes conversation fact cut, projection snapshot,
  Todo Plan, approval state, active skill runtime records and snapshot
  references, frozen config/policy references, and artifact references.
- invalid checkpoint checksum fails resume.
- invalid conversation fact cut fails resume.
- invalid projection snapshot fails resume.

### Session Control

- running `Ctrl+C` or `Esc` enters `cancelling`.
- running `Ctrl+C` or `Esc` writes `cancelled/user_cancel_running` fact.
- running `Ctrl+C` or `Esc` does not project `cancelled/user_cancel_running`
  into later provider prompts during ordinary execution or resume.
- running `Ctrl+C` or `Esc` returns REPL/TUI to input.
- running `Ctrl+C` or `Esc` leaves session/run lifecycle `running`.
- running `Ctrl+C` or `Esc` keeps active ownership.
- running `Ctrl+C` or `Esc` does not print a session close or cancelled terminal
  summary.
- idle `Ctrl+C` or `Esc` writes `cancelled/user_cancel_idle` fact.
- idle `Ctrl+C` or `Esc` does not project `cancelled/user_cancel_idle` into
  later provider prompts during resume.
- idle `Ctrl+C` or `Esc` terminalizes session/run.
- idle `Ctrl+C` or `Esc` releases active ownership.
- ownership release failure after durable terminalization records
  `runtime_error/ownership_release_failed`, does not roll back terminal facts,
  and leaves active ownership blocked.
- `/exit` terminalizes eligible idle session with terminal reason `user_exit`
  and releases ownership.
- normal graceful shutdown outside explicit `/exit` uses terminal reason
  `user_exit`.
- terminal prompt failure uses terminal reason `terminal_failure`.
- one-shot normal completion uses terminal reason `terminal_completion`.
- stale fail-close uses terminal reason `terminal_stale`.
- all user input, including `Ctrl+C` and `Esc`, is blocked while `cancelling`
  and does not accept partial state.

### Resume

- eligible terminalized REPL prompt session resumes into REPL.
- eligible one-shot terminal prompt session resumes into REPL.
- resume preserves `session_id`.
- resume preserves `run_id`.
- resume writes `session_resumed` and `run_resumed`.
- resume reacquires active ownership before lifecycle revival.
- resume records current owner `pid`, `host_id`, and fresh `owner_token`.
- resume restores conversation from checkpoint-frozen projection snapshot.
- resume restores Todo Plan for same run from checkpoint-embedded snapshot.
- resume Todo Plan restore does not increment plan version and does not emit
  `todo_updated` or a separate Todo restore event.
- resume Todo Plan restore overwrites only mutable current-row drift and fails
  closed when durable Todo Plan history, checkpoint snapshot, checksum, run
  ownership, plan version, item order, content, status, or active form does not
  validate.
- resume restores approval mode and session-scoped grants.
- resume restores active skill runtime records, including skill id,
  snapshot/content hash reference, activation reason, scope, and frozen resource
  references.
- resume uses frozen config, policy, tool availability, skill, and resource
  snapshots from the original session; current disk config or skill source edits
  do not change restored context.
- resume rejects running session.
- resume rejects idle non-terminal session.
- resume rejects startup/config/schema failure sessions using structured
  session/run lifecycle or terminal metadata, without inferring the marker from
  event replay, checkpoint payload text, trace output, or natural-language
  messages.
- resume targeting the current stale active owner follows the documented
  pre-validation branch: prove stale, confirm, owner-token fenced fail-close,
  require a valid terminal recovery checkpoint, then run ordinary resume
  validation.
- resume rejects startup/config/schema failure session.
- resume rejects missing checkpoint.
- resume maps unset `latest_checkpoint_id` to
  `runtime_error/resume_checkpoint_required`.
- resume maps missing checkpoint row to `persistence_error/checkpoint_missing`.
- resume rejects non-terminal checkpoint kind.
- resume rejects invalid checkpoint checksum.
- resume rejects invalid durable conversation fact cut.
- resume rejects invalid projection snapshot.
- store/API paths other than explicit resume reject terminal-to-running
  transition.

### Provider And Shell Cancellation

- adapter public contract remains `run()` / `stream()`.
- Phase 3 removes the older placeholder `AgentLoopAdapter.cancel(run_id)` API
  from the adapter protocol and concrete adapter implementation.
- implementation planning audits the concrete main-model adapter and
  `view_image` provider path before coding cancellation, and stops for a
  contract/provider-path decision if either path is sync-only and
  uncancellable.
- fake async adapter observes cancellation handle and returns normalized
  cancelled result.
- cancellation audit facts record interrupt requested, provider cancellation
  requested/observed with remote-stop uncertainty metadata, shell termination
  requested/result, turn cancellation fact, and whether REPL returned to input or
  exited interrupted.
- main model provider calls run through runtime-owned async provider tasks
  internally while preserving the public synchronous adapter `run()` /
  `stream()` contract.
- non-streaming authoritative main-model execution uses the configured
  provider async invocation API, such as `ainvoke`, when available.
- streaming observational main-model execution uses the configured provider
  async streaming API, such as `astream`, when available.
- one-shot/non-stream REPL coverage proves the authoritative `run()` path
  inherits async provider cancellation behavior.
- streaming REPL/TUI coverage proves stream output inherits async provider
  cancellation behavior.
- `view_image` provider calls run through runtime-owned async cancellable
  workers.
- streaming fallback to non-streaming provider invocation still runs the
  provider call through a runtime-owned async provider task when an async
  invocation API is available.
- sync `invoke()` / `stream()` wrapped in a worker is rejected as an accepted
  concrete main-model provider fallback when the provider exposes usable async
  APIs.
- late provider results after accepted cancellation are ignored.
- main model provider cancellation uses `cancelled/model_call_cancelled` with
  `scope = "provider"`.
- main model provider cancellation records `cancelled/model_call_cancelled` only
  as an internal/audit provider-boundary fact during running `Ctrl+C` or `Esc`;
  it does not append a separate durable conversation cancellation message.
- cancelled `view_image` returns model-visible
  `cancelled/tool_call_cancelled`.
- cancelled brokered tool observations use the existing `tool_call_failed`
  event kind with `payload.error.reason = "tool_call_cancelled"`.
- runtime does not claim remote provider stop or billing stop.
- stream tokens shown before cancellation are not accepted as final output.
- async stream cancellation does not drain late chunks into durable assistant
  output and closes or observes the local async provider boundary before the
  runtime accepts durable turn cancellation.
- late `view_image` provider results are not accepted as `ToolResult`, vision
  analysis, raw provider text, or provider response.
- cancelled `view_image` metadata preserves Phase 2 query redaction and contains
  no image bytes, base64 image content, or provider image content parts.
- `view_image` normalized error metadata, audit payloads, status, trace, and
  engine-log diagnostics preserve Phase 2 query redaction: no effective query
  text, raw `query`, query preview, or query length is persisted outside the
  assistant-authored tool-call transcript.
- active shell process receives best-effort terminate request.
- shell/tool cancellation returns normalized `cancelled/tool_call_cancelled`.
- running cancellation after an accepted assistant tool call first appends the
  cancelled/failed `tool_result` with the original `tool_call_id`, then appends
  the turn-scoped runtime `cancellation_fact`.
- running cancellation during partial unaccepted provider tool-use fragments does
  not append an `assistant_tool_call` or matching `tool_result`.
- partial shell output is included only after command-runner boundary closes.
- shell cancellation writes no terminal recovery checkpoint by itself.
- cancellation cleanup uses frozen
  `execution.cancellation_timeout_seconds`.
- REPL/TUI returns to input only after the local provider/tool/shell boundary
  closes and the durable cancellation/failure fact is accepted.
- cleanup that cannot close within the cancellation envelope exits/fails closed
  without accepting partial state or leaving hidden background runtime work.
- after cleanup cannot close within the cancellation envelope, later startup or
  resume remains blocked by the last durable active ownership facts unless
  user-confirmed stale fail-close or manual cleanup resolves the blockage.
- input while `cancelling`, including `Ctrl+C` and `Esc`, does not create a
  double-interrupt process-abort path, does not queue prompt/command/approval
  input, does not bypass the cleanup envelope, and does not accept partial
  provider/tool/shell state.

### Stale Fail-Close

- active owner that appears alive blocks startup/resume.
- host identity provider returns `host-v1:sha256(...)` from the documented
  platform machine-id source and does not persist raw machine id values.
- host identity provider unavailability makes stale proof fail closed.
- owner with different or missing `host_id` blocks startup/resume.
- same-host owner whose recorded pid no longer exists and whose `owner_token` is
  captured is proven stale.
- owner with missing recorded pid blocks startup/resume.
- owner with missing recorded `owner_token` blocks startup/resume.
- same-host owner with existing pid is not proven stale.
- insufficient stale evidence blocks startup/resume.
- non-interactive missing confirmation blocks startup/resume.
- interactive confirmed proven-stale owner can be terminalized.
- confirmed stale fail-close releases ownership.
- confirmed stale fail-close releases ownership only when the active owner row
  still matches the captured `session_id`, `run_id`, `host_id`, `pid`, and
  `owner_token`.
- stale fail-close with changed `owner_token` fails closed and does not
  terminalize or release the new owner.
- stale fail-close with changed `owner_token` leaves no resumable terminal
  checkpoint reference or `latest_checkpoint_id`.
- stale fail-close marks old session/run `failed` with terminal reason
  `terminal_stale`.
- stale fail-close writes one minimal `stale_fail_closed` run event.
- `stale_fail_closed` event includes only the redacted proof summary
  `host_match=true`, `pid_absent=true`, and `token_fenced=true`, and excludes
  raw `host_id`, raw `pid`, raw `owner_token`, process diagnostics, command
  line, and confirmation text.
- stale fail-close does not write a normalized error fact.
- stale fail-close does not append durable conversation failure or cancellation
  facts.
- status and trace render `terminal_stale` as administrative closure and do not
  require `stale_fail_closed` to carry `payload.error`.
- stale fail-close writes terminal checkpoint only when durable facts are
  sufficient and session is checkpoint-eligible.
- stale fail-close without sufficient durable facts writes no terminal
  checkpoint and clears `latest_checkpoint_id` so the old session is
  non-resumable.
- stale fail-close never attaches to stale session.
- stale fail-close never auto-resumes stale session during fail-close.
- stale fail-closed session with valid terminal checkpoint can later be
  explicitly resumed through `debug-agent resume <session_id>`.
- `debug-agent resume <session_id>` targeting the stale active owner itself may
  user-confirmed fail-close that target and continue ordinary resume validation
  only when a valid terminal checkpoint was produced and ownership was released.
- `debug-agent resume <session_id>` targeting the stale active owner itself
  fails closed after confirmed fail-close when no valid terminal checkpoint or
  recovery manifest exists.
- stale fail-close confirmation does not promise target resumability; if
  explicit resume of the target fails after administrative fail-close because no
  valid terminal recovery checkpoint exists, the user-facing resume error says
  the target session cannot be recovered.

### Retry And Output Token Continuation

- retry rule registry rejects unknown rule reason.
- retry rule registry rejects enabled rules with `max_attempts <= 0`.
- retry rule registry exposes the Phase 3 default `RetrySpec` values, including
  `backoff` and `backoff_seconds`, for
  `provider_timeout`, `model_call_timeout`, `provider_rate_limited`,
  `provider_exception`, `compression_model_failed`,
  `output_token_limit_reached`, and `sqlite_busy_timeout`.
- `output_token_limit_reached` retry is applicable only when runtime metadata
  proves `partial_output_kind = "text_only_no_tool_fragment"`.
- `output_token_limit_reached` without that metadata precondition routes to
  ordinary error handling instead of continuation.
- `sqlite_busy_timeout` retry is allowed only when no partial commit occurred
  and the complete persistence operation can be retried from the beginning
  inside the documented transaction boundary.
- runtime call sites do not duplicate retry `max_attempts` magic numbers outside
  the central registry.
- runtime call sites do not duplicate retry sleep durations outside the central
  registry.
- retry audit facts include strategy, attempt number, maximum attempts, source
  error class/reason, exhaustion flag, and resulting error class/reason when
  failed.
- late provider results from a timed-out, failed, or retry-abandoned repeat-call
  attempt are ignored and never become durable conversation, accepted assistant
  output, accepted tool calls, or replacements for successful retry results.
- unregistered ordinary tool failure is not retried.
- shell timeout is not retried by default.
- registered retry-safe transient runtime failure uses bounded `repeat_call`.
- retry exhaustion returns to ordinary error handling.
- output token limit maps to `model_error/output_token_limit_reached`.
- partial assistant output is not accepted before continuation succeeds.
- text-only partial output can use bounded `continue_generation`.
- partial output containing any complete or partial tool-call fragment does not
  use continuation.
- incomplete tool call from partial output is not executed.
- successful continuation appends one final accepted assistant output.
- continuation response containing any complete or partial tool-use fragment is
  not accepted as a successful continuation and routes to ordinary failure
  handling.
- successful continuation appends one final assistant output constructed from
  partial text plus continuation text, while partial text and continuation
  prompts remain outside durable conversation until success.
- successful continuation uses deterministic concatenation and computes the
  final durable conversation checksum from the canonical accepted assistant
  content.
- continuation attempts are bounded and audited.

### Shell Timeout

- omitted `shell_exec.timeout_seconds` uses configured default.
- explicit timeout below maximum is honored.
- explicit timeout above maximum is rejected.
- model-visible `shell_exec` schema describes the frozen session maximum, and
  resume does not change that maximum from current `config.toml`.
- explicit timeout above maximum maps to `tool_error/tool_schema_invalid`.
- explicit timeout is not silently capped by default.
- explicit timeout is not silently capped by maximum; values above maximum are
  rejected.
- Phase 3 shell timeout behavior replaces the Phase 1
  `min(requested_timeout_seconds, default_shell_timeout_seconds)` behavior.
- `[defaults].timeout_seconds` remains main model/provider call timeout when
  supported and is not reused as shell execution default.
- invalid default timeout fails startup config.
- invalid maximum timeout fails startup config.
- invalid `cancellation_timeout_seconds` fails startup config.
- shell timeout writes normalized `tool_error/tool_execution_timeout`.
- shell timeout best-effort terminates process.

## Integration Tests

- one-shot session completes enough accepted conversation, terminalizes, and
  resumes into REPL with same session/run lineage.
- one-shot prompt policy denial after accepted runtime facts terminalizes as
  `terminal_failure` and follows ordinary terminal-checkpoint resume eligibility.
- one-shot context/compression failure after accepted runtime facts follows the
  documented terminal failure checkpoint and resume eligibility behavior.
- REPL session with Todo Plan and approval grant terminalizes and resumes with
  both restored.
- running cancellation during fake model call returns to REPL input and later
  idle terminalization produces a resumable terminal checkpoint.
- running cancellation during fake `view_image` provider call cancels the async
  vision provider task, records cancellation fact, and does not persist partial
  analysis.
- running cancellation during fake shell command terminates the subprocess,
  records cancellation fact, and keeps session active.
- startup skill snapshot failure after session creation terminalizes without
  checkpoint and resume rejects it.
- legacy Phase 2 database is deleted and replaced during startup before
  ownership is interpreted, while status, trace, and resume fail closed before
  reading legacy rows.
- stale ownership conflict with insufficient proof blocks; with fixture-proven
  stale proof, captured `owner_token`, and confirmation, startup/resume proceeds
  after fenced fail-close.

## Manual Verification

Manual verification is required for TTY behaviors that are not reliably covered
by automated tests:

- running `Ctrl+C` and `Esc` while model call is visibly active.
- running `Ctrl+C` and `Esc` while `shell_exec` is visibly active.
- idle `Ctrl+C` and `Esc` terminalization and ownership release.
- `debug-agent resume <session_id>` into interactive REPL.
- stale fail-close confirmation prompt.
- input-lockout presentation while cancelling.

Manual verification must record:

- terminal application used.
- command sequence.
- expected result.
- observed result.
- session id and run id.
- relevant trace/status excerpts.
- any known limitation.

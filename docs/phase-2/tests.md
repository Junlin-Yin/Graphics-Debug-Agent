# Phase 2 Test Plan

## Acceptance Criteria

Phase 2 acceptance requires:

- `view_image` is exposed as a brokered model-visible tool when frozen
  multimodal config enables it.
- invalid or incomplete startup multimodal config disables `view_image` without
  failing the session or hiding other tools.
- `view_image` supports one to four authorized local PNG/JPEG path inputs.
- `view_image` rejects remote URL input.
- `view_image` rejects artifact id input.
- `view_image` rejects unsupported image inputs.
- `view_image` applies path policy, approval mode, and audit before reading
  local path bytes.
- `view_image` records MIME type, byte size, SHA-256, width, and height.
- `view_image` rejects images over the Phase 2 dimension or pixel budget.
- `view_image` rejects projected provider requests over 100,000,000 bytes.
- `view_image` calls the configured multimodal provider through the Phase 2
  `VisionModelClient` boundary.
- `view_image` requires provider text to parse as a JSON object with valid
  semantic fields.
- `view_image` returns the required structured observation fields.
- `view_image` does not create `ArtifactStore` records or files for source image
  bytes; only large textual provider output may use existing artifact rules.
- `view_image` does not place image bytes or base64 in ordinary conversation,
  run events, trace output, context snapshots, or engine log.
- `todo` is exposed as a brokered model-visible tool.
- Todo Plan persists as run-scoped runtime truth.
- Todo Plan mutation events are written.
- Todo Plan is injected into ordinary task `ModelContextFrame`.
- Todo Plan participates in token estimation.
- Todo Plan survives automatic compression and manual `/compress`.
- Todo Plan is not reconstructed from compression summary.
- Phase 1 runtime databases fail closed with the Phase 2 compatibility error.
- Phase 2 `status` and `trace` perform schema-version checks before reading
  runtime truth.

## Unit Tests

### `view_image` Tool Schema And Source Validation

- `view_image` appears in the model-visible tool list.
- `view_image` schema rejects unknown fields.
- `view_image` schema failures return `ToolResult.status = "denied"` with
  `error_class = "user_error"` and write `tool_call_denied`, matching Phase 1
  ToolBroker behavior.
- call with missing `paths` returns `user_error`.
- call with empty `paths` returns `user_error`.
- call with more than four paths returns `user_error`.
- call with an empty path string returns `user_error`.
- call with `artifact_id`, `local_path`, or other unsupported source fields
  returns `user_error`.
- bare string values that look like artifact ids are treated as local path
  candidates, not resolved as artifact ids.
- call with an empty or whitespace-only `query` returns `user_error`.
- local path ending in `.png` but missing PNG signature returns `tool_error`.
- local path ending in `.jpg` or `.jpeg` but missing JPEG signature returns
  `tool_error`.
- valid PNG or JPEG with uncommon extension is accepted only if local path
  policy allows it and byte signature/dimensions validate.
- remote `https://`, `http://`, `file://`, and `data:` URL-like strings return
  `tool_error` without provider call.
- directory input returns `tool_error`.
- missing file under an otherwise allowed path returns `tool_error`.
- path denied by builtin or user path policy returns `denied` with
  `policy_denied` before file bytes are read.
- if any path in a multi-image call fails validation or policy, the entire call
  fails without provider call.
- call with `query` longer than frozen `max_query_chars` after trimming returns
  `user_error`.
- reusable approval scope for `view_image` includes the tool name, read access,
  and ordered canonical image paths.
- reusable approval scope for `view_image` excludes `query`, query source, image
  metadata, hashes, provider, model, timeout, and request-size projection.

### Image Metadata

- valid PNG width and height are parsed deterministically.
- valid JPEG width and height are parsed deterministically.
- PNG and JPEG metadata parsing uses Pillow rather than extension-only checks or
  ad hoc header parsing.
- image with width greater than 4096 returns `tool_error` before provider call.
- image with height greater than 4096 returns `tool_error` before provider call.
- image with `width * height > 4096 * 2160` returns `tool_error` before provider
  call.
- byte size equals source byte length.
- SHA-256 is stable for identical bytes.
- display metadata is included in `ToolResult.output.metadata` once per input
  image in input order.
- SHA-256 and byte size are included in `ToolResult.metadata.images`.
- trace renders metadata without base64.

### Vision Provider Boundary

- missing multimodal provider, model, API key environment variable, or base URL
  environment variable at startup starts the session with `view_image` disabled.
- unsupported multimodal provider at startup starts the session with
  `view_image` disabled.
- unsupported multimodal model at startup starts the session with `view_image`
  disabled.
- invalid multimodal `timeout_seconds` at startup starts the session with
  `view_image` disabled.
- invalid multimodal `max_tokens` at startup starts the session with
  `view_image` disabled.
- invalid multimodal `max_query_chars` or `max_analysis_chars` at startup starts
  the session with `view_image` disabled.
- test-only fake `VisionModelClient` enablement is available only through test
  injection or fixtures, not through ordinary user config.
- disabled `view_image` is omitted from `ModelContextFrame.tool_schema_bindings`.
- disabled `view_image` is omitted from the model-visible tool list while `todo`
  and Phase 1 tools remain visible.
- a direct or stale `view_image` call while disabled returns `config_error`
  with `ToolResult.status = "denied"` and a `tool_call_denied` event, without
  invoking `ViewImageTool` or `VisionModelClient`.
- an unknown tool name is not classified as disabled `view_image` and keeps the
  existing Phase 1 unknown-tool denial behavior.
- disabled `view_image` records a no-secret disabled reason in
  `sessions.config_snapshot_json`.
- disabled `view_image` cannot be enabled by setting environment variables after
  session startup; a new session is required.
- required API key environment variable missing at `view_image` execution after a
  valid startup snapshot returns `config_error`.
- required base URL environment variable missing at `view_image` execution after
  a valid startup snapshot returns `config_error`.
- multimodal config facts, including `max_query_chars` and
  `max_analysis_chars`, are frozen into `sessions.config_snapshot_json` without
  secret values.
- provider timeout returns `ToolResult.status = "timeout"`.
- provider HTTP or SDK failure returns `model_error`.
- provider response that is not JSON object text returns `model_error`.
- provider JSON object missing non-empty `analysis` returns `model_error`.
- provider JSON object with `analysis` longer than frozen `max_analysis_chars`
  returns `model_error`.
- successful provider response normalizes into required fields:
  `analysis` and `metadata`.
- omitted `query` uses the runtime default effective query.
- assistant-provided non-empty `query` is included in the provider request as
  the effective query.
- assistant-authored raw `view_image` tool-call arguments may contain `query`.
- immediate tool-loop transcript may contain the raw `view_image` tool call with
  `query` when required by provider tool-call protocol.
- `ToolResult.metadata` does not include the concrete effective query text.
- trace and audit facts record `effective_query_source` as `default` or
  `assistant`.
- runtime-authored persisted `view_image` audit metadata, trace output, engine
  log entries, context snapshot metadata, and `ToolResult.metadata` do not
  include the concrete effective query text, raw `query` argument, query
  preview, or query length.
- provider request includes all images in the order supplied by `paths`.
- enabled `view_image` treats complete frozen multimodal configuration as the
  provider-egress contract and does not add a separate approval scope beyond the
  ordered canonical image path read scope.
- provider request uses one OpenAI-compatible Chat Completions user message with
  image `data:` URL content parts followed by one text instruction part.
- provider request uses JSON-object response format.
- provider request disables Kimi thinking through the SDK-supported
  `extra_body={"thinking": {"type": "disabled"}}` or equivalent request field.
- provider/model paths that cannot support JSON-object response format are not
  valid Phase 2 real multimodal execution paths.
- provider request uses byte-validated MIME types in image data URLs.
- projected Chat Completions request body over 100,000,000 bytes returns
  `tool_error` before provider call.
- projected Chat Completions request body size is measured from the compact
  UTF-8 JSON serialization of the provider wire-equivalent request body,
  including `model`, `messages`, `response_format`, `max_tokens`, image data URL
  content parts, the text instruction content part, and required
  provider-specific request fields after SDK request-extension merging, such as
  top-level `thinking`.
- projected Chat Completions request body size has a deterministic compact JSON
  golden or snapshot test covering `model`, `messages`, `response_format`,
  `max_tokens`, all image data URL content parts, the text instruction content
  part, and merged Kimi thinking-disable fields.
- provider response text is read from `completion.choices[0].message.content`.
- `VisionModelClient` uses non-streaming Chat Completions and does not emit
  model stream events for `view_image` provider deltas.
- `VisionModelClient` performs at most one provider request per `view_image`
  tool call and disables SDK/client implicit retry.
- `view_image` uses the normal ToolBroker timeout path with an effective timeout
  from frozen multimodal `timeout_seconds`.
- the same effective timeout is passed to the underlying Chat Completions call.
- frozen multimodal `max_tokens`, `max_query_chars`, and
  `max_analysis_chars` are read from config when provided and default when
  omitted.
- raw provider text is artifacted when it exceeds the existing large-output
  threshold.
- successful `view_image` calls do not create source-image artifact records or
  files.
- failed `view_image` calls do not create source-image artifact records or
  files.
- source-image artifact prevention is tested separately from large textual
  provider-output artifacting.
- image bytes and base64 are not written to ordinary conversation, events,
  trace, context snapshots, or engine log.

### Plan Tool Schema

- `todo` appears in the model-visible tool list.
- `todo` is an audit-only runtime-owned tool in every approval mode and does not
  request interactive approval.
- `todo` is the only new Phase 2 narrow exception to the generic
  `runtime_control` approval matrix.
- Plan tool auto-allow decisions do not write `approval_grants`,
  `approval_requested`, or `approval_decision_recorded` records.
- schema rejects unknown fields.
- `ToolBroker` schema validation covers object shape, required fields, field
  types, unknown fields, item count, and `status` enum.
- `TodoToolHandler` performs trim-based semantic validation for `content` and
  `activeForm` length and non-empty checks.
- schema and semantic validation failures return `ToolResult.status = "denied"`
  with `error_class = "user_error"` and write `tool_call_denied`, matching Phase
  1 ToolBroker behavior.
- `todo` accepts an empty item array as a whole-plan clear.
- `todo` rejects more than 20 items.
- `todo` rejects empty item content.
- `todo` rejects empty `activeForm` when provided.
- `todo` accepts `activeForm` on non-`in_progress` items and normalizes it away
  before persistence and output.
- `todo` rejects invalid statuses.
- `todo` rejects multiple `in_progress` items.
- failed plan mutations do not partially update persisted plan state.

### Todo Plan Persistence And Events

- successful `todo` persists current plan for the run.
- successful `todo` writes a `todo_updated` event.
- successful `todo` increments plan version.
- the first successful `todo` mutation reports `previous_plan_version = 0` and
  `plan_version = 1`.
- successful `todo` replacement and its `todo_updated` event commit atomically
  in the same SQLite transaction.
- successful `todo` preserves item ordering and assigns 1-based display indexes.
- successful `todo` returns structured output with `plan_version`, `item_count`,
  status counts, and ordered items.
- successful `todo` returns a compact `redacted_output` using `[o]`, `[>]`, and
  `[ ]` status markers.
- clearing the plan with `items=[]` persists an empty current plan and writes a
  `todo_updated` event.
- clearing the plan with `items=[]` returns structured zero counts and a compact
  empty rendering such as `Todo Plan vN: empty`.
- Todo Plan for one run is not visible to another run.
- Todo Plan remains available after process-local runtime objects are
  reconstructed from stores in the same Phase 2 process boundary.

### ModelContextFrame Injection

- `PromptComposer` injects current Todo Plan as `runtime_todo_plan`.
- `PromptComposer` injects `runtime_todo_plan` even when no plan exists, using
  `plan_version = 0`, `items = []`, and an explicit empty summary.
- `PromptComposer` injects an explicit empty summary after a persisted
  `todo(items=[])` clear, using the persisted plan version.
- injected Todo Plan is not appended to durable conversation history.
- injected Todo Plan appears after active skill context and before rolling
  summary and retained raw conversation.
- injected Todo Plan includes a runtime-owned instruction to call `todo` when
  task status changes or the current plan no longer matches the work.
- injected Todo Plan renders item `content` and `activeForm` as structured plan
  data, not as independent system instructions.
- provider materialization preserves the Todo Plan segment as delimited
  structured data so item text cannot masquerade as higher-priority runtime,
  skill, or user instructions.
- token estimation includes the injected Todo Plan segment.
- provider materialization includes the Todo Plan segment for ordinary task
  model calls.
- compression model calls do not include Todo Plan as independent runtime
  truth.

### Compression Survival

- manual `/compress` leaves current Todo Plan unchanged.
- automatic omission leaves current Todo Plan unchanged.
- automatic compression leaves current Todo Plan unchanged.
- after compression, the next ordinary `ModelContextFrame` injects Todo Plan
  from `TodoPlanStore`.
- compression summary text is not used to rebuild Todo Plan.
- `view_image` normalized textual observations may be compressed like ordinary
  tool results.
- raw image bytes are not compression input.

### Status And Trace

- `debug-agent status <session_id>` includes compact Todo Plan counts when a
  plan exists.
- `debug-agent status <session_id>` omits or shows empty Todo Plan consistently
  when no plan exists.
- `debug-agent trace <session_id>` shows `todo_updated` events.
- trace shows `view_image` metadata, provider/model, duration, status, and
  analysis summary.
- trace derives `view_image` audit facts from existing `tool_call_*` events, not
  from `view_image_*` events.
- trace does not show image base64.

### Compatibility

- fresh database is created with `PRAGMA user_version = 2`.
- database with `user_version = 0` fails closed before startup reads runtime
  truth.
- Phase 1 database with `user_version = 1` fails closed before startup reads
  runtime truth.
- unknown future `user_version` fails closed.
- `debug-agent status` fails closed on legacy database before reading session
  rows.
- `debug-agent trace` fails closed on legacy database before reading event rows.
- legacy-schema error includes guidance to move or remove `.sessions/` or use a
  fresh workspace.
- runtime does not migrate, delete, or rewrite legacy database files.

## Integration Tests

- one-shot prompt can call `todo`, receive the result, and continue with a model
  call that sees the injected Todo Plan.
- REPL session can call `todo`, `/compress`, then continue with Todo Plan still
  visible.
- REPL or one-shot can call `view_image` on a small valid PNG under authorized
  workspace path using a fake `VisionModelClient`.
- REPL or one-shot can call `view_image` on multiple valid PNG/JPEG paths under
  authorized workspace paths using a fake `VisionModelClient`.
- REPL or one-shot with invalid startup multimodal config can still call `todo`
  and continue ordinary model execution without `view_image` in tool bindings.
- policy-denied `view_image` call returns denial and lets long-lived REPL
  continue.
- `view_image` provider timeout returns timeout result and lets long-lived REPL
  continue.
- `trace` after a session with plan changes and `view_image` calls contains
  required facts and no base64.

## Manual Verification

Manual verification is required for TTY presentation details that are not
covered by automated tests:

- `/tools` lists `todo` and lists `view_image` only when enabled.
- `/tools` or status output shows a concise no-secret reason when `view_image`
  is disabled.
- approval prompt for `view_image(paths=[...])` displays readable targets.
- denial returns control to the input area without terminalizing the session.
- optional TUI Todo Plan summary, if implemented, matches persisted plan state,
  uses `[o]` for completed, `[>]` for in progress, and `[ ]` for pending, and
  does not become the source of truth.

Manual verification must record:

- terminal application used.
- command sequence.
- expected result.
- observed result.
- any known limitation.

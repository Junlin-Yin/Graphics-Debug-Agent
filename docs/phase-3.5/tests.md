# Phase 3.5 Test Plan

## Acceptance Criteria

Phase 3.5 acceptance requires:

- Phase 3.5 centralizes built-in constants into the documented settings modules
  without making constants configurable unless a config spec explicitly allows
  it.
- `[agent_loop].max_tool_call_iterations` defaults to `1000`, accepts configured
  positive integers, rejects invalid values, freezes into the session config
  snapshot, and is restored from that snapshot on resume.
- `[execution].default_tool_timeout_seconds` defaults to `30`, accepts
  configured positive integers, rejects invalid values, freezes into the session
  config snapshot, and is restored from that snapshot on resume.
- invalid Phase 3.5 `[agent_loop]` or `[execution]` config fails through the
  existing startup config boundary before runtime database bootstrap or startup
  legacy schema reset.
- `view_image` image count, image edge, image pixel, and request body limits
  remain fixed runtime constants and are not parsed from
  `[multimodal.defaults]`.
- fresh databases initialize SQLite `PRAGMA user_version = 4`.
- startup deletes legacy `< 4` `.sessions/runtime.db` before interpreting rows
  and creates a fresh Phase 3.5 database.
- startup deletes only `.sessions/runtime.db` and does not reference orphaned
  legacy files left under `.sessions/`.
- `status`, `trace`, and `resume` never reset or create runtime databases.
- `status`, `trace`, and `resume` fail closed for legacy, missing-version, and
  unknown future databases before interpreting runtime truth.
- `find_file` is model-visible.
- `search_text` uses `pattern`; `query` is rejected as an unknown field.
- `search_text` is line-oriented and rejects `multiline` as an unknown field.
- ToolBroker schema validation supports Phase 3.5 schema features and default
  injection.
- every Phase 3.5 native tool still passes through ToolBroker policy, approval,
  artifact, result normalization, and audit.
- `view_image` ordinary output path behavior remains Phase 2 display-path
  behavior.
- `view_image` approval, policy, and audit continue to use canonical paths and
  query redaction.
- pagination rules and hard maximums are enforced.
- portable glob supports `*`, `?`, `[...]`, and `**`.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- `find_file` and `search_text` enumerate only after root approval.
- `search_text` uses filtered `rg --json --files-from` and does not fallback to
  Python regex.
- missing `rg`, regex compile failure, and unknown `type` return
  `tool_error/tool_execution_failed`.
- `search_text.skipped_files.denied` exposes only aggregate denied count and no
  denied path names.
- `read_file` updates whole-file hash cache even for paginated reads.
- existing-file `edit_file` and overwrite `write_file` require stale-write
  guard.
- resume starts with empty file metadata cache and direct existing-file write
  fails until `read_file` runs.
- successful `shell_exec` returns structured shell metadata.
- `shell_exec` does not gain raw shell string, background, interactive, PTY, or
  long-running behavior.
- terminal recovery checkpoint `tool_availability` contains the Phase 3.5
  native tool contract marker and dynamic tool facts.
- Phase 3.5 does not add deterministic call/audit signature persistence.
- Phase 3.5 does not add per-tool schema/result hash persistence.
- `activate_skill`, `load_skill_resource`, and `todo` remain model-visible
  runtime-control tools under their earlier-phase availability rules.
- Phase 3.5 does not expand or tighten `activate_skill`,
  `load_skill_resource`, or `todo` schemas, target validation, behavior,
  persistence, checkpoint facts, or result contracts.
- terminalization generates `.sessions/<session_id>/logs/trace.md`.
- run-event observations and runtime diagnostics are written to
  `.sessions/<session_id>/logs/events.jsonl`.
- Phase 3.5 does not generate legacy `.sessions/<session_id>/trace.md` or
  `.sessions/<session_id>/logs/engine.log`.
- `trace.md` is a conversation transcript rendered from durable
  `conversation_messages`, not a runtime event timeline.
- `trace.md` contains rendered `user_input`, `assistant_output`,
  `assistant_tool_call`, paired `tool_result`, and runtime
  failure/cancellation facts.
- `trace.md` does not render ordinary run events, checkpoint internals,
  approval internals, context compression internals, or `context_summary` rows.
- `debug-agent trace <session_id>` fully rebuilds `logs/trace.md` from the
  database, may run for running sessions, does not claim ownership, and does not
  start, resume, terminalize, fail-close, run a model, or execute tools.
- automatic trace generation failure after terminal checkpoint success does not
  write runtime truth or audit events, does not affect terminalization or
  ownership release, and does not change the original workflow exit code.
- trace writes use same-directory temporary files and atomic replace so output
  is never partial or interleaved.
- `events.jsonl` keeps the same non-authoritative status as the old
  `engine.log` and is never used by `status`, `trace`, `resume`, checkpoint
  validation, or recovery as runtime truth.
- trace output keeps emoji/non-ASCII section headings with no ASCII-only
  variant.

## Unit Tests

### Configuration And Constants

- settings modules import successfully and expose the documented constant groups.
- config loading includes `agent_loop.max_tool_call_iterations = 1000` when
  `[agent_loop]` is absent.
- config loading accepts a configured positive integer for
  `agent_loop.max_tool_call_iterations`.
- config loading rejects `agent_loop.max_tool_call_iterations` when it is `0`,
  negative, non-integer, or boolean.
- config loading includes `execution.default_tool_timeout_seconds = 30` when the
  field is absent.
- config loading accepts a configured positive integer for
  `execution.default_tool_timeout_seconds`.
- config loading rejects `execution.default_tool_timeout_seconds` when it is `0`,
  negative, non-integer, or boolean.
- invalid Phase 3.5 `[agent_loop]` or `[execution]` config returns
  `config_error/invalid_runtime_config`.
- invalid Phase 3.5 config is returned before `.sessions/runtime.db` is opened,
  deleted, reset, created, or interpreted.
- fake-provider and real-provider config snapshot shapes both include
  `agent_loop` and the expanded `execution` object.
- resume uses frozen `agent_loop.max_tool_call_iterations` and
  `execution.default_tool_timeout_seconds` from the original session snapshot,
  not current `config.toml`.
- config loading does not parse `max_images`, `max_image_edge`,
  `max_image_pixels`, or `max_request_bytes` from `[multimodal.defaults]`.
- unknown `config.toml` keys do not become a new global fail-closed condition in
  Phase 3.5.

### Schema Compatibility

- fresh workspace initializes schema version 4.
- fresh workspace writes `PRAGMA user_version = 4`.
- startup with missing schema version deletes only `.sessions/runtime.db` before
  reading runtime truth.
- startup with schema version `0`, `1`, `2`, or `3` deletes only
  `.sessions/runtime.db` before reading runtime truth.
- startup legacy reset creates a fresh schema 4 database with no references to
  orphaned legacy artifacts, logs, traces, checkpoint payloads, or session
  directories.
- startup with unknown future schema version fails closed and does not delete
  the database.
- `status`, `trace`, and `resume` with missing database do not create one.
- `status` with missing database returns read-only no-session observation.
- `trace` and `resume` with missing database return lookup-not-found.
- `status`, `trace`, and `resume` fail closed for missing schema version before
  reading rows.
- `status`, `trace`, and `resume` fail closed for legacy `< 4` before reading
  rows.
- active ownership check during startup runs only after startup reset or schema
  validation.
- user-facing startup reset message mentions deletion of legacy runtime DB and
  ignored legacy files.
- read-only/recovery fail-closed messages instruct the user to move or delete
  `.sessions/` or use a fresh workspace.

### Tool Schema Validation

- `read_file`, `list_dir`, `find_file`, `search_text`, `write_file`,
  `edit_file`, and `shell_exec` schemas reject unknown fields.
- `activate_skill`, `load_skill_resource`, and `todo` ToolDefinitions match
  `specs/native-tools.md` and reject unknown fields under their existing
  contracts.
- boolean fields reject non-boolean values.
- enum fields reject unsupported values.
- integer `minimum` and `maximum` are enforced.
- default values are injected into normalized arguments before approval and
  audit.
- array `minItems` and `maxItems` are enforced for `view_image.paths` and
  `shell_exec.argv`.
- malformed tool calls return `tool_error/tool_schema_invalid`.
- `search_text.query` is rejected.
- `search_text.multiline` is rejected.

### Approval And Audit

- reusable approval scope for each Phase 3.5 tool matches the scope table in
  `specs/native-tools.md`.
- pagination parameters are excluded from reusable approval scope.
- `search_text.type` is included in reusable approval scope.
- `search_text.output_mode`, context settings, `fixed_strings`,
  `case_sensitive`, `glob`, and `include_hidden` are included in reusable
  approval scope.
- `view_image` reusable approval scope excludes query and query source.
- audit events include normalized or redacted arguments for started,
  completed, failed, and denied tool calls.
- audit events do not include a new deterministic call/audit signature field.
- `view_image` audit arguments record `effective_query_source` and do not record
  query text, query preview, or query length.

### Portable Glob

- `*` matches characters within one path segment and does not cross `/`.
- `?` matches one character within one path segment and does not cross `/`.
- `[...]` matches one character within one path segment.
- `**` as a complete path segment matches zero or more directory levels.
- `foo**bar` is not treated as globstar.
- brace expansion such as `{a,b}` returns `tool_error/tool_schema_invalid`.
- extglob such as `!(x)` returns `tool_error/tool_schema_invalid`.
- matching uses `/` separators independent of platform.
- `case_sensitive=false` performs matcher-level case folding.

### `find_file`

- path omitted searches `workspace_root`.
- path provided resolves to canonical root and must pass policy and approval
  before enumeration.
- denied roots fail before traversal.
- denied descendants are skipped without names or counts.
- hidden descendants are skipped by default.
- `include_hidden=true` includes dot-prefix files but not denied paths.
- returns files only.
- result sorting is canonical path ascending.
- pagination returns `total_returned`, `truncated`, and `next_offset`.
- `maxResults > 1000` returns `tool_error/tool_schema_invalid`.
- symlink directories are not recursively followed.
- symlink files whose resolved target escapes allowed scope are skipped.

### `read_file`

- reads UTF-8 text.
- invalid UTF-8 returns `tool_error/tool_execution_failed`.
- default `limit` is 2000.
- `limit > 2000` returns `tool_error/tool_schema_invalid`.
- `offset` is 0-based line number.
- `offset` beyond EOF succeeds with empty content and no next page.
- final line without newline counts as one line.
- output contains canonical `path`, `content`, `offset`, `limit`,
  `total_returned`, `truncated`, `next_offset`, whole-file `sha256`, and whole-
  file `bytes`.
- successful paginated read updates cache using whole-file raw byte hash.

### `list_dir`

- lists immediate children only.
- denied children are omitted without names or counts.
- hidden children are omitted by default.
- `include_hidden=true` includes dot-prefix children but not denied children.
- `ignore` applies to immediate child names.
- `ignore` supports literal, `*`, and `?`.
- `ignore` does not support recursive glob, brace expansion, or extglob.
- filtering order is deny -> hidden -> ignore -> sort -> pagination.
- sorting is entry name ascending, case-sensitive, no directory-first grouping.
- `limit > 1000` returns `tool_error/tool_schema_invalid`.
- output contains canonical `path`, `entries`, `offset`, `limit`,
  `total_returned`, `truncated`, and `next_offset`.

### `search_text`

- root approval occurs before candidate enumeration.
- candidate enumeration skips denied paths and counts them only in
  `skipped_files.denied`.
- hidden files are skipped by default and counted in `skipped_files.hidden`.
- `include_hidden=true` includes hidden files but not denied files.
- symlink directories are not recursively followed.
- symlink files whose resolved target escapes allowed scope are skipped.
- `glob` filters candidates relative to the search root.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- ripgrep is invoked with a files-from list containing only allowed candidates.
- ripgrep exit code `1` for no matches returns success empty result.
- missing `rg` returns `tool_error/tool_execution_failed`.
- regex compile failure returns `tool_error/tool_execution_failed`.
- unknown `type` returns `tool_error/tool_execution_failed`.
- `fixed_strings=true` performs literal search.
- `fixed_strings=false` performs ripgrep/Rust regex search.
- `context` conflicts with `before_context` or `after_context` and returns
  `tool_error/tool_schema_invalid`.
- `context=N` equals `before_context=N` and `after_context=N`.
- context lines do not count toward `maxResults` or `total_returned`.
- duplicate context lines in a file are de-duplicated.
- match lines win over context lines for the same line number.
- content output is sorted by canonical path then line number.
- `files_with_matches` output is sorted by canonical path.
- `count` output is sorted by canonical path and includes only count > 0.
- line previews over 4000 codepoints are truncated and mark
  `line_truncated=true`.
- only the active output-mode result field is present.
- `skipped_files` has exactly `denied`, `hidden`, `decode_error`, and `other`
  counters and no path names.

### `edit_file`

- `old_text` empty returns `tool_error/tool_schema_invalid`.
- nonexistent target returns `tool_error/tool_execution_failed`.
- default `replace_all=false` replaces only when exactly one match exists.
- zero matches returns `tool_error/tool_execution_failed`.
- multiple matches with `replace_all=false` returns
  `tool_error/tool_execution_failed`.
- `replace_all=true` replaces all non-overlapping matches from left to right.
- LF-normalized matching preserves dominant line endings on write-back.
- existing file edit without cache entry fails and requires `read_file`.
- stale cache mismatch fails and requires `read_file`.
- successful edit output contains canonical `path`, `replacements`, `bytes`,
  `sha256_before`, `sha256_after`, and `guard`.
- successful edit updates cache to new revision.

### `write_file`

- creates missing file when target path passes write policy and approval.
- creates missing parent directories when target path passes write policy and
  approval.
- creating a new file does not require pre-write cache entry.
- creating a new file returns `created=true`, `overwritten=false`,
  `sha256_before=null`, and `guard.used=false`.
- overwriting an existing file without cache entry fails and requires
  `read_file`.
- overwriting an existing empty file without cache entry fails and requires
  `read_file`.
- stale cache mismatch fails and requires `read_file`.
- successful overwrite output contains canonical `path`, `bytes`,
  `created=false`, `overwritten=true`, `sha256_before`, `sha256_after`, and
  `guard`.
- successful write updates cache to new revision.
- write failure is `tool_error/tool_execution_failed` and cannot report partial
  write as success.

### `shell_exec`

- schema still requires structured `argv`.
- raw shell string field `command` is rejected.
- alias fields `directory` and `description` are rejected.
- background, interactive, and PTY flags are rejected as unknown fields.
- generated schema includes frozen maximum timeout.
- omitted timeout uses frozen default.
- requested timeout greater than frozen maximum returns
  `tool_error/tool_schema_invalid`.
- successful output includes `argv`, canonical `cwd`, `stdout`, `stderr`,
  `returncode`, `signal`, and `duration_seconds`.
- `signal` is integer or `null`; normal exit and Windows use `null`.
- nonzero process exit returns `tool_error/shell_nonzero_exit`.

### `view_image`

- disabled `view_image` remains omitted from model-visible tool bindings.
- direct valid disabled call returns `config_error/tool_unavailable`.
- malformed disabled call validates first and returns
  `tool_error/tool_schema_invalid`.
- ordinary output still uses display path behavior.
- approval scope uses ordered canonical image paths.
- query validation follows frozen `max_query_chars`.
- non-string, whitespace-only, or over-limit query returns
  `tool_error/tool_schema_invalid`.
- runtime-authored audit, events JSONL, status, error metadata, and
  `ToolResult.metadata` do not contain query text or query length.
- assistant-authored `view_image.query` is allowed to appear only in
  `trace.md` when rendered from accepted raw tool-call arguments.

### Observability Trace Rendering

- terminalized sessions write `.sessions/<session_id>/logs/trace.md`.
- `.sessions/<session_id>/trace.md` is not created.
- trace top matter contains `# debug-agent conversation trace` and an exported
  UTC ISO timestamp.
- trace top matter does not contain hidden HTML metadata or stale-detection
  fields such as `terminal_checkpoint_id`, `event_count`, or `latest_event_id`.
- session summary includes session id, run id, workspace, status, terminal
  reason, start/update timestamps, approval mode, total messages, total user
  messages, total assistant messages, and total tool calls.
- session summary excludes trace source, events log path, raw event count,
  latest event id, checkpoint validation internals, approval grant internals,
  and context compression internals.
- `Total Messages` counts rendered durable conversation rows and excludes
  filtered `context_summary` rows.
- `Total Tool Calls` counts tool-call items and not `tool_result` rows.
- trace body ordering follows `conversation_messages.message_index ASC`.
- trace renders `## 👤 User`, `## 🤖 Assistant`, `### 🔧 Tool Calls`, and
  `## ⚠️ Runtime Fact` headings.
- no ASCII-only heading variant is produced.
- user and assistant content is rendered as-is without fencing, escaping, or
  Markdown sanitization.
- ordinary run event names such as `model_call_started`,
  `checkpoint_written`, `approval_requested`, and `context_optimized` do not
  appear as trace timeline sections.
- `context_summary` rows are filtered without an omitted-summary placeholder.
- model name and token counts are not reintroduced from model events.
- runtime failure and cancellation facts render normalized
  `error_class/reason` and message.

### Observability Validation

- trace rendering fails closed for unsupported role or kind.
- trace rendering fails closed for any accepted conversation row that is not in
  a closed group.
- trace rendering fails closed for duplicate or non-contiguous group positions.
- trace rendering fails closed when group completeness source is missing.
- trace rendering pairs tool calls and tool results by
  `model_call_id + tool_call_id`.
- trace rendering fails closed when `model_call_id` is missing for a tool-call
  sequence.
- trace rendering fails closed for mismatched `model_call_id` between tool call
  and tool result.
- trace rendering fails closed for missing, duplicate, cross-sequence, or
  orphan tool results.
- trace rendering fails closed for artifact-backed rows whose artifact source
  is missing, conflicting, or checksum-invalid.
- durable conversation validation failure returns
  `persistence_error/conversation_cut_invalid` and does not overwrite an
  existing `logs/trace.md`.

### Tool Result Preview

- tool result previews over 4000 characters are truncated and marked
  `[truncated]`.
- tool result previews over 100 lines are truncated and marked `[truncated]`.
- JSON/dict tool outputs are pretty-printed before preview limits are applied.
- text previews apply limits to original text.
- preview truncation does not modify durable conversation content.
- artifact-backed results do not read artifact content while rendering trace.
- artifact-backed results display only `artifact_id` and relative artifact path.
- artifact-backed results do not display checksum or additional verification
  hints.
- a durable redacted inline preview may be displayed without reading the
  unredacted artifact.

### Trace Generation And Failure Handling

- terminal checkpoint success triggers full trace rebuild from durable
  conversation rows.
- terminal trace refresh overwrites `logs/trace.md` and never appends.
- manual `debug-agent trace <session_id>` overwrites `logs/trace.md` and never
  appends.
- trace generation does not use old trace file contents, Markdown
  high-watermarks, or hidden metadata.
- trace writes use a temporary file in the same `logs/` directory followed by
  atomic replace.
- concurrent automatic and manual trace writes produce one complete last-writer
  result, never interleaved or partial output.
- trace render/write failure returns `ui_error/trace_render_failed`.
- manual trace render/write failure after lookup succeeds maps to
  `ERROR_TRACE_RENDER = 11`.
- automatic trace failure after terminal checkpoint success does not roll back
  checkpoint creation, block terminalization, block ownership release, write
  runtime truth, write audit/run events, or change the original workflow exit
  code.
- `debug-agent trace <session_id>` may run against a running session and renders
  only accepted closed durable conversation rows.
- `debug-agent trace <session_id>` does not include mid-flight model, tool,
  provider, or shell state.
- `debug-agent trace <session_id>` does not claim ownership and is not blocked
  solely by an active runner.
- SQLite busy/read failure during manual trace returns a standardized error
  without waiting, taking ownership, or changing active owner state.
- missing session or missing database uses lookup-not-found behavior for
  manual trace.

### Events JSONL

- `.sessions/<session_id>/logs/events.jsonl` is written instead of
  `.sessions/<session_id>/logs/engine.log`.
- legacy `engine.log` is not created, migrated, copied, or symlinked.
- JSONL entry schema remains unchanged from `engine.log`.
- the JSONL writer is named `EventsJsonlWriter`; `EngineLogWriter` is not kept
  as the canonical writer class or test target.
- `write_event_log` entries include `metadata.event_id` for persisted run-event
  observations.
- `write_runtime_log` entries are runtime diagnostic observations and may lack
  `metadata.event_id`.
- `status`, `trace`, `resume`, checkpoint validation, and recovery do not read
  JSONL to reconstruct runtime truth.

### Terminal Recovery Tool Availability

- terminal recovery checkpoint includes
  `tool_availability.native_tools_contract.phase = "3.5"`.
- terminal recovery checkpoint includes
  `tool_availability.native_tools_contract.contract_marker =
  "phase-3.5-native-tools-v1"`.
- terminal recovery checkpoint includes shell maximum timeout from frozen
  config.
- terminal recovery checkpoint includes `view_image` enabled/disabled state and
  limits from frozen config.
- terminal recovery checkpoint `tool_availability` does not include
  `agent_loop.max_tool_call_iterations`.
- terminal recovery checkpoint `tool_availability` does not include
  `execution.default_tool_timeout_seconds`.
- checkpoint validation recomputes Phase 3.5 `tool_availability` from frozen
  session config and rejects mismatch.
- no per-tool input schema hash is persisted.
- no per-tool result contract hash is persisted.

### Existing Runtime-Control Tools

- `activate_skill` remains available as a model-visible runtime-control tool.
- `load_skill_resource` remains available as a model-visible runtime-control
  tool.
- `todo` remains available as a model-visible runtime-control tool.
- Phase 3.5 shared schema-validator changes enforce the existing
  `activate_skill`, `load_skill_resource`, and `todo` schemas without adding new
  fields or changing target validation.
- `todo` continues to use Phase 2 TodoPlanStore persistence and Phase 3
  normalized validation errors; Phase 3.5 does not add Todo Plan persistence
  fields, checkpoint facts, or result fields.
- `activate_skill` and `load_skill_resource` continue to use frozen skill
  snapshots, active skill records, and resource snapshots as defined by earlier
  phases; Phase 3.5 does not add skill persistence fields, checkpoint facts, or
  result fields.

## Integration Tests

- one-shot session can call `find_file`, `read_file`, `search_text`, and
  `list_dir` and persist normalized audit events.
- one-shot session still exposes `activate_skill`, `load_skill_resource`, and
  `todo` alongside the Phase 3.5 native tool changes.
- one-shot session can `read_file` then `edit_file` the same existing file.
- one-shot session cannot overwrite an existing file without prior `read_file`.
- REPL resume after terminalization starts with empty file metadata cache and
  cannot immediately overwrite an existing file until `read_file`.
- stale legacy Phase 3 database is reset on startup and fail-closed on
  `status`, `trace`, and `resume`.
- `search_text` over a tree with hidden files, denied files, symlinks, and
  UTF-8 decode failures produces documented counters and no denied path names.
- one-shot terminalization writes `logs/trace.md` and `logs/events.jsonl`, and
  does not write legacy root `trace.md` or `logs/engine.log`.
- one-shot trace renders user, assistant, tool-call arguments, paired tool
  result preview, and final assistant response from durable conversation rows.
- REPL terminalization, resume, additional conversation, and second
  terminalization rebuild a complete trace containing pre-resume and post-resume
  accepted messages exactly once.
- manual `debug-agent trace <session_id>` against a running REPL rebuilds trace
  from accepted closed rows without claiming ownership or affecting the runner.
- a session with context compression renders the original accepted messages,
  filters `context_summary`, and does not show an omitted-summary placeholder.
- `view_image.query` appears in trace only as assistant-authored raw tool-call
  args, while runtime-authored audit metadata, events JSONL, status, error
  metadata, and `ToolResult.metadata` keep query redaction.

## Manual Verification

Manual verification is useful for human readability:

- inspect `trace <session_id>` for Phase 3.5 conversation readability,
  tool-call arguments, result previews, pagination metadata, guard status, and
  runtime facts.
- confirm `view_image.query` is visible only in assistant-authored trace tool
  args and not in runtime-authored metadata.
- confirm `events.jsonl` has the same diagnostic usefulness as the old
  `engine.log`.
- confirm shell successful output and nonzero failure presentation are clear in
  REPL/TUI.

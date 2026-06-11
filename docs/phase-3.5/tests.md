# Phase 3.5 Test Plan

## Acceptance Criteria

Phase 3.5 acceptance requires:

- Phase 3.5 centralizes built-in constants into the documented settings modules
  without making constants configurable unless a config spec explicitly allows
  it.
- `[agent_loop].max_tool_call_iterations` defaults to `1000`, accepts configured
  positive integers, rejects invalid values, has no Phase 3.5 hard maximum,
  documents the local-user risk of extremely large values, freezes into the
  session config snapshot, and is restored from that snapshot on resume.
- `[execution].default_tool_timeout_seconds` defaults to `30`, accepts
  configured positive integers, rejects invalid values, has no Phase 3.5 hard
  maximum, documents the local-user risk of extremely large values, freezes into
  the session config snapshot, and is restored from that snapshot on resume.
- invalid Phase 3.5 `[agent_loop]` or `[execution]` config fails through the
  existing startup config boundary before runtime database bootstrap or startup
  legacy schema reset.
- `view_image` image count, image edge, image pixel, and request body limits
  remain fixed runtime constants and are not parsed from
  `[multimodal.defaults]`.
- fresh databases initialize SQLite `PRAGMA user_version = 4`.
- startup deletes legacy `< 4` `.sessions/runtime.db` before interpreting rows
  and creates a fresh Phase 3.5 database.
- startup deletes `.sessions/runtime.db` plus SQLite sidecar files and does not
  reference orphaned legacy files left under `.sessions/`.
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
- present path strings for model-visible tools reject empty or whitespace-only
  values with `tool_error/tool_schema_invalid`; tools that explicitly allow an
  omitted `path` still default to `workspace_root`, non-empty path strings are
  trimmed before canonicalization, and `"."` remains valid.
- successful Phase 3.5 native tools keep their documented structured result in
  `ToolResult.output`; provider-visible and durable `tool_result` conversation
  content is derived from `ToolResult.output`, while `redacted_output` remains
  presentation-only.
- Phase 3.5 accepts exactly these `ToolResult.status` values in model-visible
  and durable tool-result serialization: `ok`, `error`, `denied`, `timeout`, and
  `cancelled`.
- Phase 3.5 durable tool-result serialization follows the documented matrix for
  inline success, field-level artifact success, native observation still too
  large after field-level artifacting, and non-success statuses.
- Phase 3.5 large-output artifacting inherits the existing ToolBroker,
  ArtifactStore, and durable conversation mechanisms and does not add a new
  durable conversation row shape.
- Phase 3.5 large-output artifacting for successful native-tool results applies
  the deterministic field-level artifacting pass for the documented large
  native-tool output fields. If the full native-tool observation is still too
  large afterward, the tool call returns `tool_error/tool_execution_failed`
  instead of using row-level artifact-backed conversation fallback.
- Phase 3.5 field-level artifacting reuses the existing ToolBroker
  inline/artifact threshold and does not add a separate field-level threshold or
  config key.
- field-level artifacting is triggered by the complete native-tool
  model-visible observation exceeding the durable conversation inline threshold;
  ToolBroker externalizes eligible fields in stable documented field order until
  the observation fits inline or no eligible fields remain.
- when `read_file.content` is field-level artifacted, `path`, `offset`, `limit`,
  `total_returned`, `truncated`, `next_offset`, `sha256`, and `bytes` remain
  inline in the structured result.
- when `shell_exec.stdout` or `shell_exec.stderr` is field-level artifacted, the
  other shell metadata fields remain inline and stdout/stderr are externalized
  independently. If the full native-tool observation remains too large after
  field-level artifacting, the call returns `tool_error/tool_execution_failed`.
- `view_image` ordinary output path behavior remains Phase 2 display-path
  behavior.
- `view_image` approval, policy, and audit continue to use canonical paths and
  query redaction.
- pagination rules and hard maximums are enforced.
- portable glob supports `*`, `?`, `[...]`, and `**`.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- backslash in a portable glob pattern returns
  `tool_error/tool_schema_invalid`.
- `find_file` and `search_text` enumerate only after root approval.
- symlink file policy checks use the resolved target, while successful
  `find_file` and `search_text` results return, sort, paginate, and de-duplicate
  by the absolute normalized symlink candidate path that matched under the
  approved root.
- `search_text` uses runtime-filtered candidate files, invokes ripgrep with
  `shell=False` argv, and does not fallback to Python regex.
- `search_text` invokes ripgrep with `--no-config`, prevents
  `RIPGREP_CONFIG_PATH` from changing child-process semantics, and produces
  pagination independent of ripgrep discovery order.
- `search_text` performs regex pattern validation through ripgrep only when
  `fixed_strings=false`; `fixed_strings=true` still requires `rg` availability
  but does not perform a regex compilation check.
- `search_text.type` uses the Phase 3.5 runtime-owned text type allowlist and
  does not inspect local ripgrep type registries or custom type definitions.
- `search_text.type` file-family matching is case-insensitive over candidate
  relative paths and does not change content match case sensitivity.
- `search_text` content and count modes are line-oriented: multiple matches on the
  same line count once.
- unknown `search_text.type` returns `tool_error/tool_schema_invalid`.
- missing `rg` and regex compile failure return
  `tool_error/tool_execution_failed`, including when the filtered candidate set is
  empty after root approval.
- `search_text.skipped_files.denied` exposes only aggregate denied file-leaf
  count and no denied path names.
- `read_file` updates whole-file hash cache even for paginated reads.
- `read_file` whole-file hash calculation, line pagination, `search_text`
  candidate enumeration, UTF-8 pre-screening, type filtering, and ripgrep JSON
  parsing use streaming or bounded-memory implementation techniques under the
  ToolBroker timeout envelope.
- ToolBroker timeout measurement starts after interactive approval and before
  handler/traversal/provider/command work; it includes ArtifactStore
  registration and artifact writes caused by large tool output, and excludes
  approval wait, audit emission, and final result envelope formatting.
- `write_file` timeout handling is cooperative: runtime checks the deadline
  before observable phases it controls, and failed or timed-out calls still emit
  side-effect audit for known parent directories created before failure.
- artifact writes use a temporary file plus atomic finalization before accepting
  ArtifactStore truth; timeout, cancellation, or artifact registration failure
  must not expose an artifact id, append `artifact_ids`, or persist accepted
  conversation content that references an incomplete artifact.
- Phase 3.5 does not add a detailed native-tool failure audit schema. Non-success
  observations keep the Phase 3 model-visible error projection; only completed
  diagnostic artifacts may be exposed through `artifact_ids`; diagnostic
  artifacts remain optional.
- failed or timed-out `write_file` calls that already created parent directories
  record the minimal known side-effect facts in audit:
  `side_effects.created_directories`, `file_write_completed=false`, and
  `cache_updated=false`.
- successful Phase 3.5 native-tool `tool_result` rows never use row-level
  artifact-backed conversation fallback; field-level artifacting must leave an
  inline durable `tool_result.content_json`, or the call returns
  `tool_error/tool_execution_failed`.
- timeout during generic text tool traversal/search/read returns
  `tool_error/tool_execution_timeout`, does not return partial success, and does
  not update the file metadata cache.
- existing-file `edit_file` and overwrite `write_file` require stale-write
  guard.
- after stale-write guard succeeds, `edit_file` and overwrite `write_file` write
  complete content through a same-directory temporary file followed by atomic
  target replace; Phase 3.5 does not require crash-consistency or fsync-grade
  durability.
- resume starts with empty file metadata cache and direct existing-file write
  fails until `read_file` runs.
- successful `shell_exec` returns structured shell metadata.
- `shell_exec` does not gain raw shell string, background, interactive, PTY, or
  long-running behavior.
- terminal recovery checkpoint tool-availability facts contain the Phase 3.5
  native tool contract marker and dynamic tool facts.
- terminal recovery checkpoint payloads use `manifest_schema_version = 2`.
- Phase 3.5 does not add deterministic call/audit signature persistence.
- Phase 3.5 does not add per-tool schema/result hash persistence.
- `activate_skill`, `load_skill_resource`, and `todo` remain model-visible
  runtime-control tools under their earlier-phase availability rules.
- Phase 3.5 does not expand or tighten `activate_skill`,
  `load_skill_resource`, or `todo` schemas, target validation, behavior,
  persistence, checkpoint facts, or tool-specific logical result objects.
- Phase 3.5 runtime-control tool `ToolResult` envelopes, statuses, and
  normalized error projections follow the Phase 3/3.5 ToolBroker boundary:
  schema, local semantic, invalid target/config, and persistence failures use
  `status="error"`; policy or interactive approval denials use
  `status="denied"` when applicable.
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
- `trace.md` does not render standalone `todo_updated`, `stale_fail_closed`,
  `session_resumed`, `run_resumed`, checkpoint, resume, or admin timeline events;
  those remain in `events.jsonl` when written there.
- `debug-agent trace <session_id>` fully rebuilds `logs/trace.md` from the
  database, may run for running sessions, does not claim ownership, and does not
  start, resume, terminalize, fail-close, run a model, or execute tools.
- automatic trace generation failure after terminal checkpoint success does not
  write runtime truth, audit events, run events, or `events.jsonl`, does not
  affect terminalization or ownership release, and does not change the original
  workflow exit code.
- trace writes use unique same-directory temporary files and atomic replace so
  output is never partial or interleaved.
- `events.jsonl` keeps the same non-authoritative status as the old
  `engine.log` and is never used by `status`, `trace`, `resume`, checkpoint
  validation, or recovery as runtime truth.
- trace output keeps emoji/non-ASCII section headings with no ASCII-only
  variant and is verified as UTF-8 output.
- trace rendering never wraps tool arguments, tool results, redacted argument
  objects, or artifact reference previews in Markdown fenced code blocks; it
  renders those previews as indented plain preview blocks.

## Unit Tests

### Configuration And Constants

- settings modules import successfully and expose the documented constant groups.
- config loading includes `agent_loop.max_tool_call_iterations = 1000` when
  `[agent_loop]` is absent.
- config loading accepts a configured positive integer for
  `agent_loop.max_tool_call_iterations`.
- config loading accepts very large positive
  `agent_loop.max_tool_call_iterations` values without applying a Phase 3.5 hard
  cap.
- config loading rejects `agent_loop.max_tool_call_iterations` when it is `0`,
  negative, non-integer, or boolean.
- config loading includes `execution.default_tool_timeout_seconds = 30` when the
  field is absent.
- config loading accepts a configured positive integer for
  `execution.default_tool_timeout_seconds`.
- config loading accepts very large positive
  `execution.default_tool_timeout_seconds` values without applying a Phase 3.5
  hard cap.
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
- startup with missing schema version deletes `.sessions/runtime.db` plus
  `.sessions/runtime.db-wal` and `.sessions/runtime.db-shm` when present before
  reading runtime truth.
- startup with schema version `0`, `1`, `2`, or `3` deletes
  `.sessions/runtime.db` plus `.sessions/runtime.db-wal` and
  `.sessions/runtime.db-shm` when present before reading runtime truth.
- startup with corrupt or unreadable `.sessions/runtime.db` fails closed with
  `persistence_error/persistence_read_failed` and does not reset the database.
- startup legacy reset creates a fresh schema 4 database with no references to
  orphaned legacy artifacts, logs, traces, checkpoint payloads, or session
  directories.
- startup legacy reset fails closed with
  `persistence_error/persistence_write_failed` if a fresh Phase 3.5
  session/log/artifact/checkpoint-payload/temp path would collide with an
  orphaned legacy file or directory.
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
- `read_file.path`, `list_dir.path`, `write_file.path`, `edit_file.path`,
  `shell_exec.cwd`, explicit `find_file.path`, and explicit `search_text.path`
  reject empty or whitespace-only strings.
- omitted `find_file.path` and omitted `search_text.path` search the single
  `workspace_root`; explicit `"."` resolves to `workspace_root`.
- `load_skill_resource.path` remains a Phase 1 skill-local resource path, not a
  workspace-root path. Absolute paths, traversal outside the frozen resource set,
  and missing frozen resources continue to fail under the existing
  `load_skill_resource` target rules rather than the Phase 3.5 native filesystem
  path canonicalization rule.
- `activate_skill`, `load_skill_resource`, and `todo` ToolDefinitions match
  `specs/native-tools.md` and reject unknown fields under their existing
  contracts.
- malformed `activate_skill`, `load_skill_resource`, and `todo` calls return
  `ToolResult.status = "error"` with `tool_error/tool_schema_invalid`.
- invalid `activate_skill` or `load_skill_resource` frozen target/config
  failures return `ToolResult.status = "error"` with the appropriate normalized
  config or tool error projection.
- invalid `todo` local semantic validation failures return
  `ToolResult.status = "error"` with `tool_error/tool_schema_invalid`, while the
  Phase 2 audit-only approval exception remains unchanged.
- these runtime-control tool status/error expectations are the Phase 3 normalized
  ToolBroker boundary applied to unchanged earlier-phase tool behavior; Phase 3.5
  does not add new fields or target semantics for those tools.
- boolean fields reject non-boolean values.
- enum fields reject unsupported values.
- integer `minimum` and `maximum` are enforced.
- integer fields reject JSON boolean values.
- default values are injected into normalized arguments before approval and
  audit.
- execution-before local semantic validation failures are returned before path
  policy or approval, including empty `find_file.pattern`, unsupported glob
  syntax, unknown `search_text.type`, invalid `search_text.context`
  combinations, and empty `edit_file.old_text`.
- denied roots and denied explicit target paths return policy denial before
  handler traversal, file read/write, ripgrep invocation, shell execution, or
  provider calls.
- array `minItems` and `maxItems` are enforced for `view_image.paths`.
- array `minItems` is enforced for `shell_exec.argv`.
- malformed tool calls return `tool_error/tool_schema_invalid`.
- `search_text.query` is rejected.
- `search_text.multiline` is rejected.
- empty-after-trim `find_file.pattern` and `search_text.pattern` are rejected.
- `search_text.pattern` containing CR or LF returns
  `tool_error/tool_schema_invalid`.
- `search_text.context=N` without explicit `before_context` or `after_context`
  is valid even after default injection, and produces
  `before_context_effective=N` plus `after_context_effective=N`.
- `search_text.context` with an explicitly provided `before_context` or
  `after_context` returns `tool_error/tool_schema_invalid`.
- `list_dir.ignore` default injection and `maxItems=100` are enforced.

### Approval And Audit

- reusable approval scope for each Phase 3.5 tool matches the scope table in
  `specs/native-tools.md`.
- pagination parameters are excluded from reusable approval scope.
- `list_dir.ignore` is included in reusable approval scope.
- `search_text.type` is included in reusable approval scope.
- `search_text.output_mode`, `before_context_effective`,
  `after_context_effective`, `fixed_strings`, `case_sensitive`, `glob`, and
  `include_hidden` are included in reusable approval scope.
- `view_image` reusable approval scope excludes query and query source.
- `write_file` reusable approval scope includes the exact canonical planned
  parent directories to create, and a grant without parent-directory creation
  does not authorize a later call that creates parent directories.
- `edit_file.old_text`, `edit_file.new_text`, and `write_file.content` remain
  intentionally excluded from reusable approval scope; same-scope reusable
  grants can authorize later content changes, while path policy, approval mode,
  stale-write guard, and normalized/redacted audit still apply.
- `activate_skill` reusable approval scope follows Phase 1 skill `name` and
  frozen skill `content_hash`.
- `load_skill_resource` reusable approval scope follows Phase 1 skill `name`,
  skill content hash, resource path, resource kind, and resource content hash.
- `todo` follows Phase 2/3 runtime-control approval behavior without adding a
  new Phase 3.5 reusable approval scope or grant behavior.
- audit events include normalized or redacted arguments for started,
  completed, failed, and denied tool calls.
- audit events do not include a new deterministic call/audit signature field.
- `write_file` audit arguments include `content_sha256` and `content_bytes`
  instead of full content.
- `edit_file` audit arguments include `old_text_sha256`, `old_text_bytes`,
  `new_text_sha256`, and `new_text_bytes` instead of full replacement text.
- `view_image` audit arguments record `effective_query_source` and do not record
  query text, query preview, or query length.

### Portable Glob

- `*` matches characters within one path segment and does not cross `/`.
- `?` matches one character within one path segment and does not cross `/`.
- `[...]` matches one character within one path segment.
- `**` as a complete path segment matches zero or more directory levels.
- `foo**bar` and any other non-segment `**` usage returns
  `tool_error/tool_schema_invalid`.
- escape syntax and negated character classes return
  `tool_error/tool_schema_invalid`.
- any backslash in a portable glob pattern returns
  `tool_error/tool_schema_invalid`.
- malformed character classes return `tool_error/tool_schema_invalid`.
- brace expansion such as `{a,b}` returns `tool_error/tool_schema_invalid`.
- extglob such as `!(x)` returns `tool_error/tool_schema_invalid`.
- matching uses `/` separators independent of platform.
- `case_sensitive=false` performs matcher-level case folding.
- matcher-level case folding uses Python `str.casefold()`, while result sorting
  still uses canonical path strings.

### `find_file`

- path omitted searches `workspace_root`.
- path provided resolves to canonical root and must pass policy and approval
  before enumeration.
- `pattern` values that are empty after trimming return
  `tool_error/tool_schema_invalid`.
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
- allowed symlink file results return the absolute normalized symlink candidate
  path rather than the resolved target path.

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
- `ignore` supports literal child names plus `*` and `?` over one immediate child
  name segment.
- `ignore` defaults to `[]` and rejects more than 100 patterns.
- `foo/` and `foo/**` hide only the immediate child directory named `foo`;
  `foo/**` does not recurse through descendants.
- `a/b`, bare `**`, `*.py/`, nested directory patterns, character classes,
  brace expansion, extglob, and backslash escape syntax return
  `tool_error/tool_schema_invalid`.
- any backslash in a `list_dir.ignore` pattern returns
  `tool_error/tool_schema_invalid`.
- filtering order is deny -> hidden -> ignore -> sort -> pagination.
- sorting is entry name ascending, case-sensitive, no directory-first grouping.
- `limit > 1000` returns `tool_error/tool_schema_invalid`.
- output contains canonical `path`, `entries`, `offset`, `limit`,
  `total_returned`, `truncated`, and `next_offset`.

### `search_text`

- root approval occurs before candidate enumeration.
- candidate enumeration skips denied paths and counts them only in
  `skipped_files.denied`.
- skipped-file counters count file leaves only.
- runtime does not enter denied directory subtrees merely to compute
  `skipped_files.denied`.
- runtime does not enter hidden directory subtrees merely to compute
  `skipped_files.hidden`.
- candidate enumeration enforces inherited Phase 1 builtin denies including
  `.sessions/`, `~/.debug-agent/skills/`, and
  `<workspace_root>/.debug-agent/skills/`.
- hidden files are skipped by default and counted in `skipped_files.hidden`.
- `include_hidden=true` includes hidden files but not denied files.
- symlink directories are not recursively followed.
- symlink files whose resolved target escapes allowed scope or hits a deny rule
  are skipped and counted in `skipped_files.other`.
- `glob` filters candidates relative to the search root.
- `pattern` values that are empty after trimming return
  `tool_error/tool_schema_invalid`.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- ripgrep is invoked with `shell=False` argv over only runtime-filtered allowed
  candidates.
- ripgrep argv uses `--no-config`, `--regexp <pattern>`, and `--` before
  candidate file paths so patterns and paths are not interpreted by a shell and
  paths with spaces, parentheses, shell metacharacters, or leading `-` are
  handled correctly.
- ripgrep runs with a controlled child-process environment where
  `RIPGREP_CONFIG_PATH` cannot inject local config behavior.
- regex compile checks for empty and non-empty candidate sets use a
  runtime-owned empty UTF-8 temporary file, not workspace traversal, stdin, or an
  environment-dependent path. The temporary file is not counted in
  `skipped_files`, not exposed through audit arguments or artifacts, is covered
  by the ToolBroker timeout envelope, and is cleaned up best-effort.
- regex compile checks are skipped in `fixed_strings=true` mode after `rg`
  availability has been verified.
- ripgrep argv does not include context flags. Content-mode context rows are
  attached after matching-line pagination by bounded runtime reads of only files
  needed for the selected page.
- context attachment read/stat/decode failure after ripgrep matches returns
  `tool_error/tool_execution_failed` and does not return a partial successful
  page.
- if ripgrep invocation is chunked, runtime preserves canonical path and line
  ordering across chunks without materializing all matches in memory.
- `search_text` keeps only bounded aggregation state: skipped counters, the
  selected output-mode aggregation, the requested page, and one extra item for
  `truncated`.
- ripgrep exit code `1` for no matches returns success empty result.
- missing `rg` returns `tool_error/tool_execution_failed`.
- regex compile failure returns `tool_error/tool_execution_failed` with a
  message in the form
  `rg execution failure: <short ripgrep diagnostic>`.
- empty candidate sets return successful empty results only after required `rg`
  availability and regex compilation checks pass.
- if candidate enumeration or UTF-8 pre-screening times out before ripgrep is
  invoked, timeout wins over later regex validation and returns
  `tool_error/tool_execution_timeout`.
- unknown `type` returns `tool_error/tool_schema_invalid`.
- `type` is applied by runtime to the already authorized candidate list before
  ripgrep search using the Phase 3.5 runtime-owned text type allowlist, and does
  not rely on `rg --type` filtering explicit file argv.
- `type` file-family matching is case-insensitive over candidate relative paths.
- custom ripgrep type definitions and local ripgrep config do not affect
  `search_text.type`.
- local ripgrep config does not affect `search_text` matching, case behavior,
  hidden behavior, output mode, or result ordering.
- unknown `type` returns `tool_error/tool_schema_invalid`.
- an empty candidate set after filtering returns successful empty output without
  invoking the main ripgrep search after required `rg` availability and regex
  compilation checks pass.
- `fixed_strings=true` performs literal search.
- `fixed_strings=false` performs ripgrep/Rust regex search.
- `context` conflicts with `before_context` or `after_context` and returns
  `tool_error/tool_schema_invalid`.
- `context=N` equals `before_context=N` and `after_context=N`.
- approval scope and audit arguments use normalized
  `before_context_effective` and `after_context_effective`.
- `context`, `before_context`, and `after_context` are accepted no-ops for
  `files_with_matches` and `count`, and remain in approval scope.
- context lines do not count toward `maxResults` or `total_returned`.
- for `output_mode=content`, pagination is applied to sorted match result items
  before context rows are attached.
- for every `search_text` output mode, `next_offset = offset + total_returned`
  when `truncated=true`.
- content-mode result items are matching lines, not regex submatches.
- multiple regex or fixed-string matches on the same line return and count as one
  matching line.
- same-line repeated matches are de-duplicated by `(canonical_path, line_number)`
  before content-mode pagination and count-mode aggregation.
- for `output_mode=content`, `next_offset = offset + total_returned` when
  `truncated=true`.
- context rows may repeat across pages when adjacent pages request matches whose
  context windows overlap.
- duplicate context lines in a file are de-duplicated.
- match lines win over context lines for the same line number.
- content output is sorted by canonical path then line number.
- `files_with_matches` output is sorted by canonical path.
- `count` output is sorted by canonical path, includes only count > 0, and counts
  matching lines per file rather than repeated submatches on the same line.
- line previews over 4000 codepoints are truncated and mark
  `line_truncated=true`.
- only the active output-mode result field is present.
- `skipped_files` has exactly `denied`, `hidden`, `decode_error`, and `other`
  counters and no path names.
- `skipped_files.decode_error` is populated from UTF-8 pre-screening and ripgrep
  JSON byte payload decode failures.
- `skipped_files.other` is populated for symlink escape and ordinary candidate
  file stat/read/pre-screen failures that are not deny, hidden, or decode
  failures.

### `edit_file`

- `old_text` empty returns `tool_error/tool_schema_invalid`.
- nonexistent target returns `tool_error/tool_execution_failed`.
- default `replace_all=false` replaces only when exactly one match exists.
- zero matches returns `tool_error/tool_execution_failed`.
- multiple matches with `replace_all=false` returns
  `tool_error/tool_execution_failed`.
- `replace_all=true` replaces all non-overlapping matches from left to right.
- LF-normalized matching preserves dominant line endings on write-back, and files
  with no dominant existing style write LF.
- existing file edit without cache entry fails and requires `read_file`.
- stale cache mismatch fails and requires `read_file`.
- successful edit writes complete content through a same-directory temporary file
  followed by atomic target replace after stale-write guard succeeds.
- successful edit output contains canonical `path`, `replacements`, `bytes`,
  `sha256_before`, `sha256_after`, and `guard`.
- successful edit updates cache to new revision.

### `write_file`

- creates missing file when target path passes write policy and approval.
- creates missing parent directories when target path passes write policy and
  approval and each candidate parent directory passes path policy.
- creates only the minimal missing parent directory chain required for the target
  file and does not create sibling directories or broader directory trees.
- approval and UI presentation for a create-new-file call that creates parent
  directories list every canonical parent directory that will be created before
  the call proceeds.
- reusable approval grants for `write_file` include the planned parent-directory
  creation set, so changing that set requires a different scope signature.
- every candidate parent directory created by `write_file` must pass path policy.
- interactive approval for parent-directory creation remains scoped to the final
  canonical target file path and does not widen to a directory grant.
- audit arguments for parent-directory creation include the final canonical
  target path and the canonical candidate parent directories created by the call.
- if a later create/write step fails after parent directories are created, the
  directories are not guaranteed to roll back; failed audit records capture
  directory side effects and the call does not report file write success.
- if ToolBroker timeout fires after parent directories are created but before
  file create/write success, the call returns
  `tool_error/tool_execution_timeout`, records created-directory side effects in
  the timed-out audit record, does not report write success, and does not update
  the file metadata cache.
- creating a new file does not require pre-write cache entry.
- creating a new file uses exclusive create semantics.
- if a target appears between missing-target classification and exclusive create,
  the call returns `tool_error/tool_execution_failed` and does not overwrite.
- creating a new file returns `created=true`, `overwritten=false`,
  `sha256_before=null`, and `guard.used=false`.
- failed or timed-out create-new-file calls that already created parent
  directories record those side effects in audit while reporting no file write
  success and leaving the file metadata cache unchanged.
- those failed or timed-out calls record only the minimal known side-effect fields
  required by Phase 3.5: `side_effects.created_directories`,
  `file_write_completed=false`, and `cache_updated=false`.
- overwriting an existing file without cache entry fails and requires
  `read_file`.
- overwriting an existing empty file without cache entry fails and requires
  `read_file`.
- stale cache mismatch fails and requires `read_file`.
- successful overwrite output contains canonical `path`, `bytes`,
  `created=false`, `overwritten=true`, `sha256_before`, `sha256_after`, and
  `guard`.
- successful overwrite writes complete content through a same-directory temporary
  file followed by atomic target replace after stale-write guard succeeds.
- successful write updates cache to new revision.
- write failure is `tool_error/tool_execution_failed` and cannot report partial
  write as success.

### `shell_exec`

- schema still requires structured `argv`.
- raw shell string field `command` is rejected.
- alias fields `directory` and `description` are rejected.
- background, interactive, and PTY flags are rejected as unknown fields.
- generated schema includes frozen maximum timeout.
- generated schema uses the frozen `execution.max_shell_timeout_seconds` value
  for `timeout_seconds.maximum`; tests must cover a non-default configured
  maximum and must not accept hard-coded `3600` when the frozen snapshot differs.
- omitted `cwd` uses the session workspace root, and successful output reports
  that canonical `cwd`.
- omitted timeout specifically uses frozen
  `execution.default_shell_timeout_seconds`, not
  `execution.default_tool_timeout_seconds`.
- requested timeout greater than frozen maximum returns
  `tool_error/tool_schema_invalid`.
- successful output includes `argv`, canonical `cwd`, `stdout`, `stderr`,
  `returncode`, `signal`, and integer `duration_ms`.
- `signal` is integer or `null`; normal exit and Windows use `null`.
- nonzero process exit returns `tool_error/shell_nonzero_exit`.
- nonzero process exit keeps stdout/stderr diagnostic behavior from earlier
  phases: concrete stderr/stdout may be summarized in the message, and large
  stdout/stderr may appear only as completed diagnostic artifacts referenced by
  `artifact_ids`.

### `view_image`

- disabled `view_image` remains omitted from model-visible tool bindings.
- direct valid disabled call returns `config_error/tool_unavailable`.
- malformed disabled call validates first and returns
  `tool_error/tool_schema_invalid`.
- ordinary output still uses display path behavior.
- approval scope uses ordered canonical image paths.
- query validation follows frozen `max_query_chars`.
- empty or whitespace-only `paths[]` entries return
  `tool_error/tool_schema_invalid`.
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
- session summary includes session id, run id, workspace, status, start/update
  timestamps, approval mode, total messages, total user
  messages, total assistant messages, and total tool calls.
- terminalized session summaries include terminal reason; running session
  summaries omit terminal reason rather than rendering an unknown placeholder.
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
- trace documentation and tests explicitly accept that raw user or assistant
  Markdown can visually mimic trace section structure, while runtime truth,
  events, artifact records, and checkpoints remain authoritative for audit and
  recovery.
- ordinary run event names such as `model_call_started`,
  `checkpoint_written`, `approval_requested`, and `context_optimized` do not
  appear as trace timeline sections.
- `context_summary` rows are filtered without an omitted-summary placeholder.
- model name and token counts are not reintroduced from model events.
- runtime failure and cancellation facts render normalized
  `error_class/reason` and message.

### Observability Validation

- trace rendering fails closed for unsupported role or kind.
- trace rendering performs trace-render validation only; it is not
  recovery-grade resume/checkpoint validation and does not read artifact body
  content to verify artifact payload checksums.
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
- trace rendering fails closed for paired tool results whose `ToolResult.status`
  is not one of `ok`, `error`, `denied`, `timeout`, or `cancelled`.
- trace rendering treats inline `tool_result.content_json.status` as the
  canonical status source for Phase 3.5 native tool results and fails closed when
  a duplicated `metadata_json.status` conflicts with it.
- assistant tool-call rows with optional normalized `content.text` render that
  text before the tool-call block, while assistant text stored outside the
  normalized field fails closed instead of being silently dropped.
- trace rendering fails closed for artifact-backed rows or inline Phase 3.5
  field-level artifact references whose durable artifact record/reference
  metadata is missing or conflicting.
- missing artifact record/content, missing inline field-level artifact reference
  target, or missing referenced artifact file maps to
  `persistence_error/artifact_missing`; conflicting artifact metadata,
  mismatched field-level artifact reference metadata, or mismatch between inline
  artifact refs and `tool_result.artifact_ids` maps to
  `persistence_error/conversation_cut_invalid`.
- trace rendering does not read artifact body content to verify checksums,
  generate previews, or enrich previews.
- artifact-backed user and assistant rows render artifact references and any
  already-inline preview/reference metadata without reading artifact body
  content.
- non-artifact durable conversation validation failure returns
  `persistence_error/conversation_cut_invalid` and does not overwrite an
  existing `logs/trace.md`.

### Tool Result And Argument Preview

- tool result previews over 4000 characters are truncated and marked
  `[truncated]`.
- tool result previews over 100 lines are truncated and marked `[truncated]`.
- tool argument blocks over 4000 characters or 100 lines are truncated and marked
  `[truncated]` after tool-specific trace redaction.
- trace replaces `write_file.content` with a redacted object containing
  `redacted=true`, SHA-256, and UTF-8 byte count.
- trace replaces `edit_file.old_text` and `edit_file.new_text` with the same
  redacted object shape.
- trace does not render content previews for `write_file.content`,
  `edit_file.old_text`, or `edit_file.new_text`.
- JSON/dict tool outputs are pretty-printed before preview limits are applied.
- text previews apply limits to original text.
- preview truncation does not modify durable conversation content.
- artifact-backed results do not read artifact content while rendering trace.
- artifact-backed results display only `artifact_id`, relative artifact path, and
  any already-inline preview/reference metadata.
- artifact-backed results do not display checksum or additional verification
  hints.
- inline Phase 3.5 field-level artifact references inside
  `tool_result.content_json.content` validate against ArtifactStore records,
  existing artifact files, session/run scope, `relative_path`, and
  `tool_result.artifact_ids`.
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
- manual and automatic trace generation read session summary, durable
  conversation rows, artifact records, and summary counts from one consistent
  SQLite read transaction/snapshot.
- trace writes use a unique temporary file in the same `logs/` directory followed
  by atomic replace.
- concurrent automatic and manual trace writes produce one complete last-writer
  result, never interleaved or partial output.
- trace render/write failure returns `ui_error/trace_render_failed`.
- manual trace render/write failure after lookup succeeds maps to
  `ERROR_TRACE_RENDER = 11`.
- automatic trace failure after terminal checkpoint success does not roll back
  checkpoint creation, block terminalization, block ownership release, write
  runtime truth, write audit/run events, write `events.jsonl`, or change the
  original workflow exit code.
- automatic trace failure after terminal checkpoint success is reported through
  the current CLI/UI surface, and REPL/TUI must show it in an error block.
- `debug-agent trace <session_id>` may run against a running session and renders
  only accepted closed durable conversation rows.
- `debug-agent trace <session_id>` treats any accepted row with
  `group_status != "closed"` as corrupt or invariant-violating durable truth and
  fails closed rather than selecting a partial high-watermark.
- `debug-agent trace <session_id>` against a running session never mixes summary
  counts, conversation rows, or artifact validation from different database
  points-in-time; inability to obtain the bounded consistent read snapshot fails
  without overwriting the existing trace.
- `debug-agent trace <session_id>` does not include mid-flight model, tool,
  provider, or shell state.
- `debug-agent trace <session_id>` does not claim ownership and is not blocked
  solely by an active runner.
- manual trace does not wait for active runners, take ownership, or change active
  owner state.
- SQLite busy after the persistence layer's bounded busy handling returns
  `persistence_error/sqlite_busy_timeout`; ordinary read failure returns
  `persistence_error/persistence_read_failed`.
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

- terminal recovery checkpoint tool-availability facts include
  `native_tools_contract.phase = "3.5"`.
- terminal recovery checkpoint preserves the existing Phase 3 tool-availability
  placement mechanism while replacing the facts inside that existing
  representation.
- terminal recovery checkpoint payload has `manifest_schema_version = 2`.
- terminal recovery checkpoint includes
  `native_tools_contract.contract_marker =
  "phase-3.5-native-tools-v1"`.
- terminal recovery checkpoint includes shell maximum timeout from frozen
  config.
- terminal recovery checkpoint includes `view_image` enabled/disabled state and
  limits from frozen config.
- terminal recovery checkpoint tool-availability facts do not include
  `agent_loop.max_tool_call_iterations`.
- terminal recovery checkpoint tool-availability facts do not include
  `execution.default_tool_timeout_seconds`.
- checkpoint validation recomputes Phase 3.5 tool-availability facts from frozen
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
- one-shot native tool results persist the Phase 3.5 durable `tool_result`
  content shape, with structured `ToolResult.output` as provider-visible content
  and `redacted_output` kept presentation-only.
- when a successful native-tool result remains too large after documented
  field-level artifacting, the one-shot tool observation is a normalized
  `tool_error/tool_execution_failed` result and no row-level artifact-backed
  `tool_result` conversation row is accepted.
- one-shot trace renders ordinary tool-call arguments while rendering sensitive
  `write_file.content`, `edit_file.old_text`, and `edit_file.new_text` arguments
  as redacted SHA-256 plus UTF-8 byte-count objects only.
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
- a session with Todo Plan updates or stale fail-close administrative closure
  keeps `todo_updated` and `stale_fail_closed` in `events.jsonl` rather than
  rendering them as standalone `trace.md` body entries.
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

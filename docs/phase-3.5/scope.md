# Phase 3.5 Scope

## Goal

Phase 3.5 delivers runtime ergonomics, native tooling, and audit hardening
between Phase 3 session/failure control and Phase 4 RenderDoc readiness.

This phase strengthens generic `debug-agent` framework behavior without adding
RenderDoc, Ralph Loop, shader-specific, workflow, subagent, MCP, plugin, PTY, or
long-running shell semantics.

The native-tool portion of Phase 3.5 focuses on:

- safer and more useful model-visible file tools.
- line-oriented text search backed by controlled ripgrep execution.
- consistent pagination and output shapes.
- stricter ToolBroker schema validation and normalized error mapping.
- volatile stale-write protection for model-visible file writes.
- audit records that preserve normalized/redacted tool arguments without adding
  a new call-signature mechanism.
- compatibility handling for the changed model-visible tool schema and result
  contracts.

The trace observability portion of Phase 3.5 focuses on:

- converting `trace.md` from a runtime event dump into a human-readable
  conversation transcript rendered from durable `conversation_messages`.
- moving trace output to `.sessions/<session_id>/logs/trace.md`.
- renaming `logs/engine.log` to `logs/events.jsonl` while preserving its
  non-authoritative observability stream role.
- keeping trace and JSONL output outside runtime truth, checkpoint validation,
  resume, and recovery.

## Must Implement

### Configuration And Constants

- add `specs/configuration.md` as the authoritative Phase 3.5 configuration and
  constants contract.
- centralize runtime constants into directory-level settings modules without
  making a constant configurable unless a phase config spec explicitly allows it.
- add `[agent_loop].max_tool_call_iterations` to frozen runtime config with
  default `1000` and positive-integer validation. Phase 3.5 intentionally does
  not define a hard maximum; local users who configure extremely large values
  accept the local execution-risk tradeoff.
- add `[execution].default_tool_timeout_seconds` to the Phase 3 frozen execution
  config group with default `30` and positive-integer validation. Phase 3.5 also
  intentionally does not define a hard maximum for this field; local users who
  configure extremely large values accept the local execution-risk tradeoff.
- keep Phase 3 startup/config ordering. Invalid Phase 3.5 `[agent_loop]` or
  `[execution]` config must fail through the existing startup config failure
  boundary before runtime database bootstrap or startup legacy schema reset.
- keep `agent.toml` as the path and shell policy declaration channel; do not move
  path policy or shell policy into `config.toml`.
- keep project-local `config.toml` unsupported.
- keep `view_image` image count, image edge, image pixel, and request body limits
  as fixed runtime constants rather than `[multimodal.defaults]` fields.
- do not add global unknown-key fail-closed behavior for `config.toml` in Phase
  3.5.

### Compatibility

- identify fresh Phase 3.5 runtime databases with
  `PHASE_3_5_SCHEMA_USER_VERSION = 4` and SQLite `PRAGMA user_version = 4`.
- before startup, active ownership checks, `status`, `trace`, or `resume`
  interpret runtime truth, read existing `.sessions/runtime.db`
  `PRAGMA user_version`.
- for startup paths that will create a new REPL or one-shot session/run, delete
  missing-version, `0`, or legacy `< 4` `.sessions/runtime.db` before
  interpreting any rows, then create a fresh Phase 3.5 database.
- startup legacy reset is intentionally destructive and does not request active
  owner confirmation for legacy databases. Phase 3.5 treats legacy owner/session
  truth as unsupported and accepts the reset risk documented in
  `project-contract.md`.
- startup legacy reset deletes `.sessions/runtime.db` plus SQLite sidecar files
  `.sessions/runtime.db-wal` and `.sessions/runtime.db-shm` when present; legacy
  artifact, log, trace, checkpoint-payload, and session subdirectories under
  `.sessions/` may remain on disk for manual cleanup but must not be interpreted
  as runtime truth or referenced by the fresh database.
- fresh Phase 3.5 session/log/artifact/checkpoint-payload/temp paths must fail
  closed on collision with orphaned legacy files or directories under
  `.sessions/`; runtime must not delete, merge, reuse, or interpret the collided
  legacy path.
- `status`, `trace`, and `resume` must never reset or create a runtime database.
  They fail closed for missing-version, `0`, legacy `< 4`, unknown future
  version `> 4`, or non-startup schema mismatch before interpreting runtime
  truth.
- do not migrate, reinterpret, preserve, or rewrite legacy rows into Phase 3.5
  shape.
- schema-version fail-closed failures use the Phase 3 normalized
  `config_error/{legacy_schema_version,unknown_schema_version,schema_version_missing}`
  reasons and existing CLI mapping rules.
- user-facing messages must explain that Phase 3.5 does not support older
  runtime databases. Startup reset messages must say the old runtime database
  was deleted and mention that legacy files under `.sessions/` may remain but
  are not interpreted.

### Native Tools

- keep all model-visible tools behind ToolBroker, schema validation, path
  policy, approval, artifact handling, timeout handling, result normalization,
  and audit.
- extend ToolBroker schema validation to support the JSON schema features needed
  by Phase 3.5 tool schemas: `boolean`, `enum`, `minimum`, `maximum`, default
  injection, `minItems`, `maxItems`, arrays, and nested objects.
- model-visible path fields keep the existing `path` style, accepting relative
  or absolute input. Runtime canonicalizes paths before policy, approval,
  handler execution, and audit.
- present path strings must be non-empty after trimming whitespace. Omitting a
  path is valid only for tools that explicitly define an omitted-path default;
  `"."` remains the explicit workspace-root path.
- explicitly exclude `view_image` ordinary model-visible output from the
  Phase 3.5 canonical-absolute-path result rule. `view_image` keeps its Phase 2
  display-path output behavior while approval, policy, and audit use canonical
  paths internally.
- add `find_file` as a model-visible native read tool.
- enhance `read_file` with `offset`, fixed default and maximum line limits, and
  structured output.
- enhance `list_dir` with `ignore`, `offset`, `include_hidden`, pagination, and
  structured output.
- change `search_text` from the Phase 1 literal `query` schema to a
  line-oriented `pattern` schema backed by controlled ripgrep execution.
- isolate controlled ripgrep execution from local ripgrep configuration and make
  `search_text` result pagination deterministic by canonical path and line
  ordering.
- because controlled ripgrep search has distinct traversal, filtering, chunking,
  output-mode, pagination, and error-mapping risks, the future Phase 3.5
  `implementation-plan.md` must keep `search_text` as its own implementation
  milestone rather than bundling it into a broad native-tool milestone.
- serialize structured Phase 3.5 native tool results through `ToolResult.output`,
  durable `tool_result.content_json`, and artifact reference objects as defined
  by `specs/native-tools.md`.
- apply the Phase 3.5 deterministic minimal large-output plan for successful
  structured native-tool results: when the full native-tool observation exceeds
  the durable inline threshold, externalize documented large result fields in
  stable field order until the observation fits inline or no eligible fields
  remain. If the resulting native-tool observation still cannot fit the durable
  inline `tool_result` row, the tool call returns
  `tool_error/tool_execution_failed` instead of using row-level
  artifact-backed conversation fallback.
- enhance `edit_file` with `replace_all`, uniqueness semantics, structured
  output, stale-write guard, and same-directory temporary-file atomic replace
  for existing-file writes.
- enhance `write_file` output and require stale-write guard when overwriting an
  existing file. Overwrite writes use same-directory temporary-file atomic
  replace after stale-write guard succeeds; create-new writes keep exclusive
  create semantics.
- enhance successful `shell_exec` output with `argv`, canonical `cwd`,
  `stdout`, `stderr`, `returncode`, `signal`, and integer `duration_ms`.
- preserve `shell_exec` structured argv execution. Do not add raw shell strings,
  `command`, `directory`, `description`, background execution, interactive
  execution, PTY, or long-running shell runtime.
- preserve `view_image` as image-only. Do not add video, audio, URL, base64,
  artifact-id, or general multimedia inputs.
- preserve existing `activate_skill`, `load_skill_resource`, and `todo`
  runtime-control tools from earlier phases. Phase 3.5 does not update or
  tighten their schemas, target validation, behavior semantics, approval
  exceptions, runtime truth, persistence, or checkpoint facts. Their
  `ToolResult` envelope, status, normalized error projection, and model-visible
  artifact references still follow the Phase 3/3.5 ToolBroker boundary
  contract, which supersedes older Phase 1/2 example status/error wording for
  malformed tool input and local semantic validation.
- keep `load_skill_resource.path` as a Phase 1 skill-local resource path. It is
  not converted into a Phase 3.5 native filesystem path or canonicalized against
  `workspace_root`.

### Trace Observability

- add `specs/observability.md` as the authoritative Phase 3.5 trace/events
  contract.
- generate conversation trace at `.sessions/<session_id>/logs/trace.md`.
- stop generating `.sessions/<session_id>/trace.md`.
- rename `.sessions/<session_id>/logs/engine.log` to
  `.sessions/<session_id>/logs/events.jsonl`.
- do not implement legacy trace/log path compatibility, migration, symlink, or
  copy.
- render trace body only from accepted closed durable conversation rows ordered
  by `conversation_messages.message_index ASC`.
- include user messages, assistant final messages, assistant tool-call messages
  with paired tool results, and durable runtime failure/cancellation facts.
- exclude ordinary run event timelines, checkpoint internals, approval
  internals, context compression internals, and `context_summary` rows from
  trace.
- preserve original durable user and assistant message content as-is for human
  reading, without Markdown escaping or sanitization.
- allow emoji section headings and other non-ASCII trace output; do not provide
  an ASCII-only trace format.
- treat `trace.md` as human-readable conversation history, not audit truth,
  runtime truth, checkpoint input, resume input, or recovery input.
- after terminal checkpoint success, automatic trace generation failure must not
  roll back checkpoint creation, block terminalization, block ownership release,
  write runtime truth, write audit/run events, write `events.jsonl` runtime
  diagnostics, or change the original workflow exit code.
- manual `debug-agent trace <session_id>` must fully rebuild trace from the
  database, may run against a running session, must not claim active ownership,
  and must not start, resume, terminalize, fail-close, model-call, or tool-call
  anything.
- automatic and manual trace writes must use unique same-directory temporary
  files and atomic replace so the final trace is never partial or interleaved.
- `events.jsonl` keeps the same authority as the old `engine.log`: a
  non-authoritative JSONL stream for run-event observations and runtime
  diagnostics.

### Pagination And Limits

- `read_file`: `offset + limit`, default `limit = 2000`, hard maximum `2000`,
  offset unit is 0-based line number.
- `list_dir`: `offset + limit`, default `limit = 200`, hard maximum `1000`,
  offset unit is sorted entry item.
- `list_dir.ignore`: default `[]`, hard maximum `100` patterns.
- `find_file`: `offset + maxResults`, default `maxResults = 100`, hard maximum
  `1000`, offset unit is sorted file match item.
- `search_text`: `offset + maxResults`, default `maxResults = 100`, hard
  maximum `1000`, offset unit is result item for the selected output mode.
  Content-mode result items are matching lines, not regex submatches; multiple
  matches on the same line count once.
- `find_file.pattern` and `search_text.pattern` must be non-empty after
  trimming whitespace. `search_text.pattern` must not contain CR or LF
  characters because Phase 3.5 search is line-oriented and does not define
  multiline matching.
- `search_text.skipped_files` counters use aggregate file-leaf counts only;
  runtime must not enter denied or hidden directory subtrees merely to count
  descendants.
- `search_text` content-mode pagination is applied to sorted match result items
  before context rows are attached; context rows do not count toward
  `total_returned` and may repeat across adjacent pages.
- `search_text` attaches content-mode context rows by bounded runtime reads
  after matching-line pagination, not by asking ripgrep to return context for
  the whole candidate set.
- `search_text` count-mode counts matching lines per file, not regex captures or
  repeated submatches within a line.
- requested page sizes above hard maximum return
  `tool_error/tool_schema_invalid`; they are not silently capped.
- `total_returned` is the number of result items returned on this page, not a
  full result-set count.
- `truncated=true` means the same normalized parameter set has another page at
  `next_offset`; otherwise `truncated=false` and `next_offset=null`.
- for every Phase 3.5 paginated native tool, `next_offset = offset +
  total_returned` when `truncated=true`.
- Phase 3.5 does not add model-visible or configurable byte-size limits for
  generic text tools. `read_file` whole-file hashing, `search_text` candidate
  enumeration and UTF-8 pre-screening, and related traversal work must use
  streaming or bounded-memory implementation techniques inside the ToolBroker
  timeout envelope. Internal parser and argv safety limits are implementation
  settings, not `config.toml` fields. Timeout returns
  `tool_error/tool_execution_timeout` and must not return partial successful
  pages.

### ToolBroker Volatile File Metadata Cache

- maintain a session-runtime-local, in-process file metadata cache for
  stale-write guard.
- cache entries are keyed by canonical absolute path and include `sha256`,
  `size`, `mtime_ns`, `observed_at`, and `source_tool`.
- successful `read_file` must compute whole-file raw byte SHA-256 and update the
  cache even when returning only a paginated slice.
- successful guarded `edit_file` and overwrite `write_file` advance the cache to
  the new revision.
- successful create-new-file `write_file` creates a cache entry after writing.
- `search_text`, `list_dir`, `find_file`, and `view_image` do not create cache
  entries usable for write guard.
- cache is not persisted runtime truth. It must not be written to SQLite,
  checkpoints, artifacts, or events as recoverable state. Resume or process
  restart starts with an empty cache.

### Approval And Audit

- keep `approval_scope_signature` as the only reusable approval signature
  mechanism.
- do not add a separate deterministic call/audit signature.
- ToolBroker audit events are runtime-authored audit facts and must persist
  normalized or redacted tool arguments sufficient to explain each call.
  `view_image` keeps Phase 2 query redaction and records only
  `effective_query_source`, not query text or query length.
- do not introduce a detailed native-tool failure audit schema in Phase 3.5.
  Failed/timed-out `write_file` calls that created parent directories record only
  the known minimal side-effect facts needed for audit and tests; `shell_exec`
  nonzero keeps the existing stdout/stderr diagnostic behavior.
- approval reusable grant scope is defined per tool in
  `specs/native-tools.md`. Pagination parameters are excluded from reusable
  approval scope.
- conversation trace is a presentation layer over assistant-authored durable
  tool-call arguments. It redacts `write_file.content`, `edit_file.old_text`, and
  `edit_file.new_text` to SHA-256 plus UTF-8 byte count and applies the trace
  preview limit to rendered argument blocks.

### Tool Availability In Terminal Recovery Checkpoints

- do not add a complete per-tool schema/result hash contract.
- extend the existing terminal recovery checkpoint tool-availability facts with
  the dynamic facts needed to restore Phase 3.5 tool bindings and a native tool
  contract marker.
- keep the existing Phase 3 terminal recovery checkpoint representation for tool
  availability. Phase 3.5 updates the facts contained in that existing
  representation, but does not move tool availability between top-level payload
  fields, `frozen_snapshots`, or a separate `tool_availability_snapshot_id`.
- bump terminal recovery checkpoint payload `manifest_schema_version` to `2`
  because Phase 3.5 changes the terminal checkpoint payload shape.
- use `PRAGMA user_version = 4` as the cross-version compatibility boundary;
  do not rely on per-tool schema hashes to migrate or accept older sessions.

## Must Not Implement

- `renderdoc-gpu-debug` business adaptation.
- fake `rdc` CI scenario.
- Windows + real `rdc` smoke.
- RenderDoc command allowlists, Ralph Loop state machines, shader-specific
  validators, shader patch tools, or business report schemas.
- subagents, workflow runtime, MCP, plugin packaging, background tasks, or task
  graph.
- PTY shell, interactive terminal execution, background shell execution, or
  long-running shell runtime.
- deterministic call/audit signature.
- complete per-tool schema hash or result-contract hash.
- full node minimatch compatibility, brace expansion, extglob, or glob behavior
  outside the Phase 3.5 portable subset.
- environment-dependent ripgrep type registry behavior for `search_text.type`;
  Phase 3.5 uses a fixed runtime-owned text type allowlist.
- `search_text.multiline`.
- Python dependency additions for glob matching.
- Python regex fallback for `search_text` when `rg` is missing.
- persistent file metadata cache, revision-token schema, or model-visible
  `expected_sha256` fields.
- `view_image` video, audio, URL, base64, or artifact-id input.
- changing `view_image` ordinary output path display behavior.
- legacy `.sessions/<session_id>/trace.md` compatibility.
- legacy `.sessions/<session_id>/logs/engine.log` compatibility.
- trace-to-runtime-truth reconstruction.
- trace audit persistence or trace failure audit events.
- ASCII-only trace output variant.

## Runtime Contract Additions

Phase 3.5 changes the model-visible native tool schemas and native tool result
contracts. These changes are runtime truth and require schema version 4.

Phase 3.5 adds `find_file` to the model-visible native tool set.

Phase 3.5 changes `search_text` from Phase 1 literal `query` search to
line-oriented ripgrep-backed `pattern` search. The old `query` field is not an
alias.

Phase 3.5 preserves `activate_skill`, `load_skill_resource`, and `todo` as
model-visible runtime-control tools. They are not native-tool enhancement
targets in this phase, but they are not removed, renamed, deprecated, or
semantically changed. Their model-visible artifact references still follow the
shared ToolResult artifact contract, including `artifact_ids` consistency and
model-readable `artifact_path` when an accepted artifact is intentionally
exposed for `read_file` follow-up.

Phase 3.5 adds a volatile ToolBroker file metadata cache. This cache is runtime
execution state only, not recovery truth.

Phase 3.5 extends the existing terminal recovery checkpoint tool-availability
facts with a native tool marker and dynamic tool facts. It does not introduce a
separate complete tool-contract hash system or change the Phase 3 checkpoint
placement mechanism.

Phase 3.5 changes observability file paths and trace rendering behavior:

- `trace.md` is now derived from durable conversation rows and written under
  `logs/trace.md`.
- `events.jsonl` replaces `engine.log` as the JSONL observability stream.
- these files remain non-authoritative observability outputs and are not runtime
  truth.

Phase 3.5 uses the existing Phase 3 `ui_error/trace_render_failed` reason for
trace Markdown render/write failures. Durable conversation validation failures remain
`persistence_error/conversation_cut_invalid`.

## Minimum Runnable Slice

1. User starts a fresh Phase 3.5 REPL or one-shot session.
2. Runtime creates a schema version 4 database, freezes config and policy, and
   exposes the Phase 3.5 model-visible tool set.
3. The model calls `find_file`, `read_file`, `list_dir`, and `search_text` with
   authorized roots and receives structured paginated results.
4. The model calls `read_file` on an existing file, then successfully uses
   `edit_file` or overwrite `write_file` guarded by the volatile cache.
5. A direct overwrite of an existing file without a prior valid cache entry
   fails with `tool_error/tool_execution_failed`.
6. Successful `shell_exec` returns structured shell metadata without expanding
   shell capabilities.
7. Terminalization writes a terminal recovery checkpoint whose
   existing tool-availability representation contains Phase 3.5 dynamic tool
   facts.
8. Runtime fully rebuilds `.sessions/<session_id>/logs/trace.md` from durable
   conversation rows after terminal checkpoint success, and writes
   `.sessions/<session_id>/logs/events.jsonl` instead of legacy `engine.log`.
9. `resume` validates the checkpoint tool availability against the frozen
   session config and starts with an empty volatile file metadata cache.

## Completion Definition

Phase 3.5 native-tool spec work is complete when:

- this phase document set defines scope, architecture, native tool contracts,
  configuration contracts, observability contracts, compatibility, tests, and
  operations.
- no `docs/phase-3.5/implementation-plan.md` is created as part of this spec
  task.
- implementation work does not begin until a future approved
  `implementation-plan.md` exists for Phase 3.5.

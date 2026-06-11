# Phase 3.5 Architecture

## Module Impact

Phase 3.5 is a generic runtime and ToolBroker hardening phase. It refines
existing modules rather than adding new architecture layers.

### CLI Entrypoint

Responsibilities:

- initialize Phase 3.5 schema compatibility with `PRAGMA user_version = 4`.
- perform startup-only legacy database reset for REPL and one-shot startup
  paths before interpreting runtime truth.
- keep `status`, `trace`, and `resume` read/recovery paths fail-closed for
  schema mismatch without deleting or creating `.sessions/runtime.db`.
- surface user-facing messages that explain legacy reset or fail-closed
  behavior.
- route `debug-agent trace <session_id>` through the Phase 3.5 conversation
  trace renderer, rebuilding `.sessions/<session_id>/logs/trace.md` from the
  database without claiming active workspace ownership.
- print the Phase 3.5 trace path under `logs/trace.md`, not the legacy root
  session trace path.

The CLI must not interpret legacy runtime rows before schema validation or
startup reset.

### Runtime Orchestrator

Responsibilities:

- create fresh Phase 3.5 sessions with frozen config, policy, and dynamic tool
  availability facts.
- build model-visible tool bindings from the frozen session config.
- omit disabled `view_image` from model-visible bindings while preserving the
  broker-side disabled-tool behavior.
- pass the frozen config to ToolBroker so shell maximum timeout, multimodal
  limits, and approval/policy facts are evaluated from session truth.
- on resume, use the existing terminal recovery checkpoint path and validate
  Phase 3.5 tool-availability facts.
- after a terminal recovery checkpoint is successfully written, request a full
  conversation trace rebuild as non-authoritative observability output.

Resume must not rebuild tool availability from current mutable config files or
environment variables. The session frozen config remains the source for dynamic
tool facts.

Automatic trace refresh failure must not roll back checkpoint creation, block
terminalization, block ownership release, write runtime truth, write audit/run
events, write `events.jsonl` runtime diagnostics, or change the original command
exit code or lifecycle outcome. The runtime may surface the trace refresh
failure through the current CLI/UI only.

### Configuration And Settings

Phase 3.5 keeps runtime configuration resolution in the existing config loading
path and centralizes built-in constants into settings modules.

Responsibilities:

- `runtime/config.py` resolves the Phase 3.5 `[agent_loop]` and `[execution]`
  additions before policy freeze, runtime database bootstrap, startup legacy
  schema reset, session/run creation, ownership checks, model calls, or tool
  calls.
- `runtime/config.py` writes resolved `agent_loop.max_tool_call_iterations` and
  `execution.default_tool_timeout_seconds` into the session frozen config
  snapshot.
- resume uses the stored session config snapshot and must not read current
  `config.toml` to rebuild these values.
- `runtime/settings.py` owns runtime default values, policy safety baselines,
  retry data, token estimator constants, provider execution constants, platform
  constants, and prompt/runtime ordering constants.
- `tools/settings.py` owns native tool pagination constants, ToolBroker internal
  limits, and fixed `view_image` defaults and image/request limits.
- `cli/settings.py` owns REPL/TUI presentation constants.
- `persistence/settings.py` owns schema/checkpoint version constants and
  inline/artifact thresholds.

Central settings modules are implementation constant sources, not a new
configuration control plane. A constant is configurable only when a phase config
spec explicitly defines a `config.toml` field.

### ToolBroker

ToolBroker remains the only execution boundary for model-visible tools.

Phase 3.5 responsibilities:

- validate Phase 3.5 JSON schemas and inject defaults into normalized
  arguments before approval, audit, or handler execution.
- normalize and canonicalize path-like arguments.
- evaluate path policy, shell policy, and approval using the existing
  `approval_scope_signature` mechanism.
- write audit events with normalized or redacted arguments.
- maintain the volatile file metadata cache used by `edit_file` and
  `write_file` stale-write guard.
- route allowed calls to native, shell, runtime-control, or `view_image`
  handlers.
- normalize schema, policy, timeout, handler, shell nonzero, and cancellation
  results through the Phase 3 error taxonomy.
- use frozen `execution.default_tool_timeout_seconds` as the default execution
  envelope for brokered tool calls without a tool-specific timeout source.
- serialize Phase 3.5 native tool results through the ToolResult envelope and
  durable `tool_result.content_json` contract defined in
  `specs/native-tools.md`.
- keep ArtifactStore registration and artifact writes caused by large tool output
  inside the ToolBroker timeout envelope; final result envelope formatting remains
  outside that envelope.

Phase 3.5 changes selected structured native tool outputs, but it does not add a
new durable conversation artifact source shape. It uses the explicit
`ToolResult.status` values defined in `specs/native-tools.md`: `ok`, `error`,
`denied`, `timeout`, and `cancelled`.

ToolBroker must not add a deterministic call/audit signature field. Audit truth
is the existing event payload with normalized or redacted arguments, target,
status, duration, artifact ids, result, and error fields.

### Native Tool Handlers

Native handlers own concrete local filesystem behavior after ToolBroker allows a
call.

Responsibilities:

- `find_file`: perform controlled candidate enumeration and portable glob
  matching over authorized roots.
- `read_file`: read UTF-8 text with line pagination and update file metadata
  cache with whole-file hash.
- `list_dir`: list immediate children with deny, hidden, ignore, sort, and
  pagination rules.
- `search_text`: use controlled ripgrep invocation over a pre-filtered candidate
  file list.
- `edit_file`: perform LF-normalized exact replacement on existing files after
  stale-write guard.
- `write_file`: create or completely overwrite UTF-8 files, requiring
  stale-write guard for existing targets.

Handlers do not ask for approval, read mutable policy directly, write tool audit
events directly, or widen model-visible schemas.

### Portable Glob Matcher

Phase 3.5 uses a runtime-owned portable glob subset for `find_file.pattern` and
`search_text.glob`.

Data flow:

1. ToolBroker resolves the search root and obtains approval for that root and
   behavior scope.
2. Runtime enumerates candidate paths under the root using its own traversal.
3. Runtime applies builtin/user deny policy, hidden filtering, symlink rules, and
   file-only filtering.
4. Runtime converts each candidate to a `/`-separated path relative to the
   search root.
5. Runtime applies the portable glob matcher to that relative path.

The matcher may use Python standard-library `fnmatch.fnmatchcase()` for
segment-level `*`, `?`, and `[...]` matching. `**` is handled by runtime as a
complete path segment that matches zero or more directory levels.

The runtime must not delegate traversal to Python `glob.glob()` or
`Path.glob()` as the primary implementation because traversal must remain under
ToolBroker root approval, path policy, hidden, symlink, and denied-count rules.

### Search Text Ripgrep Boundary

`search_text` uses ripgrep only after runtime has produced a filtered candidate
file list.

Data flow:

1. validate schema and local semantic constraints.
2. resolve and approve the search root.
3. enumerate candidate files under the root.
4. apply path policy deny, hidden filtering, symlink rules, and optional glob
   filtering.
5. apply optional `type` filtering to the already authorized candidate file list
   using the Phase 3.5 runtime-owned type allowlist.
6. verify that `rg` is available and, for regex mode, that the pattern compiles
   through ripgrep even if no candidate files remain. The regex compile check
   uses a runtime-owned empty UTF-8 temporary file rather than workspace
   traversal or stdin.
7. if no candidates remain, return the selected output mode's empty success
   result without invoking the main search.
8. invoke ripgrep with `shell=False` argv, `--json`, `--regexp <pattern>`, `--`,
   and a runtime-filtered candidate file argv list.
9. parse ripgrep JSON records and normalize them into the documented result
   shapes.
10. preserve deterministic canonical path and line ordering while streaming,
   skipping, and collecting only the requested page plus one extra result item
   when possible.

`rg` exit code `1` for no matches is success with empty results. Missing `rg`,
regex compile errors, candidate argv/chunk execution failure, or other ripgrep
execution errors return `tool_error/tool_execution_failed`. Unknown `type` is a
pre-execution semantic validation failure and returns
`tool_error/tool_schema_invalid`.
Phase 3.5 does not inspect ripgrep's local type registry and does not fallback
to Python regex. Ripgrep execution must also be isolated from local ripgrep
configuration by using `--no-config` and a controlled child-process environment
that prevents `RIPGREP_CONFIG_PATH` from changing search semantics. Result
pagination must be independent of ripgrep discovery order by searching
canonical-path-sorted files or chunks and merging records by canonical path and
line number before pagination.

### File Metadata Cache

ToolBroker owns the volatile file metadata cache.

Responsibilities:

- store only current-process, session-runtime-local revision observations.
- key entries by canonical absolute path.
- update entries from successful `read_file`, guarded `edit_file`, guarded
  overwrite `write_file`, and create-new-file `write_file`.
- reject existing-file `edit_file` and overwrite `write_file` when the cache is
  missing or stale.
- serialize writes to the same canonical path inside the process.

The cache is intentionally not durable. Resume starts with an empty cache so the
model must re-read an existing file before editing or overwriting it.

### Approval And Audit

Approval has two distinct persisted surfaces:

- reusable grants use `approval_scope_signature`, as in Phase 1.
- audit events store normalized or redacted arguments.

Phase 3.5 does not add a second call-signature hash. This avoids expanding
persistence semantics while still preserving human-readable audit detail.

Audit payloads must preserve all behavior-affecting normalized arguments unless
a tool has explicit redaction rules. `view_image` keeps its existing redaction
rule and does not persist query text or query length in runtime-authored fields.

### Terminal Recovery Checkpoint Tool Availability

Phase 3 terminal recovery checkpoints already validate tool availability against
frozen session config through the Phase 3 checkpoint representation. Phase 3.5
extends those facts instead of introducing a complete tool-contract hash system.

Phase 3.5 preserves whichever Phase 3 checkpoint placement mechanism the
implementation already uses for tool availability. It updates only the facts
contained in that existing representation for `manifest_schema_version = 2`.
It must not introduce a new placement, migrate between the Phase 3 placement
forms, or require implementations to switch from one Phase 3 representation form
to another.

Because Phase 3.5 changes the terminal recovery checkpoint payload shape, fresh
Phase 3.5 terminal recovery checkpoints use `manifest_schema_version = 2`.
Resume validates this checkpoint payload version after the SQLite
`PRAGMA user_version = 4` gate passes.

The Phase 3.5 tool-availability facts record:

- a native tools contract marker.
- `shell_exec.max_timeout_seconds` facts.
- `view_image` enabled/disabled state and multimodal limits.

The tool-availability representation is checkpoint truth used for resume
validation. It is not a model message, tool result, or substitute for SQLite
schema version compatibility.
`agent_loop.max_tool_call_iterations` and
`execution.default_tool_timeout_seconds` remain frozen session config facts and
are not included in tool-availability facts.

### Observability

Phase 3.5 splits observability into two non-authoritative files:

- `.sessions/<session_id>/logs/trace.md`: a human-readable conversation
  transcript rendered from accepted durable conversation rows.
- `.sessions/<session_id>/logs/events.jsonl`: the renamed `engine.log`
  JSONL stream for run-event observations, audit facts, and runtime diagnostics.

Observability remains non-authoritative. Trace, events JSONL, TUI, and streaming
observations are not resume truth and must not be used to reconstruct runtime
state.

### Conversation Trace Renderer

The conversation trace renderer is a presentation component over durable
conversation truth.

Responsibilities:

- read accepted closed durable conversation rows ordered by
  `conversation_messages.message_index ASC`.
- validate schema version and durable conversation group completeness before
  rendering.
- render user input, assistant output, assistant tool-call groups with paired
  tool results, and runtime failure/cancellation facts.
- filter `context_summary` rows without replacement text.
- pair tool calls and tool results by durable `model_call_id + tool_call_id`.
- apply tool-result preview limits, tool-argument preview/redaction rules, and
  artifact-display rules.
- write trace output to a unique same-directory temporary file and atomically
  replace `logs/trace.md`.

The renderer must not read event timelines, current conversation projection
state, terminal checkpoint projection snapshots, existing Markdown trace files,
or `events.jsonl` as the trace body source.

Trace output is optimized for convenient human reading, not tamper-resistant
audit. User and assistant content is rendered as durable Markdown content
without escaping or sanitization. Emoji section headings are part of the output
format and no ASCII-only trace variant is provided.

### Events JSONL Writer

Phase 3.5 renames the per-session JSONL log path from
`logs/engine.log` to `logs/events.jsonl`.

The implementation-facing writer class is renamed from `EngineLogWriter` to
`EventsJsonlWriter`; the legacy name is not the canonical Phase 3.5 writer
contract.

The JSONL writer keeps the old authority model:

- `write_event_log` writes observations of persisted run events with
  `metadata.event_id`.
- `write_runtime_log` writes runtime diagnostic observations.
- neither output path is runtime truth.
- `status`, `trace`, `resume`, checkpoint validation, and recovery must not read
  JSONL to reconstruct runtime truth.
- legacy `logs/engine.log` compatibility is not implemented.

### View Image Trace Redaction Boundary

`view_image.query` follows a split redaction boundary:

- trace may show `query` when it appears in assistant-authored raw tool-call
  arguments stored in the durable conversation transcript.
- runtime-authored audit metadata, events JSONL metadata, status output, error
  metadata, `ToolResult.metadata`, approval scope, and other persisted
  runtime-authored fields must not copy concrete query text, query preview, or
  query length.

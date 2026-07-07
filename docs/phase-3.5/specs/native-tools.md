# Phase 3.5 Native Tools Specification

## Boundary

This spec defines Phase 3.5 model-visible native tool schemas, native tool
result contracts, approval scope rules, audit argument rules, pagination,
portable glob semantics, controlled ripgrep search, stale-write guard, and
tool-specific error mapping.

All model-visible tools continue to pass through ToolBroker. Tool handlers must
not bypass schema validation, path policy, shell policy, approval, timeout,
artifact handling, result normalization, or audit.

Phase 3.5 does not add MCP tools, subagent tools, workflow tools, PTY shell,
interactive shell, background shell, long-running shell runtime, or RenderDoc
business tools.

## Model-Visible Tool Set

Phase 3.5 exposes these generic tools, subject to frozen availability rules:

- `read_file`
- `list_dir`
- `find_file`
- `search_text`
- `write_file`
- `edit_file`
- `shell_exec`
- `activate_skill`
- `load_skill_resource`
- `todo`
- `view_image` only when enabled by the frozen multimodal config.

The old Phase 1 `search_text.query` schema is removed. `query` is not an alias
for `pattern`.

`activate_skill`, `load_skill_resource`, and `todo` are existing
runtime-control tools from earlier phases. Phase 3.5 does not remove, rename,
or deprecate them.

## Common Tool Rules

### Schema Validation

ToolBroker schema validation must support the features used by this spec:

- object type validation.
- string, integer, boolean, array, and object properties.
- required fields.
- `additionalProperties=false`.
- enum validation.
- `minimum` and `maximum`.
- `minItems` and `maxItems`.
- default injection.

Defaults must be injected into normalized arguments before approval scope,
audit arguments, and handler execution. Defaults must not remain schema
annotations only.

JSON booleans must not be accepted as integers for integer fields, even if the
implementation language treats booleans as an integer subtype.

Unknown fields fail with `tool_error/tool_schema_invalid`.

When a tool has semantic rules that depend on whether a field was explicitly
provided by the assistant, schema validation must preserve raw argument field
presence before default injection. In Phase 3.5 this is required for
`search_text.context` conflict detection: injected defaults for
`before_context` and `after_context` do not count as assistant-provided fields.

### Path Handling

Phase 3.5 native filesystem tools keep the current `path` style. They do not
add `absolute_path`, `file_path`, `directory`, or similar aliases.

For Phase 3.5 native filesystem tools, runtime accepts relative or absolute
path input where the tool defines `path`. Relative paths resolve against
`workspace_root`. Runtime canonicalizes paths before path policy, approval,
handler execution, and audit.

String path inputs that are present must be non-empty after trimming whitespace.
For Phase 3.5 this common string-path validation applies to native filesystem
tool `path` fields, `shell_exec.cwd`, and `view_image.paths[]`. Empty or
whitespace-only path strings return `tool_error/tool_schema_invalid`. Runtime
trims non-empty path strings before canonicalization.
Omitting a path is valid only for tools that explicitly define an omitted-path
default, such as `find_file.path` and `search_text.path`. The string `"."`
remains the explicit way to target `workspace_root`.

`load_skill_resource.path` is intentionally excluded from this common
workspace-path rule. It keeps the Phase 1 skill-local resource-path semantics:
the value is resolved inside the active frozen skill snapshot, absolute paths
and traversal outside the frozen resource set remain invalid, and it is not
canonicalized against `workspace_root`.

For tools other than `view_image` ordinary model-visible output, Phase 3.5
structured results expose canonical absolute paths. `view_image` ordinary output
keeps the Phase 2 display-path behavior. Its approval, policy, and audit paths
still use canonical paths internally.

For returned symlink file candidates in discovery/search tools, path-policy and
escape checks use the resolved target, but the model-visible result path is the
absolute normalized candidate path that matched under the approved root, not the
resolved target path. Result sorting, pagination, and de-duplication use this
candidate path. Audit metadata may include the resolved target path when needed
to explain the policy decision.

### Hidden And Deny

`include_hidden=true` affects only lexical dot-prefix filtering for ordinary
dot files and dot directories. It never overrides builtin or user path deny.

Hidden path means any path segment starts with `.`. Windows hidden attributes do
not participate in Phase 3.5 hidden detection.

Denied children must not be exposed by name, path, error message, or count,
except `search_text.skipped_files.denied`, which intentionally exposes only an
aggregate denied file-leaf count. Runtime must not enter denied directory
subtrees merely to compute this count; descendants hidden behind a denied
directory are unknown and uncounted.

Phase 3.5 inherits Phase 1 builtin path deny rules for every model-visible
filesystem tool and for classified `shell_exec` path tokens. At minimum, these
include:

- `.sessions/`
- `.git/`
- `node_modules/`
- `build/`
- `dist/`
- `.venv/`
- `__pycache__/`
- `.pytest_cache/`
- `~/.debug-agent/skills/`
- `<workspace_root>/.debug-agent/skills/`

`include_hidden=true`, `find_file.pattern`, `search_text.glob`,
`list_dir.ignore`, symlinks, pagination, stale-write guard, and approval cannot
override these denies. The skill-source denies apply only to the configured
global and project skill source roots; unrelated directories named `skills` are
not denied solely by name.

The only Phase 3.5 exception for `.sessions/` is a runtime-issued,
model-visible artifact path. When a successful tool result exposes an
`artifact_path` for an accepted ArtifactStore record, `read_file` may read that
exact artifact file through its ordinary pagination contract. This exception
does not allow listing, searching, writing, editing, executing, glob expansion,
directory traversal, or reading arbitrary `.sessions/` paths. It also does not
allow the model to treat `artifact_id` as a filesystem path. Runtime must
validate the supplied path against the accepted ArtifactStore record before
applying the exception.

### Pagination

Pagination fields:

| Tool | Fields | Default | Hard maximum | Offset unit |
| --- | --- | ---: | ---: | --- |
| `read_file` | `offset`, `limit` | 2000 | 2000 | 0-based line number |
| `list_dir` | `offset`, `limit` | 200 | 1000 | sorted entry item |
| `find_file` | `offset`, `maxResults` | 100 | 1000 | sorted file match item |
| `search_text` | `offset`, `maxResults` | 100 | 1000 | result item for selected `output_mode` |

Rules:

- `offset` defaults to `0` and must be integer `>= 0`.
- `limit` and `maxResults` default to the tool value above and must be integer
  `>= 1`.
- requests above hard maximum return `tool_error/tool_schema_invalid`.
- `total_returned` is this page's returned result-item count.
- `truncated=true` means a next page exists at `next_offset`.
- `truncated=false` means `next_offset=null`.
- when `truncated=true`, `next_offset = offset + total_returned` for
  `read_file`, `list_dir`, `find_file`, and every `search_text` output mode.
- ToolBroker large-output artifact handling remains independent from
  pagination. Pagination metadata describes result-set slicing; artifact
  metadata describes a single tool output externalized for size.
- The result contracts in this spec describe the logical structured output. When
  the ToolResult envelope externalizes a large field as an artifact, field-level
  artifact references are preferred. The model-visible tool observation must keep
  the documented metadata fields and an inline preview or artifact reference
  instead of inlining the complete large field. Artifacting must not remove
  pagination, guard, path, status, checksum, or other control metadata needed to
  understand the result.

### ToolResult Envelope And Durable Serialization

The result sections below define each tool's logical successful result object.
ToolBroker wraps that object in the standard `ToolResult` envelope:

```json
{
  "status": "ok",
  "output": {"tool_specific": "result"},
  "error": null,
  "artifacts": [],
  "metadata": {"tool_name": "read_file"},
  "redacted_output": null
}
```

Rules:

- Phase 3.5 uses exactly these `ToolResult.status` values for model-visible and
  durable tool-result serialization:
  - `ok`: handler completed successfully and `ToolResult.output` contains the
    documented logical result object.
  - `error`: schema validation failure, config failure, broker failure, handler
    failure, provider failure, persistence failure, or other non-authorization
    failure produced a normalized model-visible error projection.
  - `denied`: path policy, shell policy, or interactive approval denied the tool
    call before handler execution. Schema/config failures do not use
    `status="denied"` in Phase 3.5.
  - `timeout`: the brokered timeout envelope expired and the tool call produced
    no successful output.
  - `cancelled`: runtime cancellation stopped waiting for or collecting the
    tool result and produced a normalized cancellation projection.
- Phase 3.5 does not introduce any additional `ToolResult.status` values.
  `completed`, `success`, and `failed` are not Phase 3.5 `ToolResult.status`
  values.
- successful Phase 3.5 native tools store the documented logical result object in
  `ToolResult.output`.
- failed, denied, timed-out, or cancelled tool calls store the Phase 3 normalized
  model-visible error projection in `ToolResult.error`.
- `ToolResult.redacted_output` is a presentation preview for TUI/CLI/log surfaces
  only. It must not replace `ToolResult.output` as the provider-visible or
  durable conversation content unless a tool-specific earlier-phase contract
  explicitly says otherwise.
- provider-visible tool-loop observations for successful structured tools are
  derived from `ToolResult.output`. They must not be derived from
  `redacted_output`.
- provider-visible tool-loop observations for failed tool calls use the Phase 3
  model-visible error projection: `error_class`, `reason`, `message`, and
  `artifact_ids`.
- durable `conversation_messages` rows with `kind = "tool_result"` store the
  model-visible tool observation as `content_json` with this logical shape:

  ```json
  {
    "message_type": "tool_result",
    "tool_name": "read_file",
    "tool_call_id": "model_call_1_tool_1",
    "status": "ok",
    "content": {"path": "/abs/file.txt"},
    "error": null,
    "artifact_ids": [],
    "metadata": {}
  }
  ```

- for non-success statuses, `content` is `null` and `error` contains the
  model-visible normalized error projection.
- `artifact_ids` mirrors the model-visible artifact references attached to the
  `ToolResult`. Runtime may keep richer artifact metadata in the artifact store
  and audit events, but those stores remain the authority for artifact checksums
  and paths.
- `metadata` is allowlisted, non-secret, model-visible metadata only. It must not
  contain policy internals, approval grant internals, process owner facts, raw
  provider request/response objects, image bytes/base64, or concrete
  `view_image.query` text/preview/length.

Durable serialization matrix for Phase 3.5 native-tool results:

| Case | `ToolResult.output` | Provider-visible observation | Durable `tool_result` content | `artifact_ids` | ArtifactStore |
| --- | --- | --- | --- | --- | --- |
| inline success | logical successful result object | derived from `ToolResult.output` | `content` stores the same logical result object inline | `[]` unless the result also exposes earlier artifact refs | no new large-output artifact required |
| field-level artifact success | logical result object after selected large fields are replaced by artifact reference objects | derived from the artifact-referenced `ToolResult.output` | `content` stores the same artifact-referenced result object inline | includes every inline field-level artifact id and no unrelated ids | each referenced artifact has an accepted ArtifactStore record before the tool result is exposed |
| native observation still too large after field-level artifacting | `null` | model-visible normalized error projection | inline `content=null` and `error` stores `tool_error/tool_execution_failed` | `[]` unless a completed diagnostic artifact is intentionally exposed | no row-level native-tool observation artifact is created |
| non-success | `null` | model-visible normalized error projection | inline `content=null` and `error` stores `error_class`, `reason`, `message`, and `artifact_ids` | mirrors exposed diagnostic artifact ids, usually `[]` | optional diagnostic artifacts only; no incomplete large-output artifact may be exposed |

Minimum non-success diagnostic rules:

- Phase 3.5 does not define a detailed native-tool failure audit schema.
- Model-visible non-success observations keep the Phase 3 projection:
  `error_class`, `reason`, `message`, and `artifact_ids`.
- When `write_file` fails or times out after creating parent directories, audit
  must record the known minimal side-effect facts:
  `side_effects.created_directories`, `file_write_completed=false`, and
  `cache_updated=false`.
- `shell_exec` nonzero exit keeps the existing Phase 1/3 behavior: the failure
  uses `tool_error/shell_nonzero_exit`, the message prefers concrete stderr/stdout
  text when available, and large stdout/stderr may be exposed only through
  diagnostic artifacts referenced by `artifact_ids`.
- Timeout, cancellation, or artifact registration failure must not expose an
  incomplete artifact id. Diagnostic artifacts are optional completed diagnostics;
  they are not row-level successful tool-result fallback and do not change Phase 3
  error taxonomy.

Large-output artifacting rules:

- Phase 3.5 inherits the existing ToolBroker, ArtifactStore, and durable
  `conversation_messages` artifacting mechanisms from earlier phases for
  non-native-tool conversation rows and earlier contracts. Successful Phase 3.5
  structured native-tool `tool_result` rows, however, must remain inline after
  any documented field-level artifacting pass.
- The result objects in this spec are the normalized logical successful outputs
  produced by handlers before the existing large-output handling decides how much
  can remain inline.
- Phase 3.5 uses a deterministic minimal field-level artifacting plan. It reuses
  the existing ToolBroker inline/artifact threshold and does not add a separate
  field-level threshold or configuration key. If the complete native-tool
  model-visible observation exceeds the durable conversation inline threshold,
  ToolBroker externalizes eligible fields in the stable order below until the
  observation fits inline or until no eligible fields remain. ToolBroker may
  externalize only these logical
  successful-output fields:
  - `read_file.content`
  - `search_text.matches`
  - `search_text.paths`
  - `search_text.counts`
  - `shell_exec.stdout`
  - `shell_exec.stderr`
- Field-level artifacting replaces the selected field's complete value with an
  artifact reference object and keeps the surrounding structured result fields
  inline. Phase 3.5 does not recursively externalize nested subfields or split a
  single logical field across multiple artifacts.
- A field does not need to individually exceed the inline threshold to be
  externalized. The trigger is the complete native-tool observation exceeding
  the inline threshold; stable field order determines the minimal set of
  eligible fields to externalize. If more than one externalizable field is
  selected in the same result, ToolBroker externalizes those fields independently
  in stable field order as listed above.
- If the complete Phase 3.5 native-tool model-visible observation still exceeds
  the durable conversation inline threshold after the deterministic field-level
  artifacting pass, ToolBroker must not accept an oversized successful
  `tool_result` row and must not switch that native-tool observation to a
  row-level artifact-backed conversation row. The call returns
  `status="error"` with `tool_error/tool_execution_failed` and a message asking
  the model to narrow the request, reduce pagination, or otherwise produce a
  smaller result.
- The artifact reference object uses this logical shape:

  ```json
  {
    "artifact_id": "art_...",
    "artifact_path": ".sessions/sess_.../artifacts/art_....txt",
    "relative_path": "sess_.../artifacts/art_....txt",
    "preview": "bounded inline preview",
    "truncated": true,
    "bytes": 12345,
    "sha256": "sha256:..."
  }
  ```

- `relative_path` is the artifact store relative path recorded for the artifact.
  It must match the durable `ArtifactStore` record for `artifact_id`. The
  artifact store remains authoritative for artifact path, checksum, and metadata
  validation.
- `artifact_path` is the model-visible filesystem path that may be passed to
  `read_file` when the model needs the externalized text content. It must point
  to the same accepted artifact as `artifact_id` and `relative_path`, and it
  must be readable only through the runtime-issued artifact-path exception
  defined above. Runtime may omit `artifact_path` for artifacts that are not
  intended to be model-readable, including audit-only diagnostics, redacted
  internal payloads, non-text assets, or artifacts whose content should be
  referenced but not exposed through `read_file`.
- Phase 3.5 uses this checksum string convention: file-content SHA fields such as
  `sha256`, `sha256_before`, `sha256_after`, `content_sha256`, and trace
  redaction hashes are lowercase raw hex without a `sha256:` prefix; artifact and
  checkpoint checksum fields use the existing `sha256:<hex>` form.
- Artifact registration must be atomic with respect to runtime truth. ToolBroker
  writes large fields to a unique artifact temporary file, computes the required
  metadata, atomically moves the file into its final artifact path, and only then
  commits the accepted ArtifactStore record and exposes the artifact id in
  `ToolResult.artifacts` or durable conversation content.
- If artifact writing or registration fails, times out, or is cancelled before
  the accepted ArtifactStore record is committed, the tool call must not expose
  the artifact id, must not append `artifact_ids`, and must not persist an
  accepted conversation row that references the incomplete artifact. Temporary
  artifact files are cleaned up best-effort; any leftover temporary file is not
  runtime truth and must not be referenced by status, trace, resume, checkpoint
  validation, or recovery.
- When a successful tool result exposes an artifact reference object in
  model-visible content, `ToolResult.artifacts` and durable
  `tool_result.artifact_ids` must include that artifact id exactly once. This
  applies equally to native field-level artifacting, runtime-control tools such
  as `load_skill_resource`, and any other successful tool result that exposes a
  model-visible artifact reference.
- `preview` is optional when the large value is not safe or cheap to preview. If
  present, it must be bounded and derived before or during artifact registration,
  not by later reading arbitrary artifact paths.
- Artifacting must not remove or externalize control fields needed for
  pagination, stale-write guard, path identity, status, checksums, byte counts, or
  result interpretation.
- Conversation trace may render an artifact reference and any already-inline
  redacted preview, but must not read large artifact body content merely to
  enrich trace output.

### Case Sensitivity Defaults

`find_file.case_sensitive` defaults to `false` because filename discovery is
usually an ergonomic lookup operation across filesystems with different case
behavior. `search_text.case_sensitive` defaults to `true` because Phase 1
literal text search was case-sensitive and regex searches should preserve exact
matching unless the model asks otherwise.

When a portable glob tool uses `case_sensitive=false`, matcher-level case
folding uses Python `str.casefold()`. Result sorting still uses canonical path
strings in their original case, not case-folded keys.

### Streaming I/O And Timeout Boundary

Phase 3.5 does not define per-file or per-tree byte-size limits for generic text
tools. Instead, `read_file`, `find_file`, `list_dir`, `search_text`, and stale
write guard revision checks must run inside the ToolBroker timeout envelope and
use streaming or bounded-memory implementation techniques.

Required behavior:

- `read_file` may compute whole-file SHA-256 while streaming raw bytes and must
  not require loading the complete file into memory solely to hash it.
- `read_file` may collect only the requested line page plus the minimal state
  needed to decide `truncated` and `next_offset`.
- `search_text` candidate enumeration, UTF-8 pre-screening, type filtering, and
  ripgrep JSON parsing must avoid accumulating unbounded file contents in memory.
- `find_file` may accumulate result path metadata needed for deterministic
  sorting and pagination.
- `search_text` must process the complete authorized candidate stream
  deterministically for the normalized parameters before returning success, but
  it need not materialize the full result set in memory. It may stream candidate
  files in canonical path order, skip `offset` result items, retain only the
  requested page plus one extra item needed to decide `truncated`, and maintain
  aggregate skipped-file counters.
- `search_text` may invoke ripgrep per file or in bounded candidate chunks. Chunk
  size, parser buffer limits, and argv-safety limits are internal settings, not
  `config.toml` fields or model-visible contract fields.
- If `search_text` encounters a ripgrep JSON record or line payload that exceeds
  an internal parser safety limit, runtime treats that candidate file as an
  ordinary non-deny, non-hidden, non-decode skipped file and increments
  `skipped_files.other`. Runtime must not return an oversized line payload as a
  successful page item.
- `find_file` and `search_text` must not report partial success if the ToolBroker
  timeout fires before the deterministic candidate stream processing required for
  the selected parameters completes.
- ToolBroker timeout for these paths returns
  `tool_error/tool_execution_timeout`; it must not advance the file metadata
  cache, write guarded file changes, or return a partial successful page.
- The ToolBroker timeout envelope starts after interactive approval has completed
  and immediately before handler, traversal, provider, or command work begins. It
  includes handler work, traversal, provider/command work, and ArtifactStore
  registration or artifact writes caused by large tool output. It excludes
  interactive approval wait time, audit emission, and final result envelope
  formatting.

### Error Mapping

Use Phase 3 normalized error taxonomy.

- Execution order for Phase 3.5 native tools is:
  schema validation and default injection -> execution-before local semantic
  validation -> path canonicalization and policy -> approval -> handler execution
  -> artifact handling -> result normalization -> audit.
- Audit is still emitted at the relevant ToolBroker boundary for started,
  denied, completed, failed, timed-out, and cancelled calls. The ordered pipeline
  above describes the decision and normalization flow; it does not mean audit is
  only a single final write after every call.
- schema shape errors, unknown fields, missing required fields, invalid enum,
  min/max violations, and execution-before local semantic validation failures:
  `tool_error/tool_schema_invalid`.
- ordinary handler failures after execution begins:
  `tool_error/tool_execution_failed`.
- brokered timeout: `tool_error/tool_execution_timeout`.
- `shell_exec` nonzero process exit: `tool_error/shell_nonzero_exit`.

Specific details go in message and allowlisted metadata. Phase 3.5 must not add
tool-specific reason symbols.

If a call contains both an execution-before semantic error and a path that would
later be denied, the semantic validation error wins because no filesystem target
has been authorized or traversed yet. Examples include empty-after-trim
`find_file.pattern`, unsupported `find_file.pattern` syntax,
`search_text.pattern` CR/LF, unsupported `search_text.glob` syntax, unknown
`search_text.type`, invalid
`search_text.context` combinations, and empty `edit_file.old_text`.

For `search_text`, runtime verifies that `rg` is available after root approval
and before returning any successful empty page. In regex mode, runtime also
verifies the pattern through ripgrep after root approval and before returning any
successful empty page. If candidate enumeration or UTF-8 pre-screening times out
before those checks complete, the call returns
`tool_error/tool_execution_timeout`. Missing `rg` and regex compile failures
return `tool_error/tool_execution_failed`.

Once semantic validation needs a canonical target, path policy and approval are
evaluated before any handler traversal, file read/write, ripgrep invocation,
shell execution, or provider call. Denied roots and denied explicit target paths
therefore return policy denial before execution begins.

### Approval Scope And Audit Arguments

Phase 3.5 keeps only one reusable approval signature mechanism:
`approval_scope_signature`.

Phase 3.5 does not add a deterministic call/audit signature. Tool audit events
persist normalized or redacted arguments.

Reusable approval scope:

| Tool | Scope fields |
| --- | --- |
| `view_image` | `tool`, `access=read`, ordered canonical image paths. Excludes `query`, query source, image metadata, hashes, provider, model, timeout, and request-size projection. |
| `read_file` | `tool`, `access=read`, canonical path. |
| `list_dir` | `tool`, `access=read`, canonical path, `ignore`, `include_hidden`. |
| `find_file` | `tool`, `access=read`, canonical root, `pattern`, `case_sensitive`, `include_hidden`. |
| `search_text` | `tool`, `access=read`, canonical root, `pattern`, `glob`, `case_sensitive`, `fixed_strings`, `type`, `output_mode`, `before_context_effective`, `after_context_effective`, `include_hidden`. Excludes only `offset` and `maxResults`. |
| `edit_file` | `tool`, `access=write`, canonical path, `replace_all`. Excludes `old_text` and `new_text`. |
| `write_file` | `tool`, `access=write`, canonical path, canonical planned parent directories to create. Excludes `content`. |
| `shell_exec` | `tool`, `access=execute`, normalized argv, canonical cwd, effective timeout, classified argv paths. |
| `activate_skill` | Phase 1 scope: skill `name` and frozen skill `content_hash`. |
| `load_skill_resource` | Phase 1 scope: skill `name`, skill content hash, resource path, resource kind, and resource content hash. |
| `todo` | Phase 2/3 runtime-control rules. Phase 3.5 does not add a new reusable approval scope or approval grant behavior for `todo`. |

Excluding `edit_file.old_text`, `edit_file.new_text`, and `write_file.content`
from reusable approval scope is an intentional session-local approval tradeoff.
It means a reusable grant authorizes later same-scope edits or writes to the
same target even when the replacement text or file content differs. The
operation remains constrained by path policy, approval mode, stale-write guard,
and normalized/redacted audit arguments.

Audit arguments:

- include all normalized behavior-affecting arguments unless a tool has explicit
  redaction rules.
- include pagination parameters.
- include `search_text.type`, `before_context_effective`,
  `after_context_effective`, `output_mode`, `fixed_strings`, `case_sensitive`,
  `include_hidden`, and `glob`.
- include `write_file.content_sha256` and `write_file.content_bytes`, not full
  content beyond the normal model-visible tool transcript.
- include `edit_file.old_text_sha256`, `edit_file.old_text_bytes`,
  `edit_file.new_text_sha256`, and `edit_file.new_text_bytes`, not full
  replacement text beyond the normal model-visible tool transcript.
- `view_image` audit arguments must redact query text and query length,
  recording only `effective_query_source = "assistant"` or `"default"`.

## Portable Glob Subset

`find_file.pattern` and `search_text.glob` use the Phase 3.5 portable glob
subset.

Supported:

- `*`: matches zero or more characters within one path segment; does not cross
  `/`.
- `?`: matches exactly one character within one path segment; does not cross
  `/`.
- `[...]`: character class for one character within one path segment; does not
  cross `/`. Negated character classes are not supported.
- `**`: only when it is a complete path segment. It matches zero or more
  directory levels.

Not supported:

- brace expansion such as `{a,b}`.
- extglob such as `!(...)`, `?(...)`, `+(...)`, `*(...)`, `@(...)`.
- escape syntax such as `\*` or `\[`; backslash has no escape semantics in the
  Phase 3.5 portable glob subset.
- any backslash in a portable glob pattern. Phase 3.5 glob paths always use `/`
  separators and do not treat `\` as a literal segment character.
- negated character classes such as `[!a]` or `[^a]`.
- malformed character classes, such as `[` or `[abc`.
- `**` inside a path segment, such as `foo**bar` or `a**`.
- node minimatch behavior outside the supported subset.

Unsupported glob syntax returns `tool_error/tool_schema_invalid`.

Runtime must perform controlled traversal itself and then apply the matcher to
relative `/`-separated paths. It must not delegate traversal to Python
`glob.glob()` or `Path.glob()` as the primary implementation.

## Runtime-Owned Text Type Allowlist

`search_text.type` uses a fixed Phase 3.5 runtime-owned allowlist. Runtime must
not inspect `rg --type-list`, `.ripgreprc`, project-local ripgrep config,
environment-specific custom types, or user-defined ripgrep type additions to
decide type membership.

Allowed values and portable glob patterns:

| Type | Patterns |
| --- | --- |
| `c` | `**/*.c`, `**/*.h` |
| `cpp` | `**/*.cc`, `**/*.cpp`, `**/*.cxx`, `**/*.hh`, `**/*.hpp`, `**/*.hxx` |
| `csharp` | `**/*.cs` |
| `css` | `**/*.css` |
| `go` | `**/*.go` |
| `html` | `**/*.html`, `**/*.htm` |
| `java` | `**/*.java` |
| `javascript` | `**/*.js`, `**/*.mjs`, `**/*.cjs`, `**/*.jsx` |
| `json` | `**/*.json`, `**/*.jsonl` |
| `markdown` | `**/*.md`, `**/*.markdown` |
| `python` | `**/*.py`, `**/*.pyi` |
| `rust` | `**/*.rs` |
| `shell` | `**/*.sh`, `**/*.bash`, `**/*.zsh` |
| `text` | `**/*.txt` |
| `toml` | `**/*.toml` |
| `typescript` | `**/*.ts`, `**/*.tsx` |
| `yaml` | `**/*.yaml`, `**/*.yml` |

The allowlist is intentionally generic and not shader-, RenderDoc-, or
business-domain-specific. Users who need other file families can use
`search_text.glob` directly. Adding, removing, or changing type definitions is a
future tool-contract change.

Type matching is case-insensitive over candidate relative paths by applying
Python `str.casefold()` before matching the allowlist patterns. This affects only
`search_text.type` file-family filtering; it does not change
`search_text.case_sensitive`, which controls content matching.

## ToolBroker File Metadata Cache

### Cache Shape

Cache key is canonical absolute path.

Each entry contains:

```json
{
  "sha256": "hex",
  "size": 123,
  "mtime_ns": 1234567890,
  "observed_at": "2026-06-10T00:00:00Z",
  "source_tool": "read_file"
}
```

`source_tool` is one of:

- `read_file`
- `edit_file`
- `write_file`

The cache is volatile runtime state only.

### Cache Updates

- successful `read_file` computes whole-file raw bytes SHA-256 and updates the
  cache.
- successful guarded `edit_file` updates the cache to the new revision.
- successful overwrite `write_file` updates the cache to the new revision.
- successful create-new-file `write_file` creates a cache entry with
  `source_tool="write_file"`.

`search_text`, `list_dir`, `find_file`, and `view_image` do not create entries
usable for write guard.

### Stale-Write Guard

`edit_file` modifying any existing file and `write_file` overwriting any
existing file must use the guard.

Rules:

- missing cache entry fails with `tool_error/tool_execution_failed` and a
  message requiring `read_file` first.
- before writing, ToolBroker re-reads current raw bytes and computes SHA-256.
- current SHA-256 mismatch fails with `tool_error/tool_execution_failed` and a
  message requiring `read_file` again.
- existing empty files still require a cache entry.
- create-new-file `write_file` does not require a pre-write cache entry.
- `edit_file` never creates a file.
- same-process writes to the same canonical path must be serialized with an
  in-process lock.
- the guard is best-effort stale detection, not a cross-process compare-and-swap
  guarantee.
- after the guard succeeds, `edit_file` and overwrite `write_file` must write
  the complete new content to a unique temporary file in the same directory as
  the target and then atomically replace the target. Phase 3.5 does not promise
  crash-consistency or fsync-grade durability.

Successful write result `guard` shape:

```json
{
  "used": true,
  "cache_source": "read_file"
}
```

`cache_source` is `"read_file"`, `"edit_file"`, `"write_file"`, or `null`.

Create-new-file `write_file` result:

```json
{
  "used": false,
  "cache_source": null
}
```

## `view_image`

### ToolDefinition

```json
{
  "name": "view_image",
  "description": "Inspect one to four local PNG or JPEG images.",
  "input_schema": {
    "type": "object",
    "properties": {
      "paths": {
        "type": "array",
        "description": "Local filesystem paths to PNG or JPEG images to inspect. Paths are checked by runtime path policy before files are read.",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 4
      },
      "query": {
        "type": "string",
        "description": "Optional analysis focus for the image(s). If omitted, runtime uses the default image-inspection query. If provided, it must be non-empty after trimming whitespace and must not exceed the frozen max_query_chars limit."
      }
    },
    "required": ["paths"],
    "additionalProperties": false
  },
  "category": "native",
  "risk_level": "read",
  "access": ["read"]
}
```

### Behavior

Phase 3.5 does not change `view_image` capability. It remains the Phase 2
image-only local PNG/JPEG tool.

Its malformed input error mapping follows the Phase 3 normalized tool-boundary
taxonomy. Phase 3.5 is a breaking tool-contract update relative to Phase 2 and
does not preserve the older Phase 2 `user_error` wording for invalid `query`
input.

Phase 3.5 preserves:

- `paths` with one to four local image paths.
- each path string must be non-empty after trimming whitespace.
- optional `query`.
- omitted `query` uses runtime default query.
- provided `query` is trimmed and must be non-empty and no longer than frozen
  `max_query_chars`.
- non-string, whitespace-only, or over-limit query returns
  `tool_error/tool_schema_invalid`.
- no URL, base64, artifact id, video, audio, or general multimedia input.
- ordinary output uses Phase 2 display path behavior rather than Phase 3.5
  canonical-absolute-path output.
- runtime-authored metadata, audit, events JSONL, status, and error metadata do
  not include concrete query text, raw query argument, query preview, or query
  length.
- Phase 3.5 conversation trace may render `query` only when it is present in
  assistant-authored raw tool-call arguments stored in durable
  `assistant_tool_call.content.tool_calls[].args`; this exception does not allow
  runtime-authored metadata to copy query text, preview, or length.
- ordinary output remains the current JSON object with `analysis` as the primary
  model-visible result field. Phase 3.5 does not expand `view_image` output to
  Codely multimedia output shapes.

## `read_file`

### ToolDefinition

```json
{
  "name": "read_file",
  "description": "Read UTF-8 text file contents with line-based pagination.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "offset": {"type": "integer", "minimum": 0, "default": 0},
      "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 2000}
    },
    "required": ["path"],
    "additionalProperties": false
  },
  "category": "native",
  "risk_level": "read",
  "access": ["read"]
}
```

### Behavior

- reads UTF-8 text only.
- `offset` is 0-based line number.
- `limit` is returned line count.
- `offset` beyond EOF succeeds with empty content,
  `total_returned=0`, `truncated=false`, and `next_offset=null`.
- line splitting follows Python `splitlines(keepends=True)` equivalent
  semantics. A final line without newline still counts as one line.
- UTF-8 decode failure returns `tool_error/tool_execution_failed`.
- successful read computes whole-file raw byte SHA-256 and updates the
  ToolBroker file metadata cache even when only a slice is returned.

### Result

```json
{
  "path": "/abs/path/file.txt",
  "content": "returned text slice",
  "offset": 0,
  "limit": 2000,
  "total_returned": 120,
  "truncated": false,
  "next_offset": null,
  "sha256": "whole-file-raw-bytes-sha256",
  "bytes": 12345
}
```

`bytes` is whole-file raw byte size, not returned-slice byte size.

## `list_dir`

### ToolDefinition

```json
{
  "name": "list_dir",
  "description": "List immediate directory entries with filtering and pagination.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "ignore": {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 100,
        "default": []
      },
      "include_hidden": {"type": "boolean", "default": false},
      "offset": {"type": "integer", "minimum": 0, "default": 0},
      "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200}
    },
    "required": ["path"],
    "additionalProperties": false
  },
  "category": "native",
  "risk_level": "read",
  "access": ["read"]
}
```

### Behavior

- lists immediate children only.
- denied children are omitted and not counted.
- hidden children are omitted unless `include_hidden=true`.
- `ignore` patterns are relative to the listed directory and affect immediate
  child names only.
- `ignore` supports literal child names plus `*` and `?` over one immediate
  child name segment. It does not support character classes.
- an ignore pattern without `/` matches an immediate child file or directory
  name.
- for directory entries only, `foo/` and `foo/**` are accepted aliases for the
  immediate child directory named `foo`. `foo/**` is not recursive matching for
  descendants.
- all other `/` usage, including `a/b`, `**`, `*.py/`, and nested directory
  patterns, is unsupported.
- brace expansion, extglob, bracket syntax, backslash escape semantics,
  recursion, and cross-directory matching are unsupported.
- any backslash in an `ignore` pattern is unsupported.
- unsupported syntax returns `tool_error/tool_schema_invalid`.
- `ignore` defaults to `[]` and accepts at most 100 patterns.
- filtering order: path policy deny -> hidden -> ignore -> sort -> pagination.
- sort order is entry `name` ascending, case-sensitive, with no directory-first
  grouping.

### Result

```json
{
  "path": "/abs/path",
  "entries": [
    {
      "name": "file.txt",
      "type": "file"
    }
  ],
  "offset": 0,
  "limit": 200,
  "total_returned": 1,
  "truncated": false,
  "next_offset": null
}
```

`entries[].type` is one of `"file"`, `"directory"`, `"symlink"`, or `"other"`.

## `find_file`

### ToolDefinition

```json
{
  "name": "find_file",
  "description": "Find files by glob pattern under an authorized path.",
  "input_schema": {
    "type": "object",
    "properties": {
      "pattern": {"type": "string"},
      "path": {"type": "string"},
      "case_sensitive": {"type": "boolean", "default": false},
      "include_hidden": {"type": "boolean", "default": false},
      "maxResults": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
      "offset": {"type": "integer", "minimum": 0, "default": 0}
    },
    "required": ["pattern"],
    "additionalProperties": false
  },
  "category": "native",
  "risk_level": "read",
  "access": ["read"]
}
```

### Behavior

- `path` omitted searches the single `workspace_root`.
- `path` provided resolves by normal debug-agent path rules and becomes the
  search root after path policy and approval.
- enumeration begins only after root approval succeeds.
- `pattern` is trimmed only for validation and must be non-empty after trimming;
  matching uses the original pattern value.
- `pattern` is matched against `/`-separated paths relative to the search root.
- only the Phase 3.5 portable glob subset is supported.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- returns files only; directories are never result items.
- default skips hidden paths.
- `case_sensitive=false` performs matcher-level case folding and does not depend
  on filesystem case behavior. Case folding uses Python `str.casefold()`.
- result sorting still uses the canonical absolute path string, not the
  case-folded string.
- does not recursively follow symlink directories.
- symlink files may be returned only if their resolved target canonicalizes and
  passes path policy. Symlink targets escaping allowed scope or hitting deny are
  skipped. Returned path values use the absolute normalized symlink candidate
  path that matched the pattern, not the resolved target path.
- denied paths are skipped without names or counts.
- result order is canonical absolute path ascending.

### Result

```json
{
  "root": "/abs/search/root",
  "pattern": "**/*.py",
  "matches": [
    "/abs/search/root/app.py"
  ],
  "offset": 0,
  "maxResults": 100,
  "total_returned": 1,
  "truncated": false,
  "next_offset": null
}
```

## `search_text`

### ToolDefinition

```json
{
  "name": "search_text",
  "description": "Search UTF-8 text files with ripgrep-compatible pattern matching under authorized paths.",
  "input_schema": {
    "type": "object",
    "properties": {
      "pattern": {"type": "string"},
      "path": {"type": "string"},
      "glob": {"type": "string"},
      "output_mode": {
        "type": "string",
        "enum": ["content", "files_with_matches", "count"],
        "default": "content"
      },
      "maxResults": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
      "offset": {"type": "integer", "minimum": 0, "default": 0},
      "case_sensitive": {"type": "boolean", "default": true},
      "fixed_strings": {"type": "boolean", "default": false},
      "before_context": {"type": "integer", "minimum": 0, "maximum": 10, "default": 0},
      "after_context": {"type": "integer", "minimum": 0, "maximum": 10, "default": 0},
      "context": {"type": "integer", "minimum": 0, "maximum": 10},
      "include_hidden": {"type": "boolean", "default": false},
      "type": {"type": "string"}
    },
    "required": ["pattern"],
    "additionalProperties": false
  },
  "category": "native",
  "risk_level": "read",
  "access": ["read"]
}
```

Phase 3.5 does not define `multiline`. Supplying `multiline` is an unknown-field
schema error.

### Behavior

- `path` omitted searches the single `workspace_root`.
- `path` provided resolves by normal debug-agent path rules and becomes the
  search root after path policy and approval.
- enumeration begins only after root approval succeeds.
- `pattern` is trimmed only for validation and must be non-empty after trimming;
  ripgrep receives the original pattern value.
- `pattern` containing `\r` or `\n` returns
  `tool_error/tool_schema_invalid`. Phase 3.5 search is line-oriented and does
  not define multiline pattern semantics for regex or fixed-string mode.
- runtime enumerates candidate files, applies path policy, hidden filtering,
  symlink rules, optional `glob`, and UTF-8 pre-screening, then invokes the main
  ripgrep search with `shell=False` argv over only the filtered candidate files
  when candidates remain.
- `glob`, when present, is relative to the search root and uses the Phase 3.5
  portable glob subset.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- `fixed_strings=false` means `pattern` is ripgrep/Rust regex.
- `fixed_strings=true` means `pattern` is literal.
- after root approval, runtime must verify `rg` availability before returning a
  successful result, including the no-candidate success case.
- after root approval, when `fixed_strings=false`, runtime must verify the regex
  pattern through ripgrep before returning a successful result, including the
  no-candidate success case. When `fixed_strings=true`, runtime performs only
  the required `rg` availability check; no regex compilation check is required.
- regex compile failures return `tool_error/tool_execution_failed` with a
  message in the form
  `rg execution failure: <short ripgrep diagnostic>`.
- `case_sensitive` controls ripgrep case behavior.
- `type` is part of reusable approval scope. It can only narrow the already
  runtime-authorized candidate file list; it must not cause ripgrep to traverse
  or discover additional files.
- runtime applies `type` before search by matching the Phase 3.5 runtime-owned
  type allowlist against the already authorized candidate list. Runtime must not
  rely on `rg --type` to filter explicit candidate file argv because ripgrep does
  not reliably apply type filters to explicit file arguments.
- unknown `type` returns `tool_error/tool_schema_invalid`.
- `rg` missing returns `tool_error/tool_execution_failed`.
- `rg` exit code `1` for no matches is success with empty results.
- no Python regex fallback exists.
- `context` is mutually exclusive with assistant-provided `before_context` and
  assistant-provided `after_context`. Injected default values for
  `before_context` and `after_context` do not make a call with only
  `context=N` invalid.
  Providing `context` with either explicit field returns
  `tool_error/tool_schema_invalid`.
- `context=N` means `before_context=N` and `after_context=N`.
- after schema/default normalization and raw-field conflict validation, approval
  scope and audit arguments must use `before_context_effective` and
  `after_context_effective`. When `context` is provided, both effective values
  equal `context`; otherwise they come from normalized `before_context` and
  `after_context`.
- when `output_mode` is `files_with_matches` or `count`, `context`,
  `before_context`, and `after_context` are accepted but have no effect on the
  result shape. They remain in reusable approval scope to keep signatures
  conservative and audit-complete.
- for `output_mode=content`, pagination applies to sorted match result items
  before context rows are attached. Runtime selects the page of matches using
  `offset` and `maxResults`, then attaches context rows for only those matches.
- runtime does not ask ripgrep to return context for the full candidate set. For
  `output_mode=content`, context attachment happens after matching-line
  pagination by bounded runtime reads of only the files needed for the selected
  page.
- if context attachment cannot read, stat, or decode a selected-page file after
  ripgrep has returned matches, the call fails with
  `tool_error/tool_execution_failed` and must not return a partial successful
  page.
- `search_text` is line-oriented. A result item is one matching line, not one
  regex submatch. If a regex or fixed-string pattern matches multiple spans on the
  same line, that line is returned and counted once.
- context lines do not count against `maxResults` or `total_returned`.
- for `output_mode=content`, `next_offset` is `offset + total_returned` when
  `truncated=true`; context rows may repeat across pages when adjacent pages
  request matches whose context windows overlap.
- same-file context lines are de-duplicated by line number. Match lines win over
  context lines and use `is_context=false`.
- content results are sorted by canonical path ascending, then 1-based
  `line_number` ascending, then match before context for the same line.
- `files_with_matches` and `count` results are sorted by canonical path
  ascending.
- `output_mode=count` reports the number of matching lines per file. It does not
  report the number of regex captures or repeated submatches within a line.
- line preview maximum is 4000 UTF-8 codepoints. Longer lines are truncated and
  set `line_truncated=true`.
- skipped file counters are aggregate file-leaf counts only. `denied` count
  intentionally reveals only the number of denied candidate file leaves that were
  safely enumerable before a deny boundary, not their names or paths. Runtime
  must not recurse into denied directory subtrees merely to count descendants.
- `hidden` counts hidden candidate file leaves skipped by lexical dot-prefix
  filtering. Runtime must not recurse into hidden directory subtrees when
  `include_hidden=false` merely to count hidden descendants.
- `skipped_files.decode_error` is computed during runtime UTF-8 pre-screening and
  from ripgrep JSON records whose byte payload cannot be decoded as UTF-8 for the
  documented line preview. Decode-error counters must not include path names.
- symlink files whose resolved target escapes allowed scope or hits a deny rule
  are skipped and counted in `skipped_files.other`. Returned match paths for
  allowed symlink files use the absolute normalized symlink candidate path that
  matched under the approved root, not the resolved target path.
- file leaves that vanish, cannot be stat/read for ordinary filesystem reasons,
  or fail candidate pre-screening for non-deny, non-hidden, non-decode reasons
  are counted in `skipped_files.other`.
- if no candidate files remain after deny, hidden, symlink, glob, UTF-8
  pre-screening, and optional type filtering, runtime returns a successful empty
  result for the selected output mode only after the required `rg` availability
  and regex compilation checks have passed. It does not invoke the main ripgrep
  search over candidates.

Ripgrep invocation rules:

- runtime builds argv with `shell=False`; it must not build a shell command
  string.
- argv must include `--json`, `--no-config`, `--regexp`, the pattern value, `--`,
  and candidate file paths as separate argv elements.
- runtime must execute ripgrep with a controlled environment that prevents local
  ripgrep configuration from changing search semantics. At minimum,
  `RIPGREP_CONFIG_PATH` must be unset or set to an inert value for the ripgrep
  child process.
- argv must not include ripgrep context flags such as `--context`,
  `--before-context`, or `--after-context`.
- file paths with spaces, parentheses, shell metacharacters, or leading `-` are
  safe because they are separate argv elements after `--`.
- `fixed_strings=true` adds the ripgrep fixed-string flag.
- `case_sensitive=false` adds the ripgrep ignore-case flag.
- `type` is not passed to the search invocation after runtime has applied the
  type filter.
- the `rg` availability and regex pattern-validation checks must use the same
  `--no-config` and controlled-environment isolation as the main ripgrep search.
- the regex pattern-validation check applies only when `fixed_strings=false`.
  It must not search the workspace, stdin, or an environment-dependent path.
  Runtime performs it against a runtime-owned empty UTF-8 temporary file created
  for the check, using the same `--regexp` pattern and case-sensitivity flags
  that the main search would use.
- the regex pattern-validation temporary file is not a model-visible candidate
  file, is not counted in `skipped_files`, is not exposed through audit
  arguments or artifacts, is covered by the ToolBroker timeout envelope, and is
  cleaned up best-effort after the check.
- if candidate file count or platform argv limits require chunking, runtime may
  invoke multiple ripgrep processes. Runtime must preserve canonical path and
  line ordering across chunks. It may stream chunks in canonical order and keep
  only the requested page plus one extra result item instead of materializing all
  matches in memory.
- runtime must make result ordering independent of ripgrep discovery order. The
  minimal implementation is to invoke ripgrep over one canonical-path-sorted file
  at a time or over canonical-path-sorted chunks and merge records by canonical
  path and line number before pagination. An implementation may instead use an
  equivalent ripgrep sort flag when it is available and covered by tests.

Minimum implementation strategy:

- enumerate authorized candidate files, apply deny/hidden/symlink/glob/type and
  UTF-8 pre-screening, then process candidates in canonical absolute path order.
- verify `rg` availability and, when in regex mode, regex compilation before
  returning success.
- invoke `rg` for one file at a time or for bounded canonical-path-sorted chunks;
  argv chunk sizing is an internal setting.
- normalize each ripgrep match to the selected output-mode result item before
  pagination: matching line for `content`, distinct path for
  `files_with_matches`, or per-file matching-line count for `count`.
- de-duplicate same-line repeated matches before content pagination and count
  aggregation.
- retain only skipped counters, output-mode aggregation state, the requested page,
  and one extra result item needed to decide `truncated`.
- attach content-mode context rows only after the matching-line page is selected.
- if timeout, cancellation, ripgrep failure, context attachment failure, or
  artifact registration failure occurs before a complete successful page is
  produced, return the normalized failure and do not return partial success.

### Output Modes

`output_mode=content`:

- result item is one matching line.
- multiple regex or fixed-string matches on the same line still produce one
  result item for that line.
- context lines are additional rows in `matches` but do not count as result
  items.

`output_mode=files_with_matches`:

- result item is one distinct matching file path.

`output_mode=count`:

- result item is one `{path, count}` record for a file with count > 0.
- `count` is matching-line count for that file. Multiple matches on the same line
  count once.

### Result

Common fields:

```json
{
  "root": "/abs/search/root",
  "pattern": "needle",
  "output_mode": "content",
  "offset": 0,
  "maxResults": 100,
  "total_returned": 1,
  "truncated": false,
  "next_offset": null,
  "skipped_files": {
    "denied": 0,
    "hidden": 0,
    "decode_error": 0,
    "other": 0
  }
}
```

Only the active output-mode result field is present.

`output_mode=content` uses `matches`:

```json
{
  "path": "/abs/path/file.txt",
  "line_number": 42,
  "line": "matching line preview",
  "is_context": false,
  "line_truncated": false
}
```

`output_mode=files_with_matches` uses:

```json
{
  "paths": ["/abs/path/file.txt"]
}
```

`output_mode=count` uses:

```json
{
  "counts": [
    {"path": "/abs/path/file.txt", "count": 3}
  ]
}
```

## `edit_file`

### ToolDefinition

```json
{
  "name": "edit_file",
  "description": "Replace exact UTF-8 text in an existing file.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "old_text": {"type": "string"},
      "new_text": {"type": "string"},
      "replace_all": {"type": "boolean", "default": false}
    },
    "required": ["path", "old_text", "new_text"],
    "additionalProperties": false
  },
  "category": "native",
  "risk_level": "write",
  "access": ["write"]
}
```

### Behavior

- modifies existing UTF-8 text files only.
- does not create files.
- `old_text` empty returns `tool_error/tool_schema_invalid`.
- matching is case-sensitive.
- matching uses the Phase 1 LF-normalized view and write-back rules: file
  content, `old_text`, and `new_text` are matched in an LF-normalized view;
  write-back preserves the file's dominant existing line ending; if no dominant
  existing style can be determined, runtime writes LF line endings.
- after stale-write guard succeeds, write-back uses same-directory temporary-file
  write followed by atomic target replace. Phase 3.5 does not promise
  crash-consistency or fsync-grade durability.
- default `replace_all=false` requires exactly one match. Zero or multiple
  matches return `tool_error/tool_execution_failed`.
- `replace_all=true` replaces all non-overlapping matches from left to right.
  Zero matches still fails with `tool_error/tool_execution_failed`.
- write must pass stale-write guard.

### Result

```json
{
  "path": "/abs/path/file.txt",
  "replacements": 1,
  "bytes": 12345,
  "sha256_before": "whole-file-sha256-before",
  "sha256_after": "whole-file-sha256-after",
  "guard": {
    "used": true,
    "cache_source": "read_file"
  }
}
```

`bytes` is whole-file byte size after successful write.

## `write_file`

### ToolDefinition

```json
{
  "name": "write_file",
  "description": "Create or completely overwrite a UTF-8 text file.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "content": {"type": "string"}
    },
    "required": ["path", "content"],
    "additionalProperties": false
  },
  "category": "native",
  "risk_level": "write",
  "access": ["write"]
}
```

### Behavior

- writes complete UTF-8 content.
- creates missing parent directories only when the target canonical path and each
  candidate parent directory to be created pass path policy, preserving the
  Phase 1 non-existing path canonicalization rules. Interactive approval scope is
  still the final canonical target file path, not a widened directory grant.
  Normalized audit arguments must include the final canonical target path and the
  canonical candidate parent directories created by the call.
- reusable approval scope includes the exact canonical planned parent directories
  that the call will create. A reusable grant for a simple file create or
  overwrite without parent-directory creation does not authorize a later call
  that would create parent directories, and a grant for one planned parent
  directory set does not authorize a different set.
- creates only the minimal missing parent directory chain needed to create the
  requested target file. It must not create sibling directories or any broader
  directory tree.
- approval and UI presentation for a create-new-file call that will create
  parent directories must list every canonical parent directory that will be
  created before the call proceeds.
- if a later file create/write step fails after parent directories have been
  created, runtime does not guarantee rollback of those directories. The failed
  tool audit must record the directory side effects that occurred, and the call
  must not report file write success.
- if the ToolBroker timeout fires after parent directories have been created but
  before the file create/write succeeds, runtime returns
  `tool_error/tool_execution_timeout`, records the directory side effects in the
  timed-out audit record, does not report file write success, and does not
  advance the file metadata cache.
- `write_file` timeout handling is a cooperative ToolBroker deadline, not a
  promise that the runtime can preempt an in-progress filesystem syscall. Runtime
  must check the deadline before each observable phase it controls, including
  parent-directory creation, exclusive file create, overwrite temporary-file
  write, atomic replace, cache update, and artifact/result handling.
- side-effect audit for failed or timed-out `write_file` calls is outside the
  ToolBroker timeout envelope and must still be emitted after the timeout
  boundary when runtime knows parent directories were created. The audit record
  must distinguish created-directory side effects from successful file write
  completion.
- creates a new file without pre-write cache entry using exclusive create
  semantics.
- if the target did not exist during target classification but appears before the
  exclusive create opens it, the call fails with
  `tool_error/tool_execution_failed`; runtime must not silently convert that race
  into an overwrite.
- overwrites an existing file only after stale-write guard succeeds.
- existing-file overwrites use same-directory temporary-file write followed by
  atomic target replace after stale-write guard succeeds. Phase 3.5 does not
  promise crash-consistency or fsync-grade durability.
- existing empty files still require stale-write guard.
- local write errors return `tool_error/tool_execution_failed`.
- partial writes must not be reported as success.

### Result

```json
{
  "path": "/abs/path/file.txt",
  "bytes": 12345,
  "created": false,
  "overwritten": true,
  "sha256_before": "whole-file-sha256-before",
  "sha256_after": "whole-file-sha256-after",
  "guard": {
    "used": true,
    "cache_source": "read_file"
  }
}
```

Create-new-file result uses:

- `created=true`
- `overwritten=false`
- `sha256_before=null`
- `guard.used=false`
- `guard.cache_source=null`

## `shell_exec`

### ToolDefinition

```json
{
  "name": "shell_exec",
  "description": "Run a structured argv command.",
  "input_schema": {
    "type": "object",
    "properties": {
      "argv": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1
      },
      "cwd": {"type": "string"},
      "timeout_seconds": {
        "type": "integer",
        "minimum": 1,
        "maximum": 3600
      }
    },
    "required": ["argv"],
    "additionalProperties": false
  },
  "category": "shell",
  "risk_level": "execute",
  "access": ["execute"]
}
```

The generated model-visible schema must include
`timeout_seconds.maximum = frozen execution.max_shell_timeout_seconds`. The
`3600` value above is only the built-in default maximum for sessions using the
default frozen config. Implementations and tests must not hard-code `3600` when
the frozen session config contains a different maximum.

### Behavior

- preserves structured argv.
- executes with `shell=False`.
- keeps existing shell policy, raw shell trampoline deny, argv path
  classification, path policy, approval, timeout, artifact, and audit behavior.
- does not accept raw shell string, `command`, `directory`, `description`,
  background, interactive, PTY, or long-running execution.
- omitted `cwd` uses the existing shell execution default working directory:
  the session workspace root. Successful results always report the final
  canonical `cwd` used for execution.
- requested `timeout_seconds` is validated against frozen maximum.
- omitted `timeout_seconds` uses frozen
  `execution.default_shell_timeout_seconds`. `shell_exec` has a tool-specific
  timeout source, so it does not use
  `execution.default_tool_timeout_seconds`.
- nonzero process exit is a tool failure with
  `tool_error/shell_nonzero_exit`.
- Phase 3.5 does not change earlier-phase nonzero failure message, metadata, or
  output handling.

### Successful Result

```json
{
  "argv": ["cmd"],
  "cwd": "/abs/cwd",
  "stdout": "",
  "stderr": "",
  "returncode": 0,
  "signal": null,
  "duration_ms": 100
}
```

`signal` is integer or `null`. Normal exit and Windows process results use
`null`.
`duration_ms` is a non-negative integer elapsed duration rounded to the nearest
millisecond or rounded down consistently by implementation. It is an audit and
presentation fact, not a recovery timing source.

## Unchanged Existing Runtime-Control Tools

The tools in this section are model-visible Phase 3.5 tools, but they are not
native-tool enhancement targets.

Phase 3.5 does not update or tighten their model-visible schemas, target
validation, behavior semantics, approval exceptions, runtime truth,
persistence, or checkpoint facts. Their authoritative behavior remains the Phase
1 skill/runtime-control contracts, Phase 2 Todo Plan contracts, Phase 3
normalized error contracts, and the shared Phase 3.5 ToolResult artifact
reference contract.

Phase 3 normalized ToolResult status and error projection supersede earlier
Phase 1/2 example status/error wording for malformed tool input and local
semantic validation. This is not a Phase 3.5 behavior expansion for these tools;
it is the Phase 3 normalized ToolBroker boundary applied consistently.

Their `ToolResult` envelope, `ToolResult.status`, normalized model-visible error
projection, and model-visible artifact references still follow the Phase 3/3.5
ToolBroker boundary contract defined above. Schema validation failures, local
semantic validation failures, invalid frozen target/config failures, and
persistence failures use
`status="error"` with the appropriate normalized error projection. Path policy,
shell policy, or interactive approval denials use `status="denied"` when such a
denial path applies. The Phase 2 `todo` audit-only approval exception remains
unchanged and does not create interactive approval prompts or approval grants.

Shared ToolBroker schema-validator improvements apply only as enforcement of
the schemas already defined for these tools. They are not a Phase 3.5 expansion
of `activate_skill`, `load_skill_resource`, or `todo`.

### `activate_skill`

```json
{
  "name": "activate_skill",
  "description": "Activate a frozen prompt skill for this run.",
  "input_schema": {
    "type": "object",
    "properties": {
      "name": {"type": "string"}
    },
    "required": ["name"],
    "additionalProperties": false
  },
  "category": "runtime_control",
  "risk_level": "runtime_control",
  "access": ["runtime_control"]
}
```

### `load_skill_resource`

```json
{
  "name": "load_skill_resource",
  "description": "Load one frozen resource file for an active skill.",
  "input_schema": {
    "type": "object",
    "properties": {
      "skill_name": {"type": "string"},
      "path": {"type": "string"}
    },
    "required": ["skill_name", "path"],
    "additionalProperties": false
  },
  "category": "runtime_control",
  "risk_level": "read",
  "access": ["read"]
}
```

### `todo`

```json
{
  "name": "todo",
  "description": "Replace the current run Todo Plan.",
  "input_schema": {
    "type": "object",
    "properties": {
      "items": {
        "type": "array",
        "minItems": 0,
        "maxItems": 20,
        "items": {
          "type": "object",
          "properties": {
            "content": {"type": "string"},
            "status": {
              "type": "string",
              "enum": ["pending", "in_progress", "completed"]
            },
            "activeForm": {
              "type": "string",
              "description": "Optional present-continuous label."
            }
          },
          "required": ["content", "status"],
          "additionalProperties": false
        }
      }
    },
    "required": ["items"],
    "additionalProperties": false
  },
  "category": "runtime_control",
  "risk_level": "runtime_control",
  "access": []
}
```

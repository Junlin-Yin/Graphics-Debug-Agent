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

Unknown fields fail with `tool_error/tool_schema_invalid`.

### Path Handling

Model-visible schemas keep the current `path` style. They do not add
`absolute_path`, `file_path`, `directory`, or similar aliases.

Runtime accepts relative or absolute path input where a tool defines `path`.
Relative paths resolve against `workspace_root`. Runtime canonicalizes paths
before path policy, approval, handler execution, and audit.

For tools other than `view_image` ordinary model-visible output, Phase 3.5
structured results expose canonical absolute paths. `view_image` ordinary output
keeps the Phase 2 display-path behavior. Its approval, policy, and audit paths
still use canonical paths internally.

### Hidden And Deny

`include_hidden=true` affects only lexical dot-prefix filtering for ordinary
dot files and dot directories. It never overrides builtin or user path deny.

Hidden path means any path segment starts with `.`. Windows hidden attributes do
not participate in Phase 3.5 hidden detection.

Denied children must not be exposed by name, path, error message, or count,
except `search_text.skipped_files.denied`, which intentionally exposes only an
aggregate denied-file count.

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
- ToolBroker large-output artifact handling remains independent from
  pagination. Pagination metadata describes result-set slicing; artifact
  metadata describes a single tool output externalized for size.

### Error Mapping

Use Phase 3 normalized error taxonomy.

- schema shape errors, unknown fields, missing required fields, invalid enum,
  min/max violations, and execution-before local semantic validation failures:
  `tool_error/tool_schema_invalid`.
- ordinary handler failures after execution begins:
  `tool_error/tool_execution_failed`.
- brokered timeout: `tool_error/tool_execution_timeout`.
- `shell_exec` nonzero process exit: `tool_error/shell_nonzero_exit`.

Specific details go in message and allowlisted metadata. Phase 3.5 must not add
tool-specific reason symbols.

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
| `list_dir` | `tool`, `access=read`, canonical path, `include_hidden`. |
| `find_file` | `tool`, `access=read`, canonical root, `pattern`, `case_sensitive`, `include_hidden`. |
| `search_text` | `tool`, `access=read`, canonical root, `pattern`, `glob`, `case_sensitive`, `fixed_strings`, `type`, `output_mode`, context settings, `include_hidden`. Excludes only `offset` and `maxResults`. |
| `edit_file` | `tool`, `access=write`, canonical path, `replace_all`. Excludes `old_text` and `new_text`. |
| `write_file` | `tool`, `access=write`, canonical path. Excludes `content`. |
| `shell_exec` | `tool`, `access=execute`, normalized argv, canonical cwd, effective timeout, classified argv paths. |

Audit arguments:

- include all normalized behavior-affecting arguments unless a tool has explicit
  redaction rules.
- include pagination parameters.
- include `search_text.type`, context parameters, `output_mode`,
  `fixed_strings`, `case_sensitive`, `include_hidden`, and `glob`.
- include `write_file` and `edit_file` content hashes and byte lengths where
  available, not full replacement content beyond the normal model-visible tool
  transcript.
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
  cross `/`.
- `**`: only when it is a complete path segment. It matches zero or more
  directory levels.

Not supported:

- brace expansion such as `{a,b}`.
- extglob such as `!(...)`, `?(...)`, `+(...)`, `*(...)`, `@(...)`.
- node minimatch behavior outside the supported subset.

Unsupported glob syntax returns `tool_error/tool_schema_invalid`.

Runtime must perform controlled traversal itself and then apply the matcher to
relative `/`-separated paths. It must not delegate traversal to Python
`glob.glob()` or `Path.glob()` as the primary implementation.

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
        "items": {"type": "string"}
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
- `ignore` supports literal name, `*`, and `?` over immediate child names.
- `ignore` does not support brace expansion, extglob, recursion, or cross
  directory matching.
- for directory entries, `foo`, `foo/`, and `foo/**` all hide immediate child
  directory `foo`.
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
- `pattern` is matched against `/`-separated paths relative to the search root.
- only the Phase 3.5 portable glob subset is supported.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- returns files only; directories are never result items.
- default skips hidden paths.
- `case_sensitive=false` performs matcher-level case folding and does not depend
  on filesystem case behavior.
- does not recursively follow symlink directories.
- symlink files may be returned only if their resolved target canonicalizes and
  passes path policy. Symlink targets escaping allowed scope or hitting deny are
  skipped.
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
- runtime enumerates candidate files, applies path policy, hidden filtering,
  symlink rules, and optional `glob`, then invokes ripgrep with
  `--json --files-from`.
- `glob`, when present, is relative to the search root and uses the Phase 3.5
  portable glob subset.
- unsupported glob syntax returns `tool_error/tool_schema_invalid`.
- `fixed_strings=false` means `pattern` is ripgrep/Rust regex.
- `fixed_strings=true` means `pattern` is literal.
- regex compile failures return `tool_error/tool_execution_failed`.
- `case_sensitive` controls ripgrep case behavior.
- `type` is passed to ripgrep as `--type <type>` and is part of reusable
  approval scope. Unknown type returns `tool_error/tool_execution_failed`.
- `rg` missing returns `tool_error/tool_execution_failed`.
- `rg` exit code `1` for no matches is success with empty results.
- no Python regex fallback exists.
- `context` is mutually exclusive with `before_context` and `after_context`.
  Providing both returns `tool_error/tool_schema_invalid`.
- `context=N` means `before_context=N` and `after_context=N`.
- context lines do not count against `maxResults` or `total_returned`.
- same-file context lines are de-duplicated by line number. Match lines win over
  context lines and use `is_context=false`.
- content results are sorted by canonical path ascending, then 1-based
  `line_number` ascending, then match before context for the same line.
- `files_with_matches` and `count` results are sorted by canonical path
  ascending.
- line preview maximum is 4000 UTF-8 codepoints. Longer lines are truncated and
  set `line_truncated=true`.
- skipped file counters are aggregate only. `denied` count intentionally reveals
  only the number of denied candidate files, not their names or paths.

### Output Modes

`output_mode=content`:

- result item is one match.
- context lines are additional rows in `matches` but do not count as result
  items.

`output_mode=files_with_matches`:

- result item is one distinct matching file path.

`output_mode=count`:

- result item is one `{path, count}` record for a file with count > 0.

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
- matching uses LF-normalized view and write-back preserves dominant existing
  line ending.
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
- creates missing parent directories when the target canonical path passes
  write policy and approval.
- creates a new file without pre-write cache entry.
- overwrites an existing file only after stale-write guard succeeds.
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
`3600` value above is the built-in default maximum shown for the static contract
shape.

### Behavior

- preserves structured argv.
- executes with `shell=False`.
- keeps existing shell policy, raw shell trampoline deny, argv path
  classification, path policy, approval, timeout, artifact, and audit behavior.
- does not accept raw shell string, `command`, `directory`, `description`,
  background, interactive, PTY, or long-running execution.
- requested `timeout_seconds` is validated against frozen maximum.
- omitted `timeout_seconds` uses frozen default.
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
  "duration_seconds": 0.1
}
```

`signal` is integer or `null`. Normal exit and Windows process results use
`null`.

## Unchanged Existing Runtime-Control Tools

The tools in this section are model-visible Phase 3.5 tools, but they are not
native-tool enhancement targets.

Phase 3.5 does not update or tighten their model-visible schemas, target
validation, behavior semantics, approval exceptions, runtime truth, persistence,
checkpoint facts, or result contracts. Their authoritative behavior remains the
Phase 1 skill/runtime-control contracts, Phase 2 Todo Plan contracts, and Phase
3 normalized error contracts.

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

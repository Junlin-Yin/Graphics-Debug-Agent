# Phase 1 Tooling And Safety Specification

## Boundary

All model-visible tools are exposed through `ToolBroker`.

Phase 1 adds controlled writable native tools, `shell_exec`, `activate_skill`,
`load_skill_ref_file`, and local slash command visibility for available tools.
It does not add MCP, subagent tools, workflow step tools, or unrestricted shell
execution.

Path policy applies only to model-visible tool invocations mediated by
`ToolBroker`. Runtime-owned persistence and artifact store operations are not
tool invocations and are governed by persistence, artifact, checkpoint, and audit
contracts.

## Phase 1 Model-Visible Tool Set

Phase 1 exposes exactly these tools to the model:

- `read_file`
- `list_dir`
- `search_text`
- `write_file`
- `edit_file`
- `shell_exec`
- `activate_skill`
- `load_skill_ref_file`

The Phase 0 model-visible `git_status` native tool is removed.

Minimum native tool intent:

- `read_file`: read a UTF-8 text file under authorized read paths, optionally
  limited to the first `limit` lines.
- `list_dir`: list immediate directory entries under authorized read paths.
- `search_text`: search text under authorized read paths, skipping directories
  denied by builtin or user path policy.
- `write_file`: write complete UTF-8 file content under authorized write paths.
- `edit_file`: perform a structured exact-match text replacement under
  authorized write paths.
- `shell_exec`: run structured argv with `shell=False` after shell policy, path
  policy, approval, timeout, and audit checks pass.
- `activate_skill`: activate a frozen prompt skill for the current run.
- `load_skill_ref_file`: load one frozen reference file for an active skill as a
  controlled tool observation.

`write_file` may create missing parent directories when the target path remains
inside authorized write scope. `edit_file` does not create files; it reads an
existing file and replaces the first exact occurrence of `old_text`.

Here, authorized read/write paths mean paths that pass the final `ToolBroker`
decision. Path policy itself remains only `trust`/`deny`; read/write/execute
tool type is handled by tool metadata and approval mode.

Model-visible tools must not use `.sessions/` paths. Runtime-owned stores may
write `.sessions/` through runtime service APIs, but model-visible tools cannot
read, list, search, write, edit, shell into, or use artifact ids to bypass the
builtin `.sessions/` deny rule.

Model-visible tools must not use `~/.debug-agent/skills/` or
`<workspace_root>/.debug-agent/skills/` paths. Runtime may snapshot skills from
those directories during startup, but the model-visible surface for skill
content is the frozen skill snapshot and runtime-control skill tools, not live
source-file access.

Minimum Phase 1 schemas:

```json
{
  "name": "read_file",
  "description": "Read file contents.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "limit": {"type": "integer"}
    },
    "required": ["path"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "list_dir",
  "description": "List immediate directory entries.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "limit": {"type": "integer"}
    },
    "required": ["path"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "search_text",
  "description": "Search text under a path.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "query": {"type": "string"},
      "limit": {"type": "integer"}
    },
    "required": ["path", "query"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "write_file",
  "description": "Write content to file.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "content": {"type": "string"}
    },
    "required": ["path", "content"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "edit_file",
  "description": "Replace exact text in file.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "old_text": {"type": "string"},
      "new_text": {"type": "string"}
    },
    "required": ["path", "old_text", "new_text"],
    "additionalProperties": false
  }
}
```

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
      "timeout_seconds": {"type": "integer"}
    },
    "required": ["argv"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "activate_skill",
  "description": "Activate a frozen prompt skill for the current run.",
  "input_schema": {
    "type": "object",
    "properties": {
      "name": {"type": "string"}
    },
    "required": ["name"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "load_skill_ref_file",
  "description": "Load one frozen reference file for an active skill.",
  "input_schema": {
    "type": "object",
    "properties": {
      "skill_name": {"type": "string"},
      "path": {"type": "string"}
    },
    "required": ["skill_name", "path"],
    "additionalProperties": false
  }
}
```

All Phase 1 model-visible tool schemas reject unknown input fields.

`limit` values must be positive integers when provided. `read_file.limit` means
line count, `list_dir.limit` means entry count, and `search_text.limit` means
match count. When omitted, runtime uses tool-specific defaults. Runtime may cap
requested limits at tool-specific maximums from the frozen runtime config or
built-in defaults.

`search_text.query` is a literal UTF-8 substring, not a regular expression.
Matching is case-sensitive and line-oriented. Runtime returns matching lines plus
file path and line number metadata until `search_text.limit` matches are
reached. Files that cannot be decoded as UTF-8 are skipped. Phase 1 does not add
regex search semantics to `search_text`.

For `shell_exec`, `timeout_seconds` must be a positive integer when provided.
The effective timeout is `min(requested_timeout_seconds,
frozen `default_shell_timeout_seconds`)`. If `timeout_seconds` is omitted, the
effective timeout is the frozen `default_shell_timeout_seconds`. If the frozen
config does not declare a shell timeout, the built-in Phase 1 default is `300`
seconds. The effective timeout participates in approval grant scope signatures.

`edit_file` replaces only the first exact occurrence. If `old_text` is absent,
the tool returns `ToolResult(status="error")` with `error_class="tool_error"`.

Matching rules for `edit_file`:

- Matching is case-sensitive.
- Matching is on UTF-8 codepoints after normalizing line endings to `\n`.
- Multi-line `old_text` is supported.
- Only the first exact occurrence is replaced.
- The tool returns `error` when `old_text` is not found or when the
  replacement would produce invalid UTF-8.
- Write-back preserves the file's dominant existing line-ending style. The
  normalized `\n` view is only for matching and replacement. If no dominant
  existing style can be determined, runtime writes LF line endings.

## Tool Listing

Phase 1 adds local slash command:

```text
/tools
```

`/tools` is handled locally by the REPL and is never sent to the model.

`/tools` first lists all runtime-visible tools, then lists path policy and
shell policy details last.

Each tool entry includes only:

- tool name.
- normalized approval policy.
- tool description.

Normalized approval policy values:

- `allow`: rendered for approval behavior `auto-allow`, `audit-only`, and
  `audit-only when target is valid`.
- `ask-all`: rendered for approval behavior `ask`.
- `ask-distrust`: rendered for approval behavior
  `auto-allow in trusted paths; ask outside trusted paths`.

`/tools` renders path policy and shell policy after all tools:

```text
Tools:

- <tool-name> [<allow|ask-distrust|ask-all>]
<tool description>

Path policy:
- trust = <trusted paths>
- deny  = <denied paths>

Shell policy:
- allow = <allowed commands>
- deny  = <denied commands>
```

`/tools` must reflect the current frozen session config, active approval mode,
path policy, and shell policy.

## Tool Definition Metadata

Runtime-owned tool definitions include:

```python
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    category: str
    risk_level: str
    access: list[str]
```

Minimum categories:

- `native`
- `shell`
- `runtime_control`

Minimum risk levels:

- `read`
- `write`
- `execute`
- `runtime_control`

Phase 1 does not define per-tool approval metadata. Approval behavior is derived
from approval mode plus runtime-owned risk level, category, access, path policy,
and shell policy.

## Tool Control Plane

`ToolBroker` is the Phase 1 tool control plane. It is not only a handler lookup
table. It owns the ordered execution envelope for every model-visible tool call:

1. schema validation.
2. runtime target resolution and tool-call fact normalization.
3. permission evaluation.
4. approval request dispatch when needed.
5. handler routing.
6. artifact handling.
7. `ToolResult` normalization.
8. audit event writing.

Tool handlers must not:

- bypass `ToolBroker`.
- read mutable global policy directly.
- ask users for approval directly.
- write tool audit events directly.
- widen the model-visible tool schema.

### ToolUseContext

Each brokered tool call receives a frozen `ToolUseContext` assembled by
`ToolBroker`:

```python
class ToolUseContext:
    session_id: str
    run_id: str
    workspace_root: str
    artifact_root: str
    approval_mode: str
    frozen_config: dict
    tool_definition: ToolDefinition
    frozen_policy: dict
    approval_grants: object
    approval_provider: object
    event_writer: object
    artifact_store: object
    skill_snapshot_store: object
```

`ToolUseContext` is an execution context, not persisted runtime truth. It is
derived from frozen session config, frozen main-agent policy declarations,
runtime builtin policy, and current session approval records.

The normative permission decision pipeline is defined in
`docs/phase-1/specs/approval.md`. Tooling code must consume frozen, validated
policy facts instead of reinterpreting raw path or shell TOML structures at
execution time. Implementations may introduce internal helper types for policy
facts, but Phase 1 does not require a generic permission-rule engine.

### ToolRouter

`ToolRouter` dispatches an allowed tool call to one Phase 1 handler category:

- `native`
- `shell`
- `runtime_control`

Routing happens only after permission evaluation and approval are complete.
Future MCP, subagent, or workflow tool categories must integrate through the
same broker envelope, but they are not Phase 1 categories.

`activate_skill` uses category `runtime_control` and risk level
`runtime_control`. `load_skill_ref_file` uses category `runtime_control` and
risk level `read`; it can only read frozen reference snapshots for already active
skills. When the target skill is active, the reference path resolves inside the
frozen reference snapshot, and the frozen reference hash validates,
`load_skill_ref_file` is audit-only in every approval mode and does not request
interactive approval. Invalid, inactive, missing, corrupt, or hash-mismatched
targets are denied before approval and cannot be overridden.

Trusted workspace is the session `workspace_root` plus path policy `trust`
paths. `trust` adds trusted roots and does not narrow or replace the default
trust for `workspace_root`. Trusted workspace controls automatic allow behavior
under the active approval mode; it is not an exclusive path allowlist. Path
policy does not classify read, write, or execute tool types. Path policy `deny`
entries are a blacklist veto and cannot be overridden by approval mode,
including `yolo`. Phase 1 builtin path deny rules are hard denies; future
relaxation is a later breaking contract change.

## Model-Visible Git

Phase 1 removes `git_status` from the model-visible native tool set.

Rationale:

- users may need to ban all git access for a workflow.
- a native `git_status` tool would bypass a simple `deny = [["git"]]` shell
  policy.
- keeping all model-initiated git access behind shell policy is easier to
  audit.

Runtime-owned CLI commands are separate from model-visible tools. This change
does not affect `debug-agent status` or `debug-agent trace`.

## Shell Policy

Shell policy is defined in `~/.debug-agent/agent.toml`.

See `docs/phase-1/specs/approval.md` for the normative shell policy rules.

`shell_policy` is independent from `path_policy`. A shell command must satisfy
both before it can execute.

Builtin shell deny rules are always active before user shell policy. They cannot
be overridden by user `allow` rules or approval. See
`docs/phase-1/specs/approval.md` for the normative builtin deny list.

An empty user shell `allow` list means default allow after builtin shell deny,
user shell deny, path policy, approval mode, timeout, artifact handling, and
audit pass. Phase 1 accepts this as a skill-guided local automation tradeoff; it
is not a filesystem or process sandbox.

## Cross-Platform Shell Wrapper

`shell_exec` accepts structured argv:

```json
{
  "argv": ["uv", "run", "pytest", "tests/unit", "-v"],
  "cwd": "."
}
```

Rules:

- `argv[0]` is required.
- raw shell strings are not accepted by the Phase 1 model-visible shell tool.
- execution uses `shell=False`.
- command lookup is platform-aware.
- policy matching uses normalized executable identities and normalized argv
  tokens.
- path-qualified `argv[0]` executable paths are checked by path policy before
  execution as well as normalized for shell-policy matching.
- default `cwd` is `workspace_root`; a provided `cwd` is resolved against
  `workspace_root` and checked by path policy.
- stdout and stderr are captured separately, then normalized into `ToolResult`.
- a non-zero process exit code is a tool failure, not a successful tool call;
  the failure message must prefer concrete stderr/stdout text from the tool and
  may append the exit code for clarity.
- large stdout/stderr are stored as artifacts.
- timeout returns `ToolResult(status="timeout")`.

## Path Policy Interaction

Shell policy decides whether the command type is allowed, such as `git`, `npm`,
or `uv`. Path policy decides whether a requested path is blacklisted, trusted,
or untrusted; it does not classify read, write, or execute tool types. These are
independent checks and both must pass where applicable.

The shell tool schema exposes `cwd` as the generic path-like field. Generic
`shell_exec` also evaluates path-qualified `argv[0]` and argv tokens that can be
classified as path-like by the runtime-owned argv classification rules.
Specialized wrappers may expose additional explicit file path arguments.

`shell_exec` is execute access for approval-mode and shell-policy purposes.
Separately, generic `shell_exec` evaluates `cwd` through path policy: blacklist
veto first, then trusted/untrusted classification. It evaluates classified argv
paths the same way. Path classification is aggregated conservatively: if `cwd`,
a path-qualified executable path, or any runtime-classified argv path is
untrusted, the whole shell call is untrusted for the approval-mode matrix. It
must not claim to fully understand every command's file-system side effects from
arbitrary argv. If a command needs strict read/write path enforcement, it should
be modeled as a native tool or specialized wrapper with explicit path fields.

Shell policy command matching normalizes executable names before applying
argv-prefix rules. Path-qualified executables and common Windows executable
suffixes are normalized to their command identity. Runtime-defined transparent
wrappers, such as `env FOO=1 git status`, are unwrapped when the nested command
is structurally visible. Opaque wrappers such as `npm run`, `make`, `uv run`,
language-interpreter script execution, and arbitrary local scripts are not
semantically inspected; users must deny those wrapper commands directly or use a
narrow allowlist when they need that restriction.

Because argv side effects cannot be fully inferred, shell tools remain high
risk. Whether approval is requested depends on approval mode and trusted
workspace rules, but `shell_exec` always requires shell policy and path policy.

Known issue: argv path classification cannot prove that shell commands avoid
paths that are not present in argv or are hidden behind command-specific
semantics. Phase 1 accepts this limitation. A future phase may need filesystem
sandboxing or specialized command wrappers for complete shell path isolation.

The normative builtin shell deny and argv path option lists are defined in
`docs/phase-1/specs/approval.md`. Phase 1 implementations must not add
ad-hoc regex matching or extra implicit path options outside that documented
runtime-owned list.

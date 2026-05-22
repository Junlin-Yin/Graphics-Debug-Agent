# Phase 1 Skill Specification

## Boundary

Phase 1 supports prompt skills only.

Subagent skills, workflow skills, MCP-provided skills, and plugin-packaged
skills are outside Phase 1 execution scope.

Phase 1 intentionally does not implement section-level progressive disclosure,
semantic reference retrieval, automatic active-skill disclosure degradation, or
`deactivate_skill`.

## Discovery Paths

Phase 1 discovers prompt skills from exactly two configured roots:

- `~/.debug-agent/skills`
- `<workspace_root>/.debug-agent/skills`

Precedence:

1. project paths
2. global paths

CLI explicit skill paths and builtin skill roots are not part of Phase 1.

Same-name skill override is whole-skill override. Phase 1 does not merge skill
directories or files.

Duplicate names in the same discovery scope are invalid and must be reported as
`config_error`. Same-name skills across scopes use the precedence order above:
the project skill replaces the global skill as a whole skill.

Each skill can contain:

- a required `SKILL.md` with YAML front matter followed by Markdown body.
- optional reference files under `references/**`.

Phase 1 snapshots only `SKILL.md` and files under `references/**`. Files outside
`references/**` are ignored even if they are present in the skill directory.

The skill directory name is not the runtime skill id. The runtime skill id comes
from `SKILL.md` front matter `name`.

## Snapshot Strategy

Phase 1 uses registration-time content snapshot plus frozen-snapshot hash
verification.

At session startup, after the Phase 1 database, session, prompt run, and session
artifact root exist, `SkillRegistry`:

1. reads `SKILL.md` front matter.
2. reads the full `SKILL.md` Markdown body.
3. reads every file under `references/**` as a file-level reference snapshot.
4. computes stable SHA-256 content hashes for `SKILL.md`, each reference file,
   and the overall skill snapshot.
5. stores manifest metadata, source path, source scope, `SKILL.md` content, file
   reference metadata, and the session-local frozen snapshot.
6. stores large reference payloads as artifacts according to normal artifact
   rules.

The full `SKILL.md` body is not injected into model context at startup. It is
injected only after the skill is activated.

Skill discovery, snapshotting, and persistence are startup-blocking. One-shot
execution and REPL input must not accept a user prompt until the frozen skill
registry snapshot has been persisted and available skill headers can be composed
from it.

Skill registry snapshots are persisted separately from
`sessions.config_snapshot_json` and associated with the session and prompt run.
The session config snapshot records configuration and policy facts; the skill
registry snapshot records skill manifests, hashes, `SKILL.md` content, and
reference file snapshots.

Minimum persisted snapshot facts:

- session id.
- skill name.
- execution mode and support status.
- source scope and source path.
- normalized manifest metadata.
- `SKILL.md` content and content hash.
- reference file path, media kind, size, content hash, inline text payload when
  small enough, and payload artifact id when artifact-backed.
- overall content hash.
- payload artifact id when the serialized snapshot is too large for inline
  SQLite storage.
- version and creation timestamp.

The prompt composer uses the frozen registry snapshot to build the available
skill header shown to the model in the stable system block for ordinary task
model calls. The header lists prompt skills as activation candidates for
`activate_skill`. Unsupported/deferred skills are listed separately with
non-activatable reasons. The header must not include full skill bodies or
reference file contents.

Available skill headers are not durable conversation messages and are not part
of `/compress` input. Compression does not rewrite them. Ordinary task
`ModelContextFrame` estimates still count them because they are sent to the
provider as part of the stable system block.

The overall content hash is based on normalized manifest facts, normalized
`SKILL.md` text, reference file paths, reference content hashes, and reference
metadata. Session-local artifact ids are not hash inputs.

Hash normalization must be deterministic across platforms:

- UTF-8 text where text decoding applies.
- `\n` line endings for text hash inputs.
- stable path ordering.
- `/` path separators inside hash inputs.
- canonical JSON serialization for structured manifest and metadata.
- SHA-256 output formatted as `sha256:<hex>`.

At activation time, runtime validates the requested skill against the frozen
session snapshot and verifies that the frozen content still matches the stored
content hash. It does not re-read or re-hash the source file. The frozen session
snapshot is the execution truth. File changes after session startup do not
change active session behavior, including first activation behavior.

If the frozen snapshot is missing, corrupt, or does not match its stored hash,
activation returns `ToolResult(status="denied")` with
`error_class="config_error"` and an audit event is written.

After a skill is active, later model calls reconstruct skill context from the
frozen session snapshot and structured active skill refs. They do not re-read or
re-hash source files.

## Manifest

`SKILL.md` must start with YAML front matter, followed by the prompt skill body
as Markdown.

Required fields:

- `name`
- `description`

Optional fields:

- `execution_mode`
- `triggers`
- `metadata`

Phase 1 accepts only:

```yaml
execution_mode: prompt
```

If `execution_mode` is absent, Phase 1 treats the skill as `prompt`.

If `execution_mode: workflow`, Phase 1 records it as unsupported/deferred and
does not expose it to `activate_skill`.

Unknown top-level manifest fields are invalid in Phase 1. `name`,
`description`, and `execution_mode` must be strings when present. `triggers`
must be a list of strings when present. `metadata` must be a JSON-like mapping
when present. Skill names must match `[A-Za-z0-9_.-]+` and be at most 128
characters.

## Model-Visible Skill Tools

Phase 1 exposes two skill-related runtime tools:

- `activate_skill`
- `load_skill_ref_file`

Both are runtime tools. They must be exposed to the model only through the normal
runtime tool-definition path and invoked only through `ToolBroker`.

### `activate_skill`

Input schema:

```json
{
  "type": "object",
  "properties": {
    "name": {"type": "string"}
  },
  "required": ["name"],
  "additionalProperties": false
}
```

Execution rules:

- `ToolBroker` resolves the requested skill target from the frozen session
  snapshot before interactive approval is requested.
- unknown skill returns `ToolResult(status="denied")` without prompting for
  approval.
- unsupported workflow skill returns `ToolResult(status="denied")` without
  prompting for approval.
- missing, corrupt, or hash-mismatched frozen snapshots return
  `ToolResult(status="denied")` without prompting for approval.
- repeated activation is idempotent.
- successful activation updates run-scoped `active_skills`.
- active skill `SKILL.md` content becomes visible starting with the next model
  call.
- activation writes audit events.

`activate_skill` must not bypass path policy, approval mode, `ToolBroker`, or
audit just because it does not edit files.

`activate_skill` returns a short activation result such as
`Skill activated: <name> (<hash>)`. It must not return the full skill body as an
ordinary tool output.

### `load_skill_ref_file`

`load_skill_ref_file` loads one file-level reference snapshot for an already
active skill. It is the Phase 1 mechanism that lets prompt skills use
`references/**` without automatically injecting every reference into every model
call.

Input schema:

```json
{
  "type": "object",
  "properties": {
    "skill_name": {"type": "string"},
    "path": {"type": "string"}
  },
  "required": ["skill_name", "path"],
  "additionalProperties": false
}
```

Execution rules:

- the target skill must be active in the current run.
- `path` is a skill-local relative path and must resolve to a frozen file under
  that skill's `references/**`.
- path traversal, absolute paths, and paths outside the frozen reference set are
  denied before approval is requested.
- runtime resolves the file only from the frozen session skill snapshot; it does
  not read the source file.
- missing, corrupt, or hash-mismatched frozen reference snapshots return
  `ToolResult(status="denied")` with `error_class="config_error"`.
- successful loads write audit events.
- repeated loads are allowed and produce ordinary tool observations.

Text reference files that fit the inline tool-output threshold return their full
text content plus metadata: skill name, reference path, content hash, size, and
media kind.

Large text reference files and non-text reference files return a controlled
artifact/reference marker plus metadata. They must not inject raw large content
or binary content into the model-visible context.

Loaded reference output is ordinary durable LLM-visible working history. It may
be omitted or compressed later by `ContextManager`. The frozen skill snapshot
and artifacts remain the audit truth.

`load_skill_ref_file` is not a general file-read tool and does not grant access
to `.sessions/` paths or source files. It can only expose controlled content
from the current session's frozen skill registry snapshot.

## Run Active Skill State

Phase 0 documented `Run.active_skills` as `list[str]`. Phase 1 changes this
field to structured runtime state. Phase 1 does not need to keep forward
compatibility with Phase 0 sessions.

Minimum Phase 1 shape:

```json
{
  "name": "systematic-debugging",
  "content_hash": "sha256:...",
  "activation_reason": "model_requested",
  "scope": "run"
}
```

Persisted database schema, checkpoints, context snapshots, traces, and
`/skills` output must use the structured shape where active skill references are
recorded.

Loaded reference files are not active skill state. They are tool observations in
durable conversation and may be omitted or compressed.

## Model Context Design

Phase 1 uses a two-layer context model:

- durable LLM-visible working history, such as `ReplRuntime.conversation`.
- per-call `ModelContextFrame`, generated by `PromptComposer`.

System prompts and active skill `SKILL.md` content are not stored in
`ReplRuntime.conversation`. They participate in model calls through
`AgentRunRequest.system_prompt`, `PromptComposer`, and the generated
`ModelContextFrame`.

Ordinary task model calls include the stable system block, available skill
headers, active skill context, retained conversation, current user input or
tool-loop messages, and tool schemas. Runtime-owned compression calls use a
separate compression frame and do not include available skill headers, the main
agent system prompt, model-visible tool schemas, or active `SKILL.md` bodies.

Loaded reference file outputs are stored as ordinary durable conversation tool
observations. They are not re-injected automatically on every later model call.

Token estimates use `ModelContextFrame`, not raw conversation history.

## Injection Design Decision

Phase 1 considered three ways to put active skill and reference content into
model context.

### Option A: Inject Active `SKILL.md` Content During Prompt Composition

The prompt composer places active skill `SKILL.md` content in the near context
zone before the current user input or tool-loop messages for every model call.
Reference files are not injected automatically; they are available through
`load_skill_ref_file`.

Benefits:

- `/compress` cannot accidentally summarize or delete core skill instructions.
- active skill identity remains structured runtime state.
- core skill behavior is reproducible from the frozen snapshot.
- reference files are useful without becoming permanent context growth.

Costs:

- active `SKILL.md` bodies still consume context every model call.
- very large `SKILL.md` files may cause context-limit failure.

Decision:

Phase 1 uses this option.

### Option B: Add Skill Content As Conversation Messages

`activate_skill` returns the skill content as a tool result or assistant-visible
message, and that message remains in conversation history.

Benefits:

- implementation may be simpler in adapter loops.

Costs:

- `/compress` must special-case skill messages to avoid summarizing them away.
- compression could accidentally change normative instructions.
- skill content would be mixed with observations, making recovery and audit less
  structured.

Decision:

Phase 1 does not use this option.

### Option C: Section-Level Progressive Disclosure

The runtime parses Markdown sections and injects selected section subtrees under
budget pressure.

Benefits:

- finer-grained context control for very large skills.

Costs:

- more runtime state, tests, recovery rules, and failure modes.
- not required by the Phase 1 target workflows.

Decision:

Phase 1 does not implement this option.

## Active Skill Context Message

For each model call, `PromptComposer` may add a runtime-authored context message:

```text
[Runtime supplied active skill context]
This block is authoritative for this turn.
```

Each active skill entry should include:

- `skill_id`.
- `version` or `content_hash`.
- `activation_reason`.
- `scope`, such as run-scoped active skill.
- `instructions` from the frozen `SKILL.md` body.
- available reference file paths and hashes.

Reference file lists are guidance for the model. The model must call
`load_skill_ref_file` to load a reference file's frozen content. Listing a
reference file path does not authorize general filesystem access.

Skill context may include model-visible guidance such as `allowed_tools` or
`path_policy`, but those fields are not authorization. Actual authorization is
decided only by runtime and `ToolBroker` using frozen config, path policy, shell
policy, approval mode, and approval grants.

## Active Skill Lifetime

Phase 1 does not implement `deactivate_skill` and does not automatically
deactivate skills.

Reasoning:

- prompt skill activation is run-scoped and bounded by the prompt run lifetime.
- adding deactivate creates more state transitions and more model-control
  surface before subagents/workflows exist.
- Phase 1 can rely on `/compress`, old tool-result omission, and explicit
  context-limit failure for context pressure.
- automatic deactivation would silently remove behavior constraints and make
  failures harder to explain.

Under budget pressure, Phase 1 does not reduce active skill content. If
omission and compression cannot make the next `ModelContextFrame` fit under
`window_tokens`, runtime records `context_limit_exceeded` and aborts the current
turn according to the context-compression contract.

A future phase may add `deactivate_skill` or progressive disclosure if real
sessions show that active skill sets need user-directed pruning or finer context
control.

## `/compress` Interaction

Active `SKILL.md` content is not in `/compress` input.

Compression stores only active skill references:

- skill name.
- content hash.
- activation reason and scope.

After compression, the prompt composer reconstructs active `SKILL.md` context
from the frozen skill snapshot and active skill references.

Loaded reference file outputs are ordinary conversation history. They may be
omitted or compressed. Compression must not mutate the frozen reference
snapshots or artifacts.

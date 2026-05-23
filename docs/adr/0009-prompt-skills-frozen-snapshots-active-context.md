# ADR 0009: Prompt Skills As Frozen Snapshots With Runtime-Supplied Active Context

## Status

Accepted for Phase 1.

## Context

Phase 1 introduces prompt skills. Skills need to be reproducible during long
sessions, survive context compression, and avoid being silently changed by file
edits after session startup.

The runtime also needs to decide where active skill content appears in the model
input. Putting full skill content into normal conversation history makes it
vulnerable to compression and mixes normative instructions with observations.
Putting all active skill content into the stable system prompt can cause prompt
growth and makes compression boundaries harder to reason about.

## Decision

At session startup, after the Phase 1 database, session, prompt run, and session
artifact root exist, `SkillRegistry` snapshots supported prompt skill content and
computes stable content hashes. Skill discovery, snapshotting, persistence, and
available-skill header generation are startup-blocking; one-shot execution and
REPL input must not accept a user prompt until the frozen skill registry snapshot
is persisted.

Skill registry snapshots are persisted separately from
`sessions.config_snapshot_json` and associated with the session and prompt run.
The session config snapshot records configuration and policy facts; the skill
registry snapshot records skill manifests, hashes, `SKILL.md` content, and
file-level reference snapshots.

The frozen session snapshot is the execution truth. Activation validates the
requested skill against the frozen snapshot and stored content hash; it does not
re-read source files. File changes after session startup do not change active
session behavior, including first activation behavior.

Phase 1 supports:

- required `SKILL.md`.
- file-level references under `references/**`.

Reference files are frozen at startup. Large references are artifact-backed, and
all reference content hashes participate in the skill content hash so source-file
edits after session startup do not change active-session behavior.

Active skills are stored as structured runtime state:

- skill id.
- content hash or version.
- activation reason.
- scope.

The stable system block contains runtime safety, the main agent prompt, and a
stable formatter/header for active skill context. Dynamic skill instructions are
not appended to `ReplRuntime.conversation` and do not mutate the stable system
prompt.

For each model call, `PromptComposer` generates a runtime-supplied active skill
context block from the frozen `SKILL.md` body inside the per-call
`ModelContextFrame`. This block is marked as authoritative for that turn,
uses `role="system"` and `kind="runtime_active_skill_context"`, and is placed
before rolling summary and retained raw conversation so recent raw history and
live messages remain contiguous. It is not stable system prompt content, is not
durable conversation history, and is not part of `/compress` input. Phase 1
`AgentRunRequest` carries the complete `ModelContextFrame` in
`model_context_frame`; the older `system_prompt`, `conversation`, and
`user_input` fields are not independent context truth. Phase 1 also does not
use a separate `AgentRunRequest.tools` field as prompt/context truth; provider
tool bindings are materialized from `ModelContextFrame.tool_schema_bindings`.

Phase 1 does not implement section-level progressive disclosure, semantic
reference retrieval, or automatic active-skill disclosure degradation.

Reference files are not injected automatically. The model may call the brokered
runtime tool `load_skill_ref_file(skill_name, path)` to load one frozen
reference file for an already active skill. Loaded reference output is ordinary
durable conversation history and may later be omitted or compressed.

Skill context may include model-visible guidance such as `allowed_tools` or
`path_policy`, but those fields are not authorization. Actual authorization is
decided only by runtime and `ToolBroker`.

Phase 1 does not implement `deactivate_skill` and does not automatically
deactivate active skills. Under context budget pressure, runtime relies on
omission, compression, and explicit context-limit failure rather than silently
reducing or removing active skill instructions.

## Alternatives Considered

### Store skill content as normal conversation messages

This keeps the loaded skill near recent history, but compression would need
special handling to avoid summarizing or deleting normative instructions. It
also makes recovery depend on natural-language transcript state.

### Put all active skill content into the stable system prompt

This makes instructions high priority, but dynamic activation can cause
unbounded system prompt growth and blurs the stable system prompt boundary.

### Automatically inject all reference files

This makes references immediately useful, but it can turn a single skill
activation into a large permanent context increase. Phase 1 instead keeps
references frozen and available through `load_skill_ref_file`.

### Automatically deactivate oldest skills under token pressure

This controls prompt size, but it silently removes behavior constraints and
makes later failures harder to explain.

## Consequences

- Skill behavior is reproducible from the frozen session snapshot and active
  skill refs.
- Skill activation is not affected by source-file edits after session startup.
- `/compress` does not rewrite or delete skill bodies.
- Token estimates must use `ModelContextFrame`, not raw conversation history.
- Prompt composition becomes a first-class runtime responsibility.
- Skill tests focus on hash stability, activation, `SKILL.md` injection,
  reference file loading, compression survival, and audit.

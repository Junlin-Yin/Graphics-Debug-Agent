# Phase 2 Scope

## Goal

Phase 2 delivers brokered `view_image` vision analysis and runtime-owned Todo
Plan state.

The phase solves the two v1 gaps needed before RenderDoc readiness work:

- the agent can inspect exported PNG/JPEG images through a controlled
  model-visible tool.
- the agent has structured plan continuity that survives ordinary context
  compression and does not depend on natural-language conversation history.

Phase 2 extends the Phase 1 runtime, ToolBroker, persistence, and
`ModelContextFrame` contracts. It does not introduce subagents, workflow, MCP,
plugin packaging, session interruption, or RenderDoc readiness e2e behavior.

## Must Implement

- `view_image`:
  - expose `view_image` as a model-visible native tool only when enabled by
    frozen multimodal tool availability.
  - execute `view_image` only through `ToolBroker`.
  - enforce Phase 1 path policy, approval mode, audit, timeout, artifact
    handling, and standardized `ToolResult` behavior.
  - accept one to four authorized local PNG/JPEG paths per call.
  - reject remote URLs, artifact ids, and unsupported image inputs.
  - support an optional `query` field; provider calls must always use an
    effective query, falling back to a runtime-owned default when omitted, and
    enforce a frozen configurable query length limit.
  - record source image metadata: MIME type, byte size, SHA-256, width, height,
    and normalized path, with Pillow as the runtime image metadata parser.
  - call a separately configured OpenAI-compatible multimodal provider for
    `kimi-k2.5` through Chat Completions.
  - resolve and freeze multimodal provider configuration in the session config
    snapshot at startup, using the same no-secret snapshot discipline as the main
    model config.
  - expose `view_image` only when frozen multimodal provider configuration is
    complete and valid at session startup; otherwise freeze `view_image` as
    disabled for the session and keep it out of the model-visible tool set.
  - enforce Kimi-compatible request limits before the provider call: one to four
    PNG/JPEG images, each image no larger than 4096 pixels on either side and no
    more than 4096 * 2160 total pixels, and projected Chat Completions request
    body no larger than 100,000,000 bytes, measured from the compact
    wire-equivalent JSON request body that will be sent to the provider after
    SDK request-extension fields are merged.
  - require the vision provider response to parse as a JSON object with runtime-
    validated semantic fields, including a frozen configurable analysis length
    limit; invalid JSON or invalid fields are `model_error`.
  - keep image bytes, base64, and provider image content parts out of the main
    prompt agent's durable conversation history.
  - return only concise semantic analysis, display image metadata, and error
    facts to the main agent.
  - allow assistant-authored `view_image` tool-call arguments to contain
    `query`, because that is how the model supplies the analysis focus, but do
    not copy the concrete effective query text into runtime-authored persisted
    audit metadata, trace output, engine log, context snapshot metadata, or
    `ToolResult.metadata`; record only whether the effective query source was
    runtime default or assistant-provided tool input.
  - write trace/audit facts for tool call, policy decision, image metadata,
    vision provider/model, latency, success/failure, and analysis summary.
  - never write image base64 into runtime events, trace output, ordinary
    conversation history, or engine log.
- Todo Plan:
  - add a run-scoped Todo Plan runtime truth owned by the runtime.
  - expose model-visible `todo` tool for whole-plan replacement.
  - execute `todo` only through `ToolBroker`.
  - persist Todo Plan state independently from conversation history and
    compression summary.
  - write dedicated runtime events for plan replacement.
  - inject the current Todo Plan into every ordinary task `ModelContextFrame`.
  - include injected Todo Plan content in token estimation and context window
    accounting.
  - keep Todo Plan visible after automatic compression and manual `/compress`
    without relying on compression summary reconstruction.
  - expose Todo Plan state in `status` and `trace` enough to explain runtime
    continuity and plan changes.
- Compatibility:
  - bump SQLite `PRAGMA user_version` for Phase 2.
  - fail closed for missing, legacy, unknown, or non-Phase-2 schema versions
    before startup, active ownership checks, `status`, or `trace` interpret
    runtime truth.
  - do not migrate, delete, or rewrite old `.sessions/runtime.db`.
  - return `config_error` with a user-facing message instructing the user to
    move or remove `.sessions/` or use a fresh workspace.

## Must Not Implement

- `AgentRegistry`.
- `/agents`.
- `/models`.
- subagents or the brokered `task` tool.
- workflow runtime, workflow skills, workflow handoff, or workflow resume.
- MCP server lifecycle, MCP tool discovery, or MCP tool invocation.
- plugin packaging.
- RenderDoc readiness e2e, fake `rdc` scenario, or Windows `rdc` smoke.
- session interruption, `/cancel`, terminalization, or `resume`.
- remote image URLs.
- image cache hits or tool-call cache.
- Anthropic-compatible vision path.
- fallback vision path.
- arbitrary image formats beyond PNG and JPEG.
- artifact id input for `view_image`.
- automatic copying of local image path inputs into `ArtifactStore`.
- image base64 or raw image bytes in ordinary history, trace output, or event
  payloads.
- shader-specific runtime validators, RenderDoc command allowlists, Ralph Loop
  state machines, or business report schemas.

## Phase 2 Runtime Contract Additions

Phase 2 adds the model-visible `todo` tool and the conditionally model-visible
`view_image` tool. `view_image` is visible only when enabled by frozen
multimodal tool availability.

Phase 2 adds run-scoped Todo Plan runtime truth. Todo Plan is authoritative
runtime state and must not be inferred from `ReplRuntime.conversation`,
compression summaries, model output, trace rendering, or UI state.

Phase 2 adds the `todo_updated` runtime event for Todo Plan changes. `view_image`
uses the existing `tool_call_started`, `tool_call_completed`, `tool_call_failed`,
and `tool_call_denied` ToolBroker audit events with Phase 2 metadata fields; it
does not add separate `view_image_*` event kinds.

Phase 2 adds multimodal provider configuration under
`~/.debug-agent/config.toml`. This configuration is operational runtime config,
not agent policy.

Multimodal configuration is tool availability configuration. Missing or invalid
multimodal configuration must not fail session startup by itself. Instead,
runtime freezes `view_image` as disabled for the session, records the disabled
reason in the no-secret config snapshot, and omits `view_image` from
model-visible tool bindings. Config changes and environment changes do not
hot-reload into an active session.

Phase 2 does not add new shared error classes. It reuses existing project error
classes, including `user_error`, `config_error`, `policy_denied`, `tool_error`,
`model_error`, `timeout`, `cancelled`, and `internal_error`, for the new tool and
plan failure modes.

## Compatibility

Phase 2 is a schema and tool-contract breaking change from Phase 1.

Runtime initialization, `debug-agent status`, `debug-agent trace`, and active
workspace ownership checks must read SQLite `PRAGMA user_version` before
interpreting runtime truth rows. A missing (`0`), Phase 0, Phase 0.5, Phase 1,
unknown, or otherwise mismatched version fails closed with
`error_class="config_error"`.

If `.sessions/runtime.db` does not exist, Phase 2 creates it with the Phase 2
schema and writes `PHASE_2_SCHEMA_USER_VERSION = 2` before interpreting runtime
rows.

The user-facing legacy-schema error must say that older runtime databases are
unsupported by Phase 2 and instruct the user to move or remove `.sessions/` or
use a fresh workspace.

Runtime must not automatically migrate, delete, or rewrite legacy databases.

## ADR Impact

Phase 2 adds ADR 0013 for runtime-owned Todo Plan continuity. ADR 0013 records
the architecture decision that Todo Plan is authoritative run-scoped continuity
state, not conversation history, compression summary, UI state, workflow, or
task graph state.

Phase 2 does not require changing the accepted decisions in ADR 0010 or ADR
0011.

ADR 0010 already defines `ModelContextFrame` as the runtime-owned LLM-visible
ordinary task request boundary. Phase 2 narrows this by adding Todo Plan as a
non-persistent runtime segment in ordinary task frames.

ADR 0011 already states that compression summaries are LLM-visible continuity,
not authoritative runtime truth. Phase 2 narrows this by requiring Todo Plan to
remain outside compression input and outside summary-based restoration.

Phase 2 does not require a new ADR for `view_image`. The design is a concrete
tool-contract addition under the existing ToolBroker boundary, frozen config
snapshot strategy, and runtime-enforced path/approval policy decisions.

If a later phase changes Todo Plan resume semantics or makes vision
observations part of an executable recovery source, that later phase must
re-evaluate ADR 0010 and ADR 0011.

## Minimum Runnable Slice

1. User starts a REPL or one-shot session in a fresh workspace.
2. Runtime initializes the Phase 2 database, validates schema version, freezes
   config, policy, and multimodal tool availability, and exposes the Phase 2
   model-visible tool set.
3. The model calls `todo` with a small task plan.
4. Runtime validates the plan, writes Todo Plan runtime truth, emits plan
   events, and returns a compact `ToolResult`.
5. The next ordinary model call receives the current Todo Plan through
   `ModelContextFrame` injection.
6. The model calls `todo` again to rewrite the list when task status changes.
7. Runtime persists the replacement and keeps the plan visible after `/compress`.
8. If `view_image` is enabled by the frozen multimodal config, the model calls
   `view_image` on one or more authorized local PNG/JPEG paths.
9. `ToolBroker` validates input, applies policy and approval, computes image
   metadata, calls the configured multimodal provider, records audit facts, and
   returns structured JSON-derived analysis.
10. `debug-agent trace <session_id>` shows plan changes and `view_image`
    metadata/analysis summary without image base64.

## Completion Definition

Phase 2 is complete when:

- all Phase 2 acceptance criteria in `tests.md` pass.
- `operations.md` canonical verification commands have been run as applicable.
- legacy Phase 1 databases fail closed with the Phase 2 compatibility error.
- `docs/phase-2/implementation-plan.md` has been created and approved before
  implementation work starts.

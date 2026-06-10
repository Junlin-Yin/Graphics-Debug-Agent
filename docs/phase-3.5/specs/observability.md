# Phase 3.5 Observability Specification

## Boundary

Phase 3.5 changes the human-facing observability surfaces for prompt sessions:

- `trace.md` becomes a conversation transcript rendered from durable
  `conversation_messages`.
- `logs/engine.log` is renamed to `logs/events.jsonl`.

Both files are non-authoritative observability outputs. Runtime truth remains in
SQLite runtime tables, checkpoint payloads, and artifact records. `status`,
`trace`, `resume`, checkpoint validation, and recovery must not reconstruct
runtime truth from Markdown or JSONL files.

This spec is generic runtime observability work. It does not add RenderDoc,
Ralph Loop, shader-specific trace formats, workflow, subagent, MCP, plugin, PTY,
or long-running shell semantics.

## File Layout

Phase 3.5 writes:

| File | Path | Content |
| --- | --- | --- |
| conversation trace | `.sessions/<session_id>/logs/trace.md` | user, assistant, tool interaction, and runtime fact transcript. |
| event log | `.sessions/<session_id>/logs/events.jsonl` | run events, audit facts, and runtime diagnostics as JSON Lines. |
| runtime truth | `.sessions/runtime.db` | authoritative session, run, conversation, checkpoint, artifact, and audit truth. |

Phase 3.5 must not generate, migrate, copy, or symlink these legacy paths:

- `.sessions/<session_id>/trace.md`
- `.sessions/<session_id>/logs/engine.log`

Legacy files left under `.sessions/` after startup schema reset may remain on
disk for manual cleanup, but Phase 3.5 must not interpret them as runtime truth
or use them as compatibility inputs.

## Conversation Trace Purpose

`trace.md` is a human-readable conversation history. It is not an audit log, not
a checkpoint, not runtime truth, and not a recovery input.

The trace body contains only:

- user messages.
- assistant final messages.
- assistant tool-call messages with paired tool results.
- durable runtime failure or cancellation facts.

Ordinary run events such as `model_call_started`, `checkpoint_written`,
`approval_requested`, and `context_optimized` must not appear in the trace body.
They remain visible through `events.jsonl` when written there.

## Top Format

`trace.md` starts with:

```markdown
# debug-agent conversation trace

*Exported on 2026-06-09T12:00:00Z*
```

The export timestamp is UTC ISO.

`trace.md` must not write hidden HTML metadata or stale-detection fields,
including:

- `terminal_checkpoint_id`
- `trace_schema_version`
- `conversation_high_watermark`
- `latest_message_sha256`
- `event_count`
- `latest_event_id`
- `events_high_watermark`

`debug-agent trace <session_id>` must rebuild directly from the database. It
must not decide staleness from Markdown metadata.

## Session Information

The session summary is intentionally small:

```markdown
**📊 Session Information**
- **Session ID**: `sess_...`
- **Run ID**: `run_...`
- **Workspace**: `/path/to/workspace`
- **Status**: `completed`
- **Terminal Reason**: `terminal_completion`
- **Started**: 2026-06-09T...
- **Last Updated**: 2026-06-09T...
- **Approval Mode**: `normal`
- **Total Messages**: 42
- **Total User Messages**: 8
- **Total Assistant Messages**: 8
- **Total Tool Calls**: 26
```

The session summary must not include:

- `Trace Source`
- `Events Log`
- raw event count
- latest event id
- checkpoint validation internals
- approval grant internals
- context compression internals

Counting rules:

- `Total Messages` counts rendered durable conversation rows and excludes
  filtered `context_summary` rows.
- `Total User Messages` counts rendered `user_input` rows.
- `Total Assistant Messages` counts rendered `assistant_output` and
  `assistant_tool_call` rows.
- `Total Tool Calls` counts `assistant_tool_call.content.tool_calls[]` items and
  does not count `tool_result` rows.

## Non-ASCII Output

Phase 3.5 trace output intentionally uses emoji section headings and does not
provide an ASCII-only variant:

- `**📊 Session Information**`
- `## 👤 User`
- `## 🤖 Assistant`
- `### 🔧 Tool Calls`
- `## ⚠️ Runtime Fact`

Tool status icons are:

| Status | Icon |
| --- | --- |
| `ok`, `completed`, `success` | ✅ |
| `error`, `failed`, `denied` | ❌ |
| `timeout` | ⏱️ |
| `cancelled` | ⏹️ |
| unknown | ❓ |

## Data Source

The trace body renders only from append-only durable conversation rows ordered
by `conversation_messages.message_index ASC`. It must not use event timestamps
to interleave or reorder messages.

Renderable rows:

| Role | Kind | Rendered | Form |
| --- | --- | --- | --- |
| `user` | `user_input` | yes | `## 👤 User` |
| `assistant` | `assistant_output` | yes | `## 🤖 Assistant` |
| `assistant` | `assistant_tool_call` | yes | `## 🤖 Assistant` plus `### 🔧 Tool Calls` |
| `tool` | `tool_result` | yes | folded into the paired tool call item |
| `runtime` | `failure_fact` | yes | `## ⚠️ Runtime Fact` |
| `runtime` | `cancellation_fact` | yes | `## ⚠️ Runtime Fact` |
| `runtime` | `context_summary` | no | filtered with no replacement notice |

Before rendering, `debug-agent trace <session_id>` must run the Phase 3.5 schema
version gate and durable conversation validation. It must fail closed and leave
the existing `logs/trace.md` unchanged when validation fails.

Validation failures include:

- unsupported role or kind.
- accepted row is not in a closed group.
- group position is duplicated or not contiguous.
- group completeness source is missing.
- tool result cannot be uniquely paired to an assistant tool call by
  `model_call_id + tool_call_id`.
- artifact-backed row has a missing, conflicting, or checksum-invalid artifact
  source.

## Message Rendering

User messages render as:

```markdown
---

## 👤 User
*2026-06-09T10:00:00Z* • **Message Index**: 1 • **Turn**: `turn-1`

user content
```

Assistant final messages render as:

```markdown
---

## 🤖 Assistant
*2026-06-09T10:00:03Z* • **Message Index**: 5 • **Turn**: `turn-1`

assistant content
```

Rules:

- timestamp comes from `accepted_at` and is rendered as UTC ISO.
- message index always comes from `message_index`.
- turn id is shown when `turn_id` is present.
- user and assistant content comes from `content_json.content`.
- user and assistant content is written as-is, without fencing, escaping, or
  Markdown sanitization.

Trace is optimized for convenient human reading, not tamper-resistant audit.
If user or assistant content includes Markdown headings, horizontal rules, HTML
comments, unclosed code fences, or other Markdown control text, the renderer
still writes the durable content as-is.

Phase 3.5 trace must not render model name, token counts, or `unknown`
placeholders by pulling model events back into the trace body.

## Tool Call Rendering

Assistant tool-call groups render as:

```markdown
---

## 🤖 Assistant
*2026-06-09T10:00:01Z* • **Message Index**: 2 • **Turn**: `turn-1`

### 🔧 Tool Calls

**✅ read_file** (`read_file`)
- **Status**: `ok`
- **Call ID**: `model_call_1_tool_1`
- **Tool Result Index**: 3
- **Timestamp**: 2026-06-09T10:00:02Z
- **Arguments**:
  ```json
  {
    "path": "/repo/README.md",
    "limit": 100
  }
  ```
- **Result**:
  ```text
  # README preview...
  ```
```

Fields:

| Field | Source | Rule |
| --- | --- | --- |
| assistant timestamp | `assistant_tool_call.accepted_at` | required. |
| status | paired `tool_result` content or metadata | required. |
| call id | `tool_call_id` | required. |
| tool result index | paired `tool_result.message_index` | required. |
| timestamp | paired `tool_result.accepted_at` | required. |
| arguments | `assistant_tool_call.content.tool_calls[].args` | pretty JSON in original tool-call order. |
| result | `tool_result.content.content` or artifact ref | preview-limited. |
| artifacts | ToolResult artifacts or durable artifact refs | shown when present. |

Tool call/result pairing rules:

- every assistant tool call must find exactly one paired `tool_result` in the
  same accepted model-call sequence.
- the sequence is identified by durable `conversation_messages.model_call_id`.
- a tool-call item and tool result pair by identical `tool_call_id`.
- missing `model_call_id`, mismatched `model_call_id`, missing tool result,
  duplicate tool result, cross-sequence pairing, or orphan tool result is a
  durable conversation validation failure.
- when one assistant tool-call message contains multiple tool calls, trace
  renders them in `assistant_tool_call.content.tool_calls[]` order.

`view_image.query` redaction is deliberately split:

- trace can render `query` when it appears in assistant-authored raw tool-call
  arguments in `assistant_tool_call.content.tool_calls[].args`.
- runtime-authored audit metadata, `events.jsonl`, status output, error
  metadata, `ToolResult.metadata`, approval scope, and other persisted
  runtime-authored fields must not copy concrete query text, query preview, or
  query length.

## Runtime Facts

`failure_fact` and `cancellation_fact` rows render as:

```markdown
---

## ⚠️ Runtime Fact
*2026-06-09T10:05:00Z* • **Message Index**: 9 • **Kind**: `cancellation_fact`

`cancelled/user_cancel_running`: User cancelled the running turn.
```

`context_summary` rows are filtered and no "summary omitted" replacement text is
rendered.

## Result Preview

Tool result previews are limited by both characters and lines:

| Limit | Value |
| --- | ---: |
| maximum characters | 4000 chars |
| maximum lines | 100 lines |

Rules:

- if either limit is exceeded, truncate and mark the preview with
  `[truncated]`.
- truncation happens only in the preview layer and never modifies durable
  conversation content.
- JSON or dict outputs are pretty-printed before preview limits are applied.
- text outputs apply preview limits to the original text.

Artifact-backed content rules:

- do not read artifact content to generate a preview.
- display only `artifact_id` and relative artifact path.
- if the durable `tool_result` already stores a redacted inline preview, trace
  may display that inline preview.
- do not read an unredacted artifact to generate or enrich the trace.
- do not add checksum or other extra artifact verification hints to the trace.

## Context Compression

Trace must preserve original messages from before context compression:

- render from the full append-only `conversation_messages` rows.
- do not render from current `conversation_projection_state`.
- do not render from terminal checkpoint projection snapshots.
- filter `context_summary` rows.
- keep `context_optimized`, `compression_failed`, and `context_limit_exceeded`
  events only in `events.jsonl`.

## Generation

Phase 3.5 has one trace generation strategy: full rebuild.

After a terminal recovery checkpoint is successfully written, runtime must
rebuild the full conversation trace from `conversation_messages` and overwrite:

```text
.sessions/<session_id>/logs/trace.md
```

Manual `debug-agent trace <session_id>` must also rebuild the full trace from
the database and overwrite the same path.

Generation rules:

- never append to an existing trace.
- never incrementally update from old trace file contents.
- never depend on Markdown high-watermarks or hidden metadata.
- write to a temporary file in the same `logs/` directory and atomically replace
  `logs/trace.md`.
- automatic terminal trace refresh and manual trace generation may run
  concurrently.
- concurrent trace writes are last-writer-wins, but the final file must be one
  complete render, never interleaved or partial output.
- atomic replace failure is a trace render/write failure.

Automatic trace failure after terminal checkpoint success:

- must not roll back the terminal checkpoint.
- must not block session/run terminalization.
- must not block ownership release.
- must not write a run event, conversation row, checkpoint, or other runtime
  truth.
- does not go through runtime audit.
- must not change the original workflow exit code or lifecycle outcome.
- may be reported through the current CLI output or UI.

## Manual Trace Command

`debug-agent trace <session_id>`:

- may run against a running session.
- renders only accepted closed durable conversation rows.
- must not include mid-flight model, tool, provider, or shell state.
- is a read-only observability command except for overwriting
  `logs/trace.md`.
- must not create a session, start a run, resume a session, terminalize a
  session/run, release ownership, execute stale fail-close, run a model, or
  execute tools.
- must not claim active workspace ownership.
- must not block just because another runner is active.
- must not require the target session to be idle.
- must fail with a standardized error on SQLite busy or read failure without
  waiting, taking ownership, or changing active owner state.

Manual trace failure rules:

- missing session or missing DB uses the existing lookup-not-found behavior.
- schema/version gate failure uses Phase 3.5 compatibility reasons:
  `config_error/legacy_schema_version`,
  `config_error/unknown_schema_version`, or
  `config_error/schema_version_missing`.
- durable conversation validation failure uses
  `persistence_error/conversation_cut_invalid` and must not overwrite the
  existing `logs/trace.md`.
- artifact reference failure uses `persistence_error/artifact_missing`.
- SQLite/read path failure uses `persistence_error/persistence_read_failed`.
- trace Markdown render/write failure uses `ui_error/trace_render_failed`.
- failures are returned through CLI exit code and message, not persisted as
  runtime truth.
- `ui_error/trace_render_failed` for manual trace after lookup succeeds maps to
  `ERROR_TRACE_RENDER = 11`.

## Resume And Re-Terminalization

The same session/run lineage can terminalize, resume, continue, and terminalize
again. Trace generation must always fully rebuild from ordered accepted rows:

| Scenario | Required behavior |
| --- | --- |
| first terminal checkpoint | render message indexes `1..N`. |
| resume then continue | append new messages to the same run with increasing `message_index`. |
| second terminal checkpoint | overwrite `logs/trace.md` from message indexes `1..M`. |
| multiple resume/terminal cycles | every terminal checkpoint rebuilds the full ordered transcript. |
| manual trace | rebuilds the current complete accepted conversation. |
| compression before resume | still reads all `conversation_messages` and filters `context_summary`. |

Forbidden:

- append to old trace.
- derive new trace from old trace content.
- reorder conversation rows by event timestamp.
- render compression summary as original conversation.

## `events.jsonl`

`events.jsonl` has the same authority as the previous `engine.log`: it is a
non-authoritative observability JSONL stream.

Rules:

- path is `.sessions/<session_id>/logs/events.jsonl`.
- no legacy `logs/engine.log` compatibility is implemented.
- JSONL entry schema remains unchanged for the first Phase 3.5 version.
- the writer class is renamed from `EngineLogWriter` to `EventsJsonlWriter`;
  Phase 3.5 code and tests must not keep the legacy class name as the canonical
  writer.
- `write_event_log` writes persisted run-event observations with
  `metadata.event_id`.
- `write_runtime_log` writes runtime diagnostic observations.
- both are for human debugging and log inspection only.
- neither can be used by `status`, `trace`, `resume`, checkpoint validation, or
  runtime recovery as truth.

JSONL entry shape:

```json
{
  "timestamp": "...",
  "session_id": "sess_...",
  "run_id": "run_...",
  "step_id": null,
  "level": "INFO",
  "event": "tool_call_completed",
  "message": "tool_call_completed ...",
  "metadata": {"payload": {}, "event_id": "evt_..."}
}
```

## Example

```markdown
# debug-agent conversation trace

*Exported on 2026-06-09T12:00:00Z*

**📊 Session Information**
- **Session ID**: `sess_abc`
- **Run ID**: `run_abc`
- **Workspace**: `/Users/xinzhu/Workspace/MyAgent`
- **Status**: `completed`
- **Terminal Reason**: `terminal_completion`
- **Started**: 2026-06-09T11:00:00Z
- **Last Updated**: 2026-06-09T11:05:00Z
- **Total Messages**: 4
- **Total Tool Calls**: 1

---

## 👤 User
*2026-06-09T11:00:01Z* • **Message Index**: 1 • **Turn**: `turn-1`

Please read README.

---

## 🤖 Assistant
*2026-06-09T11:00:02Z* • **Message Index**: 2 • **Turn**: `turn-1`

### 🔧 Tool Calls

**✅ read_file** (`read_file`)
- **Status**: `ok`
- **Call ID**: `model_call_1_tool_1`
- **Tool Result Index**: 3
- **Timestamp**: 2026-06-09T11:00:03Z
- **Arguments**:
  ```json
  {
    "path": "/Users/xinzhu/Workspace/MyAgent/README.md"
  }
  ```
- **Result**:
  ```text
  # Shader-Debug-Agent
  ```

---

## 🤖 Assistant
*2026-06-09T11:00:04Z* • **Message Index**: 4 • **Turn**: `turn-1`

The README title is `Shader-Debug-Agent`.
```

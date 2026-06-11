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

Phase 3.5 intentionally supersedes earlier trace event-timeline expectations for
prompt sessions. Runtime/admin timeline facts such as `todo_updated`,
`stale_fail_closed`, `session_resumed`, `run_resumed`, checkpoint writes, resume
attempts, and approval internals are not rendered as standalone trace body
entries. They remain inspectable through `events.jsonl` when written there. A
`todo` mutation may appear in trace only through the durable assistant tool call
and paired `tool_result` transcript rows.

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

`Terminal Reason` is rendered only when the session has a terminal reason.
Manual trace against a running session omits this line instead of rendering an
unknown or placeholder value.

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
| `ok` | ✅ |
| `error`, `denied` | ❌ |
| `timeout` | ⏱️ |
| `cancelled` | ⏹️ |

This table is a presentation mapping only. It does not add or remove allowed
`ToolResult.status` values. Phase 3.5 allowed `ToolResult.status` values are
`ok`, `error`, `denied`, `timeout`, and `cancelled`; any unrecognized status in
corrupt or manually modified data is a durable conversation validation failure,
not a renderable trace value.

## Data Source

The trace body renders only from append-only durable conversation rows ordered
by `conversation_messages.message_index ASC`. It must not use event timestamps
to interleave or reorder messages.

Renderable rows:

| Role | Kind | Rendered | Form |
| --- | --- | --- | --- |
| `user` | `user_input` | yes | `## 👤 User` |
| `assistant` | `assistant_output` | yes | `## 🤖 Assistant` |
| `assistant` | `assistant_tool_call` | yes | `## 🤖 Assistant`, optional assistant text, plus `### 🔧 Tool Calls` |
| `tool` | `tool_result` | yes | folded into the paired tool call item |
| `runtime` | `failure_fact` | yes | `## ⚠️ Runtime Fact` |
| `runtime` | `cancellation_fact` | yes | `## ⚠️ Runtime Fact` |
| `runtime` | `context_summary` | no | filtered with no replacement notice |

Before rendering, `debug-agent trace <session_id>` must run the Phase 3.5 schema
version gate and trace-render validation over the durable conversation rows it
will render. Trace-render validation is not full recovery-grade resume or
checkpoint validation and does not read artifact body content to verify
artifact payload checksums. It must fail closed and leave the existing
`logs/trace.md` unchanged when validation fails.

Manual and automatic trace rendering must read the session, run, durable
conversation rows, artifact records, and any summary counts from one consistent
SQLite read transaction/snapshot. This is required even when manual trace runs
against a running session, because the renderer must not combine a session
summary from one database point-in-time with conversation rows or artifact
validation from another. If the persistence layer cannot obtain the bounded
consistent read snapshot, manual trace uses the SQLite busy or read-failure
mapping defined below and leaves the existing `logs/trace.md` unchanged.

Trace-render validation is deliberately narrower than Phase 3 resume validation.
Resume and terminal checkpoint validation remain responsible for recovery-grade
conversation fact-cut, projection, checkpoint, and artifact checksum validation.
Trace validates only the durable rows, pairings, artifact records/references,
session/run scope, referenced artifact file existence, and presentation shapes
needed to produce a non-authoritative human-readable transcript.

Manual trace against a running session renders the current accepted closed
durable conversation snapshot. Normal runtime must not expose open groups in
`conversation_messages`; any accepted row with `group_status != "closed"` is
corrupt or invariant-violating durable truth and must fail closed rather than
selecting a partial high-watermark.

Trace-render validation failures include:

- unsupported role or kind.
- accepted row is not in a closed group.
- group position is duplicated or not contiguous.
- group completeness source is missing.
- tool result cannot be uniquely paired to an assistant tool call by
  `model_call_id + tool_call_id`.
- paired tool result uses an unsupported `ToolResult.status` value.
- inline paired `tool_result.content_json.status` is missing, or
  `metadata_json.status` is present and conflicts with `content_json.status`.
  For Phase 3.5 native tool results, `content_json.status` is the canonical
  status source.
- artifact-backed row has a missing or conflicting durable artifact
  record/reference. Trace validation verifies artifact record/reference metadata
  and referenced file existence only. It must not read large artifact body
  content to compute checksums, generate previews, or enrich previews.

Artifact validation failure mapping:

- missing artifact record, missing referenced artifact file, or missing artifact
  reference uses `persistence_error/artifact_missing`.
- conflicting artifact metadata, invalid artifact-backed conversation source
  shape, or artifact/session/run mismatch uses
  `persistence_error/conversation_cut_invalid`.
- checksum-invalid artifact body content is not detected by trace rendering in
  Phase 3.5 because trace is non-authoritative observability, not recovery truth.

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
- inline user and assistant content comes from `content_json.content`.
- inline user and assistant content is written as-is, without fencing, escaping,
  or Markdown sanitization.
- artifact-backed user and assistant rows are valid durable conversation rows,
  but Phase 3.5 trace does not read artifact body content to reconstruct their
  full Markdown. It renders an artifact reference using the durable artifact id,
  relative artifact path, and any already-inline preview/reference metadata. If
  that metadata is missing or conflicts with the ArtifactStore record, trace
  validation fails closed using the artifact validation mapping in this spec.

Trace is optimized for convenient human reading, not tamper-resistant audit.
If user or assistant content includes Markdown headings, horizontal rules, HTML
comments, unclosed code fences, or other Markdown control text, the renderer
still writes the durable content as-is. Phase 3.5 explicitly accepts that such
content can visually mimic trace section structure in the Markdown presentation;
SQLite runtime truth, run events, artifact records, and checkpoint payloads
remain the authoritative audit and recovery sources.

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
    {
      "path": "/repo/README.md",
      "limit": 100
    }
- **Result**:
    # README preview...
```

Sensitive write/edit tool arguments render redacted values instead of content:

{
  "path": "/repo/app.py",
  "content": {
    "redacted": true,
    "sha256": "hex",
    "bytes": 12345
  }
}

Trace must not wrap tool arguments, tool results, redacted argument objects, or
artifact reference previews in Markdown fenced code blocks. Argument and result
previews are rendered as indented plain preview blocks under their labels. The
renderer prefixes every preview line with indentation after preview truncation
and redaction, so Markdown headings, horizontal rules, HTML, and backtick
sequences inside the preview cannot escape into trace section structure.

Fields:

| Field | Source | Rule |
| --- | --- | --- |
| assistant timestamp | `assistant_tool_call.accepted_at` | required. |
| status | paired inline `tool_result.content_json.status` | required and canonical for Phase 3.5 native tool results. |
| call id | `tool_call_id` | required. |
| tool result index | paired `tool_result.message_index` | required. |
| timestamp | paired `tool_result.accepted_at` | required. |
| arguments | `assistant_tool_call.content.tool_calls[].args` | redacted where required, pretty JSON in original tool-call order, preview-limited. |
| result | durable `tool_result.content_json.content` or artifact reference object from the Phase 3.5 ToolResult serialization contract | preview-limited. |
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
- when `assistant_tool_call.content` contains an optional non-empty string
  `text` field alongside `tool_calls[]`, trace renders that text as-is before
  `### 🔧 Tool Calls`. If assistant tool-call text is present in any
  non-normalized location, or if `tool_calls[]` is missing or malformed, trace
  validation fails closed instead of silently dropping accepted assistant
  content.

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

## Result And Argument Preview

Tool result previews and tool argument blocks are limited by both characters and
lines:

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
- argument preview limits apply after tool-specific trace redaction has been
  applied.

Tool argument redaction rules:

- `write_file.content` is replaced before rendering with:

{
  "redacted": true,
  "sha256": "hex",
  "bytes": 12345
}

- `edit_file.old_text` and `edit_file.new_text` are replaced with the same
  redacted object shape.
- trace does not render content previews for these three fields.
- redaction uses UTF-8 bytes for the `bytes` value and SHA-256 input.
- this is presentation-only redaction. It does not modify durable conversation
  content, model-visible tool observations, ToolResult payloads, audit events, or
  checkpoint truth.

Artifact-backed content rules:

- do not read artifact content to generate a preview or verify body checksums.
- display only `artifact_id`, relative artifact path, and any already-inline
  preview/reference metadata. The relative path comes from the durable artifact
  reference object's `relative_path` when present, or from the matching
  ArtifactStore record during validation.
- validate Phase 3.5 field-level artifact reference objects inside inline
  `tool_result.content_json.content` using the same ArtifactStore record checks
  as artifact-backed rows. Each referenced `artifact_id` must have a matching
  durable ArtifactStore record, a matching session/run scope, a matching
  `relative_path`, and an existing referenced file. The durable
  `tool_result.artifact_ids` list must include every inline field-level artifact
  reference and must not contain unrelated ids.
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
- write to a unique temporary file in the same `logs/` directory and atomically
  replace `logs/trace.md`.
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
- must not write `events.jsonl`, including non-authoritative runtime diagnostic
  observations.
- must not change the original workflow exit code or lifecycle outcome.
- may be reported through the current CLI output or UI.
- in REPL/TUI, must be shown as an error block because the trace refresh is a
  user-visible observability failure even though runtime truth is already
  terminalized.

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
- must not wait for an active runner, claim ownership, or change active owner
  state.
- must render session summary counts, accepted conversation rows, and artifact
  validation from one consistent SQLite read transaction/snapshot.
- SQLite reads may use the current persistence layer's bounded busy handling. If
  SQLite remains busy after that bounded handling, manual trace returns
  `persistence_error/sqlite_busy_timeout`. Ordinary read failures return
  `persistence_error/persistence_read_failed`.

Manual trace failure rules:

- missing session or missing DB uses the existing lookup-not-found behavior.
- schema/version gate failure uses Phase 3.5 compatibility reasons:
  `config_error/legacy_schema_version`,
  `config_error/unknown_schema_version`, or
  `config_error/schema_version_missing`.
- durable conversation validation failure uses
  `persistence_error/conversation_cut_invalid` and must not overwrite the
  existing `logs/trace.md`.
- missing artifact reference, missing artifact record, missing inline
  field-level artifact reference target, or missing artifact content uses
  `persistence_error/artifact_missing`.
- conflicting artifact metadata, mismatched field-level artifact reference
  metadata, or mismatch between inline artifact refs and `artifact_ids` uses
  `persistence_error/conversation_cut_invalid`.
- SQLite busy uses `persistence_error/sqlite_busy_timeout`.
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
- **Approval Mode**: `normal`
- **Total Messages**: 4
- **Total User Messages**: 1
- **Total Assistant Messages**: 2
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
    {
      "path": "/Users/xinzhu/Workspace/MyAgent/README.md"
    }
- **Result**:
    # Shader-Debug-Agent

---

## 🤖 Assistant
*2026-06-09T11:00:04Z* • **Message Index**: 4 • **Turn**: `turn-1`

The README title is `Shader-Debug-Agent`.
```

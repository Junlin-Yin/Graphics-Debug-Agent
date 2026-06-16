# Phase 4 Thinking Specification

## Boundary

Phase 4 adds narrow main-agent thinking support for the existing
Anthropic-compatible Kimi model path.

The selected Phase 4 strategy is:

- allow users to enable thinking through frozen `config.toml`.
- allow users to configure `effort`.
- use thinking only inside the current main model call.
- discard all provider thinking content before runtime accepts assistant
  content.
- never replay, summarize, persist, compress, display, or audit thinking text.

This is not provider-generic reasoning support. It does not implement signed
thinking replay, redacted thinking, thinking retention, thinking summaries,
thinking trace artifacts, adaptive retention windows, or cross-call thinking
continuity.

## Config Shape

Phase 4 adds:

```toml
[thinking]
enabled = false
effort = "high"
```

Defaults:

| Key | Default | Validation | Meaning |
| --- | --- | --- | --- |
| `thinking.enabled` | `false` | boolean | Enables thinking request options for main agent model calls. |
| `thinking.effort` | `"high"` | fixed enum | Effort hint passed only when thinking is enabled. |

Allowed `effort` values are:

- `"low"`
- `"medium"`
- `"high"`

`thinking.effort` is valid even when `thinking.enabled = false`. In that case
the value freezes into the session config snapshot but has no runtime effect.
This combination must not fail closed.

Invalid value types or unsupported effort strings use
`config_error/invalid_runtime_config`.

## Frozen Snapshot

The resolved thinking config is frozen into `sessions.config_snapshot_json`.

Fresh sessions, fake-provider sessions, and real-provider sessions must use the
same snapshot shape:

```json
{
  "thinking": {
    "enabled": false,
    "effort": "high"
  }
}
```

Resume uses the original frozen snapshot. It must not read current
`config.toml` to rebuild thinking settings.

When Phase 4 startup upgrades a Phase 3.5 `user_version = 4` database to
`user_version = 5`, existing session snapshots that do not contain a
`thinking` object are backfilled with:

```json
{
  "thinking": {
    "enabled": false,
    "effort": "high"
  }
}
```

That backfilled value becomes the frozen thinking config for those upgraded
sessions, including later explicit resume. The upgrade must not infer thinking
settings from current mutable `config.toml`.

The v4-to-v5 startup upgrade must not rewrite existing terminal recovery
checkpoint payloads, `payload_sha256`, checkpoint manifests, or frozen snapshot
checksum fields. Phase 3.5 checkpoints may contain a config checksum computed
before the `thinking` object existed. During Phase 4 resume validation, runtime
first validates the stored config checksum against the full upgraded Phase 4
snapshot. If that fails and the upgraded snapshot contains exactly:

```json
{
  "thinking": {
    "enabled": false,
    "effort": "high"
  }
}
```

runtime retries config checksum validation against the Phase 3.5-compatible
canonical config shape with only that default `thinking` object omitted.

This fallback is limited to frozen config checksum validation. It does not
rewrite the checkpoint, does not change `payload_sha256`, does not remove
`thinking` from `sessions.config_snapshot_json`, and does not permit any
non-default thinking value to validate against a Phase 3.5 checksum. Runtime
continues to use the upgraded frozen config with disabled thinking for later
model calls and resume behavior.

Adding this frozen snapshot shape requires Phase 4 SQLite
`PRAGMA user_version = 5`.

## Request Projection

Thinking applies only to main agent model calls.

When frozen `thinking.enabled = false`, runtime must not send thinking request
options and must not send `effort`.

When frozen `thinking.enabled = true`, runtime sends Kimi-compatible thinking
request options for the main model call and includes the frozen `effort` value.
The request projection may use the SDK-supported representation for the current
Anthropic-compatible adapter path, but it must include an explicit thinking
enable option. Passing `effort` alone is not sufficient to enable thinking.

For the current LangChain Anthropic-compatible adapter path, the expected
projection is equivalent to:

```python
thinking={"type": "enabled"}
effort="<frozen-effort>"
```

Phase 4 does not apply thinking to:

- `view_image` provider calls.
- context compression model calls.
- schema validation helpers.
- deployment smoke commands.
- fake provider tests unless a test explicitly exercises thinking projection.

`view_image` keeps the Phase 2 Kimi thinking-disabled request behavior.

## Response Handling

Providers may return assistant content as a list of content blocks, including:

```json
[
  {
    "type": "thinking",
    "thinking": "...",
    "signature": null
  },
  {
    "type": "text",
    "text": "..."
  },
  {
    "type": "tool_use",
    "name": "...",
    "input": {}
  }
]
```

Runtime must remove every content block with `type = "thinking"` before
accepting assistant content.

Accepted assistant content may retain:

- text blocks.
- tool use blocks.
- tool call ids, names, and arguments required for provider/tool pairing.

Thinking content must not enter:

- `conversation_messages`.
- model-visible tool-call continuation messages.
- future main model calls.
- context compression input.
- terminal recovery checkpoint projection.
- resume projection.
- `trace.md`.
- `events.jsonl`.
- `run_metrics_*.json`.
- TUI/REPL streaming display.
- assistant final text.
- tool result pairing metadata.
- audit truth.
- token estimation inputs.

Thinking content may exist only in transient in-memory provider parsing state
before the runtime strips it.

Provider usage may include thinking-token cost inside ordinary
`output_tokens`/`total_tokens`, but Phase 4 must not derive token usage from
thinking text and must not persist separate reasoning-token metrics.

## Tool Choice Limitation

When Anthropic-compatible thinking is enabled, provider or SDK layers may not
support forced tool use. They may drop or reject forced `tool_choice`
parameters.

Phase 4 runtime and tests must not rely on provider-forced tool choice when
thinking is enabled. The model may still choose tools naturally from bound tool
schemas, and runtime must still execute accepted tool calls through ToolBroker.

This limitation does not change ToolBroker policy. It only constrains provider
request assumptions.

## Non-Goals

Phase 4 thinking does not:

- expose thinking text to users.
- write thinking trace artifacts.
- replay thinking blocks across tool calls or turns.
- require signed thinking.
- validate thinking signatures.
- support Claude native signed-thinking replay.
- support `redacted_thinking`.
- make thinking part of durable runtime truth.
- add a provider-generic reasoning abstraction.
- change `view_image` thinking behavior.
